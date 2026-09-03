"""Governed surveillance signals and typed evidence.

Revision ID: 0020_signal_engine
Revises: 0019_spatial_clustering
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_signal_engine"
down_revision: str | None = "0019_spatial_clustering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ANALYTICS = "mars_analytics"
CORE = "mars_core"
GOVERNANCE = "mars_governance"


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        "repeat_positive",
        "recurrence_cluster",
        "temporal_anomaly",
        "spatial_cluster",
        "testing_anomaly",
        "treatment_anomaly",
        "commodity_associated",
        "facility_anomaly",
        "data_quality",
        "reconciliation",
        name="signal_type",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "unclassified",
        "informational",
        "attention",
        "high",
        "urgent",
        name="signal_priority",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM("active", "superseded", name="signal_status", schema=ANALYTICS).create(
        bind, checkfirst=True
    )
    postgresql.ENUM(
        "running",
        "completed",
        "not_configured",
        "failed",
        name="signal_generation_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "supporting",
        "counter",
        "context",
        name="signal_evidence_role",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "temporal_anomaly",
        "hotspot",
        "spatial_cluster",
        "recurrence",
        "reconciliation",
        "testing",
        "treatment",
        "commodity_alert",
        name="signal_evidence_kind",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "signal_generation_run",
        sa.Column(
            "run_status",
            postgresql.ENUM(name="signal_generation_status", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column("method_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("missing_configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("candidates_examined", sa.Integer(), nullable=False),
        sa.Column("signals_created", sa.Integer(), nullable=False),
        sa.Column("signals_superseded", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _uuid_pk(),
        *_timestamps(),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_signal_generation_run_period_ordered")
        ),
        sa.CheckConstraint(
            "run_status <> 'completed' OR method_version_id IS NOT NULL",
            name=op.f("ck_signal_generation_run_completed_run_has_method"),
        ),
        sa.CheckConstraint(
            "run_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name=op.f("ck_signal_generation_run_refusal_names_missing_configuration"),
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            [f"{GOVERNANCE}.method_version.id"],
            name="fk_signal_run_method_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_generation_run")),
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_signal_generation_run_period",
        "signal_generation_run",
        ["period_start", "period_end"],
        schema=ANALYTICS,
    )

    op.create_table(
        "surveillance_signal",
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "signal_type",
            postgresql.ENUM(name="signal_type", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "signal_status",
            postgresql.ENUM(name="signal_status", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "priority",
            postgresql.ENUM(name="signal_priority", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column("group_key", sa.String(length=64), nullable=False),
        sa.Column("geography_unit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("facility_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rule_code", sa.String(length=64), nullable=False),
        sa.Column("rule_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Numeric(12, 6), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("counter_evidence_count", sa.Integer(), nullable=False),
        sa.Column("data_quality", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainty", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "recommended_action_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        _uuid_pk(),
        *_timestamps(),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_surveillance_signal_period_ordered")
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_surveillance_signal_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "priority = 'unclassified' OR (score IS NOT NULL AND method_version_id IS NOT NULL)",
            name=op.f("ck_surveillance_signal_classified_priority_requires_governance"),
        ),
        sa.CheckConstraint(
            "score IS NULL OR method_version_id IS NOT NULL",
            name=op.f("ck_surveillance_signal_score_requires_method"),
        ),
        sa.CheckConstraint(
            "signal_status <> 'superseded' OR superseded_by_id IS NOT NULL",
            name=op.f("ck_surveillance_signal_superseded_signal_names_replacement"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_run_id"],
            [f"{ANALYTICS}.signal_generation_run.id"],
            name="fk_surveillance_signal_generation_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            [f"{GOVERNANCE}.method_version.id"],
            name="fk_surveillance_signal_method_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            [f"{CORE}.geography_unit.id"],
            name="fk_surveillance_signal_geography_unit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            [f"{CORE}.facility.id"],
            name="fk_surveillance_signal_facility",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            [f"{ANALYTICS}.surveillance_signal.id"],
            name="fk_surveillance_signal_supersedes",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            [f"{ANALYTICS}.surveillance_signal.id"],
            name="fk_surveillance_signal_superseded_by",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surveillance_signal")),
        sa.UniqueConstraint("input_fingerprint", name="uq_surveillance_signal_input_fingerprint"),
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_surveillance_signal_scope",
        "surveillance_signal",
        ["geography_unit_id", "facility_id"],
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_surveillance_signal_type_period",
        "surveillance_signal",
        ["signal_type", "period_start"],
        schema=ANALYTICS,
    )
    op.create_index(
        "uq_surveillance_signal_active_group",
        "surveillance_signal",
        ["group_key"],
        unique=True,
        schema=ANALYTICS,
        postgresql_where=sa.text("signal_status = 'active'"),
    )

    op.create_table(
        "signal_evidence",
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "evidence_kind",
            postgresql.ENUM(name="signal_evidence_kind", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "role",
            postgresql.ENUM(name="signal_evidence_role", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contribution", sa.Numeric(12, 6), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _uuid_pk(),
        *_timestamps(),
        sa.CheckConstraint(
            "source_table IN ('temporal_anomaly_result', 'hotspot_result', "
            "'spatial_cluster_result', 'recurrence_result', 'reconciliation_finding', "
            "'testing_surveillance_result', 'treatment_surveillance_result', "
            "'commodity_operational_alert')",
            name=op.f("ck_signal_evidence_source_table_is_known"),
        ),
        sa.CheckConstraint(
            "(evidence_kind = 'temporal_anomaly' AND "
            "source_table = 'temporal_anomaly_result') OR "
            "(evidence_kind = 'hotspot' AND source_table = 'hotspot_result') OR "
            "(evidence_kind = 'spatial_cluster' AND "
            "source_table = 'spatial_cluster_result') OR "
            "(evidence_kind = 'recurrence' AND source_table = 'recurrence_result') OR "
            "(evidence_kind = 'reconciliation' AND "
            "source_table = 'reconciliation_finding') OR "
            "(evidence_kind = 'testing' AND source_table IN "
            "('testing_surveillance_result', 'temporal_anomaly_result')) OR "
            "(evidence_kind = 'treatment' AND source_table IN "
            "('treatment_surveillance_result', 'temporal_anomaly_result')) OR "
            "(evidence_kind = 'commodity_alert' AND "
            "source_table = 'commodity_operational_alert')",
            name=op.f("ck_signal_evidence_evidence_kind_matches_source_table"),
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            [f"{ANALYTICS}.surveillance_signal.id"],
            name=op.f("fk_signal_evidence_signal_id_surveillance_signal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_evidence")),
        sa.UniqueConstraint(
            "signal_id",
            "evidence_kind",
            "source_record_id",
            "role",
            name="uq_signal_evidence_source",
        ),
        schema=ANALYTICS,
    )
    op.create_index("ix_signal_evidence_signal", "signal_evidence", ["signal_id"], schema=ANALYTICS)


def downgrade() -> None:
    op.drop_index("ix_signal_evidence_signal", table_name="signal_evidence", schema=ANALYTICS)
    op.drop_table("signal_evidence", schema=ANALYTICS)
    op.drop_index(
        "uq_surveillance_signal_active_group", table_name="surveillance_signal", schema=ANALYTICS
    )
    op.drop_index(
        "ix_surveillance_signal_type_period", table_name="surveillance_signal", schema=ANALYTICS
    )
    op.drop_index(
        "ix_surveillance_signal_scope", table_name="surveillance_signal", schema=ANALYTICS
    )
    op.drop_table("surveillance_signal", schema=ANALYTICS)
    op.drop_index(
        "ix_signal_generation_run_period", table_name="signal_generation_run", schema=ANALYTICS
    )
    op.drop_table("signal_generation_run", schema=ANALYTICS)
    bind = op.get_bind()
    postgresql.ENUM(name="signal_evidence_kind", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="signal_evidence_role", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="signal_generation_status", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="signal_status", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="signal_priority", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="signal_type", schema=ANALYTICS).drop(bind, checkfirst=True)
