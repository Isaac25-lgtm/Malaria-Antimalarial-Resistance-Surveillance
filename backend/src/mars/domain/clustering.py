"""Versioned spatial adjacency and clustering results — Prompt 20.

Clustering is a governed judgement over Prompt 19 geographic results.  A row
never means merely "not red": non-evaluable areas retain an explicit outcome,
the method parameters used, and the exact neighbouring evidence considered.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import ANALYTICS, CORE, GOVERNANCE
from mars.domain.enums import (
    BaselineSeriesKind,
    ClusterMethod,
    ClusterOutcome,
    GeographyGrain,
    PeriodGrain,
    SpatialAggregationBasis,
    SpatialRunStatus,
)


class SpatialClusterRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One configured clustering evaluation, or a recorded refusal."""

    __tablename__ = "spatial_cluster_run"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint(
            "run_status <> 'completed' OR (method_version_id IS NOT NULL AND "
            "privacy_configuration_version_id IS NOT NULL AND cluster_method IS NOT NULL)",
            name="completed_run_carries_governance",
        ),
        CheckConstraint(
            "run_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name="refusal_names_missing_configuration",
        ),
        CheckConstraint(
            "minimum_case_count IS NULL OR minimum_case_count >= 0",
            name="minimum_case_count_not_negative",
        ),
        CheckConstraint(
            "minimum_neighbours IS NULL OR minimum_neighbours >= 1",
            name="minimum_neighbours_positive",
        ),
        CheckConstraint(
            "minimum_cluster_units IS NULL OR minimum_cluster_units >= 2",
            name="minimum_cluster_units_at_least_two",
        ),
        CheckConstraint(
            "neighbour_ratio_threshold IS NULL OR neighbour_ratio_threshold > 0",
            name="neighbour_ratio_threshold_positive",
        ),
        Index("ix_spatial_cluster_run_period", "period_start", "period_end"),
        Index("ix_spatial_cluster_run_status", "run_status"),
        {"schema": ANALYTICS},
    )

    run_status: Mapped[SpatialRunStatus] = mapped_column(
        pg_enum(SpatialRunStatus, name="spatial_run_status", schema=ANALYTICS), nullable=False
    )
    cluster_method: Mapped[ClusterMethod | None] = mapped_column(
        pg_enum(ClusterMethod, name="cluster_method", schema=ANALYTICS), nullable=True
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    privacy_configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.configuration_version.id",
            name="fk_spatial_cluster_run_privacy_version",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    boundary_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.boundary_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    series_key: Mapped[str] = mapped_column(Text, nullable=False)
    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    aggregation_basis: Mapped[SpatialAggregationBasis] = mapped_column(
        pg_enum(SpatialAggregationBasis, name="spatial_aggregation_basis", schema=ANALYTICS),
        nullable=False,
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    minimum_neighbours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    neighbour_ratio_threshold: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    minimum_case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    minimum_cluster_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_cell_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_aggregation_level: Mapped[GeographyGrain | None] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=True
    )
    missing_configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    units_examined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    results: Mapped[list[SpatialClusterResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SpatialClusterResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One administrative area evaluated against its governed neighbours."""

    __tablename__ = "spatial_cluster_result"
    __table_args__ = (
        UniqueConstraint(
            "spatial_cluster_run_id",
            "geography_unit_id",
            "input_fingerprint",
            name="uq_spatial_cluster_result_run_unit_input",
        ),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint("neighbour_count >= 0", name="neighbour_count_not_negative"),
        CheckConstraint(
            "usable_neighbour_count >= 0 AND usable_neighbour_count <= neighbour_count",
            name="usable_neighbours_within_total",
        ),
        CheckConstraint(
            "outcome <> 'clustered' OR (observed_value IS NOT NULL AND "
            "method_version_id IS NOT NULL)",
            name="clustered_result_carries_evidence",
        ),
        CheckConstraint(
            "outcome <> 'not_clustered' OR observed_value IS NOT NULL",
            name="not_clustered_means_evaluated",
        ),
        CheckConstraint(
            "cluster_group_size IS NULL OR cluster_group_size >= 2",
            name="cluster_group_size_at_least_two",
        ),
        Index("ix_spatial_cluster_result_unit", "geography_unit_id", "period_start"),
        Index("ix_spatial_cluster_result_outcome", "outcome", "period_start"),
        {"schema": ANALYTICS},
    )

    spatial_cluster_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.spatial_cluster_run.id",
            name="fk_spatial_cluster_result_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="RESTRICT"),
        nullable=False,
    )
    aggregation_result_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.geographic_aggregation_result.id",
            name="fk_spatial_cluster_result_aggregation",
            ondelete="RESTRICT",
        ),
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
    )
    outcome: Mapped[ClusterOutcome] = mapped_column(
        pg_enum(ClusterOutcome, name="cluster_outcome", schema=ANALYTICS), nullable=False
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Numeric(18, 6))
    case_count: Mapped[int | None] = mapped_column(Integer)
    reporting_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4))
    neighbour_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usable_neighbour_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    neighbourhood_value: Mapped[float | None] = mapped_column(Numeric(18, 6))
    concentration_ratio: Mapped[float | None] = mapped_column(Numeric(18, 6))
    cluster_group_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    cluster_group_size: Mapped[int | None] = mapped_column(Integer)
    neighbour_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)

    run: Mapped[SpatialClusterRun] = relationship(back_populates="results")


__all__ = ["SpatialClusterResult", "SpatialClusterRun"]
