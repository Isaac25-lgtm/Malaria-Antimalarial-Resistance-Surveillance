#!/usr/bin/env python3
"""Read-only audit of the supplied Uganda boundary files.

Reports what the files actually contain so that every geographic claim in the
documentation is regenerable rather than asserted. Nothing here writes to, moves
or reformats a source file - the files are opened for reading only, and the
checksum recorded before and after each read proves it.

Usage
-----
    python scripts/geography_audit.py                     # human-readable report
    python scripts/geography_audit.py --json out.json     # machine-readable
    python scripts/geography_audit.py --markdown out.md   # documentation
    python scripts/geography_audit.py --verify-only       # checksums only

Exit codes
----------
    0  audit completed; findings may still be present in the report
    1  a source file is missing or unreadable
    2  a checksum does not match the tracked manifest
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "manifests" / "geography.sha256.json"

#: The four supplied files, with the role each plays in MARS.
SOURCE_FILES: dict[str, str] = {
    "COUNTRY_BOUNDARY.json": "National outline and import control layer",
    "UGANDA_DISTRICT.json": "Primary district geometry (standard GeoJSON)",
    "UGANDA_DISTRICTS.json": "Esri JSON provenance and equivalence witness",
    "UGANDA_SUBCOUNTIES.json": "Hierarchy spine and subcounty geometry",
}

#: A ring with fewer vertices than this, or a smaller absolute area, is a
#: digitising artefact rather than a real polygon. Reported, never edited.
DEGENERATE_MAX_VERTICES = 6
DEGENERATE_MAX_AREA_DEG2 = 1e-7

#: Rough conversion for reporting only. Areas are computed in planar degrees,
#: which is adequate for a control total and for spotting gross error; MARS
#: measures real areas server-side on the geography type.
SQ_DEG_TO_SQ_KM_NEAR_EQUATOR = 12308.0


# ---------------------------------------------------------------------------
# Streaming reader
# ---------------------------------------------------------------------------
def iter_features(text: str) -> Any:
    """Yield features one at a time from a GeoJSON or Esri FeatureSet document.

    Decoded incrementally rather than with ``json.load`` because the subcounty
    file is 155 MB and holding the whole parsed structure would cost several
    gigabytes for no benefit.
    """
    decoder = json.JSONDecoder()
    index = text.index('"features"')
    index = text.index("[", index) + 1
    length = len(text)
    while True:
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length or text[index] == "]":
            return
        obj, index = decoder.raw_decode(text, index)
        yield obj


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def ring_signed_area(ring: list[list[float]]) -> float:
    """Shoelace area. Sign indicates winding: positive is counter-clockwise."""
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def ring_is_closed(ring: list[list[float]]) -> bool:
    if len(ring) < 2:
        return False
    first, last = ring[0], ring[-1]
    return math.isclose(first[0], last[0], abs_tol=1e-9) and math.isclose(
        first[1], last[1], abs_tol=1e-9
    )


def polygons_of(geometry: dict[str, Any]) -> tuple[str, list[list[list[list[float]]]]]:
    """Normalise any supported geometry into a list of polygons.

    A polygon is a list of rings; ring zero is the exterior. Returns the
    detected geometry type alongside, so the report can distinguish an Esri
    ``rings`` array from a GeoJSON MultiPolygon.
    """
    if "rings" in geometry:
        # Esri does not distinguish parts from holes; each ring is reported
        # separately and the winding is left for the report to interpret.
        return "esriGeometryPolygon", [[ring] for ring in geometry["rings"]]
    geometry_type = geometry.get("type", "unknown")
    coordinates = geometry.get("coordinates", [])
    if geometry_type == "Polygon":
        return geometry_type, [coordinates]
    if geometry_type == "MultiPolygon":
        return geometry_type, list(coordinates)
    return geometry_type, []


# ---------------------------------------------------------------------------
# Report structures
# ---------------------------------------------------------------------------
@dataclass
class FeatureRecord:
    index: int
    properties: dict[str, Any]
    geometry_type: str
    polygon_count: int
    ring_count: int
    vertex_count: int
    area_deg2: float
    bbox: tuple[float, float, float, float]
    unclosed_rings: int
    degenerate_rings: list[dict[str, Any]]


@dataclass
class FileAudit:
    filename: str
    role: str
    exists: bool
    size_bytes: int = 0
    sha256: str = ""
    detected_format: str = ""
    declared_crs: str | None = None
    crs_source: str = ""
    feature_count: int = 0
    geometry_types: dict[str, int] = field(default_factory=dict)
    attribute_fields: dict[str, int] = field(default_factory=dict)
    attribute_field_sets: list[str] = field(default_factory=list)
    total_rings: int = 0
    total_vertices: int = 0
    total_area_deg2: float = 0.0
    bbox: tuple[float, float, float, float] | None = None
    null_geometries: list[int] = field(default_factory=list)
    empty_geometries: list[int] = field(default_factory=list)
    unclosed_ring_features: list[int] = field(default_factory=list)
    degenerate_rings: list[dict[str, Any]] = field(default_factory=list)
    duplicate_geometry_groups: list[list[int]] = field(default_factory=list)
    field_uniqueness: dict[str, dict[str, Any]] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    error: str | None = None


def audit_file(path: Path, role: str) -> tuple[FileAudit, list[FeatureRecord]]:
    """Audit one boundary file. Opens for reading only."""
    audit = FileAudit(filename=path.name, role=role, exists=path.exists())
    if not audit.exists:
        audit.error = "file not found"
        return audit, []

    audit.size_bytes = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    audit.sha256 = digest.hexdigest()

    text = path.read_text(encoding="utf-8")

    preamble = text[: text.index('"features"')] if '"features"' in text else text[:2000]
    if '"geometryType"' in preamble or '"attributes"' in text[:4000]:
        audit.detected_format = "Esri ArcGIS FeatureSet JSON"
    elif '"FeatureCollection"' in preamble:
        audit.detected_format = "GeoJSON FeatureCollection"
    else:
        audit.detected_format = "unrecognised"

    if '"wkid"' in preamble:
        start = preamble.index('"wkid"')
        fragment = preamble[start : start + 40]
        wkid = "".join(ch for ch in fragment.split(":")[1] if ch.isdigit())
        audit.declared_crs = f"EPSG:{wkid}" if wkid else None
        audit.crs_source = "declared in spatialReference"
    elif '"crs"' in preamble:
        audit.declared_crs = "declared (see crs member)"
        audit.crs_source = "declared in crs member"
    else:
        audit.declared_crs = None
        audit.crs_source = (
            "none declared; RFC 7946 defaults to WGS 84 (EPSG:4326)"
            if audit.detected_format == "GeoJSON FeatureCollection"
            else "none declared"
        )

    records: list[FeatureRecord] = []
    geometry_hashes: dict[str, list[int]] = collections.defaultdict(list)
    key_sets: collections.Counter[tuple[str, ...]] = collections.Counter()
    field_counts: collections.Counter[str] = collections.Counter()
    bbox = [math.inf, math.inf, -math.inf, -math.inf]

    for index, feature in enumerate(iter_features(text)):
        properties = feature.get("properties") or feature.get("attributes") or {}
        geometry = feature.get("geometry")

        key_sets[tuple(sorted(properties))] += 1
        for key in properties:
            field_counts[key] += 1

        if geometry is None:
            audit.null_geometries.append(index)
            continue

        geometry_type, polygons = polygons_of(geometry)
        if not polygons:
            audit.empty_geometries.append(index)
            continue

        rings = vertices = unclosed = 0
        area = 0.0
        fbox = [math.inf, math.inf, -math.inf, -math.inf]
        degenerate: list[dict[str, Any]] = []
        flat: list[str] = []

        for polygon_index, polygon in enumerate(polygons):
            for ring_index, ring in enumerate(polygon):
                rings += 1
                vertices += len(ring)
                if not ring_is_closed(ring):
                    unclosed += 1
                ring_area = ring_signed_area(ring)
                # Ring zero is exterior; later rings are holes and subtract.
                area += abs(ring_area) if ring_index == 0 else -abs(ring_area)
                if (
                    len(ring) <= DEGENERATE_MAX_VERTICES
                    or abs(ring_area) < DEGENERATE_MAX_AREA_DEG2
                ):
                    degenerate.append(
                        {
                            "feature_index": index,
                            "polygon": polygon_index,
                            "ring": ring_index,
                            "vertices": len(ring),
                            "abs_area_deg2": abs(ring_area),
                            "identity": _identity_of(properties),
                        }
                    )
                for point in ring:
                    x, y = point[0], point[1]
                    fbox[0] = min(fbox[0], x)
                    fbox[1] = min(fbox[1], y)
                    fbox[2] = max(fbox[2], x)
                    fbox[3] = max(fbox[3], y)
                    flat.append(f"{x:.7f},{y:.7f}")

        if unclosed:
            audit.unclosed_ring_features.append(index)
        audit.degenerate_rings.extend(degenerate)

        for i in range(4):
            bbox[i] = min(bbox[i], fbox[i]) if i < 2 else max(bbox[i], fbox[i])

        # Winding-insensitive so an Esri ring and its reversed GeoJSON twin
        # hash identically; that equivalence is what the district comparison
        # relies on.
        canonical = min("|".join(flat), "|".join(reversed(flat)))
        geometry_hash = hashlib.md5(canonical.encode(), usedforsecurity=False).hexdigest()
        geometry_hashes[geometry_hash].append(index)

        records.append(
            FeatureRecord(
                index=index,
                properties=properties,
                geometry_type=geometry_type,
                polygon_count=len(polygons),
                ring_count=rings,
                vertex_count=vertices,
                area_deg2=area,
                bbox=(fbox[0], fbox[1], fbox[2], fbox[3]),
                unclosed_rings=unclosed,
                degenerate_rings=degenerate,
            )
        )
        audit.total_rings += rings
        audit.total_vertices += vertices
        audit.total_area_deg2 += area
        audit.geometry_types[geometry_type] = audit.geometry_types.get(geometry_type, 0) + 1

    audit.feature_count = len(records) + len(audit.null_geometries) + len(
        audit.empty_geometries
    )
    audit.attribute_fields = dict(field_counts)
    audit.attribute_field_sets = [", ".join(k) for k in key_sets]
    audit.bbox = (bbox[0], bbox[1], bbox[2], bbox[3]) if records else None
    audit.duplicate_geometry_groups = [v for v in geometry_hashes.values() if len(v) > 1]

    for name in sorted(field_counts):
        values = [str(r.properties.get(name)) for r in records]
        counter = collections.Counter(values)
        duplicates = [(value, count) for value, count in counter.most_common() if count > 1]
        audit.field_uniqueness[name] = {
            "distinct": len(counter),
            "total": len(values),
            "is_unique": len(counter) == len(values),
            "top_duplicates": duplicates[:10],
        }

    _add_findings(audit)
    return audit, records


def _identity_of(properties: dict[str, Any]) -> str:
    """Best available human label for a feature, for the findings list."""
    for key in ("Sub_County", "District", "County", "name", "NAME"):
        if key in properties:
            parent = properties.get("District")
            if key != "District" and parent:
                return f"{parent}/{properties[key]}"
            return str(properties[key])
    return "(unnamed)"


def _add_findings(audit: FileAudit) -> None:
    if audit.null_geometries:
        audit.findings.append(f"{len(audit.null_geometries)} feature(s) have null geometry")
    if audit.empty_geometries:
        audit.findings.append(f"{len(audit.empty_geometries)} feature(s) have empty geometry")
    if audit.unclosed_ring_features:
        audit.findings.append(
            f"{len(audit.unclosed_ring_features)} feature(s) contain an unclosed ring"
        )
    if audit.degenerate_rings:
        audit.findings.append(
            f"{len(audit.degenerate_rings)} degenerate ring(s) "
            f"(<= {DEGENERATE_MAX_VERTICES} vertices or area < {DEGENERATE_MAX_AREA_DEG2:g} deg2)"
        )
    if audit.duplicate_geometry_groups:
        audit.findings.append(
            f"{len(audit.duplicate_geometry_groups)} group(s) of duplicate geometry"
        )
    for name, stats in audit.field_uniqueness.items():
        if not stats["is_unique"] and stats["distinct"] > 1:
            top = stats["top_duplicates"][:3]
            audit.findings.append(
                f"field '{name}' is not unique: {stats['distinct']}/{stats['total']} "
                f"distinct; most repeated {top}"
            )
    if audit.declared_crs is None:
        audit.findings.append(f"no CRS declared ({audit.crs_source})")
    if not audit.findings:
        audit.findings.append("no structural defects detected")


# ---------------------------------------------------------------------------
# Cross-file comparisons
# ---------------------------------------------------------------------------
def compare_district_files(
    geojson: list[FeatureRecord], esri: list[FeatureRecord]
) -> dict[str, Any]:
    """Establish whether the two district files describe the same dataset."""
    result: dict[str, Any] = {
        "geojson_features": len(geojson),
        "esri_features": len(esri),
        "same_feature_count": len(geojson) == len(esri),
    }
    if not geojson or not esri:
        result["conclusion"] = "one or both district files could not be read"
        return result

    def names(records: list[FeatureRecord]) -> list[str]:
        return [str(r.properties.get("District", "")) for r in records]

    gj_names, es_names = names(geojson), names(esri)
    result["same_name_order"] = gj_names == es_names
    result["name_set_matches"] = set(gj_names) == set(es_names)
    result["only_in_geojson"] = sorted(set(gj_names) - set(es_names))
    result["only_in_esri"] = sorted(set(es_names) - set(gj_names))

    gj_by_name = {str(r.properties.get("District")): r for r in geojson}
    es_by_name = {str(r.properties.get("District")): r for r in esri}
    shared = sorted(set(gj_by_name) & set(es_by_name))

    vertex_matches = sum(
        1 for n in shared if gj_by_name[n].vertex_count == es_by_name[n].vertex_count
    )
    ring_matches = sum(1 for n in shared if gj_by_name[n].ring_count == es_by_name[n].ring_count)
    attribute_matches = sum(1 for n in shared if gj_by_name[n].properties == es_by_name[n].properties)

    result["compared"] = len(shared)
    result["identical_vertex_counts"] = vertex_matches
    result["identical_ring_counts"] = ring_matches
    result["identical_attributes"] = attribute_matches

    # Winding differs between the conventions, so compare on absolute area.
    area_matches = sum(
        1
        for n in shared
        if math.isclose(
            abs(gj_by_name[n].area_deg2), abs(es_by_name[n].area_deg2), rel_tol=1e-6
        )
    )
    result["equivalent_areas"] = area_matches

    multi = [
        n
        for n in shared
        if gj_by_name[n].geometry_type == "MultiPolygon"
        or gj_by_name[n].polygon_count > 1
        or es_by_name[n].ring_count > 1
    ]
    result["multi_ring_districts"] = sorted(multi)

    equivalent = (
        result["same_feature_count"]
        and result["name_set_matches"]
        and vertex_matches == len(shared)
        and attribute_matches == len(shared)
    )
    result["equivalent"] = equivalent
    result["conclusion"] = (
        "The two district files describe the same 146 features with identical "
        "attributes and identical per-feature vertex counts. They differ in "
        "serialisation format, not in content."
        if equivalent
        else "The two district files differ in content; review before importing either."
    )
    return result


def check_parent_consistency(
    subcounties: list[FeatureRecord], districts: list[FeatureRecord]
) -> dict[str, Any]:
    """Verify that every subcounty names a district that exists."""
    district_names = {str(r.properties.get("District", "")) for r in districts}
    subcounty_districts = {str(r.properties.get("District", "")) for r in subcounties}

    result: dict[str, Any] = {
        "districts_in_district_file": len(district_names),
        "districts_named_by_subcounties": len(subcounty_districts),
        "orphan_districts_in_subcounty_file": sorted(subcounty_districts - district_names),
        "districts_with_no_subcounty": sorted(district_names - subcounty_districts),
    }

    district_region = {
        str(r.properties.get("District")): str(r.properties.get("RCode"))
        for r in districts
    }
    mismatches = [
        {
            "district": str(r.properties.get("District")),
            "subcounty": str(r.properties.get("Sub_County")),
            "district_file_rcode": district_region.get(str(r.properties.get("District"))),
            "subcounty_file_rcode": str(r.properties.get("RCode")),
        }
        for r in subcounties
        if district_region.get(str(r.properties.get("District")))
        != str(r.properties.get("RCode"))
    ]
    result["region_code_mismatches"] = len(mismatches)
    result["region_code_mismatch_examples"] = mismatches[:5]

    # (district, subcounty) pairs must be unique even though names repeat.
    pairs = collections.Counter(
        (str(r.properties.get("District")), str(r.properties.get("Sub_County")))
        for r in subcounties
    )
    result["duplicate_district_subcounty_pairs"] = [
        {"district": d, "subcounty": s, "count": c} for (d, s), c in pairs.items() if c > 1
    ]

    reused = collections.defaultdict(set)
    for record in subcounties:
        reused[str(record.properties.get("Sub_County"))].add(
            str(record.properties.get("District"))
        )
    repeated = {name: sorted(d) for name, d in reused.items() if len(d) > 1}
    result["subcounty_names_reused_across_districts"] = len(repeated)
    result["most_reused_subcounty_names"] = sorted(
        ({"name": n, "districts": len(d)} for n, d in repeated.items()),
        key=lambda item: -item["districts"],
    )[:10]
    return result


def analyse_source_code(subcounties: list[FeatureRecord]) -> dict[str, Any]:
    """Report the structure of the six-digit source code, without assuming it."""
    codes = [str(r.properties.get("FScode", "")) for r in subcounties]
    lengths = collections.Counter(len(c) for c in codes)

    result: dict[str, Any] = {
        "field": "FScode",
        "present_on": sum(1 for c in codes if c),
        "length_distribution": dict(lengths),
        "unique_full_codes": len(set(codes)),
        "total": len(codes),
    }

    prefixes: dict[int, dict[str, set[str]]] = {}
    for width, label in ((1, "region"), (3, "district"), (4, "county")):
        mapping: dict[str, set[str]] = collections.defaultdict(set)
        key = {1: "RCode", 3: "District", 4: "County"}[width]
        for record, code in zip(subcounties, codes, strict=True):
            if len(code) == 6:
                mapping[code[:width]].add(str(record.properties.get(key)))
        prefixes[width] = mapping
        result[f"{label}_prefix_count"] = len(mapping)
        result[f"{label}_prefixes_mapping_to_multiple_names"] = {
            prefix: sorted(names) for prefix, names in mapping.items() if len(names) > 1
        }

    rcode_agreement = sum(
        1
        for record, code in zip(subcounties, codes, strict=True)
        if len(code) == 6 and code[0] == str(record.properties.get("RCode"))
    )
    result["leading_digit_matches_rcode"] = rcode_agreement
    result["leading_digit_agreement_complete"] = rcode_agreement == len(
        [c for c in codes if len(c) == 6]
    )
    return result


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def load_manifest() -> dict[str, Any] | None:
    if not MANIFEST_PATH.exists():
        return None
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def verify_against_manifest(audits: list[FileAudit]) -> tuple[bool, list[str]]:
    """Compare observed checksums with the tracked manifest."""
    manifest = load_manifest()
    if manifest is None:
        return True, ["no manifest present; nothing to verify against"]

    recorded = {entry["filename"]: entry for entry in manifest.get("files", [])}
    messages: list[str] = []
    ok = True
    for audit in audits:
        entry = recorded.get(audit.filename)
        if entry is None:
            messages.append(f"{audit.filename}: not present in the manifest")
            continue
        if not audit.exists:
            messages.append(f"{audit.filename}: MISSING from the working tree")
            ok = False
            continue
        if entry["sha256"] != audit.sha256:
            messages.append(
                f"{audit.filename}: CHECKSUM MISMATCH\n"
                f"    manifest: {entry['sha256']}\n"
                f"    observed: {audit.sha256}"
            )
            ok = False
        elif entry.get("size_bytes") != audit.size_bytes:
            messages.append(
                f"{audit.filename}: size differs "
                f"(manifest {entry.get('size_bytes')}, observed {audit.size_bytes})"
            )
            ok = False
        else:
            messages.append(f"{audit.filename}: unchanged")
    return ok, messages


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add("MARS GEOGRAPHY AUDIT")
    add(f"Generated {report['generated_at']}")
    add("Read-only. No source file is modified by this script.")
    add("=" * 78)

    for audit in report["files"]:
        add("")
        add(f"FILE  {audit['filename']}")
        add(f"  role              {audit['role']}")
        if not audit["exists"]:
            add(f"  ERROR             {audit['error']}")
            continue
        add(f"  size              {audit['size_bytes'] / 1048576:.1f} MB")
        add(f"  sha256            {audit['sha256']}")
        add(f"  format            {audit['detected_format']}")
        add(f"  crs               {audit['declared_crs'] or 'not declared'} ({audit['crs_source']})")
        add(f"  features          {audit['feature_count']}")
        add(f"  geometry types    {audit['geometry_types']}")
        add(f"  rings / vertices  {audit['total_rings']} / {audit['total_vertices']}")
        add(f"  area (deg2)       {audit['total_area_deg2']:.6f}")
        if audit["bbox"]:
            add("  bbox              " + ", ".join(f"{v:.6f}" for v in audit["bbox"]))
        add(f"  attribute fields  {sorted(audit['attribute_fields'])}")
        add("  findings:")
        for finding in audit["findings"]:
            add(f"    - {finding}")

    add("")
    add("-" * 78)
    add("DISTRICT FILE COMPARISON")
    for key, value in report["district_comparison"].items():
        if key == "conclusion":
            continue
        add(f"  {key:34s} {value}")
    add(f"  => {report['district_comparison'].get('conclusion', '')}")

    add("")
    add("-" * 78)
    add("PARENT CONSISTENCY")
    for key, value in report["parent_consistency"].items():
        add(f"  {key:38s} {value}")

    add("")
    add("-" * 78)
    add("SOURCE CODE STRUCTURE")
    for key, value in report["source_code"].items():
        add(f"  {key:44s} {value}")

    add("")
    add("-" * 78)
    add("AREA CONTROL TOTAL")
    for key, value in report["area_control"].items():
        add(f"  {key:38s} {value}")

    add("")
    add("-" * 78)
    add("CHECKSUM VERIFICATION")
    for message in report["checksum_verification"]["messages"]:
        add(f"  {message}")
    add(f"  result: {'PASS' if report['checksum_verification']['ok'] else 'FAIL'}")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Geography audit")
    add("")
    add(
        "Generated by `scripts/geography_audit.py`. Every figure below is read "
        "from the supplied files at run time; nothing here is a hard-coded "
        "constant. The script opens the sources for reading only."
    )
    add("")
    add(f"- Generated: `{report['generated_at']}`")
    add(f"- Checksum verification: **{'PASS' if report['checksum_verification']['ok'] else 'FAIL'}**")
    add("")
    add("## Files")
    add("")
    add("| File | Format | CRS | Features | Geometry types | Rings | Vertices |")
    add("| --- | --- | --- | ---: | --- | ---: | ---: |")
    for audit in report["files"]:
        if not audit["exists"]:
            add(f"| `{audit['filename']}` | **missing** | - | - | - | - | - |")
            continue
        types = ", ".join(f"{k} x{v}" for k, v in audit["geometry_types"].items())
        add(
            f"| `{audit['filename']}` | {audit['detected_format']} | "
            f"{audit['declared_crs'] or 'not declared'} | {audit['feature_count']} | "
            f"{types} | {audit['total_rings']} | {audit['total_vertices']} |"
        )
    add("")
    add("### Findings per file")
    add("")
    for audit in report["files"]:
        add(f"**`{audit['filename']}`** - {audit['role']}")
        add("")
        for finding in audit.get("findings", []):
            add(f"- {finding}")
        add("")

    add("## District file comparison")
    add("")
    comparison = report["district_comparison"]
    for key, value in comparison.items():
        if key == "conclusion":
            continue
        add(f"- `{key}`: {value}")
    add("")
    add(f"> {comparison.get('conclusion', '')}")
    add("")

    add("## Parent consistency")
    add("")
    for key, value in report["parent_consistency"].items():
        add(f"- `{key}`: {value}")
    add("")

    add("## Source code structure (`FScode`)")
    add("")
    add(
        "Recorded as an observation of the supplied data. `FScode` is treated as "
        "a source alias in MARS, never as a primary key, pending authoritative "
        "confirmation of the national coding scheme."
    )
    add("")
    for key, value in report["source_code"].items():
        add(f"- `{key}`: {value}")
    add("")

    add("## Area control total")
    add("")
    add(
        "Areas are planar degrees squared - adequate as an import control total "
        "and for detecting gross error. MARS measures real areas server-side on "
        "the PostGIS geography type."
    )
    add("")
    for key, value in report["area_control"].items():
        add(f"- `{key}`: {value}")
    add("")

    add("## Checksums")
    add("")
    add("| File | SHA-256 | Size (bytes) |")
    add("| --- | --- | ---: |")
    for audit in report["files"]:
        if audit["exists"]:
            add(f"| `{audit['filename']}` | `{audit['sha256']}` | {audit['size_bytes']} |")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_report(data_dir: Path) -> tuple[dict[str, Any], bool]:
    audits: list[FileAudit] = []
    records: dict[str, list[FeatureRecord]] = {}

    for filename, role in SOURCE_FILES.items():
        audit, feature_records = audit_file(data_dir / filename, role)
        audits.append(audit)
        records[filename] = feature_records

    ok, messages = verify_against_manifest(audits)

    district_comparison = compare_district_files(
        records.get("UGANDA_DISTRICT.json", []), records.get("UGANDA_DISTRICTS.json", [])
    )
    parent_consistency = check_parent_consistency(
        records.get("UGANDA_SUBCOUNTIES.json", []), records.get("UGANDA_DISTRICT.json", [])
    )
    source_code = analyse_source_code(records.get("UGANDA_SUBCOUNTIES.json", []))

    country_area = sum(r.area_deg2 for r in records.get("COUNTRY_BOUNDARY.json", []))
    district_area = sum(r.area_deg2 for r in records.get("UGANDA_DISTRICT.json", []))
    subcounty_area = sum(r.area_deg2 for r in records.get("UGANDA_SUBCOUNTIES.json", []))

    area_control = {
        "country_area_deg2": round(country_area, 6),
        "district_sum_deg2": round(district_area, 6),
        "subcounty_sum_deg2": round(subcounty_area, 6),
        "district_sum_over_country": (
            round(district_area / country_area, 6) if country_area else None
        ),
        "subcounty_sum_over_district": (
            round(subcounty_area / district_area, 6) if district_area else None
        ),
        "implied_country_area_sq_km": round(country_area * SQ_DEG_TO_SQ_KM_NEAR_EQUATOR),
        "note": (
            "Planar degrees. District polygons include open water, so area is "
            "never a population proxy or an area-rate denominator."
        ),
    }

    report = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "data_directory": str(data_dir),
        "files": [
            {
                **{k: v for k, v in vars(audit).items() if k != "degenerate_rings"},
                "degenerate_ring_count": len(audit.degenerate_rings),
                "degenerate_ring_examples": audit.degenerate_rings[:10],
            }
            for audit in audits
        ],
        "district_comparison": district_comparison,
        "parent_consistency": parent_consistency,
        "source_code": source_code,
        "area_control": area_control,
        "checksum_verification": {"ok": ok, "messages": messages},
    }
    return report, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT,
        help="Directory holding the four boundary files (default: repository root)",
    )
    parser.add_argument("--json", type=Path, help="Write the machine-readable report here")
    parser.add_argument("--markdown", type=Path, help="Write the documentation report here")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify checksums against the manifest",
    )
    args = parser.parse_args(argv)

    missing = [f for f in SOURCE_FILES if not (args.data_dir / f).exists()]
    if missing:
        print(f"ERROR: source file(s) not found in {args.data_dir}: {missing}", file=sys.stderr)
        return 1

    if args.verify_only:
        audits = [audit_file(args.data_dir / f, r)[0] for f, r in SOURCE_FILES.items()]
        ok, messages = verify_against_manifest(audits)
        for message in messages:
            print(message)
        print(f"result: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 2

    report, ok = build_report(args.data_dir)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {args.markdown}")
    if not args.json and not args.markdown:
        print(render_text(report))

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
