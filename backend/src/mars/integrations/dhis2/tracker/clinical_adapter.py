"""Live Tracker events to canonical clinical encounters.

The adapter joins the laboratory, medical-visit and medicine stages of one
programme through the source's own parent-event reference, which is the only
link MARS treats as verified. It never matches records by date proximity, never
reads a tracked-entity attribute, and never carries the OPD register number or
any other identifier into the canonical record.

Dates keep the precision the source recorded (``RemoteEvent.occurred_precision``).
A date entered without a time, or a wall-clock time without an offset, keeps its
date as recorded; an instant with an offset keeps its offset and the engine
takes its day in the reporting timezone.

**Each laboratory test is dated by its own laboratory event.** The visit a test
belongs to dates the encounter for the care timeline, and never replaces the
date of a positive result. See ``docs/methods/configurable-recurrence.md``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from mars.domain.longitudinal import (
    DEFAULT_TIMEZONE,
    UNRESOLVED_OUTCOMES,
    AdaptedEvidence,
    Availability,
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    DuplicateGroup,
    LabOutcome,
    Medication,
    MedicationEvidence,
    QualityFlag,
    SourceRecord,
    latest_revisions,
)
from mars.integrations.ports import RemoteEvent

LAB, VISIT, MEDICINE = "laboratory", "medical_visit", "medicine"

#: Visit fields the adapter may read. The OPD register number is deliberately
#: absent: it identifies a person and is never part of canonical evidence.
VISIT_FIELD_ROLES: tuple[str, ...] = (
    "diagnosis",
    "signs_and_symptoms",
    "malaria_treated",
    "patient_type",
    "outcome",
    "referral_out",
    "referral_reason",
)

#: Recorded by no mapped element, so reported as unmapped rather than guessed.
UNMAPPED_MEDICATION_FIELDS: tuple[str, ...] = (
    "formulation",
    "dose",
    "frequency",
    "duration",
    "dispensing",
)

_PRECISIONS: dict[str, DatePrecision] = {
    "date": DatePrecision.DAY,
    "local_time": DatePrecision.LOCAL_TIME,
    "timestamp": DatePrecision.TIMESTAMP,
}
_DISPLAY_ZONE = ZoneInfo(DEFAULT_TIMEZONE)


def normalise_option(value: str | None) -> str:
    dashes = {ord(chr(0x2013)): "-", ord(chr(0x2014)): "-"}
    return (value or "").translate(dashes).strip().casefold()


def classify_method(value: str | None) -> str:
    """``rdt``, ``microscopy`` or ``unknown``; an option code stays unknown."""
    text = normalise_option(value)
    if "rdt" in text:
        return "rdt"
    if "microscopy" in text or text.endswith(" bs"):
        return "microscopy"
    return "unknown"


def live_namespace(host: str, programme: str | None) -> str:
    return f"dhis2:{host.casefold()}:{programme or 'unknown-programme'}"


def event_time(event: RemoteEvent) -> tuple[date, datetime | None, DatePrecision]:
    """An event's date, instant and precision, as the source recorded them.

    For an instant with an offset, the returned date is its day in the default
    reporting timezone and is for display; the engine recomputes the day in the
    query's timezone from the instant.
    """
    precision = _PRECISIONS.get(event.occurred_precision, DatePrecision.DAY)
    if precision is DatePrecision.TIMESTAMP and event.occurred_at.tzinfo is not None:
        return event.occurred_at.astimezone(_DISPLAY_ZONE).date(), event.occurred_at, precision
    if precision is DatePrecision.TIMESTAMP:
        # An "instant" without an offset is not an instant; keep the recorded date.
        precision = DatePrecision.LOCAL_TIME
    return event.occurred_at.date(), None, precision


@dataclass(frozen=True, slots=True)
class LiveClinicalMapping:
    """The verified parts of the approved live mapping, and nothing more."""

    test_type: str
    result: str
    malaria_tests: frozenset[str]
    positive_results: frozenset[str]
    negative_result: str
    version: str
    programme: str | None = None
    lab_stage: str | None = None
    visit_stage: str | None = None
    medicine_stage: str | None = None
    parent: str | None = None
    visit_fields: tuple[tuple[str, str], ...] = ()
    medicine_name: str | None = None
    medicine_quantity: str | None = None

    @classmethod
    def from_config(cls, mapping: Mapping[str, Any]) -> LiveClinicalMapping:
        tracker = mapping["tracker"]
        elements: Mapping[str, str] = tracker.get("data_elements") or {}
        options: Mapping[str, Any] = tracker.get("options") or {}
        stages: Mapping[str, str] = tracker.get("stages") or {}
        return cls(
            test_type=elements["laboratory_test_type"],
            result=elements["laboratory_result"],
            malaria_tests=frozenset(
                normalise_option(item) for item in options.get("test_types_malaria", [])
            ),
            positive_results=frozenset(
                normalise_option(item) for item in options.get("positive_malaria_results", [])
            ),
            negative_result=normalise_option(options.get("negative_result", "Negative")),
            version=str(mapping.get("schema_version", "unversioned")),
            programme=mapping.get("programme_uid"),
            lab_stage=stages.get("laboratory_tests"),
            visit_stage=stages.get("medical_visit"),
            medicine_stage=stages.get("medicines_and_supplies"),
            parent=tracker.get("parent_event_data_element_uid"),
            visit_fields=tuple(
                (role, elements[role]) for role in VISIT_FIELD_ROLES if elements.get(role)
            ),
            medicine_name=elements.get("medicine_type"),
            medicine_quantity=elements.get("medicine_quantity"),
        )

    @property
    def retrieves_context(self) -> bool:
        """Whether visit and medicine stages can be joined at all."""
        return bool(self.visit_stage and self.medicine_stage and self.parent)

    def outcome(self, raw: str | None) -> LabOutcome:
        if raw is None or not raw.strip():
            return LabOutcome.NOT_RECORDED
        value = normalise_option(raw)
        if value in self.positive_results:
            return LabOutcome.POSITIVE
        if value == self.negative_result:
            return LabOutcome.NEGATIVE
        return LabOutcome.UNMAPPED


def _revision(event: RemoteEvent) -> tuple[str, ...]:
    moment = event.updated_at or event.occurred_at
    return (moment.astimezone(UTC).isoformat(),)


def _source(event: RemoteEvent, namespace: str, stage: str) -> SourceRecord:
    return SourceRecord(namespace, event.remote_id, stage, _revision(event)[0])


def _latest(events: Sequence[RemoteEvent], groups: list[DuplicateGroup]) -> list[RemoteEvent]:
    kept, repeats = latest_revisions(
        events,
        identity=lambda e: e.remote_id,
        revision=_revision,
        member_label=lambda e: e.remote_id,
    )
    groups.extend(repeats)
    return kept


def _parent(event: RemoteEvent, mapping: LiveClinicalMapping) -> str | None:
    if not mapping.parent:
        return None
    value = event.data_values.get(mapping.parent)
    return value.strip() if value and value.strip() else None


def _medication(event: RemoteEvent, mapping: LiveClinicalMapping, namespace: str) -> Medication:
    name = event.data_values.get(mapping.medicine_name) if mapping.medicine_name else None
    quantity = (
        event.data_values.get(mapping.medicine_quantity) if mapping.medicine_quantity else None
    )
    availability: dict[str, Availability] = {
        "name": Availability.RECORDED if name else Availability.NOT_RECORDED,
        "quantity": (
            Availability.NOT_MAPPED
            if not mapping.medicine_quantity
            else Availability.RECORDED
            if quantity
            else Availability.NOT_RECORDED
        ),
    }
    availability.update(dict.fromkeys(UNMAPPED_MEDICATION_FIELDS, Availability.NOT_MAPPED))
    return Medication(
        source=_source(event, namespace, MEDICINE),
        name=name or None,
        # The medicines stage does not say whether a line was prescribed or
        # dispensed, so neither is claimed.
        evidence=MedicationEvidence.RECORDED,
        quantity=quantity or None,
        availability=MappingProxyType(availability),
    )


@dataclass(slots=True)
class _Draft:
    person: str
    parent: str | None
    tests: list[tuple[RemoteEvent, ClinicalTest]]
    flags: set[QualityFlag]


def adapt_live_events(
    lab_events: Sequence[RemoteEvent],
    mapping: LiveClinicalMapping,
    *,
    namespace: str,
    visit_events: Sequence[RemoteEvent] = (),
    medicine_events: Sequence[RemoteEvent] = (),
    context_retrieved: bool = False,
    context_missing_facilities: frozenset[str] = frozenset(),
) -> AdaptedEvidence:
    """Build canonical encounters for everyone with at least one malaria test.

    ``context_retrieved`` states whether the visit and medicine stages were
    actually fetched for this evidence. When they were not, their absence is
    reported as ``not_returned`` or ``not_mapped`` - never as "no treatment".
    """
    groups: list[DuplicateGroup] = []
    labs = _latest(lab_events, groups)
    visits = {event.remote_id: event for event in _latest(visit_events, groups)}
    medicines = _latest(medicine_events, groups)

    drafts: dict[str, _Draft] = {}
    unresolved = 0
    for event in sorted(labs, key=lambda e: e.remote_id):
        if normalise_option(event.data_values.get(mapping.test_type)) not in mapping.malaria_tests:
            continue
        outcome = mapping.outcome(event.data_values.get(mapping.result))
        if outcome in UNRESOLVED_OUTCOMES:
            unresolved += 1
        observed_on, observed_at, precision = event_time(event)
        test = ClinicalTest(
            _source(event, namespace, LAB),
            classify_method(event.data_values.get(mapping.test_type)),
            outcome,
            observed_on=observed_on,
            observed_at=observed_at,
            precision=precision,
        )
        parent = _parent(event, mapping)
        key = f"visit:{parent}" if parent else f"event:{event.remote_id}"
        flags: set[QualityFlag] = set()
        if key in drafts and drafts[key].person != event.person_remote_id:
            # Two people claim one parent. People are never merged: this test
            # stands alone and the conflict is flagged.
            key, parent = f"event:{event.remote_id}", None
            flags.add(QualityFlag.IDENTITY_LINKAGE_UNCERTAIN)
        draft = drafts.setdefault(key, _Draft(event.person_remote_id, parent, [], set()))
        draft.tests.append((event, test))
        draft.flags.update(flags)

    patients = {draft.person for draft in drafts.values()}
    for visit in sorted(visits.values(), key=lambda e: e.remote_id):
        key = f"visit:{visit.remote_id}"
        if visit.person_remote_id in patients and key not in drafts:
            drafts[key] = _Draft(visit.person_remote_id, visit.remote_id, [], set())

    linked: dict[str, list[Medication]] = defaultdict(list)
    unlinked: dict[str, list[Medication]] = defaultdict(list)
    for event in sorted(medicines, key=lambda e: e.remote_id):
        if event.person_remote_id not in patients:
            continue
        medication = _medication(event, mapping, namespace)
        parent = _parent(event, mapping)
        medicine_key = f"visit:{parent}" if parent else None
        if (
            medicine_key is not None
            and medicine_key in drafts
            and drafts[medicine_key].person == event.person_remote_id
        ):
            linked[medicine_key].append(medication)
        else:
            unlinked[event.person_remote_id].append(medication)

    encounters = tuple(
        _build(
            key,
            drafts[key],
            visits,
            linked.get(key, []),
            mapping,
            namespace,
            context_retrieved,
            context_missing_facilities,
        )
        for key in sorted(drafts)
    )
    return AdaptedEvidence(
        encounters=encounters,
        unlinked_medications=MappingProxyType(
            {person: tuple(items) for person, items in sorted(unlinked.items())}
        ),
        duplicate_groups=tuple(groups),
        unresolved_results=unresolved,
        namespace=namespace,
        mapping_version=mapping.version,
    )


def _build(
    key: str,
    draft: _Draft,
    visits: Mapping[str, RemoteEvent],
    medications: list[Medication],
    mapping: LiveClinicalMapping,
    namespace: str,
    context_retrieved: bool,
    context_missing: frozenset[str],
) -> CanonicalEncounter:
    visit = visits.get(draft.parent) if draft.parent else None
    joined = visit is not None and visit.person_remote_id == draft.person
    flags = set(draft.flags)
    if visit is not None and not joined:
        flags.add(QualityFlag.IDENTITY_LINKAGE_UNCERTAIN)

    tests = sorted(draft.tests, key=lambda pair: (pair[0].occurred_at, pair[0].remote_id))
    # The encounter's own date is the visit's when the visit was joined, and
    # otherwise its earliest test's. It serves the timeline; each test keeps
    # its own date, and a positive is dated by its test.
    anchor = visit if joined and visit is not None else tests[0][0]
    day, moment, precision = event_time(anchor)
    facility = anchor.organisation_unit_remote_id

    values: Mapping[str, str | None] = visit.data_values if joined and visit is not None else {}
    fields = {role: (values.get(uid) or "").strip() for role, uid in mapping.visit_fields}
    attributes = tuple(
        (role, fields[role])
        for role in ("patient_type", "malaria_treated", "referral_reason")
        if fields.get(role)
    )
    referrals = (
        ("referred out",) if normalise_option(fields.get("referral_out")) in {"true", "yes"} else ()
    )

    if not mapping.retrieves_context:
        context = Availability.NOT_MAPPED
    elif not context_retrieved or facility in context_missing:
        context = Availability.NOT_RETURNED
    else:
        context = Availability.RECORDED
    visit_availability = (
        Availability.RECORDED
        if joined
        else context
        if context is not Availability.RECORDED
        else Availability.NOT_RETURNED
        if draft.parent
        else Availability.NOT_RECORDED
    )
    sources = tuple(
        ([_source(visit, namespace, VISIT)] if joined and visit is not None else [])
        + [test.source for _, test in tests]
        + [medication.source for medication in medications]
    )
    return CanonicalEncounter(
        encounter_key=key,
        namespace=namespace,
        patient_key=draft.person,
        facility_ref=facility,
        encounter_date=day,
        date_precision=precision,
        occurred_at=moment,
        parent_ref=draft.parent,
        parent_retrieved=joined,
        tests=tuple(test for _, test in tests),
        diagnoses=(fields["diagnosis"],) if fields.get("diagnosis") else (),
        attendance=fields.get("patient_type") or None,
        referrals=referrals,
        outcome=fields.get("outcome") or None,
        observations=(fields["signs_and_symptoms"],) if fields.get("signs_and_symptoms") else (),
        attributes=attributes,
        medications=tuple(medications),
        sources=sources,
        availability=MappingProxyType(
            {
                "visit": visit_availability,
                "medications": context,
                # Neither a fever flag nor sex/age is requested from the source.
                "fever": Availability.NOT_MAPPED,
                "sex": Availability.NOT_MAPPED,
                "age": Availability.NOT_MAPPED,
            }
        ),
        quality_flags=frozenset(flags),
        mapping_version=mapping.version,
    )


__all__ = [
    "LAB",
    "MEDICINE",
    "UNMAPPED_MEDICATION_FIELDS",
    "VISIT",
    "VISIT_FIELD_ROLES",
    "LiveClinicalMapping",
    "adapt_live_events",
    "classify_method",
    "event_time",
    "live_namespace",
    "normalise_option",
]
