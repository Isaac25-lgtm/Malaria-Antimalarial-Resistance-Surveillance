"""The hotspot engine.

Blueprint 037: *a hotspot must have a method, not just a red colour.* A
definition names the metric, the geography, the time window, the baseline, the
threshold, the minimum case count, the data completeness required and the
persistence rule. Without an approved one, MARS produces no hotspots and says
which parameters are missing.

The expectation comes from the **area's own history**. A district compared
against the history of its facilities would be measured against a quantity
nobody reports; a district compared against last month would be measured
against the season. The area's own series, summarised under the approved
temporal baseline method, is the comparison that means something.

Two rules carried over from the temporal engine, because a map makes them
matter more, not less:

* "not a hotspot" means examined and found unremarkable. Everything MARS could
  not judge keeps its own outcome and its reason. A red-free map is worth
  nothing if it cannot say which areas were looked at.
* completeness is a gate, not a footnote. A district figure built from three of
  twenty facilities is not a district figure, and colouring it red or green
  both mislead.

A hotspot is an area worth visiting. It is not a diagnosis, not an outbreak
declaration, and never a statement about antimalarial resistance.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.analytics.baseline import (
    BaselineEngine,
    BaselineSpecification,
    preceding_periods,
    summarise_history,
)
from mars.core.logging import get_logger
from mars.domain.enums import (
    AnomalyDetectionMethod,
    BaselineSeriesKind,
    BaselineSufficiency,
    GeographyGrain,
    HotspotOutcome,
    IndicatorValueStatus,
    LifecycleStatus,
    MethodKind,
    PeriodGrain,
    SpatialAggregationBasis,
    SpatialRunStatus,
)
from mars.domain.governance import MethodDefinition, MethodVersion
from mars.domain.spatial import GeographicAggregationResult, HotspotResult, SpatialRun

logger = get_logger(__name__)

#: Bumped when a change here could alter an outcome for unchanged inputs.
ENGINE_VERSION = "1.0.0"

#: The governed hotspot definition. Registered by governance; **not** shipped
#: with values, because the threshold decides which districts turn red.
HOTSPOT_DEFINITION_CODE = "hotspot_definition"

#: Blueprint 037's definition, minus the parts supplied by the run itself
#: (metric, geography and time window are arguments, and the baseline is the
#: approved temporal baseline method).
REQUIRED_PARAMETERS = (
    "detection_method",
    "deviation_threshold",
    "minimum_case_count",
    "minimum_completeness",
)

#: Optional. Without it MARS counts consecutive periods and calls nothing
#: persistent.
PERSISTENCE_PARAMETER = "persistence_periods"

INTERPRETATION_LIMIT = (
    "An area whose figure departed from its own history by more than an "
    "approved threshold. It is an area worth visiting, not a diagnosis, not an "
    "outbreak declaration, and not evidence of antimalarial resistance."
)


@dataclass(frozen=True, slots=True)
class HotspotDefinition:
    """The governed definition one hotspot run applies."""

    method_version_id: uuid.UUID
    semantic_version: str
    method: AnomalyDetectionMethod
    deviation_threshold: Decimal
    minimum_case_count: int
    minimum_completeness: Decimal
    persistence_periods: int | None = None


@dataclass(slots=True)
class HotspotReport:
    """What one hotspot run did."""

    run_id: uuid.UUID | None = None
    status: SpatialRunStatus = SpatialRunStatus.RUNNING
    units_examined: int = 0
    hotspots: int = 0
    not_hotspots: int = 0
    not_evaluated: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    missing_configuration: list[str] = field(default_factory=list)
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id) if self.run_id else None,
            "status": self.status.value,
            "units_examined": self.units_examined,
            "hotspots": self.hotspots,
            "not_hotspots": self.not_hotspots,
            "not_evaluated": self.not_evaluated,
            "outcomes": dict(sorted(self.outcomes.items())),
            "missing_configuration": sorted(self.missing_configuration),
            "notes": self.notes,
        }


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


class HotspotEngine:
    """Evaluates administrative units against a governed hotspot definition."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._baseline = BaselineEngine(session)

    # -- Governance ---------------------------------------------------------
    def definition(self) -> tuple[HotspotDefinition | None, list[str]]:
        """The approved hotspot definition, or ``None`` with what is missing."""
        row = (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition, MethodDefinition.id == MethodVersion.method_definition_id)
                .where(
                    MethodDefinition.code == HOTSPOT_DEFINITION_CODE,
                    MethodDefinition.kind == MethodKind.SPATIAL_METHOD,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None, [f"method:{HOTSPOT_DEFINITION_CODE}", *REQUIRED_PARAMETERS]

        parameters = row.parameters or {}
        missing = [name for name in REQUIRED_PARAMETERS if parameters.get(name) is None]
        if missing:
            logger.warning(
                "hotspot_definition_incomplete",
                method_version=str(row.id),
                missing=sorted(missing),
            )
            return None, missing

        try:
            method = AnomalyDetectionMethod(parameters["detection_method"])
        except ValueError:
            return None, ["detection_method"]

        threshold = _decimal(parameters["deviation_threshold"])
        if threshold is None or threshold <= 0:
            return None, ["deviation_threshold"]

        minimum_cases = int(parameters["minimum_case_count"])
        if minimum_cases < 0:
            return None, ["minimum_case_count"]

        completeness = _decimal(parameters["minimum_completeness"])
        if completeness is None or not (Decimal(0) <= completeness <= Decimal(1)):
            return None, ["minimum_completeness"]

        persistence = parameters.get(PERSISTENCE_PARAMETER)
        periods = int(persistence) if isinstance(persistence, int) and persistence >= 1 else None

        return (
            HotspotDefinition(
                method_version_id=row.id,
                semantic_version=row.semantic_version,
                method=method,
                deviation_threshold=threshold,
                minimum_case_count=minimum_cases,
                minimum_completeness=completeness,
                persistence_periods=periods,
            ),
            [],
        )

    # -- Reading ------------------------------------------------------------
    def _aggregations(
        self,
        period_start: date,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
    ) -> dict[tuple[str, uuid.UUID], GeographicAggregationResult]:
        """The latest aggregation per series and unit for one period."""
        rows = (
            self._session.execute(
                select(GeographicAggregationResult).where(
                    GeographicAggregationResult.period_start == period_start,
                    GeographicAggregationResult.series_kind == series_kind,
                    GeographicAggregationResult.geography_grain == geography_grain,
                    GeographicAggregationResult.aggregation_basis == basis,
                )
            )
            .scalars()
            .all()
        )
        latest: dict[tuple[str, uuid.UUID], GeographicAggregationResult] = {}
        for row in rows:
            key = (row.series_key, row.geography_unit_id)
            seen = latest.get(key)
            if seen is None or row.computed_at > seen.computed_at:
                latest[key] = row
        return latest

    def _history(
        self,
        *,
        series_key: str,
        unit_id: uuid.UUID,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        periods: list[tuple[date, date]],
    ) -> list[Decimal]:
        """The area's own past values, latest row per period."""
        starts = [start for start, _ in periods]
        if not starts:
            return []
        rows = (
            self._session.execute(
                select(GeographicAggregationResult).where(
                    GeographicAggregationResult.series_key == series_key,
                    GeographicAggregationResult.geography_unit_id == unit_id,
                    GeographicAggregationResult.series_kind == series_kind,
                    GeographicAggregationResult.geography_grain == geography_grain,
                    GeographicAggregationResult.aggregation_basis == basis,
                    GeographicAggregationResult.period_start.in_(starts),
                    GeographicAggregationResult.value_status == IndicatorValueStatus.AVAILABLE,
                )
            )
            .scalars()
            .all()
        )
        latest: dict[date, GeographicAggregationResult] = {}
        for row in rows:
            seen = latest.get(row.period_start)
            if seen is None or row.computed_at > seen.computed_at:
                latest[row.period_start] = row
        values: list[Decimal] = []
        for start in starts:
            newest = latest.get(start)
            if newest is None or newest.value is None:
                continue
            value = _decimal(newest.value)
            if value is not None:
                values.append(value)
        return values

    # -- Running ------------------------------------------------------------
    def evaluate(
        self,
        period_start: date,
        period_end: date,
        *,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain = GeographyGrain.DISTRICT,
        basis: SpatialAggregationBasis = SpatialAggregationBasis.FACILITY_LOCATION,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
        boundary_version_id: uuid.UUID | None = None,
    ) -> HotspotReport:
        """Evaluate every area with an aggregation for this period."""
        started = datetime.now(UTC)
        definition, missing = self.definition()
        specification, baseline_missing = self._baseline.specification()

        if definition is None or specification is None:
            absent = sorted({*missing, *(f"baseline:{name}" for name in baseline_missing)})
            run = SpatialRun(
                run_kind="hotspot",
                run_status=SpatialRunStatus.NOT_CONFIGURED,
                series_kind=series_kind,
                aggregation_basis=basis,
                geography_grain=geography_grain,
                period_start=period_start,
                period_end=period_end,
                period_grain=period_grain,
                boundary_version_id=boundary_version_id,
                missing_configuration={"parameters": absent},
                engine_version=ENGINE_VERSION,
                started_at=started,
                finished_at=datetime.now(UTC),
                notes=(
                    "A hotspot must have a method, not just a red colour. "
                    "MARS has no approved definition, or no approved baseline "
                    "method to build the expectation from, so no area was "
                    "judged. That is a statement about configuration, not "
                    "about malaria."
                ),
            )
            self._session.add(run)
            self._session.flush()
            report = HotspotReport(
                run_id=run.id,
                status=SpatialRunStatus.NOT_CONFIGURED,
                missing_configuration=absent,
                notes=run.notes,
            )
            logger.info("hotspot_run_not_configured", **report.as_dict())
            return report

        run = SpatialRun(
            run_kind="hotspot",
            run_status=SpatialRunStatus.RUNNING,
            series_kind=series_kind,
            aggregation_basis=basis,
            geography_grain=geography_grain,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            boundary_version_id=boundary_version_id,
            method_version_id=definition.method_version_id,
            deviation_threshold=definition.deviation_threshold,
            minimum_case_count=definition.minimum_case_count,
            minimum_completeness=definition.minimum_completeness,
            persistence_periods=definition.persistence_periods,
            engine_version=ENGINE_VERSION,
            started_at=started,
        )
        self._session.add(run)
        self._session.flush()
        report = HotspotReport(run_id=run.id, status=SpatialRunStatus.RUNNING)

        periods = preceding_periods(
            period_start, period_end, period_grain, specification.history_periods
        )

        for (series_key, unit_id), aggregation in self._aggregations(
            period_start, series_kind, geography_grain, basis
        ).items():
            history = self._history(
                series_key=series_key,
                unit_id=unit_id,
                series_kind=series_kind,
                geography_grain=geography_grain,
                basis=basis,
                periods=periods,
            )
            self._evaluate_unit(
                run=run,
                report=report,
                definition=definition,
                specification=specification,
                aggregation=aggregation,
                history=history,
                history_available=len(periods),
                series_kind=series_kind,
                series_key=series_key,
                unit_id=unit_id,
                geography_grain=geography_grain,
                basis=basis,
                period_grain=period_grain,
            )

        run.run_status = SpatialRunStatus.COMPLETED
        run.units_examined = report.units_examined
        run.results_written = report.units_examined
        run.not_evaluated = report.not_evaluated
        run.finished_at = datetime.now(UTC)
        report.status = SpatialRunStatus.COMPLETED
        self._session.flush()
        logger.info("hotspot_run_finished", **report.as_dict())
        return report

    def _evaluate_unit(
        self,
        *,
        run: SpatialRun,
        report: HotspotReport,
        definition: HotspotDefinition,
        specification: BaselineSpecification,
        aggregation: GeographicAggregationResult,
        history: list[Decimal],
        history_available: int,
        series_kind: BaselineSeriesKind,
        series_key: str,
        unit_id: uuid.UUID,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        period_grain: PeriodGrain,
    ) -> None:
        report.units_examined += 1

        observed = _decimal(aggregation.value)
        completeness = _decimal(aggregation.reporting_completeness)
        case_count = aggregation.numerator

        outcome: HotspotOutcome
        expected: Decimal | None = None
        dispersion: Decimal | None = None
        note: str | None = None

        if observed is None or aggregation.value_status is not IndicatorValueStatus.AVAILABLE:
            outcome = HotspotOutcome.NOT_EVALUATED_NO_OBSERVATION
            note = (
                "This area has no usable figure for the period. Nothing to "
                "judge, which is not a statement that the area is quiet."
            )
        elif (
            completeness is not None
            and basis is SpatialAggregationBasis.FACILITY_LOCATION
            and completeness < definition.minimum_completeness
        ):
            outcome = HotspotOutcome.NOT_EVALUATED_INCOMPLETE_REPORTING
            note = (
                f"{aggregation.contributing_facilities} of "
                f"{aggregation.expected_facilities} facilities reported, below "
                f"the approved minimum completeness of "
                f"{definition.minimum_completeness}. A figure built from part "
                "of an area does not describe the area, and colouring it red "
                "or green would both mislead."
            )
        elif case_count is None or case_count < definition.minimum_case_count:
            outcome = HotspotOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT
            note = (
                f"{case_count if case_count is not None else 'No'} cases is "
                f"below the approved minimum of {definition.minimum_case_count}."
            )
        else:
            history_completeness = (
                Decimal(len(history)) / Decimal(history_available)
                if history_available
                else Decimal(0)
            )
            sufficiency, _status, expected, dispersion = summarise_history(
                specification, history, len(history), history_completeness
            )
            if sufficiency is not BaselineSufficiency.SUFFICIENT or expected is None:
                outcome = HotspotOutcome.NOT_EVALUATED_NO_BASELINE
                expected = None
                note = (
                    f"{len(history)} of {history_available} historical periods "
                    "carried a usable figure for this area, which is not enough "
                    "under the approved baseline method. There is nothing to "
                    "compare against, which is not the same as nothing being "
                    "unusual."
                )
            elif self._inapplicable(definition, expected, dispersion):
                # No fallback, for the reason the temporal engine gives: a
                # substituted method is a rule nobody approved, applied
                # invisibly to a real district.
                outcome = HotspotOutcome.NOT_EVALUATED_NO_BASELINE
                note = (
                    "The approved detection method cannot be applied to this "
                    "area's baseline - no spread, or an expected level of zero. "
                    "MARS does not substitute another method."
                )
                expected = None
            else:
                outcome = (
                    HotspotOutcome.HOTSPOT
                    if self._exceeds(definition, observed, expected, dispersion)
                    else HotspotOutcome.NOT_HOTSPOT
                )

        self._write(
            run=run,
            report=report,
            definition=definition,
            specification=specification,
            aggregation=aggregation,
            outcome=outcome,
            observed=observed,
            expected=expected,
            dispersion=dispersion,
            history_used=len(history),
            series_kind=series_kind,
            series_key=series_key,
            unit_id=unit_id,
            geography_grain=geography_grain,
            basis=basis,
            period_grain=period_grain,
            note=note,
        )

    def _inapplicable(
        self, definition: HotspotDefinition, expected: Decimal, dispersion: Decimal | None
    ) -> bool:
        if definition.method is AnomalyDetectionMethod.ROBUST_Z_SCORE:
            return dispersion is None or dispersion == 0
        if definition.method is AnomalyDetectionMethod.RELATIVE_DEVIATION:
            return expected == 0
        # A band test needs a baseline row with an approved multiplier, which a
        # summarised area history does not carry.
        return True

    def _exceeds(
        self,
        definition: HotspotDefinition,
        observed: Decimal,
        expected: Decimal,
        dispersion: Decimal | None,
    ) -> bool:
        if self._inapplicable(definition, expected, dispersion):
            return False
        absolute = observed - expected
        if definition.method is AnomalyDetectionMethod.ROBUST_Z_SCORE:
            if dispersion is None or dispersion == 0:
                return False
            return abs(absolute / dispersion) >= definition.deviation_threshold
        return abs(absolute / expected) >= definition.deviation_threshold

    def _write(
        self,
        *,
        run: SpatialRun,
        report: HotspotReport,
        definition: HotspotDefinition,
        specification: BaselineSpecification,
        aggregation: GeographicAggregationResult,
        outcome: HotspotOutcome,
        observed: Decimal | None,
        expected: Decimal | None,
        dispersion: Decimal | None,
        history_used: int,
        series_kind: BaselineSeriesKind,
        series_key: str,
        unit_id: uuid.UUID,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        period_grain: PeriodGrain,
        note: str | None,
    ) -> None:
        report.outcomes[outcome.value] = report.outcomes.get(outcome.value, 0) + 1
        if outcome is HotspotOutcome.HOTSPOT:
            report.hotspots += 1
        elif outcome is HotspotOutcome.NOT_HOTSPOT:
            report.not_hotspots += 1
        else:
            report.not_evaluated += 1

        absolute = observed - expected if observed is not None and expected is not None else None
        relative = (
            absolute / expected
            if absolute is not None and expected is not None and expected != 0
            else None
        )
        score = (
            absolute / dispersion
            if absolute is not None and dispersion is not None and dispersion != 0
            else None
        )

        consecutive, first_start = self._persistence(
            outcome=outcome,
            series_key=series_key,
            unit_id=unit_id,
            series_kind=series_kind,
            geography_grain=geography_grain,
            basis=basis,
            period_start=run.period_start,
            period_end=run.period_end,
            period_grain=period_grain,
        )

        self._session.add(
            HotspotResult(
                spatial_run_id=run.id,
                aggregation_result_id=aggregation.id,
                method_version_id=(definition.method_version_id if expected is not None else None),
                baseline_method_version_id=(
                    specification.method_version_id if expected is not None else None
                ),
                series_kind=series_kind,
                series_key=series_key,
                geography_grain=geography_grain,
                geography_unit_id=unit_id,
                aggregation_basis=basis,
                period_start=run.period_start,
                period_end=run.period_end,
                period_grain=period_grain,
                outcome=outcome,
                observed_value=observed,
                expected_value=expected,
                absolute_deviation=absolute,
                relative_deviation=relative,
                deviation_score=score,
                deviation_threshold=definition.deviation_threshold,
                case_count=aggregation.numerator,
                minimum_case_count=definition.minimum_case_count,
                reporting_completeness=aggregation.reporting_completeness,
                minimum_completeness=definition.minimum_completeness,
                contributing_facilities=aggregation.contributing_facilities,
                expected_facilities=aggregation.expected_facilities,
                history_periods_used=history_used,
                consecutive_periods=consecutive,
                first_detected_period_start=first_start,
                last_detected_period_end=(
                    run.period_end if outcome is HotspotOutcome.HOTSPOT else None
                ),
                is_persistent=(
                    (consecutive >= definition.persistence_periods)
                    if definition.persistence_periods and outcome is HotspotOutcome.HOTSPOT
                    else None
                ),
                persistence_periods=definition.persistence_periods,
                input_fingerprint=_fingerprint(
                    series=series_key,
                    kind=series_kind.value,
                    unit=unit_id,
                    basis=basis.value,
                    period=run.period_start,
                    observed=observed,
                    expected=expected,
                    method=definition.method.value,
                    threshold=definition.deviation_threshold,
                    definition_version=definition.method_version_id,
                ),
                source_cutoff=aggregation.source_cutoff,
                engine_version=ENGINE_VERSION,
                computed_at=datetime.now(UTC),
                quality_context={
                    "interpretation_limit": INTERPRETATION_LIMIT,
                    "examined": outcome in (HotspotOutcome.HOTSPOT, HotspotOutcome.NOT_HOTSPOT),
                    "basis": basis.value,
                },
                notes=note,
            )
        )

    def _persistence(
        self,
        *,
        outcome: HotspotOutcome,
        series_key: str,
        unit_id: uuid.UUID,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        period_start: date,
        period_end: date,
        period_grain: PeriodGrain,
    ) -> tuple[int, date | None]:
        """Consecutive periods, read from the previous period's row.

        Computed rather than tallied. A stored counter would have to be
        rewritten every period, and rewriting an analytical row is how a past
        claim quietly changes meaning.
        """
        if outcome is not HotspotOutcome.HOTSPOT:
            return 0, None

        previous_start = preceding_periods(period_start, period_end, period_grain, 1)[0][0]
        previous = (
            self._session.execute(
                select(HotspotResult)
                .where(
                    HotspotResult.series_key == series_key,
                    HotspotResult.geography_unit_id == unit_id,
                    HotspotResult.series_kind == series_kind,
                    HotspotResult.geography_grain == geography_grain,
                    HotspotResult.aggregation_basis == basis,
                    HotspotResult.period_start == previous_start,
                    HotspotResult.outcome == HotspotOutcome.HOTSPOT,
                )
                .order_by(HotspotResult.computed_at.desc())
            )
            .scalars()
            .first()
        )
        if previous is None:
            return 1, period_start
        return previous.consecutive_periods + 1, (
            previous.first_detected_period_start or previous.period_start
        )


__all__ = [
    "ENGINE_VERSION",
    "HOTSPOT_DEFINITION_CODE",
    "INTERPRETATION_LIMIT",
    "PERSISTENCE_PARAMETER",
    "REQUIRED_PARAMETERS",
    "HotspotDefinition",
    "HotspotEngine",
    "HotspotReport",
]
