"""The HMIS forms as MARS transcribes them, and the validator that guards them.

Two things are being protected here.

**The transcription.** Every code and label came off a printed form. A test that
merely restates the code it is checking proves nothing, so these assert the
*shape* the form imposes: which elements are disaggregated, which codes MARS
assigned rather than read, which block a code belongs to.

**The two rules that are easy to lose.** An unknown code is never guessed, and a
blank cell is never a zero. Both are the kind of defect that produces a
plausible number rather than an error.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mars.domain.enums import (
    AgeBand,
    AggregateForm,
    AggregatePeriodType,
    Sex,
    StockMetric,
    ValidationSeverity,
)
from mars.domain.hmis_elements import (
    ALL_ELEMENTS,
    COMMODITY_CODES,
    ELEMENTS_BY_CODE,
    HMIS_033B_ELEMENTS,
    HMIS_105_AGE_BANDS,
    HMIS_105_ELEMENTS,
    HMIS_105_LABORATORY_TESTS,
    MARS_ASSIGNED_PREFIX,
    elements_for,
)
from mars.ingestion.aggregate.contract import (
    FORBIDDEN_SUBMISSION_FIELDS,
    AggregateContractError,
    JsonLinesAggregateAdapter,
)
from mars.ingestion.aggregate.validation import FORM_PERIODS, AggregateValidator

ENVELOPE = {
    "record_type": "envelope",
    "schema_version": "1.0",
    "source_system": "hmis-test",
    "extracted_at": "2026-04-05T08:00:00Z",
    "submission_count": 1,
}


def submission(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "record_type": "submission",
        "form": "hmis_105",
        "facility_code": "HF-401",
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
        "period_label": "March",
        "observations": [
            {"element": "EP01b", "age_band": "years_5_9", "sex": "male", "value": 40},
            {"element": "EP01c", "age_band": "years_5_9", "sex": "male", "value": 12},
        ],
    }
    payload.update(overrides)
    return payload


def write(tmp_path: Path, *rows: dict[str, object]) -> Path:
    artefact = tmp_path / "returns.jsonl"
    lines = [json.dumps({**ENVELOPE, "submission_count": len(rows)})]
    lines.extend(json.dumps(row) for row in rows)
    artefact.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artefact


def validate(tmp_path: Path, payload: dict[str, object]):
    artefact = write(tmp_path, payload)
    inbound = next(iter(JsonLinesAggregateAdapter().submissions(artefact)))
    return AggregateValidator().validate(inbound)


def codes(validation) -> set[str]:
    return {issue.code for issue in validation.issues}


class TestTheTranscription:
    def test_the_malaria_block_on_105_has_the_forms_five_sub_rows(self) -> None:
        """EP01a-e. Collapsing them would lose the difference between a
        confirmed case and a treated one."""
        malaria = [e.code for e in HMIS_105_ELEMENTS if e.code.startswith("EP01")]
        assert malaria == ["EP01a", "EP01b", "EP01c", "EP01d", "EP01e"]

    def test_every_105_diagnosis_element_is_disaggregated(self) -> None:
        """The form prints five age bands and two sexes for the whole of
        section 1. An element MARS marked as a single total would be a
        transcription error."""
        for element in HMIS_105_ELEMENTS:
            assert element.disaggregated, element.code
            assert element.age_bands == HMIS_105_AGE_BANDS

    def test_the_033b_weekly_elements_carry_no_age_band(self) -> None:
        """033b sections 1, 4 and 5 print a single total per field."""
        for element in HMIS_033B_ELEMENTS:
            assert not element.disaggregated, element.code
            assert element.age_bands == (AgeBand.UNSPECIFIED,)

    def test_codes_mars_assigned_are_marked_and_prefixed(self) -> None:
        """A reader must be able to tell a code read off the form from one MARS
        invented, without opening the PDF."""
        for element in ALL_ELEMENTS:
            if element.code.startswith(MARS_ASSIGNED_PREFIX):
                assert element.code_assigned_by_mars, element.code

    def test_codes_read_from_the_form_are_not_marked_as_assigned(self) -> None:
        for code in ("EP01a", "EP01b", "OA01", "PS01", "PS02", "SS01", "SS34", "MA."):
            assert code in ELEMENTS_BY_CODE, code
            assert not ELEMENTS_BY_CODE[code].code_assigned_by_mars, code

    def test_every_element_names_the_section_it_came_from(self) -> None:
        """So a data clerk and an engineer can find the same cell."""
        for element in ALL_ELEMENTS:
            assert element.section.strip(), element.code

    def test_the_laboratory_block_is_the_two_malaria_parasitology_rows(self) -> None:
        assert [e.code for e in HMIS_105_LABORATORY_TESTS] == ["PS01", "PS02"]
        assert {e.label for e in HMIS_105_LABORATORY_TESTS} == {
            "Malaria Microscopy",
            "Malaria RDTs",
        }

    def test_the_commodities_include_the_antimalarials_and_the_rdts(self) -> None:
        assert {"SS01", "SS02", "SS24", "SS34"} <= COMMODITY_CODES

    def test_each_form_yields_only_its_own_elements(self) -> None:
        for form in AggregateForm:
            for element in elements_for(form):
                assert element.form is form

    def test_the_period_type_matches_each_forms_stated_frequency(self) -> None:
        """033b: 'Every Monday of the Week'. 105: '7th day of the following
        month'."""
        assert FORM_PERIODS[AggregateForm.HMIS_033B] is AggregatePeriodType.WEEK
        assert FORM_PERIODS[AggregateForm.HMIS_105] is AggregatePeriodType.MONTH


