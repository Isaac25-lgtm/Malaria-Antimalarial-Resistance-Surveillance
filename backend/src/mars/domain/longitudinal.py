"""Canonical longitudinal evidence and the frozen contracts of recurrence analysis.

This is a source-neutral contract, so it lives in ``mars.domain``: the DHIS2
adapter is a leaf that may import the domain but never ``mars.analytics``.

Source adapters - the live DHIS2 Tracker adapter and the stored OPD encounter
adapter - translate into these types, and every recurrence calculation consumes
only these types. Nothing here performs I/O, reads the clock, or knows about a
database, a network or a browser.

Absence is typed. ``NOT_RECORDED`` (the source said nothing), ``NOT_MAPPED`` (no
verified mapping exists), ``NOT_RETURNED`` (the retrieval did not bring it back)
and ``NOT_AUTHORIZED`` are different facts, and none of them is a negative value.

Two rules carry most of the weight:

* **A positive's date is the positive test's own date.** A test carries its
  observed day and precision; the visit it belongs to supplies context, never
  the date of the positive.
* **A patient is a namespace plus a reference.** A reference is stable only
  within the source that issued it, so :class:`PatientKey` holds both, and
  nothing links two namespaces.

The method contract is ``docs/methods/configurable-recurrence.md``.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import json
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

#: Bumped whenever a change could alter a finding for unchanged evidence.
#: 1.1.0: positive dates come from the positive test, duplicate exclusions act
#: on eligibility, forward and per-facility coverage gaps are indeterminate.
ENGINE_VERSION = "positive-recurrence/1.1.0"
DUPLICATE_RULE_VERSION = "duplicate-rules/1.1.0"
#: Version of the canonical evidence shape; part of every dataset fingerprint.
EVIDENCE_CONTRACT_VERSION = "canonical-evidence/2"
DEFAULT_TIMEZONE = "Africa/Kampala"

#: Linkage is by stable source patient reference within one verified namespace.
LINKAGE_RULE = "stable-source-reference/2"

#: Display aliases for run outputs. Version 2 keys the alias by namespace as well
#: as reference, so equal references from two sources can never share an alias.
ALIAS_SCHEME = "namespace-hmac/2"
_ALIAS_PURPOSE = b"mars-patient-alias-v2\x00"

#: Carried by every output. Repetition of a positive result is a reason to look.
INTERPRETATION_LIMIT = (
    "A repeat-positive pattern is a reason to review a patient. Routine data "
    "cannot distinguish recrudescence from reinfection, cannot establish that a "
    "medicine was taken, and cannot identify parasite genotype. This is not "
    "evidence of treatment failure and not evidence of antimalarial resistance."
)


class Availability(enum.StrEnum):
    RECORDED = "recorded"
    NOT_RECORDED = "not_recorded"
    NOT_MAPPED = "not_mapped"
    NOT_RETURNED = "not_returned"
    NOT_AUTHORIZED = "not_authorized"


class DatePrecision(enum.StrEnum):
    #: An instant with a known offset. Its local day depends on the timezone.
    TIMESTAMP = "timestamp"
    #: A wall-clock time recorded without an offset. The recorded date is the day.
    LOCAL_TIME = "local_time"
    #: A date recorded without a time.
    DAY = "day"
    UNKNOWN = "unknown"


class LabOutcome(enum.StrEnum):
    """A malaria test outcome. Only ``POSITIVE`` is a confirmed positive."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NOT_DONE = "not_done"
    #: A value is present but the mapping cannot interpret it.
    UNMAPPED = "unmapped"
    #: The retrieval did not bring the result back.
    NOT_RETURNED = "not_returned"
    #: The source record exists and its result field is empty.
    NOT_RECORDED = "not_recorded"


class QualityFlag(enum.StrEnum):
    VALID = "VALID"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    INCOMPLETE_TEST_EVIDENCE = "INCOMPLETE_TEST_EVIDENCE"
    INCOMPLETE_TREATMENT_EVIDENCE = "INCOMPLETE_TREATMENT_EVIDENCE"
    IDENTITY_LINKAGE_UNCERTAIN = "IDENTITY_LINKAGE_UNCERTAIN"
    DATE_QUALITY_ISSUE = "DATE_QUALITY_ISSUE"


