"""The indicator registry and aggregation engine against live PostgreSQL.

The encounters below are built so every figure is knowable by hand, and the
tests assert the arithmetic against those numbers rather than against whatever
the engine happens to produce.

What needs a real database rather than a mock:

* that a definition ships as a **draft** and computes nothing until approved;
* that seeding twice registers nothing twice, and never demotes an approved
  version;
* that an undefined denominator is stored as *no value*, enforced by a
  constraint and not merely by the engine;
* that a district proportion is recomputed from summed parts, not averaged;
* that recomputing over unchanged inputs is idempotent while changed inputs
  write a new row beside the old one.

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

from mars.analytics.aggregation import IndicatorAggregationService
from mars.analytics.indicator_catalogue import CATALOGUE, CATALOGUE_BY_CODE
from mars.analytics.indicator_registry import (
    IndicatorApprovalError,
    IndicatorRegistryService,
)
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    AttendanceType,
    DateAssignmentMethod,
    FeverStatus,
    GeographyGrain,
    IndicatorUnit,
    IndicatorValueStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PatientCategory,
    PeriodGrain,
    Sex,
)
from mars.domain.indicator import (
    IndicatorDefinition,
    IndicatorDefinitionVersion,
    IndicatorResult,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_VERSION_ID = uuid.UUID("bb000000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("bb000000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("bb000000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("bb000000-0000-4000-8000-000000000020")
FACILITY_A = uuid.UUID("bb000000-0000-4000-8000-000000000001")
FACILITY_B = uuid.UUID("bb000000-0000-4000-8000-000000000002")

MONTH_START = date(2026, 3, 1)
MONTH_END = date(2026, 3, 31)

#: Facility A: 20 RDT tests of which 8 positive, plus 5 untested attendances.
A_TESTS, A_POSITIVE, A_UNTESTED = 20, 8, 5
#: Facility B: 4 tests of which 3 positive. Small on purpose - it is what makes
#: the averaging-versus-recomputing difference visible.
B_TESTS, B_POSITIVE = 4, 3


@pytest.fixture(scope="module")
def indicator_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_data(indicator_engine: Engine) -> None:
    with indicator_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-IND-0001', 'Indicator fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "601", "Indicatorville", COUNTRY_ID, 1, "UG/601"),
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
                VALUES (:id, 'district_health_office', 'DHO-601', 'Indicatorville DHO',
                        'indicatorville dho', 0, 'DHO-601', true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": ORG_UNIT_ID},
        )
        for facility_id, code, name in (
            (FACILITY_A, "HF-601A", "Alpha HC"),
            (FACILITY_B, "HF-601B", "Beta HC"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO mars_core.facility
                        (id, organisation_unit_id, district_geography_unit_id, code,
                         raw_name, normalised_name, facility_level, ownership,
                         coordinate_validated, is_active, is_synthetic,
                         created_at, updated_at)
                    VALUES (:id, :org, :geo, :code, :name, :normalised, 'hc_iii',
                            'government', false, true, true, now(), now())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": facility_id,
                    "org": ORG_UNIT_ID,
                    "geo": DISTRICT_ID,
                    "code": code,
                    "name": name,
                    "normalised": name.lower(),
                },
            )


@pytest.fixture
def session(indicator_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=indicator_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(indicator_engine: Engine) -> Iterator[None]:
    yield
    with indicator_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_analytics.indicator_result"))
        connection.execute(text("DELETE FROM mars_governance.indicator_definition_version"))
        connection.execute(text("DELETE FROM mars_governance.indicator_definition"))
        connection.execute(text("DELETE FROM mars_core.opd_encounter_test"))
        connection.execute(text("DELETE FROM mars_core.opd_encounter"))


@pytest.fixture
def encounters(session: Session) -> None:
    """A month whose every figure is computable by hand."""
    made = 0

    def add(facility_id: uuid.UUID, method: MalariaTestMethod, result: MalariaTestResult) -> None:
        nonlocal made
        made += 1
        encounter = OpdEncounter(
            facility_id=facility_id,
            encounter_date=date(2026, 3, 1 + (made % 28)),
            date_assignment_method=DateAssignmentMethod.SOURCE_SUPPLIED,
            sex=Sex.FEMALE,
            patient_category=PatientCategory.NATIONAL,
            attendance_type=AttendanceType.NEW_ATTENDANCE,
            fever_present=FeverStatus.YES,
            source_system="test",
            source_row_reference=f"ind-{made:04d}",
        )
        encounter.tests = [OpdEncounterTest(sequence=1, method=method, result=result)]
        session.add(encounter)

    for index in range(A_TESTS):
        add(
            FACILITY_A,
            MalariaTestMethod.RDT,
            MalariaTestResult.POSITIVE if index < A_POSITIVE else MalariaTestResult.NEGATIVE,
        )
    for _ in range(A_UNTESTED):
        add(FACILITY_A, MalariaTestMethod.NOT_DONE, MalariaTestResult.NOT_DONE)
    for index in range(B_TESTS):
        add(
            FACILITY_B,
            MalariaTestMethod.RDT,
            MalariaTestResult.POSITIVE if index < B_POSITIVE else MalariaTestResult.NEGATIVE,
        )
    session.commit()


def registry(session: Session) -> IndicatorRegistryService:
    return IndicatorRegistryService(session)


def engine(session: Session) -> IndicatorAggregationService:
    return IndicatorAggregationService(session)


def seeded_version(session: Session, code: str) -> IndicatorDefinitionVersion:
    registry(session).seed_catalogue()
    session.commit()
    definition = registry(session).get_definition(code)
    assert definition is not None
    return definition.versions[0]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestTheCatalogueSeedsAsDrafts:
    def test_seeding_registers_every_shipped_definition(self, session: Session) -> None:
        report = registry(session).seed_catalogue()
        session.commit()

        assert report.definitions_created == len(CATALOGUE)
        assert report.versions_created == len(CATALOGUE)
        stored = session.execute(select(func.count()).select_from(IndicatorDefinition)).scalar_one()
        assert stored == len(CATALOGUE)

    def test_every_seeded_version_is_a_draft(self, session: Session) -> None:
        """Registering a definition and putting it in force are different acts,
        and only one of them is MARS's."""
        registry(session).seed_catalogue()
        session.commit()

        statuses = {
            version.status
            for version in session.execute(select(IndicatorDefinitionVersion)).scalars().all()
        }
        assert statuses == {LifecycleStatus.DRAFT}

    def test_nothing_is_active_before_a_programme_approves_it(self, session: Session) -> None:
        registry(session).seed_catalogue()
        session.commit()
        assert registry(session).active_versions() == {}

    def test_seeding_twice_registers_nothing_twice(self, session: Session) -> None:
        registry(session).seed_catalogue()
        session.commit()
        second = registry(session).seed_catalogue()
        session.commit()

        assert second.definitions_created == 0
        assert second.versions_created == 0
        assert session.execute(
            select(func.count()).select_from(IndicatorDefinitionVersion)
        ).scalar_one() == len(CATALOGUE)

    def test_seeding_never_demotes_an_approved_version(self, session: Session) -> None:
        """A programme approved it. A deployment must not undo that."""
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        registry(session).approve_version(version.id, approved_by="programme:test")
        registry(session).activate_version(version.id)
        session.commit()

        registry(session).seed_catalogue()
        session.commit()
        session.refresh(version)
        assert version.status is LifecycleStatus.ACTIVE


