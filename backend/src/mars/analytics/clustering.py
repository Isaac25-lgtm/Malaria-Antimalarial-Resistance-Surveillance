"""Governed spatial clustering over Prompt 19 administrative summaries."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.adjacency import SHARED_BOUNDARY, GeographyAdjacency
from mars.domain.clustering import SpatialClusterResult, SpatialClusterRun
from mars.domain.enums import (
    BaselineSeriesKind,
    ClusterMethod,
    ClusterOutcome,
    GeographyGrain,
    HotspotOutcome,
    IndicatorValueStatus,
    LifecycleStatus,
    MethodKind,
    PeriodGrain,
    SpatialAggregationBasis,
    SpatialRunStatus,
)
from mars.domain.geography import GeographyUnit
from mars.domain.governance import MethodDefinition, MethodVersion
from mars.domain.spatial import GeographicAggregationResult, HotspotResult
from mars.services.spatial_availability import GRAIN_ORDER, GRAIN_TO_LEVEL, privacy_policy

logger = get_logger(__name__)

ENGINE_VERSION = "1.0.0"
CLUSTER_METHOD_CODE = "spatial_cluster_detection"


@dataclass(frozen=True, slots=True)
class ClusterDefinition:
    method_version_id: uuid.UUID
    method: ClusterMethod
    minimum_neighbours: int | None
    neighbour_ratio_threshold: Decimal | None
    minimum_case_count: int
    minimum_completeness: Decimal
    minimum_cluster_units: int | None


@dataclass(slots=True)
class ClusterReport:
    run_id: uuid.UUID
    status: SpatialRunStatus
    outcomes: dict[str, int] = field(default_factory=dict)
    missing_configuration: list[str] = field(default_factory=list)
    notes: str | None = None

    @property
    def results_written(self) -> int:
        return sum(self.outcomes.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "status": self.status.value,
            "outcomes": dict(sorted(self.outcomes.items())),
            "missing_configuration": self.missing_configuration,
            "notes": self.notes,
        }


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fingerprint(material: object) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class AdjacencyBuilder:
    """Derive symmetric, same-level neighbours from one boundary version."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def rebuild(self, boundary_version_id: uuid.UUID) -> int:
        """Replace only this version's reproducible adjacency rows.

        A shared edge is required. Polygons touching at a single point are not
        treated as neighbours because that would connect areas that do not
        share a traversable administrative boundary.
        """
        pairs = self._session.execute(
            text(
                """
                SELECT a.geography_unit_id, b.geography_unit_id
                FROM mars_core.geography_unit_geometry AS a
                JOIN mars_core.geography_unit_geometry AS b
                  ON a.boundary_version_id = b.boundary_version_id
                 AND a.geography_unit_id <> b.geography_unit_id
                 AND a.geom IS NOT NULL AND b.geom IS NOT NULL
                 AND a.geom && b.geom
                JOIN mars_core.geography_unit AS ua ON ua.id = a.geography_unit_id
                JOIN mars_core.geography_unit AS ub ON ub.id = b.geography_unit_id
                WHERE a.boundary_version_id = :version_id
                  AND ua.level = ub.level
                  AND ST_Touches(a.geom, b.geom)
                  AND ST_Dimension(ST_Intersection(a.geom, b.geom)) = 1
                ORDER BY a.geography_unit_id, b.geography_unit_id
                """
            ),
            {"version_id": boundary_version_id},
        ).all()
        self._session.execute(
            delete(GeographyAdjacency).where(
                GeographyAdjacency.boundary_version_id == boundary_version_id
            )
        )
        now = datetime.now(UTC)
        self._session.add_all(
            [
                GeographyAdjacency(
                    boundary_version_id=boundary_version_id,
                    geography_unit_id=left,
                    neighbour_unit_id=right,
                    derivation=SHARED_BOUNDARY,
                    derived_at=now,
                )
                for left, right in pairs
            ]
        )
        self._session.flush()
        return len(pairs)