class TestAnUnknownCodeIsNeverGuessed:
    def test_an_element_mars_does_not_hold_is_refused(self, tmp_path: Path) -> None:
        validation = validate(tmp_path, submission(observations=[{"element": "EP01", "value": 5}]))
        assert "unknown_element" in codes(validation)
        assert not validation.is_loadable

    def test_the_issue_names_the_form_so_a_producer_can_fix_the_mapping(
        self, tmp_path: Path
    ) -> None:
        validation = validate(
            tmp_path, submission(observations=[{"element": "NOPE99", "value": 5}])
        )
        issue = next(i for i in validation.issues if i.code == "unknown_element")
        assert issue.context is not None
        assert issue.context["form"] == "hmis_105"

    def test_a_stock_code_in_the_observation_block_is_refused(self, tmp_path: Path) -> None:
        """A commodity has its own four columns; storing it as a cell would
        lose which measure the number is."""
        validation = validate(tmp_path, submission(observations=[{"element": "SS34", "value": 5}]))
        assert "element_in_the_wrong_block" in codes(validation)

    def test_an_unknown_form_is_refused(self, tmp_path: Path) -> None:
        validation = validate(tmp_path, submission(form="hmis_999"))
        assert "unknown_form" in codes(validation)
        assert validation.submission is None


class TestABlankCellIsNotAZero:
    def test_a_null_value_stays_null(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {"element": "EP01b", "age_band": "years_5_9", "sex": "male", "value": None}
                ]
            ),
        )
        assert validation.is_loadable
        assert validation.submission is not None
        assert validation.submission.observations[0].value is None

    def test_a_reported_zero_stays_zero(self, tmp_path: Path) -> None:
        """033b requires reporting whether there are cases or not, so a zero is
        a statement the facility made."""
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {"element": "EP01b", "age_band": "years_5_9", "sex": "male", "value": 0}
                ]
            ),
        )
        assert validation.submission is not None
        assert validation.submission.observations[0].value == 0

    def test_a_non_numeric_cell_is_a_warning_and_stored_as_blank(self, tmp_path: Path) -> None:
        """'nil' and an illegible mark are different, and a transcriber should
        see which."""
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {
                        "element": "EP01b",
                        "age_band": "years_5_9",
                        "sex": "male",
                        "value": "nil",
                        "raw_value": "nil",
                    }
                ]
            ),
        )
        issue = next(i for i in validation.issues if i.code == "value_not_numeric")
        assert issue.severity is ValidationSeverity.WARNING
        assert validation.is_loadable
        assert validation.submission is not None
        assert validation.submission.observations[0].value is None
        assert validation.submission.observations[0].raw_value == "nil"

    def test_a_submission_with_no_cells_at_all_is_refused(self, tmp_path: Path) -> None:
        """A zero report has cells containing zero, not no cells."""
        validation = validate(tmp_path, submission(observations=[]))
        assert "submission_is_empty" in codes(validation)


