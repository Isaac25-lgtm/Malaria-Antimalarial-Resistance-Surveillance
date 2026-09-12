"""The shared positive-to-positive recurrence engine.

Every mandatory case from the recurrence handoff that the pure engine can decide
is pinned here. All evidence is invented; no test touches a database, a network
or live data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.analytics.recurrence_views import summarise
from mars.domain.longitudinal import (
    INTERPRETATION_LIMIT,
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    DefinitionError,
    Determination,
    DuplicateKind,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    IntervalBand,
    LabOutcome,
    Medication,
    MedicationEvidence,
    PatientFinding,
    QualityFlag,
    RecurrenceEvaluation,
    SameDayPolicy,
    SourceRecord,
)

BASE = date(2026, 8, 3)
NS = "test:namespace"
KAMPALA = ZoneInfo("Africa/Kampala")


def day(offset: int) -> date:
    return BASE + timedelta(days=offset)


def med(name: str, evidence: MedicationEvidence = MedicationEvidence.PRESCRIBED) -> Medication:
    return Medication(SourceRecord(NS, f"med-{name}", "medicine"), name, evidence)


def enc(
    key: str,
    offset: int | None,
    *,
    patient: str | None = "p1",
    facility: str = "f1",
    outcome: LabOutcome = LabOutcome.POSITIVE,
    method: str = "rdt",
    tests: tuple[ClinicalTest, ...] | None = None,
    parent: str | None = None,
    meds: Iterable[Medication] = (),
    precision: DatePrecision = DatePrecision.DAY,
    at: datetime | None = None,
    flags: frozenset[QualityFlag] = frozenset(),
    sex: str | None = None,
    diagnoses: tuple[str, ...] = (),
    namespace: str = NS,
) -> CanonicalEncounter:
    record = SourceRecord(NS, key, "lab")
    dated_on = day(offset) if offset is not None else None
    known = precision if offset is not None or at is not None else DatePrecision.UNKNOWN
    # A test carries its own evidence date. Tests supplied without one take the
    # encounter's, which is what an adapter that dates a test by its own
    # record would produce here.
    supplied = tests if tests is not None else (ClinicalTest(record, method, outcome),)
    dated_tests = tuple(
        test
        if test.precision is not DatePrecision.UNKNOWN
        else replace(test, observed_on=dated_on, observed_at=at, precision=known)
        for test in supplied
    )
    return CanonicalEncounter(
        encounter_key=key,
        namespace=namespace,
        patient_key=patient,
        facility_ref=facility,
        encounter_date=dated_on,
        date_precision=known,
        occurred_at=at,
        parent_ref=parent,
        tests=dated_tests,
        diagnoses=diagnoses,
        medications=tuple(meds),
        sources=(record,),
        sex=sex,
        quality_flags=flags,
    )


def defn(
    min_gap: int = 7,
    window: int = 28,
    n: int = 2,
    policy: SameDayPolicy = SameDayPolicy.EXCLUDE,
    **extra: object,
) -> FrozenRecurrenceDefinition:
    return FrozenRecurrenceDefinition(
        minimum_gap_days=min_gap,
        maximum_window_days=window,
        minimum_positive_encounters=n,
        same_day_policy=policy,
        **extra,  # type: ignore[arg-type]
    )


def period(start: int, end: int) -> FrozenAnalysisQuery:
    return FrozenAnalysisQuery(day(start), day(end))


def cov(start: int, end: int, *, complete: bool = True) -> EvidenceCoverage:
    return EvidenceCoverage(day(start), day(end), complete)


def run(
    encounters: Sequence[CanonicalEncounter],
    definition: FrozenRecurrenceDefinition | None = None,
    query: FrozenAnalysisQuery | None = None,
    coverage: EvidenceCoverage | None = None,
) -> RecurrenceEvaluation:
    return evaluate_recurrence(
        encounters,
        definition or defn(),
        query or period(0, 60),
        coverage or cov(-60, 70),
    )


def finding(evaluation: RecurrenceEvaluation, patient: str = "p1") -> PatientFinding:
    return next(p for p in evaluation.patients if p.patient.reference == patient)


# ---------------------------------------------------------------------------
# 1. The unit is a positive encounter, not an attendance
# ---------------------------------------------------------------------------
def test_two_attendances_with_one_positive_do_not_qualify() -> None:
    result = finding(run([enc("a", 0), enc("b", 10, outcome=LabOutcome.NEGATIVE)]))
    assert result.determination is Determination.DOES_NOT_QUALIFY
    assert result.observed_positive_count == 1
    assert [d.reason for d in result.decisions] == ["eligible_positive", "context_not_positive"]


def test_negative_context_cannot_bridge_an_anchored_window() -> None:
    result = finding(run([enc("a", 0), enc("n", 20, outcome=LabOutcome.NEGATIVE), enc("b", 40)]))
    assert result.determination is Determination.DOES_NOT_QUALIFY


# ---------------------------------------------------------------------------
# 3. Two tests of one verified encounter count once and both stay visible
# ---------------------------------------------------------------------------
def test_rdt_and_microscopy_on_one_parent_are_one_positive_encounter() -> None:
    tests = (
        ClinicalTest(SourceRecord(NS, "rdt-1", "lab"), "rdt", LabOutcome.POSITIVE),
        ClinicalTest(SourceRecord(NS, "mic-1", "lab"), "microscopy", LabOutcome.POSITIVE),
    )
    evaluation = run([enc("visit-1", 0, parent="visit-1", tests=tests)])
    result = finding(evaluation)
    assert result.eligible_positive_count == 1
    assert result.determination is Determination.DOES_NOT_QUALIFY
    group = next(
        g for g in evaluation.duplicate_groups if g.kind is DuplicateKind.SHARED_PARENT_TESTS
    )
    assert set(group.members) == {"rdt-1", "mic-1"}


# ---------------------------------------------------------------------------
# 4. Same-day policies
# ---------------------------------------------------------------------------
SAME_DAY = [enc("a1", 0), enc("a2", 0), enc("b", 10)]


def _reasons(result: PatientFinding) -> dict[str, str]:
    return {d.encounter_key: d.reason for d in result.decisions}


def test_exclude_counts_one_same_day_positive_and_keeps_every_record() -> None:
    evaluation = run(SAME_DAY, defn(policy=SameDayPolicy.EXCLUDE))
    result = finding(evaluation)
    assert result.eligible_positive_count == 2
    assert _reasons(result) == {
        "a1": "eligible_positive",
        "a2": "same_day_exclude",
        "b": "eligible_positive",
    }
    assert result.chain == ("a1", "b")
    same_day = [g for g in evaluation.duplicate_groups if g.local_day == day(0)]
    assert len(same_day) == 1 and same_day[0].unresolved is False


def test_review_separately_counts_the_same_but_queues_the_group() -> None:
    evaluation = run(SAME_DAY, defn(policy=SameDayPolicy.REVIEW_SEPARATELY))
    result = finding(evaluation)
    assert result.eligible_positive_count == 2
    assert _reasons(result)["a2"] == "same_day_review_separately"
    assert QualityFlag.POSSIBLE_DUPLICATE in result.quality_flags
    assert next(g for g in evaluation.duplicate_groups if g.local_day == day(0)).unresolved


def test_include_counts_distinct_same_day_encounters_but_obeys_the_minimum_gap() -> None:
    result = finding(run(SAME_DAY, defn(policy=SameDayPolicy.INCLUDE, min_gap=7)))
    assert result.eligible_positive_count == 3
    assert result.chain_length == 2
    assert {d.encounter_key for d in result.decisions} == {"a1", "a2", "b"}


def test_include_with_a_zero_gap_lets_same_day_encounters_chain() -> None:
    result = finding(run(SAME_DAY, defn(policy=SameDayPolicy.INCLUDE, min_gap=0)))
    assert result.chain == ("a1", "a2", "b")


def test_near_duplicate_same_day_records_are_classified_probable() -> None:
    evaluation = run(SAME_DAY)
    group = next(g for g in evaluation.duplicate_groups if g.local_day == day(0))
    assert group.kind is DuplicateKind.NEAR_DUPLICATE
    assert group.representative == "a1"


# ---------------------------------------------------------------------------
# 5. Timezones and date precision
# ---------------------------------------------------------------------------
def test_a_timestamp_takes_its_day_in_the_reporting_timezone() -> None:
    late = enc(
        "t", None, precision=DatePrecision.TIMESTAMP, at=datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
    )
    assert late.local_day(KAMPALA) == date(2026, 8, 4)
    assert late.local_day(ZoneInfo("UTC")) == date(2026, 8, 3)


def test_a_date_only_value_is_kept_exactly_as_recorded() -> None:
    assert enc("d", 1).local_day(KAMPALA) == day(1)
    assert enc("d", 1).local_day(ZoneInfo("Pacific/Kiritimati")) == day(1)


def test_a_utc_evening_timestamp_shares_a_local_day_with_a_date_only_record() -> None:
    late = enc(
        "t", None, precision=DatePrecision.TIMESTAMP, at=datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
    )
    evaluation = run([late, enc("d", 1)])
    assert any(g.local_day == day(1) for g in evaluation.duplicate_groups)


def test_a_naive_timestamp_is_a_date_quality_issue_not_a_guess() -> None:
    naive = enc("n", None, precision=DatePrecision.TIMESTAMP, at=datetime(2026, 8, 4, 9, 0))
    result = finding(run([enc("a", 0), naive]))
    assert _reasons(result)["n"] == "excluded_unusable_date"
    assert QualityFlag.DATE_QUALITY_ISSUE in result.quality_flags


# ---------------------------------------------------------------------------
# 6. Intervals, bounds, N and later anchors
# ---------------------------------------------------------------------------
def test_days_0_9_23_expose_9_14_and_23_with_their_endpoints() -> None:
    result = finding(
        run([enc("a", 0), enc("n", 5, outcome=LabOutcome.NEGATIVE), enc("b", 9), enc("c", 23)])
    )
    assert [(i.from_encounter, i.to_encounter, i.days) for i in result.adjacent_intervals] == [
        ("a", "b", 9),
        ("b", "c", 14),
    ]
    assert [(i.to_encounter, i.days) for i in result.anchor_intervals] == [("b", 9), ("c", 23)]
    assert result.chain == ("a", "b", "c")
    assert result.chain_interval is not None and result.chain_interval.days == 23


@pytest.mark.parametrize(("second", "qualifies"), [(3, False), (12, True), (35, False)])
def test_the_briefs_worked_examples(second: int, qualifies: bool) -> None:
    result = finding(run([enc("a", 0), enc("b", second)]))
    assert (result.determination is Determination.QUALIFIES) is qualifies


@pytest.mark.parametrize(("second", "qualifies"), [(6, False), (7, True), (28, True), (29, False)])
def test_both_bounds_are_inclusive(second: int, qualifies: bool) -> None:
    result = finding(run([enc("a", 0), enc("b", second)]))
    assert (result.determination is Determination.QUALIFIES) is qualifies


@pytest.mark.parametrize(
    ("n", "offsets", "qualifies"),
    [
        (2, [0, 10], True),
        (3, [0, 10, 20], True),
        (3, [0, 10], False),
        (4, [0, 8, 16, 24], True),
        (4, [0, 8, 16, 30], False),
    ],
)
def test_positive_count_thresholds(n: int, offsets: list[int], qualifies: bool) -> None:
    encounters = [enc(f"e{i}", offset) for i, offset in enumerate(offsets)]
    result = finding(run(encounters, defn(n=n)))
    assert (result.determination is Determination.QUALIFIES) is qualifies


def test_a_patient_can_qualify_on_a_later_anchor() -> None:
    result = finding(run([enc("a", 0), enc("b", 40), enc("c", 50)]))
    assert result.determination is Determination.QUALIFIES
    assert result.chain == ("b", "c")


def test_the_brief_later_anchor_example_day_35_against_another_positive() -> None:
    result = finding(run([enc("a", 0), enc("b", 35), enc("c", 45)]))
    assert result.chain == ("b", "c")


# ---------------------------------------------------------------------------
# 7. Anchored, not rolling
# ---------------------------------------------------------------------------
def test_short_adjacent_gaps_do_not_make_an_anchored_three_chain() -> None:
    result = finding(run([enc("a", 0), enc("b", 20), enc("c", 40)], defn(window=28, n=3)))
    assert result.determination is Determination.DOES_NOT_QUALIFY


# ---------------------------------------------------------------------------
# 8. Lookback
# ---------------------------------------------------------------------------
def test_a_chain_starting_before_the_period_is_found_through_lookback() -> None:
    result = finding(run([enc("a", 20), enc("b", 36)], query=period(30, 59), coverage=cov(0, 60)))
    assert result.determination is Determination.QUALIFIES
    assert result.chain == ("a", "b")
    assert result.final_date == day(36)


def test_unobserved_lookback_is_indeterminate_never_zero() -> None:
    evaluation = run([enc("b", 36)], query=period(30, 59), coverage=cov(30, 60))
    assert finding(evaluation).determination is Determination.INDETERMINATE
    assert evaluation.coverage_status == "partial"
    summary = summarise(evaluation)
    assert summary.qualifying_patients == 0
    assert summary.indeterminate_patients == 1


def test_a_positive_whose_own_window_is_observed_can_still_be_a_reliable_no() -> None:
    evaluation = run([enc("b", 59)], query=period(30, 59), coverage=cov(30, 60))
    assert finding(evaluation).determination is Determination.DOES_NOT_QUALIFY


# ---------------------------------------------------------------------------
# 9. Identity
# ---------------------------------------------------------------------------
def test_missing_stable_identity_never_links_encounters() -> None:
    evaluation = run([enc("a", 0, patient=None), enc("b", 10, patient=None)])
    assert evaluation.patients == ()
    assert evaluation.unlinked_positive_encounters == ("a", "b")
    assert summarise(evaluation).unlinked_positive_encounters == 2


def test_different_patient_keys_are_never_merged() -> None:
    evaluation = run([enc("a", 0, patient="p1"), enc("b", 10, patient="p2")])
    assert all(p.determination is not Determination.QUALIFIES for p in evaluation.patients)


# ---------------------------------------------------------------------------
# 10. Unmapped, undated and incomplete evidence
# ---------------------------------------------------------------------------
def test_an_unmapped_result_is_not_a_negative_and_makes_absence_indeterminate() -> None:
    result = finding(run([enc("a", 0), enc("u", 10, outcome=LabOutcome.UNMAPPED)]))
    assert result.determination is Determination.INDETERMINATE
    assert QualityFlag.INCOMPLETE_TEST_EVIDENCE in result.quality_flags
    assert result.observed_positive_count == 1


def test_an_unmapped_result_outside_the_window_is_flagged_but_not_decisive() -> None:
    """Only a result that could hide a chain for this period makes absence unknown."""
    result = finding(
        run([enc("u", -200, outcome=LabOutcome.UNMAPPED), enc("a", 0)], coverage=cov(-250, 70))
    )
    assert result.determination is Determination.DOES_NOT_QUALIFY
    assert QualityFlag.INCOMPLETE_TEST_EVIDENCE in result.quality_flags


def test_an_undated_positive_stays_visible_but_cannot_join_a_sequence() -> None:
    result = finding(run([enc("a", 0), enc("x", None, precision=DatePrecision.UNKNOWN)]))
    assert _reasons(result)["x"] == "excluded_unusable_date"
    assert result.eligible_positive_count == 1
    assert QualityFlag.DATE_QUALITY_ISSUE in result.quality_flags


def test_incomplete_retrieval_cannot_report_a_reliable_absence() -> None:
    evaluation = run([enc("a", 0)], coverage=cov(-60, 70, complete=False))
    assert finding(evaluation).determination is Determination.INDETERMINATE
    assert evaluation.coverage_status == "partial"


def test_a_positive_by_a_method_outside_the_definition_does_not_count() -> None:
    result = finding(
        run(
            [enc("a", 0, method="microscopy"), enc("b", 10, method="rdt")],
            defn(permitted_positive_methods=frozenset({"rdt"})),
        )
    )
    assert _reasons(result)["a"] == "positive_method_not_permitted"
    assert result.determination is Determination.DOES_NOT_QUALIFY


# ---------------------------------------------------------------------------
# 11. Treatment
# ---------------------------------------------------------------------------
def test_post_treatment_question_needs_medicine_on_every_non_final_positive() -> None:
    untreated = finding(run([enc("a", 0), enc("b", 10)], defn(require_linked_treatment=True)))
    assert untreated.determination is Determination.DOES_NOT_QUALIFY
    treated = finding(
        run(
            [enc("a", 0, meds=[med("Artemether/Lumefantrine")]), enc("b", 10)],
            defn(require_linked_treatment=True),
        )
    )
    assert treated.determination is Determination.QUALIFIES
    step = treated.transitions[0]
    assert step.prior_medications == ("artemether/lumefantrine",)
    assert step.prior_treatment_evidence == "prescribed"


def test_prescribed_is_never_reported_as_dispensed() -> None:
    result = finding(
        run([enc("a", 0, meds=[med("AL", MedicationEvidence.DISPENSED)]), enc("b", 10)])
    )
    assert result.transitions[0].prior_treatment_evidence == "dispensed"
    result = finding(run([enc("a", 0, meds=[med("AL")]), enc("b", 10)]))
    assert result.transitions[0].prior_treatment_evidence == "prescribed"


def test_missing_treatment_is_flagged_but_does_not_erase_the_positive() -> None:
    result = finding(run([enc("a", 0), enc("b", 10)]))
    assert result.determination is Determination.QUALIFIES
    assert QualityFlag.INCOMPLETE_TREATMENT_EVIDENCE in result.quality_flags
    assert QualityFlag.VALID not in result.quality_flags


def test_valid_never_coexists_with_another_flag() -> None:
    clean = finding(run([enc("a", 0, meds=[med("AL")]), enc("b", 10, meds=[med("AL")])]))
    assert clean.quality_flags == frozenset({QualityFlag.VALID})


# ---------------------------------------------------------------------------
# 13. Scale, determinism and wording
# ---------------------------------------------------------------------------
def test_totals_stay_complete_beyond_five_thousand_encounters() -> None:
    encounters: list[CanonicalEncounter] = []
    for i in range(3000):
        second = 10 if i % 2 == 0 else 3
        encounters += [enc(f"p{i}-a", 0, patient=f"p{i}"), enc(f"p{i}-b", second, patient=f"p{i}")]
    summary = summarise(run(encounters))
    assert summary.qualifying_patients == 1500
    assert summary.denominator_patients == 3000


def test_the_result_does_not_depend_on_input_order() -> None:
    encounters = [enc("a", 0), enc("b", 9), enc("c", 23), enc("d", 40, patient="p2")]
    assert run(encounters).patients == run(list(reversed(encounters))).patients


def test_no_explanation_claims_resistance_or_treatment_failure() -> None:
    evaluation = run([*SAME_DAY, enc("q", 0, patient="p2"), enc("r", 30, patient="p2")])
    text = " ".join(line for p in evaluation.patients for line in p.explanation).casefold()
    assert text
    assert "resistan" not in text
    assert "treatment failure" not in text
    assert "not evidence of antimalarial resistance" in INTERPRETATION_LIMIT


def test_the_legacy_interval_field_has_one_documented_meaning() -> None:
    qualifying = finding(run([enc("a", 0), enc("b", 9), enc("c", 23)]))
    assert qualifying.legacy_interval_days == 23
    short = finding(run([enc("a", 0), enc("b", 3)]))
    assert short.legacy_interval_days == 3
    single = finding(run([enc("a", 0)]))
    assert single.legacy_interval_days is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_gap": -1},
        {"min_gap": 10, "window": 9},
        {"n": 1},
        {"interval_bands": (IntervalBand("a", 0, 10), IntervalBand("b", 5, 20))},
        {"interval_bands": (IntervalBand("a", 0, None), IntervalBand("b", 5, 20))},
        {"interval_bands": (IntervalBand("a", 5, 2),)},
        {"quality_exclusions": frozenset({QualityFlag.VALID})},
    ],
)
def test_invalid_definitions_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(DefinitionError):
        defn(**kwargs)  # type: ignore[arg-type]


def test_windows_beyond_ten_days_are_supported() -> None:
    result = finding(run([enc("a", 0), enc("b", 90)], defn(min_gap=30, window=120), period(0, 100)))
    assert result.determination is Determination.QUALIFIES


def test_invalid_queries_are_refused() -> None:
    with pytest.raises(DefinitionError):
        FrozenAnalysisQuery(day(10), day(0))
    with pytest.raises(DefinitionError):
        FrozenAnalysisQuery(day(0), day(10), timezone="Not/AZone")


# ---------------------------------------------------------------------------
# Regressions for the independent execution audit, defects A, B, D and G
# ---------------------------------------------------------------------------
def dated(record_id: str, method: str, offset: int | None) -> ClinicalTest:
    return ClinicalTest(
        SourceRecord(NS, record_id, "lab"),
        method,
        LabOutcome.POSITIVE,
        observed_on=day(offset) if offset is not None else None,
        precision=DatePrecision.DAY,
    )


def test_a_positive_is_dated_by_its_test_not_by_its_visit() -> None:
    """Defect A at the engine boundary: a day-5 visit holding a day-9 positive."""
    result = finding(run([enc("a", 0), enc("b", 5, tests=(dated("lab-b", "rdt", 9),))]))
    assert result.determination is Determination.QUALIFIES
    assert result.chain_interval is not None and result.chain_interval.days == 9
    assert any("tests' own dates" in line for line in result.explanation)


def test_disagreeing_positive_test_dates_use_the_earliest_and_are_flagged() -> None:
    tests = (dated("rdt", "rdt", 10), dated("mic", "microscopy", 12))
    result = finding(run([enc("a", 0), enc("b", 10, tests=tests, parent="v2")]))
    assert result.chain_interval is not None and result.chain_interval.days == 10
    assert QualityFlag.DATE_QUALITY_ISSUE in result.quality_flags
    assert any("disagreeing" in line for line in result.explanation)


def test_a_positive_test_without_its_own_date_cannot_borrow_the_visit_date() -> None:
    result = finding(run([enc("a", 0), enc("b", 10, tests=(dated("lab-x", "rdt", None),))]))
    assert result.determination is not Determination.QUALIFIES
    decision = next(d for d in result.decisions if d.encounter_key == "b")
    assert decision.reason == "excluded_unusable_date"


def test_the_same_reference_in_two_namespaces_is_two_people() -> None:
    """Defect B: grouping by the bare reference merged two sources' patients."""
    evaluation = run([enc("a", 0, namespace="source-one"), enc("b", 9, namespace="source-two")])
    assert len(evaluation.patients) == 2
    assert {p.patient.namespace for p in evaluation.patients} == {"source-one", "source-two"}
    assert all(p.determination is not Determination.QUALIFIES for p in evaluation.patients)


