"""Governed spatial clustering and versioned adjacency.

Revision ID: 0019_spatial_clustering
Revises: 0018_spatial_engine
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_spatial_clustering"
down_revision: str | None = "0018_spatial_engine"
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
        "neighbour_concentration",
        "contiguous_high_cluster",
        name="cluster_method",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "clustered",
        "not_clustered",
        "not_evaluated_no_neighbours",
        "not_evaluated_insufficient_neighbours",
        "not_evaluated_no_observation",
        "not_evaluated_below_minimum_count",
        "not_evaluated_incomplete_reporting",
        "not_evaluated_method_inapplicable",
        name="cluster_outcome",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "geography_adjacency",
        sa.Column("boundary_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("geography_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("neighbour_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("derivation", sa.String(length=32), nullable=False),
        sa.Column("derived_at", sa.DateTime(timezone=True), nullable=False),
        _uuid_pk(),
        *_timestamps(),
        sa.CheckConstraint(
            "geography_unit_id <> neighbour_unit_id",
            name=op.f("ck_geography_adjacency_an_area_is_not_its_own_neighbour"),
        ),
        sa.CheckConstraint(
            "derivation IN ('shared_boundary')",
            name=op.f("ck_geography_adjacency_derivation_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["boundary_version_id"],
            [f"{CORE}.boundary_version.id"],
            name=op.f("fk_geography_adjacency_boundary_version_id_boundary_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            [f"{CORE}.geography_unit.id"],
            name="fk_geography_adjacency_unit",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["neighbour_unit_id"],
            [f"{CORE}.geography_unit.id"],
            name="fk_geography_adjacency_neighbour",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geography_adjacency")),
        sa.UniqueConstraint(
            "boundary_version_id",
            "geography_unit_id",
            "neighbour_unit_id",
            name="uq_geography_adjacency_version_pair",
        ),
        comment=(
            "Which administrative areas touch which, derived from the "
            "supplied boundaries of one boundary version."
        ),
        schema=CORE,
    )
    op.create_index(
        "ix_geography_adjacency_unit", "geography_adjacency", ["geography_unit_id"], schema=CORE
    )
    op.create_index(
        "ix_geography_adjacency_version",
        "geography_adjacency",
        ["boundary_version_id"],
        schema=CORE,
    )

    op.create_table(
        "spatial_cluster_run",
        sa.Column(
            "run_status",
            postgresql.ENUM(name="spatial_run_status", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "cluster_method",
            postgresql.ENUM(name="cluster_method", schema=ANALYTICS, create_type=False),
            nullable=True,
        ),
        sa.Column("method_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("privacy_configuration_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("boundary_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "series_kind",
            postgresql.ENUM(name="baseline_series_kind", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column("series_key", sa.Text(), nullable=False),
        sa.Column(
            "geography_grain",
            postgresql.ENUM(name="geography_grain", schema=GOVERNANCE, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "aggregation_basis",
            postgresql.ENUM(name="spatial_aggregation_basis", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "period_grain",
            postgresql.ENUM(name="period_grain", schema=GOVERNANCE, create_type=False),
            nullable=False,
        ),
        sa.Column("minimum_neighbours", sa.Integer(), nullable=True),
        sa.Column("neighbour_ratio_threshold", sa.Numeric(12, 6), nullable=True),
        sa.Column("minimum_case_count", sa.Integer(), nullable=True),
        sa.Column("minimum_completeness", sa.Numeric(5, 4), nullable=True),
        sa.Column("minimum_cluster_units", sa.Integer(), nullable=True),
        sa.Column("minimum_cell_count", sa.Integer(), nullable=True),
        sa.Column(
            "minimum_aggregation_level",
            postgresql.ENUM(name="geography_grain", schema=GOVERNANCE, create_type=False),
            nullable=True,
        ),
        sa.Column("missing_configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("units_examined", sa.Integer(), nullable=False),
        sa.Column("results_written", sa.Integer(), nullable=False),
        sa.Column("not_evaluated", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _uuid_pk(),
        *_timestamps(),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_spatial_cluster_run_period_ordered")
        ),
        sa.CheckConstraint(
            "run_status <> 'completed' OR (method_version_id IS NOT NULL AND "
            "privacy_configuration_version_id IS NOT NULL AND cluster_method IS NOT NULL)",
            name=op.f("ck_spatial_cluster_run_completed_run_carries_governance"),
        ),
        sa.CheckConstraint(
            "run_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name=op.f("ck_spatial_cluster_run_refusal_names_missing_configuration"),
        ),
        sa.CheckConstraint(
            "minimum_case_count IS NULL OR minimum_case_count >= 0",
            name=op.f("ck_spatial_cluster_run_minimum_case_count_not_negative"),
        ),
        sa.CheckConstraint(
            "minimum_neighbours IS NULL OR minimum_neighbours >= 1",
            name=op.f("ck_spatial_cluster_run_minimum_neighbours_positive"),
        ),
        sa.CheckConstraint(
            "minimum_cluster_units IS NULL OR minimum_cluster_units >= 2",
            name=op.f("ck_spatial_cluster_run_minimum_cluster_units_at_least_two"),
        ),
        sa.CheckConstraint(
            "neighbour_ratio_threshold IS NULL OR neighbour_ratio_threshold > 0",
            name=op.f("ck_spatial_cluster_run_neighbour_ratio_threshold_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            [f"{GOVERNANCE}.method_version.id"],
            name=op.f("fk_spatial_cluster_run_method_version_id_method_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["privacy_configuration_version_id"],
            [f"{GOVERNANCE}.configuration_version.id"],
            name="fk_spatial_cluster_run_privacy_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["boundary_version_id"],
            [f"{CORE}.boundary_version.id"],
            name=op.f("fk_spatial_cluster_run_boundary_version_id_boundary_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spatial_cluster_run")),
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_spatial_cluster_run_period",
        "spatial_cluster_run",
        ["period_start", "period_end"],
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_spatial_cluster_run_status", "spatial_cluster_run", ["run_status"], schema=ANALYTICS
    )

    op.create_table(
        "spatial_cluster_result",
        sa.Column("spatial_cluster_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("geography_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregation_result_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("method_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "outcome",
            postgresql.ENUM(name="cluster_outcome", schema=ANALYTICS, create_type=False),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("observed_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=True),
        sa.Column("reporting_completeness", sa.Numeric(5, 4), nullable=True),
        sa.Column("neighbour_count", sa.Integer(), nullable=False),
        sa.Column("usable_neighbour_count", sa.Integer(), nullable=False),
        sa.Column("neighbourhood_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("concentration_ratio", sa.Numeric(18, 6), nullable=True),
        sa.Column("cluster_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cluster_group_size", sa.Integer(), nullable=True),
        sa.Column("neighbour_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _uuid_pk(),
        *_timestamps(),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_spatial_cluster_result_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "neighbour_count >= 0",
            name=op.f("ck_spatial_cluster_result_neighbour_count_not_negative"),
        ),
        sa.CheckConstraint(
            "usable_neighbour_count >= 0 AND usable_neighbour_count <= neighbour_count",
            name=op.f("ck_spatial_cluster_result_usable_neighbours_within_total"),
        ),
        sa.CheckConstraint(
            "outcome <> 'clustered' OR (observed_value IS NOT NULL AND "
            "method_version_id IS NOT NULL)",
            name=op.f("ck_spatial_cluster_result_clustered_result_carries_evidence"),
        ),
        sa.CheckConstraint(
            "outcome <> 'not_clustered' OR observed_value IS NOT NULL",
            name=op.f("ck_spatial_cluster_result_not_clustered_means_evaluated"),
        ),
        sa.CheckConstraint(
            "cluster_group_size IS NULL OR cluster_group_size >= 2",
            name=op.f("ck_spatial_cluster_result_cluster_group_size_at_least_two"),
        ),
        sa.ForeignKeyConstraint(
            ["spatial_cluster_run_id"],
            [f"{ANALYTICS}.spatial_cluster_run.id"],
            name="fk_spatial_cluster_result_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            [f"{CORE}.geography_unit.id"],
            name=op.f("fk_spatial_cluster_result_geography_unit_id_geography_unit"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["aggregation_result_id"],
            [f"{ANALYTICS}.geographic_aggregation_result.id"],
            name="fk_spatial_cluster_result_aggregation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            [f"{GOVERNANCE}.method_version.id"],
            name="fk_spatial_cluster_result_method_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spatial_cluster_result")),
        sa.UniqueConstraint(
            "spatial_cluster_run_id",
            "geography_unit_id",
            "input_fingerprint",
            name="uq_spatial_cluster_result_run_unit_input",
        ),
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_spatial_cluster_result_unit",
        "spatial_cluster_result",
        ["geography_unit_id", "period_start"],
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_spatial_cluster_result_outcome",
        "spatial_cluster_result",
        ["outcome", "period_start"],
        schema=ANALYTICS,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spatial_cluster_result_outcome", table_name="spatial_cluster_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_spatial_cluster_result_unit", table_name="spatial_cluster_result", schema=ANALYTICS
    )
    op.drop_table("spatial_cluster_result", schema=ANALYTICS)
    op.drop_index(
        "ix_spatial_cluster_run_status", table_name="spatial_cluster_run", schema=ANALYTICS
    )
    op.drop_index(
        "ix_spatial_cluster_run_period", table_name="spatial_cluster_run", schema=ANALYTICS
    )
    op.drop_table("spatial_cluster_run", schema=ANALYTICS)
    op.drop_index("ix_geography_adjacency_version", table_name="geography_adjacency", schema=CORE)
    op.drop_index("ix_geography_adjacency_unit", table_name="geography_adjacency", schema=CORE)
    op.drop_table("geography_adjacency", schema=CORE)
    postgresql.ENUM(name="cluster_outcome", schema=ANALYTICS).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="cluster_method", schema=ANALYTICS).drop(op.get_bind(), checkfirst=True)
