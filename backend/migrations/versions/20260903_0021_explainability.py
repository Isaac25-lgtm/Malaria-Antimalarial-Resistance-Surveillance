"""Immutable structured signal explanations.

Revision ID: 0021_explainability
Revises: 0020_signal_engine
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_explainability"
down_revision: str | None = "0020_signal_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ANALYTICS = "mars_analytics"
GOVERNANCE = "mars_governance"


def upgrade() -> None:
    op.create_table(
        "signal_explanation",
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("why_flagged", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("counter_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data_quality", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("method_steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainty", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("missing_information", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recommended_actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("interpretation_limit", sa.Text(), nullable=False),
        sa.Column("signal_input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("generator_version", sa.String(length=32), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(input_fingerprint) = 64", name=op.f("ck_signal_explanation_fingerprint_is_sha256")),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'array'", name=op.f("ck_signal_explanation_evidence_is_array")),
        sa.CheckConstraint("jsonb_typeof(counter_evidence) = 'array'", name=op.f("ck_signal_explanation_counter_evidence_is_array")),
        sa.CheckConstraint("jsonb_typeof(data_quality) = 'object'", name=op.f("ck_signal_explanation_data_quality_is_object")),
        sa.CheckConstraint("jsonb_typeof(method_steps) = 'array'", name=op.f("ck_signal_explanation_method_steps_is_array")),
        sa.CheckConstraint("jsonb_typeof(uncertainty) = 'array'", name=op.f("ck_signal_explanation_uncertainty_is_array")),
        sa.CheckConstraint("jsonb_typeof(missing_information) = 'array'", name=op.f("ck_signal_explanation_missing_information_is_array")),
        sa.CheckConstraint("jsonb_typeof(recommended_actions) = 'array'", name=op.f("ck_signal_explanation_recommended_actions_is_array")),
        sa.ForeignKeyConstraint(["signal_id"], [f"{ANALYTICS}.surveillance_signal.id"], name=op.f("fk_signal_explanation_signal_id_surveillance_signal"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["method_version_id"], [f"{GOVERNANCE}.method_version.id"], name=op.f("fk_signal_explanation_method_version_id_method_version"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_explanation")),
        sa.UniqueConstraint("signal_id", "generator_version", "input_fingerprint", name="uq_signal_explanation_version_input"),
        schema=ANALYTICS,
    )
    op.create_index("ix_signal_explanation_signal", "signal_explanation", ["signal_id", "generated_at"], schema=ANALYTICS)


def downgrade() -> None:
    op.drop_index("ix_signal_explanation_signal", table_name="signal_explanation", schema=ANALYTICS)
    op.drop_table("signal_explanation", schema=ANALYTICS)
