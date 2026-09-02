"""The encounter schema against live PostgreSQL.

The unit tests assert the model's shape. These assert that the database
*enforces* it: that a contradictory malaria result is refused rather than
stored, that an age outside the form's own unit rules is refused, and that a
source row cannot be ingested twice.

A constraint that exists in a model but is not enforced by the database is a
comment. Each one here is proved by attempting the violation.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterDiagnosis,
    OpdEncounterPrescription,
    OpdEncounterReferral,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.enums import (
    AgeUnit,
    AttendanceType,
    DateAssignmentMethod,
    FeverStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PatientCategory,
    ReferralDirection,
    Sex,
)

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

FACILITY_ID = uuid.UUID("cc000000-0000-4000-8000-000000000001")
COUNTRY_ID = uuid.UUID("cc000000-0000-4000-8000-000000000010")
DISTRICT_ID = uuid.UUID("cc000000-0000-4000-8000-000000000011")
ORG_UNIT_ID = uuid.UUID("cc000000-0000-4000-8000-000000000020")
BOUNDARY_VERSION_ID = uuid.UUID("cc000000-0000-4000-8000-0000000000ff")


@pytest.fixture(scope="module")
def encounter_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def session(encounter_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=encounter_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def reference_data(encounter_engine: Engine) -> Iterator[None]:
    """A facility and its geography, so an encounter has something to hang on."""
    with encounter_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mars_core.boundary_version
                    (id, code, label, source_name, storage_crs, import_status,
                     imported_at, imported_by, created_at, updated_at)
                VALUES (:id, 'TEST-ENC-0001', 'Encounter fixture', 'synthetic',
                        'EPSG:4326', 'published', now(), 'test', now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": BOUNDARY_VERSION_ID},
        )
        for unit_id, level, code, name, parent, depth, path in [
            (COUNTRY_ID, "country", "UG", "Testland", None, 0, "UG"),
            (DISTRICT_ID, "district", "304", "Testville", COUNTRY_ID, 1, "UG/304"),
        ]:
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
                VALUES (:id, 'district_health_office', 'DHO-304', 'Testville DHO',
                        'testville dho', 0, 'DHO-304', true, now(), now())
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
                VALUES (:id, :org, :geo, 'HF-001', 'Test Health Centre',
                        'test health centre', 'hc_iii', 'government', false,
                        true, true, now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": FACILITY_ID, "org": ORG_UNIT_ID, "geo": DISTRICT_ID},
        )
    yield
    with encounter_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_core.opd_encounter_referral"))
        connection.execute(text("DELETE FROM mars_core.opd_encounter_test"))
        connection.execute(text("DELETE FROM mars_core.opd_encounter_prescription"))
        connection.execute(text("DELETE FROM mars_core.opd_encounter_diagnosis"))
        connection.execute(text("DELETE FROM mars_core.opd_encounter"))
        connection.execute(text("DELETE FROM mars_core.patient_reference"))


def encounter(**overrides: object) -> OpdEncounter:
    """A minimal valid encounter. Overrides make the violation under test."""
    defaults: dict[str, object] = {
        "facility_id": FACILITY_ID,
        "encounter_date": datetime.date(2026, 3, 4),
        "date_assignment_method": DateAssignmentMethod.SOURCE_SUPPLIED,
        "serial_number": "001",
        "sex": Sex.FEMALE,
        "patient_category": PatientCategory.NATIONAL,
        "attendance_type": AttendanceType.NEW_ATTENDANCE,
        "fever_present": FeverStatus.YES,
        "source_system": "test",
        "source_row_reference": f"row-{uuid.uuid4()}",
    }
    defaults.update(overrides)
    return OpdEncounter(**defaults)


class TestTheDatabaseRefusesContradictions:
    """A constraint that is not enforced by the database is a comment."""

    @pytest.mark.parametrize("result", [MalariaTestResult.POSITIVE, MalariaTestResult.NEGATIVE])
    def test_a_read_result_without_a_test_is_refused(
        self, session: Session, result: MalariaTestResult
    ) -> None:
        """The paper register permits writing it. MARS refuses to store it.

        A result with no test performed is not an unusual case; it is a
        transcription error, and storing it would put a phantom result into
        every downstream positivity rate.
        """
        parent = encounter()
        parent.tests = [
            OpdEncounterTest(sequence=1, method=MalariaTestMethod.NOT_DONE, result=result)
        ]
        session.add(parent)
        with pytest.raises(IntegrityError, match="no_result_without_a_test"):
            session.commit()

    def test_not_done_with_not_done_is_accepted(self, session: Session) -> None:
        """The consistent form of "no test happened" must still be storable."""
        parent = encounter()
        parent.tests = [
            OpdEncounterTest(
                sequence=1,
                method=MalariaTestMethod.NOT_DONE,
                result=MalariaTestResult.NOT_DONE,
            )
        ]
        session.add(parent)
        session.commit()

    def test_an_age_in_months_above_eleven_is_refused(self, session: Session) -> None:
        """The form says months are for patients under one year.

        Twelve months should have been written as one year, so the value is a
        transcription error rather than an unusual patient.
        """
        session.add(encounter(age_value=14, age_unit=AgeUnit.MONTHS))
        with pytest.raises(IntegrityError, match="age_months_under_a_year"):
            session.commit()

    def test_an_age_in_days_above_thirty_is_refused(self, session: Session) -> None:
        session.add(encounter(age_value=45, age_unit=AgeUnit.DAYS))
        with pytest.raises(IntegrityError, match="age_days_under_a_month"):
            session.commit()

    def test_an_implausible_age_in_years_is_refused(self, session: Session) -> None:
        session.add(encounter(age_value=210, age_unit=AgeUnit.YEARS))
        with pytest.raises(IntegrityError, match="age_years_plausible"):
            session.commit()

    def test_a_negative_age_is_refused(self, session: Session) -> None:
        session.add(encounter(age_value=-1, age_unit=AgeUnit.YEARS))
        with pytest.raises(IntegrityError, match="age_not_negative"):
            session.commit()

    def test_ages_at_the_boundaries_are_accepted(self, session: Session) -> None:
        """11 months and 30 days are legal; the form's rules are inclusive."""
        session.add(encounter(age_value=11, age_unit=AgeUnit.MONTHS))
        session.add(encounter(age_value=30, age_unit=AgeUnit.DAYS))
        session.add(encounter(age_value=0, age_unit=AgeUnit.YEARS))
        session.commit()


