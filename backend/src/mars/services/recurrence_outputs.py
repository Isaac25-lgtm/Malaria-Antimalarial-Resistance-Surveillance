"""Pure builders for recurrence analysis runs.

Everything a run stores, and every projection an endpoint serves, is shaped here
from one frozen evaluation. The patient list, the aggregate panels, the
duplicate review and a comparison therefore share one denominator contract and
cannot disagree about a count.

Filters apply to the *patient cohort after evaluation*. Context encounters are
never removed before the clinical sequence is reconstructed, so a filter can
narrow who is listed but can never change how a patient's chain is judged:

* ``facility_refs`` constrains attribution - the numerator by final-positive
  facility, the denominator by in-period positive facility;
* ``sexes``, ``age_groups`` and ``quality`` constrain the cohort, with
  ``unknown`` kept as an explicit category;
* ``test_methods`` keeps patients with an eligible positive by that method;
* ``treatments`` keeps patients with a recorded medicine linked to an eligible
  positive encounter.

No database, no clock and no key: sealing is injected by the caller. No output
here contains a patient key, an encounter key or a source identifier except
inside the sealed detail and duplicate payloads.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date
from typing import Any

from mars.analytics.recurrence_views import (
    CohortFilter,
    facility_breakdown,
    geography_breakdown,
    interval_distribution,
    positive_frequency,
    summarise,
    treatment_matrix,
    weekly_trend,
)
from mars.domain.longitudinal import (
    ALIAS_SCHEME,
    INTERPRETATION_LIMIT,
    CanonicalEncounter,
    DefinitionError,
    DuplicateKind,
    LabOutcome,
    Medication,
    PatientFinding,
    PatientKey,
    PositiveInterval,
    QualityFlag,
    RecurrenceEvaluation,
)
from mars.domain.longitudinal_codec import encode_encounter, encode_medication

_FILTER_FIELDS = ("facility_refs", "sexes", "age_groups", "quality", "test_methods", "treatments")

#: The counting unit and attribution rule of every projection, stated once.
UNITS: dict[str, str] = {
    "summary": (
        "Distinct linked patients. The numerator counts each qualifying patient once; the "
        "denominator counts patients with an eligible positive in the reporting period. A "
        "repeat-positive proportion is not a resistance rate and not a treatment-failure rate."
    ),
    "intervals": (
        "observed_adjacent: every adjacent eligible-positive interval ending in the period, "
        "before thresholds. qualifying_chain: each qualifying patient's chain interval, after "
        "thresholds."
    ),
    "facilities": (
        "Patients attributed to the facility of their qualifying chain's final positive. "
        "Facility rows do not add up to the unique-patient total."
    ),
    "weekly": (
        "Qualifying patients by the ISO week of their final positive. Weeks do not add up to "
        "the period total."
    ),
    "frequency": "Qualifying patients by the number of positives in their qualifying chain.",
    "treatments": (
        "Linked positive-to-positive transitions, not patients: repeat-positive after recorded "
        "treatment. Prescribed, dispensed and recorded evidence are counted separately."
    ),
    "geography": "Unique patients per mapped area. Unmapped facilities stay visible.",
}


def _plain(value: Any) -> Any:
    """A JSON-ready rendering of projection dataclasses."""
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(_plain(item) for item in value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def medicine_name(name: str | None) -> str:
    return " ".join((name or "not recorded").split()).casefold()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunFilters:
    facility_refs: frozenset[str] = frozenset()
    sexes: frozenset[str] = frozenset()
    age_groups: frozenset[str] = frozenset()
    quality: frozenset[str] = frozenset()
    test_methods: frozenset[str] = frozenset()
    treatments: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        known = {flag.value for flag in QualityFlag}
        unknown = self.quality - known
        if unknown:
            raise DefinitionError(f"Unknown quality flags: {sorted(unknown)}")

    def as_dict(self) -> dict[str, list[str]]:
        return {name: sorted(getattr(self, name)) for name in _FILTER_FIELDS}

    @classmethod
    def from_dict(cls, value: Mapping[str, Iterable[str]] | None) -> RunFilters:
        supplied = dict(value or {})
        unknown = set(supplied) - set(_FILTER_FIELDS)
        if unknown:
            raise DefinitionError(f"Unknown filters: {sorted(unknown)}")
        return cls(
            **{
                name: frozenset(str(item) for item in supplied.get(name) or ())
                for name in _FILTER_FIELDS
            }
        )

    @property
    def value_count(self) -> int:
        return sum(len(getattr(self, name)) for name in _FILTER_FIELDS)

    def cohort(self) -> CohortFilter:
        return CohortFilter(
            facility_refs=self.facility_refs or None,
            sexes=self.sexes or None,
            age_groups=self.age_groups or None,
            quality=frozenset(QualityFlag(flag) for flag in self.quality) or None,
        )


@dataclass(frozen=True, slots=True)
class PatientFacts:
    """Per-patient facts the engine's finding does not carry."""

    test_methods: frozenset[str]
    treatment_names: frozenset[str]


