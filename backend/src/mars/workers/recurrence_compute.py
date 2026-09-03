"""The recurrence computation job.

Turns the latest completed episode build for a period into recurrence measures.

Reads a **completed** build only. A ``not_configured`` build has no episodes,
and computing recurrence from it would report a confident zero for every
facility - which is worse than reporting nothing, because a zero looks like an
answer.

Safe to run repeatedly: results are keyed by an input fingerprint that includes
the episodes and the governed interval bands, so re-running over unchanged
evidence writes nothing and a change to either writes new rows beside the old
ones.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.recurrence import RecurrenceEngine, RecurrenceReport, latest_build
from mars.core.logging import get_logger
from mars.domain.enums import PeriodGrain

logger = get_logger(__name__)

JOB_NAME = "recurrence.compute"


def run(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
) -> RecurrenceReport:
    """Compute recurrence measures for one period."""
    build = latest_build(session, period_start, period_end)
    if build is None:
        # No completed episode build. Reported rather than treated as "no
        # recurrence": the two are opposite statements.
        report = RecurrenceReport(
            notes=(
                "No completed episode build for this period. Recurrence cannot "
                "be computed, which is not the same as there being none."
            )
        )
        logger.info("recurrence_job_no_build", job=JOB_NAME, **report.as_dict())
        return report

    report = RecurrenceEngine(session).compute(build, period_grain=period_grain)
    session.flush()
    logger.info("recurrence_job_finished", job=JOB_NAME, **report.as_dict())
    return report


__all__ = ["JOB_NAME", "run"]