class SameDayPolicy(enum.StrEnum):
    EXCLUDE = "exclude"
    REVIEW_SEPARATELY = "review_separately"
    INCLUDE = "include"


class MedicationEvidence(enum.StrEnum):
    PRESCRIBED = "prescribed"
    DISPENSED = "dispensed"
    #: A medicine was recorded; the source does not say prescribed or dispensed.
    RECORDED = "recorded"


class Determination(enum.StrEnum):
    QUALIFIES = "qualifies"
    DOES_NOT_QUALIFY = "does_not_qualify"
    #: No chain was found, but the evidence cannot support a reliable absence.
    INDETERMINATE = "indeterminate"
    #: No eligible positive falls inside the reporting period.
    NOT_IN_PERIOD = "not_in_period"


class DuplicateKind(enum.StrEnum):
    SOURCE_REVISION = "source_revision"
    SHARED_PARENT_TESTS = "shared_parent_tests"
    SAME_DAY_DISTINCT = "same_day_distinct"
    NEAR_DUPLICATE = "near_duplicate"


class DuplicateConfidence(enum.StrEnum):
    EXACT = "exact"
    VERIFIED = "verified"
    PROBABLE = "probable"
    POSSIBLE = "possible"


def _frozen(values: Mapping[str, Availability] | None = None) -> Mapping[str, Availability]:
    return MappingProxyType(dict(values or {}))


def _day_in(
    zone: ZoneInfo, precision: DatePrecision, recorded_on: date | None, moment: datetime | None
) -> date | None:
    """The calendar day of one piece of evidence in the reporting timezone.

    A date, or a wall-clock time recorded without an offset, keeps the date as
    recorded. An instant with an offset is converted to ``zone`` first. Nothing
    is invented for an unknown or naive-instant date.
    """
    if precision in (DatePrecision.DAY, DatePrecision.LOCAL_TIME):
        return recorded_on
    if precision is DatePrecision.TIMESTAMP and moment is not None and moment.tzinfo is not None:
        return moment.astimezone(zone).date()
    return None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, order=True)
class PatientKey:
    """A person's internal linkage key: a stable reference within one namespace.

    Two sources can issue the same reference to different people, so the
    namespace is part of the identity. Nothing here links keys across
    namespaces; that would need a separately verified linkage table, and MARS
    holds none. Never leaves the backend.
    """

    namespace: str
    reference: str


def patient_display_alias(key: bytes, patient: PatientKey) -> str:
    """A stable 100-bit HMAC alias of a namespaced patient key (``ALIAS_SCHEME``).

    Never a truncation of a source identifier, and different for equal
    references in different namespaces.
    """
    material = _ALIAS_PURPOSE + patient.namespace.encode() + b"\x00" + patient.reference.encode()
    digest = hmac.new(key, material, hashlib.sha256).digest()
    token = base64.b32encode(digest[:13]).decode("ascii").rstrip("=")
    return f"MARS-PT2-{token}"