def patient_facts(
    finding: PatientFinding, encounters: Sequence[CanonicalEncounter]
) -> PatientFacts:
    counted = {decision.encounter_key for decision in finding.decisions if decision.counted}
    eligible = [encounter for encounter in encounters if encounter.encounter_key in counted]
    return PatientFacts(
        test_methods=frozenset(
            test.method
            for encounter in eligible
            for test in encounter.tests
            if test.outcome is LabOutcome.POSITIVE
        ),
        treatment_names=frozenset(
            medicine_name(medication.name)
            for encounter in eligible
            for medication in encounter.medications
        ),
    )


def restrict(
    evaluation: RecurrenceEvaluation,
    filters: RunFilters,
    facts: Mapping[PatientKey, PatientFacts],
) -> RecurrenceEvaluation:
    """The same evaluation, with the test-type and treatment filters applied.

    Patients are removed whole; nobody's chain is re-judged.
    """
    if not filters.test_methods and not filters.treatments:
        return evaluation

    def keep(finding: PatientFinding) -> bool:
        known = facts.get(finding.patient, PatientFacts(frozenset(), frozenset()))
        if filters.test_methods and not known.test_methods & filters.test_methods:
            return False
        return not filters.treatments or bool(known.treatment_names & filters.treatments)

    return replace(evaluation, patients=tuple(p for p in evaluation.patients if keep(p)))


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
def duplicate_counts(evaluation: RecurrenceEvaluation) -> dict[str, int]:
    counts = {kind.value: 0 for kind in DuplicateKind}
    for group in evaluation.duplicate_groups:
        counts[group.kind.value] += 1
    counts["unresolved"] = sum(1 for group in evaluation.duplicate_groups if group.unresolved)
    return counts


def build_projections(
    evaluation: RecurrenceEvaluation,
    filters: RunFilters,
    *,
    facility_geography: Mapping[str, str],
    facility_names: Mapping[str, str],
) -> dict[str, Any]:
    cohort = filters.cohort()
    return {
        "summary": _plain(summarise(evaluation, cohort)),
        "intervals": {
            "observed_adjacent": _plain(
                interval_distribution(evaluation, "observed_adjacent", cohort)
            ),
            "qualifying_chain": _plain(
                interval_distribution(evaluation, "qualifying_chain", cohort)
            ),
        },
        "facilities": [
            {
                **_plain(row),
                "facility_name": facility_names.get(row.facility_ref, "Authorised facility"),
            }
            for row in facility_breakdown(evaluation, cohort)
        ],
        "weekly": _plain(weekly_trend(evaluation, cohort)),
        "frequency": _plain(positive_frequency(evaluation, cohort)),
        "treatments": _plain(treatment_matrix(evaluation, cohort)),
        "geography": _plain(geography_breakdown(evaluation, facility_geography, cohort)),
        "duplicates": duplicate_counts(evaluation),
        "coverage": {
            "status": evaluation.coverage_status,
            "notes": list(evaluation.coverage_notes),
            "lookback_start": evaluation.lookback_start.isoformat(),
        },
        "facility_names": dict(sorted(facility_names.items())),
        "units": UNITS,
        "interpretation": INTERPRETATION_LIMIT,
    }


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
def _encounter_order(encounter: CanonicalEncounter) -> tuple[date, str]:
    return (encounter.encounter_date or date.max, encounter.encounter_key)


def _interval(interval: PositiveInterval, refs: Mapping[str, str]) -> dict[str, Any]:
    return {
        "from_ref": refs.get(interval.from_encounter),
        "to_ref": refs.get(interval.to_encounter),
        "from_date": interval.from_date.isoformat(),
        "to_date": interval.to_date.isoformat(),
        "days": interval.days,
    }