class TestApprovalIsGoverned:
    def test_an_approval_requires_a_named_approver(self, session: Session) -> None:
        """An active definition with nobody's name on it is an ungoverned one."""
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        with pytest.raises(IndicatorApprovalError, match="approver must be named"):
            registry(session).approve_version(version.id, approved_by="   ")

    def test_a_draft_cannot_be_activated_directly(self, session: Session) -> None:
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        with pytest.raises(IndicatorApprovalError, match="approved version"):
            registry(session).activate_version(version.id)

    def test_activation_retires_the_version_it_replaces(self, session: Session) -> None:
        """Retired, not deleted: figures computed under it are still in the
        database and still have to be explicable."""
        definition = registry(session).get_definition("ENC_TESTED_MALARIA")
        if definition is None:
            seeded_version(session, "ENC_TESTED_MALARIA")
            definition = registry(session).get_definition("ENC_TESTED_MALARIA")
        assert definition is not None

        first = definition.versions[0]
        registry(session).approve_version(first.id, approved_by="programme:test")
        registry(session).activate_version(first.id)
        session.commit()

        second = IndicatorDefinitionVersion(
            indicator_definition_id=definition.id,
            version_number=2,
            semantic_version="2.0.0",
            status=LifecycleStatus.APPROVED,
            numerator_specification={"source": "encounter", "filter": {}},
            blank_handling="revised",
            specification_checksum="b" * 64,
            reason_for_change="revision under test",
            approved_by="programme:test",
            approved_at=datetime.now(UTC),
        )
        session.add(second)
        session.flush()
        registry(session).activate_version(second.id)
        session.commit()

        session.refresh(first)
        assert first.status is LifecycleStatus.RETIRED
        assert first.effective_to is not None
        assert second.status is LifecycleStatus.ACTIVE

    def test_only_one_version_is_active_at_a_time(self, session: Session) -> None:
        definition = registry(session).get_definition("ENC_TESTED_MALARIA")
        if definition is None:
            seeded_version(session, "ENC_TESTED_MALARIA")
            definition = registry(session).get_definition("ENC_TESTED_MALARIA")
        assert definition is not None

        first = definition.versions[0]
        registry(session).approve_version(first.id, approved_by="programme:test")
        registry(session).activate_version(first.id)
        session.commit()

        active = session.execute(
            select(func.count())
            .select_from(IndicatorDefinitionVersion)
            .where(
                IndicatorDefinitionVersion.indicator_definition_id == definition.id,
                IndicatorDefinitionVersion.status == LifecycleStatus.ACTIVE,
            )
        ).scalar_one()
        assert active == 1

    def test_the_database_refuses_an_approved_version_with_no_approver(
        self, session: Session
    ) -> None:
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        version.status = LifecycleStatus.ACTIVE
        version.approved_by = None
        with pytest.raises(IntegrityError, match="approved_requires_approver"):
            session.commit()


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------
class TestTheCountsAreTheOnesComputedByHand:
    def test_attendance_counts_every_encounter(self, session: Session, encounters: None) -> None:
        total = engine(session).count_attendances(FACILITY_A, MONTH_START, MONTH_END)
        assert total == A_TESTS + A_UNTESTED

    def test_tested_excludes_not_done(self, session: Session, encounters: None) -> None:
        """A denominator inflated by untested attendances understates positivity
        everywhere, and worst where testing has broken down."""
        tested = engine(session).count_tested(FACILITY_A, MONTH_START, MONTH_END)
        assert tested == A_TESTS
        assert tested != A_TESTS + A_UNTESTED

    def test_confirmed_counts_positive_results(self, session: Session, encounters: None) -> None:
        assert engine(session).count_confirmed(FACILITY_A, MONTH_START, MONTH_END) == A_POSITIVE

    def test_an_encounter_with_two_test_rows_is_counted_once(
        self, session: Session, encounters: None
    ) -> None:
        """The test join fans out; the inflation would be invisible in a total."""
        before = engine(session).count_tested(FACILITY_A, MONTH_START, MONTH_END)
        encounter = (
            session.execute(
                select(OpdEncounter).where(OpdEncounter.facility_id == FACILITY_A).limit(1)
            )
            .scalars()
            .one()
        )
        encounter.tests.append(
            OpdEncounterTest(
                sequence=2,
                method=MalariaTestMethod.MICROSCOPY,
                result=MalariaTestResult.NEGATIVE,
            )
        )
        session.commit()

        assert engine(session).count_tested(FACILITY_A, MONTH_START, MONTH_END) == before