def test_one_namespace_links_the_same_reference_across_facilities() -> None:
    result = finding(run([enc("a", 0, facility="f1"), enc("b", 9, facility="f2")]))
    assert result.determination is Determination.QUALIFIES
    assert result.cross_facility


def test_same_day_groups_never_span_namespaces() -> None:
    evaluation = run([enc("a", 0, namespace="one"), enc("b", 0, namespace="two")])
    same_day = {DuplicateKind.SAME_DAY_DISTINCT, DuplicateKind.NEAR_DUPLICATE}
    assert not [group for group in evaluation.duplicate_groups if group.kind in same_day]


def test_display_aliases_are_stable_and_differ_across_namespaces() -> None:
    from mars.domain.longitudinal import PatientKey, patient_display_alias

    alias = patient_display_alias(b"display-key", PatientKey("one", "tei-SECRET"))
    assert alias == patient_display_alias(b"display-key", PatientKey("one", "tei-SECRET"))
    assert alias != patient_display_alias(b"display-key", PatientKey("two", "tei-SECRET"))
    assert alias.startswith("MARS-PT2-") and "SECRET" not in alias


@pytest.mark.parametrize(
    ("policy", "min_gap", "exclusions", "expected", "counted"),
    [
        (SameDayPolicy.EXCLUDE, 7, frozenset(), Determination.QUALIFIES, 2),
        (SameDayPolicy.REVIEW_SEPARATELY, 7, frozenset(), Determination.QUALIFIES, 2),
        (SameDayPolicy.INCLUDE, 7, frozenset(), Determination.QUALIFIES, 3),
        (SameDayPolicy.INCLUDE, 0, frozenset(), Determination.QUALIFIES, 3),
        (
            SameDayPolicy.EXCLUDE,
            7,
            frozenset({QualityFlag.POSSIBLE_DUPLICATE}),
            Determination.DOES_NOT_QUALIFY,
            1,
        ),
        (
            SameDayPolicy.INCLUDE,
            0,
            frozenset({QualityFlag.POSSIBLE_DUPLICATE}),
            Determination.DOES_NOT_QUALIFY,
            1,
        ),
    ],
)
def test_day_0_0_9_under_every_same_day_policy_and_the_duplicate_exclusion(
    policy: SameDayPolicy,
    min_gap: int,
    exclusions: frozenset[QualityFlag],
    expected: Determination,
    counted: int,
) -> None:
    """Defect D: a POSSIBLE_DUPLICATE exclusion must change eligibility."""
    definition = defn(min_gap=min_gap, policy=policy, quality_exclusions=exclusions)
    result = finding(run([enc("a", 0), enc("b", 0), enc("c", 9)], definition))
    assert result.determination is expected
    assert sum(1 for decision in result.decisions if decision.counted) == counted
    assert len(result.decisions) == 3  # every record stays inspectable
    if exclusions:
        reasons = {d.encounter_key: d.reason for d in result.decisions}
        assert reasons["a"] == reasons["b"] == "excluded_possible_duplicate"
        assert any("POSSIBLE_DUPLICATE exclusion" in line for line in result.explanation)


