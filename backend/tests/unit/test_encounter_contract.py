"""The inbound contract and the validator, without a database.

These assert the two rules the validator exists to hold: **an unknown code is
never coerced**, and **blank is never zero**. Both are the kind of defect that
does not surface as an error - it surfaces months later as a testing-coverage
figure that looks slightly wrong, and by then the source of it is gone.

The contract tests assert what the reader *refuses*, because everything it
refuses is something that would otherwise be silently mis-loaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mars.domain.enums import (
    AgeUnit,
    MalariaTestMethod,
    MalariaTestResult,
    PatientCategory,
    Sex,
    ValidationSeverity,
)
from mars.ingestion.encounters.contract import (
    SUPPORTED_SCHEMA_VERSIONS,
    ContractError,
    InboundIdentity,
    InboundRow,
    JsonLinesAdapter,
)
from mars.ingestion.encounters.validation import EncounterValidator

ENVELOPE = {
    "record_type": "envelope",
    "schema_version": "1.0",
    "source_system": "ereg-test",
    "facility_code": "HF-001",
    "extracted_at": "2026-03-05T08:00:00Z",
    "row_count": 1,
}


def write_batch(tmp_path: Path, *rows: dict[str, object], envelope: dict | None = None) -> Path:
    artefact = tmp_path / "batch.jsonl"
    lines = [json.dumps(envelope if envelope is not None else {**ENVELOPE, "row_count": len(rows)})]
    lines.extend(json.dumps(row) for row in rows)
    artefact.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artefact


def row(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "record_type": "encounter",
        "source_row_id": "r-001",
        "encounter_date": "2026-03-04",
        "sex": "F",
        "patient_category": "N",
        "age": {"value": 4, "unit": "years"},
    }
    payload.update(overrides)
    return payload


def validate(**overrides: object):
    return EncounterValidator().validate(
        InboundRow(source_row_id="r-001", line_number=2, raw=row(**overrides))
    )


def codes(validation) -> set[str]:
    return {issue.code for issue in validation.issues}


class TestTheReaderRefusesRatherThanGuesses:
    def test_an_envelope_is_required(self, tmp_path: Path) -> None:
        artefact = tmp_path / "empty.jsonl"
        artefact.write_text("", encoding="utf-8")
        with pytest.raises(ContractError, match="empty"):
            JsonLinesAdapter().envelope(artefact)

    def test_a_row_without_a_source_row_id_is_refused(self, tmp_path: Path) -> None:
        """It is what makes a replay idempotent, so it cannot be defaulted.

        Generating one would make every re-send a new encounter.
        """
        artefact = write_batch(tmp_path, {k: v for k, v in row().items() if k != "source_row_id"})
        with pytest.raises(ContractError, match="source_row_id"):
            list(JsonLinesAdapter().rows(artefact))

    def test_a_malformed_line_raises_rather_than_being_skipped(self, tmp_path: Path) -> None:
        """A skipped line is a row nobody knows is missing."""
        artefact = tmp_path / "broken.jsonl"
        artefact.write_text(
            json.dumps({**ENVELOPE, "row_count": 1}) + "\n{not json}\n", encoding="utf-8"
        )
        with pytest.raises(ContractError, match="line 2"):
            list(JsonLinesAdapter().rows(artefact))

    def test_next_of_kin_is_refused_not_dropped(self, tmp_path: Path) -> None:
        """OPD 002 column 8 exists; MARS stores it nowhere.

        Accepting and dropping it would leave the producer believing MARS holds
        it, which is worse than either keeping or refusing it.
        """
        artefact = write_batch(tmp_path, row(next_of_kin="A Person"))
        with pytest.raises(ContractError, match="next_of_kin"):
            list(JsonLinesAdapter().rows(artefact))

    def test_the_supported_version_set_is_explicit(self) -> None:
        assert sorted(SUPPORTED_SCHEMA_VERSIONS) == ["1.0"]


class TestIdentityNeverRendersItself:
    def test_repr_names_which_fields_are_present_and_no_values(self) -> None:
        """A repr reaches a traceback, and a traceback reaches a log."""
        identity = InboundIdentity(
            identifier_type="national_id",
            identifier_value="CM90210ABCDE",
            surname="Nakato",
            given_name="Sarah",
            phone_contact="0772123456",
        )
        rendered = repr(identity)
        for secret in ("CM90210ABCDE", "Nakato", "Sarah", "0772123456"):
            assert secret not in rendered
        assert "identifier" in rendered and "surname" in rendered

    def test_the_redacted_payload_has_no_identity_object(self) -> None:
        payload = row(identity={"identifier_value": "CM90210ABCDE", "surname": "Nakato"})
        inbound = InboundRow(source_row_id="r-001", line_number=2, raw=payload)
        assert "identity" not in inbound.redacted
        assert "CM90210ABCDE" not in json.dumps(inbound.redacted)


class TestAnUnknownCodeIsNeverCoerced:
    def test_an_unrecognised_sex_records_an_error_and_blocks_the_row(self) -> None:
        validation = validate(sex="X")
        assert "unrecognised_code" in codes(validation)
        assert not validation.is_loadable

    def test_the_issue_names_the_accepted_set_and_not_a_patient_value(self) -> None:
        """A producer must be able to fix the mapping from the issue alone."""
        issue = next(i for i in validate(patient_category="Q").issues if i.field_path)
        assert issue.context is not None
        assert "national" in issue.context["accepted"]
        assert "Q" not in issue.message

    def test_an_absent_code_takes_the_documented_default_without_an_issue(self) -> None:
        """Absent is not the same as wrong. Only a *wrong* code is an issue."""
        validation = validate(patient_category=None)
        assert validation.is_loadable
        assert validation.encounter is not None
        assert validation.encounter.patient_category is PatientCategory.UNKNOWN
        assert "unrecognised_code" not in codes(validation)

    def test_a_recognised_code_maps(self) -> None:
        validation = validate(sex="male")
        assert validation.encounter is not None
        assert validation.encounter.sex is Sex.MALE


class TestBlankIsNeverZero:
    def test_no_tests_block_produces_no_tests_rather_than_a_negative(self) -> None:
        """ "No test was done" and "a test found nothing" are different facts."""
        validation = validate(tests=None)
        assert validation.encounter is not None
        assert validation.encounter.tests == []

    def test_an_age_value_without_a_unit_is_refused(self) -> None:
        """Read as years by anything assuming a default, a three-day-old
        becomes a three-year-old and never looks wrong again."""
        validation = validate(age={"value": 3})
        assert "age_pair_incomplete" in codes(validation)
        assert not validation.is_loadable

    def test_neither_value_nor_unit_is_accepted_as_not_recorded(self) -> None:
        validation = validate(age={})
        assert validation.is_loadable
        assert validation.encounter is not None
        assert validation.encounter.age_value is None
        assert validation.encounter.age_unit is None

    @pytest.mark.parametrize(
        ("value", "unit"),
        [(12, "months"), (31, "days"), (131, "years")],
    )
    def test_an_age_outside_the_forms_unit_rules_is_refused(self, value: int, unit: str) -> None:
        validation = validate(age={"value": value, "unit": unit})
        assert "age_outside_unit_bounds" in codes(validation)

    def test_a_valid_age_records_its_unit(self) -> None:
        validation = validate(age={"value": 8, "unit": "months"})
        assert validation.encounter is not None
        assert validation.encounter.age_unit is AgeUnit.MONTHS
        assert validation.encounter.age_value == 8


class TestTheContradictionsTheFormAllows:
    def test_a_read_result_with_no_test_performed_is_refused(self) -> None:
        """The paper register permits writing it. It is a transcription error,
        and storing it puts a phantom result into every positivity rate."""
        validation = validate(tests=[{"method": "not_done", "result": "positive"}])
        assert "result_without_a_test" in codes(validation)
        assert not validation.is_loadable

    def test_not_done_with_not_done_is_accepted(self) -> None:
        validation = validate(tests=[{"method": "not_done", "result": "not_done"}])
        assert validation.is_loadable
        assert validation.encounter is not None
        assert validation.encounter.tests == [
            (MalariaTestMethod.NOT_DONE, MalariaTestResult.NOT_DONE)
        ]

    def test_a_row_without_a_date_cannot_be_a_surveillance_record(self) -> None:
        validation = validate(encounter_date=None)
        assert validation.encounter is None
        assert "encounter_date_missing" in codes(validation)

    def test_a_future_date_is_refused(self) -> None:
        validation = validate(encounter_date="2099-01-01")
        assert "encounter_date_in_future" in codes(validation)
        assert validation.encounter is None


class TestIssueSeverity:
    def test_every_issue_the_validator_raises_names_a_severity(self) -> None:
        validation = validate(sex="X", age={"value": 3})
        assert validation.issues
        for issue in validation.issues:
            assert isinstance(issue.severity, ValidationSeverity)
            assert issue.code
            assert issue.message
