"""The testing, treatment and commodity surveillance job.

Runs the three engines over one period, facility by facility. Commodity runs
first so that the reported stock conditions exist before the testing and
treatment engines look for the supply context that explains a decline.

Each engine is invoked separately and reports separately. A caller can run one
without the others, and a failure in one does not silently produce a partial
figure attributed to another.

Safe to run repeatedly: every result is keyed by an input fingerprint, so
re-running over unchanged evidence writes nothing, and changed evidence writes
new rows beside the old ones rather than editing them.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.surveillance import (
    CommoditySurveillanceEngine,
    SurveillanceReport,
    TestingSurveillanceEngine,
    TreatmentSurveillanceEngine,
)
from mars.core.logging import get_logger
from mars.domain.enums import PeriodGrain

logger = get_logger(__name__)

JOB_NAME = "surveillance.compute"


def run(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    previous_period: tuple[date, date] | None = None,
    district_id: uuid.UUID | None = None,
) -> dict[str, SurveillanceReport]:
    """Compute all three surveillance domains for one period.

    ``previous_period`` is optional and is the only way a testing volume change
    is produced. Passing a period the source has no data for yields an
    unavailable ratio rather than an invented one.
    """
    commodity = CommoditySurveillanceEngine(session)
    testing = TestingSurveillanceEngine(session)
    treatment = TreatmentSurveillanceEngine(session)

    facilities = commodity.facilities(district_id)
    reports = {
        "commodity": SurveillanceReport(domain="commodity"),
        "testing": SurveillanceReport(domain="testing"),
        "treatment": SurveillanceReport(domain="treatment"),
    }

    for facility in facilities:
        commodity.compute_facility(
            facility,
            period_start,
            period_end,
            period_grain=period_grain,
            report=reports["commodity"],
        )

    for facility in facilities:
        testing.compute_facility(
            facility,
            period_start,
            period_end,
            period_grain=period_grain,
            report=reports["testing"],
            previous_period=previous_period,
        )
        treatment.compute_facility(
            facility,
            period_start,
            period_end,
            period_grain=period_grain,
            report=reports["treatment"],
        )

    session.flush()
    for domain, report in reports.items():
        logger.info(
            "surveillance_job_finished",
            job=JOB_NAME,
            domain=domain,
            **{k: v for k, v in report.as_dict().items() if k != "domain"},
        )
    return reports


__all__ = ["JOB_NAME", "run"]
