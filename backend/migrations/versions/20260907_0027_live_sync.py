"""Persist encrypted live snapshots and resumable retrieval jobs.

Revision ID: 0027_live_sync
Revises: 0026_reapply_runtime_role_grants
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0027_live_sync"
down_revision: str | None = "0026_reapply_runtime_role_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_sync_job",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("job_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("completed_steps", sa.Integer(), nullable=False),
        sa.Column("total_steps", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("checkpoint", sa.LargeBinary()),
        sa.Column("snapshot", sa.LargeBinary()),
        sa.Column("snapshot_status", sa.String(32)),
        schema="mars_analytics",
    )
    op.create_index(
        "ix_live_sync_scope_period",
        "live_sync_job",
        ["scope_key", "period_start", "period_end"],
        schema="mars_analytics",
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mars_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON mars_analytics.live_sync_job TO mars_app;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mars_identity_service') THEN
            REVOKE ALL ON mars_analytics.live_sync_job FROM mars_identity_service;
        END IF;
    END $$;""")


def downgrade() -> None:
    op.drop_table("live_sync_job", schema="mars_analytics")
