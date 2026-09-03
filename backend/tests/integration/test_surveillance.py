"""Testing, treatment and commodity surveillance against live PostgreSQL.

The fixture facility is built so every count below is checkable by hand: eight
attendances, six of them tested, four positive, and one month in which the
facility reported its rapid diagnostic tests out of stock.

Three engines, exercised independently - each class here runs one engine and
asserts on its own table, because a shared harness would hide the day one of
them starts depending on another.

What these tests protect:

* that a fall in confirmed cases during a fall in testing is presented as a
  testing finding, with the commodity context that explains it attached;
* that a treatment figure never claims a patient received a drug;
* that a commodity alert is a supply observation and stays one - no threshold
  is invented for prolonged, repeated, low or imminent, and severity stays
  unclassified until a programme approves rules;
* that blank, zero and unavailable remain three different things.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics import surveillance as engines
from mars.analytics.surveillance import (
    COMMODITY_RULES_KEY,
    CommoditySurveillanceEngine,
    TreatmentSurveillanceEngine,
)
from mars.domain import surveillance as models
from mars.domain.aggregate import AggregateSubmission, CommodityStockObservation
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterPrescription,
    OpdEncounterTest,
)
from mars.domain.enums import (
    AggregateForm,
    AggregatePeriodType,
    AggregateSubmissionStatus,
    AlertSeverity,
    AttendanceType,
    CommodityAlertKind,
    CommodityFactKind,
    DateAssignmentMethod,
    FeverStatus,
    IndicatorValueStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PatientCategory,
    Sex,
    StockMetric,
    TreatmentMeasure,
)
from mars.domain.enums import TestingMeasure as Measure
from mars.domain.governance import ConfigurationKey, ConfigurationVersion
from mars.domain.organisation import Facility
from mars.domain.surveillance import (
    CommodityOperationalAlert,
    CommodityStockFact,
    TreatmentSurveillanceResult,
)

# ``TestingSurveillanceEngine`` and ``TestingSurveillanceResult`` are reached
# through their modules: pytest tries to collect any module-level name
# beginning with "Test", and both of those do.

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("cc200000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("cc200000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("cc200000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("cc200000-0000-4000-8000-000000000020")
FACILITY_ID = uuid.UUID("cc200000-0000-4000-8000-000000000001")

PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)
PREVIOUS_PERIOD = (date(2026, 2, 1), date(2026, 2, 28))

RDT = "SS34"
AL = "SS01"

#: Test-only rules. The tests asserting MARS refuses to classify without them
#: are what stops these becoming production defaults.
TEST_RULES = {
    "prolonged_stock_out": {"minimum_days": 7, "severity": "urgent"},
    "repeated_stock_out": {"minimum_periods": 2, "severity": "attention"},
}


@pytest.fixture(scope="module")
def surveillance_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(surveillance_db: Engine) -> None:
    with surveillance_db.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-SURV-0001', 'Surveillance fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "911", "Supplytown", COUNTRY_ID, 1, "UG/911"),
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
                VALUES (:id, 'district_health_office', 'DHO-911', 'Supplytown DHO',
                        'supplytown dho', 0, 'DHO-911', true, now(), now())
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
                     coordinate_validated, is_active, is_synthetic, created_at, updated_at)
                VALUES (:id, :org, :geo, 'HF-911A', 'Supplytown HC', 'supplytown hc',
                        'hc_iii', 'government', false, true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID},
        )


@pytest.fixture
def session(surveillance_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=surveillance_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(surveillance_db: Engine) -> Iterator[None]:
    yield
    with surveillance_db.begin() as connection:
        for table in (
            "mars_analytics.commodity_operational_alert",
            "mars_analytics.commodity_stock_fact",
            "mars_analytics.testing_surveillance_result",
            "mars_analytics.treatment_surveillance_result",
            "mars_core.commodity_stock_observation",
            "mars_core.aggregate_submission",
            "mars_core.opd_encounter_prescription",
            "mars_core.opd_encounter_test",
            "mars_core.opd_encounter",
            "mars_core.patient_reference",
            "mars_governance.configuration_version",
            "mars_governance.configuration_key",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


@pytest.fixture
def facility(session: Session) -> Facility:
    return session.get_one(Facility, FACILITY_ID)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def add_encounter(
    session: Session,
    *,
    day: date,
    method: MalariaTestMethod,
    result: MalariaTestResult,
    treated: bool,
    prescription_row: bool = True,
) -> None:
    """One attendance.

    ``treated`` says a recognisable antimalarial was prescribed;
    ``prescription_row`` says whether the register recorded a treatment line at
    all. The two differ, and MARS reports them separately.
    """
    encounter = OpdEncounter(
        facility_id=FACILITY_ID,
        patient_reference_id=None,
        encounter_date=day,
        date_assignment_method=DateAssignmentMethod.SOURCE_SUPPLIED,
        sex=Sex.FEMALE,
        patient_category=PatientCategory.NATIONAL,
        attendance_type=AttendanceType.NEW_ATTENDANCE,
        fever_present=FeverStatus.YES,
        residence_district_id=DISTRICT_ID,
        source_system="test",
        source_row_reference=f"surv-{uuid.uuid4().hex[:12]}",
    )
    encounter.tests = [OpdEncounterTest(sequence=1, method=method, result=result)]
    if prescription_row:
        encounter.prescriptions = [
            OpdEncounterPrescription(
                sequence=1,
                prescription_raw="Artemether/Lumefantrine 1x2x3" if treated else "Paracetamol",
                drug_name_raw="Artemether/Lumefantrine" if treated else "Paracetamol",
                drug_name_normalised="artemether/lumefantrine" if treated else None,
            )
        ]
    session.add(encounter)


def add_submission(
    session: Session,
    *,
    period_start: date = PERIOD_START,
    period_end: date = PERIOD_END,
    stock: dict[str, dict[StockMetric, Decimal | None]],
    status: AggregateSubmissionStatus = AggregateSubmissionStatus.ACCEPTED,
    revision: int = 1,
) -> AggregateSubmission:
    submission = AggregateSubmission(
        facility_id=FACILITY_ID,
        form=AggregateForm.HMIS_105,
        period_type=AggregatePeriodType.MONTH,
        period_start=period_start,
        period_end=period_end,
        revision=revision,
        submission_status=status,
        source_system="test",
        payload_checksum="a" * 64,
        received_at=datetime.now(UTC),
    )
    for code, metrics in stock.items():
        for metric, value in metrics.items():
            submission.stock_observations.append(
                CommodityStockObservation(
                    commodity_code=code,
                    metric=metric,
                    value=value,
                    unit_of_issue="test" if code == RDT else "tablet",
                )
            )
    session.add(submission)
    session.flush()
    return submission


def approve_rules(session: Session) -> ConfigurationVersion:
    key = ConfigurationKey(
        key=COMMODITY_RULES_KEY,
        label="Commodity alert rules (TEST ONLY)",
        description="Test fixture. Not programme guidance.",
        category="analytics",
        requires_programme_approval=True,
    )
    session.add(key)
    session.flush()
    version = ConfigurationVersion(
        configuration_key_id=key.id,
        version_number=1,
        status=LifecycleStatus.ACTIVE,
        value=TEST_RULES,
        value_checksum="b" * 64,
        effective_from=PERIOD_START,
        reason_for_change="test fixture",
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


@pytest.fixture
def march_attendances(session: Session) -> None:
    """Eight attendances: six tested, four positive, two never tested.

    One positive was not treated, one negative was treated, one untested
    attendance was treated, and one attendance has no treatment line at all.
    """
    add_encounter(
        session,
        day=date(2026, 3, 2),
        method=MalariaTestMethod.RDT,
        result=MalariaTestResult.POSITIVE,
        treated=True,
    )
    add_encounter(
        session,
        day=date(2026, 3, 3),
        method=MalariaTestMethod.RDT,
        result=MalariaTestResult.POSITIVE,
        treated=True,
    )
    add_encounter(
        session,
        day=date(2026, 3, 4),
        method=MalariaTestMethod.MICROSCOPY,
        result=MalariaTestResult.POSITIVE,
        treated=True,
    )
    # Confirmed and not treated: a real finding, reported as its own measure.
    add_encounter(
        session,
        day=date(2026, 3, 5),
        method=MalariaTestMethod.MICROSCOPY,
        result=MalariaTestResult.POSITIVE,
        treated=False,
    )
    # Negative and treated anyway.
    add_encounter(
        session,
        day=date(2026, 3, 6),
        method=MalariaTestMethod.RDT,
        result=MalariaTestResult.NEGATIVE,
        treated=True,
    )
    # Tested, result never written down. Not the same as untested.
    add_encounter(
        session,
        day=date(2026, 3, 7),
        method=MalariaTestMethod.RDT,
        result=MalariaTestResult.UNKNOWN,
        treated=False,
    )
    # Untested and treated.
    add_encounter(
        session,
        day=date(2026, 3, 9),
        method=MalariaTestMethod.NOT_DONE,
        result=MalariaTestResult.UNKNOWN,
        treated=True,
    )
    # Untested, and the register recorded no treatment line at all.
    add_encounter(
        session,
        day=date(2026, 3, 10),
        method=MalariaTestMethod.NOT_DONE,
        result=MalariaTestResult.UNKNOWN,
        treated=False,
        prescription_row=False,
    )
    session.commit()


def read_testing_results(session: Session) -> dict[Measure, models.TestingSurveillanceResult]:
    rows = session.execute(select(models.TestingSurveillanceResult)).scalars().all()
    return {row.measure: row for row in rows}


def read_treatment_results(session: Session) -> dict[TreatmentMeasure, TreatmentSurveillanceResult]:
    rows = session.execute(select(TreatmentSurveillanceResult)).scalars().all()
    return {row.measure: row for row in rows}


# ---------------------------------------------------------------------------
# Testing engine
# ---------------------------------------------------------------------------
class TestTestingSurveillanceEngine:
    def test_counts_are_what_the_register_says(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        engines.TestingSurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        results = read_testing_results(session)

        coverage = results[Measure.TESTING_COVERAGE]
        assert (coverage.numerator, coverage.denominator) == (6, 8)
        assert coverage.value == Decimal("0.750000")

        positivity = results[Measure.TEST_POSITIVITY]
        assert (positivity.numerator, positivity.denominator) == (4, 6)

        assert results[Measure.RDT_SHARE].numerator == 4
        assert results[Measure.MICROSCOPY_SHARE].numerator == 2

    def test_a_missing_result_is_not_an_untested_patient(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        """A test was done and its outcome was never written down. Folding that
        into "untested" would understate testing effort and overstate the gap."""
        engines.TestingSurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        results = read_testing_results(session)
        assert results[Measure.MISSING_RESULT_COUNT].numerator == 1
        assert results[Measure.TESTING_COVERAGE].untested_encounters == 2

    def test_treated_without_a_test_is_counted(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        engines.TestingSurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        results = read_testing_results(session)
        assert results[Measure.UNTESTED_CASES_TREATED].numerator == 1
        assert results[Measure.NEGATIVE_CASES_TREATED].numerator == 1

    def test_a_facility_that_tested_nobody_has_no_positivity(
        self, session: Session, facility: Facility
    ) -> None:
        """Not a positivity of zero. Zero would be read as an absence of
        malaria; the truth is an absence of testing."""
        add_encounter(
            session,
            day=date(2026, 3, 2),
            method=MalariaTestMethod.NOT_DONE,
            result=MalariaTestResult.UNKNOWN,
            treated=True,
        )
        session.commit()

        engines.TestingSurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        positivity = read_testing_results(session)[Measure.TEST_POSITIVITY]
        assert positivity.value is None
        assert positivity.value_status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_volume_change_is_only_produced_against_a_real_previous_period(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        engine = engines.TestingSurveillanceEngine(session)
        engine.compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        assert Measure.TESTING_VOLUME_CHANGE not in read_testing_results(session)

        engine.compute_facility(facility, PERIOD_START, PERIOD_END, previous_period=PREVIOUS_PERIOD)
        session.commit()
        change = read_testing_results(session)[Measure.TESTING_VOLUME_CHANGE]
        # February had no encounters at all, so there is no ratio to report -
        # which is a different statement from "testing did not change".
        assert change.denominator == 0
        assert change.value is None
        assert change.value_status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_reported_stock_out_travels_with_the_testing_figure(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        """The commonest way malaria surveillance misleads itself is reading a
        supply failure as an epidemiological improvement."""
        add_submission(
            session,
            stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)}},
        )
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()

        engines.TestingSurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        coverage = read_testing_results(session)[Measure.TESTING_COVERAGE]
        assert coverage.commodity_context is not None
        conditions = coverage.commodity_context["stock_conditions"]
        assert conditions[0]["commodity"] == RDT
        assert conditions[0]["days_out_of_stock"] == 9

    def test_every_row_says_what_it_is_not(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        engines.TestingSurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        for row in read_testing_results(session).values():
            assert row.quality_context is not None
            assert "testing finding" in row.quality_context["domain_limit"]

    def test_recomputing_unchanged_inputs_writes_nothing_new(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        engine = engines.TestingSurveillanceEngine(session)
        first = engine.compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        second = engine.compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        assert second.results_written == 0
        assert second.results_unchanged == first.results_written

    def test_changed_inputs_supersede_rather_than_overwrite(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        engine = engines.TestingSurveillanceEngine(session)
        engine.compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        add_encounter(
            session,
            day=date(2026, 3, 20),
            method=MalariaTestMethod.RDT,
            result=MalariaTestResult.POSITIVE,
            treated=True,
        )
        session.commit()
        engine.compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()

        rows = (
            session.execute(
                select(models.TestingSurveillanceResult).where(
                    models.TestingSurveillanceResult.measure == Measure.TEST_POSITIVITY
                )
            )
            .scalars()
            .all()
        )
        # The figure a district acted on last week is still readable.
        assert len(rows) == 2
        assert {row.numerator for row in rows} == {4, 5}


# ---------------------------------------------------------------------------
# Treatment engine
# ---------------------------------------------------------------------------
class TestTreatmentSurveillanceEngine:
    def test_counts_are_what_the_register_says(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        TreatmentSurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        results = read_treatment_results(session)

        treated = results[TreatmentMeasure.CONFIRMED_TREATED]
        assert (treated.numerator, treated.denominator) == (3, 4)
        assert results[TreatmentMeasure.CONFIRMED_NOT_TREATED].numerator == 1

    def test_treatment_without_confirmation_is_its_own_measure(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        TreatmentSurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        # One negative treated and one untested treated.
        assert (
            read_treatment_results(session)[TreatmentMeasure.TREATED_WITHOUT_CONFIRMATION].numerator
            == 2
        )

    def test_an_unrecorded_treatment_line_is_not_an_untreated_patient(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        """A facility that records nothing and a facility that treated nobody
        are different facts about that facility."""
        TreatmentSurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        results = read_treatment_results(session)
        assert results[TreatmentMeasure.MISSING_TREATMENT_INFORMATION].numerator == 1

    def test_no_row_claims_a_patient_received_a_drug(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        TreatmentSurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        for row in read_treatment_results(session).values():
            limit = row.quality_context["domain_limit"]
            assert "prescribed" in limit
            assert "received" in limit

    def test_no_row_mentions_resistance_or_failure(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        """Routine data may produce a surveillance signal. It may never claim
        confirmed drug resistance."""
        TreatmentSurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        for row in read_treatment_results(session).values():
            text_blob = f"{row.notes or ''} {row.quality_context}".lower()
            assert "resistan" not in text_blob
            assert "treatment failure" not in text_blob

    def test_antimalarial_stock_context_is_attached_not_diagnostic_stock(
        self, session: Session, facility: Facility, march_attendances: None
    ) -> None:
        """Treatment carries the antimalarial supply position; testing carries
        the diagnostic one. Mixing them would explain a decline with the wrong
        commodity."""
        add_submission(
            session,
            stock={
                RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)},
                AL: {StockMetric.STOCK_ON_HAND: Decimal(0)},
            },
        )
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()

        TreatmentSurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        row = read_treatment_results(session)[TreatmentMeasure.CONFIRMED_TREATED]
        codes = {c["commodity"] for c in row.commodity_context["stock_conditions"]}
        assert codes == {AL}


# ---------------------------------------------------------------------------
# Commodity engine
# ---------------------------------------------------------------------------
class TestCommoditySurveillanceEngine:
    def test_without_approved_rules_only_the_reported_fact_is_raised(
        self, session: Session, facility: Facility
    ) -> None:
        add_submission(session, stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)}})
        session.commit()

        report = CommoditySurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()

        alerts = session.execute(select(CommodityOperationalAlert)).scalars().all()
        assert len(alerts) == 1
        assert alerts[0].alert_kind is CommodityAlertKind.STOCK_OUT_REPORTED
        assert alerts[0].severity is AlertSeverity.UNCLASSIFIED
        assert alerts[0].configuration_version_id is None
        assert set(report.classifications_skipped) == {
            CommodityAlertKind.PROLONGED_STOCK_OUT.value,
            CommodityAlertKind.REPEATED_STOCK_OUT.value,
            CommodityAlertKind.MULTI_COMMODITY_STOCK_OUT.value,
            CommodityAlertKind.LOW_STOCK.value,
            CommodityAlertKind.IMMINENT_STOCK_OUT.value,
        }
        assert COMMODITY_RULES_KEY in (report.notes or "")

    def test_the_missing_configuration_is_named_rather_than_the_run_looking_empty(
        self, session: Session, facility: Facility
    ) -> None:
        """An unconfigured deployment producing no classifications is expected.
        It must say so rather than appear broken."""
        add_submission(session, stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)}})
        session.commit()
        report = CommoditySurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        assert report.classifications_skipped
        assert "governed thresholds" in (report.notes or "")

    def test_nine_days_out_of_stock_is_not_called_prolonged(
        self, session: Session, facility: Facility
    ) -> None:
        """Nine days may or may not be prolonged. MARS does not know, and will
        not decide on a programme's behalf."""
        add_submission(session, stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)}})
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        kinds = {
            row.alert_kind
            for row in session.execute(select(CommodityOperationalAlert)).scalars().all()
        }
        assert CommodityAlertKind.PROLONGED_STOCK_OUT not in kinds

    def test_approved_rules_are_read_and_recorded_on_the_alert(
        self, session: Session, facility: Facility
    ) -> None:
        version = approve_rules(session)
        add_submission(session, stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)}})
        session.commit()

        report = CommoditySurveillanceEngine(session).compute_facility(
            facility, PERIOD_START, PERIOD_END
        )
        session.commit()
        assert report.classifications_skipped == []
        alert = session.execute(select(CommodityOperationalAlert)).scalars().one()
        assert alert.configuration_version_id == version.id

    def test_a_blank_stock_column_is_a_reporting_gap_not_a_stock_out(
        self, session: Session, facility: Facility
    ) -> None:
        """The difference matters most exactly when supply has failed."""
        add_submission(session, stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: None}})
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()

        fact = session.execute(select(CommodityStockFact)).scalars().one()
        assert fact.fact_kind is CommodityFactKind.STOCK_NOT_REPORTED
        assert fact.value is None
        assert fact.value_status is IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA
        assert session.execute(select(CommodityOperationalAlert)).scalars().all() == []

    def test_zero_days_out_of_stock_raises_nothing(
        self, session: Session, facility: Facility
    ) -> None:
        """A reported zero is a real figure and a good one. It is not an alert
        and it is not a gap."""
        add_submission(
            session,
            stock={
                RDT: {
                    StockMetric.DAYS_OUT_OF_STOCK: Decimal(0),
                    StockMetric.STOCK_ON_HAND: Decimal(120),
                }
            },
        )
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        assert session.execute(select(CommodityStockFact)).scalars().all() == []
        assert session.execute(select(CommodityOperationalAlert)).scalars().all() == []

    def test_a_zero_balance_is_its_own_fact(self, session: Session, facility: Facility) -> None:
        add_submission(session, stock={AL: {StockMetric.STOCK_ON_HAND: Decimal(0)}})
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        fact = session.execute(select(CommodityStockFact)).scalars().one()
        assert fact.fact_kind is CommodityFactKind.STOCK_ON_HAND_ZERO
        assert fact.stock_on_hand == Decimal("0.00")
        assert fact.commodity_code == AL

    def test_only_accepted_submissions_are_read(self, session: Session, facility: Facility) -> None:
        """A quarantined transcription is not a stock position."""
        add_submission(
            session,
            stock={RDT: {StockMetric.DAYS_OUT_OF_STOCK: Decimal(9)}},
            status=AggregateSubmissionStatus.QUARANTINED,
        )
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        assert session.execute(select(CommodityStockFact)).scalars().all() == []

    def test_the_alert_statement_denies_the_reading_it_would_otherwise_invite(
        self, session: Session, facility: Facility
    ) -> None:
        add_submission(session, stock={AL: {StockMetric.STOCK_ON_HAND: Decimal(0)}})
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        alert = session.execute(select(CommodityOperationalAlert)).scalars().one()
        assert "supply-chain observation" in alert.statement
        assert "not a finding" in alert.statement
        assert "resistance" in alert.statement

    def test_an_alert_points_back_at_the_facts_that_support_it(
        self, session: Session, facility: Facility
    ) -> None:
        add_submission(session, stock={AL: {StockMetric.STOCK_ON_HAND: Decimal(0)}})
        session.commit()
        CommoditySurveillanceEngine(session).compute_facility(facility, PERIOD_START, PERIOD_END)
        session.commit()
        alert = session.execute(select(CommodityOperationalAlert)).scalars().one()
        fact = session.execute(select(CommodityStockFact)).scalars().one()
        assert alert.supporting_fact_ids["facts"] == [str(fact.id)]


