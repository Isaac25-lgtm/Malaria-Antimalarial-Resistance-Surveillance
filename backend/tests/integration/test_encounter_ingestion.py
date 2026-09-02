"""The e-register ingestion pipeline against live PostgreSQL.

The claims worth proving are the ones a mock cannot:

* replaying an artefact creates **zero** duplicate encounters, because a unique
  constraint says so rather than an application check
* a stale read loses to the constraint instead of writing a second encounter
* two workers racing on the same artefact produce one batch
* a batch that fails as a whole leaves no half-loaded month behind
* **no direct identifier appears anywhere in** ``mars_core`` - proved by
  scanning every text-bearing column in the schema, not by inspecting the two
  columns we happen to remember

Every identifier, name and phone number below is invented.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.encounter import OpdEncounter, OpdEncounterTest, PatientReference
from mars.domain.enums import (
    AgeUnit,
    IdentifierType,
    ImportBatchStatus,
    MalariaTestMethod,
    MalariaTestResult,
    Sex,
    SourceRowOutcome,
)
from mars.domain.ingestion import (
    ImportBatch,
    ImportSourceRow,
    ImportStageExecution,
    ImportValidationIssue,
)
from mars.identity.encryption import FieldEncryptor
from mars.identity.linkage import LinkageTokenDeriver
from mars.identity.service import IdentityService
from mars.ingestion.encounters.pipeline import (
    EncounterIngestionPipeline,
    IngestOptions,
    NullIdentityLinker,
    VaultIdentityLinker,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

FACILITY_ID = uuid.UUID("dd000000-0000-4000-8000-000000000001")
COUNTRY_ID = uuid.UUID("dd000000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("dd000000-0000-4000-8000-000000000011")
SUBCOUNTY_ID = uuid.UUID("dd000000-0000-4000-8000-000000000012")
ORG_UNIT_ID = uuid.UUID("dd000000-0000-4000-8000-000000000020")
BOUNDARY_VERSION_ID = uuid.UUID("dd000000-0000-4000-8000-0000000000ff")

SOURCE_SYSTEM = "ereg-integration"

#: Invented, and deliberately unlike any real value, so a scan for it across
#: every column in ``mars_core`` cannot match something else by accident.
NIN = "ZZ00QQ11XX22"
SURNAME = "Qwertyville"
GIVEN_NAME = "Zephyrina"
PHONE = "0700111222"

LINK_KEY = b"ingestion-integration-linkage-key-not-real"
ENC_KEY = bytes(range(64, 96))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ingest_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def session(ingest_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=ingest_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(scope="module", autouse=True)
def reference_data(ingest_engine: Engine) -> None:
    """A facility and the geography a residence can resolve against."""
    with ingest_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-ING-0001', 'Ingestion fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        units = [
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "401", "Testville", COUNTRY_ID, 1, "UG/401"),
            (SUBCOUNTY_ID, "subcounty", "401201", "Alpha", DISTRICT_ID, 2, "UG/401/401201"),
        ]
        for unit_id, level, code, name, parent, depth, path in units:
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
                    "normalised": name.lower(),
                    "parent": parent,
                    "depth": depth,
                    "path": path,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO mars_core.organisation_unit
                    (id, unit_type, code, raw_name, normalised_name, depth, path,
                     is_active, created_at, updated_at)
                VALUES (:id, 'district_health_office', 'DHO-401', 'Testville DHO',
                        'testville dho', 0, 'DHO-401', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )
        connection.execute(
            text(
                """
                INSERT INTO mars_core.facility
                    (id, organisation_unit_id, district_geography_unit_id, code,
                     raw_name, normalised_name, facility_level, ownership,
                     coordinate_validated, is_active, is_synthetic,
                     created_at, updated_at)
                VALUES (:id, :org, :geo, 'HF-401', 'Alpha Health Centre',
                        'alpha health centre', 'hc_iii', 'government', false,
                        true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID},
        )


