"""Recurrence surveillance against live PostgreSQL.

The episodes below are built so every count is knowable by hand.

What these tests are really protecting:

* that no measure is ever presented as treatment failure or resistance, and
  that the statement saying so travels on every row;
* that facility of care and residence geography stay separate;
* that unlinked encounters are reported rather than silently making recurrence
  look rarer;
* that interval bands come from governed configuration and are absent until
  approved, rather than being invented here.

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
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics.episodes import EPISODE_RULE_CODE, REQUIRED_PARAMETER, EpisodeEngine
from mars.analytics.recurrence import (
    INTERVAL_BANDS_KEY,
    RecurrenceEngine,
    latest_build,
)
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterPrescription,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.enums import (
    AttendanceType,
    DateAssignmentMethod,
    FeverStatus,
    IndicatorValueStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    MethodKind,
    PatientCategory,
    RecurrenceMeasure,
    RecurrenceScopeKind,
    Sex,
)
from mars.domain.governance import (
    ConfigurationKey,
    ConfigurationVersion,
    MethodDefinition,
    MethodVersion,
)
from mars.domain.recurrence import RecurrenceResult

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("dd100000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("dd100000-0000-4000-8000-000000000010")
DISTRICT_A = uuid.UUID("dd100000-0000-4000-8000-000000000011")
DISTRICT_B = uuid.UUID("dd100000-0000-4000-8000-000000000012")
ORG_UNIT_ID = uuid.UUID("dd100000-0000-4000-8000-000000000020")
FACILITY_A = uuid.UUID("dd100000-0000-4000-8000-000000000001")

PATIENT_REPEAT = uuid.UUID("dd100000-0000-4000-8000-0000000000a1")
PATIENT_SINGLE = uuid.UUID("dd100000-0000-4000-8000-0000000000a2")

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 6, 30)

#: Test-only values. The tests that assert MARS refuses to run without them are
#: what stops these becoming production defaults.
TEST_WINDOW_DAYS = 28
TEST_BANDS = [
    {"label": "under_14_days", "lower_days": 0, "upper_days": 14},
    {"label": "14_to_27_days", "lower_days": 14, "upper_days": 28},
    {"label": "28_days_or_more", "lower_days": 28, "upper_days": None},
]


@pytest.fixture(scope="module")
def recurrence_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(recurrence_db: Engine) -> None:
    with recurrence_db.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-REC-0001', 'Recurrence fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_A, "district", "901", "Homeville", COUNTRY_ID, 1, "UG/901"),
            (DISTRICT_B, "district", "902", "Careville", COUNTRY_ID, 1, "UG/902"),
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
                VALUES (:id, 'district_health_office', 'DHO-901', 'Homeville DHO',
                        'homeville dho', 0, 'DHO-901', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )
        # The facility sits in Careville; the patients live in Homeville. That
        # separation is the point of several tests below.
        connection.execute(
            text(
                """
                INSERT INTO mars_core.facility
                    (id, organisation_unit_id, district_geography_unit_id, code,
                     raw_name, normalised_name, facility_level, ownership,
                     coordinate_validated, is_active, is_synthetic, created_at, updated_at)
                VALUES (:id, :org, :geo, 'HF-901A', 'Careville HC', 'careville hc',
                        'hc_iii', 'government', false, true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_A, "org": ORG_UNIT_ID, "geo": DISTRICT_B},
        )


