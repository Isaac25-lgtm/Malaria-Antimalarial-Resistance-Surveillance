"""Definitions, reproducible runs, projections, comparison and governed promotion.

The flow of one Apply:

1. **Submit.** The request is validated - definition, dates, filters and the
   configured resource ceilings (``RecurrenceCeilings.check``) - before any work
   is queued. The caller's *current* authorised scope is resolved and frozen,
   with the definition, dates and filters, into an immutable manifest. A retried
   submission with the same idempotency key returns the same run.
2. **Execute**, in the background. Stored evidence is loaded for the frozen
   scope, or the retained live dataset for the frozen live scope is opened. The
   dataset is published (or found) by fingerprint, evaluated by the shared
   engine, and findings, projections and duplicate review are written once. A
   database trigger then freezes the run.
3. **Read.** Every read re-checks the caller's *current* scope and permissions:
   a saved run is not a permanent grant to its creator's former scope.
   Aggregate projections need aggregate permission. The patient list, patient
   timelines and duplicate review need case-evidence permission and the
   pseudonymous sensitivity tier.

Programme mode reads the active version of the governed method
``MAL-RECURRENCE-P2P``. Promotion drafts a real method version through the
existing registry; approval and activation follow its lifecycle, need
``method:approve``, and are refused to the person who drafted the version.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.core.errors import (
    ConflictError,
    GeographyScopeDeniedError,
    NotFoundError,
    PermissionDeniedError,
    SensitivityScopeDeniedError,
    ValidationFailedError,
)
from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.domain.enums import AuditAction, LifecycleStatus, MethodKind
from mars.domain.governance import MethodDefinition, MethodVersion
from mars.domain.investigation import Investigation
from mars.domain.longitudinal import (
    DEFAULT_TIMEZONE,
    ENGINE_VERSION,
    EXPLORATORY_PRESET,
    INTERPRETATION_LIMIT,
    CanonicalEncounter,
    DefinitionError,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    Medication,
    PatientKey,
    RecurrenceCeilings,
    patient_display_alias,
)
from mars.domain.longitudinal_codec import decode_encounter, decode_medication
from mars.domain.recurrence_analysis import (
    ClinicalEvidenceDataset,
    PatientRecurrenceFinding,
    RecurrenceAnalysisRun,
    RecurrenceDefinition,
    RecurrenceDefinitionVersion,
)
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService
from mars.services.audit_service import AuditService
from mars.services.clinical_evidence_store import ClinicalEvidenceStore, EvidenceCipher
from mars.services.governance_service import MethodRegistryService
from mars.services.patient_surveillance import page_rows
from mars.services.recurrence_outputs import (
    RunFilters,
    build_duplicate_rows,
    build_findings,
    build_projections,
    compare_projections,
    compare_run_findings,
    manifest_checksum,
    patient_facts,
    restrict,
)
from mars.services.stored_encounter_adapter import STORED_MAPPING_VERSION, STORED_NAMESPACE
from mars.services.stored_evidence import load_stored_evidence

logger = get_logger(__name__)

PROGRAMME_METHOD_CODE = "MAL-RECURRENCE-P2P"
#: The frozen scope of an unrestricted (national) caller.
NATIONAL_SCOPE = "*"
SOURCES = ("stored_encounter", "live_tracker")
MODES = ("exploratory", "programme")
PROJECTIONS = (
    "summary",
    "intervals",
    "facilities",
    "weekly",
    "frequency",
    "treatments",
    "geography",
)

_FINDING_COLUMNS = (
    PatientRecurrenceFinding.id,
    PatientRecurrenceFinding.run_id,
    PatientRecurrenceFinding.patient_alias,
    PatientRecurrenceFinding.determination,
    PatientRecurrenceFinding.chain_length,
    PatientRecurrenceFinding.eligible_positive_count,
    PatientRecurrenceFinding.observed_positive_count,
    PatientRecurrenceFinding.chain_interval_days,
    PatientRecurrenceFinding.first_positive_on,
    PatientRecurrenceFinding.latest_positive_on,
    PatientRecurrenceFinding.final_date,
    PatientRecurrenceFinding.final_facility_ref,
    PatientRecurrenceFinding.in_period_facility_refs,
    PatientRecurrenceFinding.facility_refs,
    PatientRecurrenceFinding.quality_flags,
    PatientRecurrenceFinding.treatment_names,
    PatientRecurrenceFinding.test_methods,
    PatientRecurrenceFinding.cross_facility,
    PatientRecurrenceFinding.sex,
    PatientRecurrenceFinding.age_group,
)


class RunFailure(Exception):
    """A controlled reason a run could not complete; the code is safe to store."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RunSubmission:
    period_start: date
    period_end: date
    source: str = "stored_encounter"
    mode: str = "exploratory"
    definition_version_id: uuid.UUID | None = None
    parameters: Mapping[str, Any] | None = None
    filters: Mapping[str, Sequence[str]] | None = None
    idempotency_key: str | None = None
    timezone: str = DEFAULT_TIMEZONE


@dataclass(frozen=True, slots=True)
class LiveScope:
    """The caller's *current* live source scope, resolved from their session."""

    scope_key: str
    namespace: str
    facility_refs: tuple[str, ...]
    facility_names: Mapping[str, str] = field(default_factory=dict)
    facility_geography: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PatientPageFilters:
    """Server-side filters over a run's listed patients. ``none`` is a status."""

    determinations: frozenset[str] = frozenset()
    sexes: frozenset[str] = frozenset()
    age_groups: frozenset[str] = frozenset()
    quality: frozenset[str] = frozenset()
    facility_refs: frozenset[str] = frozenset()
    test_methods: frozenset[str] = frozenset()
    treatments: frozenset[str] = frozenset()
    investigation_statuses: frozenset[str] = frozenset()

    def admits(self, row: PatientRecurrenceFinding, status: str | None) -> bool:
        checks = (
            (self.determinations, {row.determination}),
            (self.sexes, {row.sex}),
            (self.age_groups, {row.age_group}),
            (self.quality, set(row.quality_flags)),
            (self.test_methods, set(row.test_methods)),
            (self.treatments, set(row.treatment_names)),
            (self.investigation_statuses, {status or "none"}),
            (
                self.facility_refs,
                set(row.in_period_facility_refs)
                | ({row.final_facility_ref} if row.final_facility_ref else set()),
            ),
        )
        return all(not wanted or bool(wanted & present) for wanted, present in checks)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "determinations": sorted(self.determinations),
            "sexes": sorted(self.sexes),
            "age_groups": sorted(self.age_groups),
            "quality": sorted(self.quality),
            "facility_refs": sorted(self.facility_refs),
            "test_methods": sorted(self.test_methods),
            "treatments": sorted(self.treatments),
            "investigation_statuses": sorted(self.investigation_statuses),
        }


