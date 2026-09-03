"""The command centre's read model, without a database.

The screen these records feed is the one a Ministry of Health user opens first,
so the tests here guard the property that makes it trustworthy: **an absent
figure is never rendered as a zero.** A fresh deployment has no approved
indicator versions, and the correct national screen for that system explains
itself rather than showing a country with no malaria in it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from mars.core.errors import ValidationFailedError
from mars.services.surveillance_summary import (
    ACTIVE_SIGNALS_CODE,
    INTERPRETATION_BOUNDARY,
    KPI_INDICATORS,
    STATUS_AVAILABLE,
    STATUS_NOT_CONFIGURED,
    STATUS_UNAVAILABLE,
    SurveillanceSummaryService,
)

PERIOD = {"period_start": date(2026, 7, 1), "period_end": date(2026, 7, 31)}


class _Result:
    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> Any:
        return self._scalar if self._scalar is not None else 0

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def __iter__(self) -> Any:
        return iter(self._rows)


class _EmptySession:
    """A database with a registered catalogue and nothing approved."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result()


def _service() -> SurveillanceSummaryService:
    return SurveillanceSummaryService(_EmptySession())


class TestAnUnconfiguredSystemSaysSo:
    def test_every_indicator_kpi_reports_not_configured(self, national_principal: Any) -> None:
        """Not zero. A zero on this screen reads as an absence of malaria."""
        records = _service().kpis(national_principal, **PERIOD)
        indicators = [r for r in records if r["code"] != ACTIVE_SIGNALS_CODE]
        assert len(indicators) == len(KPI_INDICATORS)
        for record in indicators:
            assert record["status"] == STATUS_NOT_CONFIGURED
            assert record["value"] is None

    def test_the_refusal_names_the_indicator_whose_approval_is_missing(
        self, national_principal: Any
    ) -> None:
        records = _service().kpis(national_principal, **PERIOD)
        for record in records:
            if record["code"] == ACTIVE_SIGNALS_CODE:
                continue
            assert record["missing_configuration"] == [f"indicator:{record['code']}"]
            assert record["status_detail"]

    def test_no_kpi_is_computed_in_the_absence_of_a_governed_definition(
        self, national_principal: Any
    ) -> None:
        """Every KPI names a governed indicator code as its source, so there is
        nowhere for a frontend formula to hide."""
        for record in _service().kpis(national_principal, **PERIOD):
            assert record["source"].startswith(("indicator:", "table:"))

    def test_provenance_states_the_system_is_not_analytically_configured(
        self, national_principal: Any
    ) -> None:
        provenance = _service().provenance(national_principal, **PERIOD)
        assert provenance["analytically_configured"] is False
        assert "not configured rather than as zero" in provenance["configuration_detail"]


class TestZeroAndAbsenceAreDifferentAnswers:
    def test_no_active_signals_is_a_real_zero(self, national_principal: Any) -> None:
        """A count of governed records is available even when it is zero. That
        is different from a measure that was never computed."""
        signals = next(
            r
            for r in _service().kpis(national_principal, **PERIOD)
            if r["code"] == ACTIVE_SIGNALS_CODE
        )
        assert signals["status"] == STATUS_AVAILABLE
        assert signals["value"] == "0"
        assert "not the same as no analysis having run" in signals["status_detail"]

    def test_the_statuses_a_screen_must_distinguish_are_all_declared(self) -> None:
        assert STATUS_AVAILABLE != STATUS_UNAVAILABLE != STATUS_NOT_CONFIGURED


class TestScope:
    def test_a_facility_user_reads_no_national_indicator_figure(
        self, gulu_facility_principal: Any
    ) -> None:
        """The rule from 64e3e21. A facility's district membership does not
        grant the national or district surveillance picture."""
        records = _service().kpis(gulu_facility_principal, **PERIOD)
        for record in records:
            if record["code"] == ACTIVE_SIGNALS_CODE:
                continue
            assert record["value"] is None

    def test_a_facility_user_gets_no_priority_district_list(
        self, gulu_facility_principal: Any
    ) -> None:
        assert _service().priority_districts(gulu_facility_principal, **PERIOD) == []


class TestEveryFigureCarriesItsContext:
    def test_each_record_states_its_period_and_scope(self, national_principal: Any) -> None:
        for record in _service().kpis(national_principal, **PERIOD):
            assert record["period"]["start"] == PERIOD["period_start"]
            assert record["period"]["end"] == PERIOD["period_end"]
            assert record["geography_grain"] == "national"

    def test_provenance_carries_the_interpretation_boundary(self, national_principal: Any) -> None:
        provenance = _service().provenance(national_principal, **PERIOD)
        assert provenance["interpretation_boundary"] == INTERPRETATION_BOUNDARY
        assert "does not confirm antimalarial resistance" in INTERPRETATION_BOUNDARY

    def test_a_reversed_period_is_refused_before_any_query(self, national_principal: Any) -> None:
        session = _EmptySession()
        service = SurveillanceSummaryService(session)
        with pytest.raises(ValidationFailedError):
            service.kpis(
                national_principal,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 8, 1),
            )
        assert session.statements == []


class TestDistrictScopedSummary:
    def test_a_district_summary_reports_the_district_grain(self, national_principal: Any) -> None:
        unit_id = uuid.UUID(int=42)
        records = _service().kpis(national_principal, geography_unit_id=unit_id, **PERIOD)
        for record in records:
            assert record["geography_grain"] == "district"
            assert record["geography_unit_id"] == unit_id


class TestAFacilityWorkspaceNeverInheritsItsDistrict:
    """The rule 64e3e21 established, now at the workspace boundary.

    A facility figure and a district figure are different quantities. A
    workspace that showed one under the other's heading would hand a facility
    account the district-wide picture by the back door.
    """

    def test_a_facility_summary_reports_the_facility_grain(self, national_principal: Any) -> None:
        facility_id = uuid.UUID(int=7)
        records = _service().kpis(national_principal, facility_id=facility_id, **PERIOD)
        for record in records:
            assert record["geography_grain"] == "facility"
            assert record["facility_id"] == facility_id

    def test_a_facility_summary_carries_no_geography_unit(self, national_principal: Any) -> None:
        records = _service().kpis(national_principal, facility_id=uuid.UUID(int=7), **PERIOD)
        for record in records:
            assert record["geography_unit_id"] is None

    def test_a_facility_user_reading_another_facility_gets_nothing(
        self, gulu_facility_principal: Any
    ) -> None:
        """Their own facility is in scope; a neighbour's is not, and the answer
        is an absent figure rather than a filtered-down one."""
        stranger = uuid.UUID(int=999)
        records = _service().kpis(gulu_facility_principal, facility_id=stranger, **PERIOD)
        for record in records:
            assert record["value"] is None or record["code"] == ACTIVE_SIGNALS_CODE

    def test_a_facility_user_gets_no_roster_of_its_neighbours(
        self, gulu_facility_principal: Any
    ) -> None:
        assert (
            _service().facility_contributions(
                gulu_facility_principal, geography_unit_id=uuid.UUID(int=3), **PERIOD
            )
            == []
        )
