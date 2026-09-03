"""Scope-safe analytical read API for Prompts 14-20."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import (
    AnalyticsQueryDep,
    SessionDep,
    require_permissions,
    require_sensitivity,
)
from mars.api.v1.schemas import AnalyticalRecordSummary
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    SpatialAggregationBasis,
)
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.services.spatial_availability import spatial_cells

router = APIRouter(prefix="/analytics", tags=["analytics"])
AggregateReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]
CaseReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.CASE_EVIDENCE_VIEW)),
]


@router.get(
    "/episodes",
    response_model=list[AnalyticalRecordSummary],
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def episodes(
    principal: CaseReader,
    service: AnalyticsQueryDep,
    period_from: date | None = None,
    period_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 250,
) -> list[AnalyticalRecordSummary]:
    """Pseudonymous episode candidates; never direct patient identity."""
    return [
        AnalyticalRecordSummary.model_validate(item)
        for item in service.episodes(
            principal, period_from=period_from, period_to=period_to, limit=limit
        )
    ]


@router.get("/results/{kind}", response_model=list[AnalyticalRecordSummary])
def analytical_results(
    kind: Literal[
        "recurrence", "testing", "treatment", "baseline", "anomaly", "hotspot", "cluster"
    ],
    principal: AggregateReader,
    service: AnalyticsQueryDep,
    period_from: date | None = None,
    period_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[AnalyticalRecordSummary]:
    return [
        AnalyticalRecordSummary.model_validate(item)
        for item in service.aggregate_results(
            principal,
            kind=kind,
            period_from=period_from,
            period_to=period_to,
            limit=limit,
        )
    ]


@router.get("/commodity-alerts", response_model=list[AnalyticalRecordSummary])
def commodity_alerts(
    principal: AggregateReader,
    service: AnalyticsQueryDep,
    period_from: date | None = None,
    period_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[AnalyticalRecordSummary]:
    return [
        AnalyticalRecordSummary.model_validate(item)
        for item in service.commodity_alerts(
            principal, period_from=period_from, period_to=period_to, limit=limit
        )
    ]


@router.get("/spatial/cells", response_model=dict[str, Any])
def map_cells(
    principal: AggregateReader,
    session: SessionDep,
    series_kind: BaselineSeriesKind,
    series_key: str,
    period_start: date,
    geography_grain: GeographyGrain,
    basis: SpatialAggregationBasis,
    boundary_version_id: uuid.UUID,
    unit_id: Annotated[list[uuid.UUID] | None, Query()] = None,
) -> dict[str, Any]:
    """Map-ready cells with missing/suppressed/unavailable kept distinct."""
    if principal.is_facility_restricted:
        # The same rule the indicator summary and the hotspot/cluster queries
        # apply. A facility user's district scope proves only that the facility
        # sits inside that district; it does not grant district-wide
        # surveillance access. An empty prefix tuple marks every administrative
        # cell ``outside_scope`` - the areas stay on the map, because their
        # existence is public geography, and their figures do not.
        paths: tuple[str, ...] | None = ()
    elif principal.has_national_scope:
        paths = None
    else:
        paths = principal.scope_path_prefixes()
    return spatial_cells(
        session,
        series_kind=series_kind,
        series_key=series_key,
        period_start=period_start,
        geography_grain=geography_grain,
        basis=basis,
        boundary_version_id=boundary_version_id,
        authorised_paths=paths,
        requested_unit_ids=tuple(unit_id) if unit_id else None,
    )


__all__ = ["router"]
