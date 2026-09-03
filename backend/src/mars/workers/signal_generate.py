"""Prompt 21 governed signal-generation job."""

from datetime import date

from sqlalchemy.orm import Session

from mars.signals.engine import SignalEngine, SignalReport

JOB_NAME = "signal.generate"


def run(session: Session, *, period_start: date, period_end: date) -> SignalReport:
    report = SignalEngine(session).generate(period_start, period_end)
    session.flush()
    return report


__all__ = ["JOB_NAME", "run"]
