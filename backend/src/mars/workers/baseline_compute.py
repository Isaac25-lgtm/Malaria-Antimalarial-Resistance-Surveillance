"""The historical baseline job.

Builds expected levels for one target period, one series kind at a time.

Series kinds are built separately and reported separately because they come
from different tables with different completeness. An indicator series and a
testing-measure series can be sufficient and insufficient in the same month,
and a combined run would hide which was which.

Safe to run repeatedly. Every result is keyed by a fingerprint over the history
that produced it, so re-running over unchanged history writes an identical
figure and a corrected history writes a new one beside it.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.baseline import BaselineEngine, BaselineReport
from mars.core.logging import get_logger
from mars.domain.enums import BaselineSeriesKind, PeriodGrain

logger = get_logger(__name__)

JOB_NAME = "baseline.compute"

#: Built by default, in this order. Indicators first because they are the
#: governed series; the surveillance measures follow.
DEFAULT_SERIES = (
    BaselineSeriesKind.INDICATOR,
    BaselineSeriesKind.TESTING_MEASURE,
    BaselineSeriesKind.TREATMENT_MEASURE,
)


def run(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    series_kinds: tuple[BaselineSeriesKind, ...] = DEFAULT_SERIES,
) -> dict[str, BaselineReport]:
    """Build baselines for one target period."""
    engine = BaselineEngine(session)
    reports: dict[str, BaselineReport] = {}

    for series_kind in series_kinds:
        report = engine.build(
            period_start,
            period_end,
            series_kind=series_kind,
            period_grain=period_grain,
        )
        reports[series_kind.value] = report
        logger.info(
            "baseline_job_finished",
            job=JOB_NAME,
            series_kind=series_kind.value,
            **report.as_dict(),
        )

    session.flush()
    return reports


__all__ = ["DEFAULT_SERIES", "JOB_NAME", "run"]
