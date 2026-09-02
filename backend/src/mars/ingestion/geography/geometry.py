"""Geometry validation, repair policy and simplification.

The supplied sources contain 22 degenerate rings and other digitising
artefacts. None is edited: the source bytes stay immutable, the defect is
recorded against the unit, and repair happens only in the derived geometry MARS
stores.

Every decision here is explicit and reproducible. A geometry that cannot be
repaired safely is quarantined with its reason rather than silently dropped or
silently coerced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from mars.domain.enums import GeographyLevel, GeometryValidityState

#: A ring enclosing less than this is a digitising artefact, not a boundary.
#:
#: From the audited sources: the observed degenerate rings enclose between
#: 5.8e-13 and 6.3e-9 square degrees, while the smallest genuine subcounty
#: encloses 1.4e-4 - four orders of magnitude clear of the threshold.
#:
#: Area is the discriminator, deliberately. Vertex count is not: a legitimate
#: rectangular boundary has five points (four corners plus the closing point),
#: so disqualifying on vertex count would discard real geography.
DEGENERATE_MAX_AREA_DEG2 = 1e-7

#: Minimum vertices for a closed linear ring under the GeoJSON specification.
MINIMUM_RING_VERTICES = 4

#: Uganda's bounding box, from the audited sources. A feature outside this is a
#: coordinate-order or projection error, not a valid Ugandan boundary.
UGANDA_BBOX = (29.0, -2.0, 35.5, 4.5)

#: Simplification tolerance per level, in degrees.
#:
#: Chosen so a national payload stays browser-safe while a boundary remains
#: recognisable at the zoom that level is viewed at. Roughly: 0.01 degrees is
#: about 1.1 km at the equator. Subcounties carry 1.67 million vertices raw and
#: are the reason this exists at all.
WEB_SIMPLIFICATION_TOLERANCE: dict[GeographyLevel, float] = {
    GeographyLevel.COUNTRY: 0.0050,
    GeographyLevel.REGION: 0.0040,
    GeographyLevel.DISTRICT: 0.0020,
    GeographyLevel.COUNTY: 0.0015,
    GeographyLevel.SUBCOUNTY: 0.0010,
    GeographyLevel.PARISH: 0.0005,
    GeographyLevel.VILLAGE: 0.0005,
}


class RepairPolicy(str):
    """Named, reproducible repair actions.

    Each value states exactly what was done, so a repaired boundary can be
    explained rather than merely flagged.
    """

    NONE = "none"
    DROPPED_DEGENERATE_RINGS = "dropped_degenerate_rings"
    PROMOTED_TO_MULTIPOLYGON = "promoted_to_multipolygon"
    DROPPED_DEGENERATE_AND_PROMOTED = "dropped_degenerate_rings_and_promoted"


@dataclass(slots=True)
class GeometryAssessment:
    """The outcome of validating and preparing one feature's geometry."""

    #: GeoJSON geometry ready for PostGIS, always MultiPolygon, or None when the
    #: feature is quarantined.
    prepared: dict[str, Any] | None
    validity_state: GeometryValidityState
    repair_method: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    part_count: int = 0
    ring_count: int = 0
    vertex_count: int = 0
    bbox: tuple[float, float, float, float] | None = None
    planar_area_deg2: float = 0.0

    @property
    def is_usable(self) -> bool:
        return self.prepared is not None

    def issue_codes(self) -> list[str]:
        return [issue["code"] for issue in self.issues]

    def as_dict(self) -> dict[str, Any]:
        return {
            "validity_state": self.validity_state.value,
            "repair_method": self.repair_method,
            "issues": list(self.issues),
            "part_count": self.part_count,
            "ring_count": self.ring_count,
            "vertex_count": self.vertex_count,
            "bbox": list(self.bbox) if self.bbox else None,
            "planar_area_deg2": self.planar_area_deg2,
        }


def signed_area(ring: list[list[float]]) -> float:
    """Shoelace area. Sign indicates winding: positive is counter-clockwise."""
    total = 0.0
    for index in range(len(ring) - 1):
        total += ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
    return total / 2.0


def ring_is_closed(ring: list[list[float]]) -> bool:
    if len(ring) < 2:
        return False
    return math.isclose(ring[0][0], ring[-1][0], abs_tol=1e-9) and math.isclose(
        ring[0][1], ring[-1][1], abs_tol=1e-9
    )


def is_degenerate(ring: list[list[float]]) -> bool:
    """Whether a ring is a digitising artefact rather than a real boundary.

    Judged on enclosed area alone. A ring with too few points to close at all is
    caught separately by the minimum-vertex check, which is a well-formedness
    rule rather than a judgement about size.
    """
    return abs(signed_area(ring)) < DEGENERATE_MAX_AREA_DEG2