@pytest.fixture
def session(recurrence_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=recurrence_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(recurrence_db: Engine) -> Iterator[None]:
    yield
    with recurrence_db.begin() as connection:
        for table in (
            "mars_analytics.recurrence_result",
            "mars_analytics.episode_member",
            "mars_analytics.episode_candidate",
            "mars_analytics.episode_build",
            "mars_core.opd_encounter_prescription",
            "mars_core.opd_encounter_test",
            "mars_core.opd_encounter",
            "mars_core.patient_reference",
            "mars_governance.configuration_version",
            "mars_governance.configuration_key",
            "mars_governance.method_version",
            "mars_governance.method_definition",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


# ---------------------------------------------------------------------------
# Fixtures that build evidence
# ---------------------------------------------------------------------------
def approve_episode_rule(session: Session) -> MethodVersion:
    definition = MethodDefinition(
        code=EPISODE_RULE_CODE,
        label="Episode rule (TEST ONLY)",
        kind=MethodKind.EPISODE_RULE,
        purpose="Test fixture. Not programme guidance.",
    )
    session.add(definition)
    session.flush()
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Test-only episode window.",
        parameters={REQUIRED_PARAMETER: TEST_WINDOW_DAYS},
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def approve_bands(session: Session) -> ConfigurationVersion:
    key = ConfigurationKey(
        key=INTERVAL_BANDS_KEY,
        label="Recurrence interval bands (TEST ONLY)",
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
        value={"bands": TEST_BANDS},
        value_checksum="f" * 64,
        # The schema requires an active version to say when it took effect.
        # The fixture satisfies the same rule a real approval does.
        effective_from=date(2026, 1, 1),
        reason_for_change="test fixture",
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def add_encounter(
    session: Session,
    *,
    patient: uuid.UUID | None,
    day: date,
    result: MalariaTestResult = MalariaTestResult.POSITIVE,
    treated: bool = True,
    residence: uuid.UUID | None = DISTRICT_A,
) -> None:
    encounter = OpdEncounter(
        facility_id=FACILITY_A,
        patient_reference_id=patient,
        encounter_date=day,
        date_assignment_method=DateAssignmentMethod.SOURCE_SUPPLIED,
        sex=Sex.FEMALE,
        patient_category=PatientCategory.NATIONAL,
        attendance_type=AttendanceType.NEW_ATTENDANCE,
        fever_present=FeverStatus.YES,
        residence_district_id=residence,
        source_system="test",
        source_row_reference=f"rec-{uuid.uuid4().hex[:12]}",
    )
    encounter.tests = [OpdEncounterTest(sequence=1, method=MalariaTestMethod.RDT, result=result)]
    if treated:
        encounter.prescriptions = [
            OpdEncounterPrescription(
                sequence=1,
                prescription_raw="Artemether/Lumefantrine 1x2x3",
                drug_name_raw="Artemether/Lumefantrine",
                drug_name_normalised="artemether/lumefantrine",
            )
        ]
    session.add(encounter)


@pytest.fixture
def evidence(session: Session):
    """One repeat-positive patient (17-day interval) and one single-visit patient."""
    for reference_id in (PATIENT_REPEAT, PATIENT_SINGLE):
        session.add(PatientReference(id=reference_id, linkage_token_id=None))
    approve_episode_rule(session)
    session.commit()

    add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 1))
    add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 18))
    add_encounter(session, patient=PATIENT_SINGLE, day=date(2026, 2, 5))
    session.commit()

    EpisodeEngine(session).build(PERIOD_START, PERIOD_END)
    session.commit()
    build = latest_build(session, PERIOD_START, PERIOD_END)
    assert build is not None
    return build


def results(session: Session, measure: RecurrenceMeasure, scope: RecurrenceScopeKind):
    return (
        session.execute(
            select(RecurrenceResult).where(
                RecurrenceResult.measure == measure,
                RecurrenceResult.scope_kind == scope,
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestNothingIsPresentedAsAClinicalOutcome:
    def test_every_row_carries_the_interpretation_limit(self, session: Session, evidence) -> None:
        """Carried on the row rather than added by a presentation layer: a
        figure that reaches a report without it is one someone will over-read."""
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        rows = session.execute(select(RecurrenceResult)).scalars().all()
        assert rows
        for row in rows:
            limit = (row.interpretation_context or {})["interpretation_limit"]
            assert "not evidence of treatment failure" in limit
            assert "not evidence of antimalarial resistance" in limit
            assert "recrudescence from reinfection" in limit

    def test_no_measure_name_claims_an_outcome(self) -> None:
        forbidden = ("failure", "resistance", "recrudescence", "reinfection", "cure")
        for measure in RecurrenceMeasure:
            for word in forbidden:
                assert word not in measure.value, measure

    def test_positives_without_a_treatment_record_are_reported(self, session: Session) -> None:
        """The ordinary explanation for a repeat positive, and the first thing
        an investigator should rule out."""
        for reference_id in (PATIENT_REPEAT,):
            session.add(PatientReference(id=reference_id, linkage_token_id=None))
        approve_episode_rule(session)
        session.commit()

        add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 1), treated=False)
        add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 18), treated=False)
        session.commit()
        EpisodeEngine(session).build(PERIOD_START, PERIOD_END)
        session.commit()

        build = latest_build(session, PERIOD_START, PERIOD_END)
        assert build is not None
        RecurrenceEngine(session).compute(build)
        session.commit()

        row = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )[0]
        assert row.positives_without_treatment_record == 2


