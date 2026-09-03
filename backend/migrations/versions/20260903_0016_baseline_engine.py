"""Historical baseline engine.

Revision ID: 0016_baseline_engine
Revises: 0015_surveillance_domains
Created: 2026-09-03

Two tables in mars_analytics:

``baseline_build``   one run: the governed method, the history it asked for
``baseline_result``  one expected level, and the history behind it

A baseline is the reference an anomaly is measured against, and it is where
surveillance most easily goes wrong. Comparing March against February in Uganda
flags the transmission season rather than an event. Computing an expectation
for a facility that opened two months ago produces a confident number from
nothing. Both mistakes look like working software, which is why the schema
carries the guards rather than the engine alone.

``baseline_build`` exists so a refusal is a record. With no approved temporal
baseline method the run is stored as ``not_configured`` with the missing
parameter names in ``missing_configuration``, and ``refusals_name_what_is_missing``
requires it: an operator seeing no baselines needs a parameter name, not a
shrug. That constraint tests ``jsonb_typeof`` as well as nullity, because a
JSONB column given a Python ``None`` is stored as JSON ``null`` rather than SQL
NULL - and a refusal naming nothing would otherwise pass.
``completed_builds_carry_their_method`` is the other half: a run cannot report
expected values without recording the method that made them.

``baseline_result`` keeps ``value`` meaning what it means everywhere else in
MARS: present exactly when its status says so. Here it is the expected level.
``sufficiency_matches_value_status`` ties it to the history: a series with
fewer usable periods than the approved minimum gets a row saying which kind of
"not enough" applies and no expected value at all. An expectation drawn from
two periods is worse than none, because a district can act on it.

The uncertainty band is nullable and paired. ``band_has_both_ends`` refuses
half a band, which would read as a one-sided limit - a different claim. The
band exists only when a programme has approved an uncertainty multiplier; how
wide an interval should be is a statistical choice MARS does not make on a
programme's behalf.

``dispersion_measure_matches_value`` covers the single-period case. One
historical period has a centre and no spread, and recording that spread as zero
would make the series look perfectly stable.

The enum types ``geography_grain`` and ``period_grain`` (mars_governance) and
``indicator_value_status`` (mars_analytics) belong to migration 0012 and are
referenced, not created.

Documented in ``docs/data-dictionary/baselines.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_baseline_engine"
down_revision: str | None = "0015_surveillance_domains"
ANALYTICS = "mars_analytics"
CORE = "mars_core"
GOVERNANCE = "mars_governance"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # Types created by an earlier migration are referenced with
    # create_type=False and never dropped here: geography_grain, indicator_value_status, period_grain.
    postgresql.ENUM(
        "running",
        "completed",
        "not_configured",
        "failed",
        name="baseline_build_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "indicator",
        "testing_measure",
        "treatment_measure",
        name="baseline_series_kind",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "historical_median",
        "historical_mean",
        "seasonal_period_of_year_median",
        name="baseline_method",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "sufficient",
        "insufficient_history",
        "insufficient_completeness",
        "no_history",
        name="baseline_sufficiency",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "median_absolute_deviation",
        "standard_deviation",
        "none",
        name="dispersion_measure",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "baseline_build",
        sa.Column(
            "build_status",
            postgresql.ENUM(
                "running",
                "completed",
                "not_configured",
                "failed",
                name="baseline_build_status",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "series_kind",
            postgresql.ENUM(
                "indicator",
                "testing_measure",
                "treatment_measure",
                name="baseline_series_kind",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("target_period_start", sa.Date(), nullable=False),
        sa.Column("target_period_end", sa.Date(), nullable=False),
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
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "baseline_method",
            postgresql.ENUM(
                "historical_median",
                "historical_mean",
                "seasonal_period_of_year_median",
                name="baseline_method",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("history_periods", sa.Integer(), nullable=True),
        sa.Column("minimum_history_periods", sa.Integer(), nullable=True),
        sa.Column("minimum_completeness", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("uncertainty_multiplier", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("history_start", sa.Date(), nullable=True),
        sa.Column("history_end", sa.Date(), nullable=True),
        sa.Column("missing_configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("series_evaluated", sa.Integer(), nullable=False),
        sa.Column("results_written", sa.Integer(), nullable=False),
        sa.Column("insufficient_history", sa.Integer(), nullable=False),
        sa.Column("insufficient_completeness", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "build_status <> 'completed' OR (method_version_id IS NOT NULL AND baseline_method IS NOT NULL AND history_periods IS NOT NULL)",
            name=op.f("ck_baseline_build_completed_builds_carry_their_method"),
        ),
        sa.CheckConstraint(
            "build_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name=op.f("ck_baseline_build_refusals_name_what_is_missing"),
        ),
        sa.CheckConstraint(
            "history_periods IS NULL OR history_periods >= 1",
            name=op.f("ck_baseline_build_history_periods_positive"),
        ),
        sa.CheckConstraint(
            "history_start IS NULL OR history_end IS NULL OR history_end >= history_start",
            name=op.f("ck_baseline_build_history_period_ordered"),
        ),
        sa.CheckConstraint(
            "minimum_completeness IS NULL OR (minimum_completeness >= 0 AND minimum_completeness <= 1)",
            name=op.f("ck_baseline_build_minimum_completeness_is_a_proportion"),
        ),
        sa.CheckConstraint(
            "minimum_history_periods IS NULL OR minimum_history_periods >= 1",
            name=op.f("ck_baseline_build_minimum_history_positive"),
        ),
        sa.CheckConstraint(
            "target_period_end >= target_period_start",
            name=op.f("ck_baseline_build_target_period_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name=op.f("fk_baseline_build_method_version_id_method_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_baseline_build")),
        schema=ANALYTICS,
        comment="One baseline run: the governed method in force, the history it asked for, and - when no method is approved - what is missing.",
    )
    op.create_index(
        "ix_baseline_build_status", "baseline_build", ["build_status"], unique=False, schema=ANALYTICS
    )
    op.create_index(
        "ix_baseline_build_target",
        "baseline_build",
        ["target_period_start", "series_kind"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "baseline_result",
        sa.Column("baseline_build_id", sa.UUID(), nullable=False),
        sa.Column(
            "series_kind",
            postgresql.ENUM(
                "indicator",
                "testing_measure",
                "treatment_measure",
                name="baseline_series_kind",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("series_key", sa.String(length=96), nullable=False),
        sa.Column(
            "baseline_method",
            postgresql.ENUM(
                "historical_median",
                "historical_mean",
                "seasonal_period_of_year_median",
                name="baseline_method",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
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
        sa.Column(
            "sufficiency",
            postgresql.ENUM(
                "sufficient",
                "insufficient_history",
                "insufficient_completeness",
                "no_history",
                name="baseline_sufficiency",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "dispersion_measure",
            postgresql.ENUM(
                "median_absolute_deviation",
                "standard_deviation",
                "none",
                name="dispersion_measure",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("dispersion_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("uncertainty_lower", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("uncertainty_upper", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("history_periods_available", sa.Integer(), nullable=False),
        sa.Column("history_periods_used", sa.Integer(), nullable=False),
        sa.Column("history_periods_required", sa.Integer(), nullable=True),
        sa.Column("history_start", sa.Date(), nullable=True),
        sa.Column("history_end", sa.Date(), nullable=True),
        sa.Column("contributing_periods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("excluded_periods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.Column("indicator_version_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contributing_units", sa.Integer(), nullable=True),
        sa.Column("expected_units", sa.Integer(), nullable=True),
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
            "(dispersion_measure = 'none') = (dispersion_value IS NULL)",
            name=op.f("ck_baseline_result_dispersion_measure_matches_value"),
        ),
        sa.CheckConstraint(
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_baseline_result_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "(sufficiency = 'sufficient' AND value_status = 'available') OR (sufficiency <> 'sufficient' AND value_status <> 'available')",
            name=op.f("ck_baseline_result_sufficiency_matches_value_status"),
        ),
        sa.CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_baseline_result_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "(uncertainty_lower IS NULL) = (uncertainty_upper IS NULL)",
            name=op.f("ck_baseline_result_band_has_both_ends"),
        ),
        sa.CheckConstraint(
            "history_periods_used <= history_periods_available",
            name=op.f("ck_baseline_result_used_within_available"),
        ),
        sa.CheckConstraint(
            "history_periods_used >= 0", name=op.f("ck_baseline_result_history_used_not_negative")
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64", name=op.f("ck_baseline_result_fingerprint_is_sha256")
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_baseline_result_period_ordered")
        ),
        sa.CheckConstraint(
            "uncertainty_lower IS NULL OR uncertainty_upper >= uncertainty_lower",
            name=op.f("ck_baseline_result_band_is_ordered"),
        ),
        sa.CheckConstraint(
            "uncertainty_lower IS NULL OR value IS NOT NULL",
            name=op.f("ck_baseline_result_band_requires_an_expectation"),
        ),
        sa.ForeignKeyConstraint(
            ["baseline_build_id"],
            ["mars_analytics.baseline_build.id"],
            name=op.f("fk_baseline_result_baseline_build_id_baseline_build"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_baseline_result")),
        sa.UniqueConstraint(
            "baseline_build_id",
            "series_kind",
            "series_key",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "input_fingerprint",
            name="uq_baseline_result_build_series_scope_input",
        ),
        schema=ANALYTICS,
        comment="One expected level for one series in one place, with the history behind it and what that history was missing.",
    )
    op.create_index(
        "ix_baseline_result_build",
        "baseline_result",
        ["baseline_build_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_baseline_result_facility",
        "baseline_result",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_baseline_result_geography",
        "baseline_result",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_baseline_result_period",
        "baseline_result",
        ["period_start", "period_end"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_baseline_result_series",
        "baseline_result",
        ["series_kind", "series_key", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_baseline_result_series", table_name="baseline_result", schema=ANALYTICS)
    op.drop_index("ix_baseline_result_period", table_name="baseline_result", schema=ANALYTICS)
    op.drop_index("ix_baseline_result_geography", table_name="baseline_result", schema=ANALYTICS)
    op.drop_index("ix_baseline_result_facility", table_name="baseline_result", schema=ANALYTICS)
    op.drop_index("ix_baseline_result_build", table_name="baseline_result", schema=ANALYTICS)
    op.drop_table("baseline_result", schema=ANALYTICS)
    op.drop_index("ix_baseline_build_target", table_name="baseline_build", schema=ANALYTICS)
    op.drop_index("ix_baseline_build_status", table_name="baseline_build", schema=ANALYTICS)
    op.drop_table("baseline_build", schema=ANALYTICS)

    # One call per type: the migration guard counts creates against
    # drops in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="dispersion_measure", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="baseline_sufficiency", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="baseline_method", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="baseline_series_kind", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="baseline_build_status", schema=ANALYTICS).drop(bind, checkfirst=True)
