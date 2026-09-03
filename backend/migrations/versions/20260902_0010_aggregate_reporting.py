"""HMIS 033b and 105 aggregate reporting.

Revision ID: 0010_aggregate_reporting
Revises: 0009_ingestion_lifecycle
Created: 2026-09-02

Five tables:

``aggregate_submission``          one facility's return of one form for one period
``aggregate_observation``         one cell of that form
``commodity_stock_observation``   one commodity's stock position
``laboratory_test_observation``   one laboratory test row: done and positive
``reconciliation_finding``        reported set beside derived, both kept

Three decisions are carried by the schema rather than by application code:

* **A blank cell is not a zero.** Every value column is nullable. HMIS 033b
  instruction 7 requires reporting "whether there are cases or not", so a
  reported zero is a statement and a blank is a missing statement.

* **A correction does not overwrite.** ``revision`` is part of the submission's
  unique key and ``supersedes_id`` links the chain, so the figures a district
  acted on remain readable after the week is corrected.

* **Arithmetic impossibilities are refused.** Negative counts, more positives
  than tests, a "weekly" period spanning a quarter. Each is a transcription
  error rather than an unusual month, and each is a check constraint.

The ``sex`` enum is **not** created here: migration 0005 created it for the
encounter model. It is referenced with ``create_type=False`` and is not dropped
on downgrade.

The forms these tables serve are transcribed in
``mars.domain.hmis_elements``; the model is documented in
``docs/data-dictionary/hmis-aggregate.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_aggregate_reporting"
down_revision: str | None = "0009_ingestion_lifecycle"
CORE = "mars_core"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # Prompt 9's lifecycle is shared by encounter and aggregate ingestion.
    # Domain belongs in the artefact identity so identical bytes offered to two
    # different contracts cannot alias the same batch.
    op.drop_constraint("uq_import_batch_source_checksum", "import_batch", schema=CORE)
    # The server default exists only to backfill the rows already present when
    # this column becomes NOT NULL. It is dropped immediately afterwards: this
    # column is part of the batch's identity, so a row that omits it must fail
    # loudly rather than silently become an encounter batch and alias one.
    op.add_column(
        "import_batch",
        sa.Column(
            "import_domain",
            sa.String(length=24),
            server_default=sa.text("'encounter'"),
            nullable=False,
        ),
        schema=CORE,
    )
    op.alter_column("import_batch", "import_domain", server_default=None, schema=CORE)
    op.create_check_constraint(
        "import_domain_known",
        "import_batch",
        "import_domain IN ('encounter', 'aggregate')",
        schema=CORE,
    )
    op.create_unique_constraint(
        "uq_import_batch_domain_source_checksum",
        "import_batch",
        ["import_domain", "source_system", "artefact_checksum"],
        schema=CORE,
    )

    # -- Enum types -------------------------------------------------------
    # 'sex' is deliberately absent: migration 0005 created it for the
    # encounter model. Creating it again would fail; dropping it on
    # downgrade would break a table this migration never touched.
    postgresql.ENUM(
        "hmis_033b",
        "hmis_105",
        name="aggregate_form",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "week",
        "month",
        name="aggregate_period_type",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "received",
        "validated",
        "quarantined",
        "accepted",
        "superseded",
        name="aggregate_submission_status",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "days_0_28",
        "days_29_to_years_4",
        "years_5_9",
        "years_10_19",
        "years_20_plus",
        "unspecified",
        name="age_band",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "quantity_consumed",
        "days_out_of_stock",
        "stock_on_hand",
        "quantity_expired",
        name="stock_metric",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "matched",
        "within_tolerance",
        "differs",
        "reported_only",
        "derived_only",
        "uncomparable",
        name="reconciliation_status",
        schema=CORE,
    ).create(bind, checkfirst=True)

    op.create_table(
        "aggregate_submission",
        sa.Column("facility_id", sa.UUID(), nullable=False),
        sa.Column(
            "form",
            postgresql.ENUM(
                "hmis_033b",
                "hmis_105",
                name="aggregate_form",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "period_type",
            postgresql.ENUM(
                "week",
                "month",
                name="aggregate_period_type",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("period_label_raw", sa.String(length=32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "submission_status",
            postgresql.ENUM(
                "received",
                "validated",
                "quarantined",
                "accepted",
                "superseded",
                name="aggregate_submission_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.String(length=128), nullable=True),
        sa.Column("source_batch_id", sa.UUID(), nullable=True),
        sa.Column("ingest_method_version", sa.String(length=32), nullable=True),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_on", sa.Date(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
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
            "(period_type = 'week' AND period_end - period_start = 6) OR "
            "(period_type = 'month' AND period_end - period_start BETWEEN 27 AND 30)",
            name=op.f("ck_aggregate_submission_period_length_matches_type"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_aggregate_submission_period_ordered")
        ),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_aggregate_submission_revision_is_positive")
        ),
        sa.CheckConstraint(
            "(form = 'hmis_033b' AND period_type = 'week') OR "
            "(form = 'hmis_105' AND period_type = 'month')",
            name=op.f("ck_aggregate_submission_form_matches_period_type"),
        ),
        sa.CheckConstraint(
            "period_type <> 'week' OR EXTRACT(ISODOW FROM period_start) = 1",
            name=op.f("ck_aggregate_submission_week_starts_monday"),
        ),
        sa.CheckConstraint(
            "period_type <> 'month' OR ("
            "EXTRACT(DAY FROM period_start) = 1 AND "
            "period_end = (period_start + INTERVAL '1 month' - INTERVAL '1 day')::date)",
            name=op.f("ck_aggregate_submission_month_is_calendar_month"),
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64",
            name=op.f("ck_aggregate_submission_payload_checksum_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["mars_core.facility.id"],
            name=op.f("fk_aggregate_submission_facility_id_facility"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["mars_core.aggregate_submission.id"],
            name="fk_aggregate_submission_supersedes",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["mars_core.import_batch.id"],
            name="fk_aggregate_batch",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_aggregate_submission")),
        sa.UniqueConstraint(
            "facility_id",
            "form",
            "period_start",
            "period_end",
            "revision",
            name="uq_aggregate_submission_facility_form_period_revision",
        ),
        schema=CORE,
        comment=(
            "One facility's return of one HMIS form for one period. A correction is a new "
            "revision; the superseded one is kept."
        ),
    )
    op.create_index(
        "ix_aggregate_submission_facility_period",
        "aggregate_submission",
        ["facility_id", "period_start"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_aggregate_submission_form_period",
        "aggregate_submission",
        ["form", "period_start"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_aggregate_submission_status",
        "aggregate_submission",
        ["submission_status"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "uq_aggregate_submission_one_accepted",
        "aggregate_submission",
        ["facility_id", "form", "period_start", "period_end"],
        unique=True,
        schema=CORE,
        postgresql_where=sa.text("submission_status = 'accepted'"),
    )
    op.create_table(
        "aggregate_observation",
        sa.Column("aggregate_submission_id", sa.UUID(), nullable=False),
        sa.Column("element_code", sa.String(length=48), nullable=False),
        sa.Column(
            "age_band",
            postgresql.ENUM(
                "days_0_28",
                "days_29_to_years_4",
                "years_5_9",
                "years_10_19",
                "years_20_plus",
                "unspecified",
                name="age_band",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "sex",
            postgresql.ENUM(
                "male",
                "female",
                "unknown",
                name="sex",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.Integer(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
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
            "value IS NULL OR value >= 0", name=op.f("ck_aggregate_observation_value_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_submission_id"],
            ["mars_core.aggregate_submission.id"],
            name="fk_aggregate_observation_submission",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_aggregate_observation")),
        sa.UniqueConstraint(
            "aggregate_submission_id",
            "element_code",
            "age_band",
            "sex",
            name="uq_aggregate_observation_submission_element_band_sex",
        ),
        schema=CORE,
        comment=(
            "One cell of one form. A NULL value means the cell was blank; zero means the facility "
            "reported a zero. HMIS 033b requires zero reporting, so the two are different facts."
        ),
    )
    op.create_index(
        "ix_aggregate_observation_element",
        "aggregate_observation",
        ["element_code"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_aggregate_observation_submission",
        "aggregate_observation",
        ["aggregate_submission_id"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "commodity_stock_observation",
        sa.Column("aggregate_submission_id", sa.UUID(), nullable=False),
        sa.Column("commodity_code", sa.String(length=48), nullable=False),
        sa.Column(
            "metric",
            postgresql.ENUM(
                "quantity_consumed",
                "days_out_of_stock",
                "stock_on_hand",
                "quantity_expired",
                name="stock_metric",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("unit_of_issue", sa.String(length=64), nullable=True),
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
            "metric <> 'days_out_of_stock' OR value IS NULL OR value <= 31",
            name=op.f("ck_commodity_stock_observation_days_out_of_stock_within_a_month"),
        ),
        sa.CheckConstraint(
            "value IS NULL OR value >= 0",
            name=op.f("ck_commodity_stock_observation_value_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_submission_id"],
            ["mars_core.aggregate_submission.id"],
            name="fk_commodity_stock_submission",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commodity_stock_observation")),
        sa.UniqueConstraint(
            "aggregate_submission_id",
            "commodity_code",
            "metric",
            name="uq_commodity_stock_submission_commodity_metric",
        ),
        schema=CORE,
    )
    op.create_index(
        "ix_commodity_stock_commodity",
        "commodity_stock_observation",
        ["commodity_code"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_commodity_stock_submission",
        "commodity_stock_observation",
        ["aggregate_submission_id"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "laboratory_test_observation",
        sa.Column("aggregate_submission_id", sa.UUID(), nullable=False),
        sa.Column("test_code", sa.String(length=48), nullable=False),
        sa.Column("number_done", sa.Integer(), nullable=True),
        sa.Column("number_positive", sa.Integer(), nullable=True),
        sa.Column("raw_done", sa.Text(), nullable=True),
        sa.Column("raw_positive", sa.Text(), nullable=True),
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
            "number_done IS NULL OR number_done >= 0",
            name=op.f("ck_laboratory_test_observation_done_not_negative"),
        ),
        sa.CheckConstraint(
            "number_done IS NULL OR number_positive IS NULL OR number_positive <= number_done",
            name=op.f("ck_laboratory_test_observation_positive_not_above_done"),
        ),
        sa.CheckConstraint(
            "number_positive IS NULL OR number_positive >= 0",
            name=op.f("ck_laboratory_test_observation_positive_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_submission_id"],
            ["mars_core.aggregate_submission.id"],
            name="fk_laboratory_test_submission",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_laboratory_test_observation")),
        sa.UniqueConstraint(
            "aggregate_submission_id", "test_code", name="uq_laboratory_test_submission_test"
        ),
        schema=CORE,
    )
    op.create_index(
        "ix_laboratory_test_code",
        "laboratory_test_observation",
        ["test_code"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_laboratory_test_submission",
        "laboratory_test_observation",
        ["aggregate_submission_id"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "reconciliation_finding",
        sa.Column("aggregate_submission_id", sa.UUID(), nullable=False),
        sa.Column("element_code", sa.String(length=48), nullable=False),
        sa.Column(
            "reconciliation_status",
            postgresql.ENUM(
                "matched",
                "within_tolerance",
                "differs",
                "reported_only",
                "derived_only",
                "uncomparable",
                name="reconciliation_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("reported_value", sa.Integer(), nullable=True),
        sa.Column("derived_value", sa.Integer(), nullable=True),
        sa.Column("difference", sa.Integer(), nullable=True),
        sa.Column("derived_denominator", sa.Integer(), nullable=True),
        sa.Column("method_version", sa.String(length=32), nullable=False),
        sa.Column("input_checksum", sa.String(length=64), nullable=False),
        sa.Column("absolute_tolerance", sa.Integer(), nullable=False),
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
            ["aggregate_submission_id"],
            ["mars_core.aggregate_submission.id"],
            name="fk_reconciliation_submission",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_finding")),
        sa.CheckConstraint(
            "length(input_checksum) = 64",
            name=op.f("ck_reconciliation_finding_input_checksum_is_sha256"),
        ),
        sa.CheckConstraint(
            "absolute_tolerance >= 0",
            name=op.f("ck_reconciliation_finding_tolerance_not_negative"),
        ),
        sa.UniqueConstraint(
            "aggregate_submission_id",
            "element_code",
            "method_version",
            "input_checksum",
            "absolute_tolerance",
            name="uq_reconciliation_submission_element_method_input",
        ),
        schema=CORE,
        comment=(
            "Reported against derived. Both values are kept and neither is corrected: the "
            "difference is the finding."
        ),
    )
    op.create_index(
        "ix_reconciliation_status",
        "reconciliation_finding",
        ["reconciliation_status"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_reconciliation_submission",
        "reconciliation_finding",
        ["aggregate_submission_id"],
        unique=False,
        schema=CORE,
    )

    # A lifecycle source row may produce either an encounter (Prompt 9) or an
    # aggregate submission (this migration), never an untraceable write.
    op.add_column(
        "import_source_row",
        sa.Column("aggregate_submission_id", sa.UUID(), nullable=True),
        schema=CORE,
    )
    op.create_foreign_key(
        "fk_import_row_aggregate_submission",
        "import_source_row",
        "aggregate_submission",
        ["aggregate_submission_id"],
        ["id"],
        source_schema=CORE,
        referent_schema=CORE,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_import_source_row_aggregate",
        "import_source_row",
        ["aggregate_submission_id"],
        unique=False,
        schema=CORE,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        "ix_import_source_row_aggregate", table_name="import_source_row", schema=CORE
    )
    op.drop_constraint(
        "fk_import_row_aggregate_submission",
        "import_source_row",
        schema=CORE,
        type_="foreignkey",
    )
    op.drop_column("import_source_row", "aggregate_submission_id", schema=CORE)

    op.drop_index("ix_reconciliation_submission", table_name="reconciliation_finding", schema=CORE)
    op.drop_index("ix_reconciliation_status", table_name="reconciliation_finding", schema=CORE)
    op.drop_table("reconciliation_finding", schema=CORE)
    op.drop_index(
        "ix_laboratory_test_submission", table_name="laboratory_test_observation", schema=CORE
    )
    op.drop_index("ix_laboratory_test_code", table_name="laboratory_test_observation", schema=CORE)
    op.drop_table("laboratory_test_observation", schema=CORE)
    op.drop_index(
        "ix_commodity_stock_submission", table_name="commodity_stock_observation", schema=CORE
    )
    op.drop_index(
        "ix_commodity_stock_commodity", table_name="commodity_stock_observation", schema=CORE
    )
    op.drop_table("commodity_stock_observation", schema=CORE)
    op.drop_index(
        "ix_aggregate_observation_submission", table_name="aggregate_observation", schema=CORE
    )
    op.drop_index(
        "ix_aggregate_observation_element", table_name="aggregate_observation", schema=CORE
    )
    op.drop_table("aggregate_observation", schema=CORE)
    op.drop_index("ix_aggregate_submission_status", table_name="aggregate_submission", schema=CORE)
    op.drop_index(
        "uq_aggregate_submission_one_accepted",
        table_name="aggregate_submission",
        schema=CORE,
    )
    op.drop_index(
        "ix_aggregate_submission_form_period", table_name="aggregate_submission", schema=CORE
    )
    op.drop_index(
        "ix_aggregate_submission_facility_period", table_name="aggregate_submission", schema=CORE
    )
    op.drop_table("aggregate_submission", schema=CORE)

    # One call per type: the migration guard counts creates against drops
    # in the source, and a loop would read as a single drop. 'sex' is not
    # dropped - it belongs to migration 0005.
    postgresql.ENUM(name="reconciliation_status", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="stock_metric", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="age_band", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="aggregate_submission_status", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="aggregate_period_type", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="aggregate_form", schema=CORE).drop(bind, checkfirst=True)

    op.drop_constraint(
        "uq_import_batch_domain_source_checksum",
        "import_batch",
        schema=CORE,
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_import_batch_import_domain_known"),
        "import_batch",
        schema=CORE,
        type_="check",
    )
    op.drop_column("import_batch", "import_domain", schema=CORE)
    op.create_unique_constraint(
        "uq_import_batch_source_checksum",
        "import_batch",
        ["source_system", "artefact_checksum"],
        schema=CORE,
    )
