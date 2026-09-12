"""Projections of one recurrence evaluation: units, denominators, attribution."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import pytest

from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.analytics.recurrence_views import (
    OUTSIDE_BANDS_LABEL,
    UNMAPPED_GEOGRAPHY,
    CohortFilter,
    ComparisonRefusedError,
    compare,
    facility_breakdown,
    geography_breakdown,
    interval_distribution,
    positive_frequency,
    summarise,
    treatment_matrix,
    weekly_trend,
)
from mars.domain.longitudinal import (
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    IntervalBand,
    LabOutcome,
    Medication,
    MedicationEvidence,
    PatientKey,
    RecurrenceEvaluation,
    SourceRecord,
)

BASE = date(2026, 8, 3)  # a Monday
NS = "test:namespace"


def day(offset: int) -> date:
    return BASE + timedelta(days=offset)


def enc(
    key: str,
    offset: int,
    patient: str,
    *,
    facility: str = "f1",
    meds: Sequence[str] = (),
    sex: str | None = None,
) -> CanonicalEncounter:
    record = SourceRecord(NS, key, "lab")
    return CanonicalEncounter(
        encounter_key=key,
        namespace=NS,
        patient_key=patient,
        facility_ref=facility,
        encounter_date=day(offset),
        date_precision=DatePrecision.DAY,
        tests=(
            ClinicalTest(
                record,
                "rdt",
                LabOutcome.POSITIVE,
                observed_on=day(offset),
                precision=DatePrecision.DAY,
            ),
        ),
        medications=tuple(
            Medication(SourceRecord(NS, f"{key}-{m}", "medicine"), m, MedicationEvidence.PRESCRIBED)
            for m in meds
        ),
        sources=(record,),
        sex=sex,
    )


def run(
    encounters: Sequence[CanonicalEncounter],
    *,
    bands: tuple[IntervalBand, ...] | None = None,
    coverage: EvidenceCoverage | None = None,
    min_gap: int = 7,
    dataset_id: str | None = None,
) -> RecurrenceEvaluation:
    definition = FrozenRecurrenceDefinition(
        minimum_gap_days=min_gap,
        maximum_window_days=28,
        **({"interval_bands": bands} if bands else {}),  # type: ignore[arg-type]
    )
    return evaluate_recurrence(
        encounters,
        definition,
        FrozenAnalysisQuery(day(0), day(27)),
        coverage or EvidenceCoverage(day(-40), day(40), True),
        dataset_id=dataset_id,
    )


def references(keys: Sequence[PatientKey]) -> tuple[str, ...]:
    return tuple(key.reference for key in keys)


def test_the_summary_shows_numerator_and_denominator() -> None:
    summary = summarise(run([enc("a", 0, "p1"), enc("b", 10, "p1"), enc("c", 5, "p2")]))
    assert (summary.qualifying_patients, summary.denominator_patients) == (1, 2)
    assert summary.proportion == 0.5
    assert "not evidence of antimalarial resistance" in summary.interpretation


def test_an_empty_denominator_gives_no_proportion_rather_than_zero() -> None:
    assert summarise(run([])).proportion is None


def test_facility_attribution_uses_the_final_positive_and_is_not_additive() -> None:
    evaluation = run([enc("a", 0, "p1", facility="f1"), enc("b", 10, "p1", facility="f2")])
    rows = {r.facility_ref: r for r in facility_breakdown(evaluation)}
    assert rows["f2"].qualifying_patients == 1 and rows["f1"].qualifying_patients == 0
    assert rows["f1"].denominator_patients == rows["f2"].denominator_patients == 1
    assert summarise(evaluation).denominator_patients == 1  # unique patients, not a sum


def test_observed_intervals_keep_gaps_below_the_threshold_visible() -> None:
    evaluation = run([enc("a", 0, "p1"), enc("b", 3, "p1")])
    observed = {b.label: b.count for b in interval_distribution(evaluation, "observed_adjacent")}
    chains = {b.label: b.count for b in interval_distribution(evaluation, "qualifying_chain")}
    assert observed["3-6 days"] == 1
    assert sum(chains.values()) == 0


def test_values_in_a_gap_between_bands_go_to_an_explicit_bucket() -> None:
    bands = (IntervalBand("0-5", 0, 5), IntervalBand("10-20", 10, 20))
    evaluation = run([enc("a", 0, "p1"), enc("b", 7, "p1")], bands=bands)
    rows = {b.label: b.count for b in interval_distribution(evaluation, "qualifying_chain")}
    assert rows[OUTSIDE_BANDS_LABEL] == 1


def test_frequency_counts_qualifying_patients_by_chain_length() -> None:
    evaluation = run(
        [enc("a", 0, "p1"), enc("b", 10, "p1")]
        + [enc(f"q{i}", i * 8, "p2") for i in range(3)]
        + [enc(f"r{i}", i * 7, "p3") for i in range(4)]
    )
    assert {r.label: r.patients for r in positive_frequency(evaluation)} == {
        "2": 1,
        "3": 1,
        "4+": 1,
    }


def test_the_weekly_trend_uses_the_iso_week_of_the_final_positive() -> None:
    evaluation = run([enc("a", 0, "p1"), enc("b", 10, "p1")])
    rows = weekly_trend(evaluation)
    qualifying = [r for r in rows if r.qualifying_patients]
    assert qualifying[0].week_start == day(7)  # day 10 is in the week starting day 7


def test_the_treatment_matrix_counts_transitions_not_patients() -> None:
    evaluation = run(
        [enc("a", 0, "p1", meds=["AL"]), enc("b", 8, "p1", meds=["AL"]), enc("c", 16, "p1")]
    )
    cells = treatment_matrix(evaluation)
    assert sum(cell.transitions for cell in cells) == 2
    assert all(cell.prior_treatment == "al" for cell in cells)


def test_an_unlinked_treatment_stays_an_explicit_row() -> None:
    cells = treatment_matrix(run([enc("a", 0, "p1"), enc("b", 10, "p1")]))
    assert [c.prior_treatment for c in cells] == ["no linked treatment"]


def test_geography_keeps_unmapped_facilities_visible_and_counts_people_once() -> None:
    evaluation = run(
        [
            enc("a", 0, "p1", facility="f1"),
            enc("b", 10, "p1", facility="f2"),
            enc("c", 5, "p2", facility="f9"),
        ]
    )
    rows = {
        r.geography_ref: r for r in geography_breakdown(evaluation, {"f1": "sc-1", "f2": "sc-1"})
    }
    assert rows["sc-1"].denominator_patients == 1
    assert rows["sc-1"].qualifying_patients == 1
    assert rows[UNMAPPED_GEOGRAPHY].denominator_patients == 1


def test_unknown_sex_is_an_explicit_category() -> None:
    evaluation = run([enc("a", 0, "p1"), enc("b", 10, "p1"), enc("c", 0, "p2", sex="female")])
    unknown = summarise(evaluation, CohortFilter(sexes=frozenset({"unknown"})))
    assert unknown.denominator_patients == 1


def test_comparison_overlap_is_deterministic() -> None:
    encounters = [enc("a", 0, "p1"), enc("b", 3, "p1"), enc("c", 0, "p2"), enc("d", 10, "p2")]
    loose = run(encounters, min_gap=1)
    strict = run(encounters, min_gap=7)
    result = compare(loose, strict)
    assert references(result.both) == ("p2",)
    assert references(result.a_only) == ("p1",)
    assert result.b_only == ()
    assert result.union_count == 2
    assert result.dataset_hash == loose.dataset_hash == strict.dataset_hash


def test_comparison_across_different_evidence_is_refused() -> None:
    encounters = [enc("a", 0, "p1"), enc("b", 10, "p1")]
    with pytest.raises(ComparisonRefusedError):
        compare(run(encounters), run(encounters, coverage=EvidenceCoverage(day(0), day(40), True)))


def test_different_patient_evidence_with_identical_coverage_is_refused() -> None:
    """Audit defect F: equal coverage labels over different patients were accepted."""
    first = run([enc("a", 0, "p1"), enc("b", 10, "p1")])
    second = run([enc("x", 0, "p7"), enc("y", 12, "p8")])
    assert first.coverage == second.coverage and first.query == second.query
    assert first.dataset_hash != second.dataset_hash
    with pytest.raises(ComparisonRefusedError, match="different evidence"):
        compare(first, second)


def test_one_retained_dataset_under_two_definitions_is_comparable() -> None:
    encounters = [enc("a", 0, "p1"), enc("b", 4, "p1"), enc("c", 0, "p2"), enc("d", 9, "p2")]
    loose = run(encounters, min_gap=3, dataset_id="dataset-1")
    strict = run(encounters, min_gap=7, dataset_id="dataset-1")
    result = compare(loose, strict)
    assert result.dataset_id == "dataset-1"
    assert references(result.a_only) == ("p1",)
    assert references(result.both) == ("p2",)


def test_runs_over_two_retained_datasets_are_refused_even_with_equal_content() -> None:
    encounters = [enc("a", 0, "p1"), enc("b", 10, "p1")]
    with pytest.raises(ComparisonRefusedError, match="different retained datasets"):
        compare(run(encounters, dataset_id="one"), run(encounters, dataset_id="two"))


def test_the_dataset_fingerprint_ignores_input_order_but_not_content() -> None:
    encounters = [enc("a", 0, "p1"), enc("b", 10, "p1")]
    assert run(encounters).dataset_hash == run(list(reversed(encounters))).dataset_hash
    assert run(encounters).dataset_hash != run([enc("a", 0, "p1"), enc("b", 11, "p1")]).dataset_hash
