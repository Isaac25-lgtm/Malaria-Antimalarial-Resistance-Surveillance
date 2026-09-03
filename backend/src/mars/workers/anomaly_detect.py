"""The temporal anomaly detection job.

Judges one period's observations against the baselines built for it.

The baseline comes first and is passed in explicitly rather than rebuilt here.
Detection reads the most recent **completed** baseline build for the period; a
``not_configured`` baseline run produced nothing, and comparing against nothing
would let every observation be recorded as unremarkable.

Series kinds are detected separately and reported separately, because an
indicator series and a testing-measure series can have quite different
baselines available in the same month.

Safe to run repeatedly: results are keyed by a fingerprint over the
observation, the expectation and the rule, and a persistence run refuses to
count the same period twice.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.anomaly import AnomalyEngine, AnomalyReport
from mars.analytics.baseline import latest_build
from mars.core.logging import get_logger
from mars.domain.enums import BaselineSeriesKind, PeriodGrain
from mars.workers.baseline_compute import DEFAULT_SERIES

logger = get_logger(__name__)

JOB_NAME = "anomaly.detect"


def run(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    series_kinds: tuple[BaselineSeriesKind, ...] = DEFAULT_SERIES,
) -> dict[str, AnomalyReport]:
    """Detect temporal anomalies for one period."""
    engine = AnomalyEngine(session)
    reports: dict[str, AnomalyReport] = {}

    for series_kind in series_kinds:
        baseline_build = latest_build(session, period_start, period_end, series_kind)
        if baseline_build is None:
            logger.info(
                "anomaly_job_no_baseline",
                job=JOB_NAME,
                series_kind=series_kind.value,
                note=(
                    "No completed baseline build for this period. Every "
                    "observation will be recorded as not evaluated for want of "
                    "a baseline, rather than as unremarkable."
                ),
            )
        report = engine.detect(
            period_start,
            period_end,
            series_kind=series_kind,
            baseline_build=baseline_build,
            period_grain=period_grain,
        )
        reports[series_kind.value] = report
        logger.info(
            "anomaly_job_finished",
            job=JOB_NAME,
            series_kind=series_kind.value,
            **report.as_dict(),
        )

    session.flush()
    return reports


__all__ = ["JOB_NAME", "run"]