def finding_detail(
    finding: PatientFinding,
    encounters: Sequence[CanonicalEncounter],
    unlinked: Sequence[Medication],
) -> dict[str, Any]:
    """Everything a timeline needs, keyed by opaque per-patient references.

    Sealed before storage. ``refs`` maps internal encounter keys to ``E1``,
    ``E2``...; only the references ever leave the service.
    """
    ordered = sorted(encounters, key=_encounter_order)
    refs = {encounter.encounter_key: f"E{index}" for index, encounter in enumerate(ordered, 1)}
    return {
        "patient": [finding.patient.namespace, finding.patient.reference],
        "refs": refs,
        "determination": finding.determination.value,
        "explanation": list(finding.explanation),
        "quality_flags": sorted(flag.value for flag in finding.quality_flags),
        "chain": [refs.get(key) for key in finding.chain],
        "intervals": {
            "adjacent": [_interval(item, refs) for item in finding.adjacent_intervals],
            "anchor": [_interval(item, refs) for item in finding.anchor_intervals],
            "chain": _interval(finding.chain_interval, refs) if finding.chain_interval else None,
        },
        "transitions": [
            {
                "from_ref": refs.get(step.from_encounter),
                "to_ref": refs.get(step.to_encounter),
                "days": step.days,
                "prior_medications": list(step.prior_medications),
                "prior_treatment_evidence": step.prior_treatment_evidence,
            }
            for step in finding.transitions
        ],
        "decisions": {
            refs[decision.encounter_key]: {
                "counted": decision.counted,
                "reason": decision.reason,
                "positive_day": decision.local_day.isoformat() if decision.local_day else None,
                "confirmed_positive": decision.confirmed_positive,
                "duplicate_group": decision.duplicate_group,
                "representative_of": (
                    refs.get(decision.representative_of) if decision.representative_of else None
                ),
                "quality_flags": sorted(flag.value for flag in decision.quality_flags),
            }
            for decision in finding.decisions
            if decision.encounter_key in refs
        },
        "encounters": [encode_encounter(encounter) for encounter in ordered],
        "unlinked_medications": [encode_medication(item) for item in unlinked],
    }