class TestMarsDoesNotReBand:
    def test_a_disaggregated_element_without_a_band_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(observations=[{"element": "EP01b", "value": 40}]),
        )
        assert "disaggregation_missing" in codes(validation)

    def test_a_single_total_element_with_a_band_is_refused(self, tmp_path: Path) -> None:
        """A band here means the producer split a figure MARS cannot verify."""
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "age_band": "years_5_9", "value": 3}],
            ),
        )
        assert "unexpected_disaggregation" in codes(validation)

    def test_a_band_outside_the_forms_own_set_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(observations=[{"element": "EP01b", "age_band": "years_1_4", "value": 40}]),
        )
        assert "unknown_age_band" in codes(validation)

    def test_the_forms_own_bands_are_accepted(self, tmp_path: Path) -> None:
        rows = [
            {"element": "EP01b", "age_band": band.value, "sex": "female", "value": 3}
            for band in HMIS_105_AGE_BANDS
        ]
        validation = validate(tmp_path, submission(observations=rows))
        assert validation.is_loadable
        assert validation.submission is not None
        assert len(validation.submission.observations) == len(HMIS_105_AGE_BANDS)

    def test_the_same_cell_twice_is_refused(self, tmp_path: Path) -> None:
        rows = [
            {"element": "EP01b", "age_band": "years_5_9", "sex": "male", "value": 3},
            {"element": "EP01b", "age_band": "years_5_9", "sex": "male", "value": 4},
        ]
        validation = validate(tmp_path, submission(observations=rows))
        assert "duplicate_cell" in codes(validation)


class TestThePeriodMustBeTheShapeTheFormPrints:
    def test_a_033b_week_runs_monday_to_sunday(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "value": 12}],
            ),
        )
        assert validation.is_loadable
        assert validation.submission is not None
        assert validation.submission.period_type is AggregatePeriodType.WEEK

    def test_a_week_starting_on_a_wednesday_is_refused(self, tmp_path: Path) -> None:
        """A week on other days silently overlaps its neighbours."""
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-04",
                period_end="2026-03-10",
                observations=[{"element": "MA.", "value": 12}],
            ),
        )
        assert "week_does_not_start_on_monday" in codes(validation)

    def test_a_weekly_form_covering_a_quarter_is_refused(self, tmp_path: Path) -> None:
        """It would later be summed with real weeks and look ordinary."""
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-01-05",
                period_end="2026-03-29",
                observations=[{"element": "MA.", "value": 900}],
            ),
        )
        assert "period_is_not_a_week" in codes(validation)

    def test_a_105_period_must_be_a_whole_calendar_month(self, tmp_path: Path) -> None:
        validation = validate(tmp_path, submission(period_end="2026-03-30"))
        assert "month_does_not_end_on_the_last_day" in codes(validation)

    def test_a_month_not_starting_on_the_first_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path, submission(period_start="2026-03-02", period_end="2026-04-01")
        )
        assert "month_does_not_start_on_the_first" in codes(validation)

    def test_february_is_accepted_at_its_own_length(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path, submission(period_start="2026-02-01", period_end="2026-02-28")
        )
        assert validation.is_loadable


class TestArithmeticImpossibilities:
    def test_more_positives_than_tests_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(laboratory=[{"test": "PS02", "done": 10, "positive": 12}]),
        )
        assert "more_positive_than_done" in codes(validation)

    def test_equal_positives_and_tests_is_accepted(self, tmp_path: Path) -> None:
        """Unusual, not impossible. Refusing it would lose a real outbreak week."""
        validation = validate(
            tmp_path,
            submission(laboratory=[{"test": "PS02", "done": 10, "positive": 10}]),
        )
        assert validation.is_loadable

    def test_a_negative_count_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {"element": "EP01b", "age_band": "years_5_9", "sex": "male", "value": -3}
                ]
            ),
        )
        assert "value_negative" in codes(validation)

    def test_more_days_out_of_stock_than_the_month_has_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(stock=[{"commodity": "SS34", "metric": "days_out_of_stock", "value": 45}]),
        )
        assert "days_out_of_stock_exceeds_period" in codes(validation)

    def test_033b_refuses_days_out_of_stock_because_it_prints_balance_only(
        self, tmp_path: Path
    ) -> None:
        """033b sections 7 and 8 are headed STOCK BALANCE; the days-out
        column belongs to monthly HMIS 105 and must not be invented here."""
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "value": 1}],
                stock=[{"commodity": "M033B_TRA_RDT", "metric": "days_out_of_stock", "value": 9}],
            ),
        )
        assert "unknown_stock_metric" in codes(validation)

    def test_a_weekly_stock_balance_is_accepted(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "value": 1}],
                stock=[{"commodity": "M033B_TRA_RDT", "metric": "stock_on_hand", "value": 7}],
            ),
        )
        assert validation.is_loadable