class TestASourceRowIsIngestedOnce:
    def test_the_same_source_row_cannot_be_stored_twice(self, session: Session) -> None:
        """Two extracts of the same register book must not double-count."""
        session.add(encounter(source_row_reference="book-7-row-12"))
        session.commit()

        session.add(encounter(source_row_reference="book-7-row-12"))
        with pytest.raises(IntegrityError, match="uq_opd_encounter_source_row"):
            session.commit()

    def test_the_same_reference_from_a_different_system_is_allowed(self, session: Session) -> None:
        """Row identifiers are only unique within the system that issued them."""
        session.add(encounter(source_system="ereg_a", source_row_reference="row-1"))
        session.add(encounter(source_system="ereg_b", source_row_reference="row-1"))
        session.commit()


class TestRepeatableChildren:
    def test_an_encounter_carries_several_diagnoses_in_order(self, session: Session) -> None:
        """Column 18: "If more space is required, use another line"."""
        parent = encounter()
        parent.diagnoses = [
            OpdEncounterDiagnosis(
                sequence=1, diagnosis_raw="Malaria", diagnosis_normalised="malaria"
            ),
            OpdEncounterDiagnosis(
                sequence=2,
                diagnosis_raw="Upper respiratory tract infection",
                diagnosis_normalised="upper respiratory tract infection",
            ),
        ]
        session.add(parent)
        session.commit()
        session.refresh(parent)

        assert [d.sequence for d in parent.diagnoses] == [1, 2]
        assert parent.diagnoses[0].diagnosis_raw == "Malaria"

    def test_a_diagnosis_sequence_cannot_repeat_within_one_encounter(
        self, session: Session
    ) -> None:
        parent = encounter()
        parent.diagnoses = [
            OpdEncounterDiagnosis(sequence=1, diagnosis_raw="A", diagnosis_normalised="a"),
            OpdEncounterDiagnosis(sequence=1, diagnosis_raw="B", diagnosis_normalised="b"),
        ]
        session.add(parent)
        with pytest.raises(IntegrityError, match="uq_opd_diagnosis_encounter_sequence"):
            session.commit()

    def test_an_unmatched_diagnosis_is_stored_with_a_null_hmis_item(self, session: Session) -> None:
        """Unmatched is a reportable fact, not an error.

        Forcing a free-text diagnosis to the nearest HMIS 105 item would
        silently change what a clinician wrote.
        """
        parent = encounter()
        parent.diagnoses = [
            OpdEncounterDiagnosis(
                sequence=1,
                diagnosis_raw="?malaria vs typhoid",
                diagnosis_normalised="?malaria vs typhoid",
            )
        ]
        session.add(parent)
        session.commit()
        assert parent.diagnoses[0].hmis_105_item_id is None

    def test_prescriptions_keep_raw_text_when_the_format_does_not_parse(
        self, session: Session
    ) -> None:
        """A partial parse would be a number nobody wrote."""
        parent = encounter()
        parent.prescriptions = [
            OpdEncounterPrescription(
                sequence=1,
                prescription_raw="Coartem as directed",
                drug_name_raw="Coartem",
                drug_name_normalised="coartem",
            )
        ]
        session.add(parent)
        session.commit()

        stored = parent.prescriptions[0]
        assert stored.prescription_raw == "Coartem as directed"
        assert stored.units_per_dose is None
        assert stored.doses_per_day is None
        assert stored.days is None
        assert stored.total_units is None

    def test_a_fully_parsed_prescription_keeps_its_factors(self, session: Session) -> None:
        """The form's printed format: units per dose x doses per day x days."""
        parent = encounter()
        parent.prescriptions = [
            OpdEncounterPrescription(
                sequence=1,
                prescription_raw="Coartem 4 x 2 x 3",
                drug_name_raw="Coartem",
                drug_name_normalised="coartem",
                units_per_dose=4,
                doses_per_day=2,
                days=3,
                total_units=24,
            )
        ]
        session.add(parent)
        session.commit()
        assert float(parent.prescriptions[0].total_units or 0) == 24.0

    def test_a_zero_dose_is_refused(self, session: Session) -> None:
        parent = encounter()
        parent.prescriptions = [
            OpdEncounterPrescription(sequence=1, prescription_raw="X 0 x 2 x 3", units_per_dose=0)
        ]
        session.add(parent)
        with pytest.raises(IntegrityError, match="units_positive"):
            session.commit()

    def test_a_device_is_kept_but_marked(self, session: Session) -> None:
        """Column 19 also carries spectacles and wheelchairs, which are not drugs."""
        parent = encounter()
        parent.prescriptions = [
            OpdEncounterPrescription(sequence=1, prescription_raw="Spectacles", is_device=True)
        ]
        session.add(parent)
        session.commit()
        assert parent.prescriptions[0].is_device is True

    def test_deleting_an_encounter_removes_its_children(
        self, session: Session, encounter_engine: Engine
    ) -> None:
        parent = encounter()
        parent.diagnoses = [
            OpdEncounterDiagnosis(sequence=1, diagnosis_raw="M", diagnosis_normalised="m")
        ]
        parent.prescriptions = [OpdEncounterPrescription(sequence=1, prescription_raw="P")]
        session.add(parent)
        session.commit()
        encounter_id = parent.id

        session.delete(parent)
        session.commit()

        with encounter_engine.connect() as connection:
            for table in ("opd_encounter_diagnosis", "opd_encounter_prescription"):
                remaining = connection.execute(
                    text(f"SELECT count(*) FROM mars_core.{table} WHERE opd_encounter_id = :id"),
                    {"id": encounter_id},
                ).scalar_one()
                assert remaining == 0, f"{table} rows survived their encounter"


