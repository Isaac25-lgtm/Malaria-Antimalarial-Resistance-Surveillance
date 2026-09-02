"""Map delivery: scope-safe geometry for a browser.

Separate from :class:`~mars.services.geography_service.GeographyService`, which
answers questions about the hierarchy. This service answers a different one -
"what may this caller draw, and how small can it be sent" - and that concern
owns the payload budget, the property allow-list and the cache validator.

Three rules shape everything here:

**Simplified geometry only.** ``geom`` is the analytical copy and stays on the
server. The subcounty layer carries 1.67 million vertices; a request that could
return it would be a denial-of-service vector wearing a map. Every response is
built from ``geom_web``.

**Scope is a WHERE clause.** The predicate is borrowed from ``GeographyService``
rather than reimplemented, so map visibility and hierarchy visibility cannot
drift apart. A district user's feature collection never contains another
district, because the row never leaves the database.

**Properties are an allow-list.** A feature carries the fields named in
:data:`FEATURE_PROPERTIES` and nothing else. Adding a column to the geometry
table must never widen a public payload by accident.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.orm import Session

from mars.core.errors import MarsError, NotFoundError
from mars.domain.enums import BoundaryImportStatus, GeographyLevel
from mars.domain.geography import BoundaryVersion, GeographyUnit, GeographyUnitGeometry
from mars.security.principal import AuthenticatedPrincipal
from mars.services.geography_service import GeographyService

#: Properties a map feature is allowed to carry.
#:
#: Everything here is administrative reference data that is already public: the
#: name and code of an administrative area, its place in the hierarchy, and its
#: measured size. Nothing derived from health data appears, and nothing may be
#: added without a decision, which is what the allow-list makes visible.
#:
#: ``area_sq_km`` is measured server-side on the geography type. It is included
#: because a map legend that offers a rate per unit area must not invite the
#: client to compute the denominator from projected screen geometry.
FEATURE_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        "unit_id",
        "level",
        "code",
        "name",
        "parent_id",
        "path",
        "area_sq_km",
        "is_active",
    }
)

#: Hard ceiling on features in one response, whatever the caller asks for.
#:
#: The largest legitimate national request is the 146-district layer. 2,190
#: subcounties nationally is 3.2 MB of simplified GeoJSON and is not a view any
#: screen needs, so the ceiling sits above the former and below the latter: a
#: national subcounty request is refused rather than silently truncated into a
#: map that looks complete and is not.
MAX_FEATURES: Final[int] = 400

#: Default when the caller names no limit.
DEFAULT_FEATURE_LIMIT: Final[int] = 200

#: Levels a browser may request as a whole layer.
#:
#: Subcounty is absent deliberately: it is drawn one district at a time, which
#: the ``parent_id`` filter expresses. Requesting every subcounty in Uganda is
#: not a supported view.
NATIONAL_LAYER_LEVELS: Final[frozenset[GeographyLevel]] = frozenset(
    {GeographyLevel.COUNTRY, GeographyLevel.REGION, GeographyLevel.DISTRICT}
)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A geographic extent, in EPSG:4326 degrees."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def as_list(self) -> list[float]:
        """GeoJSON bbox order: west, south, east, north."""
        return [self.min_lon, self.min_lat, self.max_lon, self.max_lat]


@dataclass(frozen=True, slots=True)
class LevelAvailability:
    """What exists at one hierarchy level, and whether it can be drawn."""

    level: str
    unit_count: int
    geometry_count: int
    simplification_tolerance_deg: float | None
    #: False where the level is supported by the schema but nothing is loaded.
    #: A map legend must distinguish "no parishes supplied" from "no parishes".
    is_drawable: bool
    #: True where the whole level may be requested without a parent filter.
    supports_national_layer: bool


@dataclass(frozen=True, slots=True)
class MapMetadata:
    """Everything a map client needs before it draws anything.

    Deliberately answerable in one request. A map that has to make four calls
    before it knows which boundary version it is drawing will eventually draw
    two versions at once.
    """

    boundary_version_id: uuid.UUID | None
    boundary_version_code: str | None
    boundary_version_label: str | None
    source_name: str | None
    source_checksum: str | None
    imported_at: datetime | None
    #: Extent of the highest unit the caller can see, for the initial viewport.
    initial_bounds: BoundingBox | None
    initial_unit_id: uuid.UUID | None
    initial_unit_name: str | None
    initial_unit_level: str | None
    levels: list[LevelAvailability]
    geometry_resolution: str
    max_features: int
    #: Null when nothing is published. The client shows "no boundaries loaded"
    #: rather than an empty map that looks like a rendering failure.
    is_available: bool
    generated_at: datetime


@dataclass(slots=True)
class FeatureCollection:
    """A GeoJSON FeatureCollection plus the metadata a cache and a legend need."""

    features: list[dict[str, Any]] = field(default_factory=list)
    boundary_version_id: uuid.UUID | None = None
    boundary_version_code: str | None = None
    level: str | None = None
    parent_id: uuid.UUID | None = None
    within_id: uuid.UUID | None = None
    bbox: BoundingBox | None = None
    #: Always false, by construction: a request that would exceed the limit is
    #: refused rather than trimmed. The field is carried anyway because it says
    #: so to the client - a collection that arrives is a complete answer to the
    #: question asked, and in surveillance a silently partial map is worse than
    #: no map. If truncation is ever introduced, this is where it is declared.
    truncated: bool = False
    matched_count: int = 0
    etag: str = ""

    def as_geojson(self) -> dict[str, Any]:
        """The wire form, with MARS metadata under a namespaced key.

        Foreign members are legal in GeoJSON. Putting the boundary version
        inside the document rather than only in a header means a saved or
        forwarded response still says which boundaries it came from.
        """
        document: dict[str, Any] = {
            "type": "FeatureCollection",
            "features": self.features,
            "mars": {
                "boundary_version_id": str(self.boundary_version_id)
                if self.boundary_version_id
                else None,
                "boundary_version_code": self.boundary_version_code,
                "level": self.level,
                "parent_id": str(self.parent_id) if self.parent_id else None,
                "within_id": str(self.within_id) if self.within_id else None,
                "geometry_resolution": "simplified",
                "feature_count": len(self.features),
                "matched_count": self.matched_count,
                "truncated": self.truncated,
            },
        }
        if self.bbox is not None:
            document["bbox"] = self.bbox.as_list()
        return document


class FeatureLimitExceededError(MarsError):
    """The requested layer is larger than the payload budget allows.

    Refused rather than truncated. A national subcounty map that quietly stops
    at 400 features is a map with a hole in it, and nothing on screen would say
    so. The response names the matched count and the ceiling, so the client can
    tell the user how to narrow the request instead of retrying the same one.
    """

    status_code = 413
    code = "geography_request_too_broad"
    title = "Requested map layer is too large"

    def __init__(self, level: str, matched: int, limit: int) -> None:
        self.level = level
        self.matched = matched
        self.limit = limit
        super().__init__(
            f"{matched} {level} features match this request, above the {limit} "
            "feature ceiling. Narrow it by naming a parent unit.",
            context={"level": level, "matched": matched, "limit": limit},
        )


class GeographyMapService:
    """Reads renderable geometry within the caller's geography scope."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._hierarchy = GeographyService(session)

    # -- Published version -------------------------------------------------
    def published_version(self) -> BoundaryVersion | None:
        """The one published boundary version, or None if none is published.

        A partial unique index guarantees at most one, so this cannot be
        ambiguous.
        """
        return self._session.execute(
            select(BoundaryVersion).where(
                BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED
            )
        ).scalar_one_or_none()

    # -- Scope -------------------------------------------------------------
    def _scoped(self, statement: Select[Any], principal: AuthenticatedPrincipal) -> Select[Any]:
        """Apply the caller's geography scope.

        Reuses the hierarchy service's predicate rather than restating it. Two
        implementations of "what may this user see" is one more than a system
        should ever have.
        """
        if principal.has_national_scope:
            return statement
        if not principal.geography_scopes:
            return statement.where(false())
        return statement.where(or_(*GeographyService._scope_conditions(principal)))

    # -- Metadata ----------------------------------------------------------
    def map_metadata(self, principal: AuthenticatedPrincipal) -> MapMetadata:
        """Describe what this caller can draw, and from which boundary version."""
        version = self.published_version()
        now = datetime.now(UTC)

        if version is None:
            return MapMetadata(
                boundary_version_id=None,
                boundary_version_code=None,
                boundary_version_label=None,
                source_name=None,
                source_checksum=None,
                imported_at=None,
                initial_bounds=None,
                initial_unit_id=None,
                initial_unit_name=None,
                initial_unit_level=None,
                levels=[],
                geometry_resolution="simplified",
                max_features=MAX_FEATURES,
                is_available=False,
                generated_at=now,
            )

        levels = self._level_availability(principal, version)
        root = self._scope_root(principal, version)

        return MapMetadata(
            boundary_version_id=version.id,
            boundary_version_code=version.code,
            boundary_version_label=version.label,
            source_name=version.source_name,
            source_checksum=version.source_checksum,
            imported_at=version.imported_at,
            initial_bounds=root[0] if root else None,
            initial_unit_id=root[1] if root else None,
            initial_unit_name=root[2] if root else None,
            initial_unit_level=root[3] if root else None,
            levels=levels,
            geometry_resolution="simplified",
            max_features=MAX_FEATURES,
            is_available=any(level.is_drawable for level in levels),
            generated_at=now,
        )

    def _level_availability(
        self, principal: AuthenticatedPrincipal, version: BoundaryVersion
    ) -> list[LevelAvailability]:
        """Per-level counts within scope, including levels that hold nothing."""
        statement = (
            select(
                GeographyUnit.level,
                func.count(GeographyUnit.id),
                func.count(GeographyUnitGeometry.geom_web),
                func.min(GeographyUnitGeometry.simplification_tolerance_deg),
            )
            .outerjoin(
                GeographyUnitGeometry,
                GeographyUnitGeometry.geography_unit_id == GeographyUnit.id,
            )
            .where(
                GeographyUnit.boundary_version_id == version.id,
                GeographyUnit.is_active.is_(True),
            )
            .group_by(GeographyUnit.level)
        )
        rows = self._session.execute(self._scoped(statement, principal)).all()
        by_level = {row[0].value: (row[1], row[2], row[3]) for row in rows}

        return [
            LevelAvailability(
                level=level.value,
                unit_count=by_level.get(level.value, (0, 0, None))[0],
                geometry_count=by_level.get(level.value, (0, 0, None))[1],
                simplification_tolerance_deg=by_level.get(level.value, (0, 0, None))[2],
                is_drawable=by_level.get(level.value, (0, 0, None))[1] > 0,
                supports_national_layer=level in NATIONAL_LAYER_LEVELS,
            )
            for level in GeographyLevel
        ]

    def _scope_root(
        self, principal: AuthenticatedPrincipal, version: BoundaryVersion
    ) -> tuple[BoundingBox, uuid.UUID, str, str] | None:
        """The unit the caller's map should open on, and its extent.

        This is the caller's *assigned* geography, not the shallowest unit they
        can see. The two differ, and the difference matters: the scope predicate
        deliberately admits ancestors so a district user can render the
        breadcrumb "Uganda / Northern / Gulu", which means Uganda is visible to
        them. Opening the viewport on the shallowest visible unit would zoom a
        district officer out to the whole country on every page load.

        A national account's scope root *is* the country, so both readings agree
        there and no special case is needed.
        """
        base = (
            select(
                GeographyUnit.id,
                GeographyUnit.raw_name,
                GeographyUnit.level,
                GeographyUnitGeometry.bbox_min_lon,
                GeographyUnitGeometry.bbox_min_lat,
                GeographyUnitGeometry.bbox_max_lon,
                GeographyUnitGeometry.bbox_max_lat,
            )
            .join(
                GeographyUnitGeometry,
                GeographyUnitGeometry.geography_unit_id == GeographyUnit.id,
            )
            .where(
                GeographyUnit.boundary_version_id == version.id,
                GeographyUnit.is_active.is_(True),
                GeographyUnitGeometry.bbox_min_lon.is_not(None),
            )
            .order_by(GeographyUnit.depth)
            .limit(1)
        )

        row = None
        scope_ids = [scope.geography_unit_id for scope in principal.geography_scopes]
        if scope_ids:
            row = self._session.execute(base.where(GeographyUnit.id.in_(scope_ids))).first()

        if row is None:
            # The assigned unit is not in the published version - a scope
            # pointing at a unit a later import deactivated, say. Fall back to
            # the shallowest unit still visible rather than reporting no map at
            # all, which would be a harsher failure than the situation warrants.
            row = self._session.execute(self._scoped(base, principal)).first()

        if row is None:
            return None
        return (
            BoundingBox(
                min_lon=float(row[3]),
                min_lat=float(row[4]),
                max_lon=float(row[5]),
                max_lat=float(row[6]),
            ),
            row[0],
            row[1],
            row[2].value,
        )

    # -- Features ----------------------------------------------------------
    def feature_collection(
        self,
        principal: AuthenticatedPrincipal,
        *,
        level: GeographyLevel,
        parent_id: uuid.UUID | None = None,
        within_id: uuid.UUID | None = None,
        limit: int = DEFAULT_FEATURE_LIMIT,
    ) -> FeatureCollection:
        """Simplified geometry for one level, optionally restricted to a subtree.

        Two different restrictions, because the hierarchy has five levels and a
        map has fewer useful steps than that:

        ``parent_id``  direct children only.
        ``within_id``  every descendant at the requested level, by materialised
                       path. This is what a district-to-subcounty drill needs.
                       Subcounties hang off counties, so filtering on a district
                       as *parent* would return nothing, which reads on screen
                       as "this district has no subcounties" rather than as a
                       badly formed question.

        Either id is resolved through the hierarchy service first, so an
        out-of-scope unit raises the same not-found as one that does not exist.
        Filtering on an unresolved id would instead return an empty collection,
        which would tell the caller the id was real.
        """
        version = self.published_version()
        collection = FeatureCollection(level=level.value, parent_id=parent_id, within_id=within_id)
        if version is None:
            return collection

        collection.boundary_version_id = version.id
        collection.boundary_version_code = version.code

        if parent_id is not None:
            self._hierarchy.get_unit(principal, parent_id)
        within_path: str | None = None
        if within_id is not None:
            within_path = self._hierarchy.get_unit(principal, within_id).path

        matched = self._count_features(principal, version, level, parent_id, within_path)
        collection.matched_count = matched

        effective_limit = min(max(limit, 1), MAX_FEATURES)
        if matched > effective_limit:
            raise FeatureLimitExceededError(level.value, matched, effective_limit)

        collection.etag = self._etag(version, level, parent_id, within_id, effective_limit)

        rows = self._session.execute(
            self._scoped(
                self._feature_statement(version, level, parent_id, within_path).limit(
                    effective_limit
                ),
                principal,
            )
        ).all()

        for row in rows:
            geometry = row[0]
            if not geometry:
                continue
            collection.features.append(
                {
                    "type": "Feature",
                    # The canonical unit id is the feature id: stable across
                    # re-imports, and the value every other MARS endpoint keys
                    # on, so a map click needs no translation table.
                    "id": str(row[1]),
                    "geometry": json.loads(geometry),
                    "properties": {
                        "unit_id": str(row[1]),
                        "level": row[2].value,
                        "code": row[3],
                        "name": row[4],
                        "parent_id": str(row[5]) if row[5] else None,
                        "path": row[6],
                        "area_sq_km": round(float(row[7]), 2) if row[7] is not None else None,
                        "is_active": row[8],
                    },
                }
            )

        collection.bbox = self._collection_bounds(principal, version, level, parent_id, within_path)
        return collection

    def _feature_statement(
        self,
        version: BoundaryVersion,
        level: GeographyLevel,
        parent_id: uuid.UUID | None,
        within_path: str | None = None,
    ) -> Select[Any]:
        """Select simplified geometry and the allowed properties.

        ``ST_AsGeoJSON`` runs in the database at six decimal places - roughly
        0.1 m, far finer than the simplification tolerance and far coarser than
        the 15 significant digits PostGIS would otherwise emit. Trimming here
        rather than in Python removes about a fifth of the payload before it is
        ever serialised.
        """
        statement = (
            select(
                func.ST_AsGeoJSON(GeographyUnitGeometry.geom_web, 6),
                GeographyUnit.id,
                GeographyUnit.level,
                GeographyUnit.preferred_code,
                GeographyUnit.raw_name,
                GeographyUnit.parent_id,
                GeographyUnit.path,
                GeographyUnitGeometry.area_sq_km,
                GeographyUnit.is_active,
            )
            .join(
                GeographyUnitGeometry,
                GeographyUnitGeometry.geography_unit_id == GeographyUnit.id,
            )
            .where(
                GeographyUnit.boundary_version_id == version.id,
                GeographyUnit.level == level,
                GeographyUnit.is_active.is_(True),
                GeographyUnitGeometry.geom_web.is_not(None),
            )
            .order_by(GeographyUnit.normalised_name)
        )
        return _restrict(statement, parent_id, within_path)

    def _count_features(
        self,
        principal: AuthenticatedPrincipal,
        version: BoundaryVersion,
        level: GeographyLevel,
        parent_id: uuid.UUID | None,
        within_path: str | None = None,
    ) -> int:
        """How many features the caller would receive, before any limit.

        Counted without geometry so the ceiling check costs an index scan
        rather than a serialisation of everything about to be rejected.
        """
        statement = (
            select(func.count(GeographyUnit.id))
            .join(
                GeographyUnitGeometry,
                GeographyUnitGeometry.geography_unit_id == GeographyUnit.id,
            )
            .where(
                GeographyUnit.boundary_version_id == version.id,
                GeographyUnit.level == level,
                GeographyUnit.is_active.is_(True),
                GeographyUnitGeometry.geom_web.is_not(None),
            )
        )
        statement = _restrict(statement, parent_id, within_path)
        return int(self._session.execute(self._scoped(statement, principal)).scalar_one())

    def _collection_bounds(
        self,
        principal: AuthenticatedPrincipal,
        version: BoundaryVersion,
        level: GeographyLevel,
        parent_id: uuid.UUID | None,
        within_path: str | None = None,
    ) -> BoundingBox | None:
        """Extent of the whole collection, so the client can fit its viewport.

        Aggregated from the stored per-unit boxes rather than from the returned
        geometry: it is four numbers out of an index rather than a pass over
        every returned coordinate in JavaScript.
        """
        statement = (
            select(
                func.min(GeographyUnitGeometry.bbox_min_lon),
                func.min(GeographyUnitGeometry.bbox_min_lat),
                func.max(GeographyUnitGeometry.bbox_max_lon),
                func.max(GeographyUnitGeometry.bbox_max_lat),
            )
            .select_from(GeographyUnit)
            .join(
                GeographyUnitGeometry,
                GeographyUnitGeometry.geography_unit_id == GeographyUnit.id,
            )
            .where(
                GeographyUnit.boundary_version_id == version.id,
                GeographyUnit.level == level,
                GeographyUnit.is_active.is_(True),
                GeographyUnitGeometry.bbox_min_lon.is_not(None),
            )
        )
        statement = _restrict(statement, parent_id, within_path)

        row = self._session.execute(self._scoped(statement, principal)).first()
        if row is None or row[0] is None:
            return None
        return BoundingBox(
            min_lon=float(row[0]),
            min_lat=float(row[1]),
            max_lon=float(row[2]),
            max_lat=float(row[3]),
        )

    # -- Single unit -------------------------------------------------------
    def unit_geometry(
        self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID
    ) -> dict[str, Any]:
        """One unit as a GeoJSON Feature.

        Resolved through the hierarchy service, so an out-of-scope unit is
        indistinguishable from one that does not exist.
        """
        unit = self._hierarchy.get_unit(principal, unit_id)
        row = self._session.execute(
            select(
                func.ST_AsGeoJSON(GeographyUnitGeometry.geom_web, 6),
                GeographyUnitGeometry.area_sq_km,
                GeographyUnitGeometry.bbox_min_lon,
                GeographyUnitGeometry.bbox_min_lat,
                GeographyUnitGeometry.bbox_max_lon,
                GeographyUnitGeometry.bbox_max_lat,
            ).where(GeographyUnitGeometry.geography_unit_id == unit.id)
        ).first()

        if row is None or not row[0]:
            raise NotFoundError("no boundary geometry has been loaded for this unit")

        feature: dict[str, Any] = {
            "type": "Feature",
            "id": str(unit.id),
            "geometry": json.loads(row[0]),
            "properties": {
                "unit_id": str(unit.id),
                "level": unit.level.value,
                "code": unit.preferred_code,
                "name": unit.raw_name,
                "parent_id": str(unit.parent_id) if unit.parent_id else None,
                "path": unit.path,
                "area_sq_km": round(float(row[1]), 2) if row[1] is not None else None,
                "is_active": unit.is_active,
            },
        }
        if row[2] is not None:
            feature["bbox"] = [float(row[2]), float(row[3]), float(row[4]), float(row[5])]
        return feature

    def unit_bounds(self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID) -> BoundingBox:
        """The extent of one unit, without transferring its geometry.

        A client zooming to a district needs four numbers, not 3 kB of ring.
        """
        unit = self._hierarchy.get_unit(principal, unit_id)
        row = self._session.execute(
            select(
                GeographyUnitGeometry.bbox_min_lon,
                GeographyUnitGeometry.bbox_min_lat,
                GeographyUnitGeometry.bbox_max_lon,
                GeographyUnitGeometry.bbox_max_lat,
            ).where(
                GeographyUnitGeometry.geography_unit_id == unit.id,
                GeographyUnitGeometry.bbox_min_lon.is_not(None),
            )
        ).first()
        if row is None:
            raise NotFoundError("no boundary geometry has been loaded for this unit")
        return BoundingBox(
            min_lon=float(row[0]),
            min_lat=float(row[1]),
            max_lon=float(row[2]),
            max_lat=float(row[3]),
        )

    # -- Cache validator ---------------------------------------------------
    @staticmethod
    def _etag(
        version: BoundaryVersion,
        level: GeographyLevel,
        parent_id: uuid.UUID | None,
        within_id: uuid.UUID | None,
        limit: int,
    ) -> str:
        """A strong validator for one layer of one boundary version.

        Derived from the inputs that determine the bytes, so it is stable across
        restarts and across replicas - a timestamp or a process-local counter
        would not be. Publishing a new boundary version changes ``code`` and so
        invalidates every layer at once, which is correct: the hierarchy moved.

        The caller's scope is deliberately absent. Responses are private to the
        request and never shared between users, and mixing a principal into the
        validator would make it a weak identifier of who fetched what.
        """
        material = "|".join(
            [
                version.code,
                str(version.id),
                level.value,
                str(parent_id) if parent_id else "-",
                str(within_id) if within_id else "-",
                str(limit),
            ]
        )
        return f'"{hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]}"'


