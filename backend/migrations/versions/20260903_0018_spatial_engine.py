"""Geographic aggregation and hotspots.

Revision ID: 0018_spatial_engine
Revises: 0017_anomaly_engine
Created: 2026-09-03

Three tables in mars_analytics:

``spatial_run``                      one aggregation or hotspot run, or its refusal
``geographic_aggregation_result``    one measure for one administrative unit
``hotspot_result``                   one area evaluated against a governed definition

A malaria map is the output most likely to be believed and least likely to be
questioned, so the schema carries more of the argument than usual.

``geographic_aggregation_result`` records a figure **recomputed** from the
numerators and denominators underneath, never averaged from the facility
values. ``a_rate_needs_a_denominator`` keeps that honest: a value with a zero
denominator is not a rate. ``an_aggregate_is_not_a_facility`` keeps the table
to administrative units, because mapping patient-derived figures to facility
points is the thing the blueprint forbids outright.

``aggregation_basis`` is on the row rather than implied by the run. A patient
may attend a clinic outside their own district; rolling up by where care was
given points at a clinic and rolling up by where people live points at a
village. The two are different questions and the column stops them being
summed together. ``unresolved_contributions`` counts the encounters whose
residence never resolved, because their absence always makes a residence map
look emptier than the truth.

``contributing_facilities`` and ``expected_facilities`` travel on every row,
with ``contributors_within_expected`` between them. A district figure built
from three of its twenty facilities is not a district figure, and a reader who
cannot see that will treat it as one.

``hotspot_result`` answers blueprint 037: a hotspot must have a method, not
just a red colour. ``a_hotspot_carries_its_method`` requires two governed
versions on the row - the definition that called it a hotspot and the temporal
baseline method that produced the expectation - because those are two separate
decisions. The threshold, minimum case count and required completeness are
copied onto the row so a later change to the definition cannot rewrite what a
past map meant.

``not_hotspot_means_examined`` is the same rule the temporal engine keeps, and
a map makes it matter more: a red-free map is worth nothing if it cannot say
which areas were looked at. Everything MARS could not evaluate - no figure, no
baseline, too few cases, too little of the area reporting - keeps its own
outcome and its reason.

``consecutive_periods`` counts and ``is_persistent`` labels;
``persistent_requires_configuration`` keeps the label null until a programme
approves a persistence rule. The count is read from the previous period's row
rather than tallied, so nothing already written changes meaning.

``refusals_name_what_is_missing`` tests ``jsonb_typeof`` as well as nullity,
for the reason migration 0016 records.

The enum types ``geography_grain`` and ``period_grain`` (mars_governance) and
``indicator_value_status`` and ``baseline_series_kind`` (mars_analytics) belong
to earlier migrations and are referenced, not created.

Documented in ``docs/data-dictionary/spatial.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_spatial_engine"
down_revision: str | None = "0017_anomaly_engine"
ANALYTICS = "mars_analytics"
CORE = "mars_core"
GOVERNANCE = "mars_governance"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # Types created by an earlier migration are referenced with
    # create_type=False and never dropped here: baseline_series_kind, geography_grain, indicator_value_status, period_grain.
    postgresql.ENUM(
        "running",
        "completed",
        "not_configured",
        "failed",
        name="spatial_run_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "residence",
        "facility_location",
        name="spatial_aggregation_basis",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "hotspot",
        "not_hotspot",
        "not_evaluated_no_observation",
        "not_evaluated_no_baseline",
        "not_evaluated_below_minimum_count",
        "not_evaluated_incomplete_reporting",
        name="hotspot_outcome",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "spatial_run",
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "run_status",
            postgresql.ENUM(
                "running",
                "completed",
                "not_configured",
                "failed",
                name="spatial_run_status",
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
        sa.Column(
            "aggregation_basis",
            postgresql.ENUM(
                "residence",
                "facility_location",
                name="spatial_aggregation_basis",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
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
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("baseline_build_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("deviation_threshold", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("minimum_case_count", sa.Integer(), nullable=True),
        sa.Column("minimum_completeness", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("persistence_periods", sa.Integer(), nullable=True),
        sa.Column("missing_configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("units_examined", sa.Integer(), nullable=False),
        sa.Column("results_written", sa.Integer(), nullable=False),
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
            "run_kind IN ('aggregation', 'hotspot')", name=op.f("ck_spatial_run_run_kind_is_known")
        ),
        sa.CheckConstraint(
            "run_status <> 'completed' OR run_kind <> 'hotspot' OR (method_version_id IS NOT NULL AND deviation_threshold IS NOT NULL AND minimum_case_count IS NOT NULL AND minimum_completeness IS NOT NULL)",
            name=op.f("ck_spatial_run_completed_hotspot_runs_carry_their_definition"),
        ),
        sa.CheckConstraint(
            "run_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND jsonb_typeof(missing_configuration) = 'object')",
            name=op.f("ck_spatial_run_refusals_name_what_is_missing"),
        ),
        sa.CheckConstraint(
            "minimum_completeness IS NULL OR (minimum_completeness >= 0 AND minimum_completeness <= 1)",
            name=op.f("ck_spatial_run_minimum_completeness_is_a_proportion"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_spatial_run_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["baseline_build_id"],
            ["mars_analytics.baseline_build.id"],
            name=op.f("fk_spatial_run_baseline_build_id_baseline_build"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["boundary_version_id"],
            ["mars_core.boundary_version.id"],
            name=op.f("fk_spatial_run_boundary_version_id_boundary_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name=op.f("fk_spatial_run_method_version_id_method_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spatial_run")),
        schema=ANALYTICS,
        comment="One geographic aggregation or hotspot run. A refusal is a row, not an absence.",
    )
    op.create_index(
        "ix_spatial_run_period",
        "spatial_run",
        ["period_start", "run_kind"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_spatial_run_status", "spatial_run", ["run_status"], unique=False, schema=ANALYTICS
    )
    op.create_table(
        "geographic_aggregation_result",
        sa.Column("spatial_run_id", sa.UUID(), nullable=False),
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
        sa.Column("geography_unit_id", sa.UUID(), nullable=False),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "aggregation_basis",
            postgresql.ENUM(
                "residence",
                "facility_location",
                name="spatial_aggregation_basis",
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
        sa.Column("contributing_facilities", sa.Integer(), nullable=False),
        sa.Column("expected_facilities", sa.Integer(), nullable=False),
        sa.Column("reporting_completeness", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("unresolved_contributions", sa.Integer(), nullable=True),
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
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_geographic_aggregation_result_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "geography_grain <> 'facility'",
            name=op.f("ck_geographic_aggregation_result_not_a_facility_grain"),
        ),
        sa.CheckConstraint(
            "contributing_facilities <= expected_facilities",
            name=op.f("ck_geographic_aggregation_result_contributors_within_expected"),
        ),
        sa.CheckConstraint(
            "contributing_facilities >= 0 AND expected_facilities >= 0",
            name=op.f("ck_geographic_aggregation_result_facility_counts_not_negative"),
        ),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator >= 0",
            name=op.f("ck_geographic_aggregation_result_denominator_not_negative"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_geographic_aggregation_result_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "numerator IS NULL OR numerator >= 0",
            name=op.f("ck_geographic_aggregation_result_numerator_not_negative"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start",
            name=op.f("ck_geographic_aggregation_result_period_ordered"),
        ),
        sa.CheckConstraint(
            "value IS NULL OR denominator IS NULL OR denominator > 0",
            name=op.f("ck_geographic_aggregation_result_a_rate_needs_a_denominator"),
        ),
        sa.ForeignKeyConstraint(
            ["boundary_version_id"],
            ["mars_core.boundary_version.id"],
            name="fk_geographic_aggregation_boundary_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name="fk_geographic_aggregation_geography_unit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_run_id"],
            ["mars_analytics.spatial_run.id"],
            name="fk_geographic_aggregation_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geographic_aggregation_result")),
        sa.UniqueConstraint(
            "spatial_run_id",
            "series_kind",
            "series_key",
            "geography_unit_id",
            "aggregation_basis",
            "input_fingerprint",
            name="uq_geographic_aggregation_run_series_unit_basis_input",
        ),
        schema=ANALYTICS,
        comment="One measure rolled up to one administrative unit, recomputed from its parts and carrying how much of the unit reported.",
    )
    op.create_index(
        "ix_geographic_aggregation_run",
        "geographic_aggregation_result",
        ["spatial_run_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_geographic_aggregation_series",
        "geographic_aggregation_result",
        ["series_kind", "series_key", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_geographic_aggregation_unit",
        "geographic_aggregation_result",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "hotspot_result",
        sa.Column("spatial_run_id", sa.UUID(), nullable=False),
        sa.Column("aggregation_result_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("baseline_method_version_id", sa.UUID(), nullable=True),
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
        sa.Column("geography_unit_id", sa.UUID(), nullable=False),
        sa.Column(
            "aggregation_basis",
            postgresql.ENUM(
                "residence",
                "facility_location",
                name="spatial_aggregation_basis",
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
        sa.Column(
            "outcome",
            postgresql.ENUM(
                "hotspot",
                "not_hotspot",
                "not_evaluated_no_observation",
                "not_evaluated_no_baseline",
                "not_evaluated_below_minimum_count",
                "not_evaluated_incomplete_reporting",
                name="hotspot_outcome",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("observed_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("expected_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("absolute_deviation", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("relative_deviation", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("deviation_score", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("deviation_threshold", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=True),
        sa.Column("minimum_case_count", sa.Integer(), nullable=True),
        sa.Column("reporting_completeness", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("minimum_completeness", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("contributing_facilities", sa.Integer(), nullable=True),
        sa.Column("expected_facilities", sa.Integer(), nullable=True),
        sa.Column("history_periods_used", sa.Integer(), nullable=True),
        sa.Column("consecutive_periods", sa.Integer(), nullable=False),
        sa.Column("first_detected_period_start", sa.Date(), nullable=True),
        sa.Column("last_detected_period_end", sa.Date(), nullable=True),
        sa.Column("is_persistent", sa.Boolean(), nullable=True),
        sa.Column("persistence_periods", sa.Integer(), nullable=True),
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
            "geography_grain <> 'facility'",
            name=op.f("ck_hotspot_result_a_hotspot_is_an_area_not_a_facility"),
        ),
        sa.CheckConstraint(
            "outcome <> 'hotspot' OR (method_version_id IS NOT NULL AND baseline_method_version_id IS NOT NULL AND deviation_threshold IS NOT NULL AND observed_value IS NOT NULL AND expected_value IS NOT NULL)",
            name=op.f("ck_hotspot_result_a_hotspot_carries_its_method"),
        ),
        sa.CheckConstraint(
            "outcome <> 'not_evaluated_no_baseline' OR expected_value IS NULL",
            name=op.f("ck_hotspot_result_no_baseline_means_no_expectation"),
        ),
        sa.CheckConstraint(
            "outcome <> 'not_hotspot' OR (expected_value IS NOT NULL AND baseline_method_version_id IS NOT NULL)",
            name=op.f("ck_hotspot_result_not_hotspot_means_examined"),
        ),
        sa.CheckConstraint(
            "consecutive_periods >= 0",
            name=op.f("ck_hotspot_result_consecutive_periods_not_negative"),
        ),
        sa.CheckConstraint(
            "is_persistent IS NULL OR (method_version_id IS NOT NULL AND persistence_periods IS NOT NULL)",
            name=op.f("ck_hotspot_result_persistent_requires_configuration"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64", name=op.f("ck_hotspot_result_fingerprint_is_sha256")
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_hotspot_result_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["aggregation_result_id"],
            ["mars_analytics.geographic_aggregation_result.id"],
            name="fk_hotspot_result_aggregation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_method_version_id"],
            ["mars_governance.method_version.id"],
            name="fk_hotspot_result_baseline_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_hotspot_result_geography_unit_id_geography_unit"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name="fk_hotspot_result_definition_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_run_id"],
            ["mars_analytics.spatial_run.id"],
            name=op.f("fk_hotspot_result_spatial_run_id_spatial_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hotspot_result")),
        sa.UniqueConstraint(
            "spatial_run_id",
            "series_kind",
            "series_key",
            "geography_unit_id",
            "input_fingerprint",
            name="uq_hotspot_result_run_series_unit_input",
        ),
        schema=ANALYTICS,
        comment="One area evaluated against a governed hotspot definition, including the areas that could not be evaluated and why.",
    )
    op.create_index(
        "ix_hotspot_result_outcome",
        "hotspot_result",
        ["outcome", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_hotspot_result_run",
        "hotspot_result",
        ["spatial_run_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_hotspot_result_unit",
        "hotspot_result",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_hotspot_result_unit", table_name="hotspot_result", schema=ANALYTICS)
    op.drop_index("ix_hotspot_result_run", table_name="hotspot_result", schema=ANALYTICS)
    op.drop_index("ix_hotspot_result_outcome", table_name="hotspot_result", schema=ANALYTICS)
    op.drop_table("hotspot_result", schema=ANALYTICS)
    op.drop_index(
        "ix_geographic_aggregation_unit",
        table_name="geographic_aggregation_result",
        schema=ANALYTICS,
    )
    op.drop_index(
        "ix_geographic_aggregation_series",
        table_name="geographic_aggregation_result",
        schema=ANALYTICS,
    )
    op.drop_index(
        "ix_geographic_aggregation_run",
        table_name="geographic_aggregation_result",
        schema=ANALYTICS,
    )
    op.drop_table("geographic_aggregation_result", schema=ANALYTICS)
    op.drop_index("ix_spatial_run_status", table_name="spatial_run", schema=ANALYTICS)
    op.drop_index("ix_spatial_run_period", table_name="spatial_run", schema=ANALYTICS)
    op.drop_table("spatial_run", schema=ANALYTICS)

    # One call per type: the migration guard counts creates against
    # drops in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="hotspot_outcome", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="spatial_aggregation_basis", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="spatial_run_status", schema=ANALYTICS).drop(bind, checkfirst=True)