# ---------------------------------------------------------------------------
# Canonical evidence
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One source record revision. Never a patient identifier."""

    namespace: str
    record_id: str
    stage: str
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class ClinicalTest:
    source: SourceRecord
    #: Normalised method: ``rdt``, ``microscopy``, ``other`` or ``unknown``.
    method: str
    outcome: LabOutcome
    #: The test's own evidence date. A positive's date is taken from here and
    #: never from the visit the test belongs to.
    observed_on: date | None = None
    observed_at: datetime | None = None
    precision: DatePrecision = DatePrecision.UNKNOWN

    def local_day(self, zone: ZoneInfo) -> date | None:
        return _day_in(zone, self.precision, self.observed_on, self.observed_at)


@dataclass(frozen=True, slots=True)
class Medication:
    """What the source recorded about a medicine, and nothing inferred."""

    source: SourceRecord
    name: str | None
    evidence: MedicationEvidence
    formulation: str | None = None
    dose: str | None = None
    dose_unit: str | None = None
    quantity: str | None = None
    frequency: str | None = None
    duration_days: str | None = None
    prescribed_on: date | None = None
    dispensed_on: date | None = None
    dispensed_quantity: str | None = None
    availability: Mapping[str, Availability] = field(default_factory=_frozen)


@dataclass(frozen=True, slots=True)
class CanonicalEncounter:
    """One clinical encounter, built from one or more source records.

    ``patient_key`` is a stable source reference *within* ``namespace``; use
    :attr:`patient` wherever people are grouped. It is an internal linkage key:
    it never leaves the backend, and output layers replace it with the keyed
    display alias.

    ``encounter_date`` is the encounter's own date - the visit when one was
    joined - and serves the care timeline. Positive-to-positive intervals use
    each positive test's own date (:meth:`positive_days`).
    """

    encounter_key: str
    namespace: str
    patient_key: str | None
    facility_ref: str
    encounter_date: date | None
    date_precision: DatePrecision
    occurred_at: datetime | None = None
    parent_ref: str | None = None
    #: Whether the parent (visit) record itself was retrieved and joined.
    parent_retrieved: bool = False
    tests: tuple[ClinicalTest, ...] = ()
    diagnoses: tuple[str, ...] = ()
    fever: str | None = None
    attendance: str | None = None
    referrals: tuple[str, ...] = ()
    outcome: str | None = None
    #: Recorded signs and observations, verbatim; nothing is inferred from them.
    observations: tuple[str, ...] = ()
    #: Other recorded visit fields as ``(label, value)`` pairs, in source order.
    attributes: tuple[tuple[str, str], ...] = ()
    medications: tuple[Medication, ...] = ()
    sources: tuple[SourceRecord, ...] = ()
    sex: str | None = None
    age_group: str | None = None
    availability: Mapping[str, Availability] = field(default_factory=_frozen)
    quality_flags: frozenset[QualityFlag] = frozenset()
    mapping_version: str = ""

    @property
    def patient(self) -> PatientKey | None:
        if self.patient_key is None:
            return None
        return PatientKey(self.namespace, self.patient_key)

    def local_day(self, zone: ZoneInfo) -> date | None:
        """The encounter's calendar day in the reporting timezone."""
        return _day_in(zone, self.date_precision, self.encounter_date, self.occurred_at)

    def positive_days(
        self, zone: ZoneInfo, methods: frozenset[str] = frozenset()
    ) -> tuple[date | None, ...]:
        """Each confirmed positive test's own day; ``None`` where it has none."""
        return tuple(
            test.local_day(zone)
            for test in self.tests
            if test.outcome is LabOutcome.POSITIVE and (not methods or test.method in methods)
        )

    def has_outcome(self, outcome: LabOutcome, methods: frozenset[str] = frozenset()) -> bool:
        return any(
            test.outcome is outcome and (not methods or test.method in methods)
            for test in self.tests
        )

    @property
    def has_linked_medication(self) -> bool:
        return bool(self.medications)


#: Results that could hide a confirmed positive. None of them is a negative.
UNRESOLVED_OUTCOMES: frozenset[LabOutcome] = frozenset(
    {LabOutcome.UNMAPPED, LabOutcome.NOT_RETURNED, LabOutcome.NOT_RECORDED}
)


@dataclass(frozen=True, slots=True)
class AdaptedEvidence:
    """What a source adapter produces: canonical encounters and their context.

    ``unlinked_medications`` holds medicines whose encounter could not be
    verified, keyed by the patient's source reference within ``namespace``;
    they are contextual evidence for the patient and are never attached to an
    encounter by proximity of date.
    """

    encounters: tuple[CanonicalEncounter, ...]
    unlinked_medications: Mapping[str, tuple[Medication, ...]]
    duplicate_groups: tuple[DuplicateGroup, ...]
    unresolved_results: int
    namespace: str
    mapping_version: str


# ---------------------------------------------------------------------------
# Definition, query and coverage
# ---------------------------------------------------------------------------
class DefinitionError(ValueError):
    """A recurrence definition or query that cannot be evaluated as stated."""


@dataclass(frozen=True, slots=True)
class IntervalBand:
    label: str
    lower_days: int
    #: Inclusive upper bound; ``None`` is open-ended and only allowed last.
    upper_days: int | None

    def contains(self, days: int) -> bool:
        return days >= self.lower_days and (self.upper_days is None or days <= self.upper_days)


