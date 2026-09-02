"""Outpatient encounter model, grounded in HMIS OPD 002.

Every column here traces to a printed field on the register. The mapping,
including the printed labels and the ambiguities that survive, is
``docs/data-dictionary/opd-002.md``; the form's own bytes are identified by
checksum in ``data/manifests/hmis-reference-documents.sha256.json``.

Three rules govern the shape of this module.

**One row is one visit.** The register's stated objective is "to record detailed
information about each outpatient visit". The same person attending twice is two
rows, which is what makes the new/re-attendance tick meaningful.

**No direct identifier lives here.** Columns 2, 3 and 8 - national ID, patient
name and phone, next of kin - carry direct identity. ``mars_core`` holds a
pseudonymous :class:`PatientReference` and nothing more; the vault that resolves
it is ``mars_identity`` (ADR 0006). Next of kin is not stored at all, in either
schema: surveillance has no purpose for a third party's contact details.

**Recorded, never inferred.** The register says what was observed and done - a
test, a result, a diagnosis written, a medicine prescribed. It says nothing
about whether treatment worked, and it has no outcome column at all. Nothing in
this model may be read as evidence of antimalarial resistance (ADR 0005).
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import CORE
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

if TYPE_CHECKING:
    from mars.domain.geography import GeographyUnit
    from mars.domain.organisation import Facility


class PatientReference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person, as ``mars_core`` is allowed to know them.

    Deliberately almost empty. It exists so encounters can be grouped by person
    without ``mars_core`` holding anything that identifies one: no name, no
    national ID, no phone number, no date of birth.

    The link back to a real person lives in ``mars_identity`` and is reachable
    only through the vault, under a permission no role is granted by default.
    A service reading this table learns that two encounters share a person, and
    nothing else about who that person is.
    """

    __tablename__ = "patient_reference"
    __table_args__ = (
        Index("ix_patient_reference_first_seen", "first_seen_on"),
        {
            "schema": CORE,
            "comment": (
                "A person as mars_core is allowed to know them: no name, no "
                "national ID, no phone, no date of birth. The vault that "
                "resolves this to a real person lives in mars_identity."
            ),
        },
    )

    #: Set only when a linkage token resolved. Null means the encounter could
    #: not be attributed to a known person, which is the common case for a
    #: register row carrying no usable identifier - and is recorded as such
    #: rather than papered over with a per-row synthetic person.
    linkage_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )

    #: Earliest and latest encounter dates attributed to this reference. Held
    #: here so a cohort query does not have to scan every encounter.
    first_seen_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    last_seen_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    encounter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: No delete cascade, deliberately. An encounter is the surveillance record
    #: and outlives any linkage: if a reference is removed - a withdrawn
    #: consent, a corrected match - the visit still happened and must stay in
    #: the counts. The foreign key is ON DELETE SET NULL, and
    #: ``passive_deletes`` lets the database apply it rather than SQLAlchemy
    #: cascading a delete the schema never asked for.
    encounters: Mapped[list[OpdEncounter]] = relationship(
        back_populates="patient_reference", passive_deletes=True
    )