def build_findings(
    evaluation: RecurrenceEvaluation,
    filters: RunFilters,
    *,
    encounters_by_patient: Mapping[PatientKey, Sequence[CanonicalEncounter]],
    unlinked_by_patient: Mapping[PatientKey, Sequence[Medication]],
    facts: Mapping[PatientKey, PatientFacts],
    alias: Callable[[PatientKey], str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(clear columns, detail to seal)`` for every listed patient.

    Listed patients are the denominator cohort the filters admit.
    """
    cohort = filters.cohort()
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for finding in evaluation.patients:
        if not cohort.admits_denominator(finding):
            continue
        counted = [decision for decision in finding.decisions if decision.counted]
        days = [decision.local_day for decision in counted if decision.local_day is not None]
        known = facts.get(finding.patient, PatientFacts(frozenset(), frozenset()))
        encounters = encounters_by_patient.get(finding.patient, ())
        columns = {
            "patient_alias": alias(finding.patient),
            "alias_scheme": ALIAS_SCHEME,
            "determination": finding.determination.value,
            "chain_length": finding.chain_length,
            "eligible_positive_count": finding.eligible_positive_count,
            "observed_positive_count": finding.observed_positive_count,
            "chain_interval_days": finding.chain_interval.days if finding.chain_interval else None,
            "first_positive_on": days[0] if days else None,
            "latest_positive_on": days[-1] if days else None,
            "final_date": finding.final_date,
            "final_facility_ref": finding.final_facility,
            "in_period_facility_refs": sorted(finding.in_period_positive_facilities),
            "facility_refs": sorted({decision.facility_ref for decision in finding.decisions}),
            "quality_flags": sorted(flag.value for flag in finding.quality_flags),
            "treatment_names": sorted(known.treatment_names),
            "test_methods": sorted(known.test_methods),
            "cross_facility": finding.cross_facility,
            "sex": finding.sex,
            "age_group": finding.age_group,
        }
        detail = finding_detail(finding, encounters, unlinked_by_patient.get(finding.patient, ()))
        rows.append((columns, detail))
    rows.sort(
        key=lambda pair: (
            pair[0]["latest_positive_on"] or date.min,
            pair[0]["patient_alias"],
        ),
        reverse=True,
    )
    return rows


def build_duplicate_rows(
    evaluation: RecurrenceEvaluation,
    *,
    encounters_by_key: Mapping[tuple[str, str], CanonicalEncounter],
    alias: Callable[[PatientKey], str],
    facility_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Duplicate review rows, with scoped source references for authorised review."""
    rows: list[dict[str, Any]] = []
    for group in evaluation.duplicate_groups:
        namespace = group.patient.namespace if group.patient else None
        members: list[dict[str, Any]] = []
        for key in group.members:
            encounter = encounters_by_key.get((namespace, key)) if namespace else None
            members.append(
                {
                    "reference": key,
                    "facility_name": (
                        facility_names.get(encounter.facility_ref, "Authorised facility")
                        if encounter
                        else None
                    ),
                    "occurred_at": (
                        encounter.occurred_at.isoformat()
                        if encounter and encounter.occurred_at
                        else None
                    ),
                    "encounter_date": (
                        encounter.encounter_date.isoformat()
                        if encounter and encounter.encounter_date
                        else None
                    ),
                    "tests": [
                        {
                            "method": test.method,
                            "result": test.outcome.value,
                            "observed_on": test.observed_on.isoformat()
                            if test.observed_on
                            else None,
                            "source_event": test.source.record_id,
                        }
                        for test in (encounter.tests if encounter else ())
                    ],
                    "is_representative": key == group.representative,
                }
            )
        rows.append(
            {
                "group_id": group.group_id,
                "kind": group.kind.value,
                "confidence": group.confidence.value,
                "day": group.local_day.isoformat() if group.local_day else None,
                "patient_alias": alias(group.patient) if group.patient else None,
                "reasons": list(group.reasons),
                "compared_fields": [list(pair) for pair in group.compared_fields],
                "unresolved": group.unresolved,
                "rule_version": group.rule_version,
                "members": members,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Comparison and manifest
# ---------------------------------------------------------------------------
def compare_run_findings(
    a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Overlap of qualifying patients, by display alias, over one dataset."""
    left = {row["patient_alias"] for row in a if row["determination"] == "qualifies"}
    right = {row["patient_alias"] for row in b if row["determination"] == "qualifies"}
    return {
        "a_only": sorted(left - right),
        "b_only": sorted(right - left),
        "both": sorted(left & right),
        "union_count": len(left | right),
        "a_count": len(left),
        "b_count": len(right),
    }


def _by(rows: Sequence[Mapping[str, Any]], key: str, value: str) -> dict[str, int]:
    return {str(row[key]): int(row[value]) for row in rows}


def compare_projections(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Facility, treatment and geography differences between two runs."""

    def deltas(left: dict[str, int], right: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "a": left.get(key, 0),
                "b": right.get(key, 0),
                "delta": right.get(key, 0) - left.get(key, 0),
            }
            for key in sorted(set(left) | set(right))
        ]

    def treatments(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for row in rows:
            totals[str(row["prior_treatment"])] = totals.get(str(row["prior_treatment"]), 0) + int(
                row["transitions"]
            )
        return totals

    return {
        "facilities": deltas(
            _by(a.get("facilities", []), "facility_ref", "qualifying_patients"),
            _by(b.get("facilities", []), "facility_ref", "qualifying_patients"),
        ),
        "treatments": deltas(
            treatments(a.get("treatments", [])), treatments(b.get("treatments", []))
        ),
        "geography": deltas(
            _by(a.get("geography", []), "geography_ref", "qualifying_patients"),
            _by(b.get("geography", []), "geography_ref", "qualifying_patients"),
        ),
        "units": {
            "facilities": UNITS["facilities"],
            "treatments": UNITS["treatments"],
            "geography": UNITS["geography"],
        },
    }


def manifest_checksum(**parts: Any) -> str:
    """SHA-256 of the frozen manifest: what was asked, over what, by whom."""
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


__all__ = [
    "UNITS",
    "PatientFacts",
    "RunFilters",
    "build_duplicate_rows",
    "build_findings",
    "build_projections",
    "compare_projections",
    "compare_run_findings",
    "duplicate_counts",
    "finding_detail",
    "manifest_checksum",
    "medicine_name",
    "patient_facts",
    "restrict",
]
