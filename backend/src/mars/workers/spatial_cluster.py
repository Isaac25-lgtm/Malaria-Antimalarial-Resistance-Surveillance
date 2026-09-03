"""Prompt 20 adjacency refresh and governed spatial-clustering job."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.clustering import AdjacencyBuilder, ClusterReport, SpatialClusterEngine
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    PeriodGrain,
    SpatialAggregationBasis,
)

JOB_NAME = "spatial.cluster"


def run(
    session: Session,
    *,
    series_kind: BaselineSeriesKind,
    series_key: str,
    period_start: date,
    period_end: date,
    boundary_version_id: uuid.UUID,
    geography_grain: GeographyGrain,
    basis: SpatialAggregationBasis,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    rebuild_adjacency: bool = False,
) -> ClusterReport:
    """Optionally refresh adjacency, then evaluate one spatial series."""
    if rebuild_adjacency:
        AdjacencyBuilder(session).rebuild(boundary_version_id)
    report = SpatialClusterEngine(session).evaluate(
        series_kind=series_kind,
        series_key=series_key,
        period_start=period_start,
        period_end=period_end,
        boundary_version_id=boundary_version_id,
        geography_grain=geography_grain,
        basis=basis,
        period_grain=period_grain,
    )
    session.flush()
    return report


__all__ = ["JOB_NAME", "run"]
