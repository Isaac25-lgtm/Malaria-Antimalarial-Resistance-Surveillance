"""Immutable structured explanations for surveillance signals — Prompt 22."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from mars.db.schemas import ANALYTICS, GOVERNANCE


class SignalExplanation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What one immutable signal version meant when it was issued."""

    __tablename__ = "signal_explanation"
    __table_args__ = (
        UniqueConstraint(
            "signal_id",
            "generator_version",
            "input_fingerprint",
            name="uq_signal_explanation_version_input",
        ),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_is_array"),
        CheckConstraint(
            "jsonb_typeof(counter_evidence) = 'array'", name="counter_evidence_is_array"
        ),
        CheckConstraint("jsonb_typeof(data_quality) = 'object'", name="data_quality_is_object"),
        CheckConstraint("jsonb_typeof(method_steps) = 'array'", name="method_steps_is_array"),
        CheckConstraint("jsonb_typeof(uncertainty) = 'array'", name="uncertainty_is_array"),
        CheckConstraint(
            "jsonb_typeof(missing_information) = 'array'", name="missing_information_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(recommended_actions) = 'array'", name="recommended_actions_is_array"
        ),
        Index("ix_signal_explanation_signal", "signal_id", "generated_at"),
        {"schema": ANALYTICS},
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.surveillance_signal.id", ondelete="RESTRICT"),
        nullable=False,
    )
    method_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    why_flagged: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    counter_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    method_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    uncertainty: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    missing_information: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    recommended_actions: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    interpretation_limit: Mapped[str] = mapped_column(Text, nullable=False)
    signal_input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    signal: Mapped[Any] = relationship("SurveillanceSignal")


__all__ = ["SignalExplanation"]
