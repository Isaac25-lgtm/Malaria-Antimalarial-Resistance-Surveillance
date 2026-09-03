"""Temporal anomaly and persistence.

Revision ID: 0017_anomaly_engine
Revises: 0016_baseline_engine
Created: 2026-09-03

Three tables in mars_analytics:

``anomaly_build``            one detection run, or its refusal
``temporal_anomaly_result``  one observation judged against one baseline
``anomaly_persistence``      an unbroken run of flagged periods

The constraints here exist to protect one distinction: between *nothing was
unusual* and *nothing could be judged*. A surveillance system that stores the
second as the first is quietly useless, and looks identical from the outside.

``not_flagged_means_evaluated`` is the load-bearing one. A row may only say
"not flagged" if it carries a deviation and cites the baseline that produced
it. Everything MARS could not judge - no observation, no baseline, too few
cases, no case count, a method that cannot be applied to the baseline available
- goes to its own outcome and keeps its reason. A district reading a quiet map
is entitled to know which quiet it is looking at.

``a_flag_carries_its_evidence`` requires the other direction: a flag cites a
baseline result, the method version that judged it, and the threshold applied.
The threshold is copied onto the row rather than joined at read time, because a
later change to the rule must not silently rewrite what a past detection meant.

``no_baseline_means_no_expectation`` stops an expectation appearing on a row
that had nothing to compare against, and ``a_deviation_needs_an_expectation``
stops a deviation appearing without one.

``anomaly_persistence`` splits arithmetic from judgement into two columns.
``consecutive_periods`` counts; ``is_sustained`` labels, and
``sustained_requires_configuration`` keeps it null until a programme approves
how many periods make a run sustained. Presenting a one-period spike and a
six-month rise identically is how alert fatigue starts, but deciding where the
line falls is not MARS's decision.

``refusals_name_what_is_missing`` tests ``jsonb_typeof`` as well as nullity, for
the reason migration 0016 records: a JSONB column given a Python ``None`` is
stored as JSON ``null`` rather than SQL NULL.

The enum types ``geography_grain`` and ``period_grain`` (mars_governance),
``baseline_series_kind`` (mars_analytics, migration 0016) are referenced, not
created.

Documented in ``docs/data-dictionary/anomalies.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_anomaly_engine"
down_revision: str | None = "0016_baseline_engine"
ANALYTICS = "mars_analytics"
CORE = "mars_core"
GOVERNANCE = "mars_governance"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # Types created by an earlier migration are referenced with
    # create_type=False and never dropped here: baseline_series_kind, geography_grain, period_grain.
    postgresql.ENUM(
        "running",
        "completed",
        "not_configured",
        "failed",
        name="anomaly_build_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "robust_z_score",
        "relative_deviation",
        "exceeds_uncertainty_band",
        name="anomaly_detection_method",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "increase",
        "decrease",
        "unchanged",
        name="anomaly_direction",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "flagged",
        "not_flagged",
        "not_evaluated_no_observation",
        "not_evaluated_no_baseline",
        "not_evaluated_below_minimum_count",
        "not_evaluated_count_unknown",
        "not_evaluated_method_inapplicable",
        name="anomaly_outcome",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "anomaly_persistence",
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
        sa.Column("first_period_start", sa.Date(), nullable=False),
        sa.Column("last_period_end", sa.Date(), nullable=False),
        sa.Column("consecutive_periods", sa.Integer(), nullable=False),
        sa.Column("is_sustained", sa.Boolean(), nullable=True),
        sa.Column("persistence_periods", sa.Integer(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "contributing_result_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
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
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_anomaly_persistence_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "consecutive_periods >= 1",
            name=op.f("ck_anomaly_persistence_a_run_has_at_least_one_period"),
        ),
        sa.CheckConstraint(
            "is_sustained IS NULL OR (method_version_id IS NOT NULL AND persistence_periods IS NOT NULL)",
            name=op.f("ck_anomaly_persistence_sustained_requires_configuration"),
        ),
        sa.CheckConstraint(
            "last_period_end >= first_period_start",
            name=op.f("ck_anomaly_persistence_run_period_ordered"),
        ),
        sa.CheckConstraint(
            "persistence_periods IS NULL OR persistence_periods >= 1",
            name=op.f("ck_anomaly_persistence_persistence_periods_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name=op.f("fk_anomaly_persistence_method_version_id_method_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomaly_persistence")),
        sa.UniqueConstraint(
            "series_kind",
            "series_key",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "first_period_start",
            name="uq_anomaly_persistence_series_scope_start",
        ),
        schema=ANALYTICS,
        comment="An unbroken run of flagged periods. The count is arithmetic; calling it sustained requires an approved rule.",
    )
    op.create_index(
        "ix_anomaly_persistence_last_seen",
        "anomaly_persistence",
        ["last_period_end"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_anomaly_persistence_series",
        "anomaly_persistence",
        ["series_kind", "series_key"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "anomaly_build",
        sa.Column(
            "build_status",
            postgresql.ENUM(
                "running",
                "completed",
                "not_configured",
                "failed",
                name="anomaly_build_status",
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
        sa.Column("baseline_build_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "detection_method",
            postgresql.ENUM(
                "robust_z_score",
                "relative_deviation",
                "exceeds_uncertainty_band",
                name="anomaly_detection_method",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("deviation_threshold", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("minimum_case_count", sa.Integer(), nullable=True),
        sa.Column("persistence_periods", sa.Integer(), nullable=True),
        sa.Column("missing_configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("observations_examined", sa.Integer(), nullable=False),
        sa.Column("flagged", sa.Integer(), nullable=False),
        sa.Column("not_flagged", sa.Integer(), nullable=False),
        sa.Column("not_evaluated", sa.Integer(), nullable=False),
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
            "build_status <> 'completed' OR (method_version_id IS NOT NULL AND detection_method IS NOT NULL AND deviation_threshold IS NOT NULL)",
            name=op.f("ck_anomaly_build_completed_runs_carry_their_rule"),
        ),
        sa.CheckConstraint(
            "build_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND jsonb_typeof(missing_configuration) = 'object')",
            name=op.f("ck_anomaly_build_refusals_name_what_is_missing"),
        ),
        sa.CheckConstraint(
            "minimum_case_count IS NULL OR minimum_case_count >= 0",
            name=op.f("ck_anomaly_build_minimum_case_count_not_negative"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_anomaly_build_period_ordered")
        ),
        sa.CheckConstraint(
            "persistence_periods IS NULL OR persistence_periods >= 1",
            name=op.f("ck_anomaly_build_persistence_periods_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["baseline_build_id"],
            ["mars_analytics.baseline_build.id"],
            name=op.f("fk_anomaly_build_baseline_build_id_baseline_build"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name=op.f("fk_anomaly_build_method_version_id_method_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomaly_build")),
        schema=ANALYTICS,
        comment="One anomaly detection run: the governed rule in force, or - when none is approved - which parameters are missing.",
    )
    op.create_index(
        "ix_anomaly_build_period",
        "anomaly_build",
        ["period_start", "series_kind"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_anomaly_build_status", "anomaly_build", ["build_status"], unique=False, schema=ANALYTICS
    )
    op.create_table(
        "temporal_anomaly_result",
        sa.Column("anomaly_build_id", sa.UUID(), nullable=False),
        sa.Column("baseline_result_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
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
            "outcome",
            postgresql.ENUM(
                "flagged",
                "not_flagged",
                "not_evaluated_no_observation",
                "not_evaluated_no_baseline",
                "not_evaluated_below_minimum_count",
                "not_evaluated_count_unknown",
                "not_evaluated_method_inapplicable",
                name="anomaly_outcome",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "direction",
            postgresql.ENUM(
                "increase",
                "decrease",
                "unchanged",
                name="anomaly_direction",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "detection_method",
            postgresql.ENUM(
                "robust_z_score",
                "relative_deviation",
                "exceeds_uncertainty_band",
                name="anomaly_detection_method",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("observed_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("expected_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("absolute_deviation", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("relative_deviation", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("deviation_score", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("uncertainty_lower", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("uncertainty_upper", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("deviation_threshold", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=True),
        sa.Column("minimum_case_count", sa.Integer(), nullable=True),
        sa.Column("history_periods_used", sa.Integer(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_temporal_anomaly_result_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "outcome <> 'flagged' OR (baseline_result_id IS NOT NULL AND method_version_id IS NOT NULL AND deviation_threshold IS NOT NULL)",
            name=op.f("ck_temporal_anomaly_result_a_flag_carries_its_evidence"),
        ),
        sa.CheckConstraint(
            "outcome <> 'flagged' OR absolute_deviation IS NOT NULL",
            name=op.f("ck_temporal_anomaly_result_a_flag_has_a_deviation"),
        ),
        sa.CheckConstraint(
            "outcome <> 'not_evaluated_no_baseline' OR (expected_value IS NULL AND absolute_deviation IS NULL AND baseline_result_id IS NULL)",
            name=op.f("ck_temporal_anomaly_result_no_baseline_means_no_expectation"),
        ),
        sa.CheckConstraint(
            "outcome <> 'not_flagged' OR (absolute_deviation IS NOT NULL AND baseline_result_id IS NOT NULL)",
            name=op.f("ck_temporal_anomaly_result_not_flagged_means_evaluated"),
        ),
        sa.CheckConstraint(
            "(uncertainty_lower IS NULL) = (uncertainty_upper IS NULL)",
            name=op.f("ck_temporal_anomaly_result_band_has_both_ends"),
        ),
        sa.CheckConstraint(
            "absolute_deviation IS NULL OR expected_value IS NOT NULL",
            name=op.f("ck_temporal_anomaly_result_a_deviation_needs_an_expectation"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_temporal_anomaly_result_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_temporal_anomaly_result_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["anomaly_build_id"],
            ["mars_analytics.anomaly_build.id"],
            name=op.f("fk_temporal_anomaly_result_anomaly_build_id_anomaly_build"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_result_id"],
            ["mars_analytics.baseline_result.id"],
            name=op.f("fk_temporal_anomaly_result_baseline_result_id_baseline_result"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name=op.f("fk_temporal_anomaly_result_method_version_id_method_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_temporal_anomaly_result")),
        sa.UniqueConstraint(
            "anomaly_build_id",
            "series_kind",
            "series_key",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "input_fingerprint",
            name="uq_temporal_anomaly_build_series_scope_input",
        ),
        schema=ANALYTICS,
        comment="One observation judged against one baseline, including the observations MARS could not judge and why.",
    )
    op.create_index(
        "ix_temporal_anomaly_build",
        "temporal_anomaly_result",
        ["anomaly_build_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_temporal_anomaly_facility",
        "temporal_anomaly_result",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_temporal_anomaly_outcome",
        "temporal_anomaly_result",
        ["outcome", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_temporal_anomaly_series",
        "temporal_anomaly_result",
        ["series_kind", "series_key", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        "ix_temporal_anomaly_series", table_name="temporal_anomaly_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_temporal_anomaly_outcome", table_name="temporal_anomaly_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_temporal_anomaly_facility", table_name="temporal_anomaly_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_temporal_anomaly_build", table_name="temporal_anomaly_result", schema=ANALYTICS
    )
    op.drop_table("temporal_anomaly_result", schema=ANALYTICS)
    op.drop_index("ix_anomaly_build_status", table_name="anomaly_build", schema=ANALYTICS)
    op.drop_index("ix_anomaly_build_period", table_name="anomaly_build", schema=ANALYTICS)
    op.drop_table("anomaly_build", schema=ANALYTICS)
    op.drop_index(
        "ix_anomaly_persistence_series", table_name="anomaly_persistence", schema=ANALYTICS
    )
    op.drop_index(
        "ix_anomaly_persistence_last_seen", table_name="anomaly_persistence", schema=ANALYTICS
    )
    op.drop_table("anomaly_persistence", schema=ANALYTICS)

    # One call per type: the migration guard counts creates against
    # drops in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="anomaly_outcome", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="anomaly_direction", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="anomaly_detection_method", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="anomaly_build_status", schema=ANALYTICS).drop(bind, checkfirst=True)
