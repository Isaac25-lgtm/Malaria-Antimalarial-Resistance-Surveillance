"""Geography import: readers, geometry policy and result reporting.

These run without a database. The hierarchy build and the PostGIS load are
exercised against a live server in the integration suite.

Expected values are taken from the audited sources and hand-checked, so a change
in behaviour fails the test rather than silently updating the expectation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mars.domain.enums import GeographyLevel, GeometryValidityState
from mars.ingestion.geography import geometry as geom
from mars.ingestion.geography.reader import (
    SourceFormat,
    SourceRole,
    detect_crs,
    detect_format,
    esri_rings_to_geojson,
)
from mars.ingestion.geography.result import ImportOutcome, ImportResult

# A closed 1-degree square. Large enough to clear the degenerate thresholds.
SQUARE = [[32.0, 2.0], [33.0, 2.0], [33.0, 3.0], [32.0, 3.0], [32.0, 2.0]]
# A hole inside it.
HOLE = [[32.4, 2.4], [32.6, 2.4], [32.6, 2.6], [32.4, 2.6], [32.4, 2.4]]
# A four-point sliver of effectively zero area - the shape of the artefacts the
# audit found in the supplied sources.
SLIVER = [[32.1, 2.1], [32.1000001, 2.1], [32.1000001, 2.1000001], [32.1, 2.1]]


def polygon(*rings: list[list[float]]) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [list(ring) for ring in rings]}


def multipolygon(*polygons: list[list[list[float]]]) -> dict[str, Any]:
    return {"type": "MultiPolygon", "coordinates": [list(p) for p in polygons]}


class TestFormatDetection:
    def test_detects_geojson(self) -> None:
        head = '{ "type" : "FeatureCollection", "features" : ['
        assert detect_format(head, head) is SourceFormat.GEOJSON

    def test_detects_esri_featureset(self) -> None:
        head = '{"displayFieldName":"","geometryType":"esriGeometryPolygon","features":['
        assert detect_format(head, head) is SourceFormat.ESRI_FEATURESET

    def test_reports_unknown_rather_than_guessing(self) -> None:
        assert detect_format("{}", "{}") is SourceFormat.UNKNOWN


class TestCrsDetection:
    def test_reads_a_declared_wkid(self) -> None:
        """Only the Esri twin declares a CRS, which is why it is retained."""
        preamble = '{"spatialReference":{"wkid":4326,"latestWkid":4326},'
        crs, note = detect_crs(preamble, SourceFormat.ESRI_FEATURESET)
        assert crs == "EPSG:4326"
        assert "spatialReference" in note

    def test_records_the_rfc7946_default_explicitly(self) -> None:
        """An undeclared GeoJSON CRS means WGS 84 - stated, not assumed silently."""
        crs, note = detect_crs('{ "type" : "FeatureCollection",', SourceFormat.GEOJSON)
        assert crs is None
        assert "RFC 7946" in note
        assert "4326" in note


class TestEsriRingConversion:
    def test_clockwise_rings_become_separate_parts(self) -> None:
        """Under the Esri convention a clockwise ring is an exterior ring."""
        clockwise_a = list(reversed(SQUARE))
        clockwise_b = list(reversed([[p[0] + 5, p[1]] for p in SQUARE]))
        result = esri_rings_to_geojson([clockwise_a, clockwise_b])
        assert result["type"] == "MultiPolygon"
        assert len(result["coordinates"]) == 2

    def test_a_counter_clockwise_ring_becomes_a_hole(self) -> None:
        clockwise = list(reversed(SQUARE))
        result = esri_rings_to_geojson([clockwise, HOLE])
        assert result["type"] == "Polygon"
        assert len(result["coordinates"]) == 2


class TestGeometryAssessment:
    def test_polygon_is_promoted_to_multipolygon(self) -> None:
        """One database type for every level keeps the importer and API simple."""
        assessment = geom.assess(polygon(SQUARE), label="test")
        assert assessment.is_usable
        assert assessment.prepared is not None
        assert assessment.prepared["type"] == "MultiPolygon"
        assert assessment.repair_method == geom.RepairPolicy.PROMOTED_TO_MULTIPOLYGON
        assert assessment.validity_state is GeometryValidityState.VALID

    def test_multipolygon_is_left_alone(self) -> None:
        assessment = geom.assess(multipolygon([SQUARE]), label="test")
        assert assessment.repair_method == geom.RepairPolicy.NONE
        assert assessment.part_count == 1

    def test_a_hole_is_preserved(self) -> None:
        assessment = geom.assess(polygon(SQUARE, HOLE), label="test")
        assert assessment.prepared is not None
        assert len(assessment.prepared["coordinates"][0]) == 2

    def test_degenerate_ring_is_dropped_and_recorded(self) -> None:
        """The audit found 22 of these. They are removed from the derived copy
        and reported - the source bytes are never touched."""
        assessment = geom.assess(polygon(SQUARE, SLIVER), label="test")
        assert assessment.is_usable
        assert assessment.validity_state is GeometryValidityState.INVALID_REPAIRED
        assert "degenerate_ring" in assessment.issue_codes()
        assert assessment.prepared is not None
        assert len(assessment.prepared["coordinates"][0]) == 1

    def test_geometry_of_only_slivers_is_quarantined(self) -> None:
        """Nothing usable is left, so it is quarantined rather than stored empty."""
        assessment = geom.assess(polygon(SLIVER), label="test")
        assert not assessment.is_usable
        assert assessment.validity_state is GeometryValidityState.INVALID_UNREPAIRED
        assert "no_usable_rings" in assessment.issue_codes()

    def test_null_geometry_is_quarantined(self) -> None:
        assessment = geom.assess(None, label="test")
        assert not assessment.is_usable
        assert "null_geometry" in assessment.issue_codes()

    def test_unsupported_geometry_type_is_refused(self) -> None:
        assessment = geom.assess({"type": "LineString", "coordinates": []}, label="test")
        assert not assessment.is_usable
        assert "unsupported_geometry_type" in assessment.issue_codes()

    def test_an_unclosed_ring_is_closed_and_recorded(self) -> None:
        assessment = geom.assess(polygon(SQUARE[:-1]), label="test")
        assert assessment.is_usable
        assert "unclosed_ring" in assessment.issue_codes()

    def test_geometry_outside_uganda_is_flagged(self) -> None:
        """A boundary in the wrong hemisphere is a coordinate-order error."""
        far_away = [[p[1], p[0]] for p in SQUARE]
        assessment = geom.assess(polygon(far_away), label="test")
        assert "bbox_outside_expected_extent" in assessment.issue_codes()

    def test_vertex_and_ring_counts_are_reported(self) -> None:
        assessment = geom.assess(polygon(SQUARE), label="test")
        assert assessment.ring_count == 1
        assert assessment.vertex_count == len(SQUARE)
        assert assessment.part_count == 1

    def test_planar_area_is_computed_for_the_control_total(self) -> None:
        """One square degree, used only as an import control total."""
        assessment = geom.assess(polygon(SQUARE), label="test")
        assert assessment.planar_area_deg2 == pytest.approx(1.0, abs=1e-9)


class TestSimplificationPolicy:
    def test_every_level_has_a_tolerance(self) -> None:
        for level in GeographyLevel:
            assert geom.tolerance_for(level) > 0

    def test_finer_levels_use_finer_tolerances(self) -> None:
        """A subcounty is viewed closer than a country and must stay recognisable."""
        assert geom.tolerance_for(GeographyLevel.SUBCOUNTY) < geom.tolerance_for(
            GeographyLevel.DISTRICT
        )
        assert geom.tolerance_for(GeographyLevel.DISTRICT) < geom.tolerance_for(
            GeographyLevel.COUNTRY
        )


class TestSourceRoles:
    def test_the_two_district_sources_have_different_roles(self) -> None:
        """ADR 0004: the same dataset in two formats, not duplicates."""
        assert SourceRole.DISTRICT_GEOMETRY is not SourceRole.DISTRICT_PROVENANCE

    def test_the_esri_twin_is_excluded_from_import(self) -> None:
        """It is the CRS and field-schema witness, and is never imported."""
        from mars.ingestion.geography.cli import DEFAULT_FILENAMES

        assert SourceRole.DISTRICT_PROVENANCE not in DEFAULT_FILENAMES
        assert "UGANDA_DISTRICTS.json" not in DEFAULT_FILENAMES.values()

    def test_the_importable_sources_are_the_three_documented_ones(self) -> None:
        from mars.ingestion.geography.cli import DEFAULT_FILENAMES

        assert set(DEFAULT_FILENAMES.values()) == {
            "COUNTRY_BOUNDARY.json",
            "UGANDA_DISTRICT.json",
            "UGANDA_SUBCOUNTIES.json",
        }


class TestImportResult:
    def test_context_may_contain_a_key_named_code(self) -> None:
        """Most findings are about a geography code, so this must not collide."""
        result = ImportResult(outcome=ImportOutcome.FAILED)
        issue = result.add_issue("x", "y", blocking=True, context={"code": "304"})
        assert issue.context["code"] == "304"
        assert issue.code == "x"

    def test_blocking_and_advisory_issues_are_separated(self) -> None:
        result = ImportResult(outcome=ImportOutcome.FAILED)
        result.add_issue("advisory", "recorded, not fatal")
        result.add_issue("fatal", "stops publication", blocking=True)
        assert len(result.issues) == 2
        assert len(result.blocking_issues) == 1

    def test_a_dry_run_counts_as_success(self) -> None:
        assert ImportResult(outcome=ImportOutcome.VALIDATED_ONLY).succeeded

    def test_a_repeat_import_counts_as_success(self) -> None:
        """Identical bytes already published is a correct outcome, not a failure."""
        assert ImportResult(outcome=ImportOutcome.ALREADY_IMPORTED).succeeded

    def test_validation_failure_is_not_success(self) -> None:
        assert not ImportResult(outcome=ImportOutcome.VALIDATION_FAILED).succeeded

    def test_result_serialises_for_the_boundary_version(self) -> None:
        result = ImportResult(outcome=ImportOutcome.PUBLISHED)
        result.level("district").created = 146
        result.add_issue("note", "something advisory")
        document = result.as_dict()
        assert json.dumps(document)  # must be JSONB-serialisable
        assert document["levels"]["district"]["created"] == 146
        assert document["blocking_issue_count"] == 0


class TestImporterVersioning:
    def test_importer_version_is_recorded(self) -> None:
        """A stored hierarchy must be traceable to the code that built it."""
        from mars.ingestion.geography.importer import IMPORTER_VERSION

        assert IMPORTER_VERSION
        assert IMPORTER_VERSION.count(".") == 2
