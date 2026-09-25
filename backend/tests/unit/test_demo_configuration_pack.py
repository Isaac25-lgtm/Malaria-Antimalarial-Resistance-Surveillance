"""The demonstration configuration pack, without a database.

The pack must refuse to run anywhere but a demo deployment, and every value in
it must be one the engine it feeds will accept. A pack value an engine rejects
would leave the demo showing *not configured* with no error anywhere.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise
from typing import Any

import pytest

from mars.core.settings import Environment, Settings
from mars.demo.compute import months_between
from mars.demo.configuration_pack import (
    CONFIGURATIONS,
    DEMO_APPROVER,
    METHODS,
    DemoPackRefused,
    demo_principal,
    ensure_allowed,
)
from mars.services.overview import _single_district
from mars.signals.engine import SIGNAL_METHOD_CODE, SignalEngine

DATABASE_URL = "postgresql+psycopg://mars:test@localhost:5432/mars_test"


def _settings(**overrides: Any) -> Settings:
    return Settings(database_url=DATABASE_URL, **overrides)


class TestThePackRefusesOutsideADemo:
    def test_it_refuses_when_demo_mode_is_off(self) -> None:
        with pytest.raises(DemoPackRefused, match="MARS_DEMO_MODE_ENABLED"):
            ensure_allowed(_settings(demo_mode_enabled=False))

    def test_it_refuses_in_a_protected_environment_even_with_demo_mode_on(self) -> None:
        # Settings validation already forbids this combination; the pack must
        # not depend on that, so validation is bypassed here on purpose.
        settings = Settings.model_construct(environment=Environment.STAGING, demo_mode_enabled=True)
        with pytest.raises(DemoPackRefused, match="staging"):
            ensure_allowed(settings)

    def test_it_runs_in_a_local_demo(self) -> None:
        ensure_allowed(_settings(demo_mode_enabled=True))


class TestThePackSaysWhatItIs:
    def test_the_approver_name_is_marked_synthetic_and_fits_the_column(self) -> None:
        assert "synthetic" in DEMO_APPROVER
        assert "not-programme-approved" in DEMO_APPROVER
        assert len(DEMO_APPROVER) <= 128

    def test_the_principal_is_synthetic_and_holds_no_permission(self) -> None:
        principal = demo_principal()
        assert principal.is_synthetic
        assert principal.username == DEMO_APPROVER
        assert not principal.permissions

    def test_every_method_and_setting_is_labelled_as_a_demonstration(self) -> None:
        for method in METHODS:
            assert "demonstration" in method.label
        for configuration in CONFIGURATIONS:
            assert "demonstration" in configuration.label


class TestEngineAcceptsEveryPackValue:
    def test_the_signal_rules_parse(self) -> None:
        method = next(m for m in METHODS if m.code == SIGNAL_METHOD_CODE)
        engine = SignalEngine(None)  # type: ignore[arg-type]
        rules = [engine._parse_rule(raw) for raw in method.parameters["rules"]]
        codes = [rule.code for rule in rules]
        assert len(codes) == len(set(codes))
        assert all(code.startswith("demo-") for code in codes)

    def test_the_interval_bands_are_ordered_and_do_not_overlap(self) -> None:
        bands = next(c for c in CONFIGURATIONS if c.key == "recurrence_interval_bands_days")
        entries = bands.value["bands"]
        for previous, current in pairwise(entries):
            assert previous["upper_days"] is not None
            assert current["lower_days"] == previous["upper_days"] + 1
        assert entries[-1]["upper_days"] is None


class TestComputeMonths:
    def test_months_cover_a_span_that_starts_and_ends_mid_month(self) -> None:
        months = months_between(date(2025, 11, 15), date(2026, 2, 3))
        assert months == [
            (date(2025, 11, 1), date(2025, 11, 30)),
            (date(2025, 12, 1), date(2025, 12, 31)),
            (date(2026, 1, 1), date(2026, 1, 31)),
            (date(2026, 2, 1), date(2026, 2, 28)),
        ]


class TestOverviewKeyMeasuresReadTheUsersDistrict:
    def test_a_district_user_reads_their_district(self, pader_district_principal: Any) -> None:
        scope = pader_district_principal.geography_scopes[0]
        assert _single_district(pader_district_principal) == scope.geography_unit_id

    def test_a_national_user_reads_the_national_figure(self, national_principal: Any) -> None:
        assert _single_district(national_principal) is None

    def test_a_facility_user_is_never_given_the_district_figure(
        self, gulu_facility_principal: Any
    ) -> None:
        assert _single_district(gulu_facility_principal) is None