class TestAnUndefinedDenominatorIsNotZero:
    def test_a_zero_denominator_yields_no_value(self, session: Session) -> None:
        """A facility that tested nobody has no positivity - not a positivity
        of zero. Reporting the latter puts a real-looking 0% into every
        district average."""
        computed = engine(session).proportion(0, 0)
        assert computed.value is None
        assert computed.status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_null_denominator_yields_no_value(self, session: Session) -> None:
        computed = engine(session).proportion(5, None)
        assert computed.value is None
        assert computed.status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_real_proportion_is_stored_as_a_fraction(self, session: Session) -> None:
        """Presentation multiplies. A bare 40.0 with no unit is not reversible."""
        computed = engine(session).proportion(A_POSITIVE, A_TESTS)
        assert computed.status is IndicatorValueStatus.AVAILABLE
        assert computed.value == Decimal("0.400000")

    def test_the_database_refuses_a_value_without_an_available_status(
        self, session: Session
    ) -> None:
        """Enforced by a constraint, not only by the engine: a second writer
        must not be able to bypass the first."""
        version = seeded_version(session, "ENC_TEST_POSITIVITY")
        session.add(
            IndicatorResult(
                indicator_version_id=version.id,
                indicator_code="ENC_TEST_POSITIVITY",
                geography_grain=GeographyGrain.FACILITY,
                facility_id=FACILITY_A,
                period_start=MONTH_START,
                period_end=MONTH_END,
                period_grain=PeriodGrain.MONTH,
                numerator=1,
                denominator=0,
                value=Decimal("0"),
                value_status=IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR,
                input_fingerprint="c" * 64,
                source_cutoff=datetime.now(UTC),
                computed_at=datetime.now(UTC),
                engine_version="test",
            )
        )
        with pytest.raises(IntegrityError, match="value_present_iff_available"):
            session.commit()

    def test_the_database_refuses_a_facility_id_on_a_national_row(self, session: Session) -> None:
        """Otherwise a national row could be double-counted by anything joining
        on facility."""
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        session.add(
            IndicatorResult(
                indicator_version_id=version.id,
                indicator_code="ENC_TESTED_MALARIA",
                geography_grain=GeographyGrain.NATIONAL,
                facility_id=FACILITY_A,
                period_start=MONTH_START,
                period_end=MONTH_END,
                period_grain=PeriodGrain.MONTH,
                numerator=1,
                value=Decimal("1"),
                value_status=IndicatorValueStatus.AVAILABLE,
                input_fingerprint="d" * 64,
                source_cutoff=datetime.now(UTC),
                computed_at=datetime.now(UTC),
                engine_version="test",
            )
        )
        with pytest.raises(IntegrityError, match="facility_id_matches_grain"):
            session.commit()


