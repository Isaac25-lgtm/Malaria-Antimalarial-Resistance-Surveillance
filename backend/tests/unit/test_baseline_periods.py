"""Baseline period arithmetic, without a database.

Getting the comparison period wrong is the quiet failure mode of a baseline
engine: nothing errors, the figures look plausible, and every district is
compared against the wrong month. These tests pin the arithmetic down.
"""

from __future__ import annotations

from datetime import date

import pytest

from mars.analytics.baseline import (
    BASELINE_METHOD_CODE,
    DISPERSION_FOR_METHOD,
    REQUIRED_PARAMETERS,
    UNCERTAINTY_PARAMETER,
    preceding_periods,
    seasonal_periods,
)
from mars.domain.enums import BaselineMethod, DispersionMeasure, PeriodGrain


class TestPrecedingPeriods:
    def test_months_walk_back_by_calendar_month(self) -> None:
        """Not by thirty days. A calendar month is what the source reports, and
        a thirty-day block would straddle two of them."""
        periods = preceding_periods(date(2026, 3, 1), date(2026, 3, 31), PeriodGrain.MONTH, 3)
        assert periods == [
            (date(2026, 2, 1), date(2026, 2, 28)),
            (date(2026, 1, 1), date(2026, 1, 31)),
            (date(2025, 12, 1), date(2025, 12, 31)),
        ]

    def test_a_month_walk_crosses_the_year_boundary(self) -> None:
        periods = preceding_periods(date(2026, 1, 1), date(2026, 1, 31), PeriodGrain.MONTH, 2)
        assert periods == [
            (date(2025, 12, 1), date(2025, 12, 31)),
            (date(2025, 11, 1), date(2025, 11, 30)),
        ]

    def test_february_length_follows_the_year(self) -> None:
        periods = preceding_periods(date(2024, 3, 1), date(2024, 3, 31), PeriodGrain.MONTH, 1)
        assert periods == [(date(2024, 2, 1), date(2024, 2, 29))]

    def test_weeks_are_seven_days_and_stay_aligned(self) -> None:
        periods = preceding_periods(
            date(2026, 3, 2), date(2026, 3, 8), PeriodGrain.EPIDEMIOLOGICAL_WEEK, 2
        )
        assert periods == [
            (date(2026, 2, 23), date(2026, 3, 1)),
            (date(2026, 2, 16), date(2026, 2, 22)),
        ]
        for start, _ in periods:
            assert start.weekday() == 0

    def test_the_target_period_is_never_part_of_its_own_history(self) -> None:
        """Including it would let a period vote for its own expectation."""
        periods = preceding_periods(date(2026, 3, 1), date(2026, 3, 31), PeriodGrain.MONTH, 6)
        assert (date(2026, 3, 1), date(2026, 3, 31)) not in periods

    def test_asking_for_none_returns_none(self) -> None:
        assert preceding_periods(date(2026, 3, 1), date(2026, 3, 31), PeriodGrain.MONTH, 0) == []


class TestSeasonalPeriods:
    def test_the_same_month_in_previous_years(self) -> None:
        """Malaria in Uganda is seasonal. Comparing March against February
        flags the season rather than an event."""
        periods, skipped = seasonal_periods(
            date(2026, 3, 1), date(2026, 3, 31), PeriodGrain.MONTH, 3
        )
        assert periods == [
            (date(2025, 3, 1), date(2025, 3, 31)),
            (date(2024, 3, 1), date(2024, 3, 31)),
            (date(2023, 3, 1), date(2023, 3, 31)),
        ]
        assert skipped == []

    def test_the_same_iso_week_in_previous_years(self) -> None:
        periods, skipped = seasonal_periods(
            date(2026, 3, 2), date(2026, 3, 8), PeriodGrain.EPIDEMIOLOGICAL_WEEK, 2
        )
        target_week = date(2026, 3, 2).isocalendar().week
        assert skipped == []
        for start, end in periods:
            assert start.isocalendar().week == target_week
            assert (end - start).days == 6

    def test_a_year_without_week_53_is_recorded_not_dropped(self) -> None:
        """Silently skipping it would shorten the history behind an
        expectation without saying so."""
        # 2020 has an ISO week 53; 2019 and 2018 do not.
        periods, skipped = seasonal_periods(
            date(2020, 12, 28), date(2021, 1, 3), PeriodGrain.EPIDEMIOLOGICAL_WEEK, 2
        )
        assert len(periods) + len(skipped) == 2
        assert skipped
        assert all(entry["reason"] == "iso_week_absent" for entry in skipped)

    def test_the_twenty_ninth_of_february_is_recorded_not_dropped(self) -> None:
        periods, skipped = seasonal_periods(
            date(2024, 2, 29), date(2024, 2, 29), PeriodGrain.DAY, 2
        )
        assert len(periods) + len(skipped) == 2
        assert any(entry["reason"] == "date_absent_in_year" for entry in skipped)


class TestMethodAndDispersionArePaired:
    @pytest.mark.parametrize("method", list(BaselineMethod))
    def test_every_method_names_its_dispersion(self, method: BaselineMethod) -> None:
        assert method in DISPERSION_FOR_METHOD

    def test_a_median_is_summarised_robustly(self) -> None:
        """A robust centre reported with a fragile spread would understate how
        variable a series is at exactly the moments that matter."""
        assert (
            DISPERSION_FOR_METHOD[BaselineMethod.HISTORICAL_MEDIAN]
            is DispersionMeasure.MEDIAN_ABSOLUTE_DEVIATION
        )
        assert (
            DISPERSION_FOR_METHOD[BaselineMethod.SEASONAL_PERIOD_OF_YEAR_MEDIAN]
            is DispersionMeasure.MEDIAN_ABSOLUTE_DEVIATION
        )

    def test_a_mean_is_summarised_by_a_standard_deviation(self) -> None:
        assert (
            DISPERSION_FOR_METHOD[BaselineMethod.HISTORICAL_MEAN]
            is DispersionMeasure.STANDARD_DEVIATION
        )


class TestConfigurationIsNamedNotShipped:
    def test_the_method_code_is_only_a_name(self) -> None:
        assert BASELINE_METHOD_CODE == "historical_baseline"

    def test_no_window_or_minimum_is_defined_in_this_module(self) -> None:
        """Every required parameter is a name. None has a value: the window
        decides what counts as normal."""
        assert REQUIRED_PARAMETERS == (
            "baseline_method",
            "history_periods",
            "minimum_history_periods",
            "minimum_completeness",
        )
        assert all(isinstance(name, str) for name in REQUIRED_PARAMETERS)

    def test_the_uncertainty_multiplier_is_optional_and_separate(self) -> None:
        """A baseline without it has a centre and no band, which is honest."""
        assert UNCERTAINTY_PARAMETER not in REQUIRED_PARAMETERS
