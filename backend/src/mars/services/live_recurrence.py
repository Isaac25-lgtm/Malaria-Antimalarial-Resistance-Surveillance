"""Repeat-positive evaluation for the live snapshot.

The DHIS2 adapter package is a leaf: it may not import ``mars.analytics``
(``tests/unit/test_module_boundaries.py``). It therefore hands canonical evidence
to this application service, which runs the shared engine and shapes the
pseudonymous rows. No row carries an encounter key, a source event ID or a
parent reference, and the display alias is computed by the caller's unchanged
alias function.

The live snapshot keeps its original alias scheme (``mars-live-patient-v1``)
for compatibility with aliases users have already seen. A snapshot is built
from exactly one live namespace - one Ministry host and programme - so equal
references from two namespaces cannot meet here. Run outputs use the
namespace-keyed ``ALIAS_SCHEME`` instead.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.domain.longitudinal import (
    AdaptedEvidence,
    CanonicalEncounter,
    Determination,
    DuplicateKind,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    LabOutcome,
    PatientFinding,
    PatientKey,
)


@dataclass(frozen=True, slots=True)
class LiveRecurrence:
    positive_rows: list[dict[str, Any]]
    repeat_rows: list[dict[str, Any]]
    denominator: int
    indeterminate: int
    duplicate_positive_groups: int
    coverage_status: Literal["complete", "partial"]
    coverage_notes: tuple[str, ...]
    dataset_hash: str
    #: The coverage the evaluation used, so retained evidence records it exactly.
    coverage: EvidenceCoverage


def evaluate_live_recurrence(
    evidence: AdaptedEvidence,
    *,
    definition: FrozenRecurrenceDefinition,
    period_start: date,
    period_end: date,
    extent_start: date,
    facility_names: Mapping[str, str],
    tracker_failed: Sequence[str],
    coverage_stages: frozenset[str],
    display_key: bytes,
    alias: Callable[[bytes, str], str],
) -> LiveRecurrence:
    coverage = EvidenceCoverage(
        earliest_date=extent_start,
        latest_date=period_end,
        complete=not tracker_failed,
        stages=coverage_stages,
        facility_refs=frozenset(facility_names) - frozenset(tracker_failed),
        requested_facility_refs=frozenset(facility_names),
    )
    evaluation = evaluate_recurrence(
        evidence.encounters, definition, FrozenAnalysisQuery(period_start, period_end), coverage
    )
    by_person: dict[PatientKey, list[CanonicalEncounter]] = defaultdict(list)
    for encounter in evidence.encounters:
        if encounter.patient is not None:
            by_person[encounter.patient].append(encounter)
    positive_rows = sorted(
        (
            _patient_row(finding, by_person[finding.patient], facility_names, display_key, alias)
            for finding in evaluation.patients
            if finding.in_denominator
        ),
        key=lambda row: (row["latest_positive_on"], row["mars_patient_id"]),
        reverse=True,
    )
    repeat_rows = sorted(
        (row for row in positive_rows if row["determination"] == "qualifies"),
        key=lambda row: (row["latest_positive_on"], row["positive_encounter_count"]),
        reverse=True,
    )
    positive_keys = {
        encounter.encounter_key
        for encounter in evidence.encounters
        if encounter.has_outcome(LabOutcome.POSITIVE)
    }
    return LiveRecurrence(
        positive_rows=positive_rows,
        repeat_rows=repeat_rows,
        denominator=len(evaluation.denominator),
        indeterminate=sum(
            1
            for finding in evaluation.denominator
            if finding.determination is Determination.INDETERMINATE
        ),
        duplicate_positive_groups=sum(
            1
            for group in evaluation.duplicate_groups
            if group.kind in {DuplicateKind.SAME_DAY_DISTINCT, DuplicateKind.NEAR_DUPLICATE}
            and positive_keys.intersection(group.members)
        ),
        coverage_status=evaluation.coverage_status,
        coverage_notes=evaluation.coverage_notes,
        dataset_hash=evaluation.dataset_hash,
        coverage=coverage,
    )


_RESULT_LABELS: dict[LabOutcome, str] = {
    LabOutcome.POSITIVE: "positive",
    LabOutcome.NEGATIVE: "negative",
    LabOutcome.NOT_RECORDED: "not_recorded",
    LabOutcome.NOT_DONE: "not_done",
}


def _patient_row(
    finding: PatientFinding,
    encounters: Sequence[CanonicalEncounter],
    facility_names: Mapping[str, str],
    display_key: bytes,
    alias: Callable[[bytes, str], str],
) -> dict[str, Any]:
    """One pseudonymous row. No encounter key, source ID or parent reference."""
    counted = [d for d in finding.decisions if d.counted]
    positive_days = [d.local_day for d in counted if d.local_day is not None]
    facility_ref = finding.final_facility or counted[-1].facility_ref
    tests = sorted(
        (
            {
                # The test's own date, never the visit's.
                "occurred_on": test_day.isoformat(),
                "facility_name": facility_names.get(encounter.facility_ref, "Authorised facility"),
                "result": _RESULT_LABELS.get(test.outcome, "unmapped"),
            }
            for encounter in encounters
            for test in encounter.tests
            if (test_day := test.observed_on or encounter.encounter_date) is not None
        ),
        key=lambda item: item["occurred_on"],
    )
    return {
        "mars_patient_id": alias(display_key, finding.patient.reference),
        "first_positive_on": positive_days[0].isoformat(),
        "latest_positive_on": positive_days[-1].isoformat(),
        "positive_encounter_count": finding.eligible_positive_count,
        "interval_days": finding.legacy_interval_days,
        "chain_interval_days": finding.chain_interval.days if finding.chain_interval else None,
        "adjacent_interval_days": [interval.days for interval in finding.adjacent_intervals],
        "anchor_interval_days": [interval.days for interval in finding.anchor_intervals],
        "chain_length": finding.chain_length,
        "observed_positive_count": finding.observed_positive_count,
        "determination": finding.determination.value,
        "quality_flags": sorted(flag.value for flag in finding.quality_flags),
        "explanation": list(finding.explanation),
        "facility_name": facility_names.get(facility_ref, "Authorised facility"),
        "cross_facility": finding.cross_facility,
        "tests": tests,
    }


__all__ = ["LiveRecurrence", "evaluate_live_recurrence"]
