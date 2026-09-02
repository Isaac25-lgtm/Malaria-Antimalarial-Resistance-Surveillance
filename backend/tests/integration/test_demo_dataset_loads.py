"""The demo dataset loads through the real pipeline, against live PostgreSQL.

This is the test that makes the demo worth having. A generator can produce
anything; what matters is that what it produces is accepted by the same
ingestion pipeline, the same validator and the same database constraints that
real data meets. A demo dataset that only loads through a special path proves
nothing about the system it is demonstrating - and quietly drifts into a shape
real data can never take.

So: generate, register the fictional facilities, load every artefact through
``EncounterIngestionPipeline``, and then assert the storylines are visible in
``mars_core`` - not in the JSON the generator wrote.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.demo.generator import (
    FACILITY_CODE_PREFIX,
    IDENTIFIER_PREFIX,
    SOURCE_SYSTEM,
    DemoDatasetGenerator,
    DemoDistrict,
    GeneratorOptions,
)
from mars.demo.storylines import StorylineKey
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    FacilityLevel,
    FacilityOwnership,
    ImportBatchStatus,
    MalariaTestMethod,
    MalariaTestResult,
)
from mars.domain.geography import GeographyUnit
from mars.domain.ingestion import ImportBatch, ImportValidationIssue
from mars.domain.organisation import Facility, OrganisationUnit
from mars.identity.encryption import FieldEncryptor
from mars.identity.linkage import LinkageTokenDeriver
from mars.identity.service import IdentityService
from mars.ingestion.encounters.pipeline import (
    EncounterIngestionPipeline,
    IngestOptions,
    VaultIdentityLinker,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("ee000000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("ee000000-0000-4000-8000-000000000010")
ORG_UNIT_ID = uuid.UUID("ee000000-0000-4000-8000-000000000020")

#: Fixture ids derived from a fixed namespace: deterministic across runs and
#: impossible to malform, which hand-assembled hex strings are not.
_NAMESPACE = uuid.UUID("ee000000-0000-4000-8000-000000000000")


def _fixture_id(*parts: object) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, ":".join(str(part) for part in parts))


LINK_KEY = b"demo-dataset-integration-linkage-key-fake"
ENC_KEY = bytes(range(96, 128))

#: A small dataset: enough to carry every storyline, small enough that the whole
#: thing loads inside a test rather than only inside a demo.
DISTRICTS = [
    DemoDistrict("801", "Alphaland", StorylineKey.REPEAT_POSITIVE_CLUSTER, ("Aone", "Atwo")),
    DemoDistrict("802", "Betaland", StorylineKey.TESTING_ANOMALY_STOCKOUT, ("Bone", "Btwo")),
    DemoDistrict("803", "Gammaland", StorylineKey.COMPLETENESS_ARTEFACT, ("Gone", "Gtwo")),
    DemoDistrict("804", "Deltaland", StorylineKey.SPATIAL_CLUSTER, ("Done", "Dtwo")),
    DemoDistrict("805", "Epsiland", StorylineKey.SEASONAL_CONTROL, ("Eone", "Etwo")),
]

OPTIONS = GeneratorOptions(
    seed=90210,
    period_start=date(2025, 11, 1),
    period_end=date(2025, 12, 31),
    facilities_per_district=2,
    daily_attendance=3,
)


@pytest.fixture(scope="module")
def demo_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module")
def geography(demo_engine: Engine) -> None:
    """Districts and subcounties for the demo to attach to.

    Synthetic here, because this test is about the demo pipeline and not about
    the geography import - but structurally identical to what the importer
    builds, so the demo's district resolution is exercised for real.
    """
    with demo_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-DEMO-0001', 'Demo fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        _insert_unit(connection, COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG")

        for district in DISTRICTS:
            district_id = _fixture_id("district", district.code)
            path = f"UG/{district.code}"
            _insert_unit(
                connection,
                district_id,
                "district",
                district.code,
                district.name,
                COUNTRY_ID,
                1,
                path,
            )
            for sub_index, subcounty in enumerate(district.subcounties):
                _insert_unit(
                    connection,
                    _fixture_id("subcounty", district.code, subcounty),
                    "subcounty",
                    f"{district.code}{sub_index + 1:02d}",
                    subcounty,
                    district_id,
                    2,
                    f"{path}/{district.code}{sub_index + 1:02d}",
                )

        connection.execute(
            text(
                """
                INSERT INTO mars_core.organisation_unit
                    (id, unit_type, code, raw_name, normalised_name, depth, path,
                     is_active, created_at, updated_at)
                VALUES (:id, 'district_health_office', 'DEMO-ORG', 'MARS Demo Organisation',
                        'mars demo organisation', 0, 'DEMO-ORG', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )


def _insert_unit(
    connection,
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
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": unit_id,
            "version": BOUNDARY_VERSION_ID,
            "level": level,
            "code": code,
            "name": name,
            "normalised": " ".join(name.lower().split()),
            "parent": parent,
            "depth": depth,
            "path": path,
        },
    )


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory):
    return DemoDatasetGenerator(list(DISTRICTS), OPTIONS).generate(tmp_path_factory.mktemp("demo"))


@pytest.fixture(scope="module")
def loaded(demo_engine: Engine, geography: None, dataset) -> dict[str, int]:
    """Register the facilities, then load every artefact through the pipeline.

    Module-scoped: this is the expensive step and every assertion below reads
    the same loaded state.
    """
    factory = sessionmaker(bind=demo_engine, expire_on_commit=False, future=True)

    with factory() as session:
        _register_facilities(session, dataset)
        session.commit()

    totals = Counter[str]()
    with factory() as session:
        linker = VaultIdentityLinker(
            IdentityService(
                session,
                LinkageTokenDeriver(active_key=LINK_KEY, active_version="v1"),
                FieldEncryptor(active_key=ENC_KEY, active_version="v1"),
            ),
            uuid.uuid4,
        )
        pipeline = EncounterIngestionPipeline(session, identity_linker=linker)
        for artefact in dataset.artefacts:
            report = pipeline.run(artefact, IngestOptions(initiated_by="demo-loader"))
            totals["loaded"] += report.rows_loaded
            totals["quarantined"] += report.rows_quarantined
            totals["linked"] += report.rows_linked
            totals[report.status.value] += 1
        session.commit()

    yield dict(totals)

    with demo_engine.begin() as connection:
        for table in (
            "mars_core.import_validation_issue",
            "mars_core.import_source_row",
            "mars_core.import_stage_execution",
            "mars_core.import_batch",
            "mars_core.opd_encounter_referral",
            "mars_core.opd_encounter_test",
            "mars_core.opd_encounter_prescription",
            "mars_core.opd_encounter_diagnosis",
            "mars_core.opd_encounter",
            "mars_core.patient_reference",
            "mars_core.facility",
            "mars_identity.identity_identifier",
            "mars_identity.identity_record",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


def _register_facilities(session: Session, dataset) -> None:
    entries = json.loads(dataset.facilities_path.read_text(encoding="utf-8"))
    organisation = session.get(OrganisationUnit, ORG_UNIT_ID)
    assert organisation is not None

    for entry in entries:
        district = (
            session.execute(
                select(GeographyUnit).where(GeographyUnit.preferred_code == entry["district_code"])
            )
            .scalars()
            .first()
        )
        assert district is not None, entry["district_code"]

        session.add(
            Facility(
                code=entry["code"],
                raw_name=entry["name"],
                normalised_name=" ".join(str(entry["name"]).lower().split()),
                facility_level=FacilityLevel(entry["facility_level"]),
                ownership=FacilityOwnership(entry["ownership"]),
                organisation_unit_id=organisation.id,
                district_geography_unit_id=district.id,
                is_active=True,
                is_synthetic=True,
                coordinate_validated=False,
                source_system=SOURCE_SYSTEM,
            )
        )
    session.flush()


@pytest.fixture
def session(demo_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=demo_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestTheDemoLoadsThroughTheRealPipeline:
    def test_every_artefact_reaches_a_terminal_status(self, loaded: dict[str, int]) -> None:
        """No batch may fail. A demo that half-loads is a demo that misleads."""
        assert loaded.get(ImportBatchStatus.FAILED.value, 0) == 0
        assert (
            loaded.get(ImportBatchStatus.COMPLETED.value, 0)
            + loaded.get(ImportBatchStatus.PARTIALLY_COMPLETED.value, 0)
            > 0
        )

    def test_encounters_are_in_the_canonical_table(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        stored = session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one()
        assert stored == loaded["loaded"]
        assert stored > 500, "the demo is too small to walk a journey through"

    def test_the_deliberately_invalid_rows_are_quarantined_not_loaded(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        """The quarantine screen needs something in it, and the demo is where
        an operator first sees one."""
        assert loaded["quarantined"] > 0
        codes = session.execute(select(ImportValidationIssue.code).distinct()).scalars().all()
        assert "unrecognised_code" in codes

    def test_every_batch_resolved_its_facility(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        unresolved = session.execute(
            select(func.count()).select_from(ImportBatch).where(ImportBatch.facility_id.is_(None))
        ).scalar_one()
        assert unresolved == 0

    def test_reloading_the_whole_dataset_creates_no_duplicates(
        self, loaded: dict[str, int], demo_engine: Engine, dataset
    ) -> None:
        before = _count(demo_engine, "mars_core.opd_encounter")
        factory = sessionmaker(bind=demo_engine, expire_on_commit=False, future=True)
        with factory() as session:
            pipeline = EncounterIngestionPipeline(
                session,
                identity_linker=VaultIdentityLinker(
                    IdentityService(
                        session,
                        LinkageTokenDeriver(active_key=LINK_KEY, active_version="v1"),
                        FieldEncryptor(active_key=ENC_KEY, active_version="v1"),
                    ),
                    uuid.uuid4,
                ),
            )
            for artefact in dataset.artefacts:
                pipeline.run(artefact, IngestOptions())
            session.commit()

        assert _count(demo_engine, "mars_core.opd_encounter") == before
        assert _count(demo_engine, "mars_core.import_batch") == len(dataset.artefacts)


class TestTheStorylinesSurviveIngestion:
    def test_the_repeat_positive_district_has_patients_with_several_positives(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        counts = self._positives_per_patient(session, "801")
        assert sum(1 for value in counts.values() if value >= 2) >= 8

    def test_the_control_district_has_none(self, loaded: dict[str, int], session: Session) -> None:
        """A detector measured against a dirty control measures nothing."""
        counts = self._positives_per_patient(session, "805")
        assert not [value for value in counts.values() if value >= 2]

    def test_the_stockout_district_shows_fewer_tests_in_the_window(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        before = self._tested_share(session, "802", date(2025, 11, 1), date(2025, 11, 30))
        during = self._tested_share(session, "802", date(2025, 12, 1), date(2025, 12, 31))
        assert before > during, f"testing did not fall: {before:.2f} -> {during:.2f}"

    def test_the_completeness_district_has_facilities_that_start_late(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        first_dates = session.execute(
            select(Facility.code, func.min(OpdEncounter.encounter_date))
            .join(OpdEncounter, OpdEncounter.facility_id == Facility.id)
            .join(
                GeographyUnit,
                GeographyUnit.id == Facility.district_geography_unit_id,
            )
            .where(GeographyUnit.preferred_code == "803")
            .group_by(Facility.code)
        ).all()
        assert len(first_dates) == 2
        starts = sorted(value for _code, value in first_dates)
        assert starts[0] < starts[1], "no facility started late; the storyline is absent"

    def test_the_spatial_cluster_resolved_to_subcounties(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        """Administrative aggregation, never household points."""
        resolved = session.execute(
            select(func.count())
            .select_from(OpdEncounter)
            .join(Facility, Facility.id == OpdEncounter.facility_id)
            .join(GeographyUnit, GeographyUnit.id == Facility.district_geography_unit_id)
            .where(
                GeographyUnit.preferred_code == "804",
                OpdEncounter.residence_subcounty_id.is_not(None),
            )
        ).scalar_one()
        assert resolved > 0

    @staticmethod
    def _positives_per_patient(session: Session, district_code: str) -> Counter[uuid.UUID]:
        rows = (
            session.execute(
                select(OpdEncounter.patient_reference_id)
                .join(Facility, Facility.id == OpdEncounter.facility_id)
                .join(GeographyUnit, GeographyUnit.id == Facility.district_geography_unit_id)
                .join(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
                .where(
                    GeographyUnit.preferred_code == district_code,
                    OpdEncounter.patient_reference_id.is_not(None),
                    OpdEncounterTest.result == MalariaTestResult.POSITIVE,
                )
            )
            .scalars()
            .all()
        )
        return Counter(rows)

    @staticmethod
    def _tested_share(session: Session, district_code: str, start: date, end: date) -> float:
        total, tested = session.execute(
            select(
                func.count(func.distinct(OpdEncounter.id)),
                func.count(func.distinct(OpdEncounter.id)).filter(
                    OpdEncounterTest.method != MalariaTestMethod.NOT_DONE
                ),
            )
            .select_from(OpdEncounter)
            .join(Facility, Facility.id == OpdEncounter.facility_id)
            .join(GeographyUnit, GeographyUnit.id == Facility.district_geography_unit_id)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                GeographyUnit.preferred_code == district_code,
                OpdEncounter.encounter_date.between(start, end),
            )
        ).one()
        return (tested / total) if total else 0.0


class TestTheDemoIsStillUnmistakablySynthetic:
    def test_every_loaded_facility_is_flagged_synthetic_and_has_no_coordinate(
        self, loaded: dict[str, int], session: Session
    ) -> None:
        facilities = (
            session.execute(select(Facility).where(Facility.code.like(f"{FACILITY_CODE_PREFIX}-%")))
            .scalars()
            .all()
        )
        assert facilities
        for facility in facilities:
            assert facility.is_synthetic is True
            assert facility.latitude is None
            assert facility.longitude is None

    def test_no_synthetic_identifier_reaches_the_core_schema(
        self, loaded: dict[str, int], demo_engine: Engine
    ) -> None:
        """The same exhaustive scan the ingestion tests run, over demo data.

        Synthetic identifiers are still identifiers as far as the boundary is
        concerned, and a demo is exactly where a shortcut gets taken.
        """
        with demo_engine.connect() as connection:
            columns = connection.execute(
                text(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'mars_core'
                      AND data_type IN ('text','character varying','character','jsonb','json')
                    ORDER BY table_name, column_name
                    """
                )
            ).all()
            assert columns

            leaks = []
            for table_name, column_name in columns:
                found = connection.execute(
                    text(
                        f'SELECT count(*) FROM mars_core."{table_name}" '
                        f'WHERE "{column_name}"::text LIKE :needle'
                    ),
                    {"needle": f"%{IDENTIFIER_PREFIX}0%"},
                ).scalar_one()
                if found:
                    leaks.append(f"{table_name}.{column_name}")
        assert not leaks, f"synthetic identifiers found in mars_core: {leaks}"

    def test_the_vault_holds_the_demo_patients(
        self, loaded: dict[str, int], demo_engine: Engine
    ) -> None:
        """Otherwise the previous test passes by storing nothing."""
        assert loaded["linked"] > 0
        assert _count(demo_engine, "mars_identity.identity_record") > 0


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