class TestRollupsRecomputeRatherThanAverage:
    def test_a_district_proportion_is_recomputed_from_summed_parts(
        self, session: Session, encounters: None
    ) -> None:
        """Averaging facility proportions weights a clinic that tested four
        people the same as a hospital that tested four hundred, and produces a
        district figure no facility reported and nobody can reproduce."""
        service = engine(session)
        per_facility = {
            FACILITY_A: service.proportion(A_POSITIVE, A_TESTS),
            FACILITY_B: service.proportion(B_POSITIVE, B_TESTS),
        }
        rolled = service.roll_up(per_facility, unit=IndicatorUnit.PROPORTION)

        correct = Decimal(A_POSITIVE + B_POSITIVE) / Decimal(A_TESTS + B_TESTS)
        naive_average = (
            Decimal(A_POSITIVE) / Decimal(A_TESTS) + Decimal(B_POSITIVE) / Decimal(B_TESTS)
        ) / 2

        assert rolled.value == correct.quantize(Decimal("0.000001"))
        assert rolled.value != naive_average.quantize(Decimal("0.000001"))

    def test_counts_sum(self, session: Session) -> None:
        service = engine(session)
        rolled = service.roll_up(
            {FACILITY_A: service.count_value(20), FACILITY_B: service.count_value(4)},
            unit=IndicatorUnit.COUNT,
        )
        assert rolled.numerator == 24

    def test_a_rollup_records_how_many_units_contributed(self, session: Session) -> None:
        """A district total from four of forty facilities is not a small total."""
        service = engine(session)
        rolled = service.roll_up(
            {FACILITY_A: service.count_value(20)},
            unit=IndicatorUnit.COUNT,
            expected_units=40,
        )
        assert rolled.contributing_units == 1
        assert rolled.expected_units == 40
        assert "partial_reporting" in rolled.quality

    def test_a_rollup_with_nothing_available_is_unavailable_not_zero(
        self, session: Session
    ) -> None:
        service = engine(session)
        rolled = service.roll_up(
            {FACILITY_A: service.proportion(1, 0)},
            unit=IndicatorUnit.PROPORTION,
            expected_units=2,
        )
        assert rolled.value is None
        assert rolled.status is IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA
        assert rolled.contributing_units == 0


