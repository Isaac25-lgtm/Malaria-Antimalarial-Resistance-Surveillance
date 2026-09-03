"""Building malaria episode candidates from pseudonymously linked encounters.

The engine groups one patient's encounters into candidate episodes using a
**governed rule**, and refuses to run without one.

That refusal is the most important behaviour in the module. Whether two positive
results forty days apart are one illness or two is a clinical judgement that
depends on the drug, the setting and the programme's own guidance. MARS does not
have a defensible universal answer, so it does not invent one: with no approved
rule, a build is recorded as ``not_configured`` and produces no episodes.

What the engine does record, once a rule exists, is deliberately factual:
visits, actual intervals in days, test results, whether treatment was written
down, and what it could not establish. The uncertainty travels with the episode
rather than being rediscovered by whoever reads it.

**No direct identifier is read.** Grouping is by ``patient_reference_id``. This
module imports nothing from ``mars.identity``, and a module-boundary test
enforces it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.encounter import OpdEncounter, OpdEncounterPrescription, OpdEncounterTest
from mars.domain.enums import (
    EpisodeBuildStatus,
    EpisodeEncounterRole,
    EpisodeStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    MethodKind,
)
from mars.domain.episode import EpisodeBuild, EpisodeCandidate, EpisodeMember
from mars.domain.governance import MethodDefinition, MethodVersion

logger = get_logger(__name__)

#: Bumped when a change here could alter which encounters group together.
ENGINE_VERSION = "1.0.0"

#: The governed method code the engine reads its window from. Registered by
#: governance; **not** shipped with a value, because no defensible universal
#: episode window exists.
EPISODE_RULE_CODE = "malaria_episode_rule"

#: The parameter the rule must supply. Named so an operator reading a
#: ``not_configured`` build knows exactly what is missing.
REQUIRED_PARAMETER = "episode_window_days"


class EpisodeRuleNotConfiguredError(RuntimeError):
    """No approved episode rule exists.

    Raised by callers that need a rule rather than a report. The build path
    records ``not_configured`` instead, because "the programme has not approved
    a window" is a governance fact worth storing, not an exception to swallow.
    """


@dataclass(frozen=True, slots=True)
class EpisodeRule:
    """The governed parameters an episode build applies."""

    version_id: uuid.UUID
    window_days: int
    semantic_version: str

    def __post_init__(self) -> None:
        if self.window_days < 1:
            raise ValueError("an episode window must be at least one day")


@dataclass(slots=True)
class EncounterFact:
    """One encounter as the engine sees it.

    Deliberately narrow. It carries a pseudonymous reference and clinical
    facts, and there is nowhere in this structure for a name to live.
    """

    encounter_id: uuid.UUID
    #: Optional, and that is the point. An encounter with no usable
    #: identifier cannot join an episode; its absence is a limit on what
    #: MARS can see, counted rather than patched by guessing.
    patient_reference_id: uuid.UUID | None
    encounter_date: date
    facility_id: uuid.UUID | None
    residence_district_id: uuid.UUID | None
    residence_subcounty_id: uuid.UUID | None
    attendance_type: str | None
    test_method: str | None
    test_result: str | None
    antimalarial_recorded: bool
    updated_at: datetime

    @property
    def is_positive(self) -> bool:
        return self.test_result == MalariaTestResult.POSITIVE.value

    @property
    def is_tested(self) -> bool:
        return self.test_method not in (None, MalariaTestMethod.NOT_DONE.value)


@dataclass(slots=True)
class BuildReport:
    """What one build did."""

    build_id: uuid.UUID | None = None
    status: EpisodeBuildStatus = EpisodeBuildStatus.RUNNING
    encounters_considered: int = 0
    encounters_unlinked: int = 0
    patients_considered: int = 0
    episodes_created: int = 0
    repeat_positive_episodes: int = 0
    open_at_period_end: int = 0
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "build_id": str(self.build_id) if self.build_id else None,
            "status": self.status.value,
            "encounters_considered": self.encounters_considered,
            "encounters_unlinked": self.encounters_unlinked,
            "patients_considered": self.patients_considered,
            "episodes_created": self.episodes_created,
            "repeat_positive_episodes": self.repeat_positive_episodes,
            "open_at_period_end": self.open_at_period_end,
            "notes": self.notes,
        }


class EpisodeEngine:
    """Groups encounters into candidate episodes under a governed rule."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Rule ---------------------------------------------------------------
    def active_rule(self) -> EpisodeRule | None:
        """The approved episode rule, or ``None``.

        ``None`` is the expected state for a deployment whose programme has not
        yet approved a window, and callers must treat it as "cannot build"
        rather than substituting a default. A default here would be a clinical
        parameter invented by an engineer.
        """
        row = (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition, MethodDefinition.id == MethodVersion.method_definition_id)
                .where(
                    MethodDefinition.code == EPISODE_RULE_CODE,
                    MethodDefinition.kind == MethodKind.EPISODE_RULE,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None

        parameters = row.parameters or {}
        window = parameters.get(REQUIRED_PARAMETER)
        if not isinstance(window, int) or window < 1:
            # An active rule whose parameter is missing or nonsensical is not a
            # usable rule. Treated as absent rather than repaired, because
            # repairing it would mean choosing the window.
            logger.warning(
                "episode_rule_parameter_invalid",
                method_version=str(row.id),
                parameter=REQUIRED_PARAMETER,
            )
            return None

        return EpisodeRule(
            version_id=row.id, window_days=window, semantic_version=row.semantic_version
        )

    # -- Reading ------------------------------------------------------------
    def read_encounters(self, period_start: date, period_end: date) -> list[EncounterFact]:
        """Every encounter in the period, with the facts the engine needs.

        One row per encounter. The test and prescription joins are aggregated
        rather than joined naively: an encounter with two test rows would
        otherwise appear twice and be counted as two visits.
        """
        positive = func.bool_or(OpdEncounterTest.result == MalariaTestResult.POSITIVE)
        tested = func.bool_or(OpdEncounterTest.method != MalariaTestMethod.NOT_DONE)
        any_method = func.min(cast(OpdEncounterTest.method, String(32)))

        rows = self._session.execute(
            select(
                OpdEncounter.id,
                OpdEncounter.patient_reference_id,
                OpdEncounter.encounter_date,
                OpdEncounter.facility_id,
                OpdEncounter.residence_district_id,
                OpdEncounter.residence_subcounty_id,
                OpdEncounter.attendance_type,
                OpdEncounter.updated_at,
                positive.label("has_positive"),
                tested.label("has_test"),
                any_method.label("method"),
            )
            .select_from(OpdEncounter)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                OpdEncounter.encounter_date >= period_start,
                OpdEncounter.encounter_date <= period_end,
            )
            .group_by(OpdEncounter.id)
            .order_by(OpdEncounter.encounter_date, OpdEncounter.id)
        ).all()

        treated_ids = set(
            self._session.execute(
                select(OpdEncounterPrescription.opd_encounter_id)
                .distinct()
                .where(OpdEncounterPrescription.drug_name_normalised.is_not(None))
            )
            .scalars()
            .all()
        )

        facts: list[EncounterFact] = []
        for row in rows:
            facts.append(
                EncounterFact(
                    encounter_id=row.id,
                    patient_reference_id=row.patient_reference_id,
                    encounter_date=row.encounter_date,
                    facility_id=row.facility_id,
                    residence_district_id=row.residence_district_id,
                    residence_subcounty_id=row.residence_subcounty_id,
                    attendance_type=row.attendance_type.value if row.attendance_type else None,
                    test_method=(
                        MalariaTestMethod.NOT_DONE.value if not row.has_test else str(row.method)
                    ),
                    test_result=(MalariaTestResult.POSITIVE.value if row.has_positive else None),
                    antimalarial_recorded=row.id in treated_ids,
                    updated_at=row.updated_at,
                )
            )
        return facts

    # -- Building -----------------------------------------------------------
    def build(self, period_start: date, period_end: date) -> BuildReport:
        """Build episode candidates for a period.

        Returns a report either way. A deployment with no approved rule gets a
        recorded ``not_configured`` build rather than an exception: an operator
        needs to see that the run happened and why it produced nothing.
        """
        report = BuildReport()
        facts = self.read_encounters(period_start, period_end)
        report.encounters_considered = len(facts)
        report.encounters_unlinked = sum(1 for f in facts if f.patient_reference_id is None)

        fingerprint = fingerprint_encounters(facts)
        cutoff = max((f.updated_at for f in facts), default=datetime.now(UTC))

        rule = self.active_rule()
        if rule is None:
            build = EpisodeBuild(
                rule_version_id=None,
                build_status=EpisodeBuildStatus.NOT_CONFIGURED,
                period_start=period_start,
                period_end=period_end,
                input_fingerprint=fingerprint,
                source_cutoff=cutoff,
                encounters_considered=report.encounters_considered,
                encounters_unlinked=report.encounters_unlinked,
                engine_version=ENGINE_VERSION,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                notes=(
                    f"No approved {EPISODE_RULE_CODE} supplying {REQUIRED_PARAMETER}. "
                    "MARS does not supply an episode window: whether two positive "
                    "results are one illness or two depends on the drug, the "
                    "setting and the programme's guidance, and no defensible "
                    "universal answer exists."
                ),
            )
            self._session.add(build)
            self._session.flush()

            report.build_id = build.id
            report.status = EpisodeBuildStatus.NOT_CONFIGURED
            report.notes = build.notes
            logger.info("episode_build_not_configured", **report.as_dict())
            return report

        existing = self._session.execute(
            select(EpisodeBuild).where(
                EpisodeBuild.rule_version_id == rule.version_id,
                EpisodeBuild.input_fingerprint == fingerprint,
                EpisodeBuild.period_start == period_start,
                EpisodeBuild.period_end == period_end,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Same rule, same evidence, same period. Rebuilding would produce
            # identical episodes with new ids, and anything referring to the
            # old ones would silently point at nothing.
            report.build_id = existing.id
            report.status = existing.build_status
            report.episodes_created = existing.episodes_created
            report.encounters_unlinked = existing.encounters_unlinked
            report.notes = "unchanged: this rule has already been applied to this evidence"
            return report

        build = EpisodeBuild(
            rule_version_id=rule.version_id,
            build_status=EpisodeBuildStatus.RUNNING,
            period_start=period_start,
            period_end=period_end,
            input_fingerprint=fingerprint,
            source_cutoff=cutoff,
            encounters_considered=report.encounters_considered,
            encounters_unlinked=report.encounters_unlinked,
            engine_version=ENGINE_VERSION,
            started_at=datetime.now(UTC),
        )
        self._session.add(build)
        self._session.flush()
        report.build_id = build.id

        by_patient: dict[uuid.UUID, list[EncounterFact]] = defaultdict(list)
        for fact in facts:
            if fact.patient_reference_id is None:
                continue
            by_patient[fact.patient_reference_id].append(fact)

        report.patients_considered = len(by_patient)

        for patient_id, patient_facts in by_patient.items():
            ordered = sorted(patient_facts, key=lambda f: (f.encounter_date, f.encounter_id))
            for number, group in enumerate(self._group(ordered, rule.window_days), start=1):
                episode = self._write_episode(build, patient_id, number, group, rule, period_end)
                report.episodes_created += 1
                if episode.has_repeat_positive:
                    report.repeat_positive_episodes += 1
                if episode.episode_status is EpisodeStatus.OPEN_AT_PERIOD_END:
                    report.open_at_period_end += 1

        build.build_status = EpisodeBuildStatus.COMPLETED
        build.episodes_created = report.episodes_created
        build.patients_considered = report.patients_considered
        build.finished_at = datetime.now(UTC)
        self._session.flush()

        report.status = EpisodeBuildStatus.COMPLETED
        logger.info("episode_build_finished", **report.as_dict())
        return report

    @staticmethod
    def _group(ordered: list[EncounterFact], window_days: int) -> list[list[EncounterFact]]:
        """Split one patient's encounters into episodes.

        A new episode starts when the gap since the **previous encounter**
        exceeds the window. Measured from the previous encounter rather than
        from the episode's first: an illness with weekly follow-ups is one
        episode, and measuring from the start would split it arbitrarily at the
        window boundary.
        """
        groups: list[list[EncounterFact]] = []
        current: list[EncounterFact] = []

        for fact in ordered:
            if not current:
                current = [fact]
                continue
            gap = (fact.encounter_date - current[-1].encounter_date).days
            if gap > window_days:
                groups.append(current)
                current = [fact]
            else:
                current.append(fact)

        if current:
            groups.append(current)
        return groups

    def _write_episode(
        self,
        build: EpisodeBuild,
        patient_id: uuid.UUID,
        number: int,
        group: list[EncounterFact],
        rule: EpisodeRule,
        period_end: date,
    ) -> EpisodeCandidate:
        first, last = group[0], group[-1]
        positives = sum(1 for f in group if f.is_positive)
        tested = sum(1 for f in group if f.is_tested)
        treated = sum(1 for f in group if f.antimalarial_recorded)

        uncertainty: dict[str, Any] = {}
        if positives >= 2 and treated < positives:
            # The ordinary explanation for a repeat positive is that the first
            # episode was never treated. Recorded so nobody has to assume.
            uncertainty["treatment_not_recorded_for_every_positive"] = (
                "Fewer antimalarial records than positive results. A repeat "
                "positive with no recorded treatment has an ordinary "
                "explanation, and this episode cannot rule it out."
            )
        if len({f.facility_id for f in group if f.facility_id}) > 1:
            uncertainty["multiple_facilities"] = (
                "Encounters at more than one facility. The interval is real; "
                "attributing the episode to a single facility is not."
            )
        if first.residence_district_id is None:
            uncertainty["residence_unresolved"] = (
                "Residence did not resolve, so this episode contributes to "
                "facility-based measures but not to residence-based ones."
            )
        if positives >= 2:
            uncertainty["interpretation_limit"] = (
                "Repeat positivity in routine data is a reason to investigate. "
                "It cannot distinguish recrudescence from reinfection, and it "
                "is not evidence of treatment failure or resistance."
            )

        # An episode whose window has not closed by the end of the period may
        # continue. Saying so beats presenting it as finished.
        status = EpisodeStatus.CANDIDATE
        if (period_end - last.encounter_date).days < rule.window_days:
            status = EpisodeStatus.OPEN_AT_PERIOD_END
        elif uncertainty:
            status = EpisodeStatus.QUALIFIED

        episode = EpisodeCandidate(
            episode_build_id=build.id,
            patient_reference_id=patient_id,
            episode_number=number,
            episode_status=status,
            first_encounter_date=first.encounter_date,
            last_encounter_date=last.encounter_date,
            span_days=(last.encounter_date - first.encounter_date).days,
            encounter_count=len(group),
            positive_encounter_count=positives,
            tested_encounter_count=tested,
            treated_encounter_count=treated,
            index_facility_id=first.facility_id,
            residence_district_id=first.residence_district_id,
            residence_subcounty_id=first.residence_subcounty_id,
            uncertainty=uncertainty or None,
        )
        self._session.add(episode)
        self._session.flush()

        previous: EncounterFact | None = None
        seen_positive = False
        for sequence, fact in enumerate(group, start=1):
            if sequence == 1:
                role = EpisodeEncounterRole.INDEX
            elif fact.is_positive and seen_positive:
                role = EpisodeEncounterRole.REPEAT_POSITIVE
            else:
                role = EpisodeEncounterRole.FOLLOW_UP
            if fact.is_positive:
                seen_positive = True

            self._session.add(
                EpisodeMember(
                    episode_candidate_id=episode.id,
                    opd_encounter_id=fact.encounter_id,
                    sequence=sequence,
                    member_role=role,
                    encounter_date=fact.encounter_date,
                    # Actual days. Never a band: bands are governed
                    # configuration, and an interval stored as a band cannot
                    # later be re-banded when the programme changes them.
                    days_since_previous=(
                        (fact.encounter_date - previous.encounter_date).days if previous else None
                    ),
                    test_method=fact.test_method,
                    test_result=fact.test_result,
                    attendance_type=fact.attendance_type,
                    antimalarial_recorded=fact.antimalarial_recorded,
                    facility_id=fact.facility_id,
                )
            )
            previous = fact

        self._session.flush()
        return episode


def fingerprint_encounters(facts: list[EncounterFact]) -> str:
    """A stable identity for the evidence a build read.

    Includes each encounter's ``updated_at``, so a corrected encounter changes
    the fingerprint and produces a new build rather than silently altering
    episodes a clinician has already read.
    """
    material = [
        [
            str(fact.encounter_id),
            str(fact.patient_reference_id),
            fact.encounter_date.isoformat(),
            fact.test_method,
            fact.test_result,
            fact.antimalarial_recorded,
            fact.updated_at.isoformat(),
        ]
        for fact in sorted(facts, key=lambda f: str(f.encounter_id))
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ENGINE_VERSION",
    "EPISODE_RULE_CODE",
    "REQUIRED_PARAMETER",
    "BuildReport",
    "EncounterFact",
    "EpisodeEngine",
    "EpisodeRule",
    "EpisodeRuleNotConfiguredError",
    "fingerprint_encounters",
]