class SpatialClusterEngine:
    """Evaluate local concentration or connected hotspot components."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def definition(self) -> tuple[ClusterDefinition | None, list[str]]:
        row = (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition)
                .where(
                    MethodDefinition.code == CLUSTER_METHOD_CODE,
                    MethodDefinition.kind == MethodKind.SPATIAL_METHOD,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None, [f"method:{CLUSTER_METHOD_CODE}"]
        parameters = row.parameters if isinstance(row.parameters, dict) else {}
        common = ("method", "minimum_case_count", "minimum_completeness")
        missing = [name for name in common if parameters.get(name) is None]
        try:
            method = ClusterMethod(parameters.get("method"))
        except (TypeError, ValueError):
            missing.append("method")
            return None, sorted(set(missing))
        required = (
            ("minimum_neighbours", "neighbour_ratio_threshold")
            if method is ClusterMethod.NEIGHBOUR_CONCENTRATION
            else ("minimum_cluster_units",)
        )
        missing.extend(name for name in required if parameters.get(name) is None)
        if missing:
            return None, sorted(set(missing))
        try:
            minimum_cases = int(parameters["minimum_case_count"])
            completeness = Decimal(str(parameters["minimum_completeness"]))
            minimum_neighbours = (
                int(parameters["minimum_neighbours"])
                if method is ClusterMethod.NEIGHBOUR_CONCENTRATION
                else None
            )
            ratio = (
                Decimal(str(parameters["neighbour_ratio_threshold"]))
                if method is ClusterMethod.NEIGHBOUR_CONCENTRATION
                else None
            )
            minimum_units = (
                int(parameters["minimum_cluster_units"])
                if method is ClusterMethod.CONTIGUOUS_HIGH_CLUSTER
                else None
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None, ["invalid_numeric_parameter"]
        invalid: list[str] = []
        if minimum_cases < 0:
            invalid.append("minimum_case_count")
        if not Decimal(0) <= completeness <= Decimal(1):
            invalid.append("minimum_completeness")
        if minimum_neighbours is not None and minimum_neighbours < 1:
            invalid.append("minimum_neighbours")
        if ratio is not None and ratio <= 0:
            invalid.append("neighbour_ratio_threshold")
        if minimum_units is not None and minimum_units < 2:
            invalid.append("minimum_cluster_units")
        if invalid:
            return None, invalid
        return (
            ClusterDefinition(
                row.id,
                method,
                minimum_neighbours,
                ratio,
                minimum_cases,
                completeness,
                minimum_units,
            ),
            [],
        )

    def evaluate(
        self,
        *,
        series_kind: BaselineSeriesKind,
        series_key: str,
        period_start: date,
        period_end: date,
        boundary_version_id: uuid.UUID,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        period_grain: PeriodGrain,
    ) -> ClusterReport:
        if period_end < period_start:
            raise ValueError("period_end must be on or after period_start")
        started = datetime.now(UTC)
        definition, method_missing = self.definition()
        privacy, privacy_missing = privacy_policy(self._session)
        missing = sorted({*method_missing, *privacy_missing})
        if geography_grain not in GRAIN_TO_LEVEL:
            missing.append("requested_grain_is_not_administrative")
        if (
            privacy is not None
            and GRAIN_ORDER[geography_grain] > GRAIN_ORDER[privacy.minimum_aggregation_level]
        ):
            missing.append("requested_grain_below_approved_privacy_level")
        if definition is None or privacy is None or missing:
            return self._refusal(
                series_kind,
                series_key,
                period_start,
                period_end,
                boundary_version_id,
                geography_grain,
                basis,
                period_grain,
                sorted(set(missing)),
                started,
            )

        run = SpatialClusterRun(
            run_status=SpatialRunStatus.RUNNING,
            cluster_method=definition.method,
            method_version_id=definition.method_version_id,
            privacy_configuration_version_id=privacy.configuration_version_id,
            boundary_version_id=boundary_version_id,
            series_kind=series_kind,
            series_key=series_key,
            geography_grain=geography_grain,
            aggregation_basis=basis,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            minimum_neighbours=definition.minimum_neighbours,
            neighbour_ratio_threshold=definition.neighbour_ratio_threshold,
            minimum_case_count=definition.minimum_case_count,
            minimum_completeness=definition.minimum_completeness,
            minimum_cluster_units=definition.minimum_cluster_units,
            minimum_cell_count=privacy.minimum_cell_count,
            minimum_aggregation_level=privacy.minimum_aggregation_level,
            units_examined=0,
            results_written=0,
            not_evaluated=0,
            engine_version=ENGINE_VERSION,
            started_at=started,
        )
        self._session.add(run)
        self._session.flush()
        report = ClusterReport(run.id, SpatialRunStatus.RUNNING)
        aggregations = self._latest_aggregations(
            series_kind,
            series_key,
            period_start,
            period_end,
            period_grain,
            geography_grain,
            basis,
            boundary_version_id,
        )
        units = self._unit_ids(boundary_version_id, geography_grain)
        adjacency = self._adjacency(boundary_version_id, units)
        if definition.method is ClusterMethod.NEIGHBOUR_CONCENTRATION:
            self._neighbour_results(run, report, definition, units, aggregations, adjacency)
        else:
            self._component_results(run, report, definition, units, aggregations, adjacency)
        run.run_status = SpatialRunStatus.COMPLETED
        run.units_examined = report.results_written
        run.results_written = report.results_written
        run.not_evaluated = sum(
            count
            for outcome, count in report.outcomes.items()
            if outcome.startswith("not_evaluated")
        )
        run.finished_at = datetime.now(UTC)
        report.status = SpatialRunStatus.COMPLETED
        self._session.flush()
        logger.info("spatial_cluster_finished", **report.as_dict())
        return report

    def _refusal(
        self,
        series_kind: BaselineSeriesKind,
        series_key: str,
        period_start: date,
        period_end: date,
        boundary_version_id: uuid.UUID,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        period_grain: PeriodGrain,
        missing: list[str],
        started: datetime,
    ) -> ClusterReport:
        notes = (
            "Spatial clustering was not run because its governed method or privacy policy "
            "is incomplete. This is not evidence that no cluster exists."
        )
        run = SpatialClusterRun(
            run_status=SpatialRunStatus.NOT_CONFIGURED,
            boundary_version_id=boundary_version_id,
            series_kind=series_kind,
            series_key=series_key,
            geography_grain=geography_grain,
            aggregation_basis=basis,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            missing_configuration={"parameters": missing},
            units_examined=0,
            results_written=0,
            not_evaluated=0,
            engine_version=ENGINE_VERSION,
            started_at=started,
            finished_at=datetime.now(UTC),
            notes=notes,
        )
        self._session.add(run)
        self._session.flush()
        return ClusterReport(
            run.id, SpatialRunStatus.NOT_CONFIGURED, missing_configuration=missing, notes=notes
        )

    def _latest_aggregations(
        self,
        series_kind: BaselineSeriesKind,
        series_key: str,
        period_start: date,
        period_end: date,
        period_grain: PeriodGrain,
        grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        boundary_version_id: uuid.UUID,
    ) -> dict[uuid.UUID, GeographicAggregationResult]:
        rows = self._session.execute(
            select(GeographicAggregationResult).where(
                GeographicAggregationResult.series_kind == series_kind,
                GeographicAggregationResult.series_key == series_key,
                GeographicAggregationResult.period_start == period_start,
                GeographicAggregationResult.period_end == period_end,
                GeographicAggregationResult.period_grain == period_grain,
                GeographicAggregationResult.geography_grain == grain,
                GeographicAggregationResult.aggregation_basis == basis,
                GeographicAggregationResult.boundary_version_id == boundary_version_id,
            )
        ).scalars()
        latest: dict[uuid.UUID, GeographicAggregationResult] = {}
        for row in rows:
            current = latest.get(row.geography_unit_id)
            if current is None or row.computed_at > current.computed_at:
                latest[row.geography_unit_id] = row
        return latest

    def _unit_ids(self, boundary_version_id: uuid.UUID, grain: GeographyGrain) -> set[uuid.UUID]:
        level = GRAIN_TO_LEVEL[grain]
        return set(
            self._session.execute(
                select(GeographyUnit.id).where(
                    GeographyUnit.boundary_version_id == boundary_version_id,
                    GeographyUnit.level == level,
                    GeographyUnit.is_active.is_(True),
                )
            ).scalars()
        )

    def _adjacency(
        self, boundary_version_id: uuid.UUID, units: set[uuid.UUID]
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        result: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        rows = self._session.execute(
            select(GeographyAdjacency).where(
                GeographyAdjacency.boundary_version_id == boundary_version_id,
                GeographyAdjacency.geography_unit_id.in_(units),
            )
        ).scalars()
        for row in rows:
            if row.neighbour_unit_id in units:
                result[row.geography_unit_id].add(row.neighbour_unit_id)
        return result

    def _basic_outcome(
        self, row: GeographicAggregationResult | None, definition: ClusterDefinition
    ) -> ClusterOutcome | None:
        if row is None:
            return ClusterOutcome.NOT_EVALUATED_NO_OBSERVATION
        if row.value_status is not IndicatorValueStatus.AVAILABLE or row.value is None:
            return ClusterOutcome.NOT_EVALUATED_NO_OBSERVATION
        if row.numerator is None or row.numerator < definition.minimum_case_count:
            return ClusterOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT
        completeness = _decimal(row.reporting_completeness)
        if completeness is None or completeness < definition.minimum_completeness:
            return ClusterOutcome.NOT_EVALUATED_INCOMPLETE_REPORTING
        return None

    def _neighbour_results(
        self,
        run: SpatialClusterRun,
        report: ClusterReport,
        definition: ClusterDefinition,
        units: set[uuid.UUID],
        rows: dict[uuid.UUID, GeographicAggregationResult],
        adjacency: dict[uuid.UUID, set[uuid.UUID]],
    ) -> None:
        assert definition.minimum_neighbours is not None
        assert definition.neighbour_ratio_threshold is not None
        for unit_id in sorted(units, key=str):
            row = rows.get(unit_id)
            outcome = self._basic_outcome(row, definition)
            neighbours = sorted(adjacency.get(unit_id, set()), key=str)
            usable = [
                rows[n]
                for n in neighbours
                if n in rows
                and rows[n].value_status is IndicatorValueStatus.AVAILABLE
                and rows[n].value is not None
            ]
            mean: Decimal | None = None
            ratio: Decimal | None = None
            if outcome is None and not neighbours:
                outcome = ClusterOutcome.NOT_EVALUATED_NO_NEIGHBOURS
            elif outcome is None and len(usable) < definition.minimum_neighbours:
                outcome = ClusterOutcome.NOT_EVALUATED_INSUFFICIENT_NEIGHBOURS
            elif outcome is None:
                values = [_decimal(n.value) for n in usable]
                numeric = [value for value in values if value is not None]
                mean = sum(numeric, Decimal(0)) / Decimal(len(numeric))
                observed = _decimal(row.value) if row is not None else None
                if observed is None or mean == 0:
                    outcome = ClusterOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE
                else:
                    ratio = observed / mean
                    outcome = (
                        ClusterOutcome.CLUSTERED
                        if ratio >= definition.neighbour_ratio_threshold
                        else ClusterOutcome.NOT_CLUSTERED
                    )
            self._write(
                run,
                report,
                definition,
                unit_id,
                row,
                outcome or ClusterOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE,
                neighbours,
                usable,
                mean,
                ratio,
            )

    def _component_results(
        self,
        run: SpatialClusterRun,
        report: ClusterReport,
        definition: ClusterDefinition,
        units: set[uuid.UUID],
        rows: dict[uuid.UUID, GeographicAggregationResult],
        adjacency: dict[uuid.UUID, set[uuid.UUID]],
    ) -> None:
        assert definition.minimum_cluster_units is not None
        hotspots = self._latest_hotspots(run, units)
        eligible = {
            unit_id for unit_id, row in hotspots.items() if row.outcome is HotspotOutcome.HOTSPOT
        }
        components: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
        unseen = set(eligible)
        while unseen:
            first = min(unseen, key=str)
            queue: deque[uuid.UUID] = deque([first])
            component: set[uuid.UUID] = set()
            unseen.remove(first)
            while queue:
                current = queue.popleft()
                component.add(current)
                for neighbour in adjacency.get(current, set()) & unseen:
                    unseen.remove(neighbour)
                    queue.append(neighbour)
            fingerprint = _fingerprint(sorted(map(str, component)))
            group_id = uuid.uuid5(uuid.NAMESPACE_URL, f"mars:spatial-cluster:{fingerprint}")
            for unit_id in component:
                components[unit_id] = (group_id, len(component))

        for unit_id in sorted(units, key=str):
            row = rows.get(unit_id)
            basic = self._basic_outcome(row, definition)
            neighbours = sorted(adjacency.get(unit_id, set()), key=str)
            hotspot = hotspots.get(unit_id)
            if basic is not None:
                outcome = basic
            elif not neighbours:
                outcome = ClusterOutcome.NOT_EVALUATED_NO_NEIGHBOURS
            elif hotspot is None or hotspot.outcome is not HotspotOutcome.HOTSPOT:
                outcome = ClusterOutcome.NOT_CLUSTERED
            else:
                _group, size = components[unit_id]
                outcome = (
                    ClusterOutcome.CLUSTERED
                    if size >= definition.minimum_cluster_units
                    else ClusterOutcome.NOT_CLUSTERED
                )
            usable = [rows[n] for n in neighbours if n in rows]
            group = components.get(unit_id)
            self._write(
                run,
                report,
                definition,
                unit_id,
                row,
                outcome,
                neighbours,
                usable,
                None,
                None,
                group[0] if group and outcome is ClusterOutcome.CLUSTERED else None,
                group[1] if group and outcome is ClusterOutcome.CLUSTERED else None,
                hotspot.id if hotspot is not None else None,
            )

    def _latest_hotspots(
        self, run: SpatialClusterRun, units: set[uuid.UUID]
    ) -> dict[uuid.UUID, HotspotResult]:
        rows = self._session.execute(
            select(HotspotResult).where(
                HotspotResult.series_kind == run.series_kind,
                HotspotResult.series_key == run.series_key,
                HotspotResult.period_start == run.period_start,
                HotspotResult.period_end == run.period_end,
                HotspotResult.geography_grain == run.geography_grain,
                HotspotResult.aggregation_basis == run.aggregation_basis,
                HotspotResult.geography_unit_id.in_(units),
            )
        ).scalars()
        latest: dict[uuid.UUID, HotspotResult] = {}
        for row in rows:
            current = latest.get(row.geography_unit_id)
            if current is None or row.computed_at > current.computed_at:
                latest[row.geography_unit_id] = row
        return latest

    def _write(
        self,
        run: SpatialClusterRun,
        report: ClusterReport,
        definition: ClusterDefinition,
        unit_id: uuid.UUID,
        row: GeographicAggregationResult | None,
        outcome: ClusterOutcome,
        neighbours: list[uuid.UUID],
        usable: list[GeographicAggregationResult],
        neighbourhood_value: Decimal | None,
        ratio: Decimal | None,
        group_id: uuid.UUID | None = None,
        group_size: int | None = None,
        hotspot_result_id: uuid.UUID | None = None,
    ) -> None:
        evidence = [
            {
                "geography_unit_id": str(item.geography_unit_id),
                "aggregation_result_id": str(item.id),
                "value": str(item.value) if item.value is not None else None,
                "status": item.value_status.value,
            }
            for item in usable
        ]
        fingerprint = _fingerprint(
            {
                "geography_unit_id": str(unit_id),
                "aggregation": str(row.id) if row is not None else None,
                "aggregation_input": row.input_fingerprint if row is not None else None,
                "neighbours": evidence,
                "method_version": str(definition.method_version_id),
                "privacy_version": str(run.privacy_configuration_version_id),
                "hotspot_result": str(hotspot_result_id) if hotspot_result_id else None,
            }
        )
        self._session.add(
            SpatialClusterResult(
                spatial_cluster_run_id=run.id,
                geography_unit_id=unit_id,
                aggregation_result_id=row.id if row is not None else None,
                method_version_id=definition.method_version_id,
                outcome=outcome,
                period_start=run.period_start,
                period_end=run.period_end,
                observed_value=row.value if row is not None else None,
                case_count=row.numerator if row is not None else None,
                reporting_completeness=(row.reporting_completeness if row is not None else None),
                neighbour_count=len(neighbours),
                usable_neighbour_count=len(usable),
                neighbourhood_value=neighbourhood_value,
                concentration_ratio=ratio,
                cluster_group_id=group_id,
                cluster_group_size=group_size,
                neighbour_evidence=evidence,
                input_fingerprint=fingerprint,
                source_cutoff=max(
                    [
                        run.started_at,
                        *(item.source_cutoff for item in usable),
                        *([row.source_cutoff] if row is not None else []),
                    ]
                ),
                computed_at=datetime.now(UTC),
                quality_context={
                    "minimum_case_count": definition.minimum_case_count,
                    "minimum_completeness": str(definition.minimum_completeness),
                    "hotspot_result_id": str(hotspot_result_id) if hotspot_result_id else None,
                    "observation_missing": row is None,
                },
                notes=(
                    "Routine-data spatial concentration requiring investigation; "
                    "not confirmation of resistance or treatment failure."
                ),
            )
        )
        report.outcomes[outcome.value] = report.outcomes.get(outcome.value, 0) + 1


__all__ = [
    "CLUSTER_METHOD_CODE",
    "ENGINE_VERSION",
    "AdjacencyBuilder",
    "ClusterDefinition",
    "ClusterReport",
    "SpatialClusterEngine",
]