class TestMaterialisationIsIdempotentAndImmutable:
    def test_recomputing_unchanged_inputs_writes_nothing_further(
        self, session: Session, encounters: None
    ) -> None:
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        service = engine(session)
        computed = service.count_value(A_TESTS)

        _first, created_first = service.materialise(
            version,
            "ENC_TESTED_MALARIA",
            grain=GeographyGrain.FACILITY,
            period_start=MONTH_START,
            period_end=MONTH_END,
            period_grain=PeriodGrain.MONTH,
            computed=computed,
            facility_id=FACILITY_A,
        )
        _second, created_second = service.materialise(
            version,
            "ENC_TESTED_MALARIA",
            grain=GeographyGrain.FACILITY,
            period_start=MONTH_START,
            period_end=MONTH_END,
            period_grain=PeriodGrain.MONTH,
            computed=computed,
            facility_id=FACILITY_A,
        )
        session.commit()

        assert created_first is True
        assert created_second is False
        assert session.execute(select(func.count()).select_from(IndicatorResult)).scalar_one() == 1

    def test_changed_inputs_write_a_new_row_beside_the_old_one(
        self, session: Session, encounters: None
    ) -> None:
        """The old figure was acted on. A record showing only the corrected one
        cannot explain what anyone did."""
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        service = engine(session)

        for total in (A_TESTS, A_TESTS + 3):
            service.materialise(
                version,
                "ENC_TESTED_MALARIA",
                grain=GeographyGrain.FACILITY,
                period_start=MONTH_START,
                period_end=MONTH_END,
                period_grain=PeriodGrain.MONTH,
                computed=service.count_value(total),
                facility_id=FACILITY_A,
            )
        session.commit()

        stored = sorted(
            row.numerator for row in session.execute(select(IndicatorResult)).scalars().all()
        )
        assert stored == [A_TESTS, A_TESTS + 3]

    def test_every_result_carries_its_provenance(self, session: Session, encounters: None) -> None:
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        service = engine(session)
        result, _ = service.materialise(
            version,
            "ENC_TESTED_MALARIA",
            grain=GeographyGrain.FACILITY,
            period_start=MONTH_START,
            period_end=MONTH_END,
            period_grain=PeriodGrain.MONTH,
            computed=service.count_value(A_TESTS),
            facility_id=FACILITY_A,
            boundary_version_id=BOUNDARY_VERSION_ID,
            source_cutoff=service.latest_source_cutoff(),
        )
        session.commit()

        assert result.indicator_version_id == version.id
        assert len(result.input_fingerprint) == 64
        assert result.source_cutoff is not None
        assert result.boundary_version_id == BOUNDARY_VERSION_ID
        assert result.engine_version
        assert result.computed_at is not None

    def test_an_unavailable_result_is_stored_with_its_reason(self, session: Session) -> None:
        version = seeded_version(session, "ENC_TEST_POSITIVITY")
        service = engine(session)
        result, _ = service.materialise(
            version,
            "ENC_TEST_POSITIVITY",
            grain=GeographyGrain.FACILITY,
            period_start=MONTH_START,
            period_end=MONTH_END,
            period_grain=PeriodGrain.MONTH,
            computed=service.proportion(0, 0),
            facility_id=FACILITY_A,
        )
        session.commit()

        assert result.value is None
        assert result.value_status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR
        assert result.quality_context is not None
        assert "not the same as zero" in str(result.quality_context)


class TestBlankInputsStayMissing:
    def test_an_all_blank_element_sums_to_none_not_zero(self, session: Session) -> None:
        """A facility that did not answer has not reported none."""
        total, blanks = engine(session).sum_aggregate_element(
            FACILITY_A, MONTH_START, MONTH_END, form="hmis_105", element="EP01b"
        )
        # No submissions at all in this fixture: the honest answer is None.
        assert total is None
        assert blanks == 0

    def test_a_count_value_from_none_is_unavailable(self, session: Session) -> None:
        computed = engine(session).count_value(None, missing_inputs=4)
        assert computed.value is None
        assert computed.status is IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA
        assert computed.missing_inputs == 4
        assert "did not report" in str(computed.quality)


class TestTheEngineRefusesUnapprovedDefinitions:
    def test_active_version_is_none_before_approval(self, session: Session) -> None:
        """Callers must treat this as 'cannot be computed', never as a reason
        to fall back to a draft."""
        registry(session).seed_catalogue()
        session.commit()
        assert registry(session).active_version("ENC_TESTED_MALARIA") is None

    def test_after_approval_the_active_version_is_the_approved_one(self, session: Session) -> None:
        version = seeded_version(session, "ENC_TESTED_MALARIA")
        registry(session).approve_version(version.id, approved_by="programme:test")
        registry(session).activate_version(version.id)
        session.commit()

        active = registry(session).active_version("ENC_TESTED_MALARIA")
        assert active is not None
        assert active.id == version.id
        assert active.specification_checksum == CATALOGUE_BY_CODE["ENC_TESTED_MALARIA"].checksum
