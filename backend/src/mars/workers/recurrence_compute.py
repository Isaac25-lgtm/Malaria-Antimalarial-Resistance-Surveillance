"""The recurrence computation job.

Two governed engines, chosen explicitly (``services/recurrence_dispatch.py``):

* **Episode-based** (the default, and every historical method version): turns the
  latest completed episode build for a period into recurrence measures. It reads
  a **completed** build only. A ``not_configured`` build has no episodes, and
  computing recurrence from it would report a confident zero for every facility -
  which is worse than reporting nothing, because a zero looks like an answer.
  Results are keyed by an input fingerprint that includes the episodes and the
  governed interval bands, so re-running over unchanged evidence writes nothing.
* **Positive-to-positive**: when the governed method version named by
  ``method_version_id`` declares the shared engine, the job submits and executes
  one programme analysis run over national stored evidence. The run freezes the
  method version's definition and records the engine version; re-running the
  same version and period returns the existing run.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.recurrence import RecurrenceEngine, RecurrenceReport, latest_build
from mars.core.errors import FeatureDisabledError, NotFoundError, ValidationFailedError
from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.domain.enums import PeriodGrain
from mars.domain.governance import MethodVersion
from mars.domain.recurrence_analysis import RecurrenceAnalysisRun
from mars.services.clinical_evidence_store import EvidenceCipher
from mars.services.recurrence_analysis_service import RecurrenceAnalysisService
from mars.services.recurrence_dispatch import engine_family, runs_on_this_engine

logger = get_logger(__name__)

JOB_NAME = "recurrence.compute"


def run(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    method_version_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> RecurrenceReport | RecurrenceAnalysisRun:
    """Compute governed recurrence for one period, on the engine the method names."""
    if method_version_id is not None:
        version = session.get(MethodVersion, method_version_id)
        if version is None:
            raise NotFoundError("method version not found")
        if engine_family(version.parameters) == "positive":
            return _positive(session, version, period_start, period_end, settings)

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


def _positive(
    session: Session,
    version: MethodVersion,
    period_start: date,
    period_end: date,
    settings: Settings | None,
) -> RecurrenceAnalysisRun:
    if settings is None:
        raise ValidationFailedError("A positive-engine run needs the deployment settings.")
    if not runs_on_this_engine(version.parameters):
        raise ValidationFailedError(
            f"{version.qualified_version} names an engine this deployment does not run."
        )
    secret = settings.identity_encryption_key
    display = settings.patient_display_key
    if secret is None or display is None:
        raise FeatureDisabledError(
            "Positive-engine runs need MARS_IDENTITY_ENCRYPTION_KEY and MARS_PATIENT_DISPLAY_KEY."
        )
    service = RecurrenceAnalysisService(
        session,
        settings,
        None,
        cipher=EvidenceCipher(secret.get_secret_value(), settings.identity_encryption_key_version),
        display_key=display.get_secret_value().encode("utf-8"),
    )
    analysis, token = service.submit_programme_run(
        version, period_start=period_start, period_end=period_end, actor=JOB_NAME
    )
    if token is not None:
        service.execute(analysis.id, token)
    session.flush()
    logger.info(
        "recurrence_job_positive_run",
        job=JOB_NAME,
        run_id=str(analysis.id),
        status=analysis.run_status,
        engine=analysis.engine_version,
    )
    return analysis


__all__ = ["JOB_NAME", "run"]
