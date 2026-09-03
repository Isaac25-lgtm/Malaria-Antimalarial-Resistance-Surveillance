"""The temporal anomaly and persistence engine.

Compares one period's observation against the baseline built for it, and
records how long each departure has been running.

Three things this module refuses to do.

**It does not decide how large a departure has to be.** That number decides how
many districts get an alert on Monday morning, and it is a programme decision.
With no approved detection rule the run is stored as ``not_configured`` and
names the parameters that are missing.

**It does not call an unevaluated observation normal.** A missing baseline, a
case count below the approved minimum, an absent case count, a method that
cannot be applied to the baseline available - each gets its own outcome. If
"not flagged" could also mean "could not tell", a quiet map would carry two
opposite meanings in one colour.

**It does not fall back.** A robust z-score against a baseline with no spread
is not silently replaced with a relative deviation; that would apply a rule
nobody approved. The observation is recorded as ``not_evaluated_method_inapplicable``.

Persistence separates a one-period spike from a six-month rise, because those
need different responses and presenting them identically is how alert fatigue
starts. The consecutive count is arithmetic and always recorded; calling a run
*sustained* requires an approved persistence rule.

A flagged period says an observation departed from its own history by more than
an approved amount. It does not say why. It never says resistance.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from mars.analytics.baseline import preceding_periods
from mars.core.logging import get_logger
from mars.domain.anomaly import AnomalyBuild, AnomalyPersistence, TemporalAnomalyResult
from mars.domain.baseline import BaselineBuild, BaselineResult
from mars.domain.enums import (
    AnomalyBuildStatus,
    AnomalyDetectionMethod,
    AnomalyDirection,
    AnomalyOutcome,
    BaselineSeriesKind,
    BaselineSufficiency,
    GeographyGrain,
    IndicatorValueStatus,
    LifecycleStatus,
    MethodKind,
    PeriodGrain,
)
from mars.domain.governance import MethodDefinition, MethodVersion
from mars.domain.indicator import IndicatorResult
from mars.domain.surveillance import TestingSurveillanceResult, TreatmentSurveillanceResult

logger = get_logger(__name__)

#: Bumped when a change here could alter an outcome for unchanged inputs.
ENGINE_VERSION = "1.0.0"

#: The governed rule the engine reads its threshold from. Registered by
#: governance; **not** shipped with values.
ANOMALY_RULE_CODE = "temporal_anomaly_rule"

#: Parameters an approved version must supply, named exactly as they appear in
#: ``missing_configuration``.
REQUIRED_PARAMETERS = ("detection_method", "deviation_threshold", "minimum_case_count")

#: Optional. Without it MARS counts consecutive flagged periods and declines to
#: call any run sustained.
PERSISTENCE_PARAMETER = "persistence_periods"

#: What every flagged result must carry, so a figure cannot reach a report
#: without the sentence that bounds it.
INTERPRETATION_LIMIT = (
    "A departure from this series' own history, larger than an approved "
    "threshold. It is a reason to look, not a finding. It does not establish a "
    "cause, and it is not evidence of treatment failure or antimalarial "
    "resistance."
)


@dataclass(frozen=True, slots=True)
class AnomalyRule:
    """The governed parameters one detection run applies."""

    method_version_id: uuid.UUID
    semantic_version: str
    method: AnomalyDetectionMethod
    deviation_threshold: Decimal
    minimum_case_count: int
    persistence_periods: int | None = None


@dataclass(slots=True)
class AnomalyReport:
    """What one detection run did."""

    build_id: uuid.UUID | None = None
    status: AnomalyBuildStatus = AnomalyBuildStatus.RUNNING
    observations_examined: int = 0
    flagged: int = 0
    not_flagged: int = 0
    not_evaluated: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    runs_opened: int = 0
    runs_extended: int = 0
    missing_configuration: list[str] = field(default_factory=list)
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "build_id": str(self.build_id) if self.build_id else None,
            "status": self.status.value,
            "observations_examined": self.observations_examined,
            "flagged": self.flagged,
            "not_flagged": self.not_flagged,
            "not_evaluated": self.not_evaluated,
            "outcomes": dict(sorted(self.outcomes.items())),
            "runs_opened": self.runs_opened,
            "runs_extended": self.runs_extended,
            "missing_configuration": sorted(self.missing_configuration),
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class Scope:
    """One series in one place."""

    series_key: str
    geography_grain: GeographyGrain
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None


@dataclass(slots=True)
class Observation:
    """What the source reported for the target period."""

    scope: Scope
    period_start: date
    period_end: date
    value: Decimal | None
    status: IndicatorValueStatus
    case_count: int | None
    computed_at: datetime


@dataclass(slots=True)
class Judgement:
    """The result of applying one rule to one observation."""

    outcome: AnomalyOutcome
    direction: AnomalyDirection | None = None
    expected: Decimal | None = None
    absolute: Decimal | None = None
    relative: Decimal | None = None
    score: Decimal | None = None
    note: str | None = None


def _fingerprint(**material: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {k: str(v) for k, v in sorted(material.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class AnomalyEngine:
    """Judges observations against baselines, under a governed rule."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Governance ---------------------------------------------------------
    def rule(self) -> tuple[AnomalyRule | None, list[str]]:
        """The approved detection rule, or ``None`` with what is missing."""
        row = (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition, MethodDefinition.id == MethodVersion.method_definition_id)
                .where(
                    MethodDefinition.code == ANOMALY_RULE_CODE,
                    MethodDefinition.kind == MethodKind.SIGNAL_RULE,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None, [f"method:{ANOMALY_RULE_CODE}", *REQUIRED_PARAMETERS]

        parameters = row.parameters or {}
        missing = [name for name in REQUIRED_PARAMETERS if parameters.get(name) is None]
        if missing:
            logger.warning(
                "anomaly_rule_incomplete", method_version=str(row.id), missing=sorted(missing)
            )
            return None, missing

        try:
            method = AnomalyDetectionMethod(parameters["detection_method"])
        except ValueError:
            # A method MARS has not implemented is reported as missing rather
            # than replaced, which would apply a rule nobody approved.
            return None, ["detection_method"]

        threshold = _decimal(parameters["deviation_threshold"])
        if threshold is None or threshold <= 0:
            return None, ["deviation_threshold"]

        minimum = int(parameters["minimum_case_count"])
        if minimum < 0:
            return None, ["minimum_case_count"]

        persistence = parameters.get(PERSISTENCE_PARAMETER)
        periods = int(persistence) if isinstance(persistence, int) and persistence >= 1 else None

        return (
            AnomalyRule(
                method_version_id=row.id,
                semantic_version=row.semantic_version,
                method=method,
                deviation_threshold=threshold,
                minimum_case_count=minimum,
                persistence_periods=periods,
            ),
            [],
        )

    # -- Reading ------------------------------------------------------------
    def _observation_query(
        self, series_kind: BaselineSeriesKind, period_start: date
    ) -> Select[tuple]:
        if series_kind is BaselineSeriesKind.INDICATOR:
            return select(
                IndicatorResult.indicator_code,
                IndicatorResult.geography_grain,
                IndicatorResult.geography_unit_id,
                IndicatorResult.facility_id,
                IndicatorResult.period_start,
                IndicatorResult.period_end,
                IndicatorResult.value,
                IndicatorResult.value_status,
                IndicatorResult.numerator,
                IndicatorResult.computed_at,
            ).where(IndicatorResult.period_start == period_start)
        if series_kind is BaselineSeriesKind.TESTING_MEASURE:
            return select(
                TestingSurveillanceResult.measure,
                TestingSurveillanceResult.geography_grain,
                TestingSurveillanceResult.geography_unit_id,
                TestingSurveillanceResult.facility_id,
                TestingSurveillanceResult.period_start,
                TestingSurveillanceResult.period_end,
                TestingSurveillanceResult.value,
                TestingSurveillanceResult.value_status,
                TestingSurveillanceResult.numerator,
                TestingSurveillanceResult.computed_at,
            ).where(TestingSurveillanceResult.period_start == period_start)
        return select(
            TreatmentSurveillanceResult.measure,
            TreatmentSurveillanceResult.geography_grain,
            TreatmentSurveillanceResult.geography_unit_id,
            TreatmentSurveillanceResult.facility_id,
            TreatmentSurveillanceResult.period_start,
            TreatmentSurveillanceResult.period_end,
            TreatmentSurveillanceResult.value,
            TreatmentSurveillanceResult.value_status,
            TreatmentSurveillanceResult.numerator,
            TreatmentSurveillanceResult.computed_at,
        ).where(TreatmentSurveillanceResult.period_start == period_start)

    def observations(
        self, series_kind: BaselineSeriesKind, period_start: date
    ) -> dict[Scope, Observation]:
        """The latest reported value per scope for the target period.

        Results elsewhere are immutable, so one period can hold several rows
        for the same scope. The latest is the one in force; judging a
        superseded figure would flag a number nobody is looking at.
        """
        rows = self._session.execute(self._observation_query(series_kind, period_start)).all()
        latest: dict[Scope, Observation] = {}
        for key, grain, unit_id, facility_id, start, end, value, status, numerator, at in rows:
            scope = Scope(
                series_key=key.value if hasattr(key, "value") else str(key),
                geography_grain=grain,
                geography_unit_id=unit_id,
                facility_id=facility_id,
            )
            seen = latest.get(scope)
            if seen is not None and seen.computed_at >= at:
                continue
            latest[scope] = Observation(
                scope=scope,
                period_start=start,
                period_end=end,
                value=_decimal(value),
                status=status,
                case_count=numerator,
                computed_at=at,
            )
        return latest

    def baselines(self, build: BaselineBuild) -> dict[Scope, BaselineResult]:
        rows = (
            self._session.execute(
                select(BaselineResult).where(BaselineResult.baseline_build_id == build.id)
            )
            .scalars()
            .all()
        )
        return {
            Scope(
                series_key=row.series_key,
                geography_grain=row.geography_grain,
                geography_unit_id=row.geography_unit_id,
                facility_id=row.facility_id,
            ): row
            for row in rows
        }

    # -- Judging ------------------------------------------------------------
    def judge(
        self, observation: Observation, baseline: BaselineResult | None, rule: AnomalyRule
    ) -> Judgement:
        """Apply one rule to one observation. Never guesses."""
        if observation.value is None or observation.status is not IndicatorValueStatus.AVAILABLE:
            return Judgement(
                outcome=AnomalyOutcome.NOT_EVALUATED_NO_OBSERVATION,
                note=(
                    "The source reported no usable value for this period, so "
                    "there is nothing to judge. This is not a statement that "
                    "the period was normal."
                ),
            )

        if baseline is None or baseline.sufficiency is not BaselineSufficiency.SUFFICIENT:
            return Judgement(
                outcome=AnomalyOutcome.NOT_EVALUATED_NO_BASELINE,
                note=(
                    "No baseline with sufficient history for this series here. "
                    "There is nothing to compare against, which is not the "
                    "same as nothing being unusual."
                ),
            )

        if observation.case_count is None:
            return Judgement(
                outcome=AnomalyOutcome.NOT_EVALUATED_COUNT_UNKNOWN,
                expected=_decimal(baseline.value),
                note=(
                    "The measure carries no case count, so the approved "
                    "minimum could not be checked. Different from being below "
                    "it."
                ),
            )
        if observation.case_count < rule.minimum_case_count:
            return Judgement(
                outcome=AnomalyOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT,
                expected=_decimal(baseline.value),
                note=(
                    f"{observation.case_count} cases is below the approved "
                    f"minimum of {rule.minimum_case_count}. A doubling of two "
                    "cases is arithmetic, not epidemiology."
                ),
            )

        expected = _decimal(baseline.value)
        if expected is None:
            # The schema forbids a sufficient baseline without a value, so this
            # is unreachable through the ordinary path. Handled rather than
            # asserted: a crash here would take out a whole detection run.
            return Judgement(
                outcome=AnomalyOutcome.NOT_EVALUATED_NO_BASELINE,
                note="The baseline reported sufficient history but carried no expected level.",
            )
        observed = observation.value
        absolute = observed - expected
        direction = (
            AnomalyDirection.INCREASE
            if absolute > 0
            else AnomalyDirection.DECREASE
            if absolute < 0
            else AnomalyDirection.UNCHANGED
        )
        relative = (absolute / expected) if expected != 0 else None
        dispersion = _decimal(baseline.dispersion_value)
        # A baseline with no spread, or a perfectly flat one, has no scale to
        # measure a departure against. Null rather than a division that would
        # make every departure look infinite.
        score = absolute / dispersion if dispersion is not None and dispersion != 0 else None

        exceeded, inapplicable = self._apply(rule, baseline, observed, absolute, relative, score)
        if inapplicable is not None:
            return Judgement(
                outcome=AnomalyOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE,
                direction=direction,
                expected=expected,
                absolute=absolute,
                relative=relative,
                score=score,
                note=inapplicable,
            )

        return Judgement(
            outcome=AnomalyOutcome.FLAGGED if exceeded else AnomalyOutcome.NOT_FLAGGED,
            direction=direction,
            expected=expected,
            absolute=absolute,
            relative=relative,
            score=score,
        )

    def _apply(
        self,
        rule: AnomalyRule,
        baseline: BaselineResult,
        observed: Decimal,
        absolute: Decimal,
        relative: Decimal | None,
        score: Decimal | None,
    ) -> tuple[bool, str | None]:
        """Whether the departure exceeds the threshold, or why it cannot say."""
        if rule.method is AnomalyDetectionMethod.ROBUST_Z_SCORE:
            if score is None:
                return False, (
                    "The approved method is a robust z-score, and this "
                    "baseline has no spread to measure against - a single "
                    "historical period, or a perfectly flat one. MARS does not "
                    "substitute another method."
                )
            return abs(score) >= rule.deviation_threshold, None

        if rule.method is AnomalyDetectionMethod.RELATIVE_DEVIATION:
            if relative is None:
                return False, (
                    "The approved method is a relative deviation and the "
                    "expected level is zero, so there is no proportion to "
                    "take. MARS does not substitute another method."
                )
            return abs(relative) >= rule.deviation_threshold, None

        lower = _decimal(baseline.uncertainty_lower)
        upper = _decimal(baseline.uncertainty_upper)
        if lower is None or upper is None:
            return False, (
                "The approved method tests the baseline's uncertainty band, "
                "and this baseline has none - no uncertainty multiplier was "
                "approved when it was built."
            )
        return (observed < lower or observed > upper), None

    # -- Detection ----------------------------------------------------------
    def detect(
        self,
        period_start: date,
        period_end: date,
        *,
        series_kind: BaselineSeriesKind,
        baseline_build: BaselineBuild | None,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
    ) -> AnomalyReport:
        """Judge every observation in one period against its baseline."""
        started = datetime.now(UTC)
        rule, missing = self.rule()

        if rule is None:
            build = AnomalyBuild(
                build_status=AnomalyBuildStatus.NOT_CONFIGURED,
                series_kind=series_kind,
                period_start=period_start,
                period_end=period_end,
                period_grain=period_grain,
                baseline_build_id=baseline_build.id if baseline_build else None,
                missing_configuration={"parameters": sorted(missing)},
                engine_version=ENGINE_VERSION,
                started_at=started,
                finished_at=datetime.now(UTC),
                notes=(
                    "No approved temporal anomaly rule. How large a departure "
                    "has to be decides how many districts get an alert, and "
                    "that is a programme decision. Nothing was judged, which "
                    "is not a statement that nothing is unusual."
                ),
            )
            self._session.add(build)
            self._session.flush()
            report = AnomalyReport(
                build_id=build.id,
                status=AnomalyBuildStatus.NOT_CONFIGURED,
                missing_configuration=sorted(missing),
                notes=build.notes,
            )
            logger.info("anomaly_build_not_configured", **report.as_dict())
            return report

        build = AnomalyBuild(
            build_status=AnomalyBuildStatus.RUNNING,
            series_kind=series_kind,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            baseline_build_id=baseline_build.id if baseline_build else None,
            method_version_id=rule.method_version_id,
            detection_method=rule.method,
            deviation_threshold=rule.deviation_threshold,
            minimum_case_count=rule.minimum_case_count,
            persistence_periods=rule.persistence_periods,
            engine_version=ENGINE_VERSION,
            started_at=started,
        )
        self._session.add(build)
        self._session.flush()

        report = AnomalyReport(build_id=build.id, status=AnomalyBuildStatus.RUNNING)
        expectations = self.baselines(baseline_build) if baseline_build else {}

        for scope, observation in self.observations(series_kind, period_start).items():
            judgement = self.judge(observation, expectations.get(scope), rule)
            result = self._write_result(
                build=build,
                rule=rule,
                scope=scope,
                observation=observation,
                baseline=expectations.get(scope),
                judgement=judgement,
                series_kind=series_kind,
                period_grain=period_grain,
                report=report,
            )
            if judgement.outcome is AnomalyOutcome.FLAGGED:
                self._record_persistence(
                    result=result,
                    scope=scope,
                    rule=rule,
                    series_kind=series_kind,
                    period_start=period_start,
                    period_end=period_end,
                    period_grain=period_grain,
                    report=report,
                )

        build.build_status = AnomalyBuildStatus.COMPLETED
        build.observations_examined = report.observations_examined
        build.flagged = report.flagged
        build.not_flagged = report.not_flagged
        build.not_evaluated = report.not_evaluated
        build.finished_at = datetime.now(UTC)
        report.status = AnomalyBuildStatus.COMPLETED
        self._session.flush()
        logger.info("anomaly_build_finished", **report.as_dict())
        return report

    def _write_result(
        self,
        *,
        build: AnomalyBuild,
        rule: AnomalyRule,
        scope: Scope,
        observation: Observation,
        baseline: BaselineResult | None,
        judgement: Judgement,
        series_kind: BaselineSeriesKind,
        period_grain: PeriodGrain,
        report: AnomalyReport,
    ) -> TemporalAnomalyResult:
        report.observations_examined += 1
        report.outcomes[judgement.outcome.value] = (
            report.outcomes.get(judgement.outcome.value, 0) + 1
        )
        if judgement.outcome is AnomalyOutcome.FLAGGED:
            report.flagged += 1
        elif judgement.outcome is AnomalyOutcome.NOT_FLAGGED:
            report.not_flagged += 1
        else:
            report.not_evaluated += 1

        evaluated = judgement.outcome in (AnomalyOutcome.FLAGGED, AnomalyOutcome.NOT_FLAGGED)
        # A baseline is cited when its expected level was actually read. On a
        # row where none was, citing one would suggest a comparison happened.
        cited_baseline = baseline if judgement.expected is not None else None

        result = TemporalAnomalyResult(
            anomaly_build_id=build.id,
            baseline_result_id=cited_baseline.id if cited_baseline else None,
            method_version_id=rule.method_version_id,
            series_kind=series_kind,
            series_key=scope.series_key,
            geography_grain=scope.geography_grain,
            geography_unit_id=scope.geography_unit_id,
            facility_id=scope.facility_id,
            period_start=observation.period_start,
            period_end=observation.period_end,
            period_grain=period_grain,
            outcome=judgement.outcome,
            direction=judgement.direction,
            detection_method=rule.method,
            observed_value=observation.value,
            expected_value=judgement.expected,
            absolute_deviation=judgement.absolute,
            relative_deviation=judgement.relative,
            deviation_score=judgement.score,
            uncertainty_lower=_decimal(baseline.uncertainty_lower) if baseline else None,
            uncertainty_upper=_decimal(baseline.uncertainty_upper) if baseline else None,
            deviation_threshold=rule.deviation_threshold,
            case_count=observation.case_count,
            minimum_case_count=rule.minimum_case_count,
            history_periods_used=baseline.history_periods_used if baseline else None,
            input_fingerprint=_fingerprint(
                series=scope.series_key,
                kind=series_kind.value,
                facility=scope.facility_id,
                unit=scope.geography_unit_id,
                period=observation.period_start,
                observed=observation.value,
                expected=judgement.expected,
                method=rule.method.value,
                threshold=rule.deviation_threshold,
                rule_version=rule.method_version_id,
                baseline=cited_baseline.id if cited_baseline else None,
            ),
            source_cutoff=observation.computed_at,
            engine_version=ENGINE_VERSION,
            computed_at=datetime.now(UTC),
            quality_context={
                "interpretation_limit": INTERPRETATION_LIMIT,
                "evaluated": evaluated,
            },
            notes=judgement.note,
        )
        self._session.add(result)
        self._session.flush()
        return result

    def _record_persistence(
        self,
        *,
        result: TemporalAnomalyResult,
        scope: Scope,
        rule: AnomalyRule,
        series_kind: BaselineSeriesKind,
        period_start: date,
        period_end: date,
        period_grain: PeriodGrain,
        report: AnomalyReport,
    ) -> None:
        """Extend the run this flag continues, or open a new one.

        A persistence row is a running tally over immutable results, not an
        analytical claim that gets rewritten. Extending it only ever moves the
        end forward and adds a result to the list; nothing already recorded
        changes meaning.
        """
        previous_end = preceding_periods(period_start, period_end, period_grain, 1)[0][1]
        existing = (
            self._session.execute(
                select(AnomalyPersistence)
                .where(
                    AnomalyPersistence.series_kind == series_kind,
                    AnomalyPersistence.series_key == scope.series_key,
                    AnomalyPersistence.geography_grain == scope.geography_grain,
                    AnomalyPersistence.facility_id == scope.facility_id,
                    AnomalyPersistence.geography_unit_id == scope.geography_unit_id,
                    AnomalyPersistence.last_period_end.in_([previous_end, period_end]),
                )
                .order_by(AnomalyPersistence.last_period_end.desc())
            )
            .scalars()
            .first()
        )

        if existing is not None and existing.last_period_end == period_end:
            # This period is already part of the run. A re-run must not make a
            # spike look sustained.
            return

        now = datetime.now(UTC)
        if existing is not None:
            ids = list((existing.contributing_result_ids or {}).get("results", []))
            ids.append(str(result.id))
            existing.last_period_end = period_end
            existing.consecutive_periods += 1
            existing.contributing_result_ids = {"results": ids}
            existing.last_detected_at = now
            existing.persistence_periods = rule.persistence_periods
            existing.method_version_id = (
                rule.method_version_id if rule.persistence_periods else None
            )
            existing.is_sustained = (
                existing.consecutive_periods >= rule.persistence_periods
                if rule.persistence_periods
                else None
            )
            report.runs_extended += 1
            self._session.flush()
            return

        self._session.add(
            AnomalyPersistence(
                series_kind=series_kind,
                series_key=scope.series_key,
                geography_grain=scope.geography_grain,
                geography_unit_id=scope.geography_unit_id,
                facility_id=scope.facility_id,
                period_grain=period_grain,
                first_period_start=period_start,
                last_period_end=period_end,
                consecutive_periods=1,
                # A judgement, not a count. Null unless a programme has said
                # how many periods make a run sustained.
                is_sustained=(rule.persistence_periods <= 1) if rule.persistence_periods else None,
                persistence_periods=rule.persistence_periods,
                method_version_id=rule.method_version_id if rule.persistence_periods else None,
                contributing_result_ids={"results": [str(result.id)]},
                first_detected_at=now,
                last_detected_at=now,
                engine_version=ENGINE_VERSION,
                notes=(
                    "One period so far. Whether that is a spike or the start of "
                    "something is not knowable yet."
                ),
            )
        )
        report.runs_opened += 1
        self._session.flush()


def latest_detection(
    session: Session,
    period_start: date,
    period_end: date,
    series_kind: BaselineSeriesKind,
) -> AnomalyBuild | None:
    """The most recent **completed** detection run for a period.

    A ``not_configured`` run judged nothing, and offering it as a detection
    would let a caller read "no flags" as "nothing unusual".
    """
    return (
        session.execute(
            select(AnomalyBuild)
            .where(
                AnomalyBuild.period_start == period_start,
                AnomalyBuild.period_end == period_end,
                AnomalyBuild.series_kind == series_kind,
                AnomalyBuild.build_status == AnomalyBuildStatus.COMPLETED,
            )
            .order_by(AnomalyBuild.started_at.desc())
        )
        .scalars()
        .first()
    )


__all__ = [
    "ANOMALY_RULE_CODE",
    "ENGINE_VERSION",
    "INTERPRETATION_LIMIT",
    "PERSISTENCE_PARAMETER",
    "REQUIRED_PARAMETERS",
    "AnomalyEngine",
    "AnomalyReport",
    "AnomalyRule",
    "Judgement",
    "Observation",
    "Scope",
    "latest_detection",
]
