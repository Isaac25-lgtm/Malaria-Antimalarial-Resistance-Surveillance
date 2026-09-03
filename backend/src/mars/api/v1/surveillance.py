"""The composed reads behind the command centre — Prompt 23.

These endpoints exist so the browser never computes a surveillance figure. Each
returns records carrying their own period, scope, source, method version and
availability status, which is what lets a screen render "not configured"
honestly instead of rendering a zero.

Scope is enforced in SQL by the services. Nothing here widens it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import SurveillanceSummaryDep, require_permissions
from mars.api.v1.schemas import (
    FacilityContribution,
    PriorityDistrict,
    SurveillanceMeasure,
    SurveillanceProvenance,
)
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/surveillance", tags=["surveillance"])

AggregateReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]


@router.get("/national/summary", response_model=list[SurveillanceMeasure])
def national_summary(
    principal: AggregateReader,
    service: SurveillanceSummaryDep,
    period_start: date,
    period_end: date,
) -> list[SurveillanceMeasure]:
    """The KPI strip: one governed measure per record.

    A measure with no approved indicator version reports ``not_configured``
    and names what is missing, rather than reporting zero.
    """
    return [
        SurveillanceMeasure.model_validate(item)
        for item in service.kpis(principal, period_start=period_start, period_end=period_end)
    ]


@router.get("/districts/{geography_unit_id}/summary", response_model=list[SurveillanceMeasure])
def district_summary(
    geography_unit_id: uuid.UUID,
    principal: AggregateReader,
    service: SurveillanceSummaryDep,
    period_start: date,
    period_end: date,
) -> list[SurveillanceMeasure]:
    """The same measures for one district."""
    return [
        SurveillanceMeasure.model_validate(item)
        for item in service.kpis(
            principal,
            period_start=period_start,
            period_end=period_end,
            geography_unit_id=geography_unit_id,
        )
    ]


@router.get("/priority-districts", response_model=list[PriorityDistrict])
def priority_districts(
    principal: AggregateReader,
    service: SurveillanceSummaryDep,
    period_start: date,
    period_end: date,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[PriorityDistrict]:
    """Districts ordered by active signal count.

    A count of records, not a governed priority score - MARS has no approved
    way to rank districts against one another, and the ordering says so.
    """
    return [
        PriorityDistrict.model_validate(item)
        for item in service.priority_districts(
            principal, period_start=period_start, period_end=period_end, limit=limit
        )
    ]


@router.get("/facilities/{facility_id}/summary", response_model=list[SurveillanceMeasure])
def facility_summary(
    facility_id: uuid.UUID,
    principal: AggregateReader,
    service: SurveillanceSummaryDep,
    period_start: date,
    period_end: date,
) -> list[SurveillanceMeasure]:
    """The same measures for one facility, from that facility's own results.

    Never the district it sits in. A facility workspace that summed its
    district would be the scope inheritance the surveillance API has been
    corrected twice to remove.
    """
    return [
        SurveillanceMeasure.model_validate(item)
        for item in service.kpis(
            principal,
            period_start=period_start,
            period_end=period_end,
            facility_id=facility_id,
        )
    ]


@router.get(
    "/districts/{geography_unit_id}/facilities",
    response_model=list[FacilityContribution],
)
def district_facilities(
    geography_unit_id: uuid.UUID,
    principal: AggregateReader,
    service: SurveillanceSummaryDep,
    period_start: date,
    period_end: date,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[FacilityContribution]:
    """Which facilities contributed to a district figure, and which did not."""
    return [
        FacilityContribution.model_validate(item)
        for item in service.facility_contributions(
            principal,
            geography_unit_id=geography_unit_id,
            period_start=period_start,
            period_end=period_end,
            limit=limit,
        )
    ]


@router.get("/provenance", response_model=SurveillanceProvenance)
def provenance(
    principal: AggregateReader,
    service: SurveillanceSummaryDep,
    period_start: date,
    period_end: date,
) -> SurveillanceProvenance:
    """Freshness, configuration state and the interpretation boundary."""
    return SurveillanceProvenance.model_validate(
        service.provenance(principal, period_start=period_start, period_end=period_end)
    )


__all__ = ["router"]
