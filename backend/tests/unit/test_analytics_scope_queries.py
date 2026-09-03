"""Query-level regression tests for facility/geography scope intersection."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from mars.api.v1 import analytics as analytics_router
from mars.core.errors import ValidationFailedError
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    SpatialAggregationBasis,
)
from mars.services.analytics_query import AnalyticsQueryService
from mars.services.indicator_query import IndicatorQueryService
from mars.services.signal_query import SignalQueryService


class _Rows:
    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return []

    def __iter__(self):
        return iter(())


class _CaptureSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows()


class _FixedScopeAnalytics(AnalyticsQueryService):
    def __init__(self, session: Any, geography_id: uuid.UUID, facility_id: uuid.UUID) -> None:
        super().__init__(session)
        self._geography_id = geography_id
        self._facility_id = facility_id

    def geography_ids(self, principal: Any) -> set[uuid.UUID] | None:
        return {self._geography_id}

    def facility_ids(self, principal: Any) -> set[uuid.UUID] | None:
        return {self._facility_id}


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_indicator_scope_dimensions_are_unioned_not_intersected() -> None:
    session = _CaptureSession()
    IndicatorQueryService(session).summary(
        geography_unit_ids=[uuid.UUID(int=1)],
        facility_ids=[uuid.UUID(int=2)],
    )
    statement = session.statements[-1]
    where = str(statement.whereclause)
    assert "geography_unit_id" in where
    assert "facility_id" in where
    assert " OR " in where


def test_facility_restricted_episodes_do_not_include_district_residents(
    gulu_facility_principal: Any,
) -> None:
    session = _CaptureSession()
    service = _FixedScopeAnalytics(
        session,
        gulu_facility_principal.geography_scopes[0].geography_unit_id,
        next(iter(gulu_facility_principal.facility_scopes)),
    )
    service.episodes(gulu_facility_principal, period_from=None, period_to=None, limit=20)
    sql = _sql(session.statements[-1])
    assert "index_facility_id IN" in sql
    assert "residence_district_id IN" not in sql
    assert "residence_subcounty_id IN" not in sql


def test_facility_restricted_spatial_results_use_an_empty_geography_scope(
    gulu_facility_principal: Any,
) -> None:
    session = _CaptureSession()
    service = _FixedScopeAnalytics(
        session,
        gulu_facility_principal.geography_scopes[0].geography_unit_id,
        next(iter(gulu_facility_principal.facility_scopes)),
    )
    service.aggregate_results(
        gulu_facility_principal,
        kind="cluster",
        period_from=None,
        period_to=None,
        limit=20,
    )
    sql = _sql(session.statements[-1])
    assert "geography_unit_id IN (NULL)" in sql


def test_facility_restricted_signal_query_filters_only_by_facility(
    gulu_facility_principal: Any,
) -> None:
    session = _CaptureSession()
    signal_service = SignalQueryService(session)
    signal_service._scope = _FixedScopeAnalytics(
        session,
        gulu_facility_principal.geography_scopes[0].geography_unit_id,
        next(iter(gulu_facility_principal.facility_scopes)),
    )
    signal_service.list(
        gulu_facility_principal,
        period_from=None,
        period_to=None,
        active_only=False,
        limit=20,
    )
    sql = _sql(session.statements[-1])
    assert "facility_id IN" in sql
    assert "geography_unit_id IN (NULL)" in sql


def test_reversed_query_period_is_refused_before_database_access(
    gulu_facility_principal: Any,
) -> None:
    session = _CaptureSession()
    service = _FixedScopeAnalytics(
        session,
        gulu_facility_principal.geography_scopes[0].geography_unit_id,
        next(iter(gulu_facility_principal.facility_scopes)),
    )
    with pytest.raises(ValidationFailedError):
        service.episodes(
            gulu_facility_principal,
            period_from=date(2026, 9, 1),
            period_to=date(2026, 8, 1),
            limit=20,
        )
    assert session.statements == []


def test_facility_restricted_map_cells_get_no_district_wide_geography_scope(
    gulu_facility_principal: Any,
    national_principal: Any,
    gulu_district_principal: Any,
) -> None:
    """The map endpoint applies the rule the other analytical reads apply.

    A facility user's district scope proves only that the facility sits inside
    that district. Serving them every district cell would hand a facility
    account the district-wide surveillance picture that the hotspot, cluster,
    episode and signal queries all refuse it.
    """
    captured: list[tuple[str, ...] | None] = []

    def _capture(_session: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs["authorised_paths"])
        return {}

    original = analytics_router.spatial_cells
    analytics_router.spatial_cells = _capture  # type: ignore[assignment]
    try:
        for principal in (gulu_facility_principal, national_principal, gulu_district_principal):
            analytics_router.map_cells(
                principal=principal,
                session=_CaptureSession(),
                series_kind=BaselineSeriesKind.TESTING_MEASURE,
                series_key="test_positivity",
                period_start=date(2026, 7, 1),
                geography_grain=GeographyGrain.DISTRICT,
                basis=SpatialAggregationBasis.FACILITY_LOCATION,
                boundary_version_id=uuid.UUID(int=9),
                unit_id=None,
            )
    finally:
        analytics_router.spatial_cells = original  # type: ignore[assignment]

    facility_paths, national_paths, district_paths = captured
    # Empty, not None: None means national scope and would show everything.
    assert facility_paths == ()
    assert national_paths is None
    assert district_paths == gulu_district_principal.scope_path_prefixes()
    assert district_paths
