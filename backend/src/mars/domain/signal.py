"""Immutable surveillance signals and typed evidence references — Prompt 21."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import ANALYTICS, CORE, GOVERNANCE
from mars.domain.enums import (
    SignalEvidenceKind,
    SignalEvidenceRole,
    SignalGenerationStatus,
    SignalPriority,
    SignalStatus,
    SignalType,
)


class SignalGenerationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signal_generation_run"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint(
            "run_status <> 'completed' OR method_version_id IS NOT NULL",
            name="completed_run_has_method",
        ),
        CheckConstraint(
            "run_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name="refusal_names_missing_configuration",
        ),
        Index("ix_signal_generation_run_period", "period_start", "period_end"),
        {"schema": ANALYTICS},
    )

    run_status: Mapped[SignalGenerationStatus] = mapped_column(
        pg_enum(SignalGenerationStatus, name="signal_generation_status", schema=ANALYTICS),
        nullable=False,
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT")
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    missing_configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    candidates_examined: Mapped[int] = mapped_column(nullable=False, default=0)
    signals_created: Mapped[int] = mapped_column(nullable=False, default=0)
    signals_superseded: Mapped[int] = mapped_column(nullable=False, default=0)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    signals: Mapped[list[SurveillanceSignal]] = relationship(back_populates="run")


class SurveillanceSignal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One version of a routine-data pattern requiring investigation."""

    __tablename__ = "surveillance_signal"
    __table_args__ = (
        UniqueConstraint("input_fingerprint", name="uq_surveillance_signal_input_fingerprint"),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint(
            "priority = 'unclassified' OR (score IS NOT NULL AND method_version_id IS NOT NULL)",
            name="classified_priority_requires_governance",
        ),
        CheckConstraint(
            "score IS NULL OR method_version_id IS NOT NULL", name="score_requires_method"
        ),
        CheckConstraint(
            "signal_status <> 'superseded' OR superseded_by_id IS NOT NULL",
            name="superseded_signal_names_replacement",
        ),
        Index("ix_surveillance_signal_scope", "geography_unit_id", "facility_id"),
        Index("ix_surveillance_signal_type_period", "signal_type", "period_start"),
        Index(
            "uq_surveillance_signal_active_group",
            "group_key",
            unique=True,
            postgresql_where=text("signal_status = 'active'"),
        ),
        {"schema": ANALYTICS},
    )

    generation_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.signal_generation_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    method_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    signal_type: Mapped[SignalType] = mapped_column(
        pg_enum(SignalType, name="signal_type", schema=ANALYTICS), nullable=False
    )
    signal_status: Mapped[SignalStatus] = mapped_column(
        pg_enum(SignalStatus, name="signal_status", schema=ANALYTICS), nullable=False
    )
    priority: Mapped[SignalPriority] = mapped_column(
        pg_enum(SignalPriority, name="signal_priority", schema=ANALYTICS), nullable=False
    )
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{CORE}.geography_unit.id", ondelete="RESTRICT")
    )
    facility_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{CORE}.facility.id", ondelete="RESTRICT")
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(12, 6))
    evidence_count: Mapped[int] = mapped_column(nullable=False)
    counter_evidence_count: Mapped[int] = mapped_column(nullable=False)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    uncertainty: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    recommended_action_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    generated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{ANALYTICS}.surveillance_signal.id")
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{ANALYTICS}.surveillance_signal.id")
    )

    run: Mapped[SignalGenerationRun] = relationship(back_populates="signals")
    evidence: Mapped[list[SignalEvidence]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )


class SignalEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One typed supporting, counter, or contextual source reference."""

    __tablename__ = "signal_evidence"
    __table_args__ = (
        UniqueConstraint(
            "signal_id",
            "evidence_kind",
            "source_record_id",
            "role",
            name="uq_signal_evidence_source",
        ),
        CheckConstraint(
            "source_table IN ('temporal_anomaly_result', 'hotspot_result', "
            "'spatial_cluster_result', 'recurrence_result', 'reconciliation_finding', "
            "'testing_surveillance_result', 'treatment_surveillance_result', "
            "'commodity_operational_alert')",
            name="source_table_is_known",
        ),
        CheckConstraint(
            "(evidence_kind = 'temporal_anomaly' AND source_table = 'temporal_anomaly_result') OR "
            "(evidence_kind = 'hotspot' AND source_table = 'hotspot_result') OR "
            "(evidence_kind = 'spatial_cluster' AND source_table = 'spatial_cluster_result') OR "
            "(evidence_kind = 'recurrence' AND source_table = 'recurrence_result') OR "
            "(evidence_kind = 'reconciliation' AND source_table = 'reconciliation_finding') OR "
            "(evidence_kind = 'testing' AND source_table IN "
            "('testing_surveillance_result', 'temporal_anomaly_result')) OR "
            "(evidence_kind = 'treatment' AND source_table IN "
            "('treatment_surveillance_result', 'temporal_anomaly_result')) OR "
            "(evidence_kind = 'commodity_alert' AND source_table = 'commodity_operational_alert')",
            name="evidence_kind_matches_source_table",
        ),
        Index("ix_signal_evidence_signal", "signal_id"),
        {"schema": ANALYTICS},
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.surveillance_signal.id", ondelete="CASCADE"),
        nullable=False,
    )
    evidence_kind: Mapped[SignalEvidenceKind] = mapped_column(
        pg_enum(SignalEvidenceKind, name="signal_evidence_kind", schema=ANALYTICS), nullable=False
    )
    role: Mapped[SignalEvidenceRole] = mapped_column(
        pg_enum(SignalEvidenceRole, name="signal_evidence_role", schema=ANALYTICS), nullable=False
    )
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    contribution: Mapped[float | None] = mapped_column(Numeric(12, 6))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    quality_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    signal: Mapped[SurveillanceSignal] = relationship(back_populates="evidence")


__all__ = ["SignalEvidence", "SignalGenerationRun", "SurveillanceSignal"]