class TestPatientReferenceHoldsNoIdentity:
    def test_encounters_group_under_one_reference(self, session: Session) -> None:
        person = PatientReference(
            first_seen_on=datetime.date(2026, 3, 1),
            last_seen_on=datetime.date(2026, 3, 4),
            encounter_count=2,
        )
        session.add(person)
        session.flush()

        session.add(encounter(patient_reference_id=person.id))
        session.add(
            encounter(
                patient_reference_id=person.id,
                attendance_type=AttendanceType.RE_ATTENDANCE,
                encounter_date=datetime.date(2026, 3, 18),
            )
        )
        session.commit()
        session.refresh(person)
        assert len(person.encounters) == 2

    def test_an_encounter_without_a_patient_is_valid(self, session: Session) -> None:
        """The common case: a register row with no usable identifier.

        Minting a synthetic person per row would make every visitor look like a
        different patient and would destroy re-attendance analysis.
        """
        session.add(encounter(patient_reference_id=None))
        session.commit()

    def test_removing_a_reference_keeps_the_encounters(
        self, session: Session, encounter_engine: Engine
    ) -> None:
        """Encounters are the surveillance record and outlive any linkage.

        If a linkage is later withdrawn - a consent change, a correction - the
        visits still happened and must not vanish from the counts.
        """
        person = PatientReference()
        session.add(person)
        session.flush()
        session.add(encounter(patient_reference_id=person.id))
        session.commit()

        session.delete(person)
        session.commit()

        with encounter_engine.connect() as connection:
            remaining = connection.execute(
                text("SELECT count(*) FROM mars_core.opd_encounter")
            ).scalar_one()
            orphaned = connection.execute(
                text(
                    "SELECT count(*) FROM mars_core.opd_encounter "
                    "WHERE patient_reference_id IS NULL"
                )
            ).scalar_one()
        assert remaining == 1
        assert orphaned == 1


