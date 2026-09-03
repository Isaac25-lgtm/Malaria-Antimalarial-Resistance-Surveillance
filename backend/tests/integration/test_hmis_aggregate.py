"""HMIS aggregate ingestion and reconciliation, against live PostgreSQL.

The exit criterion for this phase is exact: known totals must reconcile
exactly, and deliberate differences must appear as issues. So the encounters
below are built to make every derived figure knowable by hand - a fixed number
of RDT tests, a fixed number of positives - and the submissions then either
agree with those numbers or disagree by a stated amount.

The other half is what only a real database can prove: that a blank cell stays
null through a round trip, that a corrected week does not overwrite the week a
district acted on, and that arithmetic impossibilities are refused by
constraints rather than by application code.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.aggregate import (
    AggregateObservation,
    AggregateSubmission,
    CommodityStockObservation,
    LaboratoryTestObservation,
    ReconciliationFinding,
)
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    AggregateForm,
    AggregatePeriodType,
    AggregateSubmissionStatus,
    AttendanceType,
    DateAssignmentMethod,
    FeverStatus,
    ImportBatchStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PatientCategory,
    ReconciliationStatus,
    Sex,
    StockMetric,
)
from mars.domain.ingestion import ImportBatch, ImportSourceRow, ImportValidationIssue
from mars.ingestion.aggregate.pipeline import (
    AggregateIngestionPipeline,
    AggregateIngestOptions,
)
from mars.services.reconciliation import (
    RECONCILIATION_METHOD_VERSION,
    ReconciliationService,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

FACILITY_ID = uuid.UUID("ff000000-0000-4000-8000-000000000001")
COUNTRY_ID = uuid.UUID("ff000000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("ff000000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("ff000000-0000-4000-8000-000000000020")
BOUNDARY_VERSION_ID = uuid.UUID("ff000000-0000-4000-8000-0000000000ff")

FACILITY_CODE = "HF-AGG-001"
SOURCE_SYSTEM = "hmis-integration"

#: The month under test, and what the encounters below make true of it.
MONTH_START = date(2026, 3, 1)
MONTH_END = date(2026, 3, 31)

#: Built by ``encounters``: 20 RDT tests of which 8 positive, 10 microscopy
#: tests of which 3 positive, 6 untested attendances. Every derived figure in
#: this module is one of these numbers, computed by hand.
RDT_TESTS = 20
RDT_POSITIVE = 8
MICROSCOPY_TESTS = 10
MICROSCOPY_POSITIVE = 3
UNTESTED = 6
TOTAL_ENCOUNTERS = RDT_TESTS + MICROSCOPY_TESTS + UNTESTED
TOTAL_TESTED = RDT_TESTS + MICROSCOPY_TESTS
TOTAL_POSITIVE = RDT_POSITIVE + MICROSCOPY_POSITIVE


@pytest.fixture(scope="module")
def aggregate_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(aggregate_engine: Engine) -> None:
    with aggregate_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-AGG-0001', 'Aggregate fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "901", "Aggville", COUNTRY_ID, 1, "UG/901"),
        ):
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
                VALUES (:id, 'district_health_office', 'DHO-901', 'Aggville DHO',
                        'aggville dho', 0, 'DHO-901', true, now(), now())
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
                VALUES (:id, :org, :geo, :code, 'Aggville Health Centre',
                        'aggville health centre', 'hc_iii', 'government', false,
                        true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID, "code": FACILITY_CODE},
        )


@pytest.fixture
def session(aggregate_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=aggregate_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(aggregate_engine: Engine) -> Iterator[None]:
    yield
    with aggregate_engine.begin() as connection:
        for table in (
            "mars_core.reconciliation_finding",
            "mars_core.import_validation_issue",
            "mars_core.import_source_row",
            "mars_core.import_stage_execution",
            "mars_core.aggregate_observation",
            "mars_core.commodity_stock_observation",
            "mars_core.laboratory_test_observation",
            "mars_core.aggregate_submission",
            "mars_core.opd_encounter_test",
            "mars_core.opd_encounter",
            "mars_core.import_batch",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


@pytest.fixture
def encounters(session: Session) -> None:
    """A month whose derived figures are known by hand.

    Every encounter is a new attendance, so the attendance rule has a known
    answer too.
    """
    made = 0

    def add(method: MalariaTestMethod, result: MalariaTestResult) -> None:
        nonlocal made
        made += 1
        encounter = OpdEncounter(
            facility_id=FACILITY_ID,
            encounter_date=date(2026, 3, 1 + (made % 28)),
            date_assignment_method=DateAssignmentMethod.SOURCE_SUPPLIED,
            sex=Sex.FEMALE,
            patient_category=PatientCategory.NATIONAL,
            attendance_type=AttendanceType.NEW_ATTENDANCE,
            fever_present=FeverStatus.YES,
            source_system="test",
            source_row_reference=f"agg-{made:04d}",
        )
        encounter.tests = [OpdEncounterTest(sequence=1, method=method, result=result)]
        session.add(encounter)

    for index in range(RDT_TESTS):
        add(
            MalariaTestMethod.RDT,
            MalariaTestResult.POSITIVE if index < RDT_POSITIVE else MalariaTestResult.NEGATIVE,
        )
    for index in range(MICROSCOPY_TESTS):
        add(
            MalariaTestMethod.MICROSCOPY,
            MalariaTestResult.POSITIVE
            if index < MICROSCOPY_POSITIVE
            else MalariaTestResult.NEGATIVE,
        )
    for _ in range(UNTESTED):
        add(MalariaTestMethod.NOT_DONE, MalariaTestResult.NOT_DONE)

    session.commit()


# ---------------------------------------------------------------------------
# Artefacts
# ---------------------------------------------------------------------------
def envelope(count: int) -> dict[str, object]:
    return {
        "record_type": "envelope",
        "schema_version": "1.0",
        "source_system": SOURCE_SYSTEM,
        "extracted_at": "2026-04-05T08:00:00Z",
        "submission_count": count,
    }


def monthly(**overrides: object) -> dict[str, object]:
    """A 105 return that agrees exactly with the encounters above."""
    payload: dict[str, object] = {
        "record_type": "submission",
        "form": "hmis_105",
        "facility_code": FACILITY_CODE,
        "period_start": MONTH_START.isoformat(),
        "period_end": MONTH_END.isoformat(),
        "period_label": "March",
        "reported_on": "2026-04-05",
        "observations": [
            _cell("OA01", TOTAL_ENCOUNTERS),
            _cell("EP01b", TOTAL_TESTED),
            _cell("EP01c", TOTAL_POSITIVE),
            _cell("EP01d", TOTAL_POSITIVE),
            _cell("EP01e", TOTAL_POSITIVE + 4),
        ],
        "laboratory": [
            {"test": "PS01", "done": MICROSCOPY_TESTS, "positive": MICROSCOPY_POSITIVE},
            {"test": "PS02", "done": RDT_TESTS, "positive": RDT_POSITIVE},
        ],
        "stock": [
            {"commodity": "SS34", "metric": "days_out_of_stock", "value": 0, "unit": "Tests"},
            {"commodity": "SS01", "metric": "quantity_consumed", "value": 120, "unit": "Tablet"},
        ],
    }
    payload.update(overrides)
    return payload


def _cell(element: str, value: int | None) -> dict[str, object]:
    """One cell, put in the 20-and-above band.

    The whole figure sits in one band rather than being spread: the point of
    these tests is the comparison, and spreading a total across bands would
    only be testing addition.
    """
    return {"element": element, "age_band": "years_20_plus", "sex": "female", "value": value}


def weekly(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "record_type": "submission",
        "form": "hmis_033b",
        "facility_code": FACILITY_CODE,
        "period_start": "2026-03-02",
        "period_end": "2026-03-08",
        "period_label": "Week 10",
        "observations": [
            {"element": "MA.", "value": 3},
            {"element": "M033B_MAT_TESTED_RDT", "value": 5},
            {"element": "M033B_MAT_RDT_POSITIVE", "value": 2},
        ],
        "stock": [
            {"commodity": "M033B_TRA_RDT", "metric": "stock_on_hand", "value": 240},
        ],
    }
    payload.update(overrides)
    return payload


def write(tmp_path: Path, *rows: dict[str, object], name: str = "returns.jsonl") -> Path:
    artefact = tmp_path / name
    lines = [json.dumps(envelope(len(rows)))]
    lines.extend(json.dumps(row) for row in rows)
    artefact.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artefact


def load(session: Session, artefact: Path, options: AggregateIngestOptions | None = None):
    pipeline = AggregateIngestionPipeline(session)
    report = pipeline.run(artefact, options or AggregateIngestOptions())
    session.commit()
    return report


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestSubmissionsLoad:
    def test_a_monthly_return_becomes_a_submission_with_its_blocks(
        self, session: Session, tmp_path: Path
    ) -> None:
        report = load(session, write(tmp_path, monthly()))

        assert report.submissions_loaded == 1
        assert report.submissions_quarantined == 0
        assert report.observations_loaded == 5
        assert report.laboratory_rows_loaded == 2
        assert report.stock_rows_loaded == 2

        submission = session.execute(select(AggregateSubmission)).scalars().one()
        assert submission.form is AggregateForm.HMIS_105
        assert submission.period_type is AggregatePeriodType.MONTH
        assert submission.period_label_raw == "March"
        assert submission.submission_status is AggregateSubmissionStatus.ACCEPTED

        batch = session.get(ImportBatch, report.batch_id)
        assert batch is not None
        assert batch.import_domain == "aggregate"
        assert batch.import_status is ImportBatchStatus.COMPLETED
        source = session.execute(select(ImportSourceRow)).scalars().one()
        assert source.aggregate_submission_id == submission.id
        assert source.opd_encounter_id is None

    def test_a_weekly_return_loads_with_its_own_period_type(
        self, session: Session, tmp_path: Path
    ) -> None:
        load(session, write(tmp_path, weekly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        assert submission.period_type is AggregatePeriodType.WEEK
        assert (submission.period_end - submission.period_start).days == 6

    def test_the_laboratory_block_is_stored_apart_from_the_diagnosis_block(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The laboratory counts tests, the OPD block counts patients. Merging
        them would destroy the comparison the form makes possible."""
        load(session, write(tmp_path, monthly()))
        rows = session.execute(select(LaboratoryTestObservation)).scalars().all()
        assert {row.test_code for row in rows} == {"PS01", "PS02"}
        rdt = next(row for row in rows if row.test_code == "PS02")
        assert rdt.number_done == RDT_TESTS
        assert rdt.number_positive == RDT_POSITIVE

    def test_stock_rows_keep_their_metric_and_unit(self, session: Session, tmp_path: Path) -> None:
        """A consumption figure without its unit is not a quantity."""
        load(session, write(tmp_path, monthly()))
        rows = session.execute(select(CommodityStockObservation)).scalars().all()
        consumed = next(row for row in rows if row.metric is StockMetric.QUANTITY_CONSUMED)
        assert consumed.commodity_code == "SS01"
        assert consumed.unit_of_issue == "Tablet"

    def test_an_unresolvable_facility_quarantines_the_submission(
        self, session: Session, tmp_path: Path
    ) -> None:
        report = load(session, write(tmp_path, monthly(facility_code="HF-NOT-REAL")))
        assert report.unresolved_facility == 1
        assert report.submissions_quarantined == 1
        assert (
            session.execute(select(func.count()).select_from(AggregateSubmission)).scalar_one() == 0
        )

    def test_a_truncated_artefact_fails_the_batch(self, session: Session, tmp_path: Path) -> None:
        artefact = tmp_path / "short.jsonl"
        artefact.write_text(
            json.dumps(envelope(9)) + "\n" + json.dumps(monthly()) + "\n", encoding="utf-8"
        )
        report = load(session, artefact)
        assert report.failure_reason is not None
        assert "9" in report.failure_reason
        assert (
            session.execute(select(func.count()).select_from(AggregateSubmission)).scalar_one() == 0
        )


