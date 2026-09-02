"""Geography endpoints.

Every route requires ``geography:view`` and applies the caller's geography scope
inside the query. Phases 1-2 expose metadata and hierarchy only; boundary
geometry arrives with the Prompt 5 importer.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import GeographyServiceDep, require_permissions
from mars.api.v1.schemas import (
    BoundaryVersionSummary,
    GeographyAliasSummary,
    GeographyLevelCount,
    GeographyOverviewResponse,
    GeographyUnitDetail,
    GeographyUnitSummary,
    Page,
)
from mars.domain.enums import GeographyLevel
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/geography", tags=["geography"])

GeographyViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.GEOGRAPHY_VIEW))
]


@router.get(
    "/overview",
    response_model=GeographyOverviewResponse,
    summary="Hierarchy metadata and loaded boundary versions",
)
def overview(
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> GeographyOverviewResponse:
    """Report unit counts per level and the boundary versions registered.

    Levels with no data are reported as zero rather than omitted, so an empty
    parish level reads as "none loaded" and not as "level not supported".
    """
    counts = service.level_counts(principal)
    versions = service.list_boundary_versions()

    return GeographyOverviewResponse(
        levels=[
            GeographyLevelCount(level=level.value, count=counts.get(level.value, 0))
            for level in GeographyLevel
        ],
        boundary_versions=[BoundaryVersionSummary.model_validate(v) for v in versions],
        note=(
            "Parish and village levels are supported by the schema and are "
            "intentionally empty: no parish or village boundary data has been "
            "supplied. Geometry is imported in Prompt 5."
        ),
    )


@router.get(
    "/units",
    response_model=Page[GeographyUnitSummary],
    summary="List geography units within your scope",
)
def list_units(
    principal: GeographyViewer,
    service: GeographyServiceDep,
    level: Annotated[GeographyLevel | None, Query(description="Filter by hierarchy level")] = None,
    parent_id: Annotated[
        uuid.UUID | None, Query(description="Direct children of this unit")
    ] = None,
    active_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[GeographyUnitSummary]:
    units = service.list_units(
        principal,
        level=level,
        parent_id=parent_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[_to_summary(unit) for unit in units],
        total=None,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/units/{unit_id}",
    response_model=GeographyUnitDetail,
    summary="One geography unit, with its ancestor chain",
)
def get_unit(
    unit_id: uuid.UUID,
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> GeographyUnitDetail:
    unit = service.get_unit(principal, unit_id)
    ancestors = service.ancestors_of(principal, unit_id)
    children = service.children_of(principal, unit_id)

    detail = GeographyUnitDetail(
        **_to_summary(unit).model_dump(),
        boundary_version_id=unit.boundary_version_id,
        ancestors=[_to_summary(a) for a in ancestors],
        child_count=len(children),
        has_geometry=unit.geometry is not None,
    )
    return detail


@router.get(
    "/units/{unit_id}/children",
    response_model=list[GeographyUnitSummary],
    summary="Direct children of a geography unit",
)
def get_children(
    unit_id: uuid.UUID,
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> list[GeographyUnitSummary]:
    return [_to_summary(unit) for unit in service.children_of(principal, unit_id)]


@router.get(
    "/aliases",
    response_model=list[GeographyAliasSummary],
    summary="Resolve a source system's geography code",
)
def resolve_alias(
    principal: GeographyViewer,
    service: GeographyServiceDep,
    source_system: Annotated[str, Query(description="e.g. ubos_fscode, dhis2")],
    source_code: Annotated[str, Query()],
) -> list[GeographyAliasSummary]:
    """Return every candidate mapping for a source code.

    More than one result means the mapping is ambiguous and needs review. MARS
    does not pick one: an ambiguous source value stays unresolved.
    """
    aliases = service.find_by_alias(principal, source_system, source_code)
    return [
        GeographyAliasSummary(
            id=alias.id,
            geography_unit_id=alias.geography_unit_id,
            source_system=alias.source_system,
            source_code=alias.source_code,
            source_name=alias.source_name,
            match_status=alias.match_status.value,
            match_method=alias.match_method,
        )
        for alias in aliases
    ]


@router.get(
    "/boundary-versions",
    response_model=list[BoundaryVersionSummary],
    summary="Registered boundary dataset versions",
)
def list_boundary_versions(
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> list[BoundaryVersionSummary]:
    return [BoundaryVersionSummary.model_validate(v) for v in service.list_boundary_versions()]


def _to_summary(unit: object) -> GeographyUnitSummary:
    """Map an ORM unit onto its wire representation.

    ``name`` carries the raw supplied value; ``normalised_name`` is exposed
    separately and is documented as a lookup form, so a client cannot mistake
    the normalised value for the display name.
    """
    return GeographyUnitSummary(
        id=unit.id,  # type: ignore[attr-defined]
        level=unit.level.value,  # type: ignore[attr-defined]
        unit_kind=unit.unit_kind.value,  # type: ignore[attr-defined]
        preferred_code=unit.preferred_code,  # type: ignore[attr-defined]
        name=unit.raw_name,  # type: ignore[attr-defined]
        normalised_name=unit.normalised_name,  # type: ignore[attr-defined]
        parent_id=unit.parent_id,  # type: ignore[attr-defined]
        depth=unit.depth,  # type: ignore[attr-defined]
        path=unit.path,  # type: ignore[attr-defined]
        is_active=unit.is_active,  # type: ignore[attr-defined]
        effective_from=unit.effective_from,  # type: ignore[attr-defined]
        effective_to=unit.effective_to,  # type: ignore[attr-defined]
    )