class TestStoredValuesRoundTrip:
    def test_enum_values_are_stored_as_the_documented_strings(
        self, session: Session, encounter_engine: Engine
    ) -> None:
        """What is in the database, the API and the contract must be one string."""
        parent = encounter(
            source_row_reference="round-trip",
            sex=Sex.FEMALE,
            attendance_type=AttendanceType.RE_ATTENDANCE,
            age_value=7,
            age_unit=AgeUnit.MONTHS,
        )
        parent.tests = [
            OpdEncounterTest(
                sequence=1,
                method=MalariaTestMethod.MICROSCOPY,
                result=MalariaTestResult.NEGATIVE,
            )
        ]
        session.add(parent)
        session.commit()

        with encounter_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT e.sex::text, e.attendance_type::text, "
                    "t.method::text, t.result::text, e.age_unit::text "
                    "FROM mars_core.opd_encounter e "
                    "JOIN mars_core.opd_encounter_test t "
                    "  ON t.opd_encounter_id = e.id "
                    "WHERE e.source_row_reference = 'round-trip'"
                )
            ).one()
        assert row == ("female", "re_attendance", "microscopy", "negative", "months")

    def test_residence_keeps_unresolvable_text(self, session: Session) -> None:
        """Never silently dropped, never guessed."""
        session.add(
            encounter(
                residence_district_id=DISTRICT_ID,
                residence_parish_raw="Kicheche",
                residence_village_raw="Bar-Dege",
                residence_unresolved_raw="Subcounty written illegibly",
            )
        )
        session.commit()

    def test_test_results_are_indexed(self, encounter_engine: Engine) -> None:
        """Every positivity query filters on the result."""
        with encounter_engine.connect() as connection:
            definition = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='mars_core' AND indexname='ix_opd_test_result'"
                )
            ).scalar_one()
        assert "result" in definition


