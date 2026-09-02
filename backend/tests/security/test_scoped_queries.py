"""Regression tests for query-level geography and facility isolation."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from mars.core.errors import NotFoundError
from mars.domain.organisation import Facility, OrganisationUnit
from mars.services.geography_service import GeographyService
from mars.services.organisation_service import FacilityService, OrganisationService


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []

    def scalar_one_or_none(self) -> None:
        return None


class _CaptureSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _EmptyResult:
        self.statements.append(statement)
        return _EmptyResult()


def _sql(statement: Any) -> str:
    return str(statement.compile()).lower()


def test_facility_scope_is_intersected_with_geography_scope(
    gulu_facility_principal: Any,
) -> None:
    session = _CaptureSession()
    statement = FacilityService(session)._apply_scope(  # type: ignore[arg-type]
        select(Facility), gulu_facility_principal
    )
    sql = _sql(statement)

    assert "facility.id in" in sql
    assert "geography_unit_1.path like" in sql
    assert "district_geography_unit_id" in sql


def test_alias_resolution_is_geography_scoped(gulu_district_principal: Any) -> None:
    session = _CaptureSession()
    GeographyService(session).find_by_alias(  # type: ignore[arg-type]
        gulu_district_principal, "ubos_fscode", "304101"
    )
    sql = _sql(session.statements[-1])

    assert "join mars_core.geography_unit" in sql
    assert "geography_unit.path like" in sql
    assert "geography_unit_alias.source_system" in sql


def test_unscoped_alias_resolution_returns_no_rows(unscoped_principal: Any) -> None:
    session = _CaptureSession()
    GeographyService(session).find_by_alias(  # type: ignore[arg-type]
        unscoped_principal, "ubos_fscode", "304101"
    )
    assert "false" in _sql(session.statements[-1])


def test_only_explicitly_national_unlinked_organisation_units_are_visible(
    gulu_district_principal: Any,
) -> None:
    session = _CaptureSession()
    statement = OrganisationService(session)._apply_scope(  # type: ignore[arg-type]
        select(OrganisationUnit), gulu_district_principal
    )
    sql = _sql(statement)

    assert "primary_geography_unit_id is null" in sql
    assert "organisation_unit.unit_type" in sql
    assert "geography_unit_1.path like" in sql


def test_single_geography_lookup_is_scoped_before_execution(
    gulu_district_principal: Any,
) -> None:
    session = _CaptureSession()
    service = GeographyService(session)  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        service.get_unit(gulu_district_principal, gulu_district_principal.user_id)
    sql = _sql(session.statements[-1])
    assert "geography_unit.path like" in sql
    assert "geography_unit.id" in sql
