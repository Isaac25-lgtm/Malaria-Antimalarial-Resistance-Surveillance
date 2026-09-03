"""Governed reports must survive leaving the building.

A report is read in a meeting, pasted into a briefing and quoted six months
later by someone who never saw the screen. These tests guard the two ways that
goes wrong: a figure MARS never computed appearing as a zero, and a CSV cell
executing as a formula on a district officer's laptop.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

import pytest

from mars.core.errors import ValidationFailedError
from mars.services.report_service import (
    DISTRICT_BRIEF,
    NATIONAL_BRIEF,
    PRODUCTS,
    ReportService,
    sanitise_cell,
)
from mars.services.surveillance_summary import INTERPRETATION_BOUNDARY

PERIOD = {"period_start": date(2026, 7, 1), "period_end": date(2026, 7, 31)}


class _Result:
    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return []

    def scalar_one(self) -> int:
        return 0

    def scalar_one_or_none(self) -> Any:
        return None


class _Session:
    def execute(self, _statement: Any) -> _Result:
        return _Result()


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def _service(audit: Any = None) -> ReportService:
    return ReportService(_Session(), audit)


class TestAnAbsentFigureStaysAbsent:
    def test_an_unconfigured_measure_is_never_written_as_zero(
        self, national_principal: Any
    ) -> None:
        """The single most dangerous transformation this file could make."""
        report = _service().generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        for row in report.rows:
            if row["status"] == "not_configured":
                assert row["value"] is None

    def test_the_reason_travels_in_its_own_column(self, national_principal: Any) -> None:
        """A spreadsheet cell has nowhere to put a caveat, so the caveat gets a
        column of its own."""
        report = _service().generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        unconfigured = [r for r in report.rows if r["status"] == "not_configured"]
        assert unconfigured
        assert all(r["status_detail"] for r in unconfigured)

    def test_an_absent_value_becomes_an_empty_cell_not_a_zero(
        self, national_principal: Any
    ) -> None:
        """Parsed properly rather than by substring: a genuine zero elsewhere
        in the file (the active-signal count) is a real figure and must stay."""
        service = _service()
        csv_text = service.to_csv(
            service.generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        )
        rows = list(csv.reader(io.StringIO(csv_text)))
        header = next(row for row in rows if row and row[0] == "code")
        value_at = header.index("value")
        status_at = header.index("status")
        body = rows[rows.index(header) + 1 :]

        unconfigured = [row for row in body if row and row[status_at] == "not_configured"]
        assert unconfigured
        for row in unconfigured:
            assert row[value_at] == ""

        # The one real zero in this report is still written as a zero.
        available = [row for row in body if row and row[status_at] == "available"]
        assert any(row[value_at] == "0" for row in available)


class TestTheCaveatTravelsWithTheFile:
    def test_every_report_carries_the_interpretation_limit(self, national_principal: Any) -> None:
        report = _service().generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        assert report.interpretation_limit == INTERPRETATION_BOUNDARY

    def test_the_csv_states_it_before_any_figure(self, national_principal: Any) -> None:
        service = _service()
        csv_text = service.to_csv(
            service.generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        )
        head, rest = csv_text.split("code,label", 1)
        assert "does not confirm antimalarial resistance" in head
        assert rest

    def test_the_csv_names_its_period_and_generation_time(self, national_principal: Any) -> None:
        service = _service()
        csv_text = service.to_csv(
            service.generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        )
        assert "2026-07-01" in csv_text
        assert "generated" in csv_text


class TestCsvCannotExecute:
    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
    def test_a_formula_cell_is_rendered_inert(self, trigger: str) -> None:
        """A surveillance export is exactly the file someone opens without
        thinking."""
        assert sanitise_cell(f"{trigger}cmd|'/c calc'!A1").startswith("'")

    def test_ordinary_text_is_untouched(self) -> None:
        assert sanitise_cell("ENC_CONFIRMED_MALARIA") == "ENC_CONFIRMED_MALARIA"

    def test_none_becomes_an_empty_cell(self) -> None:
        assert sanitise_cell(None) == ""

    def test_a_tab_or_carriage_return_is_also_neutralised(self) -> None:
        assert sanitise_cell("\t=1+1").startswith("'")


class TestScopeAndAudit:
    def test_generation_is_audited_without_logging_the_figures(
        self, national_principal: Any
    ) -> None:
        audit = _RecordingAudit()
        _service(audit).generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        assert len(audit.events) == 1
        context = audit.events[0]["context"]
        assert context["product"] == NATIONAL_BRIEF
        assert "rows" in context
        # The log records that a report was produced and over what, not its
        # contents.
        assert "value" not in context

    def test_a_district_brief_requires_the_district_it_is_about(
        self, national_principal: Any
    ) -> None:
        with pytest.raises(ValidationFailedError):
            _service().generate(national_principal, product=DISTRICT_BRIEF, **PERIOD)

    def test_an_unknown_product_is_refused(self, national_principal: Any) -> None:
        with pytest.raises(ValidationFailedError):
            _service().generate(national_principal, product="resistance_report", **PERIOD)

    def test_only_declared_products_exist(self) -> None:
        assert set(PRODUCTS) == {NATIONAL_BRIEF, DISTRICT_BRIEF}


class TestNoDirectIdentifierLeaves:
    def test_no_report_column_could_hold_one(self, national_principal: Any) -> None:
        report = _service().generate(national_principal, product=NATIONAL_BRIEF, **PERIOD)
        forbidden = {"name", "patient_name", "phone", "nin", "national_id", "address"}
        for row in report.rows:
            assert not (set(row) & forbidden)
