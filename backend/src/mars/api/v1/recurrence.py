"""Configurable positive-to-positive recurrence analysis API.

Every saved result is tied to an immutable definition, evidence dataset and
authorised scope. Aggregate projections and patient evidence deliberately use
different permission and sensitivity gates.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import Field

from mars.api.dependencies import (
    InvestigationServiceDep,
    RecurrenceAnalysisDep,
    RecurrenceExecutorDep,
    require_permissions,
    require_sensitivity,
)
from mars.api.v1.schemas import InvestigationDetail, MarsModel
from mars.domain.enums import LifecycleStatus
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.services.recurrence_analysis_service import (
    PROJECTIONS,
    LiveScope,
    PatientPageFilters,
    RunSubmission,
)

router = APIRouter(prefix="/recurrence", tags=["recurrence analysis"])

Viewer = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]
CaseViewer = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.CASE_EVIDENCE_VIEW)),
]
DefinitionManager = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.CONFIGURATION_MANAGE)),
]


class DefinitionCreate(MarsModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    parameters: dict[str, Any]
    note: str | None = Field(default=None, max_length=2000)


class DefinitionVersionCreate(MarsModel):
    parameters: dict[str, Any]
    expected_latest_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)


class DefinitionVersionView(MarsModel):
    id: uuid.UUID
    definition_id: uuid.UUID
    version_number: int
    parameters: dict[str, Any]
    checksum: str
    engine_version: str
    created_by: str
    created_at: datetime
    note: str | None


class DefinitionView(MarsModel):
    id: uuid.UUID
    name: str
    description: str | None
    mode: Literal["exploratory"]
    owner_username: str
    created_at: datetime
    versions: list[DefinitionVersionView]


class ProgrammeStatusView(MarsModel):
    method_code: str
    active: bool
    detail: str
    method_version_id: uuid.UUID | None = None
    semantic_version: str | None = None
    parameters: dict[str, Any] | None = None
    effective_from: date | None = None
    approved_by: str | None = None


class PromoteDefinitionRequest(MarsModel):
    semantic_version: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=4000)


class MethodTransitionRequest(MarsModel):
    target: LifecycleStatus
    reason: str = Field(min_length=1, max_length=4000)
    effective_from: date | None = None


class MethodVersionView(MarsModel):
    id: uuid.UUID
    semantic_version: str
    status: str
    owner: str
    parameters: dict[str, Any] | None = None
    effective_from: date | None = None
    approved_by: str | None = None


class RunCreate(MarsModel):
    period_start: date
    period_end: date
    source: Literal["stored_encounter", "live_tracker"] = "stored_encounter"
    mode: Literal["exploratory", "programme"] = "exploratory"
    definition_version_id: uuid.UUID | None = None
    parameters: dict[str, Any] | None = None
    filters: dict[str, list[str]] | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="Africa/Kampala", min_length=1, max_length=64)


class RunView(MarsModel):
    id: uuid.UUID
    status: Literal["queued", "running", "completed", "failed"]
    error_code: str | None
    mode: str
    source: str
    definition_name: str
    definition: dict[str, Any]
    definition_checksum: str
    definition_version_id: uuid.UUID | None
    method_version_id: uuid.UUID | None
    exploratory: bool
    engine_version: str
    period_start: date
    period_end: date
    timezone: str
    facility_count: int | None
    national_scope: bool
    filters: dict[str, Any]
    manifest_checksum: str
    created_by: str
    created_at: datetime
    completed_at: datetime | None
    progress_completed: int
    progress_total: int
    dataset: dict[str, Any] | None
    summary: dict[str, Any] | None
    coverage: dict[str, Any] | None
    interpretation: str


class ProjectionView(MarsModel):
    run_id: uuid.UUID
    name: str
    rows: Any
    unit: str | None
    coverage: dict[str, Any] | None
    facility_names: dict[str, str]
    interpretation: str


class PatientPageView(MarsModel):
    run_id: uuid.UUID
    items: list[dict[str, Any]]
    total: int
    limit: int
    next_cursor: str | None
    previous_cursor: str | None
    filters: dict[str, list[str]]


class PatientDetailView(MarsModel):
    run_id: uuid.UUID
    finding: dict[str, Any]
    timeline: dict[str, Any]
    definition_name: str
    definition: dict[str, Any]
    engine_version: str
    exploratory: bool
    interpretation: str


class DuplicatePageView(MarsModel):
    run_id: uuid.UUID
    items: list[dict[str, Any]]
    total: int
    limit: int
    next_cursor: str | None
    previous_cursor: str | None


class CompareRequest(MarsModel):
    run_a: uuid.UUID
    run_b: uuid.UUID


class PatientInvestigationRequest(MarsModel):
    idempotency_key: str | None = Field(default=None, max_length=128)


def _live_scope(request: Request) -> LiveScope | None:
    settings = request.app.state.settings
    raw_id = request.cookies.get(settings.session_cookie_name)
    dashboard = getattr(request.app.state, "live_dashboard", None)
    if not raw_id or dashboard is None or not hasattr(dashboard, "recurrence_scope"):
        return None
    resolved = dashboard.recurrence_scope(raw_id)
    if resolved is None:
        return None
    scope_key, facilities = resolved
    refs = tuple(sorted(str(item["id"]) for item in facilities if item.get("id")))
    names = {str(item["id"]): str(item.get("name") or "Authorised facility") for item in facilities}
    geography = {
        str(item["id"]): str(item["parent_id"])
        for item in facilities
        if item.get("id") and item.get("parent_id")
    }
    return LiveScope(
        scope_key=scope_key,
        namespace="live-session",
        facility_refs=refs,
        facility_names=names,
        facility_geography=geography,
    )


def _method_shape(version: Any) -> MethodVersionView:
    return MethodVersionView(
        id=version.id,
        semantic_version=version.semantic_version,
        status=version.status.value,
        owner=version.owner,
        parameters=version.parameters,
        effective_from=version.effective_from,
        approved_by=version.approved_by,
    )


@router.get("/programme", response_model=ProgrammeStatusView)
def programme(service: RecurrenceAnalysisDep, _principal: Viewer) -> ProgrammeStatusView:
    return ProgrammeStatusView.model_validate(service.programme_status())


@router.get("/definitions", response_model=list[DefinitionView])
def definitions(service: RecurrenceAnalysisDep, principal: Viewer) -> list[DefinitionView]:
    return [
        DefinitionView.model_validate(service.definition_shape(record, versions))
        for record, versions in service.list_definitions(principal)
    ]


@router.post("/definitions", response_model=DefinitionView, status_code=status.HTTP_201_CREATED)
def create_definition(
    body: DefinitionCreate,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
) -> DefinitionView:
    record, _version = service.create_definition(
        principal,
        name=body.name,
        description=body.description,
        parameters=body.parameters,
        note=body.note,
    )
    return DefinitionView.model_validate(
        service.definition_shape(record, service.versions(record.id))
    )


@router.post(
    "/definitions/{definition_id}/versions",
    response_model=DefinitionVersionView,
    status_code=status.HTTP_201_CREATED,
)
def create_definition_version(
    definition_id: uuid.UUID,
    body: DefinitionVersionCreate,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
) -> DefinitionVersionView:
    version = service.add_version(
        principal,
        definition_id,
        parameters=body.parameters,
        expected_latest_version=body.expected_latest_version,
        note=body.note,
    )
    return DefinitionVersionView.model_validate(service.version_shape(version))


@router.post(
    "/definition-versions/{version_id}/promote",
    response_model=MethodVersionView,
    status_code=status.HTTP_201_CREATED,
)
def promote_definition(
    version_id: uuid.UUID,
    body: PromoteDefinitionRequest,
    service: RecurrenceAnalysisDep,
    principal: DefinitionManager,
) -> MethodVersionView:
    return _method_shape(
        service.promote(
            principal,
            version_id,
            semantic_version=body.semantic_version,
            summary=body.summary,
        )
    )


@router.post("/programme/versions/{version_id}/transition", response_model=MethodVersionView)
def transition_programme_method(
    version_id: uuid.UUID,
    body: MethodTransitionRequest,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
) -> MethodVersionView:
    return _method_shape(
        service.transition_method(
            principal,
            version_id,
            target=body.target,
            reason=body.reason,
            effective_from=body.effective_from,
        )
    )


@router.post("/runs", response_model=RunView, status_code=status.HTTP_202_ACCEPTED)
def submit_run(
    body: RunCreate,
    request: Request,
    service: RecurrenceAnalysisDep,
    executor: RecurrenceExecutorDep,
    principal: Viewer,
) -> RunView:
    live_scope = _live_scope(request) if body.source == "live_tracker" else None
    run, token = service.submit(
        principal,
        RunSubmission(
            period_start=body.period_start,
            period_end=body.period_end,
            source=body.source,
            mode=body.mode,
            definition_version_id=body.definition_version_id,
            parameters=body.parameters,
            filters=body.filters,
            idempotency_key=body.idempotency_key,
            timezone=body.timezone,
        ),
        live_scope=live_scope,
    )
    if token is not None:
        executor.start(
            run.id,
            token,
            facility_names=live_scope.facility_names if live_scope else None,
            facility_geography=live_scope.facility_geography if live_scope else None,
        )
    return RunView.model_validate(service.run_shape(run))


@router.get("/runs", response_model=list[RunView])
def runs(
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RunView]:
    live_scope = _live_scope(request)
    return [
        RunView.model_validate(service.run_shape(run))
        for run in service.list_runs(
            principal,
            limit=limit,
            live_scope_key=live_scope.scope_key if live_scope else None,
        )
    ]


@router.get("/runs/{run_id}", response_model=RunView)
def run(
    run_id: uuid.UUID,
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
) -> RunView:
    live_scope = _live_scope(request)
    record = service.get_run(
        principal, run_id, live_scope_key=live_scope.scope_key if live_scope else None
    )
    return RunView.model_validate(service.run_shape(record))


@router.get("/runs/{run_id}/projections/{name}", response_model=ProjectionView)
def projection(
    run_id: uuid.UUID,
    name: Literal[
        "summary", "intervals", "facilities", "weekly", "frequency", "treatments", "geography"
    ],
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
) -> ProjectionView:
    assert name in PROJECTIONS
    live_scope = _live_scope(request)
    return ProjectionView.model_validate(
        service.projection(
            principal,
            run_id,
            name,
            live_scope_key=live_scope.scope_key if live_scope else None,
        )
    )


def _values(values: list[str] | None) -> frozenset[str]:
    return frozenset(values or ())


@router.get(
    "/runs/{run_id}/patients",
    response_model=PatientPageView,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def patients(
    run_id: uuid.UUID,
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: CaseViewer,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    determination: Annotated[list[str] | None, Query()] = None,
    sex: Annotated[list[str] | None, Query()] = None,
    age_group: Annotated[list[str] | None, Query()] = None,
    quality: Annotated[list[str] | None, Query()] = None,
    facility: Annotated[list[str] | None, Query()] = None,
    test_method: Annotated[list[str] | None, Query()] = None,
    treatment: Annotated[list[str] | None, Query()] = None,
    investigation_status: Annotated[list[str] | None, Query()] = None,
) -> PatientPageView:
    live_scope = _live_scope(request)
    return PatientPageView.model_validate(
        service.patients_page(
            principal,
            run_id,
            limit=limit,
            cursor=cursor,
            filters=PatientPageFilters(
                determinations=_values(determination),
                sexes=_values(sex),
                age_groups=_values(age_group),
                quality=_values(quality),
                facility_refs=_values(facility),
                test_methods=_values(test_method),
                treatments=_values(treatment),
                investigation_statuses=_values(investigation_status),
            ),
            live_scope_key=live_scope.scope_key if live_scope else None,
        )
    )


@router.get(
    "/runs/{run_id}/patients/{patient_alias}",
    response_model=PatientDetailView,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def patient_detail(
    run_id: uuid.UUID,
    patient_alias: Annotated[str, Path(pattern=r"^MARS-PT2-[A-Z2-7]+$")],
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: CaseViewer,
) -> PatientDetailView:
    live_scope = _live_scope(request)
    return PatientDetailView.model_validate(
        service.patient_detail(
            principal,
            run_id,
            patient_alias,
            live_scope_key=live_scope.scope_key if live_scope else None,
        )
    )


@router.get(
    "/runs/{run_id}/duplicates",
    response_model=DuplicatePageView,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def duplicates(
    run_id: uuid.UUID,
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: CaseViewer,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    kind: Annotated[list[str] | None, Query()] = None,
) -> DuplicatePageView:
    live_scope = _live_scope(request)
    return DuplicatePageView.model_validate(
        service.duplicates(
            principal,
            run_id,
            limit=limit,
            cursor=cursor,
            kinds=_values(kind),
            live_scope_key=live_scope.scope_key if live_scope else None,
        )
    )


@router.post("/compare", response_model=dict[str, Any])
def compare(
    body: CompareRequest,
    request: Request,
    service: RecurrenceAnalysisDep,
    principal: Viewer,
) -> dict[str, Any]:
    live_scope = _live_scope(request)
    return service.compare(
        principal,
        body.run_a,
        body.run_b,
        live_scope_key=live_scope.scope_key if live_scope else None,
    )


@router.post(
    "/runs/{run_id}/patients/{patient_alias}/investigation",
    response_model=InvestigationDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def open_patient_investigation(
    run_id: uuid.UUID,
    patient_alias: Annotated[str, Path(pattern=r"^MARS-PT2-[A-Z2-7]+$")],
    body: PatientInvestigationRequest,
    request: Request,
    service: RecurrenceAnalysisDep,
    investigations: InvestigationServiceDep,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(
            require_permissions(Permission.CASE_EVIDENCE_VIEW, Permission.INVESTIGATION_TRIAGE)
        ),
    ],
) -> InvestigationDetail:
    live_scope = _live_scope(request)
    run_record, finding = service.finding_for_review(
        principal,
        run_id,
        patient_alias,
        live_scope_key=live_scope.scope_key if live_scope else None,
    )
    facility_id: uuid.UUID | None = None
    if finding.final_facility_ref:
        try:
            facility_id = uuid.UUID(finding.final_facility_ref)
        except ValueError:
            facility_id = None
    if facility_id is None and len(principal.facility_scopes) == 1:
        # Live Tracker uses the remote organisation-unit UID in the frozen
        # finding. A facility-scoped principal already carries the confirmed
        # local crosswalk, so retain that scope on the investigation rather
        # than creating work the same officer cannot read back.
        facility_id = next(iter(principal.facility_scopes))
    geography_unit_id = (
        principal.geography_scopes[0].geography_unit_id if principal.geography_scopes else None
    )
    record = investigations.open_patient_finding(
        principal,
        patient_finding_id=finding.id,
        period_start=run_record.period_start,
        period_end=run_record.period_end,
        geography_unit_id=geography_unit_id,
        facility_id=facility_id,
        idempotency_key=body.idempotency_key,
    )
    return InvestigationDetail.model_validate(investigations.detail_shape(record))


__all__ = ["router"]
