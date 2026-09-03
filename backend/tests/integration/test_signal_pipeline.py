"""Prompt 20-22 clustering, signals and explanations against PostgreSQL/PostGIS.

The values in this module are test fixtures, not surveillance defaults. The
tests prove that an unconfigured deployment refuses to judge, missing areas
remain explicit, unchanged immutable evidence is not counted twice, corrected
evidence supersedes safely, and explanations reproduce the governed rule.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics.clustering import CLUSTER_METHOD_CODE, SpatialClusterEngine
from mars.domain.adjacency import GeographyAdjacency
from mars.domain.clustering import SpatialClusterResult
from mars.domain.enums import (
    BaselineSeriesKind,
    BoundaryImportStatus,
    ClusterMethod,
    ClusterOutcome,
    GeographyGrain,
    GeographyLevel,
    GeographyUnitKind,
    IndicatorValueStatus,
    LifecycleStatus,
    MethodKind,
    PeriodGrain,
    SignalPriority,
    SignalStatus,
    SpatialAggregationBasis,
    SpatialRunStatus,
)
from mars.domain.geography import BoundaryVersion, GeographyUnit
from mars.domain.governance import (
    ConfigurationKey,
    ConfigurationVersion,
    MethodDefinition,
    MethodVersion,
)
from mars.domain.signal import SignalEvidence, SurveillanceSignal
from mars.domain.spatial import GeographicAggregationResult, SpatialRun
from mars.explainability.engine import ExplanationEngine
from mars.services.spatial_availability import PRIVACY_POLICY_KEY
from mars.signals.engine import SIGNAL_METHOD_CODE, SignalEngine

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_ID = uuid.UUID("f2200000-0000-4000-8000-0000000000ff")
COUNTRY_ID = uuid.UUID("f2200000-0000-4000-8000-000000000001")
DISTRICT_A = uuid.UUID("f2200000-0000-4000-8000-000000000011")
DISTRICT_B = uuid.UUID("f2200000-0000-4000-8000-000000000012")
DISTRICT_C = uuid.UUID("f2200000-0000-4000-8000-000000000013")
START = date(2026, 8, 1)
END = date(2026, 8, 31)


@pytest.fixture(scope="module")
def pipeline_db(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def reference_geography(pipeline_db: Engine) -> None:
    with Session(pipeline_db) as session:
        session.add(
            BoundaryVersion(
                id=BOUNDARY_ID,
                code="TEST-P20-22",
                label="Prompts 20-22 test boundary",
                source_name="synthetic test fixture",
                storage_crs="EPSG:4326",
                import_status=BoundaryImportStatus.PUBLISHED,
                imported_at=datetime.now(UTC),
                imported_by="test",
            )
        )
        for unit_id, level, code, name, parent, depth, path in (
            (COUNTRY_ID, GeographyLevel.COUNTRY, "UG", "Testland", None, 0, "UG"),
            (DISTRICT_A, GeographyLevel.DISTRICT, "P20A", "Alpha", COUNTRY_ID, 1, "UG/P20A"),
            (DISTRICT_B, GeographyLevel.DISTRICT, "P20B", "Bravo", COUNTRY_ID, 1, "UG/P20B"),
            (DISTRICT_C, GeographyLevel.DISTRICT, "P20C", "Charlie", COUNTRY_ID, 1, "UG/P20C"),
        ):
            session.add(
                GeographyUnit(
                    id=unit_id,
                    boundary_version_id=BOUNDARY_ID,
                    level=level,
                    unit_kind=GeographyUnitKind.UNSPECIFIED,
                    preferred_code=code,
                    raw_name=name,
                    normalised_name=name.lower(),
                    parent_id=parent,
                    depth=depth,
                    path=path,
                    is_active=True,
                )
            )
        session.commit()


@pytest.fixture
def session(pipeline_db: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=pipeline_db, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(pipeline_db: Engine) -> Iterator[None]:
    yield
    with pipeline_db.begin() as connection:
        for table in (
            "mars_analytics.signal_explanation",
            "mars_analytics.signal_evidence",
            "mars_analytics.surveillance_signal",
            "mars_analytics.signal_generation_run",
            "mars_analytics.spatial_cluster_result",
            "mars_analytics.spatial_cluster_run",
            "mars_core.geography_adjacency",
            "mars_analytics.geographic_aggregation_result",
            "mars_analytics.spatial_run",
            "mars_governance.configuration_version",
            "mars_governance.configuration_key",
            "mars_governance.method_version",
            "mars_governance.method_definition",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


def _method(
    session: Session, code: str, kind: MethodKind, parameters: dict[str, object]
) -> MethodVersion:
    definition = MethodDefinition(
        code=code,
        label=f"{code} (test only)",
        kind=kind,
        purpose="Synthetic verification; not programme guidance.",
    )
    session.add(definition)
    session.flush()
    version = MethodVersion(
        method_definition_id=definition.id,
        semantic_version="0.0.1-test",
        status=LifecycleStatus.ACTIVE,
        summary="Synthetic verification parameters.",
        parameters=parameters,
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    return version


def _approve_privacy(session: Session) -> ConfigurationVersion:
    key = ConfigurationKey(
        key=PRIVACY_POLICY_KEY,
        label="Spatial privacy (test only)",
        description="Synthetic verification; not programme guidance.",
        category="privacy",
    )
    session.add(key)
    session.flush()
    version = ConfigurationVersion(
        configuration_key_id=key.id,
        version_number=1,
        status=LifecycleStatus.ACTIVE,
        value={"minimum_cell_count": 5, "minimum_aggregation_level": "district"},
        value_checksum="a" * 64,
        effective_from=START,
        reason_for_change="Test fixture.",
        approved_by="test:fixture",
        approved_at=datetime.now(UTC),
        provenance="Synthetic test fixture.",
    )
    session.add(version)
    session.flush()
    return version


def _seed_aggregations(session: Session, *, revision: int = 1) -> SpatialRun:
    run = SpatialRun(
        run_kind="aggregation",
        run_status=SpatialRunStatus.COMPLETED,
        series_kind=BaselineSeriesKind.INDICATOR,
        aggregation_basis=SpatialAggregationBasis.RESIDENCE,
        geography_grain=GeographyGrain.DISTRICT,
        period_start=START,
        period_end=END,
        period_grain=PeriodGrain.MONTH,
        boundary_version_id=BOUNDARY_ID,
        units_examined=2,
        results_written=2,
        not_evaluated=0,
        engine_version="test",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    now = datetime.now(UTC)
    values = ((DISTRICT_A, 30, 100), (DISTRICT_B, 10, 100))
    for unit_id, numerator, denominator in values:
        session.add(
            GeographicAggregationResult(
                spatial_run_id=run.id,
                series_kind=BaselineSeriesKind.INDICATOR,
                series_key="confirmed_malaria",
                geography_grain=GeographyGrain.DISTRICT,
                geography_unit_id=unit_id,
                boundary_version_id=BOUNDARY_ID,
                aggregation_basis=SpatialAggregationBasis.RESIDENCE,
                period_start=START,
                period_end=END,
                period_grain=PeriodGrain.MONTH,
                numerator=numerator,
                denominator=denominator,
                value=Decimal(numerator) / Decimal(denominator),
                value_status=IndicatorValueStatus.AVAILABLE,
                contributing_facilities=5,
                expected_facilities=5,
                reporting_completeness=Decimal("1"),
                input_fingerprint=(str(revision) * 64)[:64],
                source_cutoff=now,
                engine_version="test",
                computed_at=now,
            )
        )
    for left, right in ((DISTRICT_A, DISTRICT_B), (DISTRICT_B, DISTRICT_A)):
        session.add(
            GeographyAdjacency(
                boundary_version_id=BOUNDARY_ID,
                geography_unit_id=left,
                neighbour_unit_id=right,
                derivation="shared_boundary",
                derived_at=now,
            )
        )
    session.flush()
    return run


def _run_cluster(session: Session) -> None:
    report = SpatialClusterEngine(session).evaluate(
        series_kind=BaselineSeriesKind.INDICATOR,
        series_key="confirmed_malaria",
        period_start=START,
        period_end=END,
        boundary_version_id=BOUNDARY_ID,
        geography_grain=GeographyGrain.DISTRICT,
        basis=SpatialAggregationBasis.RESIDENCE,
        period_grain=PeriodGrain.MONTH,
    )
    assert report.status is SpatialRunStatus.COMPLETED


def _correct_aggregation(session: Session) -> None:
    current = (
        session.execute(
            select(GeographicAggregationResult)
            .where(GeographicAggregationResult.geography_unit_id == DISTRICT_A)
            .order_by(GeographicAggregationResult.computed_at.desc())
        )
        .scalars()
        .first()
    )
    assert current is not None
    session.add(
        GeographicAggregationResult(
            spatial_run_id=current.spatial_run_id,
            series_kind=current.series_kind,
            series_key=current.series_key,
            geography_grain=current.geography_grain,
            geography_unit_id=current.geography_unit_id,
            boundary_version_id=current.boundary_version_id,
            aggregation_basis=current.aggregation_basis,
            period_start=current.period_start,
            period_end=current.period_end,
            period_grain=current.period_grain,
            numerator=31,
            denominator=current.denominator,
            value=Decimal("0.31"),
            value_status=current.value_status,
            contributing_facilities=current.contributing_facilities,
            expected_facilities=current.expected_facilities,
            reporting_completeness=current.reporting_completeness,
            input_fingerprint="9" * 64,
            source_cutoff=datetime.now(UTC),
            engine_version="test",
            computed_at=datetime.now(UTC),
        )
    )
    session.flush()


def test_unconfigured_clustering_is_an_explicit_refusal(session: Session) -> None:
    report = SpatialClusterEngine(session).evaluate(
        series_kind=BaselineSeriesKind.INDICATOR,
        series_key="confirmed_malaria",
        period_start=START,
        period_end=END,
        boundary_version_id=BOUNDARY_ID,
        geography_grain=GeographyGrain.DISTRICT,
        basis=SpatialAggregationBasis.RESIDENCE,
        period_grain=PeriodGrain.MONTH,
    )
    assert report.status is SpatialRunStatus.NOT_CONFIGURED
    assert f"method:{CLUSTER_METHOD_CODE}" in report.missing_configuration
    assert f"configuration:{PRIVACY_POLICY_KEY}" in report.missing_configuration


def test_cluster_signal_explanation_and_safe_supersession(session: Session) -> None:
    _approve_privacy(session)
    _method(
        session,
        CLUSTER_METHOD_CODE,
        MethodKind.SPATIAL_METHOD,
        {
            "method": ClusterMethod.NEIGHBOUR_CONCENTRATION.value,
            "minimum_neighbours": 1,
            "neighbour_ratio_threshold": 2,
            "minimum_case_count": 5,
            "minimum_completeness": 0.5,
        },
    )
    _method(
        session,
        SIGNAL_METHOD_CODE,
        MethodKind.SIGNAL_RULE,
        {
            "rules": [
                {
                    "code": "TEST_SPATIAL_PRIORITY",
                    "signal_type": "spatial_cluster",
                    "source_kinds": ["spatial_cluster"],
                    "minimum_evidence": 1,
                    "minimum_score": 2,
                    "weights": {"spatial_cluster": 2},
                    "priority_bands": [{"priority": "high", "minimum_score": 2}],
                    "recommended_action_codes": ["VERIFY_SOURCE", "REVIEW_NEIGHBOURS"],
                }
            ]
        },
    )
    _seed_aggregations(session)
    _run_cluster(session)

    results = list(
        session.execute(
            select(SpatialClusterResult).order_by(SpatialClusterResult.geography_unit_id)
        ).scalars()
    )
    assert len(results) == 3
    assert {row.geography_unit_id: row.outcome for row in results} == {
        DISTRICT_A: ClusterOutcome.CLUSTERED,
        DISTRICT_B: ClusterOutcome.NOT_CLUSTERED,
        DISTRICT_C: ClusterOutcome.NOT_EVALUATED_NO_OBSERVATION,
    }

    first = SignalEngine(session).generate(START, END)
    assert first.signals_created == 1
    signal = session.execute(select(SurveillanceSignal)).scalar_one()
    assert signal.geography_unit_id == DISTRICT_A
    assert signal.priority is SignalPriority.HIGH
    assert signal.signal_status is SignalStatus.ACTIVE

    explanation = ExplanationEngine(session).build(signal.id)
    replayed = ExplanationEngine(session).build(signal.id)
    assert replayed.id == explanation.id
    assert explanation.evidence[0]["kind"] == "spatial_cluster"
    assert "does not confirm" in explanation.interpretation_limit

    # An identical upstream rerun writes immutable rows but is the same
    # evidence. It must neither double the score nor replace the signal.
    _run_cluster(session)
    unchanged = SignalEngine(session).generate(START, END)
    assert unchanged.signals_created == 0
    assert unchanged.signals_unchanged == 1
    assert session.scalar(select(SignalEvidence.contribution)) == Decimal("2")

    # A corrected source fingerprint is genuinely new evidence for the same
    # period. Rebuilding then replaces the active signal without violating the
    # partial unique index.
    _correct_aggregation(session)
    _run_cluster(session)
    replacement = SignalEngine(session).generate(START, END)
    assert replacement.signals_created == 1
    assert replacement.signals_superseded == 1
    signals = list(session.execute(select(SurveillanceSignal)).scalars())
    assert len(signals) == 2
    assert sum(item.signal_status is SignalStatus.ACTIVE for item in signals) == 1
    assert sum(item.signal_status is SignalStatus.SUPERSEDED for item in signals) == 1