@pytest.fixture(autouse=True)
def clean_between_tests(ingest_engine: Engine) -> Iterator[None]:
    yield
    with ingest_engine.begin() as connection:
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
            "mars_identity.identity_identifier",
            "mars_identity.identity_record",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


# ---------------------------------------------------------------------------
# Batch construction
# ---------------------------------------------------------------------------
def envelope(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "record_type": "envelope",
        "schema_version": "1.0",
        "source_system": SOURCE_SYSTEM,
        "facility_code": "HF-401",
        "extracted_at": "2026-03-05T08:00:00Z",
        "register_opened_on": "2026-03-01",
        "register_closed_on": "2026-03-04",
    }
    payload.update(overrides)
    return payload


def encounter_row(index: int, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "record_type": "encounter",
        "source_row_id": f"row-{index:04d}",
        "serial_number": f"{index:03d}",
        "encounter_date": "2026-03-04",
        "date_source": "source_supplied",
        "sex": "F" if index % 2 else "M",
        "patient_category": "N",
        "attendance_type": "new_attendance",
        "fever_present": "yes",
        "age": {"value": 4, "unit": "years"},
        "residence": {"district": "Testville", "subcounty": "Alpha", "village": "Kanyanya"},
        "tests": [{"method": "rdt", "result": "positive"}],
        "diagnoses": ["Malaria, uncomplicated"],
        "prescriptions": [
            {
                "text": "Artemether/Lumefantrine 1x2x3",
                "drug_name": "Artemether/Lumefantrine",
                "units_per_dose": 1,
                "doses_per_day": 2,
                "days": 3,
            }
        ],
    }
    payload.update(overrides)
    return payload


def with_identity(payload: dict[str, object], suffix: str = "") -> dict[str, object]:
    return {
        **payload,
        "identity": {
            "identifier_type": "national_id",
            "identifier_value": f"{NIN}{suffix}",
            "surname": SURNAME,
            "given_name": GIVEN_NAME,
            "phone_contact": PHONE,
        },
    }