class TestFacilityAndResidenceStaySeparate:
    def test_both_scopes_are_produced(self, session: Session, evidence) -> None:
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        facility = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )
        residence = results(
            session,
            RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS,
            RecurrenceScopeKind.RESIDENCE_DISTRICT,
        )
        assert facility and residence

    def test_they_point_at_different_places(self, session: Session, evidence) -> None:
        """The fixture facility is in Careville; the patients live in Homeville.
        A single merged scope would attribute the pattern to the wrong one."""
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        facility = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )[0]
        residence = results(
            session,
            RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS,
            RecurrenceScopeKind.RESIDENCE_DISTRICT,
        )[0]

        assert facility.scope_id == FACILITY_A
        assert residence.scope_id == DISTRICT_A
        assert facility.scope_id != residence.scope_id

    def test_each_scope_says_what_it_means(self, session: Session, evidence) -> None:
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        facility = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )[0]
        assert "Facility of care" in (facility.interpretation_context or {})["scope_meaning"]

    def test_an_unresolved_residence_contributes_only_to_facility_measures(
        self, session: Session
    ) -> None:
        session.add(PatientReference(id=PATIENT_REPEAT, linkage_token_id=None))
        approve_episode_rule(session)
        session.commit()

        add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 1), residence=None)
        add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 18), residence=None)
        session.commit()
        EpisodeEngine(session).build(PERIOD_START, PERIOD_END)
        session.commit()

        build = latest_build(session, PERIOD_START, PERIOD_END)
        assert build is not None
        RecurrenceEngine(session).compute(build)
        session.commit()

        assert results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )
        assert not results(
            session,
            RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS,
            RecurrenceScopeKind.RESIDENCE_DISTRICT,
        )
        row = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )[0]
        assert row.residence_unresolved_episodes == 1


class TestTheCountsAreTheOnesComputedByHand:
    def test_one_repeat_positive_patient_out_of_two_eligible(
        self, session: Session, evidence
    ) -> None:
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        row = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )[0]
        assert row.numerator == 1
        assert row.eligible_patients == 2

    def test_the_proportion_is_recomputed_not_assumed(self, session: Session, evidence) -> None:
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        row = results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PROPORTION, RecurrenceScopeKind.FACILITY
        )[0]
        assert row.numerator == 1
        assert row.denominator == 2
        assert row.value == Decimal("0.500000")
        assert row.value_status is IndicatorValueStatus.AVAILABLE

    def test_a_scope_with_no_eligible_patients_has_no_proportion(self, session: Session) -> None:
        """Reporting 0.0 would put a real-looking 'no recurrence here' into
        every district summary."""
        from mars.analytics.recurrence import _value_for

        value, status = _value_for(RecurrenceMeasure.REPEAT_POSITIVE_PROPORTION, 0, 0)
        assert value is None
        assert status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR


class TestUnlinkedEncountersAreAlwaysReported:
    def test_the_count_travels_on_every_row(self, session: Session) -> None:
        """Their absence always makes recurrence look rarer than it is."""
        session.add(PatientReference(id=PATIENT_REPEAT, linkage_token_id=None))
        approve_episode_rule(session)
        session.commit()

        add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 1))
        add_encounter(session, patient=PATIENT_REPEAT, day=date(2026, 2, 18))
        add_encounter(session, patient=None, day=date(2026, 2, 3))
        add_encounter(session, patient=None, day=date(2026, 2, 4))
        session.commit()
        EpisodeEngine(session).build(PERIOD_START, PERIOD_END)
        session.commit()

        build = latest_build(session, PERIOD_START, PERIOD_END)
        assert build is not None
        RecurrenceEngine(session).compute(build)
        session.commit()

        rows = session.execute(select(RecurrenceResult)).scalars().all()
        assert rows
        assert all(row.excluded_unlinked_encounters == 2 for row in rows)


