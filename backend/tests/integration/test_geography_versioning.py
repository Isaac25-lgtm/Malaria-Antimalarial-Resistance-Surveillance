"""Historical boundary versioning against live PostgreSQL.

The defect these tests exist to prevent: the importer matched units on
``(level, preferred_code)``, kept the UUID, and overwrote name, parent, depth,
path, active state and geometry. A second import therefore destroyed the first.
The earlier ``BoundaryVersion`` survived as metadata describing boundaries
nothing could reconstruct, so an analysis pinned to it could not be reproduced -
and nobody would notice until they tried.

The shape of every test here is the same: import version A, record what it says,
import a *changed* version B, then assert A is still exactly what it was.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.enums import BoundaryImportStatus, GeographyLevel
from mars.domain.geography import (
    BoundaryVersion,
    GeographyUnit,
    GeographyUnitGeometry,
    GeographyUnitRevision,
)
from mars.ingestion.geography.importer import GeographyImporter, ImportOptions
from mars.ingestion.geography.reader import SourceRole
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal, GeographyScope
from mars.services.geography_map_service import GeographyMapService

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def versioning_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def session(versioning_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=versioning_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(versioning_engine: Engine) -> Iterator[None]:
    yield
    with versioning_engine.begin() as connection:
        # Revisions of a published version are immutable, so the trigger is
        # lifted to tear down. Only a test does this.
        connection.execute(
            text(
                "ALTER TABLE mars_core.geography_unit_revision "
                "DISABLE TRIGGER geography_unit_revision_immutable"
            )
        )
        for table in (
            "geography_unit_geometry",
            "geography_unit_revision",
            "geography_unit_alias",
            "geography_unit",
            "boundary_version",
        ):
            connection.execute(text(f"DELETE FROM mars_core.{table}"))
        connection.execute(
            text(
                "ALTER TABLE mars_core.geography_unit_revision "
                "ENABLE TRIGGER geography_unit_revision_immutable"
            )
        )


# ---------------------------------------------------------------------------
# Two versions of the same country, differing in ways a real recut differs.
# ---------------------------------------------------------------------------
def rectangle(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list:
    return [
        [
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]
    ]


def subcounty(code: str, district: str, county: str, name: str, ring: list) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "FScode": code,
            "District": district,
            "County": county,
            "Sub_County": name,
            "RCode": code[0],
        },
        "geometry": {"type": "Polygon", "coordinates": ring},
    }


def write_sources(directory: Path, features: list[dict]) -> dict[SourceRole, Path]:
    """Three files the importer reads, tiling exactly."""
    directory.mkdir(parents=True, exist_ok=True)

    districts: dict[str, list[float]] = {}
    for feature in features:
        code = feature["properties"]["FScode"][:3]
        ring = feature["geometry"]["coordinates"][0]
        lons = [point[0] for point in ring]
        lats = [point[1] for point in ring]
        box = districts.setdefault(code, [min(lons), min(lats), max(lons), max(lats)])
        box[0] = min(box[0], min(lons))
        box[1] = min(box[1], min(lats))
        box[2] = max(box[2], max(lons))
        box[3] = max(box[3], max(lats))

    sub_path = directory / "UGANDA_SUBCOUNTIES.json"
    sub_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
    )

    # A MultiPolygon of the district's own subcounty rings, not their bounding
    # box. A box equals the union only when the subcounties happen to stack in
    # one direction, and the control-total check correctly refuses to publish a
    # version whose district areas do not sum to the country.
    district_features = [
        {
            "type": "Feature",
            "properties": {
                "District": next(
                    f["properties"]["District"]
                    for f in features
                    if f["properties"]["FScode"].startswith(code)
                )
            },
            "geometry": {"type": "Polygon", "coordinates": rectangle(*box)},
        }
        for code, box in districts.items()
    ]
    district_path = directory / "UGANDA_DISTRICT.json"
    district_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": district_features}),
        encoding="utf-8",
    )

    all_lons = [b[0] for b in districts.values()] + [b[2] for b in districts.values()]
    all_lats = [b[1] for b in districts.values()] + [b[3] for b in districts.values()]
    country_path = directory / "COUNTRY_BOUNDARY.json"
    country_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "Testland"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": rectangle(
                                min(all_lons), min(all_lats), max(all_lons), max(all_lats)
                            ),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        SourceRole.SUBCOUNTY_HIERARCHY: sub_path,
        SourceRole.DISTRICT_GEOMETRY: district_path,
        SourceRole.COUNTRY_BOUNDARY: country_path,
    }


def version_a() -> list[dict]:
    """Two districts, two subcounties each."""
    return [
        subcounty("301101", "ALPHA", "ALPHA COUNTY", "ALPHA NORTH", rectangle(30, 0, 31, 1)),
        subcounty("301102", "ALPHA", "ALPHA COUNTY", "ALPHA SOUTH", rectangle(30, 1, 31, 2)),
        subcounty("302201", "BETA", "BETA COUNTY", "BETA NORTH", rectangle(31, 0, 32, 1)),
        subcounty("302202", "BETA", "BETA COUNTY", "BETA SOUTH", rectangle(31, 1, 32, 2)),
    ]


def version_b() -> list[dict]:
    """A recut of version A.

    * ``301101`` is **renamed** ALPHA NORTH -> ALPHA CENTRAL
    * ``302202`` (BETA SOUTH) is **dropped**
    * ``302201`` **changes shape**, absorbing the ground BETA SOUTH covered

    Each is something the old importer applied destructively to version A: the
    rename overwrote the name, the drop deactivated the row in place, and the
    reshape overwrote the single geometry row the unit had.

    Re-parenting is deliberately absent. In the supplied source scheme the
    FScode *is* the hierarchy - a subcounty's county is ``FScode[0:4]`` - so a
    unit cannot move while keeping its code. A real recut expresses a move as a
    new code appearing and the old one disappearing, which the drop covers.

    Areas still sum: ALPHA 2, BETA 2, country 4, and every district's
    subcounties tile a rectangle so the district extent is their union.
    """
    return [
        subcounty("301101", "ALPHA", "ALPHA COUNTY", "ALPHA CENTRAL", rectangle(30, 0, 31, 1)),
        subcounty("301102", "ALPHA", "ALPHA COUNTY", "ALPHA SOUTH", rectangle(30, 1, 31, 2)),
        subcounty("302201", "BETA", "BETA COUNTY", "BETA NORTH", rectangle(31, 0, 32, 2)),
    ]


@pytest.fixture
def import_version(session: Session, tmp_path_factory: pytest.TempPathFactory):
    """Import a set of features as a new published boundary version."""

    def _import(features: list[dict], label: str) -> BoundaryVersion:
        directory = tmp_path_factory.mktemp(label)
        sources = write_sources(directory, features)
        result = GeographyImporter(session, sources).run(
            ImportOptions(imported_by=f"test:{label}", force=True)
        )
        session.commit()
        assert result.outcome.value == "published", [
            issue.as_dict() for issue in result.blocking_issues[:3]
        ]
        version = session.execute(
            select(BoundaryVersion).where(
                BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED
            )
        ).scalar_one()
        return version

    return _import


def snapshot(session: Session, version_id: uuid.UUID) -> dict[str, dict]:
    """Everything a version says, keyed by code, for comparison after a recut."""
    rows = session.execute(
        select(GeographyUnitRevision).where(GeographyUnitRevision.boundary_version_id == version_id)
    ).scalars()
    return {
        row.preferred_code: {
            "name": row.raw_name,
            "level": row.level.value,
            "path": row.path,
            "depth": row.depth,
            "present": row.is_present,
            "unit_id": str(row.geography_unit_id),
        }
        for row in rows
    }


class TestVersionASurvivesVersionB:
    def test_the_first_version_is_unchanged_after_the_second(
        self, session: Session, import_version
    ) -> None:
        """The whole defect, in one assertion."""
        first = import_version(version_a(), "a")
        before = snapshot(session, first.id)
        assert before, "version A recorded nothing"

        import_version(version_b(), "b")
        session.expire_all()
        after = snapshot(session, first.id)

        assert after == before

    def test_a_renamed_unit_keeps_its_old_name_under_the_old_version(
        self, session: Session, import_version
    ) -> None:
        first = import_version(version_a(), "a")
        import_version(version_b(), "b")
        session.expire_all()

        historical = snapshot(session, first.id)
        assert historical["301101"]["name"] == "ALPHA NORTH"

    def test_a_dropped_unit_keeps_its_path_under_the_old_version(
        self, session: Session, import_version
    ) -> None:
        """BETA SOUTH is gone in B; A must still say where it was.

        Under the old importer the row was deactivated in place, so version A
        no longer recorded the hierarchy the subcounty had belonged to.
        """
        first = import_version(version_a(), "a")
        before = snapshot(session, first.id)
        # UG / region / district / county / subcounty
        assert before["302202"]["path"].startswith("UG/3/302/")

        import_version(version_b(), "b")
        session.expire_all()

        after = snapshot(session, first.id)
        assert after["302202"]["path"] == before["302202"]["path"]
        assert after["302202"]["present"] is True

    def test_the_second_version_records_the_change(self, session: Session, import_version) -> None:
        import_version(version_a(), "a")
        second = import_version(version_b(), "b")
        session.expire_all()

        current = snapshot(session, second.id)
        assert current["301101"]["name"] == "ALPHA CENTRAL"

    def test_a_dropped_unit_is_absent_from_the_new_version_only(
        self, session: Session, import_version
    ) -> None:
        """BETA SOUTH exists in A and not in B. A must still contain it."""
        first = import_version(version_a(), "a")
        second = import_version(version_b(), "b")
        session.expire_all()

        assert "302202" in snapshot(session, first.id)
        assert "302202" not in snapshot(session, second.id)

    def test_both_versions_have_their_own_revisions(self, session: Session, import_version) -> None:
        first = import_version(version_a(), "a")
        second = import_version(version_b(), "b")
        session.expire_all()

        counts = {
            version_id: session.execute(
                select(GeographyUnitRevision).where(
                    GeographyUnitRevision.boundary_version_id == version_id
                )
            )
            .scalars()
            .all()
            for version_id in (first.id, second.id)
        }
        assert len(counts[first.id]) > 0
        assert len(counts[second.id]) > 0


class TestStableIdentityIsPreserved:
    def test_a_units_uuid_survives_a_recut(self, session: Session, import_version) -> None:
        """Facilities, user scopes and encounters point at this UUID.

        If a recut renumbered it, every one of those references would break.
        """
        first = import_version(version_a(), "a")
        before = snapshot(session, first.id)["301101"]["unit_id"]

        second = import_version(version_b(), "b")
        session.expire_all()
        after = snapshot(session, second.id)["301101"]["unit_id"]

        assert before == after

    def test_a_foreign_key_to_a_unit_still_resolves_after_a_recut(
        self, session: Session, versioning_engine: Engine, import_version
    ) -> None:
        """The end-to-end reason stable identity matters."""
        import_version(version_a(), "a")
        unit_id = (
            session.execute(select(GeographyUnit.id).where(GeographyUnit.preferred_code == "301"))
            .scalars()
            .first()
        )
        assert unit_id is not None

        import_version(version_b(), "b")
        session.expire_all()

        still_there = session.execute(
            select(GeographyUnit).where(GeographyUnit.id == unit_id)
        ).scalar_one_or_none()
        assert still_there is not None


class TestGeometryIsVersioned:
    def test_geometry_is_kept_per_version(self, session: Session, import_version) -> None:
        """BETA NORTH's shape changes in version B; A's must survive."""
        first = import_version(version_a(), "a")
        second = import_version(version_b(), "b")
        session.expire_all()

        unit_id = session.execute(
            select(GeographyUnitRevision.geography_unit_id).where(
                GeographyUnitRevision.boundary_version_id == first.id,
                GeographyUnitRevision.preferred_code == "302201",
            )
        ).scalar_one()

        shapes = (
            session.execute(
                select(GeographyUnitGeometry.boundary_version_id).where(
                    GeographyUnitGeometry.geography_unit_id == unit_id
                )
            )
            .scalars()
            .all()
        )
        assert set(shapes) == {first.id, second.id}

    def test_the_two_shapes_actually_differ(
        self, session: Session, versioning_engine: Engine, import_version
    ) -> None:
        first = import_version(version_a(), "a")
        second = import_version(version_b(), "b")
        session.expire_all()

        with versioning_engine.connect() as connection:
            areas = connection.execute(
                text(
                    "SELECT g.boundary_version_id, round(ST_Area(g.geom)::numeric, 4) "
                    "  FROM mars_core.geography_unit_geometry g "
                    "  JOIN mars_core.geography_unit_revision r "
                    "    ON r.geography_unit_id = g.geography_unit_id "
                    "   AND r.boundary_version_id = g.boundary_version_id "
                    " WHERE r.preferred_code = '302201'"
                )
            ).all()
        by_version = {row[0]: row[1] for row in areas}
        assert by_version[first.id] != by_version[second.id]


class TestPublishedHistoryIsImmutable:
    def test_a_published_revision_cannot_be_updated(
        self, session: Session, versioning_engine: Engine, import_version
    ) -> None:
        """Application discipline is not enough: the importer is the code that
        would rewrite it, so the database refuses."""
        first = import_version(version_a(), "a")
        with (
            versioning_engine.connect() as connection,
            pytest.raises(DatabaseError, match="immutable"),
        ):
            connection.execute(
                text(
                    "UPDATE mars_core.geography_unit_revision SET raw_name = 'X' "
                    "WHERE boundary_version_id = :v"
                ),
                {"v": first.id},
            )

    def test_a_published_revision_cannot_be_deleted(
        self, session: Session, versioning_engine: Engine, import_version
    ) -> None:
        first = import_version(version_a(), "a")
        with (
            versioning_engine.connect() as connection,
            pytest.raises(DatabaseError, match="immutable"),
        ):
            connection.execute(
                text(
                    "DELETE FROM mars_core.geography_unit_revision WHERE boundary_version_id = :v"
                ),
                {"v": first.id},
            )


class TestPublicationIsAtomic:
    def test_a_failed_import_does_not_displace_the_published_version(
        self, session: Session, import_version, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A broken recut must leave the last good version serving."""
        good = import_version(version_a(), "a")

        # A source whose subcounty areas do not sum to the country: the control
        # total check refuses to publish it.
        broken = [subcounty("301101", "ALPHA", "ALPHA COUNTY", "ONLY", rectangle(30, 0, 30.5, 0.5))]
        directory = tmp_path_factory.mktemp("broken")
        sources = write_sources(directory, broken)
        # Deliberately mismatch the country outline so control totals fail.
        (directory / "COUNTRY_BOUNDARY.json").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "Testland"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": rectangle(0, 0, 50, 50),
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = GeographyImporter(session, sources).run(
            ImportOptions(imported_by="test:broken", force=True)
        )
        session.commit()
        session.expire_all()

        assert result.outcome.value != "published"
        still_published = session.execute(
            select(BoundaryVersion).where(
                BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED
            )
        ).scalar_one()
        assert still_published.id == good.id


