"""Dashboard overview snapshot: provenance, scope labels, no invented figures."""

from __future__ import annotations

from datetime import date
from typing import Any

from mars.core.settings import Settings
from mars.services.overview import OverviewService

PERIOD = {"period_start": date(2026, 7, 1), "period_end": date(2026, 7, 31)}


class _Result:
    def __init__(self) -> None:
        self._rows: list[Any] = []

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return None

    def scalar_one(self) -> Any:
        return 0

    def scalar_one_or_none(self) -> Any:
        return None

    def __iter__(self) -> Any:
        return iter(self._rows)


class _EmptySession:
    def execute(self, _statement: Any) -> _Result:
        return _Result()


def _service(*, demo: bool = True) -> OverviewService:
    settings = Settings(
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_test",
        demo_mode_enabled=demo,
    )
    return OverviewService(_EmptySession(), settings)


class TestOverviewDoesNotInventCoverage:
    def test_a_national_account_is_labelled_national(self, national_principal: Any) -> None:
        snap = _service().snapshot(national_principal, **PERIOD)
        assert snap["title"] == "National Overview"
        assert snap["has_national_scope"] is True
        assert snap["data_mode"] == "synthetic"
        assert "not a live" in snap["data_mode_detail"].lower()

    def test_a_pader_account_is_never_labelled_national(
        self, pader_district_principal: Any
    ) -> None:
        snap = _service().snapshot(pader_district_principal, **PERIOD)
        assert snap["title"] == "Pader Overview"
        assert snap["has_national_scope"] is False
        assert snap["requested_scope"] == "pader"
        assert "national" not in snap["title"].lower()

    def test_every_section_carries_provenance(self, national_principal: Any) -> None:
        snap = _service().snapshot(national_principal, **PERIOD)
        for key in (
            "kpis",
            "signals_by_priority",
            "investigations_by_status",
            "districts_requiring_review",
            "commodity_alerts",
            "needs_attention",
            "recent_signals",
            "confirmed_malaria_trend",
            "testing_positivity",
        ):
            section = snap[key]
            assert section["availability"]
            assert section["requested_scope"]
            assert section["reporting_period"]["start"] == PERIOD["period_start"]
            assert section["source"]
            assert "items" in section

    def test_trend_panels_do_not_fabricate_a_series(self, national_principal: Any) -> None:
        snap = _service().snapshot(national_principal, **PERIOD)
        assert snap["confirmed_malaria_trend"]["availability"] == "not_configured"
        assert snap["confirmed_malaria_trend"]["items"] == []
        assert snap["testing_positivity"]["items"] == []

    def test_overdue_attention_is_omitted_without_an_sla(self, national_principal: Any) -> None:
        snap = _service().snapshot(national_principal, **PERIOD)
        overdue = next(
            item
            for item in snap["needs_attention"]["items"]
            if item["code"] == "investigations_overdue"
        )
        assert overdue["count"] is None
        assert overdue["status"] == "not_configured"

    def test_an_unconfigured_deployment_does_not_claim_to_be_live(
        self, national_principal: Any
    ) -> None:
        snap = _service(demo=False).snapshot(national_principal, **PERIOD)
        assert snap["data_mode"] == "unavailable"
        assert snap["data_mode"] != "live"