DEFAULT_INTERVAL_BANDS: tuple[IntervalBand, ...] = (
    IntervalBand("Same day", 0, 0),
    IntervalBand("1-2 days", 1, 2),
    IntervalBand("3-6 days", 3, 6),
    IntervalBand("7-13 days", 7, 13),
    IntervalBand("14-27 days", 14, 27),
    IntervalBand("28-41 days", 28, 41),
    IntervalBand("42+ days", 42, None),
)

OUTSIDE_BANDS_LABEL = "Outside configured bands"


def validate_bands(bands: tuple[IntervalBand, ...]) -> None:
    if not bands:
        raise DefinitionError("At least one interval band is required")
    previous_upper: int | None = -1
    for index, band in enumerate(bands):
        if not band.label.strip():
            raise DefinitionError("Every interval band needs a label")
        if band.lower_days < 0:
            raise DefinitionError(f"Band {band.label!r} has a negative lower bound")
        if band.upper_days is not None and band.upper_days < band.lower_days:
            raise DefinitionError(f"Band {band.label!r} ends before it starts")
        if band.upper_days is None and index != len(bands) - 1:
            raise DefinitionError("Only the last interval band may be open-ended")
        if previous_upper is None or band.lower_days <= previous_upper:
            raise DefinitionError("Interval bands must be ordered and must not overlap")
        previous_upper = band.upper_days


