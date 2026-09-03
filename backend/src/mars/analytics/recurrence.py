"""Turning episode evidence into recurrence measures.

Everything computed here is a count of *observed patterns*. The module's
vocabulary is deliberately flat - "repeat positive", "return interval",
"patients with more than one episode" - because the interesting words are the
ones routine data cannot support.

**Nothing here is treatment failure, recrudescence, reinfection or
resistance.** Those require knowing whether the parasite persisted, whether the
patient took the drug, and what the parasite's genotype was. An e-register knows
none of them. A repeat positive is a reason to look.

**Interval bands are governed.** MARS records actual return intervals in days;
what counts as an early or late return is a clinical parameter the programme
approves. With no approved bands, the engine reports every count it can and
marks the band breakdown unavailable rather than inventing cut points.

**Facility and residence are computed separately and never merged.** A patient
may attend a clinic outside their own district.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.enums import (
    IndicatorValueStatus,
    LifecycleStatus,
    PeriodGrain,
    RecurrenceMeasure,
    RecurrenceScopeKind,
)
from mars.domain.episode import EpisodeBuild, EpisodeCandidate, EpisodeMember
from mars.domain.governance import ConfigurationKey, ConfigurationVersion
from mars.domain.recurrence import RecurrenceResult

logger = get_logger(__name__)

#: Bumped when a change here could alter a count for unchanged episodes.
ENGINE_VERSION = "1.0.0"

#: The governed configuration key supplying interval bands. Registered by
#: governance; **not** shipped with values, because what counts as an early
#: return is a clinical judgement.
INTERVAL_BANDS_KEY = "recurrence_interval_bands_days"

#: The statement that travels with every repeat-positive figure. Held once so
#: it cannot drift between measures - and so it cannot be quietly dropped from
#: one of them.
INTERPRETATION_LIMIT = (
    "A repeat positive result is a pattern requiring investigation. Routine "
    "data cannot distinguish recrudescence from reinfection, cannot establish "
    "that a patient took the drug prescribed, and cannot identify parasite "
    "genotype or molecular markers. This figure is not evidence of treatment "
    "failure, and not evidence of antimalarial resistance."
)


@dataclass(frozen=True, slots=True)
class IntervalBand:
    """One governed return-interval band."""

    label: str
    lower_days: int
    #: Exclusive upper bound. ``None`` means open-ended.
    upper_days: int | None

    def contains(self, days: int) -> bool:
        if days < self.lower_days:
            return False
        return self.upper_days is None or days < self.upper_days


@dataclass(slots=True)
class RecurrenceReport:
    """What one recurrence run produced."""

    build_id: uuid.UUID | None = None
    results_written: int = 0
    results_unchanged: int = 0
    scopes: int = 0
    eligible_patients: int = 0
    repeat_positive_patients: int = 0
    excluded_unlinked_encounters: int = 0
    bands_available: bool = False
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "build_id": str(self.build_id) if self.build_id else None,
            "results_written": self.results_written,
            "results_unchanged": self.results_unchanged,
            "scopes": self.scopes,
            "eligible_patients": self.eligible_patients,
            "repeat_positive_patients": self.repeat_positive_patients,
            "excluded_unlinked_encounters": self.excluded_unlinked_encounters,
            "bands_available": self.bands_available,
            "notes": self.notes,
        }


@dataclass(slots=True)
class ScopeCounts:
    """Everything one scope contributes, before it becomes rows."""

    eligible_patients: set[uuid.UUID] = field(default_factory=set)
    repeat_positive_patients: set[uuid.UUID] = field(default_factory=set)
    patients_with_multiple_episodes: set[uuid.UUID] = field(default_factory=set)
    repeat_positive_episodes: int = 0
    positives_without_treatment: int = 0
    intervals: list[int] = field(default_factory=list)


class RecurrenceEngine:
    """Computes recurrence measures from an episode build."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Governed bands ----------------------------------------------------
    def interval_bands(self) -> tuple[list[IntervalBand], uuid.UUID | None]:
        """The approved interval bands, or an empty list.

        An empty list is the expected state before a programme approves bands.
        Callers report the counts they can and mark the band breakdown
        unavailable; they never fall back to cut points chosen here.
        """
        version = (
            self._session.execute(
                select(ConfigurationVersion)
                .join(
                    ConfigurationKey,
                    ConfigurationKey.id == ConfigurationVersion.configuration_key_id,
                )
                .where(
                    ConfigurationKey.key == INTERVAL_BANDS_KEY,
                    ConfigurationVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if version is None:
            return [], None

        raw = (version.value or {}).get("bands")
        if not isinstance(raw, list) or not raw:
            logger.warning("recurrence_bands_malformed", configuration_version=str(version.id))
            return [], None

        bands: list[IntervalBand] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label")
            lower = entry.get("lower_days")
            upper = entry.get("upper_days")
            if not isinstance(label, str) or not isinstance(lower, int):
                continue
            if upper is not None and not isinstance(upper, int):
                continue
            bands.append(IntervalBand(label=label, lower_days=lower, upper_days=upper))

        if not bands:
            return [], None
        return bands, version.id

    # -- Computation -------------------------------------------------------
    def compute(
        self,
        build: EpisodeBuild,
        *,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
        boundary_version_id: uuid.UUID | None = None,
    ) -> RecurrenceReport:
        """Compute every recurrence measure for one episode build."""
        report = RecurrenceReport(build_id=build.id)
        bands, configuration_version_id = self.interval_bands()
        report.bands_available = bool(bands)
        if not bands:
            report.notes = (
                f"No approved {INTERVAL_BANDS_KEY}. Counts are reported; the "
                "interval-band breakdown is unavailable. What counts as an "
                "early or late return is a clinical judgement the programme "
                "approves, and MARS does not choose cut points."
            )

        episodes = (
            self._session.execute(
                select(EpisodeCandidate).where(EpisodeCandidate.episode_build_id == build.id)
            )
            .scalars()
            .all()
        )
        intervals_by_episode = self._repeat_intervals(build.id)

        # Facility of care and residence are accumulated into separate scope
        # maps. A single map keyed by "place" would merge a clinic with a
        # village, and the two questions are not the same question.
        scopes: dict[tuple[RecurrenceScopeKind, uuid.UUID], ScopeCounts] = defaultdict(ScopeCounts)
        residence_unresolved = 0

        for episode in episodes:
            targets: list[tuple[RecurrenceScopeKind, uuid.UUID]] = []
            if episode.index_facility_id is not None:
                targets.append((RecurrenceScopeKind.FACILITY, episode.index_facility_id))
            if episode.residence_district_id is not None:
                targets.append(
                    (RecurrenceScopeKind.RESIDENCE_DISTRICT, episode.residence_district_id)
                )
            else:
                residence_unresolved += 1
            if episode.residence_subcounty_id is not None:
                targets.append(
                    (RecurrenceScopeKind.RESIDENCE_SUBCOUNTY, episode.residence_subcounty_id)
                )

            for key in targets:
                counts = scopes[key]
                if episode.positive_encounter_count >= 1:
                    counts.eligible_patients.add(episode.patient_reference_id)
                if episode.has_repeat_positive:
                    counts.repeat_positive_patients.add(episode.patient_reference_id)
                    counts.repeat_positive_episodes += 1
                    counts.intervals.extend(intervals_by_episode.get(episode.id, []))
                    if episode.treated_encounter_count < episode.positive_encounter_count:
                        counts.positives_without_treatment += (
                            episode.positive_encounter_count - episode.treated_encounter_count
                        )
                if episode.episode_number > 1:
                    counts.patients_with_multiple_episodes.add(episode.patient_reference_id)

        report.scopes = len(scopes)
        report.excluded_unlinked_encounters = build.encounters_unlinked
        report.eligible_patients = len(
            {
                patient
                for key, counts in scopes.items()
                if key[0] is RecurrenceScopeKind.FACILITY
                for patient in counts.eligible_patients
            }
        )
        report.repeat_positive_patients = len(
            {
                patient
                for key, counts in scopes.items()
                if key[0] is RecurrenceScopeKind.FACILITY
                for patient in counts.repeat_positive_patients
            }
        )

        fingerprint = self._fingerprint(build, scopes, bands)
        cutoff = build.source_cutoff

        for (scope_kind, scope_id), counts in sorted(
            scopes.items(), key=lambda item: (item[0][0].value, str(item[0][1]))
        ):
            self._write_scope(
                build=build,
                scope_kind=scope_kind,
                scope_id=scope_id,
                counts=counts,
                bands=bands,
                report=report,
                fingerprint=fingerprint,
                cutoff=cutoff,
                period_grain=period_grain,
                boundary_version_id=boundary_version_id,
                configuration_version_id=configuration_version_id,
                residence_unresolved=residence_unresolved,
            )

        self._session.flush()
        logger.info("recurrence_computed", **report.as_dict())
        return report

    def _repeat_intervals(self, build_id: uuid.UUID) -> dict[uuid.UUID, list[int]]:
        """Days between consecutive positives, per episode.

        Only intervals *between positive results* count. An interval measured
        from a negative follow-up visit is a return interval for something
        else, and mixing them would make the distribution unreadable.
        """
        rows = self._session.execute(
            select(
                EpisodeMember.episode_candidate_id,
                EpisodeMember.days_since_previous,
            )
            .join(
                EpisodeCandidate,
                EpisodeCandidate.id == EpisodeMember.episode_candidate_id,
            )
            .where(
                EpisodeCandidate.episode_build_id == build_id,
                EpisodeMember.test_result == "positive",
                EpisodeMember.days_since_previous.is_not(None),
            )
        ).all()

        intervals: dict[uuid.UUID, list[int]] = defaultdict(list)
        for episode_id, days in rows:
            intervals[episode_id].append(int(days))
        return intervals

    def _write_scope(
        self,
        *,
        build: EpisodeBuild,
        scope_kind: RecurrenceScopeKind,
        scope_id: uuid.UUID,
        counts: ScopeCounts,
        bands: list[IntervalBand],
        report: RecurrenceReport,
        fingerprint: str,
        cutoff: datetime,
        period_grain: PeriodGrain,
        boundary_version_id: uuid.UUID | None,
        configuration_version_id: uuid.UUID | None,
        residence_unresolved: int,
    ) -> None:
        eligible = len(counts.eligible_patients)
        repeat = len(counts.repeat_positive_patients)

        measures: list[tuple[RecurrenceMeasure, int | None, int | None, str | None]] = [
            (RecurrenceMeasure.REPEAT_POSITIVE_PATIENTS, repeat, None, None),
            (
                RecurrenceMeasure.REPEAT_POSITIVE_EPISODES,
                counts.repeat_positive_episodes,
                None,
                None,
            ),
            (
                RecurrenceMeasure.PATIENTS_WITH_MULTIPLE_EPISODES,
                len(counts.patients_with_multiple_episodes),
                None,
                None,
            ),
            (RecurrenceMeasure.REPEAT_POSITIVE_PROPORTION, repeat, eligible, None),
        ]

        if bands:
            for band in bands:
                measures.append(
                    (
                        RecurrenceMeasure.INTERVAL_BAND_COUNT,
                        sum(1 for days in counts.intervals if band.contains(days)),
                        None,
                        band.label,
                    )
                )

        for measure, numerator, denominator, band_label in measures:
            value, status = _value_for(measure, numerator, denominator)
            self._materialise(
                build=build,
                measure=measure,
                scope_kind=scope_kind,
                scope_id=scope_id,
                band_label=band_label,
                numerator=numerator,
                denominator=denominator,
                value=value,
                status=status,
                counts=counts,
                eligible=eligible,
                report=report,
                fingerprint=fingerprint,
                cutoff=cutoff,
                period_grain=period_grain,
                boundary_version_id=boundary_version_id,
                configuration_version_id=configuration_version_id,
                residence_unresolved=residence_unresolved,
            )

    def _materialise(
        self,
        *,
        build: EpisodeBuild,
        measure: RecurrenceMeasure,
        scope_kind: RecurrenceScopeKind,
        scope_id: uuid.UUID,
        band_label: str | None,
        numerator: int | None,
        denominator: int | None,
        value: Decimal | None,
        status: IndicatorValueStatus,
        counts: ScopeCounts,
        eligible: int,
        report: RecurrenceReport,
        fingerprint: str,
        cutoff: datetime,
        period_grain: PeriodGrain,
        boundary_version_id: uuid.UUID | None,
        configuration_version_id: uuid.UUID | None,
        residence_unresolved: int,
    ) -> None:
        existing = self._session.execute(
            select(RecurrenceResult).where(
                RecurrenceResult.episode_build_id == build.id,
                RecurrenceResult.measure == measure,
                RecurrenceResult.scope_kind == scope_kind,
                RecurrenceResult.scope_id == scope_id,
                RecurrenceResult.period_start == build.period_start,
                RecurrenceResult.interval_band.is_(band_label)
                if band_label is None
                else RecurrenceResult.interval_band == band_label,
                RecurrenceResult.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            report.results_unchanged += 1
            return

        self._session.add(
            RecurrenceResult(
                episode_build_id=build.id,
                measure=measure,
                scope_kind=scope_kind,
                scope_id=scope_id,
                period_start=build.period_start,
                period_end=build.period_end,
                period_grain=period_grain,
                interval_band=band_label,
                numerator=numerator,
                denominator=denominator,
                value=value,
                value_status=status,
                eligible_patients=eligible,
                excluded_unlinked_encounters=build.encounters_unlinked,
                positives_without_treatment_record=counts.positives_without_treatment,
                residence_unresolved_episodes=residence_unresolved,
                input_fingerprint=fingerprint,
                source_cutoff=cutoff,
                episode_rule_version_id=build.rule_version_id,
                configuration_version_id=configuration_version_id,
                boundary_version_id=boundary_version_id,
                engine_version=ENGINE_VERSION,
                computed_at=datetime.now(UTC),
                interpretation_context={
                    # Carried on every row. A figure that reaches a report
                    # without it is a figure someone will over-read.
                    "interpretation_limit": INTERPRETATION_LIMIT,
                    "scope_meaning": (
                        "Facility of care"
                        if scope_kind is RecurrenceScopeKind.FACILITY
                        else "Patient residence as recorded"
                    ),
                },
            )
        )
        report.results_written += 1

    @staticmethod
    def _fingerprint(
        build: EpisodeBuild,
        scopes: dict[tuple[RecurrenceScopeKind, uuid.UUID], ScopeCounts],
        bands: list[IntervalBand],
    ) -> str:
        """Identity of the evidence and the rules this run used.

        Includes the bands: the same episodes banded differently are a
        different result, and overwriting one with the other would change what
        a district was shown without any record of it.
        """
        material = {
            "build": str(build.id),
            "build_fingerprint": build.input_fingerprint,
            "bands": [[b.label, b.lower_days, b.upper_days] for b in bands],
            "scopes": sorted(
                [
                    kind.value,
                    str(scope_id),
                    len(counts.eligible_patients),
                    len(counts.repeat_positive_patients),
                    counts.repeat_positive_episodes,
                    sorted(counts.intervals),
                ]
                for (kind, scope_id), counts in scopes.items()
            ),
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()


def _value_for(
    measure: RecurrenceMeasure, numerator: int | None, denominator: int | None
) -> tuple[Decimal | None, IndicatorValueStatus]:
    """The stored value, or an explicit statement that there is none.

    A proportion with no eligible population is **unavailable**, never zero. A
    facility with no linked positive patients has no recurrence proportion, and
    reporting 0.0 would put a real-looking "no recurrence here" into every
    district summary.
    """
    if measure is RecurrenceMeasure.REPEAT_POSITIVE_PROPORTION:
        if not denominator or numerator is None:
            return None, IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR
        return (
            (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")),
            IndicatorValueStatus.AVAILABLE,
        )

    if numerator is None:
        return None, IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA
    return Decimal(numerator), IndicatorValueStatus.AVAILABLE


def latest_build(session: Session, period_start: date, period_end: date) -> EpisodeBuild | None:
    """The most recent completed build for a period.

    Completed only. A ``not_configured`` build has no episodes, and computing
    recurrence from it would report a confident zero for every facility.
    """
    return (
        session.execute(
            select(EpisodeBuild)
            .where(
                EpisodeBuild.period_start == period_start,
                EpisodeBuild.period_end == period_end,
                EpisodeBuild.build_status == "completed",
            )
            .order_by(EpisodeBuild.started_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


__all__ = [
    "ENGINE_VERSION",
    "INTERPRETATION_LIMIT",
    "INTERVAL_BANDS_KEY",
    "IntervalBand",
    "RecurrenceEngine",
    "RecurrenceReport",
    "ScopeCounts",
    "latest_build",
]
