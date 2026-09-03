"""The geographic aggregation and hotspot job.

Aggregation runs first: a hotspot is evaluated against the area's own history in
``geographic_aggregation_result``, so the history has to exist before the
evaluation can mean anything. A deployment that has only ever aggregated one
month will find every area reported as having no baseline, which is the correct
answer rather than a fault.

Both bases are produced where they can be. Facility-location roll-ups are
available for every series; residence roll-ups are computed from encounters and
so exist only for the measures an encounter can be counted into. The report
names what a basis could not produce rather than leaving it absent.

Safe to run repeatedly. Aggregations are keyed by a fingerprint over their
parts, and hotspot persistence is read from the previous period's row rather
than tallied, so a re-run cannot lengthen a run.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.geographic import AggregationReport, GeographicAggregationEngine
from mars.analytics.hotspot import HotspotEngine, HotspotReport
from mars.core.logging import get_logger
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    PeriodGrain,
    SpatialAggregationBasis,
)
from mars.workers.baseline_compute import DEFAULT_SERIES

logger = get_logger(__name__)

JOB_NAME = "spatial.compute"

#: Coarsest first. A subcounty layer is only worth building where the source
#: coded subcounties, and the engine simply produces nothing where it did not.
DEFAULT_GRAINS = (GeographyGrain.DISTRICT, GeographyGrain.SUBCOUNTY)


def run(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    series_kinds: tuple[BaselineSeriesKind, ...] = DEFAULT_SERIES,
    grains: tuple[GeographyGrain, ...] = DEFAULT_GRAINS,
    bases: tuple[SpatialAggregationBasis, ...] = (
        SpatialAggregationBasis.FACILITY_LOCATION,
        SpatialAggregationBasis.RESIDENCE,
    ),
    boundary_version_id: uuid.UUID | None = None,
    evaluate_hotspots: bool = True,
) -> dict[str, AggregationReport | HotspotReport]:
    """Aggregate one period, then evaluate hotspots over the result."""
    aggregation = GeographicAggregationEngine(session)
    hotspot = HotspotEngine(session)
    reports: dict[str, AggregationReport | HotspotReport] = {}

    for series_kind in series_kinds:
        for grain in grains:
            for basis in bases:
                key = f"aggregation:{series_kind.value}:{grain.value}:{basis.value}"
                reports[key] = aggregation.aggregate(
                    period_start,
                    period_end,
                    series_kind=series_kind,
                    geography_grain=grain,
                    basis=basis,
                    period_grain=period_grain,
                    boundary_version_id=boundary_version_id,
                )
                logger.info("spatial_job_aggregated", job=JOB_NAME, layer=key)

    if evaluate_hotspots:
        for series_kind in series_kinds:
            for grain in grains:
                for basis in bases:
                    key = f"hotspot:{series_kind.value}:{grain.value}:{basis.value}"
                    reports[key] = hotspot.evaluate(
                        period_start,
                        period_end,
                        series_kind=series_kind,
                        geography_grain=grain,
                        basis=basis,
                        period_grain=period_grain,
                        boundary_version_id=boundary_version_id,
                    )
                    logger.info("spatial_job_evaluated", job=JOB_NAME, layer=key)

    session.flush()
    return reports


__all__ = ["DEFAULT_GRAINS", "JOB_NAME", "run"]
