"""External integration runs.

Revision ID: 0011_integration_runs
Revises: 0010_aggregate_reporting
Created: 2026-09-03

Two tables:

``integration_run``                one exchange with an external system
``integration_mapping_proposal``   a remote identifier MARS could not place

Neither holds a credential. ``error_summary`` is a sentence MARS composed, never
a remote response body - a DHIS2 error can quote the request that produced it,
and that request carries an Authorization header. The base URL is stored with
any userinfo stripped.

A remote identifier is never a MARS key. DHIS2 UIDs live in the existing
``geography_unit_alias`` and ``facility_identifier`` crosswalks, and an
unresolved one becomes a proposal a person answers rather than a name match
nobody notices was wrong.

The scope fingerprint is what makes a scheduled pull idempotent: the same
request is the same run, and ``attempt`` distinguishes a retry after partial
failure from the run it retried.

Documented in ``docs/architecture/dhis2.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_integration_runs"
down_revision: str | None = "0010_aggregate_reporting"
CORE = "mars_core"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    postgresql.ENUM(
        "organisation_unit_metadata",
        "facility_metadata",
        "data_element_metadata",
        "dataset_metadata",
        "aggregate_data_values",
        "analytics_query",
        name="integration_resource",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "pending",
        "running",
        "completed",
        "partial",
        "failed",
        name="integration_run_status",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "proposed",
        "accepted",
        "rejected",
        "superseded",
        name="mapping_proposal_status",
        schema=CORE,
    ).create(bind, checkfirst=True)

    op.create_table(
        "integration_run",
        sa.Column("system", sa.String(length=64), nullable=False),
        sa.Column(
            "resource",
            postgresql.ENUM(
                "organisation_unit_metadata",
                "facility_metadata",
                "data_element_metadata",
                "dataset_metadata",
                "aggregate_data_values",
                "analytics_query",
                name="integration_resource",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("scope_description", sa.String(length=512), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "run_status",
            postgresql.ENUM(
                "pending",
                "running",
                "completed",
                "partial",
                "failed",
                name="integration_run_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column("pages_fetched", sa.Integer(), nullable=False),
        sa.Column("records_received", sa.Integer(), nullable=False),
        sa.Column("records_accepted", sa.Integer(), nullable=False),
        sa.Column("records_rejected", sa.Integer(), nullable=False),
        sa.Column("records_unchanged", sa.Integer(), nullable=False),
        sa.Column("mappings_unresolved", sa.Integer(), nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=True),
        sa.Column("import_batch_id", sa.UUID(), nullable=True),
        sa.Column("adapter_version", sa.String(length=32), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("initiated_by", sa.String(length=160), nullable=True),
        sa.Column("error_category", sa.String(length=48), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_integration_run_attempt_is_positive")),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_integration_run_finish_after_start"),
        ),
        sa.CheckConstraint(
            "pages_fetched >= 0", name=op.f("ck_integration_run_pages_not_negative")
        ),
        sa.CheckConstraint(
            "payload_checksum IS NULL OR length(payload_checksum) = 64",
            name=op.f("ck_integration_run_payload_checksum_is_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["mars_core.import_batch.id"],
            name="fk_integration_batch",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_run")),
        sa.UniqueConstraint(
            "system",
            "resource",
            "scope_fingerprint",
            "attempt",
            name="uq_integration_run_system_resource_scope_attempt",
        ),
        schema=CORE,
        comment=(
            "One exchange with an external system. Holds no credential and no raw remote error "
            "body: error_summary is composed by MARS."
        ),
    )
    op.create_index(
        "ix_integration_run_started", "integration_run", ["started_at"], unique=False, schema=CORE
    )
    op.create_index(
        "ix_integration_run_status", "integration_run", ["run_status"], unique=False, schema=CORE
    )
    op.create_index(
        "ix_integration_run_system_resource",
        "integration_run",
        ["system", "resource"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "integration_mapping_proposal",
        sa.Column("integration_run_id", sa.UUID(), nullable=True),
        sa.Column("system", sa.String(length=64), nullable=False),
        sa.Column("remote_type", sa.String(length=48), nullable=False),
        sa.Column("remote_id", sa.String(length=128), nullable=False),
        sa.Column("remote_name", sa.String(length=255), nullable=True),
        sa.Column("remote_parent_id", sa.String(length=128), nullable=True),
        sa.Column(
            "proposal_status",
            postgresql.ENUM(
                "proposed",
                "accepted",
                "rejected",
                "superseded",
                name="mapping_proposal_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["integration_run_id"],
            ["mars_core.integration_run.id"],
            name="fk_mapping_run",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_mapping_proposal")),
        sa.UniqueConstraint(
            "system", "remote_type", "remote_id", name="uq_integration_mapping_system_type_remote"
        ),
        schema=CORE,
        comment=(
            "Remote identifiers with no MARS mapping. Never resolved by name similarity; promotion "
            "is a governance action."
        ),
    )
    op.create_index(
        "ix_integration_mapping_run",
        "integration_mapping_proposal",
        ["integration_run_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_integration_mapping_status",
        "integration_mapping_proposal",
        ["proposal_status"],
        unique=False,
        schema=CORE,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        "ix_integration_mapping_status", table_name="integration_mapping_proposal", schema=CORE
    )
    op.drop_index(
        "ix_integration_mapping_run", table_name="integration_mapping_proposal", schema=CORE
    )
    op.drop_table("integration_mapping_proposal", schema=CORE)
    op.drop_index("ix_integration_run_system_resource", table_name="integration_run", schema=CORE)
    op.drop_index("ix_integration_run_status", table_name="integration_run", schema=CORE)
    op.drop_index("ix_integration_run_started", table_name="integration_run", schema=CORE)
    op.drop_table("integration_run", schema=CORE)

    # One call per type: the migration guard counts creates against drops
    # in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="mapping_proposal_status", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="integration_run_status", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="integration_resource", schema=CORE).drop(bind, checkfirst=True)
