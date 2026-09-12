"""The shared positive-to-positive recurrence engine.

One function, :func:`evaluate_recurrence`, takes canonical encounters and a
frozen definition and returns every patient's finding. It is source-independent
and pure: no database, no network, no browser and no current clock. The live
Tracker adapter and the stored-encounter adapter both feed it, which is what
makes their numbers the same number.

The contract - anchored windows, inclusive bounds, every-anchor evaluation,
lookback-aware determination and the deterministic witness - is set out in
``docs/methods/configurable-recurrence.md``. In particular:

* a confirmed positive is dated by its positive tests, never by its visit;
* people are grouped by :class:`~mars.domain.longitudinal.PatientKey`, so equal
  references from two namespaces are two people;
* duplicate classification runs first and feeds eligibility, so a
  ``POSSIBLE_DUPLICATE`` quality exclusion actually removes records;
* an absence of recurrence is only a reliable ``does_not_qualify`` when the
  evidence covers the lookback, the whole reporting period and every requested
  facility.

Nothing here asserts treatment failure or resistance; a qualifying chain is a
reason to review a patient.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

from mars.analytics.duplicate_detection import (
    classify_same_day,
    select_representative,
    shared_parent_group,
)
from mars.domain.longitudinal import (
    ENGINE_VERSION,
    UNRESOLVED_OUTCOMES,
    CanonicalEncounter,
    ChainTransition,
    Determination,
    DuplicateGroup,
    DuplicateKind,
    EncounterDecision,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    LabOutcome,
    MedicationEvidence,
    PatientFinding,
    PatientKey,
    PositiveInterval,
    QualityFlag,
    RecurrenceEvaluation,
    SameDayPolicy,
    evidence_fingerprint,
)

UNKNOWN = "unknown"


def evaluate_recurrence(
    encounters: Sequence[CanonicalEncounter],
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    coverage: EvidenceCoverage,
    *,
    dataset_id: str | None = None,
) -> RecurrenceEvaluation:
    zone = ZoneInfo(query.timezone)
    lookback_start = query.lookback_start(definition)
    methods = definition.permitted_positive_methods

    notes = _coverage_notes(coverage, query, lookback_start)
    coverage_status: Literal["complete", "partial"] = "complete" if not notes else "partial"

    dated: dict[tuple[str, str], tuple[date | None, bool]] = {
        (e.namespace, e.encounter_key): _evidence_day(e, zone, methods) for e in encounters
    }

    def day_of(encounter: CanonicalEncounter) -> date | None:
        return dated[(encounter.namespace, encounter.encounter_key)][0]

    by_patient: dict[PatientKey, list[CanonicalEncounter]] = defaultdict(list)
    unlinked: list[str] = []
    for encounter in encounters:
        patient = encounter.patient
        if patient is None:
            if encounter.has_outcome(LabOutcome.POSITIVE, methods):
                unlinked.append(encounter.encounter_key)
            continue
        by_patient[patient].append(encounter)

    duplicate_groups: list[DuplicateGroup] = []
    for encounter in encounters:
        group = shared_parent_group(encounter)
        if group is not None:
            duplicate_groups.append(group)
    # Classification comes first and feeds eligibility below.
    same_day = classify_same_day(
        [e for items in by_patient.values() for e in items],
        zone,
        unresolved=definition.same_day_policy is SameDayPolicy.REVIEW_SEPARATELY,
        day_of=day_of,
    )
    duplicate_groups.extend(same_day)
    membership: dict[PatientKey, dict[str, DuplicateGroup]] = defaultdict(dict)
    for group in same_day:
        if group.patient is not None:
            for member in group.members:
                membership[group.patient][member] = group

    findings = tuple(
        _evaluate_patient(
            patient,
            by_patient[patient],
            definition,
            query,
            coverage,
            days={e.encounter_key: day_of(e) for e in by_patient[patient]},
            disputed=frozenset(
                e.encounter_key
                for e in by_patient[patient]
                if dated[(e.namespace, e.encounter_key)][1]
            ),
            membership=membership.get(patient, {}),
        )
        for patient in sorted(by_patient)
    )
    return RecurrenceEvaluation(
        definition=definition,
        query=query,
        coverage=coverage,
        lookback_start=lookback_start,
        coverage_status=coverage_status,
        coverage_notes=tuple(notes),
        patients=findings,
        duplicate_groups=tuple(sorted(duplicate_groups, key=lambda g: g.group_id)),
        unlinked_positive_encounters=tuple(sorted(unlinked)),
        engine_version=ENGINE_VERSION,
        dataset_hash=evidence_fingerprint(encounters, coverage),
        dataset_id=dataset_id,
    )


def _coverage_notes(
    coverage: EvidenceCoverage, query: FrozenAnalysisQuery, lookback_start: date
) -> list[str]:
    notes: list[str] = []
    if not coverage.complete:
        notes.append("Retrieval coverage is incomplete; an absence of recurrence is not reliable.")
    if not coverage.starts_by(lookback_start):
        notes.append(
            f"Evidence does not reach the lookback start {lookback_start.isoformat()}; "
            "chains that began before the observed evidence cannot be seen."
        )
    if not coverage.ends_by(query.period_end):
        notes.append(
            "Evidence ends before the reporting period does; a later positive could "
            "complete a chain that is not visible."
        )
    missing = coverage.missing_facilities
    if missing:
        notes.append(
            f"{len(missing)} requested facilit{'y has' if len(missing) == 1 else 'ies have'} "
            "no evidence coverage for this span; a chain could run through "
            f"{'it' if len(missing) == 1 else 'them'}."
        )
    return notes


def _evidence_day(
    encounter: CanonicalEncounter, zone: ZoneInfo, methods: frozenset[str]
) -> tuple[date | None, bool]:
    """The day the engine uses for one encounter, and whether it is disputed.

    A confirmed positive is dated by its positive tests - the earliest usable
    day among them - never by the visit it belongs to. Positive tests that
    disagree about the day, or a positive test with no usable day beside dated
    ones, make the date disputed. A context encounter uses its own day.
    """
    positive = encounter.positive_days(zone, methods)
    if not positive:
        return encounter.local_day(zone), False
    known = [day for day in positive if day is not None]
    if not known:
        return None, False
    return min(known), len(set(known)) > 1 or len(known) < len(positive)


# ---------------------------------------------------------------------------
# One patient
# ---------------------------------------------------------------------------
def _evaluate_patient(
    patient: PatientKey,
    encounters: list[CanonicalEncounter],
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    coverage: EvidenceCoverage,
    *,
    days: Mapping[str, date | None],
    disputed: frozenset[str],
    membership: Mapping[str, DuplicateGroup],
) -> PatientFinding:
    history = sorted(encounters, key=lambda e: (days[e.encounter_key] or date.max, e.encounter_key))
    methods = definition.permitted_positive_methods
    flags: set[QualityFlag] = set()

    decisions: dict[str, EncounterDecision] = {}
    eligible: list[CanonicalEncounter] = []
    observed_positive = 0
    for encounter in history:
        day = days[encounter.encounter_key]
        group = membership.get(encounter.encounter_key)
        own = {flag for flag in encounter.quality_flags if flag is not QualityFlag.VALID}
        if group is not None:
            own.add(QualityFlag.POSSIBLE_DUPLICATE)
        if encounter.encounter_key in disputed:
            own.add(QualityFlag.DATE_QUALITY_ISSUE)
        positive = encounter.has_outcome(LabOutcome.POSITIVE, methods)
        if positive:
            observed_positive += 1
        reason = "context_not_positive"
        counted = False
        if positive:
            excluded = own & definition.quality_exclusions
            if day is None:
                own.add(QualityFlag.DATE_QUALITY_ISSUE)
                reason = "excluded_unusable_date"
            elif QualityFlag.POSSIBLE_DUPLICATE in excluded:
                reason = "excluded_possible_duplicate"
            elif excluded:
                reason = "excluded_by_quality_rule"
            else:
                reason = "eligible_positive"
                counted = True
                eligible.append(encounter)
        elif encounter.has_outcome(LabOutcome.POSITIVE):
            reason = "positive_method_not_permitted"
        flags.update(own)
        decisions[encounter.encounter_key] = EncounterDecision(
            encounter_key=encounter.encounter_key,
            local_day=day,
            facility_ref=encounter.facility_ref,
            confirmed_positive=positive,
            counted=counted,
            reason=reason,
            duplicate_group=group.group_id if group is not None else None,
            quality_flags=frozenset(own),
        )

    sequence = _apply_same_day_policy(eligible, days, definition, decisions)

    seq_days = [days[e.encounter_key] for e in sequence]
    ordinals = [d.toordinal() for d in seq_days if d is not None]
    adjacent = tuple(
        _interval("adjacent", sequence[i], sequence[i + 1], days) for i in range(len(sequence) - 1)
    )
    anchors = tuple(_interval("anchor", sequence[0], later, days) for later in sequence[1:])

    in_period = [e for e in sequence if query.contains(days[e.encounter_key] or date.min)]
    witness = _best_chain(sequence, ordinals, definition, query, days)

    if not in_period:
        determination = Determination.NOT_IN_PERIOD
    elif witness:
        determination = Determination.QUALIFIES
    elif _absence_unreliable(in_period, history, definition, query, coverage, days):
        determination = Determination.INDETERMINATE
    else:
        determination = Determination.DOES_NOT_QUALIFY

    if _has_unresolved_tests(history, days):
        flags.add(QualityFlag.INCOMPLETE_TEST_EVIDENCE)

    chain = tuple(sequence[i] for i in witness)
    transitions = tuple(_transition(chain[i], chain[i + 1], days) for i in range(len(chain) - 1))
    if chain and any(not e.has_linked_medication for e in chain[:-1]):
        flags.add(QualityFlag.INCOMPLETE_TREATMENT_EVIDENCE)
    quality = frozenset(flags) or frozenset({QualityFlag.VALID})

    final = chain[-1] if chain else None
    latest_in_period = in_period[-1] if in_period else None
    profile = latest_in_period or (history[-1] if history else None)
    chain_interval = _interval("chain", chain[0], chain[-1], days) if len(chain) >= 2 else None

    return PatientFinding(
        patient=patient,
        determination=determination,
        observed_positive_count=observed_positive,
        eligible_positive_count=len(sequence),
        chain=tuple(e.encounter_key for e in chain),
        chain_interval=chain_interval,
        adjacent_intervals=adjacent,
        anchor_intervals=anchors,
        transitions=transitions,
        final_encounter=final.encounter_key if final else None,
        final_facility=final.facility_ref if final else None,
        final_date=days[final.encounter_key] if final else None,
        in_period_positive_facilities=frozenset(e.facility_ref for e in in_period),
        latest_in_period_positive=(
            days[latest_in_period.encounter_key] if latest_in_period else None
        ),
        facilities=frozenset(e.facility_ref for e in sequence),
        decisions=tuple(decisions[e.encounter_key] for e in history),
        quality_flags=quality,
        explanation=_explain(determination, chain, days, definition, quality, decisions),
        sex=(profile.sex if profile and profile.sex else UNKNOWN),
        age_group=(profile.age_group if profile and profile.age_group else UNKNOWN),
    )


def _apply_same_day_policy(
    eligible: list[CanonicalEncounter],
    days: Mapping[str, date | None],
    definition: FrozenRecurrenceDefinition,
    decisions: dict[str, EncounterDecision],
) -> list[CanonicalEncounter]:
    """Choose the positives that may count; record the decision for the rest."""
    if definition.same_day_policy is SameDayPolicy.INCLUDE:
        return sorted(eligible, key=lambda e: (days[e.encounter_key] or date.max, e.encounter_key))
    by_day: dict[date, list[CanonicalEncounter]] = defaultdict(list)
    for encounter in eligible:
        day = days[encounter.encounter_key]
        if day is not None:
            by_day[day].append(encounter)
    chosen: list[CanonicalEncounter] = []
    for day in sorted(by_day):
        members = by_day[day]
        representative = select_representative(members)
        chosen.append(representative)
        for other in members:
            if other is representative:
                continue
            decisions[other.encounter_key] = replace(
                decisions[other.encounter_key],
                counted=False,
                reason=f"same_day_{definition.same_day_policy.value}",
                representative_of=representative.encounter_key,
            )
    return chosen


def _best_chain(
    sequence: list[CanonicalEncounter],
    ordinals: list[int],
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    days: Mapping[str, date | None],
) -> list[int]:
    """The deterministic qualifying witness, as positions into ``sequence``.

    For each anchor, a longest-chain table is filled left to right inside the
    anchored window. A two-pointer prefix maximum supplies each position's best
    permitted predecessor, so the cost is ``O(P^2)`` per patient.
    """
    n = len(sequence)
    if n < definition.minimum_positive_encounters:
        return []
    treated = [e.has_linked_medication for e in sequence]
    minimum = definition.minimum_gap_days
    window = definition.maximum_window_days
    needed = definition.minimum_positive_encounters

    best_key: tuple[int, int, int, int] | None = None
    best_path: list[int] = []
    for anchor in range(n):
        length: list[int | None] = [None] * n
        predecessor: list[int | None] = [None] * n
        length[anchor] = 1
        pointer = anchor
        run_value: int | None = None
        run_index: int | None = None
        for j in range(anchor + 1, n):
            if ordinals[j] - ordinals[anchor] > window:
                break
            while pointer < j and ordinals[j] - ordinals[pointer] >= minimum:
                value = length[pointer]
                usable = value is not None and (
                    not definition.require_linked_treatment or treated[pointer]
                )
                if usable and value is not None and (run_value is None or value >= run_value):
                    run_value, run_index = value, pointer
                pointer += 1
            if run_value is not None:
                length[j] = run_value + 1
                predecessor[j] = run_index
            final_day = days[sequence[j].encounter_key]
            chain_length = length[j]
            if (
                chain_length is not None
                and chain_length >= needed
                and final_day is not None
                and query.contains(final_day)
            ):
                key = (chain_length, -ordinals[j], -ordinals[anchor], -j)
                if best_key is None or key > best_key:
                    best_key = key
                    best_path = _backtrack(predecessor, j)
    return best_path


def _backtrack(predecessor: list[int | None], end: int) -> list[int]:
    path = [end]
    current = predecessor[end]
    while current is not None:
        path.append(current)
        current = predecessor[current]
    return list(reversed(path))


def _absence_unreliable(
    in_period: list[CanonicalEncounter],
    history: list[CanonicalEncounter],
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    coverage: EvidenceCoverage,
    days: Mapping[str, date | None],
) -> bool:
    """Whether "no chain" could be an artefact of what was not observed.

    Incomplete retrieval, evidence that starts after the lookback a positive
    needs, evidence that stops before the reporting period ends, a requested
    facility with no coverage, and an unresolved result inside the window can
    each conceal a chain.
    """
    if not coverage.complete or coverage.earliest_date is None:
        return True
    if not coverage.ends_by(query.period_end) or coverage.missing_facilities:
        return True
    earliest = coverage.earliest_date.toordinal()
    for encounter in in_period:
        day = days[encounter.encounter_key]
        if day is not None and day.toordinal() - definition.maximum_window_days < earliest:
            return True
    return _has_unresolved_tests(history, days, query.lookback_start(definition), query.period_end)


def _has_unresolved_tests(
    history: list[CanonicalEncounter],
    days: Mapping[str, date | None],
    start: date | None = None,
    end: date | None = None,
) -> bool:
    """An unmapped or unreturned malaria result, optionally only within a window.

    An undated record cannot be placed outside the window, so it always counts.
    """
    for encounter in history:
        day = days[encounter.encounter_key]
        if start is not None and end is not None and day is not None and not start <= day <= end:
            continue
        if any(t.outcome in UNRESOLVED_OUTCOMES for t in encounter.tests):
            return True
    return False


def _interval(
    kind: Literal["adjacent", "anchor", "chain"],
    start: CanonicalEncounter,
    end: CanonicalEncounter,
    days: Mapping[str, date | None],
) -> PositiveInterval:
    first = days[start.encounter_key]
    last = days[end.encounter_key]
    assert first is not None and last is not None  # sequences hold dated encounters only
    return PositiveInterval(
        kind=kind,
        from_encounter=start.encounter_key,
        to_encounter=end.encounter_key,
        from_date=first,
        to_date=last,
        days=(last - first).days,
    )


def _transition(
    earlier: CanonicalEncounter, later: CanonicalEncounter, days: Mapping[str, date | None]
) -> ChainTransition:
    first = days[earlier.encounter_key]
    last = days[later.encounter_key]
    assert first is not None and last is not None
    names = tuple(sorted({_medicine_name(m.name) for m in earlier.medications}))
    if earlier.medications:
        kinds = {m.evidence for m in earlier.medications}
        if MedicationEvidence.DISPENSED in kinds:
            evidence = MedicationEvidence.DISPENSED.value
        elif MedicationEvidence.PRESCRIBED in kinds:
            evidence = MedicationEvidence.PRESCRIBED.value
        else:
            evidence = MedicationEvidence.RECORDED.value
    elif earlier.availability.get("medications") in {None, "recorded"}:
        evidence = "not_linked"
    else:
        evidence = "not_available"
    return ChainTransition(
        from_encounter=earlier.encounter_key,
        to_encounter=later.encounter_key,
        days=(last - first).days,
        prior_medications=names,
        prior_treatment_evidence=evidence,
    )


def _medicine_name(name: str | None) -> str:
    return " ".join((name or "not recorded").split()).casefold()


def _explain(
    determination: Determination,
    chain: tuple[CanonicalEncounter, ...],
    days: Mapping[str, date | None],
    definition: FrozenRecurrenceDefinition,
    quality: frozenset[QualityFlag],
    decisions: dict[str, EncounterDecision],
) -> tuple[str, ...]:
    rule = (
        f"minimum gap {definition.minimum_gap_days} days, maximum window "
        f"{definition.maximum_window_days} days, at least "
        f"{definition.minimum_positive_encounters} positive encounters, same-day policy "
        f"{definition.same_day_policy.value} ({definition.engine_version})"
    )
    lines: list[str] = []
    if determination is Determination.QUALIFIES:
        first = days[chain[0].encounter_key]
        last = days[chain[-1].encounter_key]
        assert first is not None and last is not None
        lines.append(
            f"{len(chain)} confirmed positive encounters from {first.isoformat()} to "
            f"{last.isoformat()} ({(last - first).days} days) satisfy {rule}."
        )
        for earlier, later in pairwise(chain):
            a, b = days[earlier.encounter_key], days[later.encounter_key]
            assert a is not None and b is not None
            treatment = (
                "a medicine was recorded on the earlier encounter"
                if earlier.has_linked_medication
                else "no medicine is linked to the earlier encounter"
            )
            lines.append(f"{a.isoformat()} to {b.isoformat()}: {(b - a).days} days; {treatment}.")
    elif determination is Determination.DOES_NOT_QUALIFY:
        lines.append(f"No chain of positives in the observed evidence satisfies {rule}.")
    elif determination is Determination.INDETERMINATE:
        lines.append(
            f"No qualifying chain was found under {rule}, but the evidence is "
            "incomplete, so this is not a reliable absence."
        )
    else:
        lines.append("No eligible positive encounter falls inside the reporting period.")
    lines.append("Positive dates are the positive tests' own dates, not visit dates.")
    skipped = sum(1 for d in decisions.values() if d.reason.startswith("same_day_"))
    if skipped:
        lines.append(
            f"Same-day rule: {skipped} additional same-day positive record(s) were kept "
            "visible and not counted separately."
        )
    else:
        lines.append("Same-day duplicate rule triggered: no.")
    excluded = sum(1 for d in decisions.values() if d.reason == "excluded_possible_duplicate")
    if excluded:
        lines.append(
            f"{excluded} positive record(s) in unresolved same-day groups were excluded "
            "by the definition's POSSIBLE_DUPLICATE exclusion; they remain visible."
        )
    disputed = sum(
        1
        for d in decisions.values()
        if d.confirmed_positive and QualityFlag.DATE_QUALITY_ISSUE in d.quality_flags
    )
    if disputed:
        lines.append(
            f"{disputed} positive encounter(s) have disagreeing or missing test dates; "
            "the earliest usable positive test date was used."
        )
    lines.append("Quality: " + ", ".join(sorted(flag.value for flag in quality)) + ".")
    return tuple(lines)


def interval_kinds() -> tuple[str, ...]:
    return ("adjacent", "anchor", "chain")


def duplicate_kinds() -> tuple[str, ...]:
    return tuple(kind.value for kind in DuplicateKind)


__all__ = ["evaluate_recurrence"]