def ceilings(settings: Settings) -> RecurrenceCeilings:
    return RecurrenceCeilings(
        maximum_window_days=settings.recurrence_max_window_days,
        maximum_positive_encounters=settings.recurrence_max_positive_encounters,
        maximum_period_days=settings.recurrence_max_period_days,
        maximum_filter_values=settings.recurrence_max_filter_values,
    )


def stored_scope_key(facility_refs: Sequence[str]) -> str:
    return hashlib.sha256(
        json.dumps(["stored_encounter", sorted(facility_refs)]).encode()
    ).hexdigest()


def finding_aad(run_id: uuid.UUID, finding_id: uuid.UUID, alias: str, key_version: str) -> str:
    return f"finding:{run_id}:{finding_id}:{alias}:{key_version}"


def duplicates_aad(run_id: uuid.UUID, key_version: str) -> str:
    return f"duplicates:{run_id}:{key_version}"


def _require(principal: AuthenticatedPrincipal, permission: Permission) -> None:
    if permission not in principal.permissions:
        raise PermissionDeniedError(f"This action requires the {permission.value} permission.")


def _require_case_evidence(principal: AuthenticatedPrincipal) -> None:
    _require(principal, Permission.CASE_EVIDENCE_VIEW)
    if not principal.max_sensitivity.covers(SensitivityLevel.PSEUDONYMOUS_CASE):
        raise SensitivityScopeDeniedError("Patient findings need the pseudonymous case tier.")


def medication_shape(medication: Medication) -> dict[str, Any]:
    """Every recorded medicine field, and the availability of the rest."""
    return {
        "name": medication.name,
        "evidence": medication.evidence.value,
        "formulation": medication.formulation,
        "dose": medication.dose,
        "dose_unit": medication.dose_unit,
        "quantity": medication.quantity,
        "frequency": medication.frequency,
        "duration_days": medication.duration_days,
        "prescribed_on": medication.prescribed_on.isoformat() if medication.prescribed_on else None,
        "dispensed_on": medication.dispensed_on.isoformat() if medication.dispensed_on else None,
        "dispensed_quantity": medication.dispensed_quantity,
        "availability": {
            key: value.value for key, value in sorted(medication.availability.items())
        },
    }


def timeline_shape(detail: Mapping[str, Any], facility_names: Mapping[str, str]) -> dict[str, Any]:
    """A chronological care timeline with opaque references and no identifiers."""
    refs: Mapping[str, str] = detail["refs"]
    decisions: Mapping[str, Any] = detail["decisions"]
    entries: list[dict[str, Any]] = []
    previous: str | None = None
    for raw in detail["encounters"]:
        encounter: CanonicalEncounter = decode_encounter(raw)
        ref = refs[encounter.encounter_key]
        entries.append(
            {
                "ref": ref,
                "encounter_date": (
                    encounter.encounter_date.isoformat() if encounter.encounter_date else None
                ),
                "date_precision": encounter.date_precision.value,
                "facility_name": facility_names.get(encounter.facility_ref, "Authorised facility"),
                "facility_changed": previous is not None and encounter.facility_ref != previous,
                "tests": [
                    {
                        "method": test.method,
                        "result": test.outcome.value,
                        "observed_on": test.observed_on.isoformat() if test.observed_on else None,
                        "precision": test.precision.value,
                    }
                    for test in encounter.tests
                ],
                "diagnoses": list(encounter.diagnoses),
                "observations": list(encounter.observations),
                "fever": encounter.fever,
                "attendance": encounter.attendance,
                "referrals": list(encounter.referrals),
                "outcome": encounter.outcome,
                "attributes": [{"label": a, "value": b} for a, b in encounter.attributes],
                "medications": [medication_shape(item) for item in encounter.medications],
                "availability": {
                    key: value.value for key, value in sorted(encounter.availability.items())
                },
                "decision": decisions.get(ref),
            }
        )
        previous = encounter.facility_ref
    return {
        "entries": entries,
        "chain": detail["chain"],
        "intervals": detail["intervals"],
        "transitions": detail["transitions"],
        "explanation": detail["explanation"],
        "quality_flags": detail["quality_flags"],
        "unlinked_medications": [
            medication_shape(decode_medication(item))
            for item in detail.get("unlinked_medications", [])
        ],
    }


