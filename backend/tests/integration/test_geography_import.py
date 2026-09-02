"""Geography import against live PostgreSQL and PostGIS.

Two tiers here:

* **Synthetic sources**, written to a temporary directory, exercising the
  hierarchy build, idempotency, rollback and geometry handling deterministically
  and in under a second.
* **The real supplied sources**, run once when ``MARS_GEOGRAPHY_DATA_DIR`` points
  at them, checking the totals against the audited figures. Skipped otherwise,
  and the skip is reported.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.enums import BoundaryImportStatus, GeographyLevel
from mars.domain.geography import BoundaryVersion, GeographyUnit, GeographyUnitAlias
from mars.ingestion.geography.importer import GeographyImporter, ImportOptions
from mars.ingestion.geography.reader import SourceRole
from mars.ingestion.geography.result import ImportOutcome

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

#: Set to the directory holding the four supplied boundary files to run the
#: real-source test. Absent, that one test skips and says so.
REAL_SOURCES_ENV = "MARS_GEOGRAPHY_DATA_DIR"

#: Audited totals from docs/data-dictionary/geography-audit.md. If the importer
#: stops reproducing these, either the sources or the importer changed.
AUDITED_DISTRICTS = 146
AUDITED_SUBCOUNTIES = 2190
AUDITED_COUNTIES = 312
AUDITED_REGIONS = 4


@pytest.fixture(scope="module")
def geo_engine(integration_database_url: str) -> Iterator[Engine]:
    """A schema at head, torn down afterwards."""
    engine = create_engine(integration_database_url, future=True)

    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)

    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def session(geo_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=geo_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean_geography(geo_engine: Engine) -> Iterator[None]:
    """Start each test from an empty hierarchy.

    Geometry, aliases and units are removed in dependency order; the boundary
    versions go last. Nothing outside mars_core geography is touched.
    """
    yield
    with geo_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_core.geography_unit_geometry"))
        connection.execute(text("DELETE FROM mars_core.geography_unit_alias"))
        connection.execute(text("DELETE FROM mars_core.geography_unit"))
        connection.execute(text("DELETE FROM mars_core.boundary_version"))


# ---------------------------------------------------------------------------
# Synthetic sources
# ---------------------------------------------------------------------------
def rectangle(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, *, densify: int = 0
) -> list[list[list[float]]]:
    """A closed rectangular ring.

    ``densify`` inserts extra collinear points along the horizontal edges. That
    raises the vertex count without altering the enclosed area, which is how the
    simplification test gets something to simplify while keeping the control
    totals exact.
    """
    bottom = [[min_lon, min_lat]]
    top = [[max_lon, max_lat]]
    if densify:
        step = (max_lon - min_lon) / (densify + 1)
        bottom += [[min_lon + step * (i + 1), min_lat] for i in range(densify)]
        top += [[max_lon - step * (i + 1), max_lat] for i in range(densify)]
    return [
        [
            *bottom,
            [max_lon, min_lat],
            *top,
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]
    ]


#: The synthetic country. Districts tile it exactly and subcounties tile their
#: district, so the area control totals hold - the same identity the audit
#: established for the real sources. A fixture that did not tile would fail
#: validation, which is the importer working correctly.
COUNTRY_BOUNDS = (32.0, 1.0, 34.0, 2.0)


def subcounty(
    fscode: str,
    name: str,
    district: str,
    county: str,
    *,
    geometry: dict[str, Any] | None = None,
    densify: int = 0,
    extra_rings: list[list[list[float]]] | None = None,
) -> dict[str, Any]:
    """One subcounty feature.

    Geometry is assigned later by :func:`write_sources`, which computes the
    tiling once it knows how many districts and subcounties there are.
    """
    return {
        "type": "Feature",
        "properties": {
            "FScode": fscode,
            "Sub_County": name,
            "District": district,
            "County": county,
            "RCode": fscode[0],
            "FID": int(fscode),
        },
        "geometry": geometry,
        "_densify": densify,
        "_extra_rings": extra_rings or [],
    }


def write_sources(
    directory: Path,
    *,
    subcounties: list[dict[str, Any]],
    districts: list[dict[str, Any]] | None = None,
) -> dict[SourceRole, Path]:
    """Write a synthetic source set that tiles exactly.

    The country is divided into vertical strips, one per district, and each
    strip into horizontal slices, one per subcounty. Every level therefore sums
    to the country area, so the importer's control-total check passes for the
    same reason it passes on the real sources.
    """
    min_lon, min_lat, max_lon, max_lat = COUNTRY_BOUNDS

    by_district: dict[str, list[dict[str, Any]]] = {}
    district_names: dict[str, str] = {}
    for feature in subcounties:
        code = str(feature["properties"]["FScode"])[:3]
        by_district.setdefault(code, []).append(feature)
        district_names.setdefault(code, feature["properties"]["District"])

    ordered_districts = sorted(by_district)
    strip_width = (max_lon - min_lon) / len(ordered_districts)

    generated_districts: list[dict[str, Any]] = []

    for index, district_code in enumerate(ordered_districts):
        strip_min = min_lon + strip_width * index
        strip_max = strip_min + strip_width

        generated_districts.append(
            {
                "type": "Feature",
                "properties": {
                    "District": district_names[district_code],
                    "RCode": district_code[0],
                    "FID": index,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": rectangle(strip_min, min_lat, strip_max, max_lat),
                },
            }
        )

        members = by_district[district_code]
        slice_height = (max_lat - min_lat) / len(members)
        for position, feature in enumerate(members):
            if feature.get("geometry") is not None:
                continue
            cell_min_lat = min_lat + slice_height * position
            cell_max_lat = cell_min_lat + slice_height
            rings = rectangle(
                strip_min,
                cell_min_lat,
                strip_max,
                cell_max_lat,
                densify=feature.get("_densify", 0),
            )
            rings.extend(feature.get("_extra_rings", []))
            feature["geometry"] = {"type": "Polygon", "coordinates": rings}

    country = {
        "type": "Feature",
        "properties": {"FID": 0, "RCode": " "},
        "geometry": {
            "type": "Polygon",
            "coordinates": rectangle(min_lon, min_lat, max_lon, max_lat),
        },
    }

    cleaned = [
        {key: value for key, value in feature.items() if not key.startswith("_")}
        for feature in subcounties
    ]

    paths = {
        SourceRole.COUNTRY_BOUNDARY: directory / "COUNTRY_BOUNDARY.json",
        SourceRole.DISTRICT_GEOMETRY: directory / "UGANDA_DISTRICT.json",
        SourceRole.SUBCOUNTY_HIERARCHY: directory / "UGANDA_SUBCOUNTIES.json",
    }
    for role, features in (
        (SourceRole.COUNTRY_BOUNDARY, [country]),
        (SourceRole.DISTRICT_GEOMETRY, districts if districts is not None else generated_districts),
        (SourceRole.SUBCOUNTY_HIERARCHY, cleaned),
    ):
        paths[role].write_text(
            json.dumps({"type": "FeatureCollection", "features": features}),
            encoding="utf-8",
        )
    return paths


def two_districts() -> list[dict[str, Any]]:
    """Two districts in one region: Gulu with two subcounties, Pader with one.

    Rebuilt per call because write_sources assigns geometry in place.
    """
    return [
        subcounty("304101", "AWACH", "GULU", "ARUU COUNTY"),
        subcounty("304102", "BUNGATIRA", "GULU", "ARUU COUNTY"),
        subcounty("312101", "ATANGA", "PADER", "ARUU NORTH COUNTY"),
    ]


class TestHierarchyConstruction:
    def test_builds_five_levels_from_the_spine(self, session: Session, tmp_path: Path) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        assert result.outcome is ImportOutcome.PUBLISHED, result.blocking_issues

        counts = {
            level.value: session.execute(select(GeographyUnit).where(GeographyUnit.level == level))
            .scalars()
            .all()
            for level in GeographyLevel
        }
        assert len(counts["country"]) == 1
        # Both district codes begin with 3, so they share one region.
        assert len(counts["region"]) == 1
        assert len(counts["district"]) == 2
        assert len(counts["county"]) == 2
        assert len(counts["subcounty"]) == 3

    def test_parish_and_village_stay_empty(self, session: Session, tmp_path: Path) -> None:
        """No parish or village data has been supplied, so those levels stay empty."""
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        for level in (GeographyLevel.PARISH, GeographyLevel.VILLAGE):
            units = (
                session.execute(select(GeographyUnit).where(GeographyUnit.level == level))
                .scalars()
                .all()
            )
            assert units == [], f"{level.value} must remain empty"

    def test_paths_and_depths_match_the_hierarchy(self, session: Session, tmp_path: Path) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        unit = session.execute(
            select(GeographyUnit).where(GeographyUnit.preferred_code == "304101")
        ).scalar_one()
        assert unit.level is GeographyLevel.SUBCOUNTY
        assert unit.depth == 4
        assert unit.path == "UG/3/304/3041/304101"

    def test_region_name_is_unresolved_and_reported(self, session: Session, tmp_path: Path) -> None:
        """The sources carry a region code and no region name.

        Naming the regions would need outside knowledge, so the code is used and
        the gap is reported rather than filled with an invented toponym.
        """
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        region = (
            session.execute(
                select(GeographyUnit).where(GeographyUnit.level == GeographyLevel.REGION)
            )
            .scalars()
            .first()
        )
        assert region is not None
        assert region.raw_name == region.preferred_code
        assert any(issue.code == "region_name_unresolved" for issue in result.issues)

    def test_source_codes_are_recorded_as_aliases(self, session: Session, tmp_path: Path) -> None:
        """FScode is an alias, never a key."""
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        unit = session.execute(
            select(GeographyUnit).where(GeographyUnit.preferred_code == "304101")
        ).scalar_one()
        aliases = (
            session.execute(
                select(GeographyUnitAlias).where(GeographyUnitAlias.geography_unit_id == unit.id)
            )
            .scalars()
            .all()
        )

        fscode_alias = next(a for a in aliases if a.source_system == "ubos_fscode")
        assert fscode_alias.source_code == "304101"
        assert fscode_alias.match_method == "source_code_derivation"
        # Derived arithmetic on a supplied code, not a guess.
        assert fscode_alias.match_status.value == "confirmed"

    def test_a_reused_subcounty_name_under_different_parents_is_accepted(
        self, session: Session, tmp_path: Path
    ) -> None:
        """CENTRAL DIVISION occurs twelve times in the supplied data."""
        features = [
            subcounty("304101", "CENTRAL DIVISION", "GULU", "GULU COUNTY"),
            subcounty("312101", "CENTRAL DIVISION", "PADER", "PADER COUNTY"),
        ]
        sources = write_sources(tmp_path, subcounties=features)
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()
        assert result.outcome is ImportOutcome.PUBLISHED, result.blocking_issues


class TestValidationRefusesBadSources:
    def test_a_duplicate_source_code_blocks_publication(
        self, session: Session, tmp_path: Path
    ) -> None:
        features = [
            subcounty("304101", "AWACH", "GULU", "ARUU COUNTY"),
            subcounty("304101", "OTHER", "GULU", "ARUU COUNTY"),
        ]
        sources = write_sources(tmp_path, subcounties=features)
        result = GeographyImporter(session, sources).run(ImportOptions())

        assert result.outcome is ImportOutcome.VALIDATION_FAILED
        assert any(issue.code == "duplicate_source_code" for issue in result.blocking_issues)

    def test_a_region_code_disagreement_blocks_publication(
        self, session: Session, tmp_path: Path
    ) -> None:
        """FScode's leading digit must agree with the RCode attribute."""
        feature = subcounty("304101", "AWACH", "GULU", "ARUU COUNTY")
        feature["properties"]["RCode"] = "2"
        sources = write_sources(tmp_path, subcounties=[feature])
        result = GeographyImporter(session, sources).run(ImportOptions())

        assert result.outcome is ImportOutcome.VALIDATION_FAILED
        assert any(issue.code == "region_code_disagreement" for issue in result.blocking_issues)

    def test_district_geometry_that_matches_no_district_blocks_publication(
        self, session: Session, tmp_path: Path
    ) -> None:
        districts = [
            {
                "type": "Feature",
                "properties": {"District": "NOWHERE", "RCode": "3", "FID": 0},
                "geometry": {"type": "Polygon", "coordinates": rectangle(32.0, 1.0, 34.0, 2.0)},
            }
        ]
        sources = write_sources(tmp_path, subcounties=two_districts()[:1], districts=districts)
        result = GeographyImporter(session, sources).run(ImportOptions())

        assert result.outcome is ImportOutcome.VALIDATION_FAILED
        assert any(issue.code == "district_geometry_unmatched" for issue in result.blocking_issues)

    def test_a_failed_validation_leaves_the_published_version_untouched(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Blueprint section 083: a failed refresh must not replace good data."""
        good_dir = tmp_path / "good"
        good_dir.mkdir()
        good = write_sources(good_dir, subcounties=two_districts())
        first = GeographyImporter(session, good).run(ImportOptions())
        session.commit()
        assert first.outcome is ImportOutcome.PUBLISHED

        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        broken = [
            subcounty("304101", "AWACH", "GULU", "ARUU COUNTY"),
            subcounty("304101", "DUPLICATE", "GULU", "ARUU COUNTY"),
        ]
        bad = write_sources(bad_dir, subcounties=broken)
        second = GeographyImporter(session, bad).run(ImportOptions())
        session.commit()

        assert second.outcome is ImportOutcome.VALIDATION_FAILED

        published = (
            session.execute(
                select(BoundaryVersion).where(
                    BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED
                )
            )
            .scalars()
            .all()
        )
        assert len(published) == 1
        assert published[0].id == first.boundary_version_id

        # The failed attempt is retained with its report, not discarded.
        failed = (
            session.execute(
                select(BoundaryVersion).where(
                    BoundaryVersion.import_status == BoundaryImportStatus.VALIDATION_FAILED
                )
            )
            .scalars()
            .all()
        )
        assert len(failed) == 1
        assert failed[0].validation_summary is not None


class TestIdempotency:
    def test_reimporting_identical_bytes_is_a_no_op(self, session: Session, tmp_path: Path) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())

        first = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()
        assert first.outcome is ImportOutcome.PUBLISHED

        second = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        assert second.outcome is ImportOutcome.ALREADY_IMPORTED
        assert second.boundary_version_id == first.boundary_version_id

        versions = session.execute(select(BoundaryVersion)).scalars().all()
        assert len(versions) == 1, "identical bytes must not create a second version"

    def test_repeating_an_import_does_not_duplicate_units_or_aliases(
        self, session: Session, tmp_path: Path
    ) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())

        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()
        units_before = len(session.execute(select(GeographyUnit)).scalars().all())
        aliases_before = len(session.execute(select(GeographyUnitAlias)).scalars().all())

        GeographyImporter(session, sources).run(ImportOptions(force=True))
        session.commit()

        assert len(session.execute(select(GeographyUnit)).scalars().all()) == units_before
        assert len(session.execute(select(GeographyUnitAlias)).scalars().all()) == aliases_before

    def test_forced_reimport_keeps_the_unit_uuid(self, session: Session, tmp_path: Path) -> None:
        """Facilities and user scopes reference these UUIDs.

        Replacing rather than updating would break every reference, which is why
        the importer matches on (level, preferred_code).
        """
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        before = session.execute(
            select(GeographyUnit).where(GeographyUnit.preferred_code == "304101")
        ).scalar_one()
        original_id = before.id

        GeographyImporter(session, sources).run(ImportOptions(force=True))
        session.commit()

        after = session.execute(
            select(GeographyUnit).where(GeographyUnit.preferred_code == "304101")
        ).scalar_one()
        assert after.id == original_id

    def test_only_one_version_is_published_at_a_time(
        self, session: Session, tmp_path: Path
    ) -> None:
        first_dir = tmp_path / "first"
        first_dir.mkdir()
        second_dir = tmp_path / "second"
        second_dir.mkdir()

        GeographyImporter(session, write_sources(first_dir, subcounties=two_districts())).run(
            ImportOptions()
        )
        session.commit()

        extended = [*two_districts(), subcounty("312102", "LAGUTI", "PADER", "ARUU NORTH COUNTY")]
        GeographyImporter(session, write_sources(second_dir, subcounties=extended)).run(
            ImportOptions()
        )
        session.commit()

        published = (
            session.execute(
                select(BoundaryVersion).where(
                    BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED
                )
            )
            .scalars()
            .all()
        )
        assert len(published) == 1

        superseded = (
            session.execute(
                select(BoundaryVersion).where(
                    BoundaryVersion.import_status == BoundaryImportStatus.SUPERSEDED
                )
            )
            .scalars()
            .all()
        )
        assert len(superseded) == 1

    def test_a_unit_absent_from_a_new_source_is_deactivated_not_deleted(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Historical encounters and signals still reference it."""
        first_dir = tmp_path / "first"
        first_dir.mkdir()
        second_dir = tmp_path / "second"
        second_dir.mkdir()

        GeographyImporter(session, write_sources(first_dir, subcounties=two_districts())).run(
            ImportOptions()
        )
        session.commit()

        GeographyImporter(session, write_sources(second_dir, subcounties=two_districts()[:2])).run(
            ImportOptions()
        )
        session.commit()

        removed = session.execute(
            select(GeographyUnit).where(GeographyUnit.preferred_code == "312101")
        ).scalar_one()
        assert removed is not None, "the unit must still exist"
        assert removed.is_active is False
        assert removed.effective_to is not None


class TestDryRun:
    def test_a_dry_run_writes_nothing(self, session: Session, tmp_path: Path) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(ImportOptions(dry_run=True))
        session.commit()

        assert result.outcome is ImportOutcome.VALIDATED_ONLY
        assert session.execute(select(GeographyUnit)).scalars().all() == []
        assert session.execute(select(BoundaryVersion)).scalars().all() == []


class TestPostGisGeometry:
    def test_geometry_is_multipolygon_in_4326(self, session: Session, tmp_path: Path) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        row = session.execute(
            text(
                """
                SELECT ST_GeometryType(g.geom) AS gtype, ST_SRID(g.geom) AS srid,
                       ST_IsValid(g.geom) AS valid, g.area_sq_km
                  FROM mars_core.geography_unit_geometry g
                  JOIN mars_core.geography_unit u ON u.id = g.geography_unit_id
                 WHERE u.preferred_code = '304101'
                """
            )
        ).one()
        assert row.gtype == "ST_MultiPolygon"
        assert row.srid == 4326
        assert row.valid is True
        assert row.area_sq_km > 0

    def test_area_is_measured_geodesically_not_in_degrees(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Area must be square kilometres on the spheroid, not square degrees.

        The subcounty cell spans one degree of longitude by half a degree of
        latitude near the equator - roughly 6,200 square kilometres. Measured in
        degrees it would be 0.5, which is not an area at all.
        """
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        area = session.execute(
            text(
                "SELECT g.area_sq_km FROM mars_core.geography_unit_geometry g "
                "JOIN mars_core.geography_unit u ON u.id = g.geography_unit_id "
                "WHERE u.preferred_code = '304101'"
            )
        ).scalar_one()
        assert 5_000 < area < 7_000, f"expected roughly 6,200 sq km, got {area}"
        assert area > 100, "an area this small would mean degrees leaked through"

    def test_browser_geometry_is_smaller_than_the_analytical_copy(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Raw subcounty geometry must never reach a client.

        The cell is densified with collinear points, which raises the vertex
        count without changing the enclosed area, so simplification has
        something to remove and the control totals still hold.
        """
        features = [subcounty("304101", "DETAILED", "GULU", "ARUU COUNTY", densify=400)]
        sources = write_sources(tmp_path, subcounties=features)
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()
        assert result.outcome is ImportOutcome.PUBLISHED, result.blocking_issues

        row = session.execute(
            text(
                """
                SELECT ST_NPoints(g.geom) AS full_points,
                       ST_NPoints(g.geom_web) AS web_points,
                       length(ST_AsGeoJSON(g.geom)) AS full_bytes,
                       length(ST_AsGeoJSON(g.geom_web)) AS web_bytes
                  FROM mars_core.geography_unit_geometry g
                  JOIN mars_core.geography_unit u ON u.id = g.geography_unit_id
                 WHERE u.preferred_code = '304101'
                """
            )
        ).one()
        assert row.web_points < row.full_points
        assert row.web_bytes < row.full_bytes

    def test_a_degenerate_ring_is_dropped_and_recorded(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The audit found 22 of these in the supplied sources.

        The sliver sits inside the subcounty's tiled cell and encloses
        effectively no area, so dropping it leaves the control totals intact -
        which is exactly how the real artefacts behave.
        """
        sliver = [
            [32.5, 1.5],
            [32.5000001, 1.5],
            [32.5000001, 1.5000001],
            [32.5, 1.5],
        ]
        features = [
            subcounty("304101", "SLIVER", "GULU", "ARUU COUNTY", extra_rings=[sliver]),
        ]
        sources = write_sources(tmp_path, subcounties=features)
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        assert result.outcome is ImportOutcome.PUBLISHED, result.blocking_issues
        row = session.execute(
            text(
                "SELECT g.validity_state::text AS state, g.repair_method, g.validity_issues "
                "FROM mars_core.geography_unit_geometry g "
                "JOIN mars_core.geography_unit u ON u.id = g.geography_unit_id "
                "WHERE u.preferred_code = '304101'"
            )
        ).one()
        assert row.state == "invalid_repaired"
        assert "degenerate" in row.repair_method
        assert row.validity_issues is not None

    def test_derived_geometry_dissolves_from_children(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Counties dissolve from subcounties; regions from districts."""
        sources = write_sources(tmp_path, subcounties=two_districts())
        GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        derived = session.execute(
            text(
                """
                SELECT u.level::text AS level, count(*) AS n
                  FROM mars_core.geography_unit u
                  JOIN mars_core.geography_unit_geometry g ON g.geography_unit_id = u.id
                 WHERE g.repair_method = 'dissolved_from_children'
                 GROUP BY 1
                """
            )
        ).all()
        levels = {row.level: row.n for row in derived}
        assert levels.get("county", 0) >= 1
        assert levels.get("region", 0) >= 1

    def test_skipping_geometry_still_builds_the_hierarchy(
        self, session: Session, tmp_path: Path
    ) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(
            ImportOptions(load_geometry=False, derive_geometry=False)
        )
        session.commit()

        assert result.outcome is ImportOutcome.PUBLISHED
        units = session.execute(select(GeographyUnit)).scalars().all()
        assert len(units) > 0
        assert result.geometry_written == 0


class TestBoundaryProvenance:
    def test_every_source_is_recorded_with_its_checksum(
        self, session: Session, tmp_path: Path
    ) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        version = session.get(BoundaryVersion, result.boundary_version_id)
        assert version is not None
        assert version.source_checksum == result.combined_checksum
        assert version.lineage is not None
        assert version.lineage["importer_version"]

        recorded = {entry["role"] for entry in version.lineage["sources"]}
        assert recorded == {
            SourceRole.COUNTRY_BOUNDARY.value,
            SourceRole.DISTRICT_GEOMETRY.value,
            SourceRole.SUBCOUNTY_HIERARCHY.value,
        }
        for entry in version.lineage["sources"]:
            assert len(entry["sha256"]) == 64

    def test_the_validation_summary_is_stored_on_the_version(
        self, session: Session, tmp_path: Path
    ) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(ImportOptions())
        session.commit()

        version = session.get(BoundaryVersion, result.boundary_version_id)
        assert version is not None
        assert version.validation_summary is not None
        assert version.validation_summary["outcome"] == "published"
        assert "control_totals" in version.validation_summary

    def test_imported_by_is_a_service_label(self, session: Session, tmp_path: Path) -> None:
        sources = write_sources(tmp_path, subcounties=two_districts())
        result = GeographyImporter(session, sources).run(
            ImportOptions(imported_by="worker:geography.import")
        )
        session.commit()

        version = session.get(BoundaryVersion, result.boundary_version_id)
        assert version is not None
        assert version.imported_by == "worker:geography.import"


# ---------------------------------------------------------------------------
# The real supplied sources
# ---------------------------------------------------------------------------
class TestRealSuppliedSources:
    """Run against the four supplied files when they are available.

    These are the figures the audit established. If the importer stops
    reproducing them, either the sources changed or the importer regressed, and
    both need a person to look.
    """

    @pytest.fixture
    def real_sources(self) -> dict[SourceRole, Path]:
        directory = os.environ.get(REAL_SOURCES_ENV)
        if not directory:
            pytest.skip(f"{REAL_SOURCES_ENV} is not set; the real-source import test is skipped")
        from mars.ingestion.geography.cli import resolve_sources

        resolved = resolve_sources(Path(directory))
        missing = [path.name for path in resolved.values() if not path.exists()]
        if missing:
            pytest.skip(f"supplied sources not found in {directory}: {missing}")
        return resolved

    def test_imports_the_audited_totals(
        self, session: Session, real_sources: dict[SourceRole, Path]
    ) -> None:
        result = GeographyImporter(session, real_sources).run(ImportOptions())
        session.commit()

        assert result.outcome is ImportOutcome.PUBLISHED, [
            issue.as_dict() for issue in result.blocking_issues[:5]
        ]

        assert result.level("country").total == 1
        assert result.level("region").total == AUDITED_REGIONS
        assert result.level("district").total == AUDITED_DISTRICTS
        assert result.level("county").total == AUDITED_COUNTIES
        assert result.level("subcounty").total == AUDITED_SUBCOUNTIES

    def test_control_totals_agree_with_the_audit(
        self, session: Session, real_sources: dict[SourceRole, Path]
    ) -> None:
        result = GeographyImporter(session, real_sources).run(ImportOptions(load_geometry=True))
        session.commit()

        totals = result.control_totals
        assert totals["district_sum_over_country"] == pytest.approx(1.0, abs=1e-6)
        assert totals["subcounty_sum_over_country"] == pytest.approx(1.0, abs=1e-6)

    def test_source_files_are_not_modified(self, real_sources: dict[SourceRole, Path]) -> None:
        """The importer opens them read-only; this proves it."""
        from mars.ingestion.geography.reader import sha256_of

        before = {path: sha256_of(path) for path in real_sources.values()}
        for path, digest in before.items():
            assert sha256_of(path) == digest, f"{path.name} changed during the test run"
