"""The encounter model against the form it claims to come from.

These run without a database. They exist because the claim "source-grounded"
is easy to make and easy to quietly break: a field added for convenience, a code
invented because it seemed obvious, a direct identifier that creeps into
``mars_core`` because it was in the extract.

The strongest test here is :class:`TestNoDirectIdentifierReachesCore`. If it
ever fails, MARS is storing identity in the wrong schema.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from mars.db.schemas import CORE
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

DICTIONARY = Path(__file__).resolve().parents[3] / "docs" / "data-dictionary" / "opd-002.md"

ENCOUNTER_TABLES = [
    PatientReference,
    OpdEncounter,
    OpdEncounterDiagnosis,
    OpdEncounterPrescription,
    OpdEncounterTest,
    OpdEncounterReferral,
]


class TestNoDirectIdentifierReachesCore:
    """``mars_core`` must hold nothing that identifies a person.

    OPD 002 columns 2, 3 and 8 carry a national ID, the patient's name and
    phone, and a next of kin's name and phone. ADR 0006 puts direct identity in
    ``mars_identity`` and nowhere else. This asserts the encounter tables have
    no column that could hold one, by name.
    """

    #: Substrings that would indicate an identifying value. Deliberately broad:
    #: a false positive costs a rename, a false negative costs a data breach.
    FORBIDDEN: ClassVar[tuple[str, ...]] = (
        "name",
        "nin",
        "national_id",
        "phone",
        "telephone",
        "contact",
        "surname",
        "given",
        "kin",
        "birth",
        "dob",
        "passport",
        "address",
    )

    #: Columns whose name trips the scan but which hold no identity.
    #:
    #: ``*_raw`` residence fields hold place names, not person names. The
    #: diagnosis and drug name columns hold clinical vocabulary. Each is listed
    #: individually so adding a genuinely identifying column cannot be waved
    #: through by widening a pattern.
    ALLOWED: ClassVar[set[str]] = {
        "residence_parish_raw",
        "residence_village_raw",
        "residence_unresolved_raw",
        "diagnosis_raw",
        "diagnosis_normalised",
        "drug_name_raw",
        "drug_name_normalised",
    }

    @pytest.mark.parametrize("model", ENCOUNTER_TABLES, ids=lambda m: m.__tablename__)
    def test_no_column_could_hold_an_identifier(self, model: type) -> None:
        offenders = [
            column.name
            for column in model.__table__.columns
            if column.name not in self.ALLOWED
            and any(word in column.name.lower() for word in self.FORBIDDEN)
        ]
        assert not offenders, (
            f"{model.__tablename__} has columns that could hold direct identity: "
            f"{offenders}. Identity belongs in mars_identity (ADR 0006)."
        )

    def test_next_of_kin_is_absent_entirely(self) -> None:
        """Column 8 is not stored in either schema.

        A next of kin is a third party who did not attend. Surveillance has no
        purpose for their contact details, so the vault is not the answer
        either - the answer is not to store them.
        """
        every_column = {
            column.name for model in ENCOUNTER_TABLES for column in model.__table__.columns
        }
        assert not [c for c in every_column if "kin" in c.lower()]

    def test_patient_reference_holds_no_attributes_of_a_person(self) -> None:
        """It groups encounters; it does not describe anyone."""
        columns = {c.name for c in PatientReference.__table__.columns}
        assert columns == {
            "id",
            "linkage_token_id",
            "first_seen_on",
            "last_seen_on",
            "encounter_count",
            "created_at",
            "updated_at",
        }

    def test_every_encounter_table_lives_in_core(self) -> None:
        for model in ENCOUNTER_TABLES:
            assert model.__table__.schema == CORE


class TestEnumsMatchThePrintedForm:
    """Every value is a printed code, or a documented MARS addition.

    The form's codes are quoted in ``docs/data-dictionary/opd-002.md``. A value
    that appears here and not on the form is an invention, which is exactly what
    a source-grounded model must not contain.
    """

    def test_age_units_are_the_three_the_form_uses(self) -> None:
        """Column 4: complete years, months under a year, days under a month."""
        assert {u.value for u in AgeUnit} == {"years", "months", "days"}

    def test_sex_has_only_the_two_printed_codes_plus_unknown(self) -> None:
        """Column 5 prints M and F. ``unknown`` is a MARS value for a blank."""
        assert {s.value for s in Sex} == {"male", "female", "unknown"}

    def test_patient_category_matches_n_r_f(self) -> None:
        """Column 6: N national, R refugee, F foreigner."""
        assert {c.value for c in PatientCategory} == {
            "national",
            "refugee",
            "foreigner",
            "unknown",
        }

    def test_attendance_has_exactly_the_two_ticks(self) -> None:
        """Column 16 offers New Attendance and Re-Attendance and nothing else."""
        assert {a.value for a in AttendanceType} == {
            "new_attendance",
            "re_attendance",
            "unknown",
        }

    def test_malaria_test_methods_are_the_printed_ones(self) -> None:
        """Column 13: B/S microscopy, RDT, ND not done."""
        assert {m.value for m in MalariaTestMethod} == {
            "microscopy",
            "rdt",
            "not_done",
            "unknown",
        }

    def test_not_done_and_not_applicable_stay_distinct(self) -> None:
        """The form's instructions say ``ND``; its grid header says ``NA``.

        Collapsing them would assert the two mean the same thing, which the
        form does not say. Both are kept and the disagreement is documented.
        """
        values = {r.value for r in MalariaTestResult}
        assert "not_done" in values
        assert "not_applicable" in values

    def test_date_assignment_records_how_certain_a_date_is(self) -> None:
        """The register writes a date once and carries it down the page."""
        assert {d.value for d in DateAssignmentMethod} == {
            "row_header",
            "carried_forward",
            "source_supplied",
            "unresolved",
        }

    def test_fever_is_the_printed_yes_no(self) -> None:
        assert {f.value for f in FeverStatus} == {"yes", "no", "unknown"}


class TestTheModelRecordsWhatTheFormRecords:
    """Structure that follows from what the register actually is."""

    def test_one_row_is_one_visit_not_one_patient(self) -> None:
        """The patient reference is nullable and not unique.

        A register row often carries no usable identifier. Requiring a patient
        would force a synthetic one per row, which would make every visitor
        look like a different person - and would make re-attendance analysis
        meaningless.
        """
        column = OpdEncounter.__table__.columns["patient_reference_id"]
        assert column.nullable
        assert not column.unique

    def test_diagnoses_prescriptions_and_tests_are_repeatable(self) -> None:
        """Columns 18 and 19 say "if more space is required, use another line".

        Tests are repeatable too. The register prints one "Tests Done" cell, so
        on paper there is one per row - but an e-register recording both a slide
        and an RDT would otherwise have to discard one, and a discarded test
        understates testing coverage.
        """
        for model in (OpdEncounterDiagnosis, OpdEncounterPrescription, OpdEncounterTest):
            assert "sequence" in model.__table__.columns
            assert "opd_encounter_id" in model.__table__.columns

    def test_referral_direction_replaces_two_parallel_columns(self) -> None:
        """Columns 21 and 22 differ only in direction."""
        assert {d.value for d in ReferralDirection} == {"inbound", "outbound"}
        columns = {c.name for c in OpdEncounterReferral.__table__.columns}
        assert {"direction", "referral_number", "opd_encounter_id"} <= columns
        assert "referral_in_number" not in {c.name for c in OpdEncounter.__table__.columns}

    def test_there_is_no_outcome_column(self) -> None:
        """The register has none.

        The only disposition it records is whether a referral note was written.
        A column named for an outcome would invite a surface that claims one.
        """
        # Matched on whole underscore-separated tokens. A substring scan would
        # trip on "residence_unresolved_raw", which is about whether a place
        # name resolved to a geography unit and has nothing to do with a
        # clinical outcome.
        tokens = {token for c in OpdEncounter.__table__.columns for token in c.name.split("_")}
        for forbidden in ("outcome", "disposition", "discharge", "cured", "resolution"):
            assert forbidden not in tokens, f"found a column token named {forbidden}"

    def test_nothing_claims_treatment_success_or_failure(self) -> None:
        """ADR 0005: routine data may never carry confirmatory language."""
        columns = {c.name for model in ENCOUNTER_TABLES for c in model.__table__.columns}
        for forbidden in ("resistance", "resistant", "failure", "failed_treatment", "efficacy"):
            assert not [c for c in columns if forbidden in c], f"found {forbidden}"

    def test_the_age_unit_is_kept_alongside_the_value(self) -> None:
        """Converting a three-day-old to years would discard what the form captured."""
        columns = OpdEncounter.__table__.columns
        assert "age_value" in columns
        assert "age_unit" in columns

    def test_the_derived_age_is_marked_approximate(self) -> None:
        """The register carries no date of birth, so no exact age is derivable."""
        assert "age_days_approx" in OpdEncounter.__table__.columns

    def test_a_source_row_can_be_ingested_only_once(self) -> None:
        constraints = {c.name for c in OpdEncounter.__table__.constraints if c.name is not None}
        assert "uq_opd_encounter_source_row" in constraints

    def test_the_encounter_carries_its_provenance(self) -> None:
        columns = {c.name for c in OpdEncounter.__table__.columns}
        assert {
            "source_system",
            "source_row_reference",
            "source_batch_id",
            "source_register_page",
            "ingest_method_version",
            "date_assignment_method",
        } <= columns

    def test_residence_is_separate_from_the_facility(self) -> None:
        """Column 7 is where the patient lives, not where they sought care.

        Conflating them attributes disease to the facility's subcounty rather
        than the patient's.
        """
        columns = {c.name for c in OpdEncounter.__table__.columns}
        assert "facility_id" in columns
        assert "residence_district_id" in columns
        assert "residence_subcounty_id" in columns

    def test_parish_and_village_stay_raw_text(self) -> None:
        """MARS has no parish or village boundaries to resolve them against."""
        columns = OpdEncounter.__table__.columns
        assert columns["residence_parish_raw"].type.python_type is str
        assert columns["residence_village_raw"].type.python_type is str
        assert not columns["residence_parish_raw"].foreign_keys
        assert not columns["residence_village_raw"].foreign_keys


class TestTheDataDictionaryMatchesTheModel:
    """The dictionary is the deliverable; the model must not outgrow it.

    A column that exists in the database but appears nowhere in the dictionary
    is a field nobody can trace back to the form.
    """

    #: Columns that are MARS bookkeeping rather than form fields, and so are not
    #: expected to appear as dictionary entries.
    STRUCTURAL: ClassVar[set[str]] = {
        "id",
        "created_at",
        "updated_at",
        "opd_encounter_id",
        "sequence",
        "linkage_token_id",
        "first_seen_on",
        "last_seen_on",
        "encounter_count",
        "patient_reference_id",
        "facility_id",
        "hmis_105_item_id",
        "is_device",
        "drug_name_raw",
        "drug_name_normalised",
        "diagnosis_normalised",
        "age_days_approx",
        "residence_unresolved_raw",
        "source_batch_id",
        "source_register_page",
        "ingest_method_version",
        "total_units",
        "method",
        "result",
        "direction",
    }

    def test_the_dictionary_exists_and_names_the_form(self) -> None:
        text = DICTIONARY.read_text(encoding="utf-8")
        assert "HMIS OPD 002" in text
        assert "July 2024" in text

    def test_the_dictionary_records_the_source_checksum(self) -> None:
        """A citation to a form is only verifiable if the bytes are identified."""
        text = DICTIONARY.read_text(encoding="utf-8")
        assert re.search(r"\b[0-9a-f]{64}\b", text), "no SHA-256 in the dictionary"

    @pytest.mark.parametrize("model", ENCOUNTER_TABLES, ids=lambda m: m.__tablename__)
    def test_every_form_derived_column_appears_in_the_dictionary(self, model: type) -> None:
        text = DICTIONARY.read_text(encoding="utf-8")
        missing = [
            column.name
            for column in model.__table__.columns
            if column.name not in self.STRUCTURAL and column.name not in text
        ]
        assert not missing, (
            f"{model.__tablename__} columns absent from the data dictionary: "
            f"{missing}. Every field must be traceable to a printed label."
        )

    def test_the_dictionary_records_the_columns_it_refuses_to_model(self) -> None:
        """Nutrition, TB/HIV, leprosy, disability and the rest.

        Omitting them silently would look like an oversight. The dictionary
        names each one and gives the reason, so the decision can be challenged.
        """
        text = DICTIONARY.read_text(encoding="utf-8")
        assert "Deliberately out of scope" in text
        for column in ("Nutrition assessment", "TB / HIV", "Leprosy", "Disability"):
            assert column in text, f"{column} not accounted for"

    def test_the_dictionary_records_the_ambiguities(self) -> None:
        """A form that disagrees with itself is a fact about the source."""
        text = DICTIONARY.read_text(encoding="utf-8")
        assert text.count("**Ambiguity**") >= 8
