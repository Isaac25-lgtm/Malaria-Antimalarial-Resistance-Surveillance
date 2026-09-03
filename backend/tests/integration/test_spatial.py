"""Geographic aggregation, hotspots and map privacy against live PostgreSQL.

Two districts, three facilities, and figures chosen so every roll-up is
checkable by hand.

What these tests protect:

* that a district rate is recomputed from its parts and is never the mean of
  its facilities' rates;
* that where care was given and where people live stay separate;
* that a hotspot cannot exist without a method, and that a red-free map can
  still say which areas were examined;
* that a map cell's six possible meanings - a value, missing, suppressed,
  unavailable, not configured, outside scope - never collapse into one blank;
* that MARS refuses patient-derived detail without an approved privacy policy,
  says exactly what is missing, and does not answer with an empty map.

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
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics.baseline import BASELINE_METHOD_CODE
from mars.analytics.geographic import GeographicAggregationEngine
from mars.analytics.hotspot import (
    HOTSPOT_DEFINITION_CODE,
    PERSISTENCE_PARAMETER,
    REQUIRED_PARAMETERS,
    HotspotEngine,
)
from mars.core.errors import GeographyScopeDeniedError
from mars.domain import surveillance as models
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    AnomalyDetectionMethod,
    AttendanceType,
    BaselineMethod,
    BaselineSeriesKind,
    DateAssignmentMethod,
    FeverStatus,
    GeographyGrain,
    HotspotOutcome,
    IndicatorValueStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    MethodKind,
    PatientCategory,
    PeriodGrain,
    Sex,
    SpatialAggregationBasis,
    SpatialCellStatus,
    SpatialRunStatus,
)
from mars.domain.enums import TestingMeasure as Measure
from mars.domain.governance import (
    ConfigurationKey,
    ConfigurationVersion,
    MethodDefinition,
    MethodVersion,
)
from mars.domain.spatial import GeographicAggregationResult, HotspotResult
from mars.services.spatial_availability import PRIVACY_POLICY_KEY, spatial_cells

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("ee500000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("ee500000-0000-4000-8000-000000000010")
DISTRICT_A = uuid.UUID("ee500000-0000-4000-8000-000000000011")
DISTRICT_B = uuid.UUID("ee500000-0000-4000-8000-000000000012")
DISTRICT_C = uuid.UUID("ee500000-0000-4000-8000-000000000013")
SUBCOUNTY_A1 = uuid.UUID("ee500000-0000-4000-8000-000000000021")
ORG_UNIT_ID = uuid.UUID("ee500000-0000-4000-8000-000000000030")
FACILITY_A1 = uuid.UUID("ee500000-0000-4000-8000-000000000001")
FACILITY_A2 = uuid.UUID("ee500000-0000-4000-8000-000000000002")
FACILITY_B1 = uuid.UUID("ee500000-0000-4000-8000-000000000003")
FACILITY_B2 = uuid.UUID("ee500000-0000-4000-8000-000000000004")

PATH_A = "UG/941"
PATH_B = "UG/942"

TARGET_START = date(2026, 7, 1)
TARGET_END = date(2026, 7, 31)
SERIES = BaselineSeriesKind.TESTING_MEASURE

#: Test-only values. The tests asserting MARS refuses without them are what
#: stops these becoming production defaults.
TEST_MINIMUM_CELL = 5
TEST_THRESHOLD = 2.0
TEST_MINIMUM_CASES = 10
TEST_MINIMUM_COMPLETENESS = 0.5


@pytest.fixture(scope="module")
def spatial_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(spatial_db: Engine) -> None:
    with spatial_db.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-SPAT-0001', 'Spatial fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_A, "district", "941", "Alpha", COUNTRY_ID, 1, PATH_A),
            (DISTRICT_B, "district", "942", "Bravo", COUNTRY_ID, 1, PATH_B),
            (DISTRICT_C, "district", "943", "Charlie", COUNTRY_ID, 1, "UG/943"),
            (SUBCOUNTY_A1, "subcounty", "941-1", "Alpha North", DISTRICT_A, 2, f"{PATH_A}/1"),
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
                VALUES (:id, 'district_health_office', 'DHO-941', 'Alpha DHO',
                        'alpha dho', 0, 'DHO-941', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )
        for facility_id, code, name, district, subcounty in (
            (FACILITY_A1, "HF-941A", "Alpha HC", DISTRICT_A, SUBCOUNTY_A1),
            (FACILITY_A2, "HF-941B", "Alpha Clinic", DISTRICT_A, SUBCOUNTY_A1),
            (FACILITY_B1, "HF-942A", "Bravo HC", DISTRICT_B, None),
            (FACILITY_B2, "HF-942B", "Bravo Clinic", DISTRICT_B, None),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.facility
                        (id, organisation_unit_id, district_geography_unit_id,
                         subcounty_geography_unit_id, code, raw_name, normalised_name,
                         facility_level, ownership, coordinate_validated, is_active,
                         is_synthetic, created_at, updated_at)
                    VALUES (:id, :org, :district, :subcounty, :code, :name, :normalised,
                            'hc_iii', 'government', false, true, true, now(), now())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": facility_id,
                    "org": ORG_UNIT_ID,
                    "district": district,
                    "subcounty": subcounty,
                    "code": code,
                    "name": name,
                    "normalised": name.lower(),
                },
            )


@pytest.fixture
def session(spatial_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=spatial_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(spatial_db: Engine) -> Iterator[None]:
    yield
    with spatial_db.begin() as connection:
        for table in (
            "mars_analytics.hotspot_result",
            "mars_analytics.geographic_aggregation_result",
            "mars_analytics.spatial_run",
            "mars_analytics.testing_surveillance_result",
            "mars_core.opd_encounter_test",
            "mars_core.opd_encounter",
            "mars_governance.configuration_version",
            "mars_governance.configuration_key",
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


def add_facility_result(
    session: Session,
    *,
    facility_id: uuid.UUID,
    numerator: int | None,
    denominator: int | None,
    period: tuple[date, date] = (TARGET_START, TARGET_END),
    measure: Measure = Measure.TEST_POSITIVITY,
) -> None:
    start, end = period
    available = numerator is not None and denominator not in (None, 0)
    session.add(
        models.TestingSurveillanceResult(
            measure=measure,
            geography_grain=GeographyGrain.FACILITY,
            facility_id=facility_id,
            period_start=start,
            period_end=end,
            period_grain=PeriodGrain.MONTH,
            numerator=numerator,
            denominator=denominator,
            value=(
                (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001"))
                if available and numerator is not None and denominator
                else None
            ),
            value_status=(
                IndicatorValueStatus.AVAILABLE
                if available
                else IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR
            ),
            input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            source_cutoff=datetime.now(UTC),
            engine_version="0.0.0-test",
            computed_at=datetime.now(UTC),
        )
    )
    session.flush()


def add_encounter(
    session: Session,
    *,
    facility_id: uuid.UUID,
    residence_district: uuid.UUID | None,
    day: date,
    result: MalariaTestResult,
    method: MalariaTestMethod = MalariaTestMethod.RDT,
) -> None:
    encounter = OpdEncounter(
        facility_id=facility_id,
        patient_reference_id=None,
        encounter_date=day,
        date_assignment_method=DateAssignmentMethod.SOURCE_SUPPLIED,
        sex=Sex.FEMALE,
        patient_category=PatientCategory.NATIONAL,
        attendance_type=AttendanceType.NEW_ATTENDANCE,
        fever_present=FeverStatus.YES,
        residence_district_id=residence_district,
        source_system="test",
        source_row_reference=f"spat-{uuid.uuid4().hex[:12]}",
    )
    encounter.tests = [OpdEncounterTest(sequence=1, method=method, result=result)]
    session.add(encounter)
    session.flush()


def approve_baseline(session: Session, *, minimum_periods: int = 4) -> MethodVersion:
    definition = MethodDefinition(
        code=BASELINE_METHOD_CODE,
        label="Temporal baseline (TEST ONLY)",
        kind=MethodKind.TEMPORAL_BASELINE,
        purpose="Test fixture. Not programme guidance.",
    )
    session.add(definition)
    session.flush()
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only baseline parameters.",
        parameters={
            "baseline_method": BaselineMethod.HISTORICAL_MEDIAN.value,
            "history_periods": 6,
            "minimum_history_periods": minimum_periods,
            "minimum_completeness": 0.5,
        },
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def approve_hotspot_definition(
    session: Session,
    *,
    threshold: float = TEST_THRESHOLD,
    minimum_cases: int = TEST_MINIMUM_CASES,
    minimum_completeness: float = TEST_MINIMUM_COMPLETENESS,
    persistence: int | None = None,
    parameters: dict[str, object] | None = None,
) -> MethodVersion:
    definition = MethodDefinition(
        code=HOTSPOT_DEFINITION_CODE,
        label="Hotspot definition (TEST ONLY)",
        kind=MethodKind.SPATIAL_METHOD,
        purpose="Test fixture. Not programme guidance.",
    )
    session.add(definition)
    session.flush()
    values: dict[str, object] = {
        "detection_method": AnomalyDetectionMethod.ROBUST_Z_SCORE.value,
        "deviation_threshold": threshold,
        "minimum_case_count": minimum_cases,
        "minimum_completeness": minimum_completeness,
    }
    if persistence is not None:
        values[PERSISTENCE_PARAMETER] = persistence
    if parameters is not None:
        values = parameters
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only hotspot definition.",
        parameters=values,
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def approve_privacy_policy(
    session: Session,
    *,
    minimum_cell_count: int | None = TEST_MINIMUM_CELL,
    minimum_aggregation_level: str | None = "district",
) -> ConfigurationVersion:
    key = ConfigurationKey(
        key=PRIVACY_POLICY_KEY,
        label="Spatial privacy policy (TEST ONLY)",
        description="Test fixture. Not programme guidance.",
        category="privacy",
        requires_programme_approval=True,
    )
    session.add(key)
    session.flush()
    value: dict[str, object] = {}
    if minimum_cell_count is not None:
        value["minimum_cell_count"] = minimum_cell_count
    if minimum_aggregation_level is not None:
        value["minimum_aggregation_level"] = minimum_aggregation_level
    version = ConfigurationVersion(
        configuration_key_id=key.id,
        version_number=1,
        status=LifecycleStatus.ACTIVE,
        value=value,
        value_checksum="c" * 64,
        effective_from=TARGET_START,
        reason_for_change="test fixture",
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def aggregate(
    session: Session,
    *,
    target: date = TARGET_START,
    grain: GeographyGrain = GeographyGrain.DISTRICT,
    basis: SpatialAggregationBasis = SpatialAggregationBasis.FACILITY_LOCATION,
):
    end = month(-1, of=target)[0] - timedelta(days=1)
    return GeographicAggregationEngine(session).aggregate(
        target,
        end,
        series_kind=SERIES,
        geography_grain=grain,
        basis=basis,
        boundary_version_id=BOUNDARY_VERSION_ID,
    )


def evaluate(session: Session, *, target: date = TARGET_START):
    end = month(-1, of=target)[0] - timedelta(days=1)
    return HotspotEngine(session).evaluate(
        target,
        end,
        series_kind=SERIES,
        geography_grain=GeographyGrain.DISTRICT,
        basis=SpatialAggregationBasis.FACILITY_LOCATION,
        boundary_version_id=BOUNDARY_VERSION_ID,
    )


def aggregations(session: Session) -> dict[uuid.UUID, GeographicAggregationResult]:
    rows = session.execute(select(GeographicAggregationResult)).scalars().all()
    return {row.geography_unit_id: row for row in rows}


def hotspots(session: Session) -> dict[uuid.UUID, HotspotResult]:
    rows = session.execute(select(HotspotResult)).scalars().all()
    return {row.geography_unit_id: row for row in rows}


def cells(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {cell["preferred_code"]: cell for cell in payload["cells"]}  # type: ignore[index,union-attr]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
class TestRecomputeNeverAverage:
    def test_a_district_rate_is_its_own_positives_over_its_own_tests(
        self, session: Session
    ) -> None:
        """The mean of the facility rates would be 0.40, which would give a
        four-test clinic the same voice as a hundred-test hospital."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        add_facility_result(session, facility_id=FACILITY_A2, numerator=2, denominator=4)
        session.commit()

        aggregate(session)
        session.commit()
        row = aggregations(session)[DISTRICT_A]
        assert (row.numerator, row.denominator) == (32, 104)
        assert row.value == Decimal("0.307692")
        assert row.value != Decimal("0.400000")

    def test_completeness_is_recorded_against_the_facility_register(self, session: Session) -> None:
        """A district figure built from one of its two facilities is not a
        district figure, and the row says so."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        add_facility_result(session, facility_id=FACILITY_B1, numerator=10, denominator=50)
        session.commit()

        aggregate(session)
        session.commit()
        rows = aggregations(session)
        assert rows[DISTRICT_A].contributing_facilities == 1
        assert rows[DISTRICT_A].expected_facilities == 2
        assert rows[DISTRICT_A].reporting_completeness == Decimal("0.5000")

    def test_a_district_with_no_denominator_has_no_rate(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=5, denominator=0)
        session.commit()

        aggregate(session)
        session.commit()
        row = aggregations(session)[DISTRICT_A]
        assert row.value is None
        assert row.value_status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_reported_zero_is_a_figure(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=0, denominator=80)
        session.commit()

        aggregate(session)
        session.commit()
        row = aggregations(session)[DISTRICT_A]
        assert row.value == Decimal("0.000000")
        assert row.value_status is IndicatorValueStatus.AVAILABLE

    def test_subcounty_rolls_up_only_where_the_source_coded_one(self, session: Session) -> None:
        """Bravo's facilities have no subcounty, so no Bravo subcounty row
        exists. An invented one would be a place MARS made up."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        add_facility_result(session, facility_id=FACILITY_B1, numerator=10, denominator=50)
        session.commit()

        aggregate(session, grain=GeographyGrain.SUBCOUNTY)
        session.commit()
        rows = aggregations(session)
        assert set(rows) == {SUBCOUNTY_A1}

    def test_an_aggregate_carries_the_method_that_made_it(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()
        context = aggregations(session)[DISTRICT_A].quality_context
        assert "Not the mean of the facility values" in context["method"]


class TestResidenceIsNotFacilityLocation:
    def test_residence_counts_where_people_live(self, session: Session) -> None:
        """Both patients were seen in Alpha; one lives in Bravo. A facility map
        puts both in Alpha, a residence map puts one in each, and the questions
        they answer are different."""
        add_encounter(
            session,
            facility_id=FACILITY_A1,
            residence_district=DISTRICT_A,
            day=date(2026, 7, 3),
            result=MalariaTestResult.POSITIVE,
        )
        add_encounter(
            session,
            facility_id=FACILITY_A1,
            residence_district=DISTRICT_B,
            day=date(2026, 7, 4),
            result=MalariaTestResult.POSITIVE,
        )
        session.commit()

        aggregate(session, basis=SpatialAggregationBasis.RESIDENCE)
        session.commit()
        rows = {
            (row.geography_unit_id, row.series_key): row
            for row in session.execute(select(GeographicAggregationResult)).scalars().all()
        }
        assert rows[(DISTRICT_A, Measure.TEST_POSITIVITY.value)].numerator == 1
        assert rows[(DISTRICT_B, Measure.TEST_POSITIVITY.value)].numerator == 1

    def test_unresolved_residence_is_counted_not_dropped(self, session: Session) -> None:
        """Its absence always makes a residence map look emptier than the
        truth."""
        add_encounter(
            session,
            facility_id=FACILITY_A1,
            residence_district=DISTRICT_A,
            day=date(2026, 7, 3),
            result=MalariaTestResult.POSITIVE,
        )
        add_encounter(
            session,
            facility_id=FACILITY_A1,
            residence_district=None,
            day=date(2026, 7, 5),
            result=MalariaTestResult.POSITIVE,
        )
        session.commit()

        report = aggregate(session, basis=SpatialAggregationBasis.RESIDENCE)
        session.commit()
        assert report.unresolved_contributions == 1
        assert all(
            row.unresolved_contributions == 1
            for row in session.execute(select(GeographicAggregationResult)).scalars().all()
        )

    def test_the_two_bases_produce_separate_rows(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        add_encounter(
            session,
            facility_id=FACILITY_A1,
            residence_district=DISTRICT_A,
            day=date(2026, 7, 3),
            result=MalariaTestResult.POSITIVE,
        )
        session.commit()

        aggregate(session)
        aggregate(session, basis=SpatialAggregationBasis.RESIDENCE)
        session.commit()
        bases = {
            row.aggregation_basis
            for row in session.execute(select(GeographicAggregationResult)).scalars().all()
        }
        assert bases == {
            SpatialAggregationBasis.FACILITY_LOCATION,
            SpatialAggregationBasis.RESIDENCE,
        }

    def test_a_series_with_no_residence_is_named_not_silently_omitted(
        self, session: Session
    ) -> None:
        report = GeographicAggregationEngine(session).aggregate(
            TARGET_START,
            TARGET_END,
            series_kind=BaselineSeriesKind.TREATMENT_MEASURE,
            geography_grain=GeographyGrain.DISTRICT,
            basis=SpatialAggregationBasis.RESIDENCE,
            boundary_version_id=BOUNDARY_VERSION_ID,
        )
        session.commit()
        assert report.measures_not_available_on_this_basis == ["treatment_measure"]
        assert "computed from encounters" in (report.notes or "")


# ---------------------------------------------------------------------------
# Hotspots
# ---------------------------------------------------------------------------
class TestAHotspotMustHaveAMethod:
    def _history(self, session: Session, *, values: list[tuple[int, int]]) -> None:
        """Six months of stable Alpha figures, oldest first."""
        for offset, (numerator, denominator) in zip(range(6, 0, -1), values, strict=True):
            add_facility_result(
                session,
                facility_id=FACILITY_A1,
                numerator=numerator,
                denominator=denominator,
                period=month(offset),
            )
            add_facility_result(
                session,
                facility_id=FACILITY_A2,
                numerator=numerator,
                denominator=denominator,
                period=month(offset),
            )
        session.commit()
        for offset in range(6, 0, -1):
            aggregate(session, target=month(offset)[0])
        session.commit()

    def test_without_an_approved_definition_no_area_is_judged(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=90, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        report = evaluate(session)
        session.commit()
        assert report.status is SpatialRunStatus.NOT_CONFIGURED
        assert hotspots(session) == {}

    def test_the_refusal_names_every_missing_parameter(self, session: Session) -> None:
        report = evaluate(session)
        session.commit()
        for name in REQUIRED_PARAMETERS:
            assert name in report.missing_configuration
        assert "red colour" in (report.notes or "")

    def test_a_definition_without_a_baseline_method_is_still_a_refusal(
        self, session: Session
    ) -> None:
        """A threshold with nothing to measure against is not a definition."""
        approve_hotspot_definition(session)
        session.commit()
        report = evaluate(session)
        session.commit()
        assert report.status is SpatialRunStatus.NOT_CONFIGURED
        assert any(name.startswith("baseline:") for name in report.missing_configuration)

    def test_a_clear_departure_is_a_hotspot_with_its_evidence(self, session: Session) -> None:
        approve_baseline(session)
        approve_hotspot_definition(session)
        self._history(session, values=[(30, 100), (31, 100)] * 3)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=90, denominator=100)
        add_facility_result(session, facility_id=FACILITY_A2, numerator=90, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        report = evaluate(session)
        session.commit()
        row = hotspots(session)[DISTRICT_A]
        assert row.outcome is HotspotOutcome.HOTSPOT
        assert row.method_version_id is not None
        assert row.baseline_method_version_id is not None
        assert row.deviation_threshold == Decimal("2.000000")
        assert row.history_periods_used == 6
        assert report.hotspots == 1

    def test_an_ordinary_area_is_examined_and_not_a_hotspot(self, session: Session) -> None:
        approve_baseline(session)
        approve_hotspot_definition(session)
        self._history(session, values=[(30, 100), (31, 100)] * 3)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        add_facility_result(session, facility_id=FACILITY_A2, numerator=31, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        evaluate(session)
        session.commit()
        row = hotspots(session)[DISTRICT_A]
        assert row.outcome is HotspotOutcome.NOT_HOTSPOT
        # Examined means examined: the evidence is on the row.
        assert row.expected_value is not None
        assert row.baseline_method_version_id is not None

    def test_an_area_with_no_history_is_not_called_quiet(self, session: Session) -> None:
        approve_baseline(session)
        approve_hotspot_definition(session)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=90, denominator=100)
        add_facility_result(session, facility_id=FACILITY_A2, numerator=90, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        evaluate(session)
        session.commit()
        row = hotspots(session)[DISTRICT_A]
        assert row.outcome is HotspotOutcome.NOT_EVALUATED_NO_BASELINE
        assert row.expected_value is None
        assert "not the same as nothing being unusual" in (row.notes or "")

    def test_incomplete_reporting_is_a_gate_not_a_footnote(self, session: Session) -> None:
        """A figure built from part of an area does not describe the area, and
        colouring it red or green would both mislead."""
        approve_baseline(session)
        approve_hotspot_definition(session, minimum_completeness=0.9)
        self._history(session, values=[(30, 100), (31, 100)] * 3)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=90, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        evaluate(session)
        session.commit()
        row = hotspots(session)[DISTRICT_A]
        assert row.outcome is HotspotOutcome.NOT_EVALUATED_INCOMPLETE_REPORTING
        assert "does not describe the area" in (row.notes or "")

    def test_too_few_cases_is_its_own_outcome(self, session: Session) -> None:
        approve_baseline(session)
        approve_hotspot_definition(session, minimum_cases=50)
        self._history(session, values=[(30, 100), (31, 100)] * 3)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=5, denominator=10)
        add_facility_result(session, facility_id=FACILITY_A2, numerator=5, denominator=10)
        session.commit()
        aggregate(session)
        session.commit()

        evaluate(session)
        session.commit()
        assert (
            hotspots(session)[DISTRICT_A].outcome
            is HotspotOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT
        )

    def test_every_row_carries_the_interpretation_limit(self, session: Session) -> None:
        approve_baseline(session)
        approve_hotspot_definition(session)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=90, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()
        evaluate(session)
        session.commit()

        for row in hotspots(session).values():
            limit = row.quality_context["interpretation_limit"]
            assert "area worth visiting" in limit
            assert "resistance" in limit

    def test_persistence_is_counted_and_labelled_only_under_a_rule(self, session: Session) -> None:
        approve_baseline(session)
        approve_hotspot_definition(session, persistence=2)
        self._history(session, values=[(30, 100), (31, 100)] * 3)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=90, denominator=100)
        add_facility_result(session, facility_id=FACILITY_A2, numerator=90, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()
        evaluate(session)
        session.commit()

        row = hotspots(session)[DISTRICT_A]
        assert row.consecutive_periods == 1
        assert row.first_detected_period_start == TARGET_START
        assert row.is_persistent is False
        assert row.persistence_periods == 2


# ---------------------------------------------------------------------------
# Map privacy
# ---------------------------------------------------------------------------
def layer(session: Session, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "series_kind": SERIES,
        "series_key": Measure.TEST_POSITIVITY.value,
        "period_start": TARGET_START,
        "geography_grain": GeographyGrain.DISTRICT,
        "basis": SpatialAggregationBasis.FACILITY_LOCATION,
        "boundary_version_id": BOUNDARY_VERSION_ID,
    }
    kwargs.update(overrides)
    return spatial_cells(session, **kwargs)  # type: ignore[arg-type]


class TestUnsafeDetailIsRefusedNotFabricated:
    def test_without_an_approved_policy_the_layer_is_withheld(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        payload = layer(session)
        assert payload["status"] == SpatialCellStatus.NOT_CONFIGURED.value
        assert payload["reason"] == "privacy_configuration_required"

    def test_the_refusal_names_the_missing_keys(self, session: Session) -> None:
        payload = layer(session)
        assert "minimum_cell_count" in payload["missing_configuration"]
        assert "minimum_aggregation_level" in payload["missing_configuration"]

    def test_the_refusal_returns_no_cells_at_all(self, session: Session) -> None:
        """Not an empty list either. An empty map is read as an absence of
        disease, which is exactly the claim MARS must not make."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        payload = layer(session)
        assert "cells" not in payload
        assert "not about malaria" in str(payload["note"])

    def test_a_partly_approved_policy_reports_the_level_it_does_have(
        self, session: Session
    ) -> None:
        """Determinable without inventing a rule: the level was approved even
        though the cell count was not."""
        approve_privacy_policy(session, minimum_cell_count=None)
        session.commit()

        payload = layer(session)
        assert payload["status"] == SpatialCellStatus.NOT_CONFIGURED.value
        assert payload["missing_configuration"] == ["minimum_cell_count"]
        assert payload["highest_safe_geography"] == "district"

    def test_with_no_policy_at_all_no_safe_geography_is_guessed(self, session: Session) -> None:
        payload = layer(session)
        assert payload["highest_safe_geography"] is None

    def test_a_grain_finer_than_the_approved_minimum_is_refused(self, session: Session) -> None:
        approve_privacy_policy(session)
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        session.commit()
        aggregate(session, grain=GeographyGrain.SUBCOUNTY)
        session.commit()

        payload = layer(session, geography_grain=GeographyGrain.SUBCOUNTY)
        assert payload["reason"] == "geography_finer_than_approved_minimum"
        assert payload["highest_safe_geography"] == "district"
        assert "cells" not in payload


class TestEveryCellSaysWhyItLooksThatWay:
    @pytest.fixture(autouse=True)
    def _policy(self, session: Session) -> None:
        approve_privacy_policy(session)
        session.commit()

    def test_a_value_including_a_zero_is_served(self, session: Session) -> None:
        """A cell counting nobody has nobody in it to identify. Suppressing it
        would hide the districts with no malaria."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=0, denominator=80)
        session.commit()
        aggregate(session)
        session.commit()

        cell = cells(layer(session))["941"]
        assert cell["status"] == SpatialCellStatus.AVAILABLE.value
        assert cell["value"] == "0.000000"

    def test_a_small_cell_is_suppressed_and_says_so(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=3, denominator=80)
        session.commit()
        aggregate(session)
        session.commit()

        cell = cells(layer(session))["941"]
        assert cell["status"] == SpatialCellStatus.SUPPRESSED.value
        assert cell["reason"] == "below_minimum_cell_count"
        assert cell["value"] is None
        assert cell["minimum_cell_count"] == TEST_MINIMUM_CELL

    def test_a_district_that_reported_nothing_is_missing_not_zero(self, session: Session) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        cell = cells(layer(session))["942"]
        assert cell["status"] == SpatialCellStatus.MISSING.value
        assert cell["value"] is None

    def test_a_measure_with_no_denominator_is_unavailable_not_missing(
        self, session: Session
    ) -> None:
        add_facility_result(session, facility_id=FACILITY_A1, numerator=5, denominator=0)
        session.commit()
        aggregate(session)
        session.commit()

        cell = cells(layer(session))["941"]
        assert cell["status"] == SpatialCellStatus.UNAVAILABLE.value
        assert cell["reason"] == "unavailable_no_denominator"

    def test_a_district_outside_scope_is_marked_not_blanked(self, session: Session) -> None:
        """The district's existence is public geography; its figure is not."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=30, denominator=100)
        add_facility_result(session, facility_id=FACILITY_B1, numerator=40, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        served = cells(layer(session, authorised_paths=(PATH_A,)))
        assert served["941"]["status"] == SpatialCellStatus.AVAILABLE.value
        assert served["942"]["status"] == SpatialCellStatus.OUTSIDE_SCOPE.value
        assert served["942"]["value"] is None

    def test_naming_an_out_of_scope_unit_is_rejected_not_filtered(self, session: Session) -> None:
        """A caller learns their request was refused rather than quietly
        receiving less than they asked for."""
        with pytest.raises(GeographyScopeDeniedError):
            layer(
                session,
                authorised_paths=(PATH_A,),
                requested_unit_ids=(DISTRICT_A, DISTRICT_B),
            )

    def test_all_six_meanings_can_appear_on_one_map(self, session: Session) -> None:
        """The point of the whole module: one blank colour would collapse
        five of these into a sixth."""
        add_facility_result(session, facility_id=FACILITY_A1, numerator=3, denominator=80)
        add_facility_result(session, facility_id=FACILITY_B1, numerator=40, denominator=100)
        session.commit()
        aggregate(session)
        session.commit()

        payload = layer(session, authorised_paths=(PATH_A, "UG/943"))
        served = cells(payload)
        assert served["941"]["status"] == SpatialCellStatus.SUPPRESSED.value
        assert served["942"]["status"] == SpatialCellStatus.OUTSIDE_SCOPE.value
        assert served["943"]["status"] == SpatialCellStatus.MISSING.value
        assert payload["status_counts"] == {"missing": 1, "outside_scope": 1, "suppressed": 1}

    def test_the_layer_says_what_a_blank_cell_does_not_mean(self, session: Session) -> None:
        payload = layer(session)
        assert "never an assertion that there is no malaria there" in str(payload["note"])
