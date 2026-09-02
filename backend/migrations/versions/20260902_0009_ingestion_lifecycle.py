"""E-register ingestion lifecycle.

Revision ID: 0009_ingestion_lifecycle
Revises: 0008_geography_versioning
Created: 2026-09-02

Four tables that record what was received, what happened to it, and why:

``import_batch``             one artefact, its checksum, its status, its counters
``import_stage_execution``   each stage of that batch, timed and counted
``import_source_row``        one inbound row and its outcome
``import_validation_issue``  one thing wrong with a row, or with the batch

Three uniqueness constraints carry the idempotency guarantees, and they are
constraints rather than application checks because every one of them is exactly
the case where a check-then-act race produces duplicates:

* ``(source_system, artefact_checksum)`` - the same content cannot create a
  second batch, however many operators upload it at once
* ``(import_batch_id, source_row_reference)`` - a row is recorded once per
  batch, so a partially processed batch can be re-run
* ``(source_system, source_row_reference)`` on ``opd_encounter``, from migration
  0005 - the same source row cannot become two encounters

**No direct identifier is stored here.** The identity object is consumed inside
the identity boundary and stripped before a row reaches ``payload_redacted``.

The contract these tables serve is documented in
``docs/data-dictionary/ereg-inbound-contract.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_ingestion_lifecycle"
down_revision: str | None = "0008_geography_versioning"
CORE = "mars_core"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    postgresql.ENUM(
        "received",
        "validating",
        "quarantined",
        "loading",
        "completed",
        "partially_completed",
        "failed",
        name="import_batch_status",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "read",
        "validate",
        "link_identity",
        "write_canonical",
        name="import_stage",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "loaded",
        "unchanged",
        "updated",
        "quarantined",
        name="source_row_outcome",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "warning",
        "error",
        "fatal",
        name="validation_severity",
        schema=CORE,
    ).create(bind, checkfirst=True)

    op.create_table(
        "import_batch",
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("artefact_checksum", sa.String(length=64), nullable=False),
        sa.Column("artefact_name", sa.String(length=255), nullable=True),
        sa.Column("artefact_size_bytes", sa.Integer(), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
        sa.Column("facility_code_raw", sa.String(length=64), nullable=True),
        sa.Column(
            "import_status",
            postgresql.ENUM(
                "received",
                "validating",
                "quarantined",
                "loading",
                "completed",
                "partially_completed",
                "failed",
                name="import_batch_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("register_opened_on", sa.Date(), nullable=True),
        sa.Column("register_closed_on", sa.Date(), nullable=True),
        sa.Column("declared_row_count", sa.Integer(), nullable=False),
        sa.Column("rows_received", sa.Integer(), nullable=False),
        sa.Column("rows_loaded", sa.Integer(), nullable=False),
        sa.Column("rows_updated", sa.Integer(), nullable=False),
        sa.Column("rows_unchanged", sa.Integer(), nullable=False),
        sa.Column("rows_quarantined", sa.Integer(), nullable=False),
        sa.Column("rows_linked", sa.Integer(), nullable=False),
        sa.Column("rows_unlinked", sa.Integer(), nullable=False),
        sa.Column("unresolved_geography", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("ingest_method_version", sa.String(length=32), nullable=True),
        sa.Column("initiated_by", sa.String(length=160), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "declared_row_count >= 0", name=op.f("ck_import_batch_declared_row_count_not_negative")
        ),
        sa.CheckConstraint(
            "length(artefact_checksum) = 64", name=op.f("ck_import_batch_checksum_is_sha256_hex")
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["mars_core.facility.id"],
            name=op.f("fk_import_batch_facility_id_facility"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batch")),
        sa.UniqueConstraint(
            "source_system", "artefact_checksum", name="uq_import_batch_source_checksum"
        ),
        schema=CORE,
        comment=(
            "One inbound artefact and its lifecycle. Holds no direct identifier: identity is "
            "consumed inside the identity boundary and never reaches this schema."
        ),
    )
    op.create_index(
        "ix_import_batch_facility", "import_batch", ["facility_id"], unique=False, schema=CORE
    )
    op.create_index(
        "ix_import_batch_received", "import_batch", ["received_at"], unique=False, schema=CORE
    )
    op.create_index(
        "ix_import_batch_status", "import_batch", ["import_status"], unique=False, schema=CORE
    )
    op.create_table(
        "import_source_row",
        sa.Column("import_batch_id", sa.UUID(), nullable=False),
        sa.Column("source_row_reference", sa.String(length=128), nullable=False),
        sa.Column("source_line_number", sa.Integer(), nullable=True),
        sa.Column(
            "outcome",
            postgresql.ENUM(
                "loaded",
                "unchanged",
                "updated",
                "quarantined",
                name="source_row_outcome",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("opd_encounter_id", sa.UUID(), nullable=True),
        sa.Column("payload_redacted", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload_checksum", sa.String(length=64), nullable=True),
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
            ["import_batch_id"],
            ["mars_core.import_batch.id"],
            name=op.f("fk_import_source_row_import_batch_id_import_batch"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opd_encounter_id"],
            ["mars_core.opd_encounter.id"],
            name=op.f("fk_import_source_row_opd_encounter_id_opd_encounter"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_source_row")),
        sa.UniqueConstraint(
            "import_batch_id", "source_row_reference", name="uq_import_source_row_batch_reference"
        ),
        schema=CORE,
        comment=(
            "One inbound row and its outcome. payload_redacted has the identity object removed "
            "rather than masked: a masked value is still a value, and this table is not the vault."
        ),
    )
    op.create_index(
        "ix_import_source_row_batch",
        "import_source_row",
        ["import_batch_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_import_source_row_encounter",
        "import_source_row",
        ["opd_encounter_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_import_source_row_outcome",
        "import_source_row",
        ["import_batch_id", "outcome"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "import_stage_execution",
        sa.Column("import_batch_id", sa.UUID(), nullable=False),
        sa.Column(
            "stage",
            postgresql.ENUM(
                "read",
                "validate",
                "link_identity",
                "write_canonical",
                name="import_stage",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_in", sa.Integer(), nullable=False),
        sa.Column("rows_out", sa.Integer(), nullable=False),
        sa.Column("rows_rejected", sa.Integer(), nullable=False),
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
            ["import_batch_id"],
            ["mars_core.import_batch.id"],
            name=op.f("fk_import_stage_execution_import_batch_id_import_batch"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_stage_execution")),
        sa.UniqueConstraint(
            "import_batch_id", "stage", name="uq_import_stage_execution_batch_stage"
        ),
        schema=CORE,
    )
    op.create_index(
        "ix_import_stage_execution_batch",
        "import_stage_execution",
        ["import_batch_id"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "import_validation_issue",
        sa.Column("import_batch_id", sa.UUID(), nullable=False),
        sa.Column("import_source_row_id", sa.UUID(), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "severity",
            postgresql.ENUM(
                "warning",
                "error",
                "fatal",
                name="validation_severity",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("field_path", sa.String(length=160), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            ["import_batch_id"],
            ["mars_core.import_batch.id"],
            name=op.f("fk_import_validation_issue_import_batch_id_import_batch"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_source_row_id"],
            ["mars_core.import_source_row.id"],
            # Named explicitly rather than by convention: the generated name is
            # 65 characters and PostgreSQL truncates silently at 63.
            name="fk_import_issue_source_row",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_validation_issue")),
        schema=CORE,
        comment=(
            "Validation findings. Messages are written to be safe to display: they name the field "
            "and the unrecognised code, never a patient value."
        ),
    )
    op.create_index(
        "ix_import_validation_issue_batch",
        "import_validation_issue",
        ["import_batch_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_import_validation_issue_code",
        "import_validation_issue",
        ["import_batch_id", "code"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_import_validation_issue_row",
        "import_validation_issue",
        ["import_source_row_id"],
        unique=False,
        schema=CORE,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        "ix_import_validation_issue_row", table_name="import_validation_issue", schema=CORE
    )
    op.drop_index(
        "ix_import_validation_issue_code", table_name="import_validation_issue", schema=CORE
    )
    op.drop_index(
        "ix_import_validation_issue_batch", table_name="import_validation_issue", schema=CORE
    )
    op.drop_table("import_validation_issue", schema=CORE)
    op.drop_index(
        "ix_import_stage_execution_batch", table_name="import_stage_execution", schema=CORE
    )
    op.drop_table("import_stage_execution", schema=CORE)
    op.drop_index("ix_import_source_row_outcome", table_name="import_source_row", schema=CORE)
    op.drop_index("ix_import_source_row_encounter", table_name="import_source_row", schema=CORE)
    op.drop_index("ix_import_source_row_batch", table_name="import_source_row", schema=CORE)
    op.drop_table("import_source_row", schema=CORE)
    op.drop_index("ix_import_batch_status", table_name="import_batch", schema=CORE)
    op.drop_index("ix_import_batch_received", table_name="import_batch", schema=CORE)
    op.drop_index("ix_import_batch_facility", table_name="import_batch", schema=CORE)
    op.drop_table("import_batch", schema=CORE)

    # One call per type: the migration guard counts creates against drops
    # in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="validation_severity", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="source_row_outcome", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="import_stage", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="import_batch_status", schema=CORE).drop(bind, checkfirst=True)