def write_artefact(
    path: Path, rows: list[dict[str, object]], *, env: dict[str, object] | None = None
) -> Path:
    head = env if env is not None else envelope(row_count=len(rows))
    head.setdefault("row_count", len(rows))
    lines = [json.dumps(head)] + [json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    """Five valid rows, three of which carry the same patient's identifier."""
    rows = [
        with_identity(encounter_row(1)),
        with_identity(encounter_row(2)),
        with_identity(encounter_row(3)),
        encounter_row(4),
        encounter_row(5),
    ]
    return write_artefact(tmp_path / "golden.jsonl", rows)


def identity_service(session: Session) -> IdentityService:
    return IdentityService(
        session,
        LinkageTokenDeriver(active_key=LINK_KEY, active_version="v1"),
        FieldEncryptor(active_key=ENC_KEY, active_version="v1"),
    )


def run(
    session: Session, artefact: Path, options: IngestOptions | None = None, *, identity: bool = True
):
    linker = (
        VaultIdentityLinker(identity_service(session), uuid.uuid4)
        if identity
        else NullIdentityLinker()
    )
    pipeline = EncounterIngestionPipeline(session, identity_linker=linker)
    report = pipeline.run(artefact, options or IngestOptions())
    session.commit()
    return report


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestAGoldenBatchLoads:
    def test_every_valid_row_becomes_a_canonical_encounter(
        self, session: Session, golden: Path
    ) -> None:
        report = run(session, golden)

        assert report.status is ImportBatchStatus.COMPLETED
        assert report.rows_received == 5
        assert report.rows_loaded == 5
        assert report.rows_quarantined == 0
        assert report.error_count == 0

        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 5

    def test_the_encounter_carries_what_the_row_said(self, session: Session, golden: Path) -> None:
        run(session, golden)
        encounter = session.execute(
            select(OpdEncounter).where(OpdEncounter.source_row_reference == "row-0001")
        ).scalar_one()

        assert encounter.facility_id == FACILITY_ID
        assert encounter.sex is Sex.FEMALE
        assert encounter.age_value == 4
        assert encounter.age_unit is AgeUnit.YEARS
        assert encounter.residence_district_id == DISTRICT_ID
        assert encounter.residence_subcounty_id == SUBCOUNTY_ID
        assert encounter.residence_village_raw == "Kanyanya"
        assert [d.diagnosis_raw for d in encounter.diagnoses] == ["Malaria, uncomplicated"]
        assert encounter.tests[0].method is MalariaTestMethod.RDT
        assert encounter.tests[0].result is MalariaTestResult.POSITIVE

    def test_a_prescription_derives_total_units_only_when_all_three_factors_are_present(
        self, session: Session, golden: Path
    ) -> None:
        run(session, golden)
        encounter = session.execute(
            select(OpdEncounter).where(OpdEncounter.source_row_reference == "row-0001")
        ).scalar_one()
        assert float(encounter.prescriptions[0].total_units or 0) == pytest.approx(6.0)

    def test_the_stage_executions_are_recorded(self, session: Session, golden: Path) -> None:
        """A run that slows down is slow in one stage; an aggregate hides which."""
        run(session, golden)
        stages = session.execute(select(ImportStageExecution)).scalars().all()
        assert {stage.stage.value for stage in stages} == {"validate", "write_canonical"}
        for stage in stages:
            assert stage.finished_at is not None
            assert stage.rows_in == 5

    def test_the_batch_counters_match_the_report_exactly(
        self, session: Session, golden: Path
    ) -> None:
        report = run(session, golden)
        batch = session.get(ImportBatch, report.batch_id)
        assert batch is not None
        assert (batch.rows_received, batch.rows_loaded, batch.rows_quarantined) == (5, 5, 0)
        assert batch.rows_linked == 3
        assert batch.rows_unlinked == 2
        assert batch.completed_at is not None


class TestLinkageWithoutDisclosure:
    def test_rows_sharing_an_identifier_share_one_patient_reference(
        self, session: Session, golden: Path
    ) -> None:
        """Three rows, one person. Getting this wrong makes every visit look
        like a new patient and destroys re-attendance analysis."""
        run(session, golden)
        references = (
            session.execute(
                select(OpdEncounter.patient_reference_id).where(
                    OpdEncounter.patient_reference_id.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        assert len(references) == 3
        assert len(set(references)) == 1

    def test_rows_without_an_identifier_load_unlinked_rather_than_inventing_a_person(
        self, session: Session, golden: Path
    ) -> None:
        run(session, golden)
        unlinked = session.execute(
            select(func.count())
            .select_from(OpdEncounter)
            .where(OpdEncounter.patient_reference_id.is_(None))
        ).scalar_one()
        assert unlinked == 2

    def test_the_patient_reference_row_exists_and_holds_nothing_identifying(
        self, session: Session, golden: Path
    ) -> None:
        run(session, golden)
        reference = session.execute(select(PatientReference)).scalars().one()
        assert reference.linkage_token_id is None


class TestNoIdentifierReachesTheCoreSchema:
    def test_no_text_column_anywhere_in_mars_core_contains_an_identifier(
        self, session: Session, golden: Path, ingest_engine: Engine
    ) -> None:
        """Scanned exhaustively rather than by checking the columns we remember.

        A leak that matters is one nobody thought to look for, so the test asks
        the catalogue which columns can hold text and checks all of them.
        """
        run(session, golden)

        with ingest_engine.connect() as connection:
            columns = connection.execute(
                text(
                    """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'mars_core'
                      AND data_type IN ('text','character varying','character','jsonb','json')
                    ORDER BY table_name, column_name
                    """
                )
            ).all()
            assert columns, "the scan found no columns; it would pass vacuously"

            leaks: list[str] = []
            for table_name, column_name, _ in columns:
                for needle in (NIN, SURNAME, GIVEN_NAME, PHONE):
                    found = connection.execute(
                        text(
                            f'SELECT count(*) FROM mars_core."{table_name}" '
                            f'WHERE "{column_name}"::text ILIKE :needle'
                        ),
                        {"needle": f"%{needle}%"},
                    ).scalar_one()
                    if found:
                        leaks.append(f"{table_name}.{column_name} ({needle})")
        assert not leaks, f"direct identifiers found in mars_core: {leaks}"

    def test_the_quarantined_payload_has_the_identity_object_removed_not_masked(
        self, session: Session, tmp_path: Path
    ) -> None:
        """A masked identifier is still an identifier, and this table is read by
        everyone debugging an import."""
        artefact = write_artefact(
            tmp_path / "bad.jsonl", [with_identity(encounter_row(1, sex="NOT-A-CODE"))]
        )
        run(session, artefact)

        row = session.execute(select(ImportSourceRow)).scalars().one()
        assert row.outcome is SourceRowOutcome.QUARANTINED
        assert row.payload_redacted is not None
        assert "identity" not in row.payload_redacted
        assert NIN not in json.dumps(row.payload_redacted)

    def test_the_vault_holds_the_identity_and_holds_it_encrypted(
        self, session: Session, golden: Path, ingest_engine: Engine
    ) -> None:
        run(session, golden)
        with ingest_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT count(*) FROM mars_identity.identity_record")
            ).scalar_one()
            assert stored == 1

            blob = connection.execute(
                text("SELECT surname_encrypted FROM mars_identity.identity_record")
            ).scalar_one()
            assert SURNAME.encode() not in bytes(blob)

    def test_the_identifier_is_still_resolvable_by_someone_who_already_holds_it(
        self, session: Session, golden: Path
    ) -> None:
        """Linkage must actually work, or the previous tests pass by storing
        nothing at all."""
        run(session, golden)
        reference = identity_service(session).find_reference(IdentifierType.NATIONAL_ID, NIN)
        assert reference is not None
        assert (
            session.execute(
                select(func.count())
                .select_from(OpdEncounter)
                .where(OpdEncounter.patient_reference_id == reference)
            ).scalar_one()
            == 3
        )


class TestInvalidRowsAreQuarantinedWithActionableIssues:
    def test_a_bad_row_is_quarantined_and_the_good_rows_still_load(
        self, session: Session, tmp_path: Path
    ) -> None:
        artefact = write_artefact(
            tmp_path / "mixed.jsonl",
            [
                encounter_row(1),
                encounter_row(2, sex="NOT-A-CODE"),
                encounter_row(3, age={"value": 3}),
                encounter_row(4, tests=[{"method": "not_done", "result": "positive"}]),
                encounter_row(5),
            ],
        )
        report = run(session, artefact)

        assert report.status is ImportBatchStatus.PARTIALLY_COMPLETED
        assert report.rows_loaded == 2
        assert report.rows_quarantined == 3
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 2

    def test_each_issue_names_a_code_a_field_and_a_message_safe_to_display(
        self, session: Session, tmp_path: Path
    ) -> None:
        artefact = write_artefact(tmp_path / "bad.jsonl", [encounter_row(1, sex="NOT-A-CODE")])
        run(session, artefact)

        issue = session.execute(select(ImportValidationIssue)).scalars().one()
        assert issue.code == "unrecognised_code"
        assert issue.field_path == "sex"
        assert issue.context is not None
        assert "female" in issue.context["accepted"]
        assert "NOT-A-CODE" not in issue.message

    def test_a_batch_of_only_bad_rows_is_quarantined_not_completed(
        self, session: Session, tmp_path: Path
    ) -> None:
        artefact = write_artefact(
            tmp_path / "all-bad.jsonl", [encounter_row(1, encounter_date=None)]
        )
        report = run(session, artefact)
        assert report.status is ImportBatchStatus.QUARANTINED
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0

    def test_a_duplicate_source_row_id_within_one_artefact_is_quarantined(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Two rows with one id are indistinguishable to every later replay."""
        artefact = write_artefact(
            tmp_path / "dupe.jsonl", [encounter_row(1), encounter_row(1, serial_number="002")]
        )
        report = run(session, artefact)
        assert report.issue_codes.get("duplicate_source_row_id") == 1
        assert report.rows_loaded == 1
        assert report.rows_quarantined == 1


class TestABatchCanFailAsAWhole:
    def test_an_unsupported_schema_version_fails_the_batch_and_loads_nothing(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Guessing a mapping is how a field lands silently in the wrong column."""
        artefact = write_artefact(
            tmp_path / "future.jsonl",
            [encounter_row(1)],
            env=envelope(schema_version="9.9", row_count=1),
        )
        report = run(session, artefact)

        assert report.status is ImportBatchStatus.FAILED
        assert "9.9" in (report.failure_reason or "")
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0
        codes = session.execute(select(ImportValidationIssue.code)).scalars().all()
        assert "unsupported_schema_version" in codes

    def test_an_unresolvable_facility_fails_the_batch(
        self, session: Session, tmp_path: Path
    ) -> None:
        """A month of attendance must not be attached to a guessed facility."""
        artefact = write_artefact(
            tmp_path / "nofac.jsonl",
            [encounter_row(1)],
            env=envelope(facility_code="HF-NOT-REAL", row_count=1),
        )
        report = run(session, artefact)
        assert report.status is ImportBatchStatus.FAILED
        assert "HF-NOT-REAL" in (report.failure_reason or "")
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0

    def test_a_truncated_artefact_fails_rather_than_loading_a_short_month(
        self, session: Session, tmp_path: Path
    ) -> None:
        """A silently short import looks exactly like a quiet week."""
        artefact = write_artefact(
            tmp_path / "short.jsonl",
            [encounter_row(1), encounter_row(2)],
            env=envelope(row_count=40),
        )
        report = run(session, artefact)

        assert report.status is ImportBatchStatus.FAILED
        assert "40" in (report.failure_reason or "")
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0

    def test_a_malformed_line_fails_the_batch_and_names_the_line(
        self, session: Session, tmp_path: Path
    ) -> None:
        artefact = tmp_path / "broken.jsonl"
        artefact.write_text(
            json.dumps(envelope(row_count=2))
            + "\n"
            + json.dumps(encounter_row(1))
            + "\n{not json}\n",
            encoding="utf-8",
        )
        report = run(session, artefact)
        assert report.status is ImportBatchStatus.FAILED
        assert "line 3" in (report.failure_reason or "")
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0

    def test_an_unreadable_envelope_reports_without_recording_a_batch(
        self, session: Session, tmp_path: Path
    ) -> None:
        """No envelope means no batch identity, so there is nothing to record
        against - reported, not stored."""
        artefact = tmp_path / "empty.jsonl"
        artefact.write_text("", encoding="utf-8")
        report = run(session, artefact)

        assert report.status is ImportBatchStatus.FAILED
        assert report.batch_id is None
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 0


class TestReplayingCreatesNoDuplicates:
    def test_the_same_artefact_loaded_twice_produces_one_batch_and_one_set_of_encounters(
        self, session: Session, golden: Path
    ) -> None:
        first = run(session, golden)
        second = run(session, golden)

        assert second.batch_id == first.batch_id
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 5

    def test_a_re_send_reports_the_previous_outcome_rather_than_reprocessing(
        self, session: Session, golden: Path
    ) -> None:
        run(session, golden)
        second = run(session, golden)
        assert second.status is ImportBatchStatus.COMPLETED
        assert second.rows_loaded == 5
        assert second.rows_received == 5

    def test_the_same_content_under_a_different_filename_is_the_same_batch(
        self, session: Session, golden: Path, tmp_path: Path
    ) -> None:
        """The batch's identity is its content, not its name."""
        renamed = tmp_path / "resent-on-monday.jsonl"
        renamed.write_bytes(golden.read_bytes())

        run(session, golden)
        run(session, renamed)
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 1

    def test_resume_reprocesses_and_still_creates_no_duplicates(
        self, session: Session, golden: Path
    ) -> None:
        run(session, golden)
        report = run(session, golden, IngestOptions(resume=True))

        assert report.rows_unchanged == 5
        assert report.rows_loaded == 0
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 5

    def test_an_interrupted_batch_is_finished_by_the_next_run(
        self, session: Session, tmp_path: Path, ingest_engine: Engine
    ) -> None:
        """A crash between rows leaves a non-terminal batch. Re-running it must
        load the rest without touching what already loaded."""
        artefact = write_artefact(
            tmp_path / "interrupted.jsonl", [encounter_row(i) for i in range(1, 6)]
        )
        run(session, artefact)

        # Simulate the crash: drop two encounters and reopen the batch, exactly
        # as an interrupted worker would have left it.
        with ingest_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM mars_core.opd_encounter "
                    "WHERE source_row_reference IN ('row-0004','row-0005')"
                )
            )
            connection.execute(
                text(
                    "UPDATE mars_core.import_batch "
                    "SET import_status = 'loading', completed_at = NULL"
                )
            )
        session.expire_all()

        report = run(session, artefact)
        assert report.rows_loaded == 2
        assert report.rows_unchanged == 3
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 5

    def test_a_revised_row_updates_rather_than_duplicating(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Same source row id, different content: one encounter, corrected."""
        first = write_artefact(tmp_path / "v1.jsonl", [encounter_row(1, fever_present="yes")])
        run(session, first)

        second = write_artefact(tmp_path / "v2.jsonl", [encounter_row(1, fever_present="no")])
        report = run(session, second)

        assert report.rows_updated == 1
        assert report.rows_loaded == 0
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 1
        encounter = session.execute(select(OpdEncounter)).scalars().one()
        session.refresh(encounter)
        assert encounter.fever_present.value == "no"


class TestTheConstraintDecidesNotTheApplication:
    def test_a_stale_read_loses_to_the_unique_constraint(
        self, session: Session, tmp_path: Path, ingest_engine: Engine
    ) -> None:
        """The check-then-act window is real: a second worker's lookup can miss
        a row another worker has just committed. The insert must then be decided
        by the database rather than producing a second encounter.

        The lookup is blinded exactly once, which is what a stale read is. The
        pipeline's recovery read is left intact - a recovery read that could
        disagree with the lookup would turn an absorbed conflict into a crash.
        """
        artefact = write_artefact(tmp_path / "race.jsonl", [encounter_row(1)])
        run(session, artefact)

        class StaleReadPipeline(EncounterIngestionPipeline):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                self.blinded = False

            def _existing_encounter(self, batch, source_row_id):  # type: ignore[no-untyped-def]
                if not self.blinded:
                    self.blinded = True
                    return None
                return super()._existing_encounter(batch, source_row_id)

        factory = sessionmaker(bind=ingest_engine, expire_on_commit=False, future=True)
        with factory() as other:
            # The first run's batch is terminal; reopening it puts the pipeline
            # back on the loading path, which is where the race lives.
            other.execute(text("UPDATE mars_core.import_batch SET import_status = 'loading'"))
            other.commit()

            pipeline = StaleReadPipeline(other, identity_linker=NullIdentityLinker())
            report = pipeline.run(artefact, IngestOptions(resume=True))
            other.commit()

        assert pipeline.blinded, "the lookup was never blinded; the race was not exercised"
        assert report.rows_unchanged == 1
        assert report.rows_loaded == 0
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 1

    def test_two_workers_on_the_same_artefact_produce_one_batch(
        self, golden: Path, ingest_engine: Engine, session: Session
    ) -> None:
        """Two operators uploading the same file at the same moment is exactly
        when a check-then-act produces two batches and twice the encounters."""
        factory = sessionmaker(bind=ingest_engine, expire_on_commit=False, future=True)

        def worker() -> None:
            with factory() as db:
                pipeline = EncounterIngestionPipeline(db, identity_linker=NullIdentityLinker())
                pipeline.run(golden, IngestOptions())
                db.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(worker), pool.submit(worker)]:
                future.result(timeout=120)

        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 5


class TestDryRunAndValidateOnly:
    def test_a_dry_run_writes_nothing_at_all(self, session: Session, golden: Path) -> None:
        report = run(session, golden, IngestOptions(dry_run=True))

        assert report.rows_received == 5
        assert report.batch_id is None
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0

    def test_a_dry_run_still_reports_what_is_wrong(self, session: Session, tmp_path: Path) -> None:
        artefact = write_artefact(
            tmp_path / "check.jsonl", [encounter_row(1), encounter_row(2, sex="NOT-A-CODE")]
        )
        report = run(session, artefact, IngestOptions(dry_run=True))

        assert report.rows_quarantined == 1
        assert report.issue_codes.get("unrecognised_code") == 1
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 0

    def test_validate_only_records_the_issues_but_no_encounters(
        self, session: Session, tmp_path: Path
    ) -> None:
        """So a producer can be sent an actionable list before a load."""
        artefact = write_artefact(
            tmp_path / "check.jsonl", [encounter_row(1), encounter_row(2, sex="NOT-A-CODE")]
        )
        report = run(session, artefact, IngestOptions(validate_only=True))

        assert report.rows_quarantined == 1
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0
        assert (
            session.execute(select(func.count()).select_from(ImportValidationIssue)).scalar_one()
            == 1
        )

    def test_a_validated_batch_can_then_be_loaded(self, session: Session, golden: Path) -> None:
        run(session, golden, IngestOptions(validate_only=True))
        report = run(session, golden)

        assert report.rows_loaded == 5
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 1


class TestGeographyIsResolvedOrLeftVisiblyUnresolved:
    def test_an_unknown_district_is_recorded_as_unresolved_rather_than_guessed(
        self, session: Session, tmp_path: Path
    ) -> None:
        """A wrong district is worse than an unresolved one: an unresolved
        residence is visible, a wrong one is not."""
        artefact = write_artefact(
            tmp_path / "geo.jsonl",
            [encounter_row(1, residence={"district": "Nowhere At All", "village": "X"})],
        )
        report = run(session, artefact)

        encounter = session.execute(select(OpdEncounter)).scalars().one()
        assert encounter.residence_district_id is None
        assert encounter.residence_unresolved_raw is not None
        assert "Nowhere At All" in encounter.residence_unresolved_raw
        assert report.unresolved_geography == 1

    def test_a_row_with_no_residence_is_not_counted_as_unresolved(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Not recorded is not the same as not resolvable."""
        artefact = write_artefact(tmp_path / "noresidence.jsonl", [encounter_row(1, residence={})])
        report = run(session, artefact)

        assert report.unresolved_geography == 0
        encounter = session.execute(select(OpdEncounter)).scalars().one()
        assert encounter.residence_unresolved_raw is None


class TestIdentityUnavailable:
    def test_loading_without_the_vault_records_every_row_unlinked(
        self, session: Session, golden: Path
    ) -> None:
        """Honest rather than convenient: without the vault MARS cannot say two
        encounters belong to one person."""
        report = run(session, golden, identity=False)

        assert report.rows_loaded == 5
        assert report.rows_linked == 0
        assert report.rows_unlinked == 5
        assert (
            session.execute(
                select(func.count())
                .select_from(OpdEncounter)
                .where(OpdEncounter.patient_reference_id.is_not(None))
            ).scalar_one()
            == 0
        )

    def test_nothing_reaches_the_vault_when_it_is_not_used(
        self, session: Session, golden: Path, ingest_engine: Engine
    ) -> None:
        run(session, golden, identity=False)
        with ingest_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM mars_identity.identity_record")
                ).scalar_one()
                == 0
            )


class TestTheDatabaseStillEnforcesTheEncounterInvariants:
    def test_the_pipeline_cannot_write_a_result_without_a_test(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The validator refuses it, and so would the database. Both, because a
        second producer path must not be able to bypass the first."""
        artefact = write_artefact(
            tmp_path / "phantom.jsonl",
            [encounter_row(1, tests=[{"method": "not_done", "result": "positive"}])],
        )
        run(session, artefact)

        assert session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(OpdEncounterTest)).scalar_one() == 0