def _restrict(
    statement: Select[Any], parent_id: uuid.UUID | None, within_path: str | None
) -> Select[Any]:
    """Apply the direct-child and subtree restrictions to any of the queries.

    Shared so the feature query, the count that guards the ceiling and the
    bounding-box aggregate can never disagree about what was asked for. A count
    matching a different set than the features would make the ceiling check
    meaningless.
    """
    if parent_id is not None:
        statement = statement.where(GeographyUnit.parent_id == parent_id)
    if within_path is not None:
        # Prefixed with the separator so "UG/3/30" cannot match "UG/3/304".
        statement = statement.where(GeographyUnit.path.like(f"{within_path}/%"))
    return statement


def geojson_property_names(feature: dict[str, Any]) -> set[str]:
    """Property keys carried by a feature. Used by the allow-list tests."""
    properties = feature.get("properties")
    return set(properties) if isinstance(properties, dict) else set()


__all__ = [
    "DEFAULT_FEATURE_LIMIT",
    "FEATURE_PROPERTIES",
    "MAX_FEATURES",
    "NATIONAL_LAYER_LEVELS",
    "BoundingBox",
    "FeatureCollection",
    "FeatureLimitExceededError",
    "GeographyMapService",
    "LevelAvailability",
    "MapMetadata",
    "geojson_property_names",
]
