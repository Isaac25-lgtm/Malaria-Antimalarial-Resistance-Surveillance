"""The historical baseline engine against live PostgreSQL.

The history below is deliberately small and hand-checkable: six monthly testing
figures for one facility, with known gaps.

What these tests protect:

* that MARS refuses to decide what "normal" means, and says which parameter is
  missing rather than producing nothing and looking broken;
* that a thin history produces no expected value at all - not a confident one
  computed from two periods;
* that a superseded figure does not vote twice in its own history;
* that the seasonal method compares like with like across years;
* that a missing period and a period reported as zero stay different things.

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

from mars.analytics.baseline import (
    BASELINE_METHOD_CODE,
    REQUIRED_PARAMETERS,
    UNCERTAINTY_PARAMETER,
    BaselineEngine,
    latest_build,
)

# Reached through its module: pytest tries to collect any module-level name
# beginning with "Test", and TestingSurveillanceResult does.
from mars.domain import surveillance as models
from mars.domain.baseline import BaselineBuild, BaselineResult
from mars.domain.enums import (
    BaselineBuildStatus,
    BaselineMethod,
    BaselineSeriesKind,
    BaselineSufficiency,
    DispersionMeasure,
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

BOUNDARY_VERSION_ID = uuid.UUID("bb300000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("bb300000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("bb300000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("bb300000-0000-4000-8000-000000000020")
FACILITY_ID = uuid.UUID("bb300000-0000-4000-8000-000000000001")

TARGET_START = date(2026, 7, 1)
TARGET_END = date(2026, 7, 31)

#: Test-only parameters. The tests asserting MARS refuses without them are what
#: stops these becoming production defaults.
TEST_HISTORY_PERIODS = 6
TEST_MINIMUM_PERIODS = 4
TEST_MINIMUM_COMPLETENESS = 0.6


@pytest.fixture(scope="module")
def baseline_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(baseline_db: Engine) -> None:
    with baseline_db.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-BASE-0001', 'Baseline fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "921", "Baseville", COUNTRY_ID, 1, "UG/921"),
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
                VALUES (:id, 'district_health_office', 'DHO-921', 'Baseville DHO',
                        'baseville dho', 0, 'DHO-921', true, now(), now())
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
                VALUES (:id, :org, :geo, 'HF-921A', 'Baseville HC', 'baseville hc',
                        'hc_iii', 'government', false, true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID},
        )


@pytest.fixture
def session(baseline_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=baseline_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(baseline_db: Engine) -> Iterator[None]:
    yield
    with baseline_db.begin() as connection:
        for table in (
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
def approve_method(
    session: Session,
    *,
    method: BaselineMethod = BaselineMethod.HISTORICAL_MEDIAN,
    history_periods: int = TEST_HISTORY_PERIODS,
    minimum_periods: int = TEST_MINIMUM_PERIODS,
    minimum_completeness: float = TEST_MINIMUM_COMPLETENESS,
    uncertainty: float | None = None,
    parameters: dict[str, object] | None = None,
) -> MethodVersion:
    definition = MethodDefinition(
        code=BASELINE_METHOD_CODE,
        label="Temporal baseline (TEST ONLY)",
        kind=MethodKind.TEMPORAL_BASELINE,
        purpose="Test fixture. Not programme guidance.",
    )
    session.add(definition)
    session.flush()
    values: dict[str, object] = {
        "baseline_method": method.value,
        "history_periods": history_periods,
        "minimum_history_periods": minimum_periods,
        "minimum_completeness": minimum_completeness,
    }
    if uncertainty is not None:
        values[UNCERTAINTY_PARAMETER] = uncertainty
    if parameters is not None:
        values = parameters
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only baseline parameters.",
        parameters=values,
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def month(offset: int) -> tuple[date, date]:
    """The month ``offset`` months before the target."""
    total = TARGET_START.year * 12 + (TARGET_START.month - 1) - offset
    start = date(total // 12, total % 12 + 1, 1)
    following = date((total + 1) // 12, (total + 1) % 12 + 1, 1)
    return start, following - timedelta(days=1)


def add_testing_result(
    session: Session,
    *,
    period: tuple[date, date],
    value: Decimal | None,
    status: IndicatorValueStatus = IndicatorValueStatus.AVAILABLE,
    measure: Measure = Measure.TEST_POSITIVITY,
    computed_at: datetime | None = None,
    fingerprint: str | None = None,
) -> models.TestingSurveillanceResult:
    start, end = period
    row = models.TestingSurveillanceResult(
        measure=measure,
        geography_grain=GeographyGrain.FACILITY,
        facility_id=FACILITY_ID,
        period_start=start,
        period_end=end,
        period_grain=PeriodGrain.MONTH,
        numerator=None,
        denominator=None,
        value=value,
        value_status=status,
        input_fingerprint=fingerprint or uuid.uuid4().hex + uuid.uuid4().hex[:32],
        source_cutoff=datetime.now(UTC),
        engine_version="0.0.0-test",
        computed_at=computed_at or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def six_months(session: Session) -> None:
    """Six monthly positivity figures: 0.10 through 0.60, oldest first."""
    for offset, value in zip(range(6, 0, -1), "123456", strict=True):
        add_testing_result(
            session,
            period=month(offset),
            value=Decimal(f"0.{value}0"),
        )
    session.commit()


def results(session: Session) -> list[BaselineResult]:
    return list(session.execute(select(BaselineResult)).scalars().all())


def build_row(session: Session) -> BaselineBuild:
    return session.execute(select(BaselineBuild)).scalars().one()


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------
class TestMarsDoesNotDecideWhatNormalMeans:
    def test_without_an_approved_method_no_baseline_is_produced(
        self, session: Session, six_months: None
    ) -> None:
        report = BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        assert report.status is BaselineBuildStatus.NOT_CONFIGURED
        assert results(session) == []

    def test_the_refusal_names_every_missing_parameter(
        self, session: Session, six_months: None
    ) -> None:
        """An operator seeing no baselines needs a parameter name, not a shrug."""
        report = BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        for name in REQUIRED_PARAMETERS:
            assert name in report.missing_configuration
        stored = build_row(session)
        assert stored.missing_configuration is not None
        assert set(REQUIRED_PARAMETERS) <= set(stored.missing_configuration["parameters"])

    def test_the_refusal_is_recorded_rather_than_leaving_an_absence(
        self, session: Session, six_months: None
    ) -> None:
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        stored = build_row(session)
        assert stored.build_status is BaselineBuildStatus.NOT_CONFIGURED
        assert stored.method_version_id is None
        assert "programme decision" in (stored.notes or "")

    def test_a_refused_build_is_not_offered_as_a_baseline(
        self, session: Session, six_months: None
    ) -> None:
        """A caller comparing against it would be comparing against nothing."""
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        assert (
            latest_build(session, TARGET_START, TARGET_END, BaselineSeriesKind.TESTING_MEASURE)
            is None
        )

    def test_an_incomplete_approval_is_treated_as_absent(
        self, session: Session, six_months: None
    ) -> None:
        """An active version missing a parameter is not a usable method, and
        filling the gap here would mean choosing the parameter."""
        approve_method(
            session,
            parameters={"baseline_method": "historical_median", "history_periods": 6},
        )
        session.commit()
        report = BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        assert report.status is BaselineBuildStatus.NOT_CONFIGURED
        assert "minimum_history_periods" in report.missing_configuration

    def test_a_method_mars_has_not_implemented_is_refused_not_substituted(
        self, session: Session, six_months: None
    ) -> None:
        approve_method(
            session,
            parameters={
                "baseline_method": "kalman_filter",
                "history_periods": 6,
                "minimum_history_periods": 4,
                "minimum_completeness": 0.6,
            },
        )
        session.commit()
        report = BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        assert report.status is BaselineBuildStatus.NOT_CONFIGURED
        assert report.missing_configuration == ["baseline_method"]

    def test_a_nonsensical_minimum_is_refused(self, session: Session, six_months: None) -> None:
        """A minimum larger than the window can never be met, so every series
        would silently report insufficient history forever."""
        approve_method(session, history_periods=3, minimum_periods=9)
        session.commit()
        report = BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        assert report.status is BaselineBuildStatus.NOT_CONFIGURED
        assert "minimum_history_periods" in report.missing_configuration


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------
class TestExpectedValues:
    def test_a_median_over_six_months(self, session: Session, six_months: None) -> None:
        approve_method(session)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()

        row = next(r for r in results(session) if r.series_key == Measure.TEST_POSITIVITY.value)
        # 0.10 0.20 0.30 0.40 0.50 0.60 -> median 0.35
        assert row.value == Decimal("0.350000")
        assert row.sufficiency is BaselineSufficiency.SUFFICIENT
        assert row.value_status is IndicatorValueStatus.AVAILABLE
        assert row.history_periods_used == 6
        assert row.history_periods_available == 6

    def test_a_mean_uses_a_standard_deviation(self, session: Session, six_months: None) -> None:
        approve_method(session, method=BaselineMethod.HISTORICAL_MEAN)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.value == Decimal("0.350000")
        assert row.dispersion_measure is DispersionMeasure.STANDARD_DEVIATION
        assert row.dispersion_value is not None

    def test_a_median_uses_a_robust_dispersion(self, session: Session, six_months: None) -> None:
        approve_method(session)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.dispersion_measure is DispersionMeasure.MEDIAN_ABSOLUTE_DEVIATION
        # Deviations from 0.35: .25 .15 .05 .05 .15 .25 -> median .15
        assert row.dispersion_value == Decimal("0.150000")

    def test_without_an_approved_multiplier_there_is_no_band(
        self, session: Session, six_months: None
    ) -> None:
        """How wide an interval should be is a statistical choice a programme
        makes. A centre with no band is the honest output."""
        approve_method(session)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.uncertainty_lower is None
        assert row.uncertainty_upper is None

    def test_an_approved_multiplier_produces_a_two_ended_band(
        self, session: Session, six_months: None
    ) -> None:
        approve_method(session, uncertainty=2.0)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.uncertainty_lower == Decimal("0.050000")
        assert row.uncertainty_upper == Decimal("0.650000")

    def test_the_history_that_produced_it_is_recorded(
        self, session: Session, six_months: None
    ) -> None:
        """So a later explainability object can show the history rather than
        assert it."""
        approve_method(session)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        periods = row.contributing_periods["periods"]
        assert len(periods) == 6
        assert {p["value"] for p in periods} == {
            "0.100000",
            "0.200000",
            "0.300000",
            "0.400000",
            "0.500000",
            "0.600000",
        }


# ---------------------------------------------------------------------------
# Sufficiency
# ---------------------------------------------------------------------------
class TestThinHistoryProducesNoExpectation:
    def test_two_periods_produce_no_expected_value(self, session: Session) -> None:
        """An expectation drawn from two periods is worse than none, because a
        district can act on it."""
        for offset in (1, 2):
            add_testing_result(session, period=month(offset), value=Decimal("0.400000"))
        approve_method(session)
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.sufficiency is BaselineSufficiency.INSUFFICIENT_HISTORY
        assert row.value is None
        assert row.value_status is IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA
        assert "requires at least" in (row.notes or "")

    def test_a_new_facility_gets_no_history_not_a_zero(self, session: Session) -> None:
        approve_method(session)
        add_testing_result(
            session,
            period=month(1),
            value=None,
            status=IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR,
        )
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.sufficiency is BaselineSufficiency.NO_HISTORY
        assert row.value is None
        assert "not the same as an expected level of zero" in (row.notes or "")

    def test_unavailable_periods_are_excluded_with_their_reason(self, session: Session) -> None:
        """A baseline that silently drops half its history cannot be audited."""
        approve_method(session)
        for offset in range(1, 7):
            add_testing_result(
                session,
                period=month(offset),
                value=None if offset % 2 else Decimal("0.400000"),
                status=(
                    IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR
                    if offset % 2
                    else IndicatorValueStatus.AVAILABLE
                ),
            )
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        excluded = row.excluded_periods["periods"]
        assert len(excluded) == 3
        assert {e["reason"] for e in excluded} == {"unavailable_no_denominator"}

    def test_a_reported_zero_counts_towards_the_baseline(self, session: Session) -> None:
        """Zero positives out of forty tests is a figure. Treating it as a gap
        would make an outbreak look like a return to normal."""
        approve_method(session)
        for offset in range(1, 7):
            add_testing_result(session, period=month(offset), value=Decimal("0.000000"))
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.history_periods_used == 6
        assert row.value == Decimal("0.000000")
        assert row.sufficiency is BaselineSufficiency.SUFFICIENT

    def test_completeness_below_the_approved_minimum_yields_no_expectation(
        self, session: Session
    ) -> None:
        approve_method(session, minimum_periods=1, minimum_completeness=0.9)
        for offset in (1, 2, 3):
            add_testing_result(session, period=month(offset), value=Decimal("0.400000"))
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.sufficiency is BaselineSufficiency.INSUFFICIENT_COMPLETENESS
        assert row.value is None
        assert "below the approved minimum" in (row.notes or "")


# ---------------------------------------------------------------------------
# Immutability and lineage
# ---------------------------------------------------------------------------
class TestHistoryIsReadAsItStands:
    def test_a_superseded_figure_does_not_vote_twice(self, session: Session) -> None:
        """Results are immutable, so one period can hold several rows. The
        latest is the one in force; counting both would let a corrected figure
        and the figure it corrected both shape the expectation."""
        approve_method(session)
        earlier = datetime.now(UTC) - timedelta(days=2)
        for offset in range(1, 7):
            add_testing_result(
                session, period=month(offset), value=Decimal("0.900000"), computed_at=earlier
            )
        for offset in range(1, 7):
            add_testing_result(session, period=month(offset), value=Decimal("0.100000"))
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.history_periods_used == 6
        assert row.value == Decimal("0.100000")

    def test_the_seasonal_method_reaches_back_by_years(self, session: Session) -> None:
        """Comparing July against June flags the season. Comparing July against
        previous Julys is the comparison a programme wants."""
        approve_method(
            session,
            method=BaselineMethod.SEASONAL_PERIOD_OF_YEAR_MEDIAN,
            history_periods=3,
            minimum_periods=2,
        )
        for year in (2023, 2024, 2025):
            add_testing_result(
                session,
                period=(date(year, 7, 1), date(year, 7, 31)),
                value=Decimal("0.500000"),
            )
        # A June figure that must not be used.
        add_testing_result(session, period=month(1), value=Decimal("0.010000"))
        session.commit()

        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        row = results(session)[0]
        assert row.value == Decimal("0.500000")
        assert row.history_periods_used == 3
        starts = {p["period_start"] for p in row.contributing_periods["periods"]}
        assert starts == {"2023-07-01", "2024-07-01", "2025-07-01"}

    def test_a_completed_build_is_offered_and_carries_its_method(
        self, session: Session, six_months: None
    ) -> None:
        version = approve_method(session)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        found = latest_build(session, TARGET_START, TARGET_END, BaselineSeriesKind.TESTING_MEASURE)
        assert found is not None
        assert found.method_version_id == version.id
        assert found.baseline_method is BaselineMethod.HISTORICAL_MEDIAN
        assert found.history_periods == TEST_HISTORY_PERIODS

    def test_every_row_says_what_a_baseline_is_not(
        self, session: Session, six_months: None
    ) -> None:
        approve_method(session)
        session.commit()
        BaselineEngine(session).build(
            TARGET_START, TARGET_END, series_kind=BaselineSeriesKind.TESTING_MEASURE
        )
        session.commit()
        for row in results(session):
            assert (
                "not what should be"
                in row.quality_context["domain_limit"].replace(
                    "not\nwhat should be", "not what should be"
                )
                or "not\nwhat should be" in row.quality_context["domain_limit"]
            )


# ---------------------------------------------------------------------------
# What the schema itself refuses
# ---------------------------------------------------------------------------
class TestTheDatabaseHoldsTheLine:
    def test_an_expectation_without_sufficient_history_is_rejected(self, session: Session) -> None:
        """Not only the engine: any future code path is held to the same rule."""
        build = BaselineBuild(
            build_status=BaselineBuildStatus.NOT_CONFIGURED,
            series_kind=BaselineSeriesKind.TESTING_MEASURE,
            target_period_start=TARGET_START,
            target_period_end=TARGET_END,
            period_grain=PeriodGrain.MONTH,
            missing_configuration={"parameters": ["baseline_method"]},
            engine_version="0.0.0-test",
            started_at=datetime.now(UTC),
        )
        session.add(build)
        session.flush()

        session.add(
            BaselineResult(
                baseline_build_id=build.id,
                series_kind=BaselineSeriesKind.TESTING_MEASURE,
                series_key="test_positivity",
                baseline_method=BaselineMethod.HISTORICAL_MEDIAN,
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_ID,
                period_start=TARGET_START,
                period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                value=Decimal("0.400000"),
                value_status=IndicatorValueStatus.AVAILABLE,
                sufficiency=BaselineSufficiency.INSUFFICIENT_HISTORY,
                dispersion_measure=DispersionMeasure.NONE,
                history_periods_available=6,
                history_periods_used=2,
                input_fingerprint="a" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                computed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="sufficiency_matches_value_status"):
            session.flush()

    def test_a_refused_build_must_name_what_is_missing(self, session: Session) -> None:
        session.add(
            BaselineBuild(
                build_status=BaselineBuildStatus.NOT_CONFIGURED,
                series_kind=BaselineSeriesKind.INDICATOR,
                target_period_start=TARGET_START,
                target_period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                missing_configuration=None,
                engine_version="0.0.0-test",
                started_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="refusals_name_what_is_missing"):
            session.flush()

    def test_a_refusal_naming_json_null_is_rejected(self, session: Session) -> None:
        """A JSONB column given Python ``None`` is stored as JSON ``null``,
        which is not SQL NULL. Without the type test the constraint would
        accept a refusal that names nothing - the exact row it exists to
        refuse, arriving by the ordinary ORM path."""
        statement = text(
            """
            INSERT INTO mars_analytics.baseline_build
                (id, build_status, series_kind, target_period_start, target_period_end,
                 period_grain, missing_configuration, engine_version, started_at,
                 series_evaluated, results_written, insufficient_history,
                 insufficient_completeness, created_at, updated_at)
            VALUES (gen_random_uuid(), 'not_configured', 'indicator',
                    :start, :end, 'month', 'null'::jsonb, '0.0.0-test', now(),
                    0, 0, 0, 0, now(), now())
            """
        )
        with pytest.raises(IntegrityError, match="refusals_name_what_is_missing"):
            session.execute(statement, {"start": TARGET_START, "end": TARGET_END})

    def test_a_completed_build_must_carry_its_method(self, session: Session) -> None:
        session.add(
            BaselineBuild(
                build_status=BaselineBuildStatus.COMPLETED,
                series_kind=BaselineSeriesKind.INDICATOR,
                target_period_start=TARGET_START,
                target_period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                method_version_id=None,
                engine_version="0.0.0-test",
                started_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="completed_builds_carry_their_method"):
            session.flush()

    def test_half_a_band_is_rejected(self, session: Session) -> None:
        """A one-ended band would read as a limit, which is a different claim."""
        build = BaselineBuild(
            build_status=BaselineBuildStatus.NOT_CONFIGURED,
            series_kind=BaselineSeriesKind.INDICATOR,
            target_period_start=TARGET_START,
            target_period_end=TARGET_END,
            period_grain=PeriodGrain.MONTH,
            missing_configuration={"parameters": []},
            engine_version="0.0.0-test",
            started_at=datetime.now(UTC),
        )
        session.add(build)
        session.flush()
        session.add(
            BaselineResult(
                baseline_build_id=build.id,
                series_kind=BaselineSeriesKind.INDICATOR,
                series_key="anything",
                baseline_method=BaselineMethod.HISTORICAL_MEDIAN,
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_ID,
                period_start=TARGET_START,
                period_end=TARGET_END,
                period_grain=PeriodGrain.MONTH,
                value=Decimal("0.400000"),
                value_status=IndicatorValueStatus.AVAILABLE,
                sufficiency=BaselineSufficiency.SUFFICIENT,
                dispersion_measure=DispersionMeasure.NONE,
                uncertainty_lower=Decimal("0.100000"),
                uncertainty_upper=None,
                history_periods_available=6,
                history_periods_used=6,
                input_fingerprint="b" * 64,
                source_cutoff=datetime.now(UTC),
                engine_version="0.0.0-test",
                computed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="band_has_both_ends"):
            session.flush()