class OpdEncounter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One outpatient visit, from one row of HMIS OPD 002.

    The register is a paper book: a date is written once on a blank row and
    governs every row beneath it until the next date. ``date_assignment_method``
    records how this row got its date, because a row whose date was carried
    forward through a lossy extract is not as certain as one that sat directly
    under a date header, and presenting the two identically would overstate what
    is known.
    """

    __tablename__ = "opd_encounter"
    __table_args__ = (
        # A source row is ingested once. Re-running a batch updates rather than
        # duplicates, and two extracts of the same book cannot double-count.
        UniqueConstraint(
            "source_system",
            "source_row_reference",
            name="uq_opd_encounter_source_row",
        ),
        # The form's own unit rules. Months above 11 or days above 30 should
        # have been written in the next unit up, so a value outside these
        # bounds is a transcription error rather than an unusual patient.
        CheckConstraint(
            "age_value IS NULL OR age_value >= 0",
            name="age_not_negative",
        ),
        CheckConstraint(
            "age_unit <> 'months' OR age_value IS NULL OR age_value <= 11",
            name="age_months_under_a_year",
        ),
        CheckConstraint(
            "age_unit <> 'days' OR age_value IS NULL OR age_value <= 30",
            name="age_days_under_a_month",
        ),
        CheckConstraint(
            "age_unit <> 'years' OR age_value IS NULL OR age_value <= 130",
            name="age_years_plausible",
        ),
        Index("ix_opd_encounter_facility_date", "facility_id", "encounter_date"),
        Index("ix_opd_encounter_date", "encounter_date"),
        Index("ix_opd_encounter_patient", "patient_reference_id"),
        Index("ix_opd_encounter_residence_district", "residence_district_id"),
        {
            "schema": CORE,
            "comment": (
                "One outpatient visit, from one row of HMIS OPD 002 (July "
                "2024). Records what was observed and done; the register has no "
                "outcome column and nothing here evidences treatment failure or "
                "resistance."
            ),
        },
    )

    # -- Where and when ----------------------------------------------------
    facility_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.facility.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: The visit date. See ``date_assignment_method`` for how certain it is.
    encounter_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    date_assignment_method: Mapped[DateAssignmentMethod] = mapped_column(
        pg_enum(DateAssignmentMethod, name="date_assignment_method", schema=CORE),
        nullable=False,
        default=DateAssignmentMethod.SOURCE_SUPPLIED,
    )

    #: Column 1. Restarts at 001 every month, so it identifies a row within a
    #: facility-month and is never used as a patient identifier.
    serial_number: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # -- Who (pseudonymously) ---------------------------------------------
    patient_reference_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.patient_reference.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Column 4, as written. The unit is kept rather than normalised away.
    age_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    age_unit: Mapped[AgeUnit | None] = mapped_column(
        pg_enum(AgeUnit, name="age_unit", schema=CORE), nullable=True
    )

    #: Approximate age in days, for banding only. Approximate because the
    #: register carries no date of birth: a "3 years" entry is anywhere in a
    #: 365-day window. Never presented as an exact age.
    age_days_approx: Mapped[int | None] = mapped_column(Integer, nullable=True)

    sex: Mapped[Sex] = mapped_column(
        pg_enum(Sex, name="sex", schema=CORE), nullable=False, default=Sex.UNKNOWN
    )

    patient_category: Mapped[PatientCategory] = mapped_column(
        pg_enum(PatientCategory, name="patient_category", schema=CORE),
        nullable=False,
        default=PatientCategory.UNKNOWN,
    )

    # -- Residence (column 7) ---------------------------------------------
    #
    # The patient's stated home, which is not the facility's geography. A
    # signal that conflates the two attributes disease to where care was sought
    # rather than to where people live.
    residence_district_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="SET NULL"),
        nullable=True,
    )
    residence_subcounty_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Parish and village are kept as raw text. MARS has no parish or village
    #: boundaries, so there is nothing to resolve them against, and minting an
    #: id for them would be fabricating geography.
    residence_parish_raw: Mapped[str | None] = mapped_column(String(160), nullable=True)
    residence_village_raw: Mapped[str | None] = mapped_column(String(160), nullable=True)

    #: Retained whenever the district or subcounty text could not be resolved,
    #: so an unresolved residence is visible rather than simply absent.
    residence_unresolved_raw: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # -- Presentation and classification ----------------------------------
    attendance_type: Mapped[AttendanceType] = mapped_column(
        pg_enum(AttendanceType, name="attendance_type", schema=CORE),
        nullable=False,
        default=AttendanceType.UNKNOWN,
    )

    #: Column 17. Free text with no controlled vocabulary on the form, so MARS
    #: imposes none and never parses it into structured findings.
    presenting_complaint_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Column 18's printed asterisk marks the *row* as notifiable, by the serial
    #: number. A row with several diagnoses and one star does not say which
    #: diagnosis it refers to, so the flag is held here rather than per
    #: diagnosis - that is what the form actually records.
    notifiable_marked: Mapped[bool] = mapped_column(nullable=False, default=False)

    # -- Malaria testing (column 13) --------------------------------------
    #: Column 13's first sub-column. A presenting sign, not a test result, so it
    #: stays on the encounter while the test itself is its own row.
    fever_present: Mapped[FeverStatus] = mapped_column(
        pg_enum(FeverStatus, name="fever_status", schema=CORE),
        nullable=False,
        default=FeverStatus.UNKNOWN,
    )

    # -- Source provenance -------------------------------------------------
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_batch_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    source_row_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    source_register_page: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ingest_method_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # -- Relationships -----------------------------------------------------
    facility: Mapped[Facility] = relationship()
    patient_reference: Mapped[PatientReference | None] = relationship(back_populates="encounters")
    residence_district: Mapped[GeographyUnit | None] = relationship(
        foreign_keys=[residence_district_id]
    )
    residence_subcounty: Mapped[GeographyUnit | None] = relationship(
        foreign_keys=[residence_subcounty_id]
    )
    diagnoses: Mapped[list[OpdEncounterDiagnosis]] = relationship(
        back_populates="encounter",
        cascade="all, delete-orphan",
        order_by="OpdEncounterDiagnosis.sequence",
    )
    prescriptions: Mapped[list[OpdEncounterPrescription]] = relationship(
        back_populates="encounter",
        cascade="all, delete-orphan",
        order_by="OpdEncounterPrescription.sequence",
    )
    tests: Mapped[list[OpdEncounterTest]] = relationship(
        back_populates="encounter",
        cascade="all, delete-orphan",
        order_by="OpdEncounterTest.sequence",
    )
    referrals: Mapped[list[OpdEncounterReferral]] = relationship(
        back_populates="encounter", cascade="all, delete-orphan"
    )

    @property
    def has_confirmed_malaria_test(self) -> bool:
        """Whether a malaria test was performed and read.

        Used wherever a denominator must be "tested" rather than "attended".
        """
        return any(test.is_read for test in self.tests)

    @property
    def is_malaria_positive(self) -> bool:
        """Whether any recorded test was read positive."""
        return any(test.result is MalariaTestResult.POSITIVE for test in self.tests)


class OpdEncounterDiagnosis(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One diagnosis written in column 18. Repeatable per encounter.

    The register instructs that a diagnosis "should correspond to one of the
    diagnoses listed in the Monthly Health Unit report (HMIS 105)", but the
    column is free text and frequently will not. MARS keeps the words the
    clinician wrote and leaves ``hmis_105_item_id`` null until a mapping is
    genuinely established. An unmatched diagnosis stays visible and unresolved
    rather than being forced to the nearest item, which would silently change
    what a clinician recorded.
    """

    __tablename__ = "opd_encounter_diagnosis"
    __table_args__ = (
        UniqueConstraint(
            "opd_encounter_id", "sequence", name="uq_opd_diagnosis_encounter_sequence"
        ),
        Index("ix_opd_diagnosis_normalised", "diagnosis_normalised"),
        Index("ix_opd_diagnosis_encounter", "opd_encounter_id"),
        {"schema": CORE},
    )

    opd_encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.opd_encounter.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Order written on the row. The form allows continuing onto another line.
    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)

    diagnosis_raw: Mapped[str] = mapped_column(String(300), nullable=False)

    #: Case- and whitespace-normalised, for matching only. Never displayed in
    #: place of the words actually written.
    diagnosis_normalised: Mapped[str] = mapped_column(String(300), nullable=False)

    #: Filled by Prompt 11, once the HMIS 105 item list exists. Null means
    #: unmatched, which is a reportable data-quality fact, not an error.
    hmis_105_item_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    encounter: Mapped[OpdEncounter] = relationship(back_populates="diagnoses")


