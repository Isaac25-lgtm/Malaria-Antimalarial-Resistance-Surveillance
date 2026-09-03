"""The episode build job.

Groups encounters into candidate episodes for a period under the approved
episode rule.

**It builds nothing when no rule is approved**, and records a
``not_configured`` build saying so. That is not a failure to route around: MARS
supplies no episode window because whether two positive results are one illness
or two depends on the drug, the setting and the programme's guidance, and no
defensible universal answer exists.

Safe to run repeatedly. A build is keyed by rule version, period and a
fingerprint of the encounters it read, so re-running over unchanged evidence
returns the existing build and a corrected encounter produces a new one rather
than silently altering episodes a clinician has already read.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.episodes import BuildReport, EpisodeEngine
from mars.core.logging import get_logger

logger = get_logger(__name__)

JOB_NAME = "episode.build"


def run(session: Session, *, period_start: date, period_end: date) -> BuildReport:
    """Build episode candidates for one period."""
    report = EpisodeEngine(session).build(period_start, period_end)
    session.flush()
    logger.info("episode_build_job_finished", job=JOB_NAME, **report.as_dict())
    return report


__all__ = ["JOB_NAME", "run"]
