"""Map delivery against live PostgreSQL and PostGIS.

The API tests prove what a refusal looks like without a database. These prove
the part that only real geometry can: that scope is applied inside the query,
that a district user's payload physically cannot contain another district, that
the browser payload is a small fraction of the analytical geometry, and that the
cache validator tracks the published boundary version.

Two tiers, as in the importer tests:

* **Synthetic geography**, four districts on a grid, inserted directly. Fast and
  deterministic, and every scope assertion is exact.
* **The real supplied sources**, when ``MARS_GEOGRAPHY_DATA_DIR`` points at
  them: the measured national payload, and the district-to-subcounty drill.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import gzip
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from mars.core.errors import NotFoundError
from mars.domain.enums import GeographyLevel
from mars.ingestion.geography.importer import GeographyImporter, ImportOptions
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal, GeographyScope
from mars.services.geography_map_service import (
    FEATURE_PROPERTIES,
    MAX_FEATURES,
    FeatureLimitExceededError,
    GeographyMapService,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]
REAL_SOURCES_ENV = "MARS_GEOGRAPHY_DATA_DIR"

#: Synthetic identifiers. Fixed so a failure names the same unit every run.
COUNTRY_ID = uuid.UUID("aa000000-0000-4000-8000-000000000001")
DISTRICT_IDS = [uuid.UUID(f"aa000000-0000-4000-8000-0000000001{i:02d}") for i in range(4)]
SUBCOUNTY_IDS = [uuid.UUID(f"aa000000-0000-4000-8000-0000000002{i:02d}") for i in range(8)]
BOUNDARY_VERSION_ID = uuid.UUID("aa000000-0000-4000-8000-00000000ffff")


@pytest.fixture(scope="module")
def map_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def session(map_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=map_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean_geography(map_engine: Engine, request: pytest.FixtureRequest) -> Iterator[None]:
    """Empty the hierarchy after each test that built its own.

    Tests using the module-scoped real_geography fixture are exempt: that
    import takes minutes, and wiping it between tests would leave every test
    after the first looking at an empty database. Those tests are read-only, so
    they cannot disturb one another.
    """
    yield
    if "real_geography" in request.fixturenames:
        return
    with map_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_core.geography_unit_geometry"))
        connection.execute(text("DELETE FROM mars_core.geography_unit_alias"))
        connection.execute(text("DELETE FROM mars_core.geography_unit"))
        connection.execute(text("DELETE FROM mars_core.boundary_version"))


def _box(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """A rectangular MultiPolygon as GeoJSON text, for ST_GeomFromGeoJSON."""
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    return json.dumps({"type": "MultiPolygon", "coordinates": [[ring]]})


@pytest.fixture
def synthetic_geography(map_engine: Engine) -> None:
    """A country, four districts in a row, two subcounties in each.

    Deliberately simple: the point of these tests is scope and payload, not
    geometry handling, which the importer tests already cover.
    """
    with map_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, source_checksum, storage_crs,
                     import_status, imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-MAP-0001', 'Synthetic map fixture', 'synthetic',
                        'deadbeef', 'EPSG:4326', 'published', now(), 'test',
                        now(), now())
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )

        def insert_unit(
            unit_id: uuid.UUID,
            level: str,
            code: str,
            name: str,
            parent: uuid.UUID | None,
            depth: int,
            path: str,
        ) -> None:
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.geography_unit
                        (id, boundary_version_id, level, unit_kind, preferred_code,
                         raw_name, normalised_name, parent_id, depth, path,
                         is_active, created_at, updated_at)
                    VALUES (:id, :version, :level, 'unspecified', :code, :name,
                            :normalised, :parent, :depth, :path, true, now(), now())
                    """
                ),
                {
                    "id": unit_id,
                    "version": BOUNDARY_VERSION_ID,
                    "level": level,
                    "code": code,
                    "name": name,
                    "normalised": name.lower(),
                    "parent": parent,
                    "depth": depth,
                    "path": path,
                },
            )

        def insert_geometry(unit_id: uuid.UUID, geojson: str) -> None:
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.geography_unit_geometry
                        (id, geography_unit_id, validity_state, geom, geom_web,
                         area_sq_km, bbox_min_lon, bbox_min_lat, bbox_max_lon,
                         bbox_max_lat, created_at, updated_at)
                    SELECT gen_random_uuid(), :unit, 'valid', g.geom, g.geom,
                           ST_Area(g.geom::geography) / 1000000.0,
                           ST_XMin(g.geom), ST_YMin(g.geom),
                           ST_XMax(g.geom), ST_YMax(g.geom), now(), now()
                      FROM (SELECT ST_Multi(ST_SetSRID(
                                ST_GeomFromGeoJSON(:geojson), 4326)) AS geom) AS g
                    """
                ),
                {"unit": unit_id, "geojson": geojson},
            )

        insert_unit(COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG")
        insert_geometry(COUNTRY_ID, _box(30.0, 0.0, 34.0, 1.0))

        for index, district_id in enumerate(DISTRICT_IDS):
            code = f"{300 + index}"
            path = f"UG/{code}"
            insert_unit(district_id, "district", code, f"District {index}", COUNTRY_ID, 1, path)
            insert_geometry(district_id, _box(30.0 + index, 0.0, 31.0 + index, 1.0))

            for half in range(2):
                sub_index = index * 2 + half
                sub_code = f"{code}{half:02d}"
                insert_unit(
                    SUBCOUNTY_IDS[sub_index],
                    "subcounty",
                    sub_code,
                    f"Subcounty {sub_index}",
                    district_id,
                    2,
                    f"{path}/{sub_code}",
                )
                insert_geometry(
                    SUBCOUNTY_IDS[sub_index],
                    _box(30.0 + index, 0.5 * half, 31.0 + index, 0.5 * (half + 1)),
                )


def principal(
    *scopes: GeographyScope, permissions: frozenset[Permission] | None = None
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid.uuid4(),
        subject="test",
        username="test",
        display_name="Test",
        roles=frozenset({"district_hsd"}),
        permissions=permissions
        if permissions is not None
        else frozenset({Permission.GEOGRAPHY_VIEW}),
        max_sensitivity=SensitivityLevel.AGGREGATE,
        geography_scopes=scopes,
        is_synthetic=True,
    )


def national_scope() -> GeographyScope:
    return GeographyScope(
        geography_unit_id=COUNTRY_ID,
        preferred_code="UG",
        level="country",
        name="Testland",
        path="UG",
    )


def district_scope(index: int) -> GeographyScope:
    return GeographyScope(
        geography_unit_id=DISTRICT_IDS[index],
        preferred_code=f"{300 + index}",
        level="district",
        name=f"District {index}",
        path=f"UG/{300 + index}",
    )


class TestScopeIsAppliedInTheQuery:
    """A district user's payload cannot contain another district."""

    def test_national_user_sees_every_district(
        self, session: Session, synthetic_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()), level=GeographyLevel.DISTRICT
        )
        assert len(collection.features) == 4

    def test_district_user_sees_only_their_own(
        self, session: Session, synthetic_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(district_scope(1)), level=GeographyLevel.DISTRICT
        )
        assert len(collection.features) == 1
        assert collection.features[0]["properties"]["code"] == "301"

    def test_no_neighbouring_district_appears_in_any_property(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """Checked against the serialised bytes, not the object graph.

        A leak through a name, a path or a parent id would be invisible to an
        assertion on feature count alone.
        """
        collection = GeographyMapService(session).feature_collection(
            principal(district_scope(1)), level=GeographyLevel.DISTRICT
        )
        body = json.dumps(collection.as_geojson())
        for other in (0, 2, 3):
            assert f"District {other}" not in body
            assert str(DISTRICT_IDS[other]) not in body

    def test_a_scopeless_principal_sees_nothing(
        self, session: Session, synthetic_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(), level=GeographyLevel.DISTRICT
        )
        assert collection.features == []
        assert collection.matched_count == 0

    def test_subcounties_are_scoped_too(self, session: Session, synthetic_geography: None) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(district_scope(2)), level=GeographyLevel.SUBCOUNTY
        )
        assert len(collection.features) == 2
        for feature in collection.features:
            assert feature["properties"]["path"].startswith("UG/302/")

    def test_the_matched_count_respects_scope(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """The ceiling must count what the caller would receive, not the table.

        Counting unscoped rows would refuse a district user a layer they are
        entitled to - and would leak the national total through the error.
        """
        collection = GeographyMapService(session).feature_collection(
            principal(district_scope(0)), level=GeographyLevel.SUBCOUNTY
        )
        assert collection.matched_count == 2


class TestCrossDistrictDenial:
    """Reaching for a neighbour is not found, never forbidden."""

    def test_geometry_of_another_district_is_not_found(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        with pytest.raises(NotFoundError):
            service.unit_geometry(principal(district_scope(0)), DISTRICT_IDS[3])

    def test_bounds_of_another_district_are_not_found(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        with pytest.raises(NotFoundError):
            service.unit_bounds(principal(district_scope(0)), DISTRICT_IDS[3])

    def test_a_hidden_unit_and_a_nonexistent_one_fail_identically(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """The whole point of existence-hiding, asserted on the messages."""
        service = GeographyMapService(session)
        caller = principal(district_scope(0))

        with pytest.raises(NotFoundError) as hidden:
            service.unit_geometry(caller, DISTRICT_IDS[3])
        with pytest.raises(NotFoundError) as absent:
            service.unit_geometry(caller, uuid.uuid4())

        assert str(hidden.value) == str(absent.value)

    def test_filtering_by_a_hidden_parent_is_not_found(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """An empty collection would confirm the parent id was real."""
        service = GeographyMapService(session)
        with pytest.raises(NotFoundError):
            service.feature_collection(
                principal(district_scope(0)),
                level=GeographyLevel.SUBCOUNTY,
                parent_id=DISTRICT_IDS[3],
            )

    def test_filtering_within_a_hidden_ancestor_is_not_found(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        with pytest.raises(NotFoundError):
            service.feature_collection(
                principal(district_scope(0)),
                level=GeographyLevel.SUBCOUNTY,
                within_id=DISTRICT_IDS[3],
            )


class TestPropertyAllowList:
    """Only the declared properties reach a client."""

    def test_features_carry_exactly_the_allow_list(
        self, session: Session, synthetic_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()), level=GeographyLevel.DISTRICT
        )
        assert collection.features
        for feature in collection.features:
            assert set(feature["properties"]) == FEATURE_PROPERTIES

    def test_no_internal_column_leaks_into_the_payload(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """Geometry-table internals are diagnostics, not public map data."""
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()), level=GeographyLevel.DISTRICT
        )
        body = json.dumps(collection.as_geojson())
        for internal in (
            "validity_issues",
            "repair_method",
            "vertex_count",
            "ring_count",
            "perimeter_km",
            "normalised_name",
            "simplification_tolerance_deg",
        ):
            assert internal not in body, f"{internal} reached the map payload"

    def test_the_feature_id_is_the_canonical_unit_id(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """So a map click needs no translation table."""
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()), level=GeographyLevel.DISTRICT
        )
        for feature in collection.features:
            assert feature["id"] == feature["properties"]["unit_id"]


class TestGeometryResponseShape:
    """The response is valid GeoJSON, and says which boundaries it came from."""

    def test_the_document_is_a_feature_collection(
        self, session: Session, synthetic_geography: None
    ) -> None:
        document = (
            GeographyMapService(session)
            .feature_collection(principal(national_scope()), level=GeographyLevel.DISTRICT)
            .as_geojson()
        )
        assert document["type"] == "FeatureCollection"
        assert all(f["type"] == "Feature" for f in document["features"])
        assert all(f["geometry"]["type"] == "MultiPolygon" for f in document["features"])

    def test_every_feature_geometry_has_coordinates(
        self, session: Session, synthetic_geography: None
    ) -> None:
        document = (
            GeographyMapService(session)
            .feature_collection(principal(national_scope()), level=GeographyLevel.DISTRICT)
            .as_geojson()
        )
        for feature in document["features"]:
            assert feature["geometry"]["coordinates"]

    def test_the_collection_carries_a_bounding_box(
        self, session: Session, synthetic_geography: None
    ) -> None:
        document = (
            GeographyMapService(session)
            .feature_collection(principal(national_scope()), level=GeographyLevel.DISTRICT)
            .as_geojson()
        )
        assert document["bbox"] == [30.0, 0.0, 34.0, 1.0]

    def test_the_bounding_box_narrows_with_scope(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """A district user's viewport must fit their district, not the country."""
        document = (
            GeographyMapService(session)
            .feature_collection(principal(district_scope(1)), level=GeographyLevel.DISTRICT)
            .as_geojson()
        )
        assert document["bbox"] == [31.0, 0.0, 32.0, 1.0]

    def test_the_boundary_version_travels_inside_the_document(
        self, session: Session, synthetic_geography: None
    ) -> None:
        document = (
            GeographyMapService(session)
            .feature_collection(principal(national_scope()), level=GeographyLevel.DISTRICT)
            .as_geojson()
        )
        assert document["mars"]["boundary_version_code"] == "TEST-MAP-0001"
        assert document["mars"]["geometry_resolution"] == "simplified"


class TestBoundaryVersion:
    """Everything is pinned to the one published version."""

    def test_metadata_names_the_published_version(
        self, session: Session, synthetic_geography: None
    ) -> None:
        metadata = GeographyMapService(session).map_metadata(principal(national_scope()))
        assert metadata.is_available is True
        assert metadata.boundary_version_code == "TEST-MAP-0001"
        assert metadata.source_checksum == "deadbeef"

    def test_metadata_reports_unavailable_with_nothing_published(self, session: Session) -> None:
        metadata = GeographyMapService(session).map_metadata(principal(national_scope()))
        assert metadata.is_available is False
        assert metadata.boundary_version_id is None

    def test_levels_with_no_data_are_reported_as_not_drawable(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """A missing parish layer must read as "none supplied", not as absent."""
        metadata = GeographyMapService(session).map_metadata(principal(national_scope()))
        by_level = {level.level: level for level in metadata.levels}
        assert by_level["parish"].is_drawable is False
        assert by_level["parish"].unit_count == 0
        assert by_level["district"].is_drawable is True

    def test_the_initial_viewport_follows_the_callers_scope(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)

        nationally = service.map_metadata(principal(national_scope()))
        assert nationally.initial_unit_level == "country"
        assert nationally.initial_bounds is not None
        assert nationally.initial_bounds.as_list() == [30.0, 0.0, 34.0, 1.0]

        locally = service.map_metadata(principal(district_scope(2)))
        assert locally.initial_unit_level == "district"
        assert locally.initial_bounds is not None
        assert locally.initial_bounds.as_list() == [32.0, 0.0, 33.0, 1.0]

    def test_level_counts_are_scoped(self, session: Session, synthetic_geography: None) -> None:
        metadata = GeographyMapService(session).map_metadata(principal(district_scope(0)))
        by_level = {level.level: level for level in metadata.levels}
        assert by_level["district"].unit_count == 1
        assert by_level["subcounty"].unit_count == 2


class TestCacheValidator:
    """The ETag identifies a layer of a boundary version, and nothing else."""

    def test_the_same_request_produces_the_same_etag(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        caller = principal(national_scope())
        first = service.feature_collection(caller, level=GeographyLevel.DISTRICT)
        second = service.feature_collection(caller, level=GeographyLevel.DISTRICT)
        assert first.etag == second.etag
        assert first.etag.startswith('"')

    def test_a_different_level_produces_a_different_etag(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        caller = principal(national_scope())
        districts = service.feature_collection(caller, level=GeographyLevel.DISTRICT)
        subcounties = service.feature_collection(caller, level=GeographyLevel.SUBCOUNTY)
        assert districts.etag != subcounties.etag

    def test_a_different_subtree_produces_a_different_etag(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        caller = principal(national_scope())
        first = service.feature_collection(
            caller, level=GeographyLevel.SUBCOUNTY, within_id=DISTRICT_IDS[0]
        )
        second = service.feature_collection(
            caller, level=GeographyLevel.SUBCOUNTY, within_id=DISTRICT_IDS[1]
        )
        assert first.etag != second.etag

    def test_the_etag_does_not_encode_the_caller(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """Responses are private and never shared between users.

        Mixing the principal in would turn a cache validator into a weak
        identifier of who fetched what, which is worse than useless here.
        """
        service = GeographyMapService(session)
        national = service.feature_collection(
            principal(national_scope()), level=GeographyLevel.DISTRICT
        )
        local = service.feature_collection(
            principal(district_scope(1)), level=GeographyLevel.DISTRICT
        )
        assert national.etag == local.etag


class TestPayloadCeiling:
    """Too large is refused, never truncated."""

    def test_a_request_above_the_ceiling_is_refused(
        self, session: Session, synthetic_geography: None
    ) -> None:
        service = GeographyMapService(session)
        with pytest.raises(FeatureLimitExceededError) as exc:
            service.feature_collection(
                principal(national_scope()), level=GeographyLevel.SUBCOUNTY, limit=4
            )
        assert exc.value.matched == 8
        assert exc.value.limit == 4

    def test_the_refusal_is_a_413(self, session: Session, synthetic_geography: None) -> None:
        service = GeographyMapService(session)
        with pytest.raises(FeatureLimitExceededError) as exc:
            service.feature_collection(
                principal(national_scope()), level=GeographyLevel.SUBCOUNTY, limit=4
            )
        assert exc.value.status_code == 413
        assert exc.value.code == "geography_request_too_broad"

    def test_a_request_within_the_ceiling_is_served_whole(
        self, session: Session, synthetic_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()), level=GeographyLevel.SUBCOUNTY, limit=8
        )
        assert len(collection.features) == 8
        assert collection.truncated is False

    def test_narrowing_by_subtree_brings_it_under_the_ceiling(
        self, session: Session, synthetic_geography: None
    ) -> None:
        """The remedy the error suggests must actually work."""
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()),
            level=GeographyLevel.SUBCOUNTY,
            within_id=DISTRICT_IDS[0],
            limit=4,
        )
        assert len(collection.features) == 2


class TestSubtreeFiltering:
    """``within_id`` reaches descendants; ``parent_id`` only children."""

    def test_within_returns_descendants_at_the_requested_level(
        self, session: Session, synthetic_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()),
            level=GeographyLevel.SUBCOUNTY,
            within_id=DISTRICT_IDS[2],
        )
        assert len(collection.features) == 2
        for feature in collection.features:
            assert feature["properties"]["path"].startswith("UG/302/")

    def test_within_does_not_match_a_sibling_with_a_shared_prefix(
        self, session: Session, map_engine: Engine, synthetic_geography: None
    ) -> None:
        """``UG/300`` must not match ``UG/3001``.

        The importer's own path guard has this property; the map query builds a
        LIKE pattern itself, so it needs its own proof.
        """
        decoy = uuid.UUID("aa000000-0000-4000-8000-0000000009ff")
        with map_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.geography_unit
                        (id, boundary_version_id, level, unit_kind, preferred_code,
                         raw_name, normalised_name, parent_id, depth, path,
                         is_active, created_at, updated_at)
                    VALUES (:id, :version, 'subcounty', 'unspecified', '3001',
                            'Decoy', 'decoy', :parent, 2, 'UG/3001/x', true,
                            now(), now())
                    """
                ),
                {"id": decoy, "version": BOUNDARY_VERSION_ID, "parent": COUNTRY_ID},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.geography_unit_geometry
                        (id, geography_unit_id, validity_state, geom, geom_web,
                         created_at, updated_at)
                    SELECT gen_random_uuid(), :unit, 'valid', g.geom, g.geom,
                           now(), now()
                      FROM (SELECT ST_Multi(ST_SetSRID(
                                ST_GeomFromGeoJSON(:geojson), 4326)) AS geom) AS g
                    """
                ),
                {"unit": decoy, "geojson": _box(40.0, 0.0, 41.0, 1.0)},
            )

        collection = GeographyMapService(session).feature_collection(
            principal(national_scope()),
            level=GeographyLevel.SUBCOUNTY,
            within_id=DISTRICT_IDS[0],
        )
        names = {f["properties"]["name"] for f in collection.features}
        assert "Decoy" not in names
        assert len(collection.features) == 2


class TestSingleUnitReads:
    def test_unit_geometry_returns_one_feature(
        self, session: Session, synthetic_geography: None
    ) -> None:
        feature = GeographyMapService(session).unit_geometry(
            principal(national_scope()), DISTRICT_IDS[0]
        )
        assert feature["type"] == "Feature"
        assert set(feature["properties"]) == FEATURE_PROPERTIES
        assert feature["bbox"] == [30.0, 0.0, 31.0, 1.0]

    def test_unit_bounds_returns_the_extent(
        self, session: Session, synthetic_geography: None
    ) -> None:
        bounds = GeographyMapService(session).unit_bounds(
            principal(national_scope()), DISTRICT_IDS[3]
        )
        assert bounds.as_list() == [33.0, 0.0, 34.0, 1.0]

    def test_a_unit_without_geometry_is_reported_as_not_found(
        self, session: Session, map_engine: Engine, synthetic_geography: None
    ) -> None:
        """Distinct from a missing unit only in the message, deliberately.

        Both are "there is nothing to draw", and neither confirms anything about
        a unit the caller cannot see.
        """
        bare = uuid.UUID("aa000000-0000-4000-8000-00000000bbbb")
        with map_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.geography_unit
                        (id, boundary_version_id, level, unit_kind, preferred_code,
                         raw_name, normalised_name, parent_id, depth, path,
                         is_active, created_at, updated_at)
                    VALUES (:id, :version, 'district', 'unspecified', '399',
                            'No Geometry', 'no geometry', :parent, 1, 'UG/399',
                            true, now(), now())
                    """
                ),
                {"id": bare, "version": BOUNDARY_VERSION_ID, "parent": COUNTRY_ID},
            )
        with pytest.raises(NotFoundError):
            GeographyMapService(session).unit_geometry(principal(national_scope()), bare)


# ---------------------------------------------------------------------------
# Over HTTP.
#
# The tests above exercise the service against real geometry. These run the
# whole request: routing, the response models, the cache headers and the
# exception handlers, against the same database. Authentication is the one thing
# short-circuited - a principal is injected rather than minted - because token
# issuance is Prompt 3's contract and is proved there.
# ---------------------------------------------------------------------------
@pytest.fixture
def map_client(map_engine: Engine):
    """A TestClient bound to the live database, authenticated as a given principal."""
    from fastapi.testclient import TestClient

    from mars.api.dependencies import get_current_principal, get_db_session
    from mars.core.settings import Environment, Settings
    from mars.main import create_app

    application = create_app(
        Settings(
            environment=Environment.LOCAL,
            database_url="postgresql+psycopg://unused:unused@localhost:5432/unused",
            log_format="console",
        )
    )
    factory = sessionmaker(bind=map_engine, expire_on_commit=False, future=True)

    def _session() -> Iterator[Session]:
        with factory() as db:
            yield db

    class _NoopAudit:
        """Denials are asserted in the API tests; here they would need a writable
        audit table on every request and prove nothing new."""

        def record(self, **_kwargs: object) -> None:
            return None

        def record_denial(self, **_kwargs: object) -> None:
            return None

    from mars.api.dependencies import get_audit_service

    application.dependency_overrides[get_db_session] = _session
    application.dependency_overrides[get_audit_service] = _NoopAudit

    def _as(caller: AuthenticatedPrincipal) -> TestClient:
        application.dependency_overrides[get_current_principal] = lambda: caller
        return TestClient(application, raise_server_exceptions=False)

    yield _as
    application.dependency_overrides.clear()


class TestOverHttp:
    """The full request path, against real geometry."""

    def test_the_national_layer_is_served_as_geojson(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get(
            "/api/v1/geography/map/features?level=district"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "FeatureCollection"
        assert len(body["features"]) == 4

    def test_the_response_carries_an_etag_and_private_caching(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get(
            "/api/v1/geography/map/features?level=district"
        )
        assert response.headers["etag"].startswith('"')
        assert "private" in response.headers["cache-control"]

    def test_a_matching_if_none_match_returns_304(
        self, map_client, synthetic_geography: None
    ) -> None:
        """The whole point of the validator: a revalidation costs a few bytes."""
        client = map_client(principal(national_scope()))
        first = client.get("/api/v1/geography/map/features?level=district")
        etag = first.headers["etag"]

        second = client.get(
            "/api/v1/geography/map/features?level=district",
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.content == b""

    def test_a_stale_if_none_match_returns_the_body(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get(
            "/api/v1/geography/map/features?level=district",
            headers={"If-None-Match": '"not-the-current-version"'},
        )
        assert response.status_code == 200
        assert response.json()["features"]

    def test_metadata_is_served(self, map_client, synthetic_geography: None) -> None:
        response = map_client(principal(national_scope())).get("/api/v1/geography/map/metadata")
        assert response.status_code == 200
        assert response.json()["boundary_version_code"] == "TEST-MAP-0001"

    def test_national_returns_the_root_and_its_children(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get("/api/v1/geography/national")
        assert response.status_code == 200
        body = response.json()
        assert body["root"]["name"] == "Testland"
        assert len(body["children"]) == 4

    def test_a_district_user_gets_their_own_root(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(district_scope(2))).get("/api/v1/geography/national")
        body = response.json()
        assert body["root"]["name"] == "District 2"
        assert body["bounds"] == {
            "min_lon": 32.0,
            "min_lat": 0.0,
            "max_lon": 33.0,
            "max_lat": 1.0,
        }

    def test_an_over_large_request_is_a_413_problem_document(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get(
            "/api/v1/geography/map/features?level=subcounty&limit=4"
        )
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == "geography_request_too_broad"
        assert "400" not in body["title"]

    def test_a_cross_district_unit_is_404_over_http(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(district_scope(0))).get(
            f"/api/v1/geography/units/{DISTRICT_IDS[3]}/geometry"
        )
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_breadcrumbs_end_with_the_requested_unit(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get(
            f"/api/v1/geography/units/{SUBCOUNTY_IDS[0]}/breadcrumbs"
        )
        assert response.status_code == 200
        trail = response.json()["breadcrumbs"]
        assert [step["level"] for step in trail] == ["country", "district", "subcounty"]
        assert trail[-1]["is_current"] is True

    def test_district_lookup_by_code(self, map_client, synthetic_geography: None) -> None:
        response = map_client(principal(national_scope())).get("/api/v1/geography/districts/301")
        assert response.status_code == 200
        assert response.json()["name"] == "District 1"

    def test_district_lookup_outside_scope_is_404(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(district_scope(0))).get("/api/v1/geography/districts/303")
        assert response.status_code == 404

    def test_bounds_are_served_without_geometry(
        self, map_client, synthetic_geography: None
    ) -> None:
        response = map_client(principal(national_scope())).get(
            f"/api/v1/geography/units/{DISTRICT_IDS[1]}/bounds"
        )
        assert response.status_code == 200
        assert response.json() == {
            "min_lon": 31.0,
            "min_lat": 0.0,
            "max_lon": 32.0,
            "max_lat": 1.0,
        }
        assert "geometry" not in response.text


# ---------------------------------------------------------------------------
# The real supplied sources.
# ---------------------------------------------------------------------------
def _real_sources() -> Path:
    directory = os.environ.get(REAL_SOURCES_ENV)
    if not directory:
        pytest.skip(f"{REAL_SOURCES_ENV} is not set; the real-source map test is skipped.")
    return Path(directory)


@pytest.fixture(scope="module")
def real_geography(map_engine: Engine) -> Iterator[None]:
    """Import the supplied Uganda geography once for this module.

    Module-scoped because the import takes minutes; the assertions below are
    read-only, so they cannot disturb each other.
    """
    from mars.ingestion.geography.cli import resolve_sources

    directory = _real_sources()
    sources = resolve_sources(directory)
    missing = [path.name for path in sources.values() if not path.exists()]
    if missing:
        pytest.skip(f"supplied sources not found in {directory}: {missing}")

    factory = sessionmaker(bind=map_engine, expire_on_commit=False, future=True)
    with factory() as db:
        result = GeographyImporter(db, sources).run(
            ImportOptions(imported_by="test:map", force=True)
        )
        db.commit()
    assert result.outcome.value == "published", [
        issue.as_dict() for issue in result.blocking_issues[:5]
    ]
    yield
    with map_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_core.geography_unit_geometry"))
        connection.execute(text("DELETE FROM mars_core.geography_unit_alias"))
        connection.execute(text("DELETE FROM mars_core.geography_unit"))
        connection.execute(text("DELETE FROM mars_core.boundary_version"))


class TestRealSuppliedGeography:
    """The measured national map, against the geography actually supplied."""

    def test_the_national_district_layer_fits_the_ceiling(
        self, session: Session, real_geography: None
    ) -> None:
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope_for(session)), level=GeographyLevel.DISTRICT, limit=400
        )
        assert len(collection.features) == 146
        assert len(collection.features) < MAX_FEATURES

    def test_the_browser_payload_is_a_small_fraction_of_the_analytical_geometry(
        self, session: Session, real_geography: None
    ) -> None:
        """The point of storing ``geom_web`` at all.

        Raw district geometry is roughly 10 MB of GeoJSON. Anything close to
        that has stopped being a map payload.
        """
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope_for(session)), level=GeographyLevel.DISTRICT, limit=400
        )
        simplified = len(json.dumps(collection.as_geojson(), separators=(",", ":")).encode())
        full = session.execute(
            text(
                """
                SELECT sum(length(ST_AsGeoJSON(g.geom, 6)))
                  FROM mars_core.geography_unit u
                  JOIN mars_core.geography_unit_geometry g
                    ON g.geography_unit_id = u.id
                 WHERE u.level = 'district'
                """
            )
        ).scalar_one()

        assert simplified < full * 0.10, (
            f"simplified payload is {simplified / full:.1%} of full resolution"
        )
        assert simplified < 600_000, f"national district payload is {simplified} bytes"

    def test_the_national_layer_compresses_to_a_reasonable_transfer(
        self, session: Session, real_geography: None
    ) -> None:
        """What actually crosses the wire, since responses are gzipped."""
        collection = GeographyMapService(session).feature_collection(
            principal(national_scope_for(session)), level=GeographyLevel.DISTRICT, limit=400
        )
        body = json.dumps(collection.as_geojson(), separators=(",", ":")).encode()
        assert len(gzip.compress(body, 6)) < 200_000

    def test_a_national_subcounty_request_is_refused(
        self, session: Session, real_geography: None
    ) -> None:
        with pytest.raises(FeatureLimitExceededError) as exc:
            GeographyMapService(session).feature_collection(
                principal(national_scope_for(session)),
                level=GeographyLevel.SUBCOUNTY,
                limit=400,
            )
        assert exc.value.matched == 2190

    def test_every_district_drills_to_its_subcounties_within_the_ceiling(
        self, session: Session, real_geography: None
    ) -> None:
        """The drill-down must work for every district, not a chosen one.

        The largest district holds 44 subcounties; if any exceeded the ceiling
        the map would have a district that could not be opened.
        """
        service = GeographyMapService(session)
        caller = principal(national_scope_for(session))
        districts = (
            session.execute(text("SELECT id FROM mars_core.geography_unit WHERE level='district'"))
            .scalars()
            .all()
        )

        assert len(districts) == 146
        largest = 0
        for district_id in districts:
            collection = service.feature_collection(
                caller, level=GeographyLevel.SUBCOUNTY, within_id=district_id, limit=400
            )
            assert collection.features, f"district {district_id} has no subcounties"
            largest = max(largest, len(collection.features))
        assert largest < MAX_FEATURES

    def test_metadata_opens_on_uganda(self, session: Session, real_geography: None) -> None:
        metadata = GeographyMapService(session).map_metadata(principal(national_scope_for(session)))
        assert metadata.is_available is True
        assert metadata.initial_unit_level == "country"
        bounds = metadata.initial_bounds
        assert bounds is not None
        # Uganda lies between roughly 29.5E-35.0E and 1.5S-4.2N.
        assert 29.0 < bounds.min_lon < 30.5
        assert 34.0 < bounds.max_lon < 35.5
        assert -2.0 < bounds.min_lat < -1.0
        assert 4.0 < bounds.max_lat < 4.5

    def test_every_level_that_holds_units_is_drawable(
        self, session: Session, real_geography: None
    ) -> None:
        metadata = GeographyMapService(session).map_metadata(principal(national_scope_for(session)))
        for level in metadata.levels:
            if level.unit_count:
                assert level.geometry_count == level.unit_count, (
                    f"{level.level}: {level.unit_count - level.geometry_count} "
                    "units carry no geometry"
                )

    def test_no_unit_with_geometry_lacks_a_bounding_box(
        self, session: Session, real_geography: None
    ) -> None:
        """Dissolved regions and counties are built by a different code path.

        They were the levels that silently carried no bounding box, so the map
        could not zoom to them. Asserted for every level rather than the ones
        that happened to be wrong.
        """
        missing = session.execute(
            text(
                """
                SELECT count(*)
                  FROM mars_core.geography_unit_geometry
                 WHERE geom IS NOT NULL AND bbox_min_lon IS NULL
                """
            )
        ).scalar_one()
        assert missing == 0


def national_scope_for(session: Session) -> GeographyScope:
    """The real country unit, for the real-source tests."""
    row = session.execute(
        text(
            "SELECT id, preferred_code, raw_name, path "
            "FROM mars_core.geography_unit WHERE level='country'"
        )
    ).first()
    assert row is not None, "no country unit was imported"
    return GeographyScope(
        geography_unit_id=row[0],
        preferred_code=row[1],
        level="country",
        name=row[2],
        path=row[3],
    )


class TestRealGeographyOverHttp:
    """Uganda, rendered from the API a browser actually calls."""

    def test_uganda_districts_are_served_over_http(
        self, map_client, real_geography: None, session: Session
    ) -> None:
        response = map_client(principal(national_scope_for(session))).get(
            "/api/v1/geography/map/features?level=district&limit=400"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["features"]) == 146
        assert body["mars"]["geometry_resolution"] == "simplified"

    def test_the_served_payload_stays_within_the_measured_budget(
        self, map_client, real_geography: None, session: Session
    ) -> None:
        """Guards the figure the delivery decision was made on.

        Pre-simplified GeoJSON was chosen over vector tiles because the national
        layer measured 376 kB. If that grows by an order of magnitude the
        decision should be revisited, not silently inherited.
        """
        response = map_client(principal(national_scope_for(session))).get(
            "/api/v1/geography/map/features?level=district&limit=400"
        )
        assert len(response.content) < 600_000

    def test_every_served_feature_is_a_multipolygon_in_the_expected_extent(
        self, map_client, real_geography: None, session: Session
    ) -> None:
        response = map_client(principal(national_scope_for(session))).get(
            "/api/v1/geography/map/features?level=district&limit=400"
        )
        body = response.json()
        for feature in body["features"]:
            assert feature["geometry"]["type"] == "MultiPolygon"
        west, south, east, north = body["bbox"]
        assert 29.0 < west < 30.5
        assert 34.0 < east < 35.5
        assert -2.0 < south < -1.0
        assert 4.0 < north < 4.5
