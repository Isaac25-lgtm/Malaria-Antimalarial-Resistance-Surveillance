"""Prompt 22 deterministic explanation materialisation job."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.domain.enums import SignalStatus
from mars.domain.explanation import SignalExplanation
from mars.domain.signal import SurveillanceSignal
from mars.explainability.engine import ExplanationEngine

JOB_NAME = "explanation.build"


def run(session: Session) -> list[SignalExplanation]:
    """Build or reuse one explanation for every active signal."""
    engine = ExplanationEngine(session)
    signals = session.execute(
        select(SurveillanceSignal.id).where(SurveillanceSignal.signal_status == SignalStatus.ACTIVE)
    ).scalars()
    explanations = [engine.build(signal_id) for signal_id in signals]
    session.flush()
    return explanations


__all__ = ["JOB_NAME", "run"]
