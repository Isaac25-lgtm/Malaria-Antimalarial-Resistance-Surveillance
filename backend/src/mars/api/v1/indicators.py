"""Indicator registry and summary endpoints.

Two surfaces with different audiences and different permissions.

The **registry** is what a metric means: readable by anyone who may view a
method, because understanding a definition is not the same as seeing a
district's figures.

The **summaries** are the figures themselves, and every request is scoped. A
geography identifier outside the caller's scope is **refused**, not filtered
out: a caller who watches a list shrink learns which districts exist, which is
the leak a scope is meant to prevent.

**The frontend receives final values.** No positivity, rollup or recurrence is
computed in a browser. A figure computed twice is a figure that disagrees with
itself eventually, and the one on a screen is the one someone acts on.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import (
    AnalyticsQueryDep,
    FacilityServiceDep,
    GeographyServiceDep,
    IndicatorQueryDep,
    require_permissions,
)
from mars.api.v1.schemas import (
    IndicatorDefinitionSummary,
    IndicatorResultSummary,
)
from mars.core.errors import NotFoundError
from mars.domain.enums import GeographyGrain
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/indicators", tags=["indicators"])

RegistryReader = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.METHOD_VIEW))
]
SummaryReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]


@router.get(
    "/definitions",
    response_model=list[IndicatorDefinitionSummary],
    summary="Every registered indicator definition",
)
def list_definitions(
    principal: RegistryReader,
    service: IndicatorQueryDep,
) -> list[IndicatorDefinitionSummary]:
    """The registry.

    A definition with no active version is returned with ``active_version:
    null`` rather than omitted. An indicator awaiting programme approval is a
    fact the programme needs to see, not an absence.
    """
    return [
        IndicatorDefinitionSummary.model_validate(service.definition_shape(definition))
        for definition in service.list_definitions()
    ]


@router.get(
    "/definitions/{code}",
    response_model=IndicatorDefinitionSummary,
    summary="One indicator definition",
)
def get_definition(
    code: str,
    principal: RegistryReader,
    service: IndicatorQueryDep,
) -> IndicatorDefinitionSummary:
    definition = service.get_definition(code)
    if definition is None:
        raise NotFoundError("No such indicator definition.")
    return IndicatorDefinitionSummary.model_validate(service.definition_shape(definition))


@router.get(
    "/summary",
    response_model=list[IndicatorResultSummary],
    summary="Materialised indicator values within the caller's scope",
)
def get_summary(
    principal: SummaryReader,
    service: IndicatorQueryDep,
    geography: GeographyServiceDep,
    facilities: FacilityServiceDep,
    scope: AnalyticsQueryDep,
    code: Annotated[list[str] | None, Query(description="Indicator codes")] = None,
    grain: Annotated[GeographyGrain | None, Query()] = None,
    geography_unit_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    facility_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    period_from: Annotated[date | None, Query()] = None,
    period_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[IndicatorResultSummary]:
    """Figures the caller is entitled to see.

    A requested geography unit outside the caller's scope is **refused**, by
    the same scope check the geography endpoints use. It is not silently
    dropped: a caller who can tell "no data" from "not yours" by watching a
    list length has been told something the scope exists to withhold.

    The refusal is a 404 rather than a 403, deliberately - a 403 confirms that
    the unit exists.
    """
    if geography_unit_id:
        # ``get_unit`` already applies the caller's scope and refuses anything
        # outside it. Reused rather than re-implemented: two scope checks
        # eventually disagree, and the one that is wrong is the one nobody
        # noticed had drifted.
        for unit_id in geography_unit_id:
            geography.get_unit(principal, unit_id)
        allowed_units: list[uuid.UUID] | None = geography_unit_id
    else:
        scoped_units = scope.geography_ids(principal)
        allowed_units = None if scoped_units is None else list(scoped_units)

    if facility_id:
        for requested_facility_id in facility_id:
            facilities.get_facility(principal, requested_facility_id)
        allowed_facilities: list[uuid.UUID] | None = facility_id
    else:
        scoped_facilities = scope.facility_ids(principal)
        allowed_facilities = None if scoped_facilities is None else list(scoped_facilities)

    if principal.is_facility_restricted:
        # A facility user's district scope only proves that the facility itself
        # sits inside the assigned geography. It does not grant district-wide
        # surveillance access or access to sibling facilities.
        allowed_units = []

    # An explicit filter on only one scope dimension should not also return
    # every row from the other dimension. With no explicit filter both are
    # included, because the summary endpoint spans administrative and facility
    # grains.
    if geography_unit_id and not facility_id:
        allowed_facilities = []
    elif facility_id and not geography_unit_id:
        allowed_units = []

    results = service.summary(
        codes=code,
        grain=grain,
        geography_unit_ids=allowed_units,
        facility_ids=allowed_facilities,
        period_from=period_from,
        period_to=period_to,
        limit=limit,
    )
    return [IndicatorResultSummary.model_validate(service.result_shape(r)) for r in results]