def assess(geometry: dict[str, Any] | None, *, label: str) -> GeometryAssessment:
    """Validate a source geometry and prepare it for storage.

    Returns a MultiPolygon in every usable case, because the storage column is
    MultiPolygon and promoting at read time keeps one type through the whole
    system.
    """
    if geometry is None:
        return GeometryAssessment(
            prepared=None,
            validity_state=GeometryValidityState.INVALID_UNREPAIRED,
            repair_method=RepairPolicy.NONE,
            issues=[{"code": "null_geometry", "detail": f"{label} has no geometry"}],
        )

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []

    if geometry_type == "Polygon":
        polygons = [coordinates]
        promoted = True
    elif geometry_type == "MultiPolygon":
        polygons = list(coordinates)
        promoted = False
    else:
        return GeometryAssessment(
            prepared=None,
            validity_state=GeometryValidityState.INVALID_UNREPAIRED,
            repair_method=RepairPolicy.NONE,
            issues=[
                {
                    "code": "unsupported_geometry_type",
                    "detail": f"{label} is {geometry_type!r}; only Polygon and MultiPolygon load",
                }
            ],
        )

    issues: list[dict[str, Any]] = []
    kept_polygons: list[list[list[list[float]]]] = []
    dropped_rings = 0
    ring_count = 0
    vertex_count = 0
    area = 0.0
    bounds = [math.inf, math.inf, -math.inf, -math.inf]

    for polygon_index, polygon in enumerate(polygons):
        kept_rings: list[list[list[float]]] = []
        for ring_index, ring in enumerate(polygon):
            ring_count += 1

            if len(ring) < MINIMUM_RING_VERTICES:
                dropped_rings += 1
                issues.append(
                    {
                        "code": "ring_below_minimum_vertices",
                        "polygon": polygon_index,
                        "ring": ring_index,
                        "vertices": len(ring),
                    }
                )
                continue

            if not ring_is_closed(ring):
                issues.append(
                    {
                        "code": "unclosed_ring",
                        "polygon": polygon_index,
                        "ring": ring_index,
                    }
                )
                ring = [*ring, ring[0]]

            if is_degenerate(ring):
                dropped_rings += 1
                issues.append(
                    {
                        "code": "degenerate_ring",
                        "polygon": polygon_index,
                        "ring": ring_index,
                        "vertices": len(ring),
                        "abs_area_deg2": abs(signed_area(ring)),
                    }
                )
                continue

            kept_rings.append(ring)
            vertex_count += len(ring)
            area += abs(signed_area(ring)) if not kept_rings[:-1] else -abs(signed_area(ring))

            for point in ring:
                bounds[0] = min(bounds[0], point[0])
                bounds[1] = min(bounds[1], point[1])
                bounds[2] = max(bounds[2], point[0])
                bounds[3] = max(bounds[3], point[1])

        # A polygon whose exterior ring was dropped has nothing left to anchor
        # its holes, so the whole part goes.
        if kept_rings:
            kept_polygons.append(kept_rings)

    if not kept_polygons:
        return GeometryAssessment(
            prepared=None,
            validity_state=GeometryValidityState.INVALID_UNREPAIRED,
            repair_method=RepairPolicy.NONE,
            issues=[*issues, {"code": "no_usable_rings", "detail": f"{label} has no valid ring"}],
            ring_count=ring_count,
        )

    bbox = (bounds[0], bounds[1], bounds[2], bounds[3])
    if not _within_uganda(bbox):
        issues.append(
            {
                "code": "bbox_outside_expected_extent",
                "bbox": list(bbox),
                "expected": list(UGANDA_BBOX),
            }
        )

    if dropped_rings and promoted:
        repair = RepairPolicy.DROPPED_DEGENERATE_AND_PROMOTED
    elif dropped_rings:
        repair = RepairPolicy.DROPPED_DEGENERATE_RINGS
    elif promoted:
        repair = RepairPolicy.PROMOTED_TO_MULTIPOLYGON
    else:
        repair = RepairPolicy.NONE

    state = GeometryValidityState.INVALID_REPAIRED if dropped_rings else GeometryValidityState.VALID

    return GeometryAssessment(
        prepared={"type": "MultiPolygon", "coordinates": kept_polygons},
        validity_state=state,
        repair_method=repair,
        issues=issues,
        part_count=len(kept_polygons),
        ring_count=ring_count,
        vertex_count=vertex_count,
        bbox=bbox,
        planar_area_deg2=area,
    )


def _within_uganda(bbox: tuple[float, float, float, float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        UGANDA_BBOX[0] <= min_lon <= UGANDA_BBOX[2]
        and UGANDA_BBOX[1] <= min_lat <= UGANDA_BBOX[3]
        and UGANDA_BBOX[0] <= max_lon <= UGANDA_BBOX[2]
        and UGANDA_BBOX[1] <= max_lat <= UGANDA_BBOX[3]
    )


def tolerance_for(level: GeographyLevel) -> float:
    """Simplification tolerance for a level's browser geometry."""
    return WEB_SIMPLIFICATION_TOLERANCE[level]