class TestBlocksBelongToTheirOwnForm:
    def test_a_laboratory_row_on_the_weekly_form_is_refused(self, tmp_path: Path) -> None:
        """Section 10 is on HMIS 105. Accepting it would put a monthly quantity
        into a weekly submission."""
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "value": 1}],
                laboratory=[{"test": "PS02", "done": 10, "positive": 3}],
            ),
        )
        assert "laboratory_block_on_the_wrong_form" in codes(validation)

    def test_a_105_commodity_on_the_weekly_form_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "value": 1}],
                stock=[{"commodity": "SS34", "metric": "days_out_of_stock", "value": 2}],
            ),
        )
        assert "unknown_commodity" in codes(validation)

    def test_the_weekly_tracer_item_is_accepted_on_033b(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                form="hmis_033b",
                period_start="2026-03-02",
                period_end="2026-03-08",
                observations=[{"element": "MA.", "value": 1}],
                stock=[{"commodity": "M033B_TRA_RDT", "metric": "stock_on_hand", "value": 240}],
            ),
        )
        assert validation.is_loadable
        assert validation.submission is not None
        assert validation.submission.stock[0].metric is StockMetric.STOCK_ON_HAND


class TestTheReaderRefusesRatherThanGuesses:
    def test_an_envelope_is_required(self, tmp_path: Path) -> None:
        artefact = tmp_path / "empty.jsonl"
        artefact.write_text("", encoding="utf-8")
        with pytest.raises(AggregateContractError, match="empty"):
            JsonLinesAggregateAdapter().envelope(artefact)

    def test_a_submission_without_a_period_is_refused(self, tmp_path: Path) -> None:
        payload = {k: v for k, v in submission().items() if k != "period_start"}
        artefact = write(tmp_path, payload)
        with pytest.raises(AggregateContractError, match="period_start"):
            list(JsonLinesAggregateAdapter().submissions(artefact))

    def test_an_observation_without_an_element_is_refused(self, tmp_path: Path) -> None:
        artefact = write(tmp_path, submission(observations=[{"value": 5}]))
        with pytest.raises(AggregateContractError, match="element code"):
            list(JsonLinesAggregateAdapter().submissions(artefact))

    def test_a_malformed_line_raises_rather_than_being_skipped(self, tmp_path: Path) -> None:
        artefact = tmp_path / "broken.jsonl"
        artefact.write_text(json.dumps(ENVELOPE) + "\n{not json}\n", encoding="utf-8")
        with pytest.raises(AggregateContractError, match="line 2"):
            list(JsonLinesAggregateAdapter().submissions(artefact))

    def test_the_reader_preserves_the_facilitys_own_period_label(self, tmp_path: Path) -> None:
        """So a transcription can be checked against the paper without
        recomputing it."""
        artefact = write(tmp_path, submission(period_label="March"))
        inbound = next(iter(JsonLinesAggregateAdapter().submissions(artefact)))
        assert inbound.period_label == "March"
        assert inbound.period_start == date(2026, 3, 1)

    def test_a_fractional_count_is_not_silently_truncated(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {
                        "element": "EP01b",
                        "age_band": "years_5_9",
                        "sex": "male",
                        "value": 1.5,
                    }
                ]
            ),
        )
        assert "value_not_numeric" in codes(validation)
        assert validation.submission is not None
        assert validation.submission.observations[0].value is None
        assert validation.submission.observations[0].raw_value == "1.5"

    def test_boolean_is_not_accepted_as_a_count(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {
                        "element": "EP01b",
                        "age_band": "years_5_9",
                        "sex": "male",
                        "value": True,
                    }
                ]
            ),
        )
        assert "value_not_numeric" in codes(validation)
        assert validation.submission is not None
        assert validation.submission.observations[0].value is None

    def test_a_negative_declared_submission_count_is_refused(self, tmp_path: Path) -> None:
        artefact = tmp_path / "negative-count.jsonl"
        artefact.write_text(
            json.dumps({**ENVELOPE, "submission_count": -1}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(AggregateContractError, match="cannot be negative"):
            JsonLinesAggregateAdapter().envelope(artefact)


class TestSexIsMappedFromWhatTheFormPrints:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [("male", Sex.MALE), ("M", Sex.MALE), ("female", Sex.FEMALE), ("F", Sex.FEMALE)],
    )
    def test_the_printed_codes_map(self, tmp_path: Path, written: str, expected: Sex) -> None:
        validation = validate(
            tmp_path,
            submission(
                observations=[
                    {"element": "EP01b", "age_band": "years_5_9", "sex": written, "value": 3}
                ]
            ),
        )
        assert validation.submission is not None
        assert validation.submission.observations[0].sex is expected

    def test_an_unrecognised_sex_is_refused(self, tmp_path: Path) -> None:
        validation = validate(
            tmp_path,
            submission(
                observations=[{"element": "EP01b", "age_band": "years_5_9", "sex": "X", "value": 3}]
            ),
        )
        assert "unknown_sex" in codes(validation)


class TestAnAggregateReturnIsCountsNeverPeople:
    """``ImportSourceRow.payload_redacted`` stores the whole inbound submission.

    That column's contract is that identity has already been removed, and the
    table is read by operators, analysts and anyone debugging an import - none
    of whom hold the re-identification permission. An aggregate form carries no
    identity by construction, but a mis-mapped export that attached a line
    listing would otherwise land patient data in ``mars_core`` with no error.
    """

    @pytest.mark.parametrize(
        "field",
        ["patient_name", "nin", "national_id", "surname", "phone", "identity", "line_list"],
    )
    def test_an_identity_shaped_field_is_refused(self, tmp_path: Path, field: str) -> None:
        artefact = write(tmp_path, submission(**{field: "should never be sent"}))
        with pytest.raises(AggregateContractError, match=field):
            list(JsonLinesAggregateAdapter().submissions(artefact))

    def test_it_is_refused_rather_than_stripped(self, tmp_path: Path) -> None:
        """A producer that believes MARS kept the value needs to be told it did
        not. Dropping it silently is the worst of both outcomes."""
        artefact = write(tmp_path, submission(patient_name="Nakato Sarah"))
        with pytest.raises(AggregateContractError) as raised:
            list(JsonLinesAggregateAdapter().submissions(artefact))
        assert "Refused rather than stripped" in str(raised.value) or "Refused" in str(raised.value)

    def test_no_forbidden_field_can_reach_the_stored_payload(self, tmp_path: Path) -> None:
        """The property the guard exists for, asserted on the object the
        pipeline actually persists."""
        artefact = write(tmp_path, submission())
        inbound = next(iter(JsonLinesAggregateAdapter().submissions(artefact)))
        assert not FORBIDDEN_SUBMISSION_FIELDS & set(inbound.raw)

    @pytest.mark.parametrize(
        "extra",
        [
            {"observations": [{"element": "EP01a", "value": 1, "patient_name": "Sarah"}]},
            {"metadata": {"line_list": [{"nin": "CM00000000AAAA"}]}},
            {"Patient-Name": "Sarah"},
            {"NATIONAL ID": "CM00000000AAAA"},
        ],
    )
    def test_identity_fields_are_refused_at_any_depth_and_casing(
        self, tmp_path: Path, extra: dict[str, object]
    ) -> None:
        """The persisted raw object includes nested objects and arrays too."""
        artefact = write(tmp_path, submission(**extra))
        with pytest.raises(AggregateContractError, match="identity-shaped"):
            list(JsonLinesAggregateAdapter().submissions(artefact))

    def test_the_refusal_message_cannot_carry_an_identity_value(self, tmp_path: Path) -> None:
        """A key can itself be a name.

        ``{"patients": {"Nakato Sarah": {...}}}`` is a plausible export shape,
        and the pipeline stores this message on ``import_batch.failure_reason``
        - a persisted column operators read. Echoing the key would move the
        name out of the payload and into the failure reason, which is the same
        leak wearing a different hat. The path is therefore built from MARS's
        own vocabulary; anything else is reported by shape alone.
        """
        artefact = write(tmp_path, submission(patients={"Nakato Sarah": {"nin": "CM90210077"}}))
        with pytest.raises(AggregateContractError) as raised:
            list(JsonLinesAggregateAdapter().submissions(artefact))

        message = str(raised.value)
        for secret in ("Nakato", "Sarah", "CM90210077"):
            assert secret not in message, f"the refusal message disclosed {secret!r}"
        # It still says where the field is, which is what an operator needs.
        assert "patients" in message
        assert "<unrecognised-key>" in message

    def test_an_unrecognised_container_is_reported_by_shape(self, tmp_path: Path) -> None:
        artefact = write(tmp_path, submission(metadata={"weird": {"NATIONAL ID": "CM1"}}))
        with pytest.raises(AggregateContractError) as raised:
            list(JsonLinesAggregateAdapter().submissions(artefact))

        message = str(raised.value)
        assert "CM1" not in message
        assert "national_id" in message

    def test_an_ordinary_submission_still_parses(self, tmp_path: Path) -> None:
        """The guard must not refuse the legitimate fields the form carries."""
        artefact = write(tmp_path, submission(remarks="RDT stock ran out on the 14th"))
        inbound = next(iter(JsonLinesAggregateAdapter().submissions(artefact)))
        assert inbound.remarks == "RDT stock ran out on the 14th"