class TestTestsAndReferralsAreTheirOwnRows:
    """Step 7.3 asks for both as normalised tables."""

    def test_an_encounter_can_record_two_tests(self, session: Session) -> None:
        """A slide and an RDT on the same visit must both survive."""
        parent = encounter()
        parent.tests = [
            OpdEncounterTest(
                sequence=1,
                method=MalariaTestMethod.RDT,
                result=MalariaTestResult.POSITIVE,
            ),
            OpdEncounterTest(
                sequence=2,
                method=MalariaTestMethod.MICROSCOPY,
                result=MalariaTestResult.POSITIVE,
            ),
        ]
        session.add(parent)
        session.commit()
        session.refresh(parent)
        assert [t.method for t in parent.tests] == [
            MalariaTestMethod.RDT,
            MalariaTestMethod.MICROSCOPY,
        ]

    def test_a_test_sequence_cannot_repeat(self, session: Session) -> None:
        parent = encounter()
        parent.tests = [
            OpdEncounterTest(
                sequence=1, method=MalariaTestMethod.RDT, result=MalariaTestResult.POSITIVE
            ),
            OpdEncounterTest(
                sequence=1,
                method=MalariaTestMethod.MICROSCOPY,
                result=MalariaTestResult.NEGATIVE,
            ),
        ]
        session.add(parent)
        with pytest.raises(IntegrityError, match="uq_opd_test_encounter_sequence"):
            session.commit()

    def test_a_confirmed_test_is_distinguished_from_an_attendance(self, session: Session) -> None:
        """Denominators must be able to say "tested", not merely "attended"."""
        tested = encounter()
        tested.tests = [
            OpdEncounterTest(
                sequence=1, method=MalariaTestMethod.RDT, result=MalariaTestResult.NEGATIVE
            )
        ]
        untested = encounter()
        untested.tests = [
            OpdEncounterTest(
                sequence=1,
                method=MalariaTestMethod.NOT_DONE,
                result=MalariaTestResult.NOT_DONE,
            )
        ]
        session.add_all([tested, untested])
        session.commit()

        assert tested.has_confirmed_malaria_test is True
        assert untested.has_confirmed_malaria_test is False
        assert tested.is_malaria_positive is False

    def test_an_encounter_carries_both_referral_directions(self, session: Session) -> None:
        """Columns 21 and 22 can both be filled on one row."""
        parent = encounter()
        parent.referrals = [
            OpdEncounterReferral(direction=ReferralDirection.INBOUND, referral_number="REF-IN-9"),
            OpdEncounterReferral(direction=ReferralDirection.OUTBOUND, referral_number="REF-OUT-4"),
        ]
        session.add(parent)
        session.commit()
        session.refresh(parent)
        assert {r.direction for r in parent.referrals} == {
            ReferralDirection.INBOUND,
            ReferralDirection.OUTBOUND,
        }

    def test_the_same_referral_number_may_appear_at_two_facilities(self, session: Session) -> None:
        """Referral numbers are facility-issued with no national scheme.

        Treating one as an identifier would merge two unrelated patients.
        """
        first = encounter()
        first.referrals = [
            OpdEncounterReferral(direction=ReferralDirection.INBOUND, referral_number="0001")
        ]
        second = encounter()
        second.referrals = [
            OpdEncounterReferral(direction=ReferralDirection.INBOUND, referral_number="0001")
        ]
        session.add_all([first, second])
        session.commit()

    def test_tests_and_referrals_die_with_their_encounter(
        self, session: Session, encounter_engine: Engine
    ) -> None:
        parent = encounter()
        parent.tests = [
            OpdEncounterTest(
                sequence=1, method=MalariaTestMethod.RDT, result=MalariaTestResult.POSITIVE
            )
        ]
        parent.referrals = [
            OpdEncounterReferral(direction=ReferralDirection.OUTBOUND, referral_number="R-1")
        ]
        session.add(parent)
        session.commit()
        encounter_id = parent.id

        session.delete(parent)
        session.commit()

        with encounter_engine.connect() as connection:
            for table in ("opd_encounter_test", "opd_encounter_referral"):
                remaining = connection.execute(
                    text(f"SELECT count(*) FROM mars_core.{table} WHERE opd_encounter_id = :id"),
                    {"id": encounter_id},
                ).scalar_one()
                assert remaining == 0
