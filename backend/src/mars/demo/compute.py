"""Run the analytical chain over the demonstration dataset.

Each engine is a separate worker that reads what the previous one wrote. In a
deployment they run on the worker's schedule; for a demonstration this runs
them once, month by month in order, so that a baseline for March can read the
figures written for the months before it.

Every step runs in its own transaction and reports its own outcome. A step that
refuses (a method not approved, too little history) is recorded as such and
the chain continues: that is the same honest partial state a deployment would
show, not a failure to hide.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.db.session import session_scope
from mars.demo.configuration_pack import ensure_allowed
from mars.domain.encounter import OpdEncounter
from mars.domain.enums import GeographyLevel
from mars.domain.geography import GeographyUnit
from mars.workers import (
    anomaly_detect,
    baseline_compute,
    episode_build,
    explanation_build,
    indicator_materialisation,
    recurrence_compute,
    signal_generate,
    spatial_compute,
    surveillance_compute,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class StepOutcome:
    month: date | None
    step: str
    status: str
    detail: str


@dataclass(slots=True)
class ChainReport:
    months: list[date] = field(default_factory=list)
    outcomes: list[StepOutcome] = field(default_factory=list)

    @property
    def failed(self) -> list[StepOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status == "failed"]


def months_between(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar months touching ``start`` to ``end``, as (first day, last day)."""
    months: list[tuple[date, date]] = []
    current = start.replace(day=1)
    while current <= end:
        following = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        months.append((current, following - timedelta(days=1)))
        current = following
    return months


def encounter_span(session: Session) -> tuple[date, date] | None:
    """The first and last encounter dates on record, or ``None`` when there are none."""
    first, last = session.execute(
        select(func.min(OpdEncounter.encounter_date), func.max(OpdEncounter.encounter_date))
    ).one()
    if first is None or last is None:
        return None
    return first, last


def _boundary_version_id(session: Session) -> Any:
    return session.execute(
        select(GeographyUnit.boundary_version_id)
        .where(GeographyUnit.level == GeographyLevel.DISTRICT, GeographyUnit.is_active.is_(True))
        .limit(1)
    ).scalar_one_or_none()


def _summarise(result: object) -> str:
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        return str(as_dict())
    if isinstance(result, dict):
        return "; ".join(f"{key}: {_summarise(value)}" for key, value in result.items())
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    return str(result)


def _steps(
    start: date, end: date, settings: Settings
) -> Iterator[tuple[str, Callable[[Session], object]]]:
    yield (
        "indicators",
        lambda s: indicator_materialisation.materialise_period(
            s, period_start=start, period_end=end
        ),
    )
    yield (
        "testing_and_treatment",
        lambda s: surveillance_compute.run(s, period_start=start, period_end=end),
    )
    yield "episodes", lambda s: episode_build.run(s, period_start=start, period_end=end)
    yield (
        "recurrence",
        lambda s: recurrence_compute.run(s, period_start=start, period_end=end, settings=settings),
    )
    yield "baselines", lambda s: baseline_compute.run(s, period_start=start, period_end=end)
    yield "anomalies", lambda s: anomaly_detect.run(s, period_start=start, period_end=end)
    yield (
        "spatial",
        lambda s: spatial_compute.run(
            s, period_start=start, period_end=end, boundary_version_id=_boundary_version_id(s)
        ),
    )
    yield "signals", lambda s: signal_generate.run(s, period_start=start, period_end=end)


def run_chain(
    settings: Settings, *, start: date | None = None, end: date | None = None
) -> ChainReport:
    """Run every engine over each month from ``start`` to ``end``.

    Without dates the span of recorded encounters is used.
    """
    ensure_allowed(settings)
    report = ChainReport()
    if start is None or end is None:
        with session_scope() as session:
            span = encounter_span(session)
        if span is None:
            return report
        start = start or span[0]
        end = end or span[1]

    for month_start, month_end in months_between(start, end):
        report.months.append(month_start)
        for name, step in _steps(month_start, month_end, settings):
            report.outcomes.append(_run_step(month_start, name, step))

    report.outcomes.append(_run_step(None, "explanations", explanation_build.run))
    return report


def _run_step(month: date | None, name: str, step: Callable[[Session], object]) -> StepOutcome:
    try:
        with session_scope() as session:
            result = step(session)
    except Exception as exc:  # one step failing must not hide the rest
        logger.warning("demo_chain_step_failed", step=name, month=str(month), error=str(exc))
        return StepOutcome(month, name, "failed", f"{type(exc).__name__}: {exc}")
    return StepOutcome(month, name, "ran", _summarise(result))


__all__ = ["ChainReport", "StepOutcome", "encounter_span", "months_between", "run_chain"]
