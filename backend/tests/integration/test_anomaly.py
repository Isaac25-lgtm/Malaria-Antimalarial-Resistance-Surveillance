"""Temporal anomaly detection and persistence against live PostgreSQL.

One facility, one testing measure, six months of history at 0.10-0.60 and a
target month the tests set deliberately.

What these tests protect:

* the distinction between *nothing was unusual* and *nothing could be judged* -
  the single most important property in this module;
* that MARS does not decide how large a departure has to be;
* that it never silently falls back to a method nobody approved;
* that a spike and a sustained rise stay distinguishable, and that calling one
  sustained needs an approved rule;
* that a flag is never presented as a cause, and never as resistance.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics.anomaly import (
    ANOMALY_RULE_CODE,
    PERSISTENCE_PARAMETER,
    REQUIRED_PARAMETERS,
    AnomalyEngine,
    latest_detection,
)
from mars.analytics.baseline import BASELINE_METHOD_CODE, BaselineEngine, latest_build

# Reached through its module: pytest tries to collect any module-level name
# beginning with "Test", and TestingSurveillanceResult does.
from mars.domain import surveillance as models
from mars.domain.anomaly import AnomalyBuild, AnomalyPersistence, TemporalAnomalyResult
from mars.domain.enums import (
    AnomalyBuildStatus,
    AnomalyDetectionMethod,
    AnomalyDirection,
    AnomalyOutcome,
    BaselineMethod,
    BaselineSeriesKind,
    GeographyGrain,
    IndicatorValueStatus,
    LifecycleStatus,
    MethodKind,
    PeriodGrain,
)
from mars.domain.enums import TestingMeasure as Measure
from mars.domain.governance import MethodDefinition, MethodVersion

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("aa400000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("aa400000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("aa400000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("aa400000-0000-4000-8000-000000000020")
FACILITY_ID = uuid.UUID("aa400000-0000-4000-8000-000000000001")

TARGET_START = date(2026, 7, 1)
TARGET_END = date(2026, 7, 31)
SERIES = BaselineSeriesKind.TESTING_MEASURE

#: Test-only rule values. The tests asserting MARS refuses without them are
#: what stops these becoming production defaults.
TEST_THRESHOLD = 2.0
TEST_MINIMUM_CASES = 20
TEST_PERSISTENCE = 2


@pytest.fixture(scope="module")
def anomaly_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(anomaly_db: Engine) -> None:
    with anomaly_db.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-ANOM-0001', 'Anomaly fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "931", "Spikeville", COUNTRY_ID, 1, "UG/931"),
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
                VALUES (:id, 'district_health_office', 'DHO-931', 'Spikeville DHO',
                        'spikeville dho', 0, 'DHO-931', true, now(), now())
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
                VALUES (:id, :org, :geo, 'HF-931A', 'Spikeville HC', 'spikeville hc',
                        'hc_iii', 'government', false, true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID},
        )


@pytest.fixture
def session(anomaly_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=anomaly_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(anomaly_db: Engine) -> Iterator[None]:
    yield
    with anomaly_db.begin() as connection:
        for table in (
            "mars_analytics.anomaly_persistence",
            "mars_analytics.temporal_anomaly_result",
            "mars_analytics.anomaly_build",
            "mars_analytics.baseline_result",
            "mars_analytics.baseline_build",
            "mars_analytics.testing_surveillance_result",
            "mars_governance.method_version",
            "mars_governance.method_definition",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def month(offset: int, *, of: date = TARGET_START) -> tuple[date, date]:
    total = of.year * 12 + (of.month - 1) - offset
    start = date(total // 12, total % 12 + 1, 1)
    following = date((total + 1) // 12, (total + 1) % 12 + 1, 1)
    return start, following - timedelta(days=1)


def add_observation(
    session: Session,
    *,
    period: tuple[date, date],
    value: Decimal | None,
    numerator: int | None = 100,
    status: IndicatorValueStatus = IndicatorValueStatus.AVAILABLE,
    computed_at: datetime | None = None,
) -> None:
    start, end = period
    session.add(
        models.TestingSurveillanceResult(
            measure=Measure.TEST_POSITIVITY,
            geography_grain=GeographyGrain.FACILITY,
            facility_id=FACILITY_ID,
            period_start=start,
            period_end=end,
            period_grain=PeriodGrain.MONTH,
            numerator=numerator,
            denominator=None,
            value=value,
            value_status=status,
            input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            source_cutoff=datetime.now(UTC),
            engine_version="0.0.0-test",
            computed_at=computed_at or datetime.now(UTC),
        )
    )
    session.flush()


def approve_baseline(session: Session, *, uncertainty: float | None = None) -> MethodVersion:
    definition = MethodDefinition(
        code=BASELINE_METHOD_CODE,
        label="Temporal baseline (TEST ONLY)",
        kind=MethodKind.TEMPORAL_BASELINE,
        purpose="Test fixture. Not programme guidance.",
    )
    session.add(definition)
    session.flush()
    parameters: dict[str, object] = {
        "baseline_method": BaselineMethod.HISTORICAL_MEDIAN.value,
        "history_periods": 6,
        "minimum_history_periods": 4,
        "minimum_completeness": 0.6,
    }
    if uncertainty is not None:
        parameters["uncertainty_multiplier"] = uncertainty
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only baseline parameters.",
        parameters=parameters,
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def approve_rule(
    session: Session,
    *,
    method: AnomalyDetectionMethod = AnomalyDetectionMethod.ROBUST_Z_SCORE,
    threshold: float = TEST_THRESHOLD,
    minimum_cases: int = TEST_MINIMUM_CASES,
    persistence: int | None = None,
    parameters: dict[str, object] | None = None,
) -> MethodVersion:
    definition = MethodDefinition(
        code=ANOMALY_RULE_CODE,
        label="Temporal anomaly rule (TEST ONLY)",
        kind=MethodKind.SIGNAL_RULE,
        purpose="Test fixture. Not programme guidance.",
    )
    session.add(definition)
    session.flush()
    values: dict[str, object] = {
        "detection_method": method.value,
        "deviation_threshold": threshold,
        "minimum_case_count": minimum_cases,
    }
    if persistence is not None:
        values[PERSISTENCE_PARAMETER] = persistence
    if parameters is not None:
        values = parameters
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only detection rule.",
        parameters=values,
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def stable_history(session: Session, *, of: date = TARGET_START) -> None:
    """Six months at 0.30, 0.31, 0.30, 0.31, 0.30, 0.31.

    A tight, well-behaved series, so a departure is unambiguous.
    """
    for offset, value in zip(range(6, 0, -1), "010101", strict=True):
        add_observation(session, period=month(offset, of=of), value=Decimal(f"0.3{value}0000"))


def build_baseline(session: Session, *, target: date = TARGET_START) -> None:
    end = month(-1, of=target)[0] - timedelta(days=1)
    BaselineEngine(session).build(target, end, series_kind=SERIES)
    session.flush()


def detect(session: Session, *, target: date = TARGET_START):
    end = month(-1, of=target)[0] - timedelta(days=1)
    build = latest_build(session, target, end, SERIES)
    return AnomalyEngine(session).detect(target, end, series_kind=SERIES, baseline_build=build)


def results(session: Session) -> list[TemporalAnomalyResult]:
    return list(session.execute(select(TemporalAnomalyResult)).scalars().all())


def runs(session: Session) -> list[AnomalyPersistence]:
    return list(session.execute(select(AnomalyPersistence)).scalars().all())


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------
class TestMarsDoesNotDecideHowLargeIsTooLarge:
    def test_without_an_approved_rule_nothing_is_judged(self, session: Session) -> None:
        approve_baseline(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()

        report = detect(session)
        session.commit()
        assert report.status is AnomalyBuildStatus.NOT_CONFIGURED
        assert results(session) == []

    def test_the_refusal_names_every_missing_parameter(self, session: Session) -> None:
        report = detect(session)
        session.commit()
        for name in REQUIRED_PARAMETERS:
            assert name in report.missing_configuration
        stored = session.execute(select(AnomalyBuild)).scalars().one()
        assert stored.build_status is AnomalyBuildStatus.NOT_CONFIGURED
        assert set(REQUIRED_PARAMETERS) <= set(stored.missing_configuration["parameters"])

    def test_a_refused_run_is_not_offered_as_a_detection(self, session: Session) -> None:
        """Offering it would let a caller read "no flags" as "nothing unusual"."""
        detect(session)
        session.commit()
        end = month(-1)[0] - timedelta(days=1)
        assert latest_detection(session, TARGET_START, end, SERIES) is None

    def test_a_method_mars_has_not_implemented_is_refused(self, session: Session) -> None:
        approve_rule(
            session,
            parameters={
                "detection_method": "farrington",
                "deviation_threshold": 2,
                "minimum_case_count": 20,
            },
        )
        session.commit()
        report = detect(session)
        session.commit()
        assert report.status is AnomalyBuildStatus.NOT_CONFIGURED
        assert report.missing_configuration == ["detection_method"]


# ---------------------------------------------------------------------------
# The distinction that matters
# ---------------------------------------------------------------------------
class TestQuietIsNotTheSameAsQuiet:
    def test_no_baseline_is_not_recorded_as_normal(self, session: Session) -> None:
        """Nothing to compare against is not a finding of nothing unusual."""
        approve_rule(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()

        report = detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_EVALUATED_NO_BASELINE
        assert row.expected_value is None
        assert row.absolute_deviation is None
        assert report.not_flagged == 0
        assert "not the same as nothing being unusual" in (row.notes or "")

    def test_too_few_cases_is_its_own_outcome(self, session: Session) -> None:
        """A doubling of two cases is arithmetic, not epidemiology - and saying
        so is different from saying the period looked fine."""
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(
            session,
            period=(TARGET_START, TARGET_END),
            value=Decimal("0.900000"),
            numerator=3,
        )
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT
        assert row.case_count == 3
        assert row.minimum_case_count == TEST_MINIMUM_CASES
        assert row.absolute_deviation is None

    def test_an_absent_case_count_is_not_a_low_one(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(
            session,
            period=(TARGET_START, TARGET_END),
            value=Decimal("0.900000"),
            numerator=None,
        )
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_EVALUATED_COUNT_UNKNOWN
        assert "Different from being below it" in (row.notes or "")

    def test_an_unreported_period_is_not_a_quiet_one(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(
            session,
            period=(TARGET_START, TARGET_END),
            value=None,
            status=IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR,
        )
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_EVALUATED_NO_OBSERVATION
        assert "not a statement that the period was normal" in (row.notes or "")

    def test_mars_does_not_fall_back_to_a_method_nobody_approved(self, session: Session) -> None:
        """A robust z-score needs a spread. A flat baseline has none, and
        quietly switching to a relative deviation would apply an unapproved
        rule to a real district."""
        approve_baseline(session)
        approve_rule(session, method=AnomalyDetectionMethod.ROBUST_Z_SCORE)
        for offset in range(6, 0, -1):
            add_observation(session, period=month(offset), value=Decimal("0.300000"))
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE
        assert "does not substitute another method" in (row.notes or "")
        # The arithmetic is still recorded; only the judgement is withheld.
        assert row.absolute_deviation == Decimal("0.600000")
        assert row.deviation_score is None

    def test_a_band_method_without_a_band_is_inapplicable(self, session: Session) -> None:
        approve_baseline(session)  # no uncertainty multiplier
        approve_rule(session, method=AnomalyDetectionMethod.EXCEEDS_UNCERTAINTY_BAND)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        assert results(session)[0].outcome is AnomalyOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE

    def test_a_relative_deviation_from_zero_is_inapplicable(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session, method=AnomalyDetectionMethod.RELATIVE_DEVIATION)
        for offset in range(6, 0, -1):
            add_observation(session, period=month(offset), value=Decimal("0.000000"))
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.400000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE
        assert "expected level is zero" in (row.notes or "")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
class TestDetection:
    def test_a_clear_departure_is_flagged_with_its_evidence(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()

        report = detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.FLAGGED
        assert row.direction is AnomalyDirection.INCREASE
        assert row.baseline_result_id is not None
        assert row.method_version_id is not None
        assert row.deviation_threshold == Decimal("2.000000")
        assert row.history_periods_used == 6
        assert report.flagged == 1

    def test_a_fall_is_flagged_as_a_decrease(self, session: Session) -> None:
        """A rise in positivity may be transmission; a fall may be a testing
        collapse. Reporting only the magnitude loses the difference."""
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.010000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        assert results(session)[0].direction is AnomalyDirection.DECREASE

    def test_an_ordinary_period_is_evaluated_and_not_flagged(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.305000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.outcome is AnomalyOutcome.NOT_FLAGGED
        # "Not flagged" means evaluated. Both halves of that are on the row.
        assert row.absolute_deviation is not None
        assert row.baseline_result_id is not None

    def test_both_deviation_forms_are_recorded(self, session: Session) -> None:
        """Each misleads alone: an absolute rise of 0.1 is trivial at 0.8 and
        dramatic at 0.02."""
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        row = results(session)[0]
        assert row.absolute_deviation is not None
        assert row.relative_deviation is not None
        assert row.deviation_score is not None

    def test_the_threshold_is_copied_onto_the_row(self, session: Session) -> None:
        """A later change to the rule must not silently rewrite what a past
        detection meant."""
        approve_baseline(session)
        approve_rule(session, threshold=2.0)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()
        detect(session)
        session.commit()

        assert results(session)[0].deviation_threshold == Decimal("2.000000")

    def test_every_row_carries_the_interpretation_limit(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()
        detect(session)
        session.commit()

        for row in results(session):
            limit = row.quality_context["interpretation_limit"]
            assert "reason to look, not a finding" in limit
            assert "resistance" in limit

    def test_no_row_claims_a_cause(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.900000"))
        session.commit()
        build_baseline(session)
        session.commit()
        detect(session)
        session.commit()

        for row in results(session):
            blob = f"{row.notes or ''} {row.quality_context}".lower()
            assert "confirmed resistance" not in blob
            assert "treatment failure" not in blob or "not evidence of treatment failure" in blob

    def test_a_superseded_observation_is_not_judged(self, session: Session) -> None:
        """Flagging a figure nobody is looking at wastes an investigation."""
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        earlier = datetime.now(UTC) - timedelta(days=2)
        add_observation(
            session,
            period=(TARGET_START, TARGET_END),
            value=Decimal("0.900000"),
            computed_at=earlier,
        )
        add_observation(session, period=(TARGET_START, TARGET_END), value=Decimal("0.305000"))
        session.commit()
        build_baseline(session)
        session.commit()

        detect(session)
        session.commit()
        rows = results(session)
        assert len(rows) == 1
        assert rows[0].observed_value == Decimal("0.305000")
        assert rows[0].outcome is AnomalyOutcome.NOT_FLAGGED


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
class TestPersistence:
    def _flag_month(self, session: Session, target: date) -> None:
        add_observation(
            session,
            period=(target, month(-1, of=target)[0] - timedelta(days=1)),
            value=Decimal("0.900000"),
        )
        session.commit()
        build_baseline(session, target=target)
        session.commit()
        detect(session, target=target)
        session.commit()

    def test_one_flag_opens_a_run_of_one(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        session.commit()
        self._flag_month(session, TARGET_START)

        run = runs(session)[0]
        assert run.consecutive_periods == 1
        assert run.first_period_start == TARGET_START
        assert run.last_period_end == TARGET_END
        assert "not knowable yet" in (run.notes or "")

    def test_without_an_approved_persistence_rule_nothing_is_called_sustained(
        self, session: Session
    ) -> None:
        """The count is arithmetic; the label is a judgement."""
        approve_baseline(session)
        approve_rule(session)
        stable_history(session)
        session.commit()
        self._flag_month(session, TARGET_START)

        run = runs(session)[0]
        assert run.consecutive_periods == 1
        assert run.is_sustained is None
        assert run.persistence_periods is None

    def test_consecutive_flags_extend_one_run(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session, persistence=TEST_PERSISTENCE)
        stable_history(session)
        session.commit()
        self._flag_month(session, TARGET_START)
        self._flag_month(session, date(2026, 8, 1))

        all_runs = runs(session)
        assert len(all_runs) == 1
        assert all_runs[0].consecutive_periods == 2
        assert all_runs[0].first_period_start == TARGET_START
        assert all_runs[0].last_period_end == date(2026, 8, 31)

    def test_an_approved_rule_makes_a_long_enough_run_sustained(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session, persistence=TEST_PERSISTENCE)
        stable_history(session)
        session.commit()
        self._flag_month(session, TARGET_START)
        assert runs(session)[0].is_sustained is False

        self._flag_month(session, date(2026, 8, 1))
        run = runs(session)[0]
        assert run.is_sustained is True
        assert run.method_version_id is not None

    def test_re_running_a_period_does_not_make_a_spike_sustained(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session, persistence=TEST_PERSISTENCE)
        stable_history(session)
        session.commit()
        self._flag_month(session, TARGET_START)
        detect(session)
        session.commit()

        all_runs = runs(session)
        assert len(all_runs) == 1
        assert all_runs[0].consecutive_periods == 1

    def test_a_run_lists_the_results_behind_it(self, session: Session) -> None:
        approve_baseline(session)
        approve_rule(session, persistence=TEST_PERSISTENCE)
        stable_history(session)
        session.commit()
        self._flag_month(session, TARGET_START)
        self._flag_month(session, date(2026, 8, 1))

        ids = runs(session)[0].contributing_result_ids["results"]
        assert len(ids) == 2
        assert set(ids) <= {str(r.id) for r in results(session)}


# ---------------------------------------------------------------------------
# What the schema itself refuses
# ---------------------------------------------------------------------------
class TestTheDatabaseHoldsTheLine:
    def _build(self, session: Session) -> AnomalyBuild:
        build = AnomalyBuild(
            build_status=AnomalyBuildStatus.NOT_CONFIGURED,
            series_kind=SERIES,
            period_start=TARGET_START,
            period_end=TARGET_END,
            period_grain=PeriodGrain.MONTH,
            missing_configuration={"parameters": ["detection_method"]},
            engine_version="0.0.0-test",
            started_at=datetime.now(UTC),
        )
        session.add(build)
        session.flush()
        return build

    def test_not_flagged_cannot_mean_could_not_tell(self, session: Session) -> None:
        """The single most important rule in this table: a quiet map must not
        carry two opposite meanings in one colour."""
        build = self._build(session)
        session.add(
            TemporalAnomalyResult(
                anomaly_build_id=build.id,
                series_kind=SERIES,
                series_key="test_positivity",
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_ID,
                period_start=TARGET_START,
                period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                outcome=AnomalyOutcome.NOT_FLAGGED,
                observed_value=Decimal("0.400000"),
                expected_value=None,
                absolute_deviation=None,
                input_fingerprint="a" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                computed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="not_flagged_means_evaluated"):
            session.flush()

    def test_a_flag_without_a_rule_is_rejected(self, session: Session) -> None:
        build = self._build(session)
        session.add(
            TemporalAnomalyResult(
                anomaly_build_id=build.id,
                series_kind=SERIES,
                series_key="test_positivity",
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_ID,
                period_start=TARGET_START,
                period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                outcome=AnomalyOutcome.FLAGGED,
                observed_value=Decimal("0.900000"),
                expected_value=Decimal("0.300000"),
                absolute_deviation=Decimal("0.600000"),
                method_version_id=None,
                deviation_threshold=None,
                input_fingerprint="b" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                computed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="a_flag_carries_its_evidence"):
            session.flush()

    def test_no_baseline_means_no_expectation(self, session: Session) -> None:
        build = self._build(session)
        session.add(
            TemporalAnomalyResult(
                anomaly_build_id=build.id,
                series_kind=SERIES,
                series_key="test_positivity",
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_ID,
                period_start=TARGET_START,
                period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                outcome=AnomalyOutcome.NOT_EVALUATED_NO_BASELINE,
                observed_value=Decimal("0.900000"),
                expected_value=Decimal("0.300000"),
                input_fingerprint="c" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                computed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="no_baseline_means_no_expectation"):
            session.flush()

    def test_sustained_without_a_rule_is_rejected(self, session: Session) -> None:
        session.add(
            AnomalyPersistence(
                series_kind=SERIES,
                series_key="test_positivity",
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_ID,
                period_grain=PeriodGrain.MONTH,
                first_period_start=TARGET_START,
                last_period_end=TARGET_END,
                consecutive_periods=1,
                is_sustained=True,
                persistence_periods=None,
                method_version_id=None,
                first_detected_at=datetime.now(UTC),
                last_detected_at=datetime.now(UTC),
                engine_version="0.0.0-test",
            )
        )
        with pytest.raises(IntegrityError, match="sustained_requires_configuration"):
            session.flush()
