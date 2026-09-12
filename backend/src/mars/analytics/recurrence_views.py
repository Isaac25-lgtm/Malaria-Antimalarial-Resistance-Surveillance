"""Projections of one frozen recurrence evaluation.

Every panel - summary, intervals, facilities, weeks, frequency, treatment matrix,
geography and comparison - reads the same :class:`RecurrenceEvaluation`, so their
counts cannot drift apart. Counting units and attribution rules follow
``docs/methods/configurable-recurrence.md`` section 6:

* the numerator counts each qualifying patient once;
* the denominator counts distinct linked patients with an eligible positive in
  the reporting period, and a proportion is shown with both;
* facility attribution uses the qualifying chain's final-positive facility, so
  facility counts do **not** sum to a unique-patient total;
* the treatment matrix counts linked positive-to-positive **transitions**, not
  patients.

A repeat-positive proportion is not a resistance rate and not a treatment-failure
rate; :data:`~mars.domain.longitudinal.INTERPRETATION_LIMIT` travels with the
summary.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from mars.domain.longitudinal import (
    INTERPRETATION_LIMIT,
    OUTSIDE_BANDS_LABEL,
    Determination,
    IntervalBand,
    PatientFinding,
    PatientKey,
    QualityFlag,
    RecurrenceEvaluation,
)

UNMAPPED_GEOGRAPHY = "unmapped"


@dataclass(frozen=True, slots=True)
class CohortFilter:
    """Filters applied *after* evaluation; context is never removed first.

    ``facility_refs`` constrains the numerator by attribution facility and the
    denominator by in-period positive facility. ``sexes``, ``age_groups`` and
    ``quality`` constrain the patient cohort. Unknown values stay explicit
    categories: ``"unknown"`` age is not zero years.
    """

    facility_refs: frozenset[str] | None = None
    sexes: frozenset[str] | None = None
    age_groups: frozenset[str] | None = None
    quality: frozenset[QualityFlag] | None = None

    def admits_patient(self, finding: PatientFinding) -> bool:
        if self.sexes is not None and finding.sex not in self.sexes:
            return False
        if self.age_groups is not None and finding.age_group not in self.age_groups:
            return False
        return self.quality is None or bool(finding.quality_flags & self.quality)

    def admits_numerator(self, finding: PatientFinding) -> bool:
        if finding.determination is not Determination.QUALIFIES or not self.admits_patient(finding):
            return False
        return self.facility_refs is None or finding.final_facility in self.facility_refs

    def admits_denominator(self, finding: PatientFinding) -> bool:
        if not finding.in_denominator or not self.admits_patient(finding):
            return False
        return self.facility_refs is None or bool(
            finding.in_period_positive_facilities & self.facility_refs
        )


NO_FILTER = CohortFilter()


def _proportion(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RecurrenceSummary:
    qualifying_patients: int
    denominator_patients: int
    proportion: float | None
    indeterminate_patients: int
    observed_positive_encounters: int
    eligible_positive_encounters: int
    unlinked_positive_encounters: int
    coverage_status: str
    coverage_notes: tuple[str, ...]
    interpretation: str = INTERPRETATION_LIMIT


def summarise(
    evaluation: RecurrenceEvaluation, cohort: CohortFilter = NO_FILTER
) -> RecurrenceSummary:
    numerator = [p for p in evaluation.patients if cohort.admits_numerator(p)]
    denominator = [p for p in evaluation.patients if cohort.admits_denominator(p)]
    return RecurrenceSummary(
        qualifying_patients=len(numerator),
        denominator_patients=len(denominator),
        proportion=_proportion(len(numerator), len(denominator)),
        indeterminate_patients=sum(
            1 for p in denominator if p.determination is Determination.INDETERMINATE
        ),
        observed_positive_encounters=sum(p.observed_positive_count for p in denominator),
        eligible_positive_encounters=sum(p.eligible_positive_count for p in denominator),
        unlinked_positive_encounters=len(evaluation.unlinked_positive_encounters),
        coverage_status=evaluation.coverage_status,
        coverage_notes=evaluation.coverage_notes,
    )


# ---------------------------------------------------------------------------
# Interval distributions
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BandCount:
    label: str
    lower_days: int | None
    upper_days: int | None
    count: int


def band_counts(values: Iterable[int], bands: tuple[IntervalBand, ...]) -> tuple[BandCount, ...]:
    counts: Counter[str] = Counter()
    for value in values:
        band = next((b for b in bands if b.contains(value)), None)
        counts[band.label if band else OUTSIDE_BANDS_LABEL] += 1
    rows = [BandCount(b.label, b.lower_days, b.upper_days, counts[b.label]) for b in bands]
    if counts[OUTSIDE_BANDS_LABEL]:
        rows.append(BandCount(OUTSIDE_BANDS_LABEL, None, None, counts[OUTSIDE_BANDS_LABEL]))
    return tuple(rows)


def interval_distribution(
    evaluation: RecurrenceEvaluation,
    kind: Literal["observed_adjacent", "qualifying_chain"],
    cohort: CohortFilter = NO_FILTER,
) -> tuple[BandCount, ...]:
    """Two views over the same cohort, before and after thresholds.

    ``observed_adjacent`` counts every adjacent eligible-positive interval that
    ends inside the reporting period, for denominator patients, before any
    threshold. ``qualifying_chain`` counts the chain interval of each qualifying
    patient, after thresholds.
    """
    query = evaluation.query
    if kind == "observed_adjacent":
        values = [
            interval.days
            for p in evaluation.patients
            if cohort.admits_denominator(p)
            for interval in p.adjacent_intervals
            if query.contains(interval.to_date)
        ]
    else:
        values = [
            p.chain_interval.days
            for p in evaluation.patients
            if cohort.admits_numerator(p) and p.chain_interval is not None
        ]
    return band_counts(values, evaluation.definition.interval_bands)


# ---------------------------------------------------------------------------
# Facility, week, frequency and geography
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FacilityRow:
    facility_ref: str
    qualifying_patients: int
    denominator_patients: int
    proportion: float | None


def facility_breakdown(
    evaluation: RecurrenceEvaluation, cohort: CohortFilter = NO_FILTER
) -> tuple[FacilityRow, ...]:
    numerator: Counter[str] = Counter()
    denominator: Counter[str] = Counter()
    for p in evaluation.patients:
        if cohort.admits_numerator(p) and p.final_facility is not None:
            numerator[p.final_facility] += 1
        if cohort.admits_denominator(p):
            for facility in p.in_period_positive_facilities:
                if cohort.facility_refs is None or facility in cohort.facility_refs:
                    denominator[facility] += 1
    facilities = sorted(set(numerator) | set(denominator))
    return tuple(
        FacilityRow(f, numerator[f], denominator[f], _proportion(numerator[f], denominator[f]))
        for f in facilities
    )


@dataclass(frozen=True, slots=True)
class WeekRow:
    week_start: date
    qualifying_patients: int
    eligible_positive_patients: int


def _week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def weekly_trend(
    evaluation: RecurrenceEvaluation, cohort: CohortFilter = NO_FILTER
) -> tuple[WeekRow, ...]:
    """Qualifying patients by ISO week (Monday) of their final positive."""
    qualifying: Counter[date] = Counter()
    eligible: Counter[date] = Counter()
    for p in evaluation.patients:
        if cohort.admits_numerator(p) and p.final_date is not None:
            qualifying[_week(p.final_date)] += 1
        if cohort.admits_denominator(p) and p.latest_in_period_positive is not None:
            eligible[_week(p.latest_in_period_positive)] += 1
    weeks = sorted(set(qualifying) | set(eligible))
    return tuple(WeekRow(w, qualifying[w], eligible[w]) for w in weeks)


@dataclass(frozen=True, slots=True)
class FrequencyRow:
    label: str
    patients: int


def positive_frequency(
    evaluation: RecurrenceEvaluation, cohort: CohortFilter = NO_FILTER
) -> tuple[FrequencyRow, ...]:
    """Qualifying patients by the number of positives in their witness chain."""
    counts: Counter[str] = Counter()
    for p in evaluation.patients:
        if cohort.admits_numerator(p):
            counts["4+" if p.chain_length >= 4 else str(p.chain_length)] += 1
    return tuple(FrequencyRow(label, counts[label]) for label in ("2", "3", "4+"))


@dataclass(frozen=True, slots=True)
class GeographyRow:
    geography_ref: str
    qualifying_patients: int
    denominator_patients: int
    proportion: float | None


def geography_breakdown(
    evaluation: RecurrenceEvaluation,
    facility_geography: Mapping[str, str],
    cohort: CohortFilter = NO_FILTER,
) -> tuple[GeographyRow, ...]:
    """Unique patients per mapped area. Unmapped facilities stay visible.

    A patient is counted once per area even when several of their facilities
    map to it; nothing is placed on a map without a verified crosswalk.
    """
    numerator: dict[str, set[PatientKey]] = defaultdict(set)
    denominator: dict[str, set[PatientKey]] = defaultdict(set)
    for p in evaluation.patients:
        if cohort.admits_numerator(p) and p.final_facility is not None:
            numerator[facility_geography.get(p.final_facility, UNMAPPED_GEOGRAPHY)].add(p.patient)
        if cohort.admits_denominator(p):
            for facility in p.in_period_positive_facilities:
                denominator[facility_geography.get(facility, UNMAPPED_GEOGRAPHY)].add(p.patient)
    areas = sorted(set(numerator) | set(denominator))
    return tuple(
        GeographyRow(
            area,
            len(numerator[area]),
            len(denominator[area]),
            _proportion(len(numerator[area]), len(denominator[area])),
        )
        for area in areas
    )


# ---------------------------------------------------------------------------
# Treatment matrix
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MatrixCell:
    """Linked positive-to-positive transitions, never patients."""

    prior_treatment: str
    band: str
    transitions: int
    by_evidence: tuple[tuple[str, int], ...]


def treatment_matrix(
    evaluation: RecurrenceEvaluation, cohort: CohortFilter = NO_FILTER
) -> tuple[MatrixCell, ...]:
    """Recorded treatment before a later positive, by interval band.

    Label: "Repeat-positive after recorded treatment". Prescribed and dispensed
    evidence are counted separately, and unlinked or unavailable treatment stays
    an explicit row rather than vanishing.
    """
    cells: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    bands = evaluation.definition.interval_bands
    for p in evaluation.patients:
        if not cohort.admits_numerator(p):
            continue
        for step in p.transitions:
            if step.prior_medications:
                row = " + ".join(step.prior_medications)
            elif step.prior_treatment_evidence == "not_available":
                row = "treatment not available"
            else:
                row = "no linked treatment"
            band = next((b.label for b in bands if b.contains(step.days)), OUTSIDE_BANDS_LABEL)
            cells[(row, band)][step.prior_treatment_evidence] += 1
    return tuple(
        MatrixCell(
            prior_treatment=row,
            band=band,
            transitions=sum(counter.values()),
            by_evidence=tuple(sorted(counter.items())),
        )
        for (row, band), counter in sorted(cells.items())
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
class ComparisonRefusedError(ValueError):
    """Two evaluations that cannot be compared without misleading."""


@dataclass(frozen=True, slots=True)
class DefinitionComparison:
    """A/B overlap. Patient keys are internal; the service aliases them."""

    a_only: tuple[PatientKey, ...]
    b_only: tuple[PatientKey, ...]
    both: tuple[PatientKey, ...]
    union_count: int
    facilities_a: tuple[FacilityRow, ...]
    facilities_b: tuple[FacilityRow, ...]
    dataset_hash: str
    dataset_id: str | None


def compare(
    a: RecurrenceEvaluation, b: RecurrenceEvaluation, cohort: CohortFilter = NO_FILTER
) -> DefinitionComparison:
    """A/B overlap over one immutable dataset, or an explicit refusal.

    The two evaluations must have read the *same evidence*: equal dataset
    fingerprints, and equal dataset identifiers where both were persisted.
    Matching coverage labels are not enough - two different patient datasets
    can share dates and facilities - and a difference between two datasets
    would otherwise be reported as a difference between two definitions.
    """
    resolution = "Re-run both definitions over one retained dataset, then compare those two runs."
    if a.dataset_hash != b.dataset_hash:
        raise ComparisonRefusedError(
            "The two analyses were evaluated over different evidence. " + resolution
        )
    if a.dataset_id is not None and b.dataset_id is not None and a.dataset_id != b.dataset_id:
        raise ComparisonRefusedError(
            "The two analyses belong to different retained datasets. " + resolution
        )
    if a.coverage != b.coverage:
        raise ComparisonRefusedError(
            "The two analyses do not share the same evidence coverage. " + resolution
        )
    if a.query != b.query:
        raise ComparisonRefusedError(
            "The two analyses cover different reporting periods or timezones."
        )
    left = {p.patient for p in a.patients if cohort.admits_numerator(p)}
    right = {p.patient for p in b.patients if cohort.admits_numerator(p)}
    return DefinitionComparison(
        a_only=tuple(sorted(left - right)),
        b_only=tuple(sorted(right - left)),
        both=tuple(sorted(left & right)),
        union_count=len(left | right),
        facilities_a=facility_breakdown(a, cohort),
        facilities_b=facility_breakdown(b, cohort),
        dataset_hash=a.dataset_hash,
        dataset_id=a.dataset_id or b.dataset_id,
    )


__all__ = [
    "NO_FILTER",
    "UNMAPPED_GEOGRAPHY",
    "BandCount",
    "CohortFilter",
    "ComparisonRefusedError",
    "DefinitionComparison",
    "FacilityRow",
    "FrequencyRow",
    "GeographyRow",
    "MatrixCell",
    "RecurrenceSummary",
    "WeekRow",
    "band_counts",
    "compare",
    "facility_breakdown",
    "geography_breakdown",
    "interval_distribution",
    "positive_frequency",
    "summarise",
    "treatment_matrix",
    "weekly_trend",
]
