"""Organisation unit and facility endpoints.

Facility listing is the sharpest test of the scoping model: a district user must
not be able to enumerate facilities outside their district, and a facility user
must not see their neighbours. Both restrictions are applied in the query.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import (
    FacilityServiceDep,
    OrganisationServiceDep,
    require_permissions,
)
from mars.api.v1.schemas import (
    FacilityDetail,
    FacilityIdentifierSummary,
    FacilitySummary,
    OrganisationUnitDetail,
    OrganisationUnitSummary,
    Page,
)
from mars.domain.enums import FacilityLevel, OrganisationUnitType
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(tags=["organisation"])

OrganisationViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.ORGANISATION_VIEW))
]
FacilityViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.FACILITY_VIEW))
]


@router.get(
    "/organisation-units",
    response_model=Page[OrganisationUnitSummary],
    summary="List organisation units within your scope",
)
def list_organisation_units(
    principal: OrganisationViewer,
    service: OrganisationServiceDep,
    unit_type: Annotated[OrganisationUnitType | None, Query()] = None,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
    active_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[OrganisationUnitSummary]:
    """List health-sector organisation units.

    Health Sub-District is one of the unit types here, not a geography level:
    MARS does not assume an HSD coincides with a county.
    """
    units = service.list_units(
        principal,
        unit_type=unit_type,
        parent_id=parent_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[_org_summary(unit) for unit in units],
        total=None,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/organisation-units/{unit_id}",
    response_model=OrganisationUnitDetail,
    summary="One organisation unit, with its ancestor chain",
)
def get_organisation_unit(
    unit_id: uuid.UUID,
    principal: OrganisationViewer,
    service: OrganisationServiceDep,
) -> OrganisationUnitDetail:
    unit = service.get_unit(principal, unit_id)
    ancestors = service.ancestors_of(principal, unit_id)
    return OrganisationUnitDetail(
        **_org_summary(unit).model_dump(),
        ancestors=[_org_summary(a) for a in ancestors],
        child_count=len(unit.children),
    )


@router.get(
    "/facilities",
    response_model=Page[FacilitySummary],
    summary="List facilities within your scope",
)
def list_facilities(
    principal: FacilityViewer,
    service: FacilityServiceDep,
    district_id: Annotated[uuid.UUID | None, Query()] = None,
    subcounty_id: Annotated[uuid.UUID | None, Query()] = None,
    organisation_unit_id: Annotated[uuid.UUID | None, Query()] = None,
    facility_level: Annotated[FacilityLevel | None, Query()] = None,
    active_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[FacilitySummary]:
    """List facilities.

    Scope is applied in the query, so a district filter naming another district
    returns nothing rather than raising - a caller learns what they may see, not
    what exists elsewhere.
    """
    facilities = service.list_facilities(
        principal,
        district_id=district_id,
        subcounty_id=subcounty_id,
        organisation_unit_id=organisation_unit_id,
        facility_level=facility_level,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[_facility_summary(f) for f in facilities],
        total=None,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/facilities/{facility_id}",
    response_model=FacilityDetail,
    summary="One facility",
)
def get_facility(
    facility_id: uuid.UUID,
    principal: FacilityViewer,
    service: FacilityServiceDep,
) -> FacilityDetail:
    facility = service.get_facility(principal, facility_id)
    identifiers = service.identifiers_for(principal, facility_id)
    return FacilityDetail(
        **_facility_summary(facility).model_dump(),
        opened_on=facility.opened_on,
        closed_on=facility.closed_on,
        coordinate_source=facility.coordinate_source,
        coordinate_validated=facility.coordinate_validated,
        identifiers=[
            FacilityIdentifierSummary(
                source_system=i.source_system,
                external_id=i.external_id,
                external_name=i.external_name,
                is_primary=i.is_primary,
            )
            for i in identifiers
        ],
    )


def _org_summary(unit: object) -> OrganisationUnitSummary:
    return OrganisationUnitSummary(
        id=unit.id,  # type: ignore[attr-defined]
        code=unit.code,  # type: ignore[attr-defined]
        name=unit.raw_name,  # type: ignore[attr-defined]
        unit_type=unit.unit_type.value,  # type: ignore[attr-defined]
        parent_id=unit.parent_id,  # type: ignore[attr-defined]
        depth=unit.depth,  # type: ignore[attr-defined]
        primary_geography_unit_id=unit.primary_geography_unit_id,  # type: ignore[attr-defined]
        is_active=unit.is_active,  # type: ignore[attr-defined]
    )


def _facility_summary(facility: object) -> FacilitySummary:
    """Map a facility onto its wire representation.

    ``has_coordinates`` is true only for a validated coordinate. An unvalidated
    point is reported as absent, so the map omits it rather than placing the
    facility somewhere it may not be.
    """
    latitude = facility.latitude  # type: ignore[attr-defined]
    validated = facility.coordinate_validated  # type: ignore[attr-defined]
    return FacilitySummary(
        id=facility.id,  # type: ignore[attr-defined]
        code=facility.code,  # type: ignore[attr-defined]
        name=facility.raw_name,  # type: ignore[attr-defined]
        facility_level=facility.facility_level.value,  # type: ignore[attr-defined]
        ownership=facility.ownership.value,  # type: ignore[attr-defined]
        district_geography_unit_id=facility.district_geography_unit_id,  # type: ignore[attr-defined]
        subcounty_geography_unit_id=facility.subcounty_geography_unit_id,  # type: ignore[attr-defined]
        organisation_unit_id=facility.organisation_unit_id,  # type: ignore[attr-defined]
        is_active=facility.is_active,  # type: ignore[attr-defined]
        is_synthetic=facility.is_synthetic,  # type: ignore[attr-defined]
        has_coordinates=latitude is not None and validated,
    )
