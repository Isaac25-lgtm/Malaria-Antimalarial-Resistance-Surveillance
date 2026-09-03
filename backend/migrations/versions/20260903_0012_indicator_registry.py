"""Governed indicator registry and materialised results.

Revision ID: 0012_indicator_registry
Revises: 0011_integration_runs
Created: 2026-09-03

Three tables across two schemas:

``mars_governance.indicator_definition``          what a metric is
``mars_governance.indicator_definition_version``  how it is computed, versioned
``mars_analytics.indicator_result``               one materialised figure

The split is deliberate. A definition is governed and durable; a result is
derived and rebuildable, which is what ``mars_analytics`` is for.

Constraints that carry the rules rather than leaving them to application code:

* ``value_present_iff_available`` - a result has a value only when its status
  says one exists. An undefined denominator can never be stored as zero, which
  is the single most consequential confusion in the whole product: a positivity
  of 0.0 and a positivity that could not be computed look identical in a chart
  and are opposite statements about a facility.

* ``facility_id_matches_grain`` - a facility-grain row names a facility and a
  higher-grain row does not. Without it a national row could carry a facility
  id and be double-counted by anything joining on it.

* ``approved_requires_approver`` - the same rule the method registry applies.
  An active definition with nobody's name on it is an ungoverned definition.

* the result uniqueness key includes ``input_fingerprint``, so recomputing over
  unchanged inputs is idempotent while changed inputs write a new row beside
  the old one rather than overwriting a figure someone already acted on.

Enum types ``lifecycle_status`` (mars_governance) is **not** created here:
migration 0002 created it. ``age_band`` and ``sex`` are created in
mars_governance even though mars_core already has types of those names -
PostgreSQL enum types are schema-scoped, so these are distinct types, and the
governance tables reference their own.

No threshold, target or alert level appears anywhere in this migration. What
counts as too high is a programme decision held in the configuration registry.

Documented in ``docs/data-dictionary/indicators.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_indicator_registry"
down_revision: str | None = "0011_integration_runs"
GOVERNANCE = "mars_governance"
ANALYTICS = "mars_analytics"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # 'lifecycle_status' is absent: migration 0002 created it in
    # mars_governance. 'age_band' and 'sex' are created here even though
    # mars_core has types of those names - enum types are schema-scoped,
    # so these are distinct types the governance tables own.
    postgresql.ENUM(
        "routine_surveillance",
        "confirmed_evidence",
        name="evidence_lane",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "day",
        "epidemiological_week",
        "month",
        name="period_grain",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "facility",
        "subcounty",
        "district",
        "national",
        name="geography_grain",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "count",
        "proportion",
        "rate_per_period",
        "days",
        name="indicator_unit",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "encounter",
        "aggregate_weekly",
        "aggregate_monthly",
        "commodity",
        "laboratory",
        "reporting_metadata",
        name="indicator_source_domain",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "days_0_28",
        "days_29_to_years_4",
        "years_5_9",
        "years_10_19",
        "years_20_plus",
        "unspecified",
        name="age_band",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "male",
        "female",
        "unknown",
        name="sex",
        schema=GOVERNANCE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "available",
        "unavailable_no_denominator",
        "unavailable_insufficient_data",
        "suppressed",
        name="indicator_value_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "indicator_definition",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM(
                "count",
                "proportion",
                "rate_per_period",
                "days",
                name="indicator_unit",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "source_domain",
            postgresql.ENUM(
                "encounter",
                "aggregate_weekly",
                "aggregate_monthly",
                "commodity",
                "laboratory",
                "reporting_metadata",
                name="indicator_source_domain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "period_grain",
            postgresql.ENUM(
                "day",
                "epidemiological_week",
                "month",
                name="period_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "base_geography_grain",
            postgresql.ENUM(
                "facility",
                "subcounty",
                "district",
                "national",
                name="geography_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "evidence_lane",
            postgresql.ENUM(
                "routine_surveillance",
                "confirmed_evidence",
                name="evidence_lane",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("definition_source", sa.Text(), nullable=False),
        sa.Column("method_definition_id", sa.UUID(), nullable=True),
        sa.Column("owner", sa.String(length=128), nullable=True),
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
            ["method_definition_id"],
            ["mars_governance.method_definition.id"],
            name="fk_indicator_method_definition",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indicator_definition")),
        sa.UniqueConstraint("code", name="uq_indicator_definition_code"),
        schema=GOVERNANCE,
        comment="What a metric is. Carries no threshold: what counts as too high is a programme decision held in the configuration registry, not a property of the definition.",
    )
    op.create_index(
        "ix_indicator_definition_domain",
        "indicator_definition",
        ["source_domain"],
        unique=False,
        schema=GOVERNANCE,
    )
    op.create_index(
        "ix_indicator_definition_lane",
        "indicator_definition",
        ["evidence_lane"],
        unique=False,
        schema=GOVERNANCE,
    )
    op.create_table(
        "indicator_definition_version",
        sa.Column("indicator_definition_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("semantic_version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "in_review",
                "approved",
                "active",
                "retired",
                name="lifecycle_status",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "numerator_specification", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "denominator_specification", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("blank_handling", sa.Text(), nullable=False),
        sa.Column("exclusion_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("permitted_dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("specification_checksum", sa.String(length=64), nullable=False),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("reason_for_change", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "status NOT IN ('approved', 'active') OR approved_by IS NOT NULL",
            name=op.f("ck_indicator_definition_version_approved_requires_approver"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name=op.f("ck_indicator_definition_version_effective_range_ordered"),
        ),
        sa.CheckConstraint(
            "length(specification_checksum) = 64",
            name=op.f("ck_indicator_definition_version_checksum_is_sha256"),
        ),
        sa.CheckConstraint(
            "version_number >= 1",
            name=op.f("ck_indicator_definition_version_version_number_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["indicator_definition_id"],
            ["mars_governance.indicator_definition.id"],
            name="fk_indicator_version_definition",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name="fk_indicator_version_method",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indicator_definition_version")),
        sa.UniqueConstraint(
            "indicator_definition_id",
            "version_number",
            name="uq_indicator_version_definition_number",
        ),
        schema=GOVERNANCE,
    )
    op.create_index(
        "ix_indicator_version_status",
        "indicator_definition_version",
        ["indicator_definition_id", "status"],
        unique=False,
        schema=GOVERNANCE,
    )
    op.create_table(
        "indicator_result",
        sa.Column("indicator_version_id", sa.UUID(), nullable=False),
        sa.Column("indicator_code", sa.String(length=64), nullable=False),
        sa.Column(
            "geography_grain",
            postgresql.ENUM(
                "facility",
                "subcounty",
                "district",
                "national",
                name="geography_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "period_grain",
            postgresql.ENUM(
                "day",
                "epidemiological_week",
                "month",
                name="period_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
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
                schema=GOVERNANCE,
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
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("numerator", sa.Integer(), nullable=True),
        sa.Column("denominator", sa.Integer(), nullable=True),
        sa.Column("value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "value_status",
            postgresql.ENUM(
                "available",
                "unavailable_no_denominator",
                "unavailable_insufficient_data",
                "suppressed",
                name="indicator_value_status",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("contributing_units", sa.Integer(), nullable=True),
        sa.Column("expected_units", sa.Integer(), nullable=True),
        sa.Column("missing_inputs", sa.Integer(), nullable=True),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_indicator_result_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_indicator_result_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator >= 0",
            name=op.f("ck_indicator_result_denominator_not_negative"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64", name=op.f("ck_indicator_result_fingerprint_is_sha256")
        ),
        sa.CheckConstraint(
            "numerator IS NULL OR numerator >= 0",
            name=op.f("ck_indicator_result_numerator_not_negative"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_indicator_result_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["indicator_version_id"],
            ["mars_governance.indicator_definition_version.id"],
            name="fk_indicator_result_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indicator_result")),
        sa.UniqueConstraint(
            "indicator_version_id",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "age_band",
            "sex",
            "input_fingerprint",
            name="uq_indicator_result_version_grain_period_dims_input",
        ),
        schema=ANALYTICS,
        comment="Materialised indicator values. Immutable: a recomputation writes a new row keyed by its input fingerprint. An undefined denominator yields no value, never zero.",
    )
    op.create_index(
        "ix_indicator_result_facility",
        "indicator_result",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_indicator_result_geography",
        "indicator_result",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_indicator_result_status",
        "indicator_result",
        ["value_status"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_indicator_result_version_period",
        "indicator_result",
        ["indicator_version_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        "ix_indicator_result_version_period", table_name="indicator_result", schema=ANALYTICS
    )
    op.drop_index("ix_indicator_result_status", table_name="indicator_result", schema=ANALYTICS)
    op.drop_index("ix_indicator_result_geography", table_name="indicator_result", schema=ANALYTICS)
    op.drop_index("ix_indicator_result_facility", table_name="indicator_result", schema=ANALYTICS)
    op.drop_table("indicator_result", schema=ANALYTICS)
    op.drop_index(
        "ix_indicator_version_status", table_name="indicator_definition_version", schema=GOVERNANCE
    )
    op.drop_table("indicator_definition_version", schema=GOVERNANCE)
    op.drop_index(
        "ix_indicator_definition_lane", table_name="indicator_definition", schema=GOVERNANCE
    )
    op.drop_index(
        "ix_indicator_definition_domain", table_name="indicator_definition", schema=GOVERNANCE
    )
    op.drop_table("indicator_definition", schema=GOVERNANCE)

    # One call per type: the migration guard counts creates against drops
    # in the source. 'lifecycle_status' is not dropped - it belongs to 0002.
    postgresql.ENUM(name="indicator_value_status", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="sex", schema=GOVERNANCE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="age_band", schema=GOVERNANCE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="indicator_source_domain", schema=GOVERNANCE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="indicator_unit", schema=GOVERNANCE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="geography_grain", schema=GOVERNANCE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="period_grain", schema=GOVERNANCE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="evidence_lane", schema=GOVERNANCE).drop(bind, checkfirst=True)
