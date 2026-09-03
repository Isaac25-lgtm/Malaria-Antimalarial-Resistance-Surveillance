"""The historical baseline engine.

Answers one question: *what does this series usually look like here?* Nothing
else in MARS can call something unusual until that has an answer.

The engine refuses to answer it without governed approval. What counts as
normal - how far back to look, how much of that history must be present, which
summary to use - is a surveillance decision with real consequences: a short
window makes a slow rise invisible, a long one makes a genuine change take a
season to surface. MARS implements the methods and declines to choose among
them. With no approved method version a build is recorded as ``not_configured``
and names the parameters that are missing.

Three implemented methods:

``historical_median``
    Median of the most recent comparable periods. Robust to one bad month.

``historical_mean``
    Mean of the same. Sensitive to outliers, which is sometimes what a
    programme wants.

``seasonal_period_of_year_median``
    Median of the *same period of the year* across previous years. Malaria in
    Uganda is seasonal; comparing March against February flags the season.

Sufficiency is enforced, not advised. A series with fewer usable periods than
the approved minimum gets a row saying so and **no** expected value. An
expectation computed from two periods is worse than none, because a district
can act on it.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.baseline import BaselineBuild, BaselineResult
from mars.domain.enums import (
    BaselineBuildStatus,
    BaselineMethod,
    BaselineSeriesKind,
    BaselineSufficiency,
    DispersionMeasure,
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

#: Bumped when a change here could alter an expected value for unchanged input.
ENGINE_VERSION = "1.0.0"

#: The governed method the engine reads its window from. Registered by
#: governance; **not** shipped with values.
BASELINE_METHOD_CODE = "historical_baseline"

#: Parameters an approved version must supply. Named exactly as they appear in
#: ``missing_configuration``, so an operator reading a refused build can act on
#: it without consulting this file.
REQUIRED_PARAMETERS = (
    "baseline_method",
    "history_periods",
    "minimum_history_periods",
    "minimum_completeness",
)

#: Optional. Without it a baseline has a centre and no uncertainty band.
UNCERTAINTY_PARAMETER = "uncertainty_multiplier"

#: Which dispersion belongs with which centre. A median summarised by a
#: standard deviation would report a robust centre with a fragile spread.
DISPERSION_FOR_METHOD: dict[BaselineMethod, DispersionMeasure] = {
    BaselineMethod.HISTORICAL_MEDIAN: DispersionMeasure.MEDIAN_ABSOLUTE_DEVIATION,
    BaselineMethod.SEASONAL_PERIOD_OF_YEAR_MEDIAN: DispersionMeasure.MEDIAN_ABSOLUTE_DEVIATION,
    BaselineMethod.HISTORICAL_MEAN: DispersionMeasure.STANDARD_DEVIATION,
}


class BaselineNotConfiguredError(RuntimeError):
    """No approved temporal baseline method exists.

    For callers that need a specification rather than a report. The build path
    records ``not_configured`` instead: "the programme has not decided what
    normal means" is a governance fact worth storing.
    """


@dataclass(frozen=True, slots=True)
class BaselineSpecification:
    """The governed parameters one build applies."""

    method_version_id: uuid.UUID
    semantic_version: str
    method: BaselineMethod
    history_periods: int
    minimum_history_periods: int
    minimum_completeness: Decimal
    uncertainty_multiplier: Decimal | None = None

    @property
    def dispersion_measure(self) -> DispersionMeasure:
        return DISPERSION_FOR_METHOD[self.method]


@dataclass(slots=True)
class BaselineReport:
    """What one build did."""

    build_id: uuid.UUID | None = None
    status: BaselineBuildStatus = BaselineBuildStatus.RUNNING
    series_evaluated: int = 0
    results_written: int = 0
    sufficient: int = 0
    insufficient_history: int = 0
    insufficient_completeness: int = 0
    no_history: int = 0
    missing_configuration: list[str] = field(default_factory=list)
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "build_id": str(self.build_id) if self.build_id else None,
            "status": self.status.value,
            "series_evaluated": self.series_evaluated,
            "results_written": self.results_written,
            "sufficient": self.sufficient,
            "insufficient_history": self.insufficient_history,
            "insufficient_completeness": self.insufficient_completeness,
            "no_history": self.no_history,
            "missing_configuration": sorted(self.missing_configuration),
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class SeriesScope:
    """One series in one place - the unit a baseline is computed for."""

    series_key: str
    geography_grain: GeographyGrain
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None


@dataclass(slots=True)
class HistoryPoint:
    period_start: date
    period_end: date
    value: Decimal | None
    status: IndicatorValueStatus


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------
def _month_start(day: date, months_back: int) -> date:
    total = day.year * 12 + (day.month - 1) - months_back
    return date(total // 12, total % 12 + 1, 1)


def _month_end(start: date) -> date:
    return _month_start(start, -1) - timedelta(days=1)


def preceding_periods(
    period_start: date, period_end: date, grain: PeriodGrain, count: int
) -> list[tuple[date, date]]:
    """The ``count`` comparable periods immediately before the target.

    Comparable means the same grain. A monthly baseline walks back calendar
    months rather than 30-day blocks, because a calendar month is what the
    source reports and a 30-day block would straddle two of them.
    """
    periods: list[tuple[date, date]] = []
    if grain is PeriodGrain.MONTH:
        for back in range(1, count + 1):
            start = _month_start(period_start, back)
            periods.append((start, _month_end(start)))
    elif grain is PeriodGrain.EPIDEMIOLOGICAL_WEEK:
        for back in range(1, count + 1):
            start = period_start - timedelta(days=7 * back)
            periods.append((start, start + timedelta(days=6)))
    else:
        for back in range(1, count + 1):
            start = period_start - timedelta(days=back)
            periods.append((start, start))
    return periods


def seasonal_periods(
    period_start: date, period_end: date, grain: PeriodGrain, years: int
) -> tuple[list[tuple[date, date]], list[dict[str, object]]]:
    """The same period of the year in each of the previous ``years`` years.

    Returns the periods it could form and the years it could not, with a
    reason. ISO week 53 does not exist in every year, and skipping such a year
    silently would quietly shorten the history behind an expectation.
    """
    periods: list[tuple[date, date]] = []
    skipped: list[dict[str, object]] = []

    if grain is PeriodGrain.MONTH:
        for back in range(1, years + 1):
            start = date(period_start.year - back, period_start.month, 1)
            periods.append((start, _month_end(start)))
        return periods, skipped

    if grain is PeriodGrain.EPIDEMIOLOGICAL_WEEK:
        iso_year, iso_week, _ = period_start.isocalendar()
        for back in range(1, years + 1):
            try:
                start = date.fromisocalendar(iso_year - back, iso_week, 1)
            except ValueError:
                # That year had no week 53. Recorded rather than dropped.
                skipped.append(
                    {"iso_year": iso_year - back, "iso_week": iso_week, "reason": "iso_week_absent"}
                )
                continue
            periods.append((start, start + timedelta(days=6)))
        return periods, skipped

    for back in range(1, years + 1):
        try:
            start = date(period_start.year - back, period_start.month, period_start.day)
        except ValueError:
            # 29 February in a common year.
            skipped.append({"year": period_start.year - back, "reason": "date_absent_in_year"})
            continue
        periods.append((start, start))
    return periods, skipped


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class BaselineEngine:
    """Builds expected levels from history, under a governed method."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Governance ---------------------------------------------------------
    def specification(self) -> tuple[BaselineSpecification | None, list[str]]:
        """The approved baseline method, or ``None`` with what is missing.

        ``None`` is the expected state for a deployment whose programme has not
        approved a method. Callers must treat it as "cannot compute" rather
        than substituting a window, because the window decides what counts as
        normal.
        """
        row = (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition, MethodDefinition.id == MethodVersion.method_definition_id)
                .where(
                    MethodDefinition.code == BASELINE_METHOD_CODE,
                    MethodDefinition.kind == MethodKind.TEMPORAL_BASELINE,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None, [f"method:{BASELINE_METHOD_CODE}", *REQUIRED_PARAMETERS]

        parameters = row.parameters or {}
        missing = [name for name in REQUIRED_PARAMETERS if parameters.get(name) is None]
        if missing:
            logger.warning(
                "baseline_method_incomplete",
                method_version=str(row.id),
                missing=sorted(missing),
            )
            return None, missing

        try:
            method = BaselineMethod(parameters["baseline_method"])
        except ValueError:
            # An active version naming a method MARS has not implemented is not
            # usable. Reported as missing rather than replaced with a default,
            # which would silently apply a method nobody approved.
            return None, ["baseline_method"]

        history_periods = int(parameters["history_periods"])
        minimum_history = int(parameters["minimum_history_periods"])
        completeness = Decimal(str(parameters["minimum_completeness"]))

        invalid: list[str] = []
        if history_periods < 1:
            invalid.append("history_periods")
        if minimum_history < 1 or minimum_history > history_periods:
            invalid.append("minimum_history_periods")
        if not (Decimal(0) <= completeness <= Decimal(1)):
            invalid.append("minimum_completeness")
        if invalid:
            logger.warning(
                "baseline_method_parameters_invalid",
                method_version=str(row.id),
                parameters=sorted(invalid),
            )
            return None, invalid

        multiplier = parameters.get(UNCERTAINTY_PARAMETER)
        band = Decimal(str(multiplier)) if multiplier is not None else None
        if band is not None and band <= 0:
            band = None

        return (
            BaselineSpecification(
                method_version_id=row.id,
                semantic_version=row.semantic_version,
                method=method,
                history_periods=history_periods,
                minimum_history_periods=minimum_history,
                minimum_completeness=completeness,
                uncertainty_multiplier=band,
            ),
            [],
        )

    # -- Reading ------------------------------------------------------------
    def _source_query(self, series_kind: BaselineSeriesKind) -> Select[tuple]:
        """One shape for three source tables.

        Each yields the same six columns - series key, scope, period, value,
        status - so the arithmetic below never has to know which table it came
        from, and adding a fourth series kind does not touch it.
        """
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
                IndicatorResult.computed_at,
            )
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
                TestingSurveillanceResult.computed_at,
            )
        return select(
            TreatmentSurveillanceResult.measure,
            TreatmentSurveillanceResult.geography_grain,
            TreatmentSurveillanceResult.geography_unit_id,
            TreatmentSurveillanceResult.facility_id,
            TreatmentSurveillanceResult.period_start,
            TreatmentSurveillanceResult.period_end,
            TreatmentSurveillanceResult.value,
            TreatmentSurveillanceResult.value_status,
            TreatmentSurveillanceResult.computed_at,
        )

    def _period_filter(self, series_kind: BaselineSeriesKind, starts: list[date]) -> Select[tuple]:
        query = self._source_query(series_kind)
        if series_kind is BaselineSeriesKind.INDICATOR:
            column = IndicatorResult.period_start
        elif series_kind is BaselineSeriesKind.TESTING_MEASURE:
            column = TestingSurveillanceResult.period_start
        else:
            column = TreatmentSurveillanceResult.period_start
        return query.where(column.in_(starts))

    def read_history(
        self, series_kind: BaselineSeriesKind, periods: list[tuple[date, date]]
    ) -> dict[SeriesScope, dict[date, HistoryPoint]]:
        """The most recent value for every series, scope and historical period.

        Results are immutable and a recomputation writes a new row beside the
        old one, so the latest ``computed_at`` per period is the one in force.
        Taking all of them would let a superseded figure vote twice.
        """
        if not periods:
            return {}
        rows = self._session.execute(
            self._period_filter(series_kind, [start for start, _ in periods])
        ).all()

        history: dict[SeriesScope, dict[date, HistoryPoint]] = {}
        latest: dict[tuple[SeriesScope, date], datetime] = {}
        for key, grain, unit_id, facility_id, start, end, value, status, computed_at in rows:
            scope = SeriesScope(
                series_key=key.value if hasattr(key, "value") else str(key),
                geography_grain=grain,
                geography_unit_id=unit_id,
                facility_id=facility_id,
            )
            seen = latest.get((scope, start))
            if seen is not None and seen >= computed_at:
                continue
            latest[(scope, start)] = computed_at
            history.setdefault(scope, {})[start] = HistoryPoint(
                period_start=start,
                period_end=end,
                value=Decimal(str(value)) if value is not None else None,
                status=status,
            )
        return history

    # -- Building -----------------------------------------------------------
    def build(
        self,
        target_period_start: date,
        target_period_end: date,
        *,
        series_kind: BaselineSeriesKind,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
    ) -> BaselineReport:
        """Compute expected levels for one target period."""
        started = datetime.now(UTC)
        specification, missing = self.specification()

        if specification is None:
            build = BaselineBuild(
                build_status=BaselineBuildStatus.NOT_CONFIGURED,
                series_kind=series_kind,
                target_period_start=target_period_start,
                target_period_end=target_period_end,
                period_grain=period_grain,
                missing_configuration={"parameters": sorted(missing)},
                engine_version=ENGINE_VERSION,
                started_at=started,
                finished_at=datetime.now(UTC),
                notes=(
                    "No approved temporal baseline method. MARS implements "
                    "several and chooses none: how far back to look and how "
                    "much history to require decide what counts as normal, "
                    "and that is a programme decision. No expected values were "
                    "produced, which is not a statement that nothing is unusual."
                ),
            )
            self._session.add(build)
            self._session.flush()
            report = BaselineReport(
                build_id=build.id,
                status=BaselineBuildStatus.NOT_CONFIGURED,
                missing_configuration=sorted(missing),
                notes=build.notes,
            )
            logger.info("baseline_build_not_configured", **report.as_dict())
            return report

        if specification.method is BaselineMethod.SEASONAL_PERIOD_OF_YEAR_MEDIAN:
            periods, skipped = seasonal_periods(
                target_period_start,
                target_period_end,
                period_grain,
                specification.history_periods,
            )
        else:
            periods = preceding_periods(
                target_period_start,
                target_period_end,
                period_grain,
                specification.history_periods,
            )
            skipped = []

        history_start = min((start for start, _ in periods), default=None)
        history_end = max((end for _, end in periods), default=None)

        build = BaselineBuild(
            build_status=BaselineBuildStatus.RUNNING,
            series_kind=series_kind,
            target_period_start=target_period_start,
            target_period_end=target_period_end,
            period_grain=period_grain,
            method_version_id=specification.method_version_id,
            baseline_method=specification.method,
            history_periods=specification.history_periods,
            minimum_history_periods=specification.minimum_history_periods,
            minimum_completeness=specification.minimum_completeness,
            uncertainty_multiplier=specification.uncertainty_multiplier,
            history_start=history_start,
            history_end=history_end,
            engine_version=ENGINE_VERSION,
            started_at=started,
        )
        self._session.add(build)
        self._session.flush()

        report = BaselineReport(build_id=build.id, status=BaselineBuildStatus.RUNNING)
        history = self.read_history(series_kind, periods)

        for scope, points in history.items():
            self._write_result(
                build=build,
                specification=specification,
                scope=scope,
                points=points,
                periods=periods,
                skipped=skipped,
                series_kind=series_kind,
                period_grain=period_grain,
                report=report,
            )

        build.build_status = BaselineBuildStatus.COMPLETED
        build.series_evaluated = report.series_evaluated
        build.results_written = report.results_written
        build.insufficient_history = report.insufficient_history
        build.insufficient_completeness = report.insufficient_completeness
        build.finished_at = datetime.now(UTC)
        report.status = BaselineBuildStatus.COMPLETED
        self._session.flush()
        logger.info("baseline_build_finished", **report.as_dict())
        return report

    def _write_result(
        self,
        *,
        build: BaselineBuild,
        specification: BaselineSpecification,
        scope: SeriesScope,
        points: dict[date, HistoryPoint],
        periods: list[tuple[date, date]],
        skipped: list[dict[str, object]],
        series_kind: BaselineSeriesKind,
        period_grain: PeriodGrain,
        report: BaselineReport,
    ) -> None:
        report.series_evaluated += 1

        contributing: list[dict[str, object]] = []
        excluded: list[dict[str, object]] = list(skipped)
        values: list[Decimal] = []

        for start, end in periods:
            point = points.get(start)
            if point is None:
                excluded.append({"period_start": start.isoformat(), "reason": "period_absent"})
                continue
            if point.value is None or point.status is not IndicatorValueStatus.AVAILABLE:
                excluded.append({"period_start": start.isoformat(), "reason": point.status.value})
                continue
            values.append(point.value)
            contributing.append(
                {
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "value": str(point.value),
                }
            )

        available = len(periods)
        used = len(values)
        completeness = Decimal(used) / Decimal(available) if available else Decimal(0)

        sufficiency, status, expected, dispersion = self._summarise(
            specification, values, used, completeness
        )

        if sufficiency is BaselineSufficiency.SUFFICIENT:
            report.sufficient += 1
        elif sufficiency is BaselineSufficiency.NO_HISTORY:
            report.no_history += 1
        elif sufficiency is BaselineSufficiency.INSUFFICIENT_HISTORY:
            report.insufficient_history += 1
        else:
            report.insufficient_completeness += 1

        lower = upper = None
        if (
            expected is not None
            and dispersion is not None
            and specification.uncertainty_multiplier is not None
        ):
            width = dispersion * specification.uncertainty_multiplier
            lower, upper = expected - width, expected + width

        fingerprint = _fingerprint(
            series=scope.series_key,
            kind=series_kind.value,
            facility=scope.facility_id,
            unit=scope.geography_unit_id,
            method=specification.method.value,
            method_version=specification.method_version_id,
            target=build.target_period_start,
            contributing=contributing,
        )

        self._session.add(
            BaselineResult(
                baseline_build_id=build.id,
                series_kind=series_kind,
                series_key=scope.series_key,
                baseline_method=specification.method,
                geography_grain=scope.geography_grain,
                geography_unit_id=scope.geography_unit_id,
                facility_id=scope.facility_id,
                period_start=build.target_period_start,
                period_end=build.target_period_end,
                period_grain=period_grain,
                method_version_id=specification.method_version_id,
                value=expected,
                value_status=status,
                sufficiency=sufficiency,
                dispersion_measure=(
                    specification.dispersion_measure
                    if dispersion is not None
                    else DispersionMeasure.NONE
                ),
                dispersion_value=dispersion,
                uncertainty_lower=lower,
                uncertainty_upper=upper,
                history_periods_available=available,
                history_periods_used=used,
                history_periods_required=specification.minimum_history_periods,
                history_start=build.history_start,
                history_end=build.history_end,
                contributing_periods={"periods": contributing},
                excluded_periods={"periods": excluded} if excluded else None,
                input_fingerprint=fingerprint,
                source_cutoff=build.started_at,
                engine_version=ENGINE_VERSION,
                computed_at=datetime.now(UTC),
                contributing_units=used,
                expected_units=available,
                quality_context={
                    "domain_limit": (
                        "An expected level derived from this series' own "
                        "history. It describes what has been usual here, not "
                        "what should be."
                    ),
                    "completeness": str(completeness.quantize(Decimal("0.0001"))),
                },
                notes=(
                    None
                    if sufficiency is BaselineSufficiency.SUFFICIENT
                    else _shortfall_note(sufficiency, used, available, specification)
                ),
            )
        )
        report.results_written += 1

    def _summarise(
        self,
        specification: BaselineSpecification,
        values: list[Decimal],
        used: int,
        completeness: Decimal,
    ) -> tuple[BaselineSufficiency, IndicatorValueStatus, Decimal | None, Decimal | None]:
        """Centre and spread, or an explicit statement that there is neither."""
        if used == 0:
            return (
                BaselineSufficiency.NO_HISTORY,
                IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                None,
                None,
            )
        if used < specification.minimum_history_periods:
            return (
                BaselineSufficiency.INSUFFICIENT_HISTORY,
                IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                None,
                None,
            )
        if completeness < specification.minimum_completeness:
            return (
                BaselineSufficiency.INSUFFICIENT_COMPLETENESS,
                IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                None,
                None,
            )

        if specification.method is BaselineMethod.HISTORICAL_MEAN:
            expected = sum(values, Decimal(0)) / Decimal(used)
            spread = (
                Decimal(str(statistics.stdev([float(v) for v in values]))) if used > 1 else None
            )
        else:
            expected = Decimal(str(statistics.median([float(v) for v in values])))
            if used > 1:
                deviations = [abs(v - expected) for v in values]
                spread = Decimal(str(statistics.median([float(d) for d in deviations])))
            else:
                # One period has a centre and no spread. Calling that spread
                # zero would make the series look perfectly stable.
                spread = None

        return (
            BaselineSufficiency.SUFFICIENT,
            IndicatorValueStatus.AVAILABLE,
            expected.quantize(Decimal("0.000001")),
            spread.quantize(Decimal("0.000001")) if spread is not None else None,
        )