@dataclass(frozen=True, slots=True)
class FrozenRecurrenceDefinition:
    minimum_gap_days: int
    maximum_window_days: int
    minimum_positive_encounters: int = 2
    same_day_policy: SameDayPolicy = SameDayPolicy.EXCLUDE
    #: Empty means any method whose result maps to a confirmed positive.
    permitted_positive_methods: frozenset[str] = frozenset()
    #: Every non-final chain member must carry a linked medicine.
    require_linked_treatment: bool = False
    #: Encounters carrying any of these flags do not take part in a sequence.
    #: ``POSSIBLE_DUPLICATE`` here removes every member of an unresolved
    #: same-day group, representative included; see the method contract.
    quality_exclusions: frozenset[QualityFlag] = frozenset()
    interval_bands: tuple[IntervalBand, ...] = DEFAULT_INTERVAL_BANDS
    duplicate_rule_version: str = DUPLICATE_RULE_VERSION
    engine_version: str = ENGINE_VERSION
    name: str = ""
    version: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_gap_days < 0:
            raise DefinitionError("The minimum gap cannot be negative")
        if self.maximum_window_days < self.minimum_gap_days:
            raise DefinitionError("The maximum window cannot be shorter than the minimum gap")
        if self.minimum_positive_encounters < 2:
            raise DefinitionError("A recurrence needs at least two positive encounters")
        if QualityFlag.VALID in self.quality_exclusions:
            raise DefinitionError("VALID cannot be an exclusion")
        validate_bands(self.interval_bands)

    def parameters(self) -> dict[str, object]:
        """The analytical parameters, in a stable order, for manifests."""
        return {
            "minimum_gap_days": self.minimum_gap_days,
            "maximum_window_days": self.maximum_window_days,
            "minimum_positive_encounters": self.minimum_positive_encounters,
            "same_day_policy": self.same_day_policy.value,
            "permitted_positive_methods": sorted(self.permitted_positive_methods),
            "require_linked_treatment": self.require_linked_treatment,
            "quality_exclusions": sorted(flag.value for flag in self.quality_exclusions),
            "interval_bands": [
                [band.label, band.lower_days, band.upper_days] for band in self.interval_bands
            ],
            "duplicate_rule_version": self.duplicate_rule_version,
            "engine_version": self.engine_version,
            "linkage_rule": LINKAGE_RULE,
        }

    def checksum(self) -> str:
        """SHA-256 of the canonical parameters; the identity of the analysis rule."""
        return hashlib.sha256(
            json.dumps(self.parameters(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def from_parameters(
        cls, parameters: Mapping[str, Any], *, name: str = "", version: int | None = None
    ) -> FrozenRecurrenceDefinition:
        """Rebuild a definition from :meth:`parameters`; validates as it goes."""
        try:
            bands_raw = parameters.get("interval_bands")
            bands = (
                tuple(
                    IntervalBand(str(label), int(lower), None if upper is None else int(upper))
                    for label, lower, upper in bands_raw
                )
                if bands_raw
                else DEFAULT_INTERVAL_BANDS
            )
            return cls(
                minimum_gap_days=int(parameters["minimum_gap_days"]),
                maximum_window_days=int(parameters["maximum_window_days"]),
                minimum_positive_encounters=int(parameters.get("minimum_positive_encounters", 2)),
                same_day_policy=SameDayPolicy(parameters.get("same_day_policy", "exclude")),
                permitted_positive_methods=frozenset(
                    str(m) for m in parameters.get("permitted_positive_methods") or ()
                ),
                require_linked_treatment=bool(parameters.get("require_linked_treatment", False)),
                quality_exclusions=frozenset(
                    QualityFlag(str(f)) for f in parameters.get("quality_exclusions") or ()
                ),
                interval_bands=bands,
                duplicate_rule_version=str(
                    parameters.get("duplicate_rule_version", DUPLICATE_RULE_VERSION)
                ),
                engine_version=str(parameters.get("engine_version", ENGINE_VERSION)),
                name=name,
                version=version,
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DefinitionError):
                raise
            raise DefinitionError(f"Malformed recurrence definition: {error}") from error


#: Exploratory starting preset from the enhancement brief's illustrative
#: examples. Not an approved programme method.
EXPLORATORY_PRESET = FrozenRecurrenceDefinition(
    minimum_gap_days=7,
    maximum_window_days=28,
    minimum_positive_encounters=2,
    same_day_policy=SameDayPolicy.EXCLUDE,
    name="Exploratory starting preset (not approved)",
)


@dataclass(frozen=True, slots=True)
class RecurrenceCeilings:
    """Resource ceilings. Supplied from settings by the service layer."""

    maximum_window_days: int = 366
    maximum_positive_encounters: int = 12
    maximum_period_days: int = 366
    maximum_filter_values: int = 500

    def check(
        self,
        definition: FrozenRecurrenceDefinition,
        query: FrozenAnalysisQuery,
        *,
        filter_values: int = 0,
    ) -> None:
        if definition.maximum_window_days > self.maximum_window_days:
            raise DefinitionError(
                f"The maximum window exceeds the configured ceiling of "
                f"{self.maximum_window_days} days"
            )
        if definition.minimum_positive_encounters > self.maximum_positive_encounters:
            raise DefinitionError(
                f"The positive-encounter requirement exceeds the configured ceiling of "
                f"{self.maximum_positive_encounters}"
            )
        if (query.period_end - query.period_start).days + 1 > self.maximum_period_days:
            raise DefinitionError(
                f"The reporting period exceeds the configured ceiling of "
                f"{self.maximum_period_days} days"
            )
        if filter_values > self.maximum_filter_values:
            raise DefinitionError(
                f"The filters name more than the configured ceiling of "
                f"{self.maximum_filter_values} values"
            )


@dataclass(frozen=True, slots=True)
class FrozenAnalysisQuery:
    period_start: date
    period_end: date
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        if self.period_end < self.period_start:
            raise DefinitionError("The reporting period ends before it starts")
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise DefinitionError(f"Unknown reporting timezone {self.timezone!r}") from error

    def lookback_start(self, definition: FrozenRecurrenceDefinition) -> date:
        return self.period_start - timedelta(days=definition.maximum_window_days)

    def contains(self, day: date) -> bool:
        return self.period_start <= day <= self.period_end


@dataclass(frozen=True, slots=True)
class EvidenceCoverage:
    """What the evidence actually covers. Absence outside it is unknown.

    ``facility_refs`` are the facilities whose evidence was retrieved or
    imported for the whole span; ``requested_facility_refs`` are the ones the
    analysis asked for. A requested facility that is not covered makes an
    absence of recurrence unreliable, because a chain could run through it.
    """

    earliest_date: date | None
    latest_date: date | None
    complete: bool
    stages: frozenset[str] = frozenset()
    facility_refs: frozenset[str] = frozenset()
    exclusions: tuple[str, ...] = ()
    requested_facility_refs: frozenset[str] = frozenset()

    def starts_by(self, day: date) -> bool:
        return self.earliest_date is not None and self.earliest_date <= day

    def ends_by(self, day: date) -> bool:
        return self.latest_date is not None and self.latest_date >= day

    @property
    def missing_facilities(self) -> frozenset[str]:
        return self.requested_facility_refs - self.facility_refs


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    group_id: str
    kind: DuplicateKind
    patient: PatientKey | None
    local_day: date | None
    members: tuple[str, ...]
    representative: str | None
    reasons: tuple[str, ...]
    #: ``(field, "match" | "differ" | "missing")`` in a fixed field order.
    compared_fields: tuple[tuple[str, str], ...]
    confidence: DuplicateConfidence
    rule_version: str
    unresolved: bool


@dataclass(frozen=True, slots=True)
class PositiveInterval:
    kind: Literal["adjacent", "anchor", "chain"]
    from_encounter: str
    to_encounter: str
    from_date: date
    to_date: date
    days: int


@dataclass(frozen=True, slots=True)
class EncounterDecision:
    """How one encounter in a patient's history was treated. Never hidden.

    ``local_day`` is the day the engine used: the positive test's own day for a
    confirmed positive, the encounter's day for context.
    """

    encounter_key: str
    local_day: date | None
    facility_ref: str
    confirmed_positive: bool
    counted: bool
    reason: str
    representative_of: str | None = None
    duplicate_group: str | None = None
    quality_flags: frozenset[QualityFlag] = frozenset()


@dataclass(frozen=True, slots=True)
class ChainTransition:
    """One linked positive-to-positive step inside a qualifying chain."""

    from_encounter: str
    to_encounter: str
    days: int
    #: Normalised medicine names linked to the earlier positive.
    prior_medications: tuple[str, ...]
    #: ``prescribed``, ``dispensed``, ``recorded``, ``not_linked`` or ``not_available``.
    prior_treatment_evidence: str


@dataclass(frozen=True, slots=True)
class PatientFinding:
    patient: PatientKey
    determination: Determination
    observed_positive_count: int
    eligible_positive_count: int
    chain: tuple[str, ...]
    chain_interval: PositiveInterval | None
    adjacent_intervals: tuple[PositiveInterval, ...]
    anchor_intervals: tuple[PositiveInterval, ...]
    transitions: tuple[ChainTransition, ...]
    final_encounter: str | None
    final_facility: str | None
    final_date: date | None
    in_period_positive_facilities: frozenset[str]
    latest_in_period_positive: date | None
    facilities: frozenset[str]
    decisions: tuple[EncounterDecision, ...]
    quality_flags: frozenset[QualityFlag]
    explanation: tuple[str, ...]
    sex: str
    age_group: str

    @property
    def chain_length(self) -> int:
        return len(self.chain)

    @property
    def cross_facility(self) -> bool:
        return len(self.facilities) > 1

    @property
    def in_denominator(self) -> bool:
        return self.determination is not Determination.NOT_IN_PERIOD

    @property
    def legacy_interval_days(self) -> int | None:
        """Compatibility value for the old bare ``interval_days`` field."""
        if self.chain_interval is not None:
            return self.chain_interval.days
        if self.adjacent_intervals:
            return self.adjacent_intervals[-1].days
        return None


@dataclass(frozen=True, slots=True)
class RecurrenceEvaluation:
    definition: FrozenRecurrenceDefinition
    query: FrozenAnalysisQuery
    coverage: EvidenceCoverage
    lookback_start: date
    coverage_status: Literal["complete", "partial"]
    coverage_notes: tuple[str, ...]
    patients: tuple[PatientFinding, ...]
    duplicate_groups: tuple[DuplicateGroup, ...]
    unlinked_positive_encounters: tuple[str, ...]
    engine_version: str
    #: Deterministic fingerprint of the evidence and its coverage.
    dataset_hash: str
    #: The retained dataset's immutable identifier, when it was persisted.
    dataset_id: str | None = None

    @property
    def qualifying(self) -> tuple[PatientFinding, ...]:
        return tuple(p for p in self.patients if p.determination is Determination.QUALIFIES)

    @property
    def denominator(self) -> tuple[PatientFinding, ...]:
        return tuple(p for p in self.patients if p.in_denominator)


# ---------------------------------------------------------------------------
# Dataset identity
# ---------------------------------------------------------------------------
def canonical_value(value: object) -> object:
    """A JSON-ready, order-independent rendering of contract values."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: canonical_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: str(item[0]))
        return {str(k): canonical_value(v) for k, v in ordered}
    if isinstance(value, set | frozenset):
        return sorted(json.dumps(canonical_value(v), sort_keys=True) for v in value)
    if isinstance(value, list | tuple):
        return [canonical_value(v) for v in value]
    return value


def evidence_fingerprint(
    encounters: Iterable[CanonicalEncounter], coverage: EvidenceCoverage
) -> str:
    """The identity of a canonical evidence dataset: content plus coverage.

    Two datasets with the same coverage descriptor but different patient
    evidence have different fingerprints, which is what lets a comparison
    refuse to set one against the other.
    """
    material = {
        "contract": EVIDENCE_CONTRACT_VERSION,
        "coverage": canonical_value(coverage),
        "encounters": [
            canonical_value(encounter)
            for encounter in sorted(encounters, key=lambda e: (e.namespace, e.encounter_key))
        ],
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Contract-level revision helpers, shared by every adapter
# ---------------------------------------------------------------------------
T = TypeVar("T")


def group_id(kind: DuplicateKind, members: Iterable[str]) -> str:
    """A stable identifier for a group, independent of input order."""
    digest = hashlib.sha256(
        (DUPLICATE_RULE_VERSION + "|" + kind.value + "|" + "|".join(sorted(members))).encode()
    ).hexdigest()
    return f"dup-{digest[:20]}"


def latest_revisions(
    items: Sequence[T],
    *,
    identity: Callable[[T], Hashable],
    revision: Callable[[T], tuple[str, ...]],
    member_label: Callable[[T], str],
) -> tuple[list[T], list[DuplicateGroup]]:
    """Keep one revision of each source record; record every repeat.

    The latest revision wins, compared by ``revision`` and then by input
    position so that ties resolve the same way on every run. A group is
    produced only when a record really arrived more than once.
    """
    by_identity: dict[Hashable, list[tuple[int, T]]] = defaultdict(list)
    for position, item in enumerate(items):
        by_identity[identity(item)].append((position, item))
    kept: list[T] = []
    groups: list[DuplicateGroup] = []
    for occurrences in by_identity.values():
        chosen = max(occurrences, key=lambda pair: (revision(pair[1]), pair[0]))[1]
        kept.append(chosen)
        if len(occurrences) > 1:
            label = member_label(chosen)
            members = tuple(f"{label}#{index}" for index in range(len(occurrences)))
            groups.append(
                DuplicateGroup(
                    group_id=group_id(DuplicateKind.SOURCE_REVISION, members),
                    kind=DuplicateKind.SOURCE_REVISION,
                    patient=None,
                    local_day=None,
                    members=members,
                    representative=label,
                    reasons=(
                        f"The same source record arrived {len(occurrences)} times; "
                        "the latest revision is kept.",
                    ),
                    compared_fields=(),
                    confidence=DuplicateConfidence.EXACT,
                    rule_version=DUPLICATE_RULE_VERSION,
                    unresolved=False,
                )
            )
    return kept, groups


__all__ = [
    "ALIAS_SCHEME",
    "DEFAULT_INTERVAL_BANDS",
    "DEFAULT_TIMEZONE",
    "DUPLICATE_RULE_VERSION",
    "ENGINE_VERSION",
    "EVIDENCE_CONTRACT_VERSION",
    "EXPLORATORY_PRESET",
    "INTERPRETATION_LIMIT",
    "LINKAGE_RULE",
    "OUTSIDE_BANDS_LABEL",
    "UNRESOLVED_OUTCOMES",
    "AdaptedEvidence",
    "Availability",
    "CanonicalEncounter",
    "ChainTransition",
    "ClinicalTest",
    "DatePrecision",
    "DefinitionError",
    "Determination",
    "DuplicateConfidence",
    "DuplicateGroup",
    "DuplicateKind",
    "EncounterDecision",
    "EvidenceCoverage",
    "FrozenAnalysisQuery",
    "FrozenRecurrenceDefinition",
    "IntervalBand",
    "LabOutcome",
    "Medication",
    "MedicationEvidence",
    "PatientFinding",
    "PatientKey",
    "PositiveInterval",
    "QualityFlag",
    "RecurrenceCeilings",
    "RecurrenceEvaluation",
    "SameDayPolicy",
    "SourceRecord",
    "canonical_value",
    "evidence_fingerprint",
    "group_id",
    "latest_revisions",
    "patient_display_alias",
    "validate_bands",
]