def test_evidence_ending_before_the_period_ends_is_indeterminate() -> None:
    """Defect G: forward coverage was not checked for a patient's absence."""
    evaluation = run([enc("a", 0)], query=period(0, 30), coverage=cov(-60, 0))
    assert finding(evaluation).determination is Determination.INDETERMINATE
    assert evaluation.coverage_status == "partial"


def test_a_requested_facility_without_coverage_makes_absence_indeterminate() -> None:
    coverage = EvidenceCoverage(
        day(-60),
        day(70),
        True,
        facility_refs=frozenset({"f1"}),
        requested_facility_refs=frozenset({"f1", "f2"}),
    )
    evaluation = run([enc("a", 0)], coverage=coverage)
    assert finding(evaluation).determination is Determination.INDETERMINATE
    assert evaluation.coverage_status == "partial"


def test_genuinely_complete_evidence_supports_a_reliable_negative() -> None:
    coverage = EvidenceCoverage(
        day(-60),
        day(70),
        True,
        facility_refs=frozenset({"f1"}),
        requested_facility_refs=frozenset({"f1"}),
    )
    evaluation = run([enc("a", 0), enc("b", 40)], coverage=coverage)
    assert finding(evaluation).determination is Determination.DOES_NOT_QUALIFY
    assert evaluation.coverage_status == "complete"