def _shortfall_note(
    sufficiency: BaselineSufficiency,
    used: int,
    available: int,
    specification: BaselineSpecification,
) -> str:
    if sufficiency is BaselineSufficiency.NO_HISTORY:
        return (
            "No comparable historical period carried a usable value. There is "
            "no expected level for this series here, which is not the same as "
            "an expected level of zero."
        )
    if sufficiency is BaselineSufficiency.INSUFFICIENT_HISTORY:
        return (
            f"{used} of {available} comparable periods carried a usable value; "
            f"the approved method requires at least "
            f"{specification.minimum_history_periods}. No expected level was "
            "computed."
        )
    return (
        f"Completeness {used}/{available} is below the approved minimum of "
        f"{specification.minimum_completeness}. No expected level was computed."
    )


def _fingerprint(**material: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {k: str(v) for k, v in sorted(material.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def latest_build(
    session: Session,
    target_period_start: date,
    target_period_end: date,
    series_kind: BaselineSeriesKind,
) -> BaselineBuild | None:
    """The most recent **completed** build for a period and series kind.

    A ``not_configured`` build is deliberately not returned. It has no
    baselines, and treating it as one would let a caller compare against
    nothing and report the result as an expectation.
    """
    return (
        session.execute(
            select(BaselineBuild)
            .where(
                BaselineBuild.target_period_start == target_period_start,
                BaselineBuild.target_period_end == target_period_end,
                BaselineBuild.series_kind == series_kind,
                BaselineBuild.build_status == BaselineBuildStatus.COMPLETED,
            )
            .order_by(BaselineBuild.started_at.desc())
        )
        .scalars()
        .first()
    )


__all__ = [
    "BASELINE_METHOD_CODE",
    "DISPERSION_FOR_METHOD",
    "ENGINE_VERSION",
    "REQUIRED_PARAMETERS",
    "UNCERTAINTY_PARAMETER",
    "BaselineEngine",
    "BaselineNotConfiguredError",
    "BaselineReport",
    "BaselineSpecification",
    "HistoryPoint",
    "SeriesScope",
    "latest_build",
    "preceding_periods",
    "seasonal_periods",
]