class TestIntervalBandsAreGovernedNotShipped:
    def test_with_no_approved_bands_no_band_counts_are_written(
        self, session: Session, evidence
    ) -> None:
        """What counts as an early return is a clinical judgement. MARS reports
        the counts it can and does not choose cut points."""
        report = RecurrenceEngine(session).compute(evidence)
        session.commit()

        assert report.bands_available is False
        assert report.notes is not None
        assert INTERVAL_BANDS_KEY in report.notes
        assert not results(
            session, RecurrenceMeasure.INTERVAL_BAND_COUNT, RecurrenceScopeKind.FACILITY
        )

    def test_the_other_measures_are_still_produced(self, session: Session, evidence) -> None:
        """Absent bands must not silence recurrence entirely."""
        RecurrenceEngine(session).compute(evidence)
        session.commit()
        assert results(
            session, RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, RecurrenceScopeKind.FACILITY
        )

    def test_with_approved_bands_the_interval_lands_in_the_right_one(
        self, session: Session, evidence
    ) -> None:
        """The fixture's return interval is 17 days."""
        approve_bands(session)
        session.commit()

        report = RecurrenceEngine(session).compute(evidence)
        session.commit()

        assert report.bands_available is True
        bands = {
            row.interval_band: row.numerator
            for row in results(
                session, RecurrenceMeasure.INTERVAL_BAND_COUNT, RecurrenceScopeKind.FACILITY
            )
        }
        assert bands["14_to_27_days"] == 1
        assert bands["under_14_days"] == 0
        assert bands["28_days_or_more"] == 0

    def test_a_configuration_with_no_usable_bands_is_treated_as_absent(
        self, session: Session
    ) -> None:
        key = ConfigurationKey(
            key=INTERVAL_BANDS_KEY,
            label="Broken bands",
            description="test",
            category="analytics",
        )
        session.add(key)
        session.flush()
        session.add(
            ConfigurationVersion(
                configuration_key_id=key.id,
                version_number=1,
                status=LifecycleStatus.ACTIVE,
                value={"bands": []},
                value_checksum="0" * 64,
                effective_from=date(2026, 1, 1),
                reason_for_change="test",
                approved_by="test:fixture",
                approved_at=datetime.now(UTC),
            )
        )
        session.commit()

        bands, version_id = RecurrenceEngine(session).interval_bands()
        assert bands == []
        assert version_id is None


class TestResultsAreImmutableAndIdempotent:
    def test_recomputing_writes_nothing_further(self, session: Session, evidence) -> None:
        first = RecurrenceEngine(session).compute(evidence)
        session.commit()
        second = RecurrenceEngine(session).compute(evidence)
        session.commit()

        assert first.results_written > 0
        assert second.results_written == 0
        assert second.results_unchanged == first.results_written

    def test_changing_the_bands_writes_new_rows_beside_the_old_ones(
        self, session: Session, evidence
    ) -> None:
        """The same episodes banded differently are a different result.
        Overwriting would change what a district was shown with no record."""
        RecurrenceEngine(session).compute(evidence)
        session.commit()
        before = session.execute(select(func.count()).select_from(RecurrenceResult)).scalar_one()

        approve_bands(session)
        session.commit()
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        after = session.execute(select(func.count()).select_from(RecurrenceResult)).scalar_one()
        assert after > before

    def test_every_row_records_the_episode_rule_it_came_from(
        self, session: Session, evidence
    ) -> None:
        """Recurrence read under a 28-day window is a different quantity from
        recurrence read under 42."""
        RecurrenceEngine(session).compute(evidence)
        session.commit()

        rows = session.execute(select(RecurrenceResult)).scalars().all()
        assert rows
        for row in rows:
            assert row.episode_rule_version_id == evidence.rule_version_id
            assert len(row.input_fingerprint) == 64
            assert row.engine_version


class TestTheDatabaseRefusesContradictions:
    def test_a_value_without_an_available_status_is_refused(
        self, session: Session, evidence
    ) -> None:
        session.add(
            _bare_result(
                evidence.id,
                value=Decimal("0"),
                status=IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR,
            )
        )
        with pytest.raises(IntegrityError, match="value_present_iff_available"):
            session.commit()

    def test_a_band_on_a_patient_count_is_refused(self, session: Session, evidence) -> None:
        """Otherwise a patient count could be double-counted by anything
        grouping on the band."""
        session.add(
            _bare_result(
                evidence.id,
                measure=RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS,
                band="under_14_days",
            )
        )
        with pytest.raises(IntegrityError, match="band_only_on_band_counts"):
            session.commit()


def _bare_result(
    build_id: uuid.UUID,
    *,
    measure: RecurrenceMeasure = RecurrenceMeasure.REPEAT_POSITIVE_PROPORTION,
    band: str | None = None,
    value: Decimal | None = Decimal("1"),
    status: IndicatorValueStatus = IndicatorValueStatus.AVAILABLE,
) -> RecurrenceResult:
    from mars.domain.enums import PeriodGrain

    return RecurrenceResult(
        episode_build_id=build_id,
        measure=measure,
        scope_kind=RecurrenceScopeKind.FACILITY,
        scope_id=FACILITY_A,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        period_grain=PeriodGrain.MONTH,
        interval_band=band,
        numerator=1,
        denominator=1,
        value=value,
        value_status=status,
        input_fingerprint="a" * 64,
        source_cutoff=datetime.now(UTC),
        engine_version="test",
        computed_at=datetime.now(UTC),
    )