class TestQueriesSelectTheRightVersion:
    def _principal(self, session: Session) -> AuthenticatedPrincipal:
        country = (
            session.execute(
                select(GeographyUnit).where(GeographyUnit.level == GeographyLevel.COUNTRY)
            )
            .scalars()
            .first()
        )
        assert country is not None
        return AuthenticatedPrincipal(
            user_id=uuid.uuid4(),
            subject="test",
            username="test",
            display_name="Test",
            roles=frozenset({"national_programme"}),
            permissions=frozenset({Permission.GEOGRAPHY_VIEW}),
            max_sensitivity=SensitivityLevel.AGGREGATE,
            geography_scopes=(
                GeographyScope(
                    geography_unit_id=country.id,
                    preferred_code=country.preferred_code,
                    level="country",
                    name=country.raw_name,
                    path=country.path or "UG",
                ),
            ),
        )

    def test_the_map_draws_the_currently_published_version(
        self, session: Session, import_version
    ) -> None:
        import_version(version_a(), "a")
        import_version(version_b(), "b")
        session.expire_all()

        collection = GeographyMapService(session).feature_collection(
            self._principal(session), level=GeographyLevel.SUBCOUNTY
        )
        names = {f["properties"]["name"] for f in collection.features}
        assert "ALPHA CENTRAL" in names
        assert "ALPHA NORTH" not in names

    def test_each_unit_is_drawn_once_after_a_recut(self, session: Session, import_version) -> None:
        """Geometry is per (unit, version).

        A join on the unit alone would return one row per version the unit ever
        had, and every boundary would be drawn twice.
        """
        import_version(version_a(), "a")
        import_version(version_b(), "b")
        session.expire_all()

        collection = GeographyMapService(session).feature_collection(
            self._principal(session), level=GeographyLevel.SUBCOUNTY
        )
        ids = [f["properties"]["unit_id"] for f in collection.features]
        assert len(ids) == len(set(ids))

    def test_a_historical_query_returns_the_pinned_version(
        self, session: Session, import_version
    ) -> None:
        """Reads revisions, never the cached columns.

        Those hold whatever the last import wrote; asking them about an earlier
        version returns today's answer wearing yesterday's date.
        """
        first = import_version(version_a(), "a")
        import_version(version_b(), "b")
        session.expire_all()

        historical = GeographyMapService(session).historical_hierarchy(
            self._principal(session), first.id, level=GeographyLevel.SUBCOUNTY
        )
        names = {unit.name for unit in historical}
        assert "ALPHA NORTH" in names
        assert "ALPHA CENTRAL" not in names

    def test_the_historical_query_includes_a_since_dropped_unit(
        self, session: Session, import_version
    ) -> None:
        first = import_version(version_a(), "a")
        import_version(version_b(), "b")
        session.expire_all()

        historical = GeographyMapService(session).historical_hierarchy(
            self._principal(session), first.id
        )
        assert "302202" in {unit.preferred_code for unit in historical}