# ---------------------------------------------------------------------------
# What the schema itself refuses
# ---------------------------------------------------------------------------
class TestTheDatabaseHoldsTheLine:
    def test_a_classified_alert_without_a_rule_is_rejected(
        self, session: Session, facility: Facility
    ) -> None:
        """Not only the engine: any future code path writing here is held to
        the same rule."""
        session.add(
            CommodityOperationalAlert(
                alert_kind=CommodityAlertKind.PROLONGED_STOCK_OUT,
                commodity_code=AL,
                facility_id=FACILITY_ID,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                severity=AlertSeverity.UNCLASSIFIED,
                statement="Invented without a governed rule.",
                configuration_version_id=None,
                input_fingerprint="c" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                raised_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="classified_alerts_need_config"):
            session.flush()

    def test_a_severity_without_a_rule_is_rejected(
        self, session: Session, facility: Facility
    ) -> None:
        session.add(
            CommodityOperationalAlert(
                alert_kind=CommodityAlertKind.STOCK_OUT_REPORTED,
                commodity_code=AL,
                facility_id=FACILITY_ID,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                severity=AlertSeverity.URGENT,
                statement="Urgent according to nobody.",
                configuration_version_id=None,
                input_fingerprint="d" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                raised_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="severity_requires_configuration"):
            session.flush()

    def test_a_days_out_of_stock_fact_must_carry_days(
        self, session: Session, facility: Facility
    ) -> None:
        session.add(
            CommodityStockFact(
                fact_kind=CommodityFactKind.DAYS_OUT_OF_STOCK_REPORTED,
                commodity_code=AL,
                facility_id=FACILITY_ID,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                period_grain="month",
                geography_grain="facility",
                days_out_of_stock=None,
                value=None,
                value_status=IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                input_fingerprint="e" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                computed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="fact_carries_its_evidence"):
            session.flush()
