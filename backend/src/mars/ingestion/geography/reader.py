"""Streaming readers for the supplied boundary sources.

The subcounty source is 156 MB. ``json.load`` would hold the entire parsed
structure - several gigabytes of Python objects - for no benefit, so features are
decoded one at a time and discarded as they are consumed.

Sources are opened read-only. Nothing in this module writes to, moves or
reformats a source file; the checksum recorded alongside each read is what
proves it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

#: Read in 1 MB blocks when checksumming, so a large file never lands in memory
#: twice.
_CHECKSUM_BLOCK = 1024 * 1024


class SourceFormat(str, Enum):
    """Serialisation of a boundary source, detected rather than assumed."""

    GEOJSON = "geojson"
    ESRI_FEATURESET = "esri_featureset"
    UNKNOWN = "unknown"


class SourceRole(str, Enum):
    """What a source file is used for.

    ADR 0004 assigns these. The two district files are the same dataset in two
    formats, so they carry different roles rather than one superseding the other.
    """

    COUNTRY_BOUNDARY = "country_boundary"
    """National outline and the area control total for import validation."""

    DISTRICT_GEOMETRY = "district_geometry"
    """District geometry. RFC 7946 winding, loads into PostGIS unreversed."""

    DISTRICT_PROVENANCE = "district_provenance"
    """Esri twin. Declares the CRS and the field schema. Never imported."""

    SUBCOUNTY_HIERARCHY = "subcounty_hierarchy"
    """The hierarchy spine: the only source carrying FScode, County and District
    together, plus subcounty geometry."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A boundary source and the role it plays."""

    path: Path
    role: SourceRole

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(slots=True)
class Feature:
    """One decoded feature, normalised across the two serialisations."""

    index: int
    properties: dict[str, Any]
    #: GeoJSON geometry mapping. Esri ``rings`` are converted to a GeoJSON
    #: Polygon or MultiPolygon here so that everything downstream sees one shape.
    geometry: dict[str, Any] | None

    @property
    def geometry_type(self) -> str:
        if self.geometry is None:
            return "null"
        return str(self.geometry.get("type", "unknown"))


@dataclass(slots=True)
class SourceProfile:
    """What a read of a source file observed.

    Recorded on the boundary version so a later reader can see what the importer
    actually saw, rather than trusting that the file has not moved on.
    """

    filename: str
    role: SourceRole
    format: SourceFormat
    sha256: str
    size_bytes: int
    modified_utc: str
    declared_crs: str | None
    crs_note: str
    feature_count: int = 0
    geometry_types: dict[str, int] = field(default_factory=dict)
    attribute_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "role": self.role.value,
            "format": self.format.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "modified_utc": self.modified_utc,
            "declared_crs": self.declared_crs,
            "crs_note": self.crs_note,
            "feature_count": self.feature_count,
            "geometry_types": dict(self.geometry_types),
            "attribute_fields": list(self.attribute_fields),
        }


def sha256_of(path: Path) -> str:
    """SHA-256 of a file, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHECKSUM_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def detect_format(text_head: str, sample: str) -> SourceFormat:
    """Identify the serialisation from the document preamble."""
    if '"geometryType"' in text_head or '"attributes"' in sample:
        return SourceFormat.ESRI_FEATURESET
    if '"FeatureCollection"' in text_head:
        return SourceFormat.GEOJSON
    return SourceFormat.UNKNOWN


def detect_crs(preamble: str, source_format: SourceFormat) -> tuple[str | None, str]:
    """Return the declared CRS and a note explaining how it was established.

    Only the Esri twin declares a CRS. The GeoJSON files declare none, which
    under RFC 7946 means WGS 84 - a default the importer records explicitly
    rather than applying silently.
    """
    if '"wkid"' in preamble:
        # Read digits only up to the first non-digit. Taking every digit in a
        # fixed window would also swallow "latestWkid", producing "43264326".
        after = preamble[preamble.index('"wkid"') + len('"wkid"') :].lstrip(": 	")
        digits = ""
        for character in after:
            if not character.isdigit():
                break
            digits += character
        if digits:
            return f"EPSG:{digits}", "declared in spatialReference.wkid"
    if '"crs"' in preamble:
        return None, "a crs member is present but was not parsed; review before import"
    if source_format is SourceFormat.GEOJSON:
        return None, "none declared; RFC 7946 defaults to WGS 84 (EPSG:4326)"
    return None, "none declared"


def esri_rings_to_geojson(rings: list[list[list[float]]]) -> dict[str, Any]:
    """Convert Esri ``rings`` to a GeoJSON geometry.

    Esri does not distinguish an interior ring from a separate part; the winding
    carries that meaning. Under the Esri convention an exterior ring is
    clockwise (negative shoelace area) and a hole is counter-clockwise.

    This is used only for the provenance twin, which is never imported. It
    exists so the two district sources can be compared feature by feature.
    """
    if not rings:
        return {"type": "Polygon", "coordinates": []}

    polygons: list[list[list[list[float]]]] = []
    for ring in rings:
        if _signed_area(ring) < 0 or not polygons:
            polygons.append([ring])
        else:
            polygons[-1].append(ring)

    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _signed_area(ring: list[list[float]]) -> float:
    total = 0.0
    for index in range(len(ring) - 1):
        total += ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
    return total / 2.0


class BoundarySourceReader:
    """Reads one boundary source, streaming its features."""

    def __init__(self, source: SourceFile) -> None:
        self._source = source
        self._decoder = json.JSONDecoder()

    @property
    def source(self) -> SourceFile:
        return self._source

    def profile(self) -> SourceProfile:
        """Describe the file without decoding every feature."""
        from datetime import UTC, datetime

        path = self._source.path
        if not path.exists():
            raise FileNotFoundError(f"boundary source not found: {path}")

        stat = path.stat()
        with path.open(encoding="utf-8") as handle:
            head = handle.read(4096)

        source_format = detect_format(head[:400], head)
        preamble = head.split('"features"', 1)[0] if '"features"' in head else head
        declared_crs, crs_note = detect_crs(preamble, source_format)

        return SourceProfile(
            filename=path.name,
            role=self._source.role,
            format=source_format,
            sha256=sha256_of(path),
            size_bytes=stat.st_size,
            modified_utc=datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            declared_crs=declared_crs,
            crs_note=crs_note,
        )

    def read(self) -> Iterator[Feature]:
        """Yield every feature in document order.

        The whole file is read as text once - unavoidable for a single JSON
        document - but only one decoded feature is held at a time.
        """
        path = self._source.path
        text = path.read_text(encoding="utf-8")
        try:
            yield from self._iterate(text)
        finally:
            del text

    def _iterate(self, text: str) -> Iterator[Feature]:
        marker = text.index('"features"')
        cursor = text.index("[", marker) + 1
        length = len(text)
        index = 0

        while True:
            while cursor < length and text[cursor] in " \t\r\n,":
                cursor += 1
            if cursor >= length or text[cursor] == "]":
                return

            raw, cursor = self._decoder.raw_decode(text, cursor)
            yield self._to_feature(index, raw)
            index += 1

    @staticmethod
    def _to_feature(index: int, raw: dict[str, Any]) -> Feature:
        if "attributes" in raw:
            geometry_raw = raw.get("geometry") or {}
            rings = geometry_raw.get("rings")
            geometry = esri_rings_to_geojson(rings) if rings else None
            return Feature(index=index, properties=raw.get("attributes") or {}, geometry=geometry)

        return Feature(
            index=index,
            properties=raw.get("properties") or {},
            geometry=raw.get("geometry"),
        )
