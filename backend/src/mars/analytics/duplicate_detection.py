"""Deterministic duplicate classification, independent of recurrence.

This engine answers "which records might be the same thing?" and records why. It
never deletes, edits or hides a record: every classification is a decision with
its evidence attached, and recurrence evaluation reads those decisions.

Similarity rules operate only *within* an already established patient linkage -
a :class:`~mars.domain.longitudinal.PatientKey`, namespace included. They are
never a fuzzy identity-matching mechanism, and a field missing on either side is
recorded as ``missing`` - it is never evidence of similarity.

Rule version ``duplicate-rules/1.1.0``; see ``docs/methods/configurable-recurrence.md``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date
from zoneinfo import ZoneInfo

from mars.domain.longitudinal import (
    DUPLICATE_RULE_VERSION,
    CanonicalEncounter,
    DatePrecision,
    DuplicateConfidence,
    DuplicateGroup,
    DuplicateKind,
    LabOutcome,
    PatientKey,
    group_id,
    latest_revisions,
)

#: Fields compared by the near-duplicate rule, in a fixed order.
COMPARED_FIELDS: tuple[str, ...] = ("facility", "positive_outcome", "test_methods", "diagnoses")

#: A near duplicate needs at least this many matching fields and none differing.
NEAR_DUPLICATE_MINIMUM_MATCHES = 2


def shared_parent_group(encounter: CanonicalEncounter) -> DuplicateGroup | None:
    """Several tests of one verified encounter: grouped, never double counted."""
    if len(encounter.tests) < 2 or encounter.parent_ref is None:
        return None
    members = tuple(test.source.record_id for test in encounter.tests)
    return DuplicateGroup(
        group_id=group_id(DuplicateKind.SHARED_PARENT_TESTS, members),
        kind=DuplicateKind.SHARED_PARENT_TESTS,
        patient=encounter.patient,
        local_day=encounter.encounter_date,
        members=members,
        representative=encounter.encounter_key,
        reasons=(f"{len(members)} tests share one verified clinical parent; one encounter.",),
        compared_fields=(),
        confidence=DuplicateConfidence.VERIFIED,
        rule_version=DUPLICATE_RULE_VERSION,
        unresolved=False,
    )


def compare(a: CanonicalEncounter, b: CanonicalEncounter) -> tuple[tuple[str, str], ...]:
    """Field-by-field comparison. Missing on either side is ``missing``."""

    def outcome(e: CanonicalEncounter) -> str | None:
        if not e.tests:
            return None
        return "positive" if e.has_outcome(LabOutcome.POSITIVE) else "not_positive"

    def methods(e: CanonicalEncounter) -> frozenset[str] | None:
        known = frozenset(t.method for t in e.tests if t.method not in {"unknown", ""})
        return known or None

    def diagnoses(e: CanonicalEncounter) -> frozenset[str] | None:
        return frozenset(d.casefold().strip() for d in e.diagnoses) or None

    pairs: tuple[tuple[str, object, object], ...] = (
        ("facility", a.facility_ref or None, b.facility_ref or None),
        ("positive_outcome", outcome(a), outcome(b)),
        ("test_methods", methods(a), methods(b)),
        ("diagnoses", diagnoses(a), diagnoses(b)),
    )
    result: list[tuple[str, str]] = []
    for name, left, right in pairs:
        if left is None or right is None:
            result.append((name, "missing"))
        else:
            result.append((name, "match" if left == right else "differ"))
    return tuple(result)


def select_representative(encounters: Sequence[CanonicalEncounter]) -> CanonicalEncounter:
    """Deterministic representative of a same-day group.

    The earliest timestamp wins only when every member carries a timestamp;
    otherwise, and on any tie, the lexicographically smallest encounter key.
    """
    if all(
        e.date_precision is DatePrecision.TIMESTAMP and e.occurred_at is not None
        for e in encounters
    ):
        return min(encounters, key=lambda e: (e.occurred_at, e.encounter_key))
    return min(encounters, key=lambda e: e.encounter_key)


def classify_same_day(
    encounters: Sequence[CanonicalEncounter],
    zone: ZoneInfo,
    *,
    unresolved: bool,
    day_of: Callable[[CanonicalEncounter], date | None] | None = None,
) -> list[DuplicateGroup]:
    """Group distinct linked encounters of one patient on one local day.

    ``day_of`` supplies the day the recurrence engine uses for each encounter -
    a positive's own test day - so a duplicate group and a recurrence interval
    can never disagree about which day a record belongs to. It defaults to the
    encounter's local day.
    """
    resolve = day_of if day_of is not None else (lambda e: e.local_day(zone))
    by_day: dict[tuple[PatientKey, date], list[CanonicalEncounter]] = defaultdict(list)
    for encounter in encounters:
        patient = encounter.patient
        day = resolve(encounter)
        if patient is None or day is None:
            continue
        by_day[(patient, day)].append(encounter)

    groups: list[DuplicateGroup] = []
    for (patient, day), members in sorted(by_day.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda e: e.encounter_key)
        representative = select_representative(members)
        comparisons = [
            compare(representative, other) for other in members if other is not representative
        ]
        near = all(
            sum(1 for _, verdict in fields if verdict == "match") >= NEAR_DUPLICATE_MINIMUM_MATCHES
            and not any(verdict == "differ" for _, verdict in fields)
            for fields in comparisons
        )
        cross_facility = len({e.facility_ref for e in members}) > 1
        kind = (
            DuplicateKind.NEAR_DUPLICATE
            if near and not cross_facility
            else DuplicateKind.SAME_DAY_DISTINCT
        )
        reasons = [f"{len(members)} distinct encounters on {day.isoformat()} for one patient."]
        if cross_facility:
            reasons.append("Recorded at different facilities; not asserted to be erroneous.")
        elif near:
            reasons.append("Every field present on both sides agrees.")
        keys = tuple(e.encounter_key for e in members)
        groups.append(
            DuplicateGroup(
                group_id=group_id(kind, (patient.namespace, *keys)),
                kind=kind,
                patient=patient,
                local_day=day,
                members=keys,
                representative=representative.encounter_key,
                reasons=tuple(reasons),
                compared_fields=comparisons[0] if comparisons else (),
                confidence=(
                    DuplicateConfidence.PROBABLE
                    if kind is DuplicateKind.NEAR_DUPLICATE
                    else DuplicateConfidence.POSSIBLE
                ),
                rule_version=DUPLICATE_RULE_VERSION,
                unresolved=unresolved,
            )
        )
    return groups


__all__ = [
    "COMPARED_FIELDS",
    "NEAR_DUPLICATE_MINIMUM_MATCHES",
    "classify_same_day",
    "compare",
    "group_id",
    "latest_revisions",
    "select_representative",
    "shared_parent_group",
]
