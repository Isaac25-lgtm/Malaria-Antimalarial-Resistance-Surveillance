"""Run outputs: one evaluation, one denominator contract, no identifiers in clear.

Every value is invented. These builders are what a run stores and what the
recurrence endpoints serve, so the patient list, the panels, the duplicate
review and a comparison are checked against each other here.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.domain.longitudinal import (
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    DefinitionError,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    LabOutcome,
    Medication,
    MedicationEvidence,
    PatientKey,
    RecurrenceEvaluation,
    SourceRecord,
    patient_display_alias,
)
from mars.services.recurrence_outputs import (
    RunFilters,
    build_duplicate_rows,
    build_findings,
    build_projections,
    compare_projections,
    compare_run_findings,
    manifest_checksum,
    patient_facts,
    restrict,
)

NS = "test:namespace"
BASE = date(2026, 8, 3)


def day(offset: int) -> date:
    return BASE + timedelta(days=offset)


def enc(
    key: str,
    offset: int,
    patient: str,
    *,
    facility: str = "F1",
    method: str = "rdt",
    meds: tuple[str, ...] = (),
    sex: str | None = None,
) -> CanonicalEncounter:
    record = SourceRecord(NS, f"evt-SECRET-{key}", "laboratory")
    return CanonicalEncounter(
        encounter_key=f"event:evt-SECRET-{key}",
        namespace=NS,
        patient_key=f"person-SECRET-{patient}",
        facility_ref=facility,
        encounter_date=day(offset),
        date_precision=DatePrecision.DAY,
        tests=(
            ClinicalTest(
                record,
                method,
                LabOutcome.POSITIVE,
                observed_on=day(offset),
                precision=DatePrecision.DAY,
            ),
        ),
        medications=tuple(
            Medication(
                SourceRecord(NS, f"med-{key}-{m}", "medicine"), m, MedicationEvidence.RECORDED
            )
            for m in meds
        ),
        sources=(record,),
        sex=sex,
    )


ENCOUNTERS = [
    enc("a1", 0, "A", meds=("AL",)),
    enc("a2", 10, "A", facility="F2"),
    enc("b1", 2, "B", method="microscopy", sex="female"),
    enc("c1", 0, "C"),
    enc("c2", 0, "C"),
    enc("c3", 12, "C"),
]


def evaluate(min_gap: int = 7, dataset_id: str = "dataset-1") -> RecurrenceEvaluation:
    return evaluate_recurrence(
        ENCOUNTERS,
        FrozenRecurrenceDefinition(minimum_gap_days=min_gap, maximum_window_days=28),
        FrozenAnalysisQuery(day(0), day(27)),
        EvidenceCoverage(day(-60), day(40), True),
        dataset_id=dataset_id,
    )


def alias(key: PatientKey) -> str:
    return patient_display_alias(b"display-key", key)


def by_patient() -> dict[PatientKey, list[CanonicalEncounter]]:
    grouped: dict[PatientKey, list[CanonicalEncounter]] = {}
    for encounter in ENCOUNTERS:
        assert encounter.patient is not None
        grouped.setdefault(encounter.patient, []).append(encounter)
    return grouped


def findings(evaluation: RecurrenceEvaluation, filters: RunFilters = RunFilters()) -> list:  # type: ignore[type-arg]
    grouped = by_patient()
    facts = {p.patient: patient_facts(p, grouped[p.patient]) for p in evaluation.patients}
    restricted = restrict(evaluation, filters, facts)
    return build_findings(
        restricted,
        filters,
        encounters_by_patient=grouped,
        unlinked_by_patient={},
        facts=facts,
        alias=alias,
    )


def test_listed_patients_and_the_summary_share_one_denominator() -> None:
    evaluation = evaluate()
    projections = build_projections(
        evaluation, RunFilters(), facility_geography={"F1": "sc-1"}, facility_names={"F1": "One"}
    )
    rows = findings(evaluation)
    summary = projections["summary"]
    assert summary["denominator_patients"] == len(rows) == 3
    assert summary["qualifying_patients"] == sum(
        1 for c, _ in rows if c["determination"] == "qualifies"
    )
    assert "not a resistance rate" in projections["units"]["summary"]


def test_nothing_in_clear_carries_a_patient_key_or_source_identifier() -> None:
    evaluation = evaluate()
    projections = build_projections(
        evaluation, RunFilters(), facility_geography={}, facility_names={}
    )
    columns = [c for c, _ in findings(evaluation)]
    assert "SECRET" not in json.dumps(projections, default=str)
    assert "SECRET" not in json.dumps(columns, default=str)
    # The sealed detail does hold them - that is what the encryption is for.
    assert any("SECRET" in json.dumps(detail, default=str) for _, detail in findings(evaluation))


def test_the_detail_references_encounters_opaquely_and_keeps_every_record() -> None:
    evaluation = evaluate()
    # Patient C: two same-day positives and a later one.
    detail = next(d for _, d in findings(evaluation) if len(d["encounters"]) == 3)
    assert sorted(detail["decisions"]) == ["E1", "E2", "E3"]
    reasons = {decision["reason"] for decision in detail["decisions"].values()}
    assert "same_day_exclude" in reasons  # the same-day record stays visible


def test_filters_narrow_the_cohort_after_evaluation_without_rejudging_chains() -> None:
    evaluation = evaluate()
    all_rows = {c["patient_alias"]: c for c, _ in findings(evaluation)}
    rdt = {
        c["patient_alias"]: c
        for c, _ in findings(evaluation, RunFilters(test_methods=frozenset({"microscopy"})))
    }
    assert len(rdt) == 1
    (only,) = rdt.values()
    assert only["determination"] == all_rows[only["patient_alias"]]["determination"]
    treated = findings(evaluation, RunFilters(treatments=frozenset({"al"})))
    assert [c["treatment_names"] for c, _ in treated] == [["al"]]
    female = findings(evaluation, RunFilters(sexes=frozenset({"female"})))
    assert len(female) == 1


def test_unknown_filters_and_quality_flags_are_refused() -> None:
    with pytest.raises(DefinitionError):
        RunFilters.from_dict({"colour": ["blue"]})
    with pytest.raises(DefinitionError):
        RunFilters(quality=frozenset({"NOT_A_FLAG"}))


def test_duplicate_rows_show_scoped_source_events_under_an_alias() -> None:
    evaluation = evaluate()
    keyed = {(e.namespace, e.encounter_key): e for e in ENCOUNTERS}
    rows = build_duplicate_rows(
        evaluation, encounters_by_key=keyed, alias=alias, facility_names={"F1": "One"}
    )
    same_day = [row for row in rows if row["kind"] in {"same_day_distinct", "near_duplicate"}]
    assert len(same_day) == 1
    row = same_day[0]
    assert row["patient_alias"].startswith("MARS-PT2-")
    assert "person-SECRET" not in json.dumps(row)
    assert {m["tests"][0]["source_event"] for m in row["members"]} == {
        "evt-SECRET-c1",
        "evt-SECRET-c2",
    }
    assert sum(m["is_representative"] for m in row["members"]) == 1


def test_comparison_by_alias_is_deterministic() -> None:
    loose = [c for c, _ in findings(evaluate(min_gap=1))]
    strict = [c for c, _ in findings(evaluate(min_gap=11))]
    first = compare_run_findings(loose, strict)
    assert first == compare_run_findings(list(reversed(loose)), strict)
    assert first["union_count"] == len(
        set(first["a_only"]) | set(first["b_only"]) | set(first["both"])
    )
    assert first["a_only"] and not first["b_only"]


def test_projection_comparison_reports_facility_and_treatment_deltas() -> None:
    a = build_projections(
        evaluate(min_gap=1), RunFilters(), facility_geography={}, facility_names={}
    )
    b = build_projections(
        evaluate(min_gap=11), RunFilters(), facility_geography={}, facility_names={}
    )
    deltas = compare_projections(a, b)
    assert {row["key"] for row in deltas["facilities"]}
    assert all(row["delta"] == row["b"] - row["a"] for row in deltas["facilities"])
    assert "transitions, not patients" in deltas["units"]["treatments"]


def test_the_manifest_checksum_changes_with_any_part() -> None:
    base = {"period_start": "2026-08-01", "filters": {"sexes": []}, "definition": "x"}
    assert manifest_checksum(**base) == manifest_checksum(**dict(reversed(list(base.items()))))
    assert manifest_checksum(**base) != manifest_checksum(**{**base, "definition": "y"})
