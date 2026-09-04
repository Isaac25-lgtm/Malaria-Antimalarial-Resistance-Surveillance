"""Geography endpoints.

Every route requires ``geography:view`` and applies the caller's geography scope
inside the query. Hierarchy reads come first, then the map delivery routes that
serve simplified geometry to a browser.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, Response

from mars.api.dependencies import (
    GeographyMapServiceDep,
    GeographyServiceDep,
    require_permissions,
)
from mars.api.v1.schemas import (
    BoundaryVersionSummary,
    BoundingBoxModel,
    GeographyAliasSummary,
    GeographyBreadcrumb,
    GeographyBreadcrumbsResponse,
    GeographyLevelCount,
    GeographyOverviewResponse,
    GeographyUnitDetail,
    GeographyUnitSummary,
    MapFeature,
    MapFeatureCollection,
    MapLevelAvailability,
    MapMetadataResponse,
    NationalGeographyResponse,
    Page,
)
from mars.core.errors import ProblemDetail
from mars.domain.enums import GeographyLevel
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal
from mars.services.geography_map_service import (
    DEFAULT_FEATURE_LIMIT,
    MAX_FEATURES,
    BoundingBox,
)
from mars.services.geography_service import GeographyService

router = APIRouter(prefix="/geography", tags=["geography"])

GeographyViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.GEOGRAPHY_VIEW))
]

# Error responses, published so the generated client knows what it must handle.
#
# The wider API documents only its success shapes; that is a pre-existing gap
# and retrofitting thirty operations is not this change's job. But the map
# routes have designed failure modes a client branches on - a layer too large
# to draw, a unit outside scope - and a contract that omits them would leave
# every consumer to discover them at runtime.
_DENIED: Final[dict[int | str, dict[str, Any]]] = {
    403: {
        "model": ProblemDetail,
        "description": "The caller does not hold `geography:view`.",
    }
}

#: 404 covers both "no such unit" and "outside your scope" - deliberately
#: indistinguishable, so the contract does not describe them separately either.
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    **_DENIED,
    404: {
        "model": ProblemDetail,
        "description": (
            "No such unit, or the unit is outside the caller's geography scope. "
            "These are the same response by design."
        ),
    },
}

_LAYER_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    **_NOT_FOUND,
    413: {
        "model": ProblemDetail,
        "description": (
            "More features match than the payload ceiling allows. The request is "
            "refused rather than truncated; narrow it with `parent_id` or "
            "`within_id`."
        ),
    },
}


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
            "supplied. MARS does not fabricate geography it was not given."
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


# ---------------------------------------------------------------------------
# Map delivery.
#
# These extend the geography namespace above rather than forming a second one:
# a map is a way of reading the hierarchy, not a separate resource. Every route
# resolves its subject through the same scoped service, so an out-of-scope unit
# is not merely hidden from the drawing - it is indistinguishable from a unit
# that does not exist.
# ---------------------------------------------------------------------------


@router.get(
    "/map/metadata",
    response_model=MapMetadataResponse,
    responses=_DENIED,
    summary="What this caller can draw, and from which boundary version",
)
def map_metadata(
    principal: GeographyViewer,
    service: GeographyMapServiceDep,
) -> MapMetadataResponse:
    """Describe the drawable geography before any geometry is fetched.

    One request, deliberately: a client that needs several calls to learn which
    boundary version it is drawing will eventually draw two at once.
    """
    metadata = service.map_metadata(principal)
    return MapMetadataResponse(
        is_available=metadata.is_available,
        boundary_version_id=metadata.boundary_version_id,
        boundary_version_code=metadata.boundary_version_code,
        boundary_version_label=metadata.boundary_version_label,
        source_name=metadata.source_name,
        source_checksum=metadata.source_checksum,
        imported_at=metadata.imported_at,
        initial_bounds=_bounds_model(metadata.initial_bounds),
        initial_unit_id=metadata.initial_unit_id,
        initial_unit_name=metadata.initial_unit_name,
        initial_unit_level=metadata.initial_unit_level,
        levels=[
            MapLevelAvailability(
                level=level.level,
                unit_count=level.unit_count,
                geometry_count=level.geometry_count,
                simplification_tolerance_deg=level.simplification_tolerance_deg,
                is_drawable=level.is_drawable,
                supports_national_layer=level.supports_national_layer,
            )
            for level in metadata.levels
        ],
        geometry_resolution=metadata.geometry_resolution,
        max_features=metadata.max_features,
        generated_at=metadata.generated_at,
    )


@router.get(
    "/map/features",
    response_model=MapFeatureCollection,
    responses=_LAYER_RESPONSES,
    summary="Simplified boundary geometry for one level, as GeoJSON",
)
def map_features(
    principal: GeographyViewer,
    service: GeographyMapServiceDep,
    request: Request,
    response: Response,
    level: Annotated[GeographyLevel, Query(description="Hierarchy level to draw")],
    parent_id: Annotated[
        uuid.UUID | None, Query(description="Restrict to direct children of this unit")
    ] = None,
    within_id: Annotated[
        uuid.UUID | None,
        Query(description="Restrict to any descendant of this unit at the requested level"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_FEATURES)] = DEFAULT_FEATURE_LIMIT,
) -> Any:
    """Return one drawable layer.

    parent_id selects direct children; within_id selects every descendant
    at the requested level, which is how a district drills to its subcounties -
    those hang off counties, so a parent filter would return nothing.

    Always simplified: full-resolution geometry is the analytical copy and never
    leaves the server. A request matching more units than the ceiling allows is
    refused with 413 rather than truncated, because a partial map that looks
    complete is worse than an error.

    The response carries a strong ETag derived from the boundary version and the
    query, so a client re-opening the national view revalidates in a few bytes.
    A matching ``If-None-Match`` returns 304.
    """
    collection = service.feature_collection(
        principal, level=level, parent_id=parent_id, within_id=within_id, limit=limit
    )

    if collection.etag:
        # The validator covers the caller's geography scope as well as the
        # query, so it identifies the representation rather than the URL. Two
        # callers with different scopes get different bodies and different
        # ETags, and one cannot revalidate against the other.
        response.headers["ETag"] = collection.etag
        # Private: the payload depends on the caller's scope, so a shared cache
        # must never serve one user's layer to another.
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
        # Defence in depth for intermediaries that key on headers, not a
        # substitute for the representation-correct ETag above: Vary does not
        # help a *private* cache that has already stored one user's body under
        # this URL, which is exactly the logout-and-login case.
        response.headers["Vary"] = "Authorization"
        if request.headers.get("if-none-match") == collection.etag:
            return Response(status_code=304, headers=dict(response.headers))

    return collection.as_geojson()


@router.get(
    "/map/context",
    response_model=MapFeatureCollection,
    responses=_LAYER_RESPONSES,
    summary="Public administrative geometry for map context, with in-scope flags",
)
def map_context(
    principal: GeographyViewer,
    service: GeographyMapServiceDep,
    request: Request,
    response: Response,
    level: Annotated[
        GeographyLevel, Query(description="Country, region or district. Finer grains are refused.")
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_FEATURES)] = DEFAULT_FEATURE_LIMIT,
) -> Any:
    """Return Uganda's public administrative outlines at one national grain.

    Every published unit at that level is included so a Pader-scoped map can
    still draw the rest of the country. ``in_scope`` says whether the caller
    may open the unit. No indicator, signal, investigation or commodity value
    is attached.

    Subcounty and finer requests are refused: those are not a context view.
    """
    collection = service.context_collection(principal, level=level, limit=limit)

    if collection.etag:
        response.headers["ETag"] = collection.etag
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
        response.headers["Vary"] = "Authorization"
        if request.headers.get("if-none-match") == collection.etag:
            return Response(status_code=304, headers=dict(response.headers))

    return collection.as_geojson()


@router.get(
    "/national",
    response_model=NationalGeographyResponse,
    responses=_DENIED,
    summary="The caller's root geography and the level below it",
)
def national_geography(
    principal: GeographyViewer,
    service: GeographyServiceDep,
    map_service: GeographyMapServiceDep,
) -> NationalGeographyResponse:
    """Open the map at the top of the caller's scope.

    "National" means the highest unit this caller can see, which for a district
    account is their district. Deriving it from scope rather than assuming
    Uganda means a delegated account opens correctly with no special case in the
    client.
    """
    metadata = map_service.map_metadata(principal)
    if metadata.initial_unit_id is None:
        return NationalGeographyResponse(
            root=None,
            bounds=None,
            child_level=None,
            children=[],
            boundary_version_id=metadata.boundary_version_id,
            boundary_version_code=metadata.boundary_version_code,
        )

    root = service.get_unit(principal, metadata.initial_unit_id)
    children = service.children_of(principal, root.id)

    return NationalGeographyResponse(
        root=_to_summary(root),
        bounds=_bounds_model(metadata.initial_bounds),
        child_level=children[0].level.value if children else None,
        children=[_to_summary(child) for child in children],
        boundary_version_id=metadata.boundary_version_id,
        boundary_version_code=metadata.boundary_version_code,
    )


@router.get(
    "/units/{unit_id}/geometry",
    response_model=MapFeature,
    responses=_NOT_FOUND,
    summary="One unit's simplified boundary, as a GeoJSON Feature",
)
def unit_geometry(
    unit_id: uuid.UUID,
    principal: GeographyViewer,
    service: GeographyMapServiceDep,
) -> Any:
    return service.unit_geometry(principal, unit_id)


@router.get(
    "/units/{unit_id}/bounds",
    response_model=BoundingBoxModel,
    responses=_NOT_FOUND,
    summary="One unit's extent, without transferring its geometry",
)
def unit_bounds(
    unit_id: uuid.UUID,
    principal: GeographyViewer,
    service: GeographyMapServiceDep,
) -> BoundingBoxModel:
    """Four numbers, so a client can zoom to a district without downloading it."""
    bounds = service.unit_bounds(principal, unit_id)
    return BoundingBoxModel(
        min_lon=bounds.min_lon,
        min_lat=bounds.min_lat,
        max_lon=bounds.max_lon,
        max_lat=bounds.max_lat,
    )


@router.get(
    "/units/{unit_id}/breadcrumbs",
    response_model=GeographyBreadcrumbsResponse,
    responses=_NOT_FOUND,
    summary="The ancestor chain from the caller's root down to this unit",
)
def unit_breadcrumbs(
    unit_id: uuid.UUID,
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> GeographyBreadcrumbsResponse:
    """Country / region / district / subcounty, ending with the unit itself.

    Only ancestors the caller may see appear, so a district user's trail starts
    at the highest unit in their scope rather than revealing the chain above it.
    """
    unit = service.get_unit(principal, unit_id)
    chain = [*service.ancestors_of(principal, unit_id), unit]
    return GeographyBreadcrumbsResponse(
        breadcrumbs=[
            GeographyBreadcrumb(
                unit_id=step.id,
                level=step.level.value,
                code=step.preferred_code,
                name=step.raw_name,
                is_current=step.id == unit.id,
            )
            for step in chain
        ]
    )


@router.get(
    "/districts/{code}",
    response_model=GeographyUnitDetail,
    responses=_NOT_FOUND,
    summary="Look up a district by its code",
)
def get_district(
    code: str,
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> GeographyUnitDetail:
    """Resolve a district code to a unit.

    A code outside the caller's scope raises the same not-found as a code that
    was never issued.
    """
    return _unit_detail(service, principal, GeographyLevel.DISTRICT, code)


@router.get(
    "/subcounties/{code}",
    response_model=GeographyUnitDetail,
    responses=_NOT_FOUND,
    summary="Look up a subcounty by its code",
)
def get_subcounty(
    code: str,
    principal: GeographyViewer,
    service: GeographyServiceDep,
) -> GeographyUnitDetail:
    return _unit_detail(service, principal, GeographyLevel.SUBCOUNTY, code)


def _unit_detail(
    service: GeographyService,
    principal: AuthenticatedPrincipal,
    level: GeographyLevel,
    code: str,
) -> GeographyUnitDetail:
    """Shared body for the code lookups, so both levels answer identically."""
    unit = service.get_unit_by_code(principal, level, code)
    ancestors = service.ancestors_of(principal, unit.id)
    children = service.children_of(principal, unit.id)
    return GeographyUnitDetail(
        **_to_summary(unit).model_dump(),
        boundary_version_id=unit.boundary_version_id,
        ancestors=[_to_summary(a) for a in ancestors],
        child_count=len(children),
        has_geometry=unit.geometry is not None,
    )


def _bounds_model(bounds: BoundingBox | None) -> BoundingBoxModel | None:
    if bounds is None:
        return None
    return BoundingBoxModel(
        min_lon=bounds.min_lon,
        min_lat=bounds.min_lat,
        max_lon=bounds.max_lon,
        max_lat=bounds.max_lat,
    )