class OpdEncounterPrescription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One prescription written in column 19. Repeatable per encounter.

    The form prescribes a quantity format - "Number of units per dose x number
    of doses per day x number of days" - and MARS parses it only when it is
    actually present. A prescription that does not match keeps its raw text with
    every parsed field null; a partial parse would be a number nobody wrote.

    The same column also carries assistive devices ("spectacles, wheel chair,
    walking stick"). A device is not a medicine, and ``is_device`` keeps it out
    of medicine analytics without discarding the record.
    """

    __tablename__ = "opd_encounter_prescription"
    __table_args__ = (
        UniqueConstraint(
            "opd_encounter_id", "sequence", name="uq_opd_prescription_encounter_sequence"
        ),
        CheckConstraint(
            "units_per_dose IS NULL OR units_per_dose > 0",
            name="units_positive",
        ),
        CheckConstraint(
            "doses_per_day IS NULL OR doses_per_day > 0",
            name="doses_positive",
        ),
        CheckConstraint("days IS NULL OR days > 0", name="days_positive"),
        Index("ix_opd_prescription_encounter", "opd_encounter_id"),
        Index("ix_opd_prescription_drug", "drug_name_normalised"),
        {"schema": CORE},
    )

    opd_encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.opd_encounter.id", ondelete="CASCADE"),
        nullable=False,
    )

    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)

    prescription_raw: Mapped[str] = mapped_column(String(300), nullable=False)

    drug_name_raw: Mapped[str | None] = mapped_column(String(200), nullable=True)
    drug_name_normalised: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Parsed only from the printed ``n x n x n`` pattern.
    units_per_dose: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    doses_per_day: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    days: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)

    #: The product, null whenever any factor is missing. A dispensed total is
    #: either fully derivable from what was written or it is not known.
    total_units: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    #: True for spectacles, wheelchairs and other devices the column also holds.
    is_device: Mapped[bool] = mapped_column(nullable=False, default=False)

    encounter: Mapped[OpdEncounter] = relationship(back_populates="prescriptions")


class OpdEncounterTest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One diagnostic test from column 13. Repeatable per encounter.

    The register prints a single "Tests Done" cell and its instruction says
    "the kind of test done", singular - so on paper there is one test per row.
    The table permits several anyway, because an e-register that records both a
    blood slide and an RDT would otherwise have to discard one, and discarding a
    performed test would understate testing coverage.

    ``method = not_done`` with a positive or negative result is refused by the
    database. The paper register permits writing that combination; it is a
    transcription error, and storing it would put a phantom result into every
    positivity rate downstream.
    """

    __tablename__ = "opd_encounter_test"
    __table_args__ = (
        UniqueConstraint("opd_encounter_id", "sequence", name="uq_opd_test_encounter_sequence"),
        CheckConstraint(
            "method <> 'not_done' OR result IN ('not_done', 'not_applicable', 'unknown')",
            name="no_result_without_a_test",
        ),
        Index("ix_opd_test_encounter", "opd_encounter_id"),
        Index("ix_opd_test_result", "result"),
        {
            "schema": CORE,
            "comment": (
                "A diagnostic test recorded on HMIS OPD 002 column 13. A "
                "result is evidence of infection at a moment, never of whether "
                "treatment worked."
            ),
        },
    )

    opd_encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.opd_encounter.id", ondelete="CASCADE"),
        nullable=False,
    )

    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)

    method: Mapped[MalariaTestMethod] = mapped_column(
        pg_enum(MalariaTestMethod, name="malaria_test_method", schema=CORE),
        nullable=False,
        default=MalariaTestMethod.UNKNOWN,
    )
    result: Mapped[MalariaTestResult] = mapped_column(
        pg_enum(MalariaTestResult, name="malaria_test_result", schema=CORE),
        nullable=False,
        default=MalariaTestResult.UNKNOWN,
    )

    encounter: Mapped[OpdEncounter] = relationship(back_populates="tests")

    @property
    def is_read(self) -> bool:
        """Whether this test produced a readable result."""
        return self.result in {MalariaTestResult.POSITIVE, MalariaTestResult.NEGATIVE}