class TestABlankCellSurvivesTheRoundTrip:
    def test_a_blank_stays_null_and_a_zero_stays_zero(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The single most important property in this module. In a total they
        look identical; they are opposite facts about whether the facility
        reported."""
        payload = monthly(observations=[_cell("EP01b", None), _cell("EP01c", 0)])
        report = load(session, write(tmp_path, payload))

        assert report.blank_cells == 1
        assert report.zero_cells == 1

        rows = {
            row.element_code: row.value
            for row in session.execute(select(AggregateObservation)).scalars().all()
        }
        assert rows["EP01b"] is None
        assert rows["EP01c"] == 0

    def test_a_blank_is_not_summed_as_zero_in_reconciliation(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        """Reporting a difference against a statement nobody made would be a
        finding about MARS, not about the facility."""
        load(session, write(tmp_path, monthly(observations=[_cell("EP01b", None)])))
        submission = session.execute(select(AggregateSubmission)).scalars().one()

        ReconciliationService(session).reconcile(submission)
        finding = (
            session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.element_code == "EP01b")
            )
            .scalars()
            .one()
        )

        assert finding.reconciliation_status is ReconciliationStatus.DERIVED_ONLY
        assert finding.reported_value is None
        assert finding.difference is None
        assert finding.detail is not None
        assert "blank is not a zero" in json.dumps(finding.detail)


class TestKnownTotalsReconcileExactly:
    def test_every_agreeing_figure_matches(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        """The exit criterion. The submission carries the numbers the
        encounters make true, and every comparison must agree exactly."""
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()

        report = ReconciliationService(session).reconcile(submission)

        assert report.encounters_in_period == TOTAL_ENCOUNTERS
        assert report.differs == 0
        # Three of the four rules have a reported cell to compare against:
        # OA01, EP01b and EP01c. OA02 has none - every fixture encounter is a
        # new attendance and the return carries no re-attendance cell - so it
        # is derived_only rather than a difference. Counting a blank as a zero
        # there would manufacture a discrepancy out of a statement nobody made.
        assert report.matched == 3, report.as_dict()
        assert report.derived_only == 1, report.as_dict()

    def test_the_derived_figures_are_the_ones_computed_by_hand(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        ReconciliationService(session).reconcile(submission)

        derived = {
            finding.element_code: finding.derived_value
            for finding in session.execute(select(ReconciliationFinding)).scalars().all()
        }
        assert derived["OA01"] == TOTAL_ENCOUNTERS
        assert derived["EP01b"] == TOTAL_TESTED
        assert derived["EP01c"] == TOTAL_POSITIVE

    def test_the_untested_attendances_are_not_in_the_tested_denominator(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        """A denominator inflated by untested attendances understates
        positivity everywhere."""
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        ReconciliationService(session).reconcile(submission)

        tested = (
            session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.element_code == "EP01b")
            )
            .scalars()
            .one()
        )
        assert tested.derived_value == TOTAL_TESTED
        assert tested.derived_value != TOTAL_ENCOUNTERS


class TestDeliberateDifferencesAppearAsIssues:
    def test_a_reported_figure_five_above_the_register_is_reported_as_differing(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        payload = monthly(
            observations=[_cell("EP01b", TOTAL_TESTED + 5), _cell("EP01c", TOTAL_POSITIVE)]
        )
        load(session, write(tmp_path, payload))
        submission = session.execute(select(AggregateSubmission)).scalars().one()

        report = ReconciliationService(session).reconcile(submission)
        assert report.differs == 1

        finding = (
            session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.element_code == "EP01b")
            )
            .scalars()
            .one()
        )
        assert finding.reconciliation_status is ReconciliationStatus.DIFFERS
        assert finding.reported_value == TOTAL_TESTED + 5
        assert finding.derived_value == TOTAL_TESTED
        assert finding.difference == 5

    def test_neither_value_is_corrected(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        """Preferring either source would hide a real data-quality problem."""
        payload = monthly(observations=[_cell("EP01b", TOTAL_TESTED + 5)])
        load(session, write(tmp_path, payload))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        ReconciliationService(session).reconcile(submission)

        observation = (
            session.execute(
                select(AggregateObservation).where(AggregateObservation.element_code == "EP01b")
            )
            .scalars()
            .one()
        )
        assert observation.value == TOTAL_TESTED + 5, "the reported figure was altered"
        assert (
            session.execute(select(func.count()).select_from(OpdEncounter)).scalar_one()
            == TOTAL_ENCOUNTERS
        ), "the encounters were altered"

    def test_a_finding_states_the_denominator_it_was_computed_from(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        """A difference of four from four encounters and one from four hundred
        deserve different attention."""
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        ReconciliationService(session).reconcile(submission)

        findings = session.execute(select(ReconciliationFinding)).scalars().all()
        assert findings
        for finding in findings:
            assert finding.derived_denominator == TOTAL_ENCOUNTERS

    def test_a_tolerance_moves_a_small_difference_out_of_differs(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        payload = monthly(observations=[_cell("EP01b", TOTAL_TESTED + 1)])
        load(session, write(tmp_path, payload))
        submission = session.execute(select(AggregateSubmission)).scalars().one()

        report = ReconciliationService(session, tolerance=2).reconcile(submission)
        assert report.differs == 0
        assert report.within_tolerance == 1

    def test_the_default_is_exact_agreement(self) -> None:
        """No supplied source defines an acceptable transcription variance, so
        MARS does not invent one."""
        from mars.services.reconciliation import DEFAULT_ABSOLUTE_TOLERANCE

        assert DEFAULT_ABSOLUTE_TOLERANCE == 0


class TestNoEncountersMeansNoComparison:
    def test_a_period_with_no_register_data_is_uncomparable_not_a_discrepancy(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Reporting a difference of everything would flood the screen with
        findings that say only 'we have no register data'."""
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()

        report = ReconciliationService(session).reconcile(submission)
        assert report.differs == 0
        assert report.uncomparable == len(
            session.execute(select(ReconciliationFinding)).scalars().all()
        )

        finding = session.execute(select(ReconciliationFinding)).scalars().first()
        assert finding is not None
        assert "completeness" in json.dumps(finding.detail)


class TestReconciliationIsRepeatable:
    def test_running_it_twice_does_not_accumulate_findings(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()

        service = ReconciliationService(session)
        service.reconcile(submission)
        first = session.execute(
            select(func.count()).select_from(ReconciliationFinding)
        ).scalar_one()
        service.reconcile(submission)
        second = session.execute(
            select(func.count()).select_from(ReconciliationFinding)
        ).scalar_one()

        assert first == second

    def test_every_finding_records_the_method_version_that_made_it(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        """So an old finding does not silently acquire a new meaning."""
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        ReconciliationService(session).reconcile(submission)

        versions = {
            finding.method_version
            for finding in session.execute(select(ReconciliationFinding)).scalars().all()
        }
        assert versions == {RECONCILIATION_METHOD_VERSION}

    def test_changed_encounter_evidence_creates_new_findings_without_rewriting_old(
        self, session: Session, tmp_path: Path, encounters: None
    ) -> None:
        load(session, write(tmp_path, monthly()))
        submission = session.execute(select(AggregateSubmission)).scalars().one()
        service = ReconciliationService(session)
        service.reconcile(submission)

        negative = (
            session.execute(
                select(OpdEncounterTest)
                .where(
                    OpdEncounterTest.method == MalariaTestMethod.RDT,
                    OpdEncounterTest.result == MalariaTestResult.NEGATIVE,
                )
                .limit(1)
            )
            .scalars()
            .one()
        )
        negative.result = MalariaTestResult.POSITIVE
        session.flush()
        service.reconcile(submission)

        confirmed = (
            session.execute(
                select(ReconciliationFinding)
                .where(ReconciliationFinding.element_code == "EP01c")
                .order_by(ReconciliationFinding.created_at)
            )
            .scalars()
            .all()
        )
        assert [finding.derived_value for finding in confirmed] == [
            TOTAL_POSITIVE,
            TOTAL_POSITIVE + 1,
        ]
        assert len({finding.input_checksum for finding in confirmed}) == 2
        assert {finding.absolute_tolerance for finding in confirmed} == {0}


class TestACorrectionDoesNotOverwrite:
    def test_a_higher_revision_supersedes_and_the_original_survives(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The district acted on the first figures; a record showing only the
        corrected number cannot explain what anyone did."""
        load(session, write(tmp_path, monthly(observations=[_cell("EP01c", 11)])))
        load(
            session,
            write(
                tmp_path,
                monthly(revision=2, observations=[_cell("EP01c", 14)]),
                name="revision2.jsonl",
            ),
        )

        submissions = (
            session.execute(select(AggregateSubmission).order_by(AggregateSubmission.revision))
            .scalars()
            .all()
        )
        assert [s.revision for s in submissions] == [1, 2]
        assert submissions[0].submission_status is AggregateSubmissionStatus.SUPERSEDED
        assert submissions[1].submission_status is AggregateSubmissionStatus.ACCEPTED
        assert submissions[1].supersedes_id == submissions[0].id

        values = sorted(
            row.value
            for row in session.execute(select(AggregateObservation)).scalars().all()
            if row.element_code == "EP01c"
        )
        assert values == [11, 14], "the original figure was lost"

    def test_resending_the_same_revision_is_a_no_op(self, session: Session, tmp_path: Path) -> None:
        first = load(session, write(tmp_path, monthly()))
        report = load(session, write(tmp_path, monthly(), name="again.jsonl"))

        assert report.batch_id == first.batch_id
        assert report.submissions_unchanged == 1
        assert report.submissions_loaded == 0
        assert (
            session.execute(select(func.count()).select_from(AggregateSubmission)).scalar_one() == 1
        )

    def test_the_same_submission_twice_in_one_batch_is_quarantined(
        self, session: Session, tmp_path: Path
    ) -> None:
        report = load(session, write(tmp_path, monthly(), monthly()))
        assert report.issue_codes.get("duplicate_submission_in_batch") == 1

    def test_changed_content_cannot_reuse_a_revision(
        self, session: Session, tmp_path: Path
    ) -> None:
        load(session, write(tmp_path, monthly(observations=[_cell("EP01c", 11)])))
        report = load(
            session,
            write(
                tmp_path,
                monthly(observations=[_cell("EP01c", 99)]),
                name="conflicting-revision.jsonl",
            ),
        )

        assert report.submissions_quarantined == 1
        assert report.issue_codes["revision_payload_conflict"] == 1
        assert (
            session.execute(select(AggregateSubmission)).scalars().one().observations[0].value == 11
        )
        issue = session.execute(
            select(ImportValidationIssue).where(
                ImportValidationIssue.code == "revision_payload_conflict"
            )
        ).scalar_one()
        assert issue.import_source_row_id is not None

    def test_a_late_older_revision_never_replaces_the_latest(
        self, session: Session, tmp_path: Path
    ) -> None:
        load(session, write(tmp_path, monthly(revision=1), name="r1.jsonl"))
        load(session, write(tmp_path, monthly(revision=3), name="r3.jsonl"))
        report = load(session, write(tmp_path, monthly(revision=2), name="r2-late.jsonl"))

        rows = (
            session.execute(select(AggregateSubmission).order_by(AggregateSubmission.revision))
            .scalars()
            .all()
        )
        assert [row.submission_status for row in rows] == [
            AggregateSubmissionStatus.SUPERSEDED,
            AggregateSubmissionStatus.SUPERSEDED,
            AggregateSubmissionStatus.ACCEPTED,
        ]
        assert rows[1].supersedes_id == rows[0].id
        assert report.submissions_superseding == 0


class TestTheDatabaseRefusesContradictions:
    def test_a_weekly_period_spanning_a_quarter_is_refused_by_the_database(
        self, session: Session
    ) -> None:
        """The validator refuses it too. Both, because a second producer path
        must not be able to bypass the first."""
        session.add(_submission_row(period_end=date(2026, 5, 31)))
        with pytest.raises(IntegrityError, match="period_length_matches_type"):
            session.commit()

    def test_a_negative_observation_is_refused_by_the_database(self, session: Session) -> None:
        submission = _submission_row()
        submission.observations = [AggregateObservation(element_code="MA.", value=-1)]
        session.add(submission)
        with pytest.raises(IntegrityError, match="value_not_negative"):
            session.commit()

    def test_a_monthly_form_cannot_be_stored_as_a_week(self, session: Session) -> None:
        session.add(_submission_row(form=AggregateForm.HMIS_105))
        with pytest.raises(IntegrityError, match="form_matches_period_type"):
            session.commit()

    def test_a_week_must_start_on_monday_in_the_database(self, session: Session) -> None:
        session.add(
            _submission_row(
                period_start=date(2026, 3, 3),
                period_end=date(2026, 3, 9),
            )
        )
        with pytest.raises(IntegrityError, match="week_starts_monday"):
            session.commit()

    def test_more_positives_than_tests_is_refused_by_the_database(self, session: Session) -> None:
        submission = _submission_row()
        submission.laboratory_observations = [
            LaboratoryTestObservation(test_code="PS02", number_done=4, number_positive=9)
        ]
        session.add(submission)
        with pytest.raises(IntegrityError, match="positive_not_above_done"):
            session.commit()

    def test_the_same_facility_form_period_and_revision_cannot_be_stored_twice(
        self, session: Session
    ) -> None:
        session.add(_submission_row())
        session.commit()
        session.add(_submission_row())
        with pytest.raises(IntegrityError, match="uq_aggregate_submission"):
            session.commit()

    def test_only_one_revision_can_be_accepted(self, session: Session) -> None:
        session.add(
            _submission_row(
                revision=1,
                submission_status=AggregateSubmissionStatus.ACCEPTED,
            )
        )
        session.commit()
        session.add(
            _submission_row(
                revision=2,
                submission_status=AggregateSubmissionStatus.ACCEPTED,
            )
        )
        with pytest.raises(IntegrityError, match="one_accepted"):
            session.commit()


class TestTheOperatingModes:
    """dry-run, validate-only, load, replay and resume.

    An operator points ``dry-run`` at an unfamiliar file to see what it
    contains, so it has to be safe to point at one; ``validate`` is how a
    producer is sent an actionable list before anything is loaded. Both claims
    are only worth making if something checks them.
    """

    def test_a_dry_run_writes_nothing_at_all(self, session: Session, tmp_path: Path) -> None:
        report = load(session, write(tmp_path, monthly()), AggregateIngestOptions(dry_run=True))

        assert report.submissions_received == 1
        assert report.batch_id is None
        assert _count(session, ImportBatch) == 0
        assert _count(session, ImportSourceRow) == 0
        assert _count(session, AggregateSubmission) == 0

    def test_a_dry_run_still_reports_what_is_wrong(self, session: Session, tmp_path: Path) -> None:
        """Silent is not the same as safe: the run has to say what it read."""
        artefact = write(tmp_path, monthly(observations=[_cell("NOPE99", 5)]))
        report = load(session, artefact, AggregateIngestOptions(dry_run=True))

        assert report.submissions_quarantined == 1
        assert report.issue_codes.get("unknown_element") == 1
        assert _count(session, ImportBatch) == 0

    def test_validate_only_records_the_findings_but_no_submission(
        self, session: Session, tmp_path: Path
    ) -> None:
        """So a producer can be sent an actionable list before a load."""
        artefact = write(tmp_path, monthly(observations=[_cell("NOPE99", 5)]))
        report = load(session, artefact, AggregateIngestOptions(validate_only=True))

        assert report.submissions_quarantined == 1
        assert _count(session, ImportBatch) == 1
        assert _count(session, ImportValidationIssue) >= 1
        assert _count(session, AggregateSubmission) == 0, "validate-only wrote a submission"

    def test_a_validated_batch_can_then_be_loaded(self, session: Session, tmp_path: Path) -> None:
        artefact = write(tmp_path, monthly())
        load(session, artefact, AggregateIngestOptions(validate_only=True))
        report = load(session, artefact)

        assert report.submissions_loaded == 1
        assert _count(session, AggregateSubmission) == 1
        assert _count(session, ImportBatch) == 1, "validate then load created two batches"

    def test_replaying_the_exact_artefact_writes_nothing_further(
        self, session: Session, tmp_path: Path
    ) -> None:
        artefact = write(tmp_path, monthly())
        first = load(session, artefact)
        second = load(session, artefact)

        assert second.batch_id == first.batch_id
        assert second.submissions_loaded == 0
        assert _count(session, AggregateSubmission) == 1
        assert _count(session, ImportBatch) == 1

    def test_resume_reprocesses_a_known_artefact_without_duplicating(
        self, session: Session, tmp_path: Path
    ) -> None:
        """An operator retrying an interrupted import must not multiply either
        the canonical rows or the diagnostic ones."""
        artefact = write(tmp_path, monthly())
        load(session, artefact)
        issues_before = _count(session, ImportValidationIssue)

        report = load(session, artefact, AggregateIngestOptions(resume=True))

        assert _count(session, AggregateSubmission) == 1
        assert _count(session, ImportBatch) == 1
        assert _count(session, ImportSourceRow) == 1
        assert _count(session, ImportValidationIssue) == issues_before
        assert report.submissions_loaded == 0

    def test_the_encounter_domain_is_untouched_by_an_aggregate_batch(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Prompt 9's behaviour must survive the shared lifecycle: an aggregate
        batch is a different artefact identity, not a relabelled encounter one."""
        load(session, write(tmp_path, monthly()))
        batches = session.execute(select(ImportBatch)).scalars().all()
        assert [batch.import_domain for batch in batches] == ["aggregate"]
        assert _count(session, OpdEncounter) == 0


def _count(session: Session, model: object) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


class TestTheSchemaMatchesTheModel:
    """Invariants that live in the database and must also be declared in the ORM.

    A constraint the model does not declare is one a future autogenerate offers
    to drop, and the drop reads as routine housekeeping.
    """

    def test_a_payload_checksum_that_is_not_a_sha256_is_refused(self, session: Session) -> None:
        """The rule that stops a producer changing figures under an unchanged
        revision number. It is only worth having if it is real."""
        session.add(_submission_row(payload_checksum="too-short"))
        with pytest.raises(IntegrityError, match="payload_checksum_sha256"):
            session.commit()

    def test_import_domain_has_no_database_default(self, aggregate_engine: Engine) -> None:
        """The server default exists only to backfill rows during migration
        0010 and is dropped immediately afterwards.

        ``import_domain`` is part of the batch's identity: a row that omits it
        must fail loudly rather than silently become an encounter batch and
        alias one.
        """
        with aggregate_engine.connect() as connection:
            default = connection.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = 'mars_core' AND table_name = 'import_batch' "
                    "AND column_name = 'import_domain'"
                )
            ).scalar_one()
        assert default is None, f"import_domain still carries a server default: {default!r}"

    def test_a_batch_without_a_domain_is_refused_rather_than_defaulted(
        self, aggregate_engine: Engine
    ) -> None:
        """Every other required column is supplied, so the only thing missing
        is the domain. It must be the thing that fails."""
        counters = (
            "rows_received, rows_loaded, rows_updated, rows_unchanged, "
            "rows_quarantined, rows_linked, rows_unlinked, unresolved_geography, "
            "warning_count, error_count"
        )
        with aggregate_engine.connect() as connection:  # noqa: SIM117
            with pytest.raises(IntegrityError, match="import_domain"):
                connection.execute(
                    text(
                        "INSERT INTO mars_core.import_batch "
                        "(id, source_system, schema_version, artefact_checksum, "
                        f" import_status, received_at, declared_row_count, {counters}, "
                        " created_at, updated_at) "
                        "VALUES (gen_random_uuid(), 'probe', '1.0', :checksum, "
                        " 'received', now(), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "
                        " now(), now())"
                    ),
                    {"checksum": "a" * 64},
                )


def _submission_row(**overrides: object) -> AggregateSubmission:
    from datetime import UTC, datetime

    defaults: dict[str, object] = {
        "facility_id": FACILITY_ID,
        "form": AggregateForm.HMIS_033B,
        "period_type": AggregatePeriodType.WEEK,
        "period_start": date(2026, 3, 2),
        "period_end": date(2026, 3, 8),
        "revision": 1,
        "source_system": SOURCE_SYSTEM,
        "payload_checksum": "0" * 64,
        "received_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return AggregateSubmission(**defaults)