class RecurrenceAnalysisService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        audit: AuditService | None,
        *,
        cipher: EvidenceCipher,
        display_key: bytes,
    ) -> None:
        self._session = session
        self._settings = settings
        self._audit = audit
        self._cipher = cipher
        self._display_key = display_key
        self._scope = AnalyticsQueryService(session)

    # -- Validation -------------------------------------------------------
    def validate(
        self, parameters: Mapping[str, Any] | None, *, name: str
    ) -> FrozenRecurrenceDefinition:
        if parameters is None:
            return EXPLORATORY_PRESET
        try:
            definition = FrozenRecurrenceDefinition.from_parameters(parameters, name=name)
        except DefinitionError as error:
            raise ValidationFailedError(str(error)) from error
        if definition.engine_version != ENGINE_VERSION:
            raise ValidationFailedError(
                f"This definition was saved for engine {definition.engine_version}; this "
                f"deployment runs {ENGINE_VERSION}. Save a new version to run it."
            )
        return definition

    def _check_ceilings(
        self,
        definition: FrozenRecurrenceDefinition,
        query: FrozenAnalysisQuery | None,
        filter_values: int,
    ) -> None:
        probe = query or FrozenAnalysisQuery(date(2000, 1, 1), date(2000, 1, 1))
        try:
            ceilings(self._settings).check(definition, probe, filter_values=filter_values)
        except DefinitionError as error:
            raise ValidationFailedError(str(error)) from error

    # -- Definitions -------------------------------------------------------
    def create_definition(
        self,
        principal: AuthenticatedPrincipal,
        *,
        name: str,
        description: str | None,
        parameters: Mapping[str, Any],
        note: str | None = None,
    ) -> tuple[RecurrenceDefinition, RecurrenceDefinitionVersion]:
        _require(principal, Permission.SURVEILLANCE_VIEW_AGGREGATE)
        clean = name.strip()
        if not clean:
            raise ValidationFailedError("A definition needs a name.")
        definition = self.validate(parameters, name=clean)
        self._check_ceilings(definition, None, 0)
        record = RecurrenceDefinition(
            id=uuid.uuid4(),
            name=clean,
            description=description,
            mode="exploratory",
            owner_subject=principal.subject,
            owner_username=principal.username,
            archived=False,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError as error:
            raise ConflictError("You already have a definition with that name.") from error
        version = self._append_version(record, definition, principal, note=note, number=1)
        self._record(
            AuditAction.RECURRENCE_DEFINITION_SAVED,
            principal,
            "recurrence_definition",
            record.id,
            {"version": 1, "checksum": version.checksum},
        )
        return record, version

    def add_version(
        self,
        principal: AuthenticatedPrincipal,
        definition_id: uuid.UUID,
        *,
        parameters: Mapping[str, Any],
        expected_latest_version: int,
        note: str | None = None,
    ) -> RecurrenceDefinitionVersion:
        """A new immutable version, or the existing one with identical parameters.

        ``expected_latest_version`` is the optimistic-concurrency token: two
        saves racing from the same starting point cannot both become version N.
        """
        record = self._owned_definition(principal, definition_id, lock=True)
        versions = self.versions(record.id)
        latest = versions[-1].version_number if versions else 0
        if expected_latest_version != latest:
            raise ConflictError(
                "This definition gained a version since you loaded it. Reload and try again."
            )
        definition = self.validate(parameters, name=record.name)
        self._check_ceilings(definition, None, 0)
        same = next((item for item in versions if item.checksum == definition.checksum()), None)
        if same is not None:
            return same
        version = self._append_version(record, definition, principal, note=note, number=latest + 1)
        self._record(
            AuditAction.RECURRENCE_DEFINITION_SAVED,
            principal,
            "recurrence_definition",
            record.id,
            {"version": version.version_number, "checksum": version.checksum},
        )
        return version

    def _append_version(
        self,
        record: RecurrenceDefinition,
        definition: FrozenRecurrenceDefinition,
        principal: AuthenticatedPrincipal,
        *,
        note: str | None,
        number: int,
    ) -> RecurrenceDefinitionVersion:
        version = RecurrenceDefinitionVersion(
            id=uuid.uuid4(),
            definition_id=record.id,
            version_number=number,
            parameters=definition.parameters(),
            checksum=definition.checksum(),
            engine_version=definition.engine_version,
            created_by=principal.username,
            note=note,
        )
        try:
            with self._session.begin_nested():
                self._session.add(version)
                self._session.flush()
        except IntegrityError as error:
            raise ConflictError(
                "Another version of this definition was saved at the same moment. "
                "Reload and try again."
            ) from error
        return version

    def _owned_definition(
        self, principal: AuthenticatedPrincipal, definition_id: uuid.UUID, *, lock: bool = False
    ) -> RecurrenceDefinition:
        statement = select(RecurrenceDefinition).where(
            RecurrenceDefinition.id == definition_id,
            RecurrenceDefinition.owner_subject == principal.subject,
        )
        if lock:
            statement = statement.with_for_update()
        record = self._session.execute(statement).scalar_one_or_none()
        if record is None:
            raise NotFoundError("recurrence definition not found")
        return record

    def _owned_version(
        self, principal: AuthenticatedPrincipal, version_id: uuid.UUID
    ) -> tuple[RecurrenceDefinition, RecurrenceDefinitionVersion]:
        row = self._session.execute(
            select(RecurrenceDefinition, RecurrenceDefinitionVersion)
            .join(
                RecurrenceDefinitionVersion,
                RecurrenceDefinitionVersion.definition_id == RecurrenceDefinition.id,
            )
            .where(
                RecurrenceDefinitionVersion.id == version_id,
                RecurrenceDefinition.owner_subject == principal.subject,
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("recurrence definition version not found")
        return row[0], row[1]

    def versions(self, definition_id: uuid.UUID) -> list[RecurrenceDefinitionVersion]:
        return list(
            self._session.execute(
                select(RecurrenceDefinitionVersion)
                .where(RecurrenceDefinitionVersion.definition_id == definition_id)
                .order_by(RecurrenceDefinitionVersion.version_number)
            ).scalars()
        )

    def list_definitions(
        self, principal: AuthenticatedPrincipal
    ) -> list[tuple[RecurrenceDefinition, list[RecurrenceDefinitionVersion]]]:
        _require(principal, Permission.SURVEILLANCE_VIEW_AGGREGATE)
        records = list(
            self._session.execute(
                select(RecurrenceDefinition)
                .where(
                    RecurrenceDefinition.owner_subject == principal.subject,
                    RecurrenceDefinition.archived.is_(False),
                )
                .order_by(RecurrenceDefinition.name)
            ).scalars()
        )
        return [(record, self.versions(record.id)) for record in records]

    @staticmethod
    def version_shape(version: RecurrenceDefinitionVersion) -> dict[str, Any]:
        return {
            "id": version.id,
            "definition_id": version.definition_id,
            "version_number": version.version_number,
            "parameters": version.parameters,
            "checksum": version.checksum,
            "engine_version": version.engine_version,
            "created_by": version.created_by,
            "created_at": version.created_at,
            "note": version.note,
        }

    def definition_shape(
        self, record: RecurrenceDefinition, versions: Sequence[RecurrenceDefinitionVersion]
    ) -> dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "description": record.description,
            "mode": record.mode,
            "owner_username": record.owner_username,
            "created_at": record.created_at,
            "versions": [self.version_shape(version) for version in versions],
        }

    # -- Programme mode and governance ------------------------------------
    def programme_version(self) -> MethodVersion | None:
        return (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition, MethodDefinition.id == MethodVersion.method_definition_id)
                .where(
                    MethodDefinition.code == PROGRAMME_METHOD_CODE,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )

    def programme_status(self) -> dict[str, Any]:
        version = self.programme_version()
        if version is None:
            return {
                "method_code": PROGRAMME_METHOD_CODE,
                "active": False,
                "detail": (
                    "No approved programme recurrence method is active. Exploratory analysis "
                    "remains available and every result says it is exploratory."
                ),
            }
        name = f"{PROGRAMME_METHOD_CODE}@{version.semantic_version}"
        try:
            definition = self.validate(version.parameters or {}, name=name)
        except ValidationFailedError as error:
            return {
                "method_code": PROGRAMME_METHOD_CODE,
                "active": False,
                "detail": f"The active programme method cannot run on this engine: {error.detail}",
            }
        return {
            "method_code": PROGRAMME_METHOD_CODE,
            "active": True,
            "method_version_id": version.id,
            "semantic_version": version.semantic_version,
            "parameters": definition.parameters(),
            "effective_from": version.effective_from,
            "approved_by": version.approved_by,
            "detail": f"Programme method {name} is active.",
        }

    def promote(
        self,
        principal: AuthenticatedPrincipal,
        version_id: uuid.UUID,
        *,
        semantic_version: str,
        summary: str,
    ) -> MethodVersion:
        """Draft an official method version from a saved exploratory version.

        A draft only. Review, approval and activation are separate governed
        transitions, and the drafter cannot approve or activate it.
        """
        _require(principal, Permission.CONFIGURATION_MANAGE)
        if self._audit is None:
            raise ValidationFailedError("Governance changes require an audit trail.")
        _, version = self._owned_version(principal, version_id)
        registry = MethodRegistryService(self._session, self._audit)
        try:
            registry.get_method(PROGRAMME_METHOD_CODE)
        except NotFoundError:
            registry.register_method(
                code=PROGRAMME_METHOD_CODE,
                label="Configurable positive-to-positive malaria recurrence",
                kind=MethodKind.EPISODE_RULE,
                purpose=(
                    "Identifies patients with repeated confirmed positive malaria encounters "
                    "for review. Not a measure of treatment failure or resistance."
                ),
                owner=principal.username,
                principal=principal,
            )
        return registry.draft_version(
            code=PROGRAMME_METHOD_CODE,
            semantic_version=semantic_version,
            summary=summary,
            parameters={
                **version.parameters,
                "source_definition_version": {"id": str(version.id), "checksum": version.checksum},
            },
            artifact_reference="docs/methods/configurable-recurrence.md",
            artifact_checksum=version.checksum,
            owner=principal.username,
        )

    def transition_method(
        self,
        principal: AuthenticatedPrincipal,
        method_version_id: uuid.UUID,
        *,
        target: LifecycleStatus,
        reason: str,
        effective_from: date | None = None,
    ) -> MethodVersion:
        if self._audit is None:
            raise ValidationFailedError("Governance changes require an audit trail.")
        version = self._session.get(MethodVersion, method_version_id)
        if version is None or version.method.code != PROGRAMME_METHOD_CODE:
            raise NotFoundError("programme recurrence method version not found")
        if not reason.strip():
            raise ValidationFailedError("A governance transition records why.")
        approving = target in {
            LifecycleStatus.APPROVED,
            LifecycleStatus.ACTIVE,
            LifecycleStatus.RETIRED,
        }
        _require(
            principal, Permission.METHOD_APPROVE if approving else Permission.CONFIGURATION_MANAGE
        )
        if target in {LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE}:
            if version.owner == principal.username:
                raise PermissionDeniedError(
                    "The person who drafted a method version cannot approve or activate it."
                )
            self.validate(version.parameters or {}, name=version.qualified_version)
        if (
            target is LifecycleStatus.ACTIVE
            and effective_from is None
            and version.effective_from is None
        ):
            raise ValidationFailedError("Activating a method version needs an effective date.")
        registry = MethodRegistryService(self._session, self._audit)
        return registry.promote(
            version_id=version.id,
            target=target,
            principal=principal,
            reason=reason,
            effective_from=effective_from,
        )

    # -- Runs --------------------------------------------------------------
    def submit(
        self,
        principal: AuthenticatedPrincipal,
        request: RunSubmission,
        *,
        live_scope: LiveScope | None = None,
    ) -> tuple[RecurrenceAnalysisRun, str | None]:
        """Validate and freeze one Apply. Returns the run and a lease token.

        The token is ``None`` when an identical earlier submission is returned,
        so the caller does not start a second worker.
        """
        _require(principal, Permission.SURVEILLANCE_VIEW_AGGREGATE)
        if request.source not in SOURCES:
            raise ValidationFailedError(f"Unknown evidence source {request.source!r}")
        if request.mode not in MODES:
            raise ValidationFailedError(f"Unknown mode {request.mode!r}")
        definition, name, definition_version_id, method_version_id = self._resolve(
            principal, request
        )
        try:
            query = FrozenAnalysisQuery(request.period_start, request.period_end, request.timezone)
            filters = RunFilters.from_dict(request.filters)
        except DefinitionError as error:
            raise ValidationFailedError(str(error)) from error
        self._check_ceilings(definition, query, filters.value_count)
        facility_refs, scope_key = self._frozen_scope(principal, request.source, live_scope)
        if (
            filters.facility_refs
            and NATIONAL_SCOPE not in facility_refs
            and not filters.facility_refs <= set(facility_refs)
        ):
            raise GeographyScopeDeniedError(
                "The facility filter names facilities outside your authorised scope."
            )
        return self._new_run(
            mode=request.mode,
            source=request.source,
            definition=definition,
            name=name,
            definition_version_id=definition_version_id,
            method_version_id=method_version_id,
            query=query,
            filters=filters,
            facility_refs=facility_refs,
            scope_key=scope_key,
            subject=principal.subject,
            username=principal.username,
            idempotency_key=request.idempotency_key,
            principal=principal,
        )

    def submit_programme_run(
        self,
        version: MethodVersion,
        *,
        period_start: date,
        period_end: date,
        actor: str,
    ) -> tuple[RecurrenceAnalysisRun, str | None]:
        """A governed, national, stored-evidence run for a worker. No principal."""
        definition = self.validate(version.parameters or {}, name=version.qualified_version)
        query = FrozenAnalysisQuery(period_start, period_end)
        self._check_ceilings(definition, query, 0)
        refs = (NATIONAL_SCOPE,)
        return self._new_run(
            mode="programme",
            source="stored_encounter",
            definition=definition,
            name=version.qualified_version,
            definition_version_id=None,
            method_version_id=version.id,
            query=query,
            filters=RunFilters(),
            facility_refs=refs,
            scope_key=stored_scope_key(refs),
            subject=f"system:{actor}",
            username=actor,
            idempotency_key=f"{version.id}:{period_start.isoformat()}:{period_end.isoformat()}",
            principal=None,
        )

    def _new_run(
        self,
        *,
        mode: str,
        source: str,
        definition: FrozenRecurrenceDefinition,
        name: str,
        definition_version_id: uuid.UUID | None,
        method_version_id: uuid.UUID | None,
        query: FrozenAnalysisQuery,
        filters: RunFilters,
        facility_refs: tuple[str, ...],
        scope_key: str,
        subject: str,
        username: str,
        idempotency_key: str | None,
        principal: AuthenticatedPrincipal | None,
    ) -> tuple[RecurrenceAnalysisRun, str | None]:
        manifest = manifest_checksum(
            mode=mode,
            source=source,
            definition=definition.checksum(),
            engine=definition.engine_version,
            period_start=query.period_start.isoformat(),
            period_end=query.period_end.isoformat(),
            timezone=query.timezone,
            scope_key=scope_key,
            facility_refs=list(facility_refs),
            filters=filters.as_dict(),
            definition_version_id=str(definition_version_id) if definition_version_id else None,
            method_version_id=str(method_version_id) if method_version_id else None,
        )
        if idempotency_key:
            existing = self._idempotent(subject, idempotency_key)
            if existing is not None:
                if existing.manifest_checksum != manifest:
                    raise ConflictError(
                        "That idempotency key was already used for a different analysis."
                    )
                return existing, None
        token = str(uuid.uuid4())
        run = RecurrenceAnalysisRun(
            id=uuid.uuid4(),
            mode=mode,
            source_kind=source,
            definition_version_id=definition_version_id,
            method_version_id=method_version_id,
            definition_name=name,
            definition_parameters=definition.parameters(),
            definition_checksum=definition.checksum(),
            engine_version=definition.engine_version,
            period_start=query.period_start,
            period_end=query.period_end,
            timezone=query.timezone,
            scope_key=scope_key,
            facility_refs=list(facility_refs),
            filters=filters.as_dict(),
            manifest_checksum=manifest,
            created_by_subject=subject,
            created_by_username=username,
            idempotency_key=idempotency_key,
            run_status="queued",
            lease_token=token,
            progress_completed=0,
            progress_total=3,
            key_version=self._cipher.key_version,
        )
        try:
            with self._session.begin_nested():
                self._session.add(run)
                self._session.flush()
        except IntegrityError as error:
            if idempotency_key:
                winner = self._idempotent(subject, idempotency_key)
                if winner is not None and winner.manifest_checksum == manifest:
                    return winner, None
            raise ConflictError("That analysis could not be recorded; submit it again.") from error
        if principal is not None:
            self._record(
                AuditAction.RECURRENCE_RUN_SUBMITTED,
                principal,
                "recurrence_analysis_run",
                run.id,
                {"mode": mode, "source": source, "definition": definition.checksum()},
            )
        return run, token

    def _idempotent(self, subject: str, key: str) -> RecurrenceAnalysisRun | None:
        return self._session.execute(
            select(RecurrenceAnalysisRun).where(
                RecurrenceAnalysisRun.created_by_subject == subject,
                RecurrenceAnalysisRun.idempotency_key == key,
            )
        ).scalar_one_or_none()

    def _resolve(
        self, principal: AuthenticatedPrincipal, request: RunSubmission
    ) -> tuple[FrozenRecurrenceDefinition, str, uuid.UUID | None, uuid.UUID | None]:
        if request.mode == "programme":
            method = self.programme_version()
            if method is None:
                raise ValidationFailedError(
                    "No approved programme recurrence method is active. Use exploratory mode, "
                    "which labels every result as exploratory."
                )
            name = method.qualified_version
            return self.validate(method.parameters or {}, name=name), name, None, method.id
        if request.definition_version_id is not None:
            record, saved = self._owned_version(principal, request.definition_version_id)
            name = f"{record.name} v{saved.version_number}"
            return self.validate(saved.parameters, name=name), name, saved.id, None
        if request.parameters is not None:
            name = "Unsaved exploratory definition"
            return self.validate(request.parameters, name=name), name, None, None
        return EXPLORATORY_PRESET, EXPLORATORY_PRESET.name, None, None

    def _frozen_scope(
        self, principal: AuthenticatedPrincipal, source: str, live_scope: LiveScope | None
    ) -> tuple[tuple[str, ...], str]:
        if source == "live_tracker":
            if live_scope is None or not live_scope.facility_refs:
                raise ValidationFailedError(
                    "Live evidence needs an active eRegisters session with a discovered "
                    "facility scope."
                )
            return tuple(sorted(live_scope.facility_refs)), live_scope.scope_key
        facilities = self._scope.facility_ids(principal)
        refs = (
            (NATIONAL_SCOPE,)
            if facilities is None
            else tuple(sorted(str(item) for item in facilities))
        )
        if not refs:
            raise GeographyScopeDeniedError("Your account has no authorised facility scope.")
        return refs, stored_scope_key(refs)

    # -- Reading runs -----------------------------------------------------
    def _readable(
        self,
        principal: AuthenticatedPrincipal,
        run: RecurrenceAnalysisRun,
        live_scope_key: str | None,
    ) -> bool:
        """Whether the caller's *current* scope still covers the run."""
        if run.source_kind == "live_tracker":
            return live_scope_key is not None and hmac.compare_digest(live_scope_key, run.scope_key)
        facilities = self._scope.facility_ids(principal)
        if facilities is None:
            return True
        if NATIONAL_SCOPE in run.facility_refs:
            return False
        return set(run.facility_refs) <= {str(item) for item in facilities}

    def get_run(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        *,
        live_scope_key: str | None = None,
    ) -> RecurrenceAnalysisRun:
        _require(principal, Permission.SURVEILLANCE_VIEW_AGGREGATE)
        run = self._session.get(RecurrenceAnalysisRun, run_id)
        if run is None or not self._readable(principal, run, live_scope_key):
            # Deliberately indistinguishable from absent.
            raise NotFoundError("analysis run not found or outside your current scope")
        return run

    def _completed(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        live_scope_key: str | None,
    ) -> RecurrenceAnalysisRun:
        run = self.get_run(principal, run_id, live_scope_key=live_scope_key)
        if run.run_status != "completed":
            raise ConflictError(f"The analysis is {run.run_status}; results are not available yet.")
        return run

    def list_runs(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int = 20,
        live_scope_key: str | None = None,
    ) -> list[RecurrenceAnalysisRun]:
        _require(principal, Permission.SURVEILLANCE_VIEW_AGGREGATE)
        candidates = self._session.execute(
            select(RecurrenceAnalysisRun)
            .where(
                or_(
                    RecurrenceAnalysisRun.created_by_subject == principal.subject,
                    RecurrenceAnalysisRun.mode == "programme",
                )
            )
            .order_by(RecurrenceAnalysisRun.created_at.desc())
            .limit(limit * 4)
        ).scalars()
        return [run for run in candidates if self._readable(principal, run, live_scope_key)][:limit]

    def run_shape(self, run: RecurrenceAnalysisRun) -> dict[str, Any]:
        dataset = (
            self._session.execute(
                select(ClinicalEvidenceDataset)
                .options(
                    load_only(
                        ClinicalEvidenceDataset.id,
                        ClinicalEvidenceDataset.created_at,
                        ClinicalEvidenceDataset.coverage_start,
                        ClinicalEvidenceDataset.coverage_end,
                        ClinicalEvidenceDataset.coverage_complete,
                        ClinicalEvidenceDataset.stages,
                        ClinicalEvidenceDataset.encounter_count,
                        ClinicalEvidenceDataset.patient_count,
                        ClinicalEvidenceDataset.mapping_version,
                        ClinicalEvidenceDataset.contract_version,
                    )
                )
                .where(ClinicalEvidenceDataset.id == run.dataset_id)
            ).scalar_one_or_none()
            if run.dataset_id is not None
            else None
        )
        projections = run.projections or {}
        return {
            "id": run.id,
            "status": run.run_status,
            "error_code": run.error_code,
            "mode": run.mode,
            "source": run.source_kind,
            "definition_name": run.definition_name,
            "definition": run.definition_parameters,
            "definition_checksum": run.definition_checksum,
            "definition_version_id": run.definition_version_id,
            "method_version_id": run.method_version_id,
            "exploratory": run.mode == "exploratory",
            "engine_version": run.engine_version,
            "period_start": run.period_start,
            "period_end": run.period_end,
            "timezone": run.timezone,
            "facility_count": None
            if NATIONAL_SCOPE in run.facility_refs
            else len(run.facility_refs),
            "national_scope": NATIONAL_SCOPE in run.facility_refs,
            "filters": run.filters,
            "manifest_checksum": run.manifest_checksum,
            "created_by": run.created_by_username,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "progress_completed": run.progress_completed,
            "progress_total": run.progress_total,
            "dataset": (
                {
                    "id": dataset.id,
                    "hash": run.dataset_hash,
                    "retained_at": dataset.created_at,
                    "coverage_start": dataset.coverage_start,
                    "coverage_end": dataset.coverage_end,
                    "coverage_complete": dataset.coverage_complete,
                    "stages": dataset.stages,
                    "encounter_count": dataset.encounter_count,
                    "patient_count": dataset.patient_count,
                    "mapping_version": dataset.mapping_version,
                    "contract_version": dataset.contract_version,
                }
                if dataset is not None
                else None
            ),
            "summary": run.summary,
            "coverage": projections.get("coverage"),
            "interpretation": INTERPRETATION_LIMIT,
        }

    def projection(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        name: str,
        *,
        live_scope_key: str | None = None,
    ) -> dict[str, Any]:
        if name not in PROJECTIONS:
            raise NotFoundError(f"unknown projection {name!r}")
        run = self._completed(principal, run_id, live_scope_key)
        projections = run.projections or {}
        return {
            "run_id": run.id,
            "name": name,
            "rows": projections.get(name),
            "unit": projections.get("units", {}).get(name),
            "coverage": projections.get("coverage"),
            "facility_names": projections.get("facility_names", {}),
            "interpretation": INTERPRETATION_LIMIT,
        }

    def _finding_rows(self, run_id: uuid.UUID) -> list[PatientRecurrenceFinding]:
        return list(
            self._session.execute(
                select(PatientRecurrenceFinding)
                .options(load_only(*_FINDING_COLUMNS))
                .where(PatientRecurrenceFinding.run_id == run_id)
            ).scalars()
        )

    def _investigation_statuses(self, finding_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not finding_ids:
            return {}
        rows = self._session.execute(
            select(Investigation.patient_finding_id, Investigation.investigation_status).where(
                Investigation.patient_finding_id.in_(list(finding_ids))
            )
        ).all()
        return {
            finding_id: status.value if hasattr(status, "value") else str(status)
            for finding_id, status in rows
            if finding_id is not None
        }

    @staticmethod
    def finding_shape(
        run: RecurrenceAnalysisRun, row: PatientRecurrenceFinding, status: str | None
    ) -> dict[str, Any]:
        names = (run.projections or {}).get("facility_names", {})
        return {
            "finding_id": row.id,
            "patient_alias": row.patient_alias,
            "determination": row.determination,
            "chain_length": row.chain_length,
            "eligible_positive_count": row.eligible_positive_count,
            "observed_positive_count": row.observed_positive_count,
            "chain_interval_days": row.chain_interval_days,
            "first_positive_on": row.first_positive_on,
            "latest_positive_on": row.latest_positive_on,
            "final_date": row.final_date,
            "final_facility_ref": row.final_facility_ref,
            "final_facility_name": (
                names.get(row.final_facility_ref, "Authorised facility")
                if row.final_facility_ref
                else None
            ),
            "facility_count": len(row.facility_refs),
            "cross_facility": row.cross_facility,
            "quality_flags": row.quality_flags,
            "sex": row.sex,
            "age_group": row.age_group,
            "test_methods": row.test_methods,
            "treatment_names": row.treatment_names,
            "investigation_status": status,
        }

    def patients_page(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        filters: PatientPageFilters,
        live_scope_key: str | None = None,
    ) -> dict[str, Any]:
        """One page of a run's listed patients; totals never depend on the page."""
        _require_case_evidence(principal)
        run = self._completed(principal, run_id, live_scope_key)
        rows = self._finding_rows(run.id)
        statuses = self._investigation_statuses([row.id for row in rows])
        kept = [row for row in rows if filters.admits(row, statuses.get(row.id))]
        kept.sort(
            key=lambda row: (row.latest_positive_on or date.min, row.patient_alias), reverse=True
        )
        items = [self.finding_shape(run, row, statuses.get(row.id)) for row in kept]
        fingerprint = hashlib.sha256(
            f"{run.id}|{json.dumps(filters.as_dict(), sort_keys=True)}".encode()
        ).hexdigest()[:32]
        page, next_cursor, previous_cursor = page_rows(
            items, limit=limit, cursor=cursor, fingerprint=fingerprint
        )
        return {
            "run_id": run.id,
            "items": page,
            "total": len(items),
            "limit": limit,
            "next_cursor": next_cursor,
            "previous_cursor": previous_cursor,
            "filters": filters.as_dict(),
        }

    def patient_detail(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        alias: str,
        *,
        live_scope_key: str | None = None,
    ) -> dict[str, Any]:
        _require_case_evidence(principal)
        run = self._completed(principal, run_id, live_scope_key)
        row = self._session.execute(
            select(PatientRecurrenceFinding).where(
                PatientRecurrenceFinding.run_id == run.id,
                PatientRecurrenceFinding.patient_alias == alias,
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("patient finding not found in this analysis")
        detail = self._cipher.open(
            row.detail, finding_aad(run.id, row.id, row.patient_alias, run.key_version)
        )
        self._record(
            AuditAction.CASE_EVIDENCE_ACCESSED,
            principal,
            "patient_recurrence_finding",
            row.id,
            {"run_id": str(run.id)},
        )
        names = (run.projections or {}).get("facility_names", {})
        status = self._investigation_statuses([row.id]).get(row.id)
        return {
            "run_id": run.id,
            "finding": self.finding_shape(run, row, status),
            "timeline": timeline_shape(detail, names),
            "definition_name": run.definition_name,
            "definition": run.definition_parameters,
            "engine_version": run.engine_version,
            "exploratory": run.mode == "exploratory",
            "interpretation": INTERPRETATION_LIMIT,
        }

    def finding_for_review(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        alias: str,
        *,
        live_scope_key: str | None = None,
    ) -> tuple[RecurrenceAnalysisRun, PatientRecurrenceFinding]:
        _require_case_evidence(principal)
        run = self._completed(principal, run_id, live_scope_key)
        row = self._session.execute(
            select(PatientRecurrenceFinding)
            .options(load_only(*_FINDING_COLUMNS))
            .where(
                PatientRecurrenceFinding.run_id == run.id,
                PatientRecurrenceFinding.patient_alias == alias,
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("patient finding not found in this analysis")
        return run, row

    def duplicates(
        self,
        principal: AuthenticatedPrincipal,
        run_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        kinds: frozenset[str] = frozenset(),
        live_scope_key: str | None = None,
    ) -> dict[str, Any]:
        _require_case_evidence(principal)
        run = self._completed(principal, run_id, live_scope_key)
        rows: list[dict[str, Any]] = (
            self._cipher.open(run.duplicate_payload, duplicates_aad(run.id, run.key_version))
            if run.duplicate_payload
            else []
        )
        kept = [row for row in rows if not kinds or row["kind"] in kinds]
        fingerprint = hashlib.sha256(f"{run.id}|dup|{sorted(kinds)}".encode()).hexdigest()[:32]
        page, next_cursor, previous_cursor = page_rows(
            kept, limit=limit, cursor=cursor, fingerprint=fingerprint
        )
        return {
            "run_id": run.id,
            "items": page,
            "total": len(kept),
            "limit": limit,
            "next_cursor": next_cursor,
            "previous_cursor": previous_cursor,
        }

    def compare(
        self,
        principal: AuthenticatedPrincipal,
        run_a: uuid.UUID,
        run_b: uuid.UUID,
        *,
        live_scope_key: str | None = None,
    ) -> dict[str, Any]:
        """A/B over one retained dataset, or an explicit refusal with the remedy."""
        first = self._completed(principal, run_a, live_scope_key)
        second = self._completed(principal, run_b, live_scope_key)
        remedy = (
            "Submit both definitions for the same source, period and scope, so they "
            "evaluate one retained dataset, then compare those two runs."
        )
        if first.dataset_id != second.dataset_id or first.dataset_hash != second.dataset_hash:
            raise ConflictError("These runs evaluated different evidence datasets. " + remedy)
        if (first.period_start, first.period_end, first.timezone) != (
            second.period_start,
            second.period_end,
            second.timezone,
        ):
            raise ConflictError("These runs cover different reporting periods. " + remedy)
        if first.filters != second.filters:
            raise ConflictError(
                "These runs used different filters, so a difference could come from the "
                "filters rather than the definitions. " + remedy
            )

        def columns(run: RecurrenceAnalysisRun) -> list[dict[str, Any]]:
            return [
                {"patient_alias": row.patient_alias, "determination": row.determination}
                for row in self._finding_rows(run.id)
            ]

        overlap = compare_run_findings(columns(first), columns(second))
        may_see_patients = (
            Permission.CASE_EVIDENCE_VIEW in principal.permissions
            and principal.max_sensitivity.covers(SensitivityLevel.PSEUDONYMOUS_CASE)
        )
        if not may_see_patients:
            overlap = {
                key: (len(value) if isinstance(value, list) else value)
                for key, value in overlap.items()
            }
        return {
            "run_a": first.id,
            "run_b": second.id,
            "dataset_id": first.dataset_id,
            "dataset_hash": first.dataset_hash,
            "definitions": {"a": first.definition_parameters, "b": second.definition_parameters},
            "summaries": {"a": first.summary, "b": second.summary},
            "overlap": overlap,
            "patients_included": may_see_patients,
            "differences": compare_projections(first.projections or {}, second.projections or {}),
            "interpretation": INTERPRETATION_LIMIT,
        }

    # -- Execution ------------------------------------------------------------
    def execute(
        self,
        run_id: uuid.UUID,
        token: str,
        *,
        facility_names: Mapping[str, str] | None = None,
        facility_geography: Mapping[str, str] | None = None,
    ) -> None:
        """Evaluate one queued run. Fenced: a stale or replaced token does nothing."""
        run = self._session.execute(
            select(RecurrenceAnalysisRun)
            .where(RecurrenceAnalysisRun.id == run_id)
            .with_for_update()
        ).scalar_one_or_none()
        if run is None or run.lease_token != token or run.run_status not in ("queued", "running"):
            return
        run.run_status = "running"
        run.progress_completed = 1
        self._session.flush()

        definition = FrozenRecurrenceDefinition.from_parameters(
            run.definition_parameters, name=run.definition_name
        )
        query = FrozenAnalysisQuery(run.period_start, run.period_end, run.timezone)
        filters = RunFilters.from_dict(run.filters)
        store = ClinicalEvidenceStore(self._session, self._cipher)
        unlinked: Mapping[str, Sequence[Medication]] = {}
        if run.source_kind == "stored_encounter":
            lookback = max(
                self._settings.recurrence_live_lookback_days, definition.maximum_window_days
            )
            facilities = (
                None
                if NATIONAL_SCOPE in run.facility_refs
                else {uuid.UUID(item) for item in run.facility_refs}
            )
            evidence = load_stored_evidence(
                self._session,
                facilities=facilities,
                period_start=run.period_start,
                period_end=run.period_end,
                lookback_start=run.period_start - timedelta(days=lookback),
            )
            dataset = store.publish(
                encounters=evidence.encounters,
                coverage=evidence.coverage,
                source_kind="stored_encounter",
                namespace=STORED_NAMESPACE,
                scope_key=run.scope_key,
                facility_refs=list(run.facility_refs),
                mapping_version=STORED_MAPPING_VERSION,
                created_by=run.created_by_username,
            )
            encounters: Sequence[CanonicalEncounter] = evidence.encounters
            coverage = evidence.coverage
            names, geography = evidence.facility_names, evidence.facility_geography
            namespace = STORED_NAMESPACE
        else:
            found = store.latest_live(
                run.scope_key,
                period_start=query.lookback_start(definition),
                period_end=run.period_end,
            )
            if found is None:
                raise RunFailure("live_evidence_not_retained")
            retained = store.open(found)
            dataset = found
            encounters, coverage, unlinked = (
                retained.encounters,
                retained.coverage,
                retained.unlinked_medications,
            )
            names, geography = dict(facility_names or {}), dict(facility_geography or {})
            namespace = found.namespace
        run.progress_completed = 2
        self._session.flush()

        evaluation = evaluate_recurrence(
            encounters, definition, query, coverage, dataset_id=str(dataset.id)
        )
        by_patient: dict[PatientKey, list[CanonicalEncounter]] = defaultdict(list)
        for encounter in encounters:
            if encounter.patient is not None:
                by_patient[encounter.patient].append(encounter)
        facts = {
            finding.patient: patient_facts(finding, by_patient[finding.patient])
            for finding in evaluation.patients
        }
        restricted = restrict(evaluation, filters, facts)

        def alias(key: PatientKey) -> str:
            return patient_display_alias(self._display_key, key)

        rows = build_findings(
            restricted,
            filters,
            encounters_by_patient=by_patient,
            unlinked_by_patient={
                PatientKey(namespace, reference): tuple(items)
                for reference, items in unlinked.items()
            },
            facts=facts,
            alias=alias,
        )
        for columns, detail in rows:
            finding_id = uuid.uuid4()
            self._session.add(
                PatientRecurrenceFinding(
                    id=finding_id,
                    run_id=run.id,
                    detail=self._cipher.seal(
                        detail,
                        finding_aad(run.id, finding_id, columns["patient_alias"], run.key_version),
                    ),
                    **columns,
                )
            )
        duplicates = build_duplicate_rows(
            restricted,
            encounters_by_key={(e.namespace, e.encounter_key): e for e in encounters},
            alias=alias,
            facility_names=names,
        )
        projections = build_projections(
            restricted, filters, facility_geography=geography, facility_names=names
        )
        run.projections = projections
        run.summary = {
            **projections["summary"],
            "patients_listed": len(rows),
            "dataset_patient_count": dataset.patient_count,
            "dataset_encounter_count": dataset.encounter_count,
        }
        run.duplicate_payload = self._cipher.seal(
            duplicates, duplicates_aad(run.id, run.key_version)
        )
        run.dataset_id = dataset.id
        run.dataset_hash = dataset.dataset_hash
        run.run_status = "completed"
        run.completed_at = datetime.now(UTC)
        run.progress_completed = 3
        run.lease_token = None
        self._session.flush()

    def mark_failed(self, run_id: uuid.UUID, token: str, code: str) -> None:
        run = self._session.execute(
            select(RecurrenceAnalysisRun)
            .where(RecurrenceAnalysisRun.id == run_id)
            .with_for_update()
        ).scalar_one_or_none()
        if run is None or run.lease_token != token or run.run_status not in ("queued", "running"):
            return
        run.run_status = "failed"
        run.error_code = code[:64]
        run.lease_token = None
        self._session.flush()

    def _record(
        self,
        action: AuditAction,
        principal: AuthenticatedPrincipal,
        object_type: str,
        object_id: uuid.UUID,
        context: dict[str, Any],
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            action=action,
            principal=principal,
            object_type=object_type,
            object_id=str(object_id),
            context=context,
        )


class RecurrenceRunExecutor:
    """Bounded background execution with its own short transactions."""

    def __init__(
        self,
        sessions: Callable[[], Session],
        settings: Settings,
        cipher: EvidenceCipher,
        display_key: bytes,
        *,
        max_workers: int,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._cipher = cipher
        self._display_key = display_key
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="mars-recurrence"
        )

    def _service(self, session: Session) -> RecurrenceAnalysisService:
        return RecurrenceAnalysisService(
            session, self._settings, None, cipher=self._cipher, display_key=self._display_key
        )

    def start(
        self,
        run_id: uuid.UUID,
        token: str,
        *,
        facility_names: Mapping[str, str] | None = None,
        facility_geography: Mapping[str, str] | None = None,
    ) -> None:
        self._pool.submit(self.run, run_id, token, facility_names, facility_geography)

    def run(
        self,
        run_id: uuid.UUID,
        token: str,
        facility_names: Mapping[str, str] | None = None,
        facility_geography: Mapping[str, str] | None = None,
    ) -> None:
        # The submitting request commits when it responds; wait briefly for it.
        for _ in range(100):
            with self._sessions() as session:
                if session.get(RecurrenceAnalysisRun, run_id) is not None:
                    break
            time.sleep(0.05)
        try:
            with self._sessions() as session, session.begin():
                self._service(session).execute(
                    run_id,
                    token,
                    facility_names=facility_names,
                    facility_geography=facility_geography,
                )
        except Exception as error:
            code = error.code if isinstance(error, RunFailure) else "analysis_failed"
            # Only a controlled code and the error type are logged: an exception
            # message from a lower layer could echo evidence.
            logger.warning(
                "recurrence_run_failed", run_id=str(run_id), code=code, error=type(error).__name__
            )
            with suppress(Exception), self._sessions() as session, session.begin():
                self._service(session).mark_failed(run_id, token, code)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "MODES",
    "NATIONAL_SCOPE",
    "PROGRAMME_METHOD_CODE",
    "PROJECTIONS",
    "SOURCES",
    "LiveScope",
    "PatientPageFilters",
    "RecurrenceAnalysisService",
    "RecurrenceRunExecutor",
    "RunFailure",
    "RunSubmission",
    "ceilings",
    "duplicates_aad",
    "finding_aad",
    "medication_shape",
    "stored_scope_key",
    "timeline_shape",
]