class OpdEncounterReferral(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A referral number from column 21 or 22.

    Held as rows with a direction rather than two parallel columns, so a row can
    carry both, neither, or two of one kind without the schema changing.

    Referral numbers are issued per facility with no national scheme. They are
    not unique across facilities and are never used as a linkage key: two
    patients at different facilities can hold the same number, and treating it
    as an identifier would merge them.
    """

    __tablename__ = "opd_encounter_referral"
    __table_args__ = (
        Index("ix_opd_referral_encounter", "opd_encounter_id"),
        {
            "schema": CORE,
            "comment": (
                "Referral numbers from HMIS OPD 002 columns 21 and 22. "
                "Facility-issued and not nationally unique; never a linkage key."
            ),
        },
    )

    opd_encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.opd_encounter.id", ondelete="CASCADE"),
        nullable=False,
    )

    direction: Mapped[ReferralDirection] = mapped_column(
        pg_enum(ReferralDirection, name="referral_direction", schema=CORE), nullable=False
    )

    referral_number: Mapped[str] = mapped_column(String(64), nullable=False)

    encounter: Mapped[OpdEncounter] = relationship(back_populates="referrals")


__all__ = [
    "OpdEncounter",
    "OpdEncounterDiagnosis",
    "OpdEncounterPrescription",
    "OpdEncounterReferral",
    "OpdEncounterTest",
    "PatientReference",
]
