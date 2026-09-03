"""The spatial vocabulary, without a database.

A blank cell on a malaria map is read as *no malaria here*. These tests guard
the machinery that stops MARS producing one: six distinct cell statuses, a
hotspot that cannot exist without a method, and a privacy gate whose thresholds
are named but never shipped.

A refactor that collapses two statuses into one, or gives a threshold a
default, would pass every test that only looks at populated cells. These would
fail.
"""

from __future__ import annotations

from mars.analytics.geographic import RESIDENCE_MEASURES
from mars.analytics.hotspot import (
    HOTSPOT_DEFINITION_CODE,
    INTERPRETATION_LIMIT,
    PERSISTENCE_PARAMETER,
    REQUIRED_PARAMETERS,
)
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    HotspotOutcome,
    SpatialAggregationBasis,
    SpatialCellStatus,
)
from mars.domain.spatial import GeographicAggregationResult, HotspotResult, SpatialRun
from mars.services.spatial_availability import (
    GRAIN_ORDER,
    GRAIN_TO_LEVEL,
    PATIENT_DERIVED_SERIES,
    PRIVACY_POLICY_KEY,
    REQUIRED_POLICY_KEYS,
)

UNEXAMINED = {
    HotspotOutcome.NOT_EVALUATED_NO_OBSERVATION,
    HotspotOutcome.NOT_EVALUATED_NO_BASELINE,
    HotspotOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT,
    HotspotOutcome.NOT_EVALUATED_INCOMPLETE_REPORTING,
}


def constraints(model: type) -> set[str]:
    return {c.name for c in model.__table__.constraints if c.name}


def columns(model: type) -> set[str]:
    return {c.name for c in model.__table__.columns}


class TestABlankCellHasSixPossibleMeanings:
    def test_each_meaning_is_its_own_status(self) -> None:
        """One colour, six situations, and only one of them is good news."""
        assert {status.value for status in SpatialCellStatus} == {
            "available",
            "missing",
            "suppressed",
            "unavailable",
            "not_configured",
            "outside_scope",
        }

    def test_zero_is_not_one_of_them(self) -> None:
        """A reported zero is a value with status ``available``. Giving it a
        status of its own would invite treating it as an absence."""
        assert "zero" not in {status.value for status in SpatialCellStatus}
        assert SpatialCellStatus.AVAILABLE.value == "available"

    def test_suppressed_and_missing_are_different_facts(self) -> None:
        """One means a real value was withheld; the other means nobody
        reported. A map that conflates them cannot be audited."""
        assert SpatialCellStatus.SUPPRESSED is not SpatialCellStatus.MISSING


class TestAHotspotMustHaveAMethod:
    def test_not_a_hotspot_means_examined(self) -> None:
        assert "ck_hotspot_result_not_hotspot_means_examined" in constraints(HotspotResult)

    def test_a_hotspot_carries_its_method(self) -> None:
        assert "ck_hotspot_result_a_hotspot_carries_its_method" in constraints(HotspotResult)

    def test_the_definition_and_the_baseline_are_separate_decisions(self) -> None:
        """One says how large a departure has to be; the other says what normal
        was. A single column would lose which was which."""
        present = columns(HotspotResult)
        assert "method_version_id" in present
        assert "baseline_method_version_id" in present

    def test_each_reason_for_not_examining_is_its_own_outcome(self) -> None:
        assert set(HotspotOutcome) - UNEXAMINED == {
            HotspotOutcome.HOTSPOT,
            HotspotOutcome.NOT_HOTSPOT,
        }
        assert len(UNEXAMINED) == 4

    def test_persistence_is_counted_but_labelled_only_under_a_rule(self) -> None:
        present = columns(HotspotResult)
        assert "consecutive_periods" in present
        assert "is_persistent" in present
        assert "ck_hotspot_result_persistent_requires_configuration" in constraints(HotspotResult)

    def test_first_and_last_detected_are_recorded(self) -> None:
        """Blueprint 037 asks for both, plus the consecutive count."""
        present = columns(HotspotResult)
        assert "first_detected_period_start" in present
        assert "last_detected_period_end" in present

    def test_a_hotspot_is_an_area_not_a_facility(self) -> None:
        assert "ck_hotspot_result_a_hotspot_is_an_area_not_a_facility" in constraints(HotspotResult)


class TestAggregationKeepsWhatItWasBuiltFrom:
    def test_an_aggregate_is_never_a_facility(self) -> None:
        assert "ck_geographic_aggregation_result_not_a_facility_grain" in constraints(
            GeographicAggregationResult
        )

    def test_a_rate_needs_a_denominator(self) -> None:
        assert "ck_geographic_aggregation_result_a_rate_needs_a_denominator" in constraints(
            GeographicAggregationResult
        )

    def test_completeness_travels_on_the_row(self) -> None:
        """A district figure built from three of twenty facilities is not a
        district figure, and a reader who cannot see that will treat it as one."""
        present = columns(GeographicAggregationResult)
        assert {"contributing_facilities", "expected_facilities", "reporting_completeness"} <= (
            present
        )

    def test_unresolved_contributions_are_counted(self) -> None:
        """Their absence always makes a residence map look emptier than the
        truth."""
        assert "unresolved_contributions" in columns(GeographicAggregationResult)

    def test_the_two_bases_are_stored_not_merged(self) -> None:
        assert {basis.value for basis in SpatialAggregationBasis} == {
            "residence",
            "facility_location",
        }
        assert "aggregation_basis" in columns(GeographicAggregationResult)

    def test_only_encounter_countable_measures_roll_up_by_residence(self) -> None:
        """A facility's missing-prescription count has no residence."""
        assert len(RESIDENCE_MEASURES) == 2
        assert {measure.value for measure in RESIDENCE_MEASURES} == {
            "testing_coverage",
            "test_positivity",
        }


class TestRefusalsAreRecords:
    def test_a_refused_run_names_what_is_missing(self) -> None:
        assert "ck_spatial_run_refusals_name_what_is_missing" in constraints(SpatialRun)

    def test_a_completed_hotspot_run_carries_its_definition(self) -> None:
        assert "ck_spatial_run_completed_hotspot_runs_carry_their_definition" in constraints(
            SpatialRun
        )


class TestPrivacyConfigurationIsNamedNotShipped:
    def test_the_policy_key_is_only_a_name(self) -> None:
        assert PRIVACY_POLICY_KEY == "spatial_privacy_policy"

    def test_no_minimum_cell_count_is_defined_in_this_module(self) -> None:
        """What counts as a cell too small to show is a disclosure decision. It
        belongs to the programme and its data protection authority."""
        assert REQUIRED_POLICY_KEYS == ("minimum_cell_count", "minimum_aggregation_level")
        assert all(isinstance(key, str) for key in REQUIRED_POLICY_KEYS)

    def test_every_series_this_service_serves_passes_through_the_gate(self) -> None:
        """Written out rather than assumed, so a later operational layer - a
        store having no artemether-lumefantrine is a fact about a store, not
        about a person - is not swept into the gate by default."""
        assert frozenset(BaselineSeriesKind) == PATIENT_DERIVED_SERIES
        assert all("commodity" not in kind.value for kind in PATIENT_DERIVED_SERIES)

    def test_grain_order_runs_coarse_to_fine(self) -> None:
        assert GRAIN_ORDER[GeographyGrain.NATIONAL] < GRAIN_ORDER[GeographyGrain.DISTRICT]
        assert GRAIN_ORDER[GeographyGrain.DISTRICT] < GRAIN_ORDER[GeographyGrain.SUBCOUNTY]
        assert GRAIN_ORDER[GeographyGrain.SUBCOUNTY] < GRAIN_ORDER[GeographyGrain.FACILITY]

    def test_the_facility_grain_has_no_administrative_level(self) -> None:
        """A facility is a reporting unit. Mapping patient-derived figures to
        facility points is what the blueprint forbids."""
        assert GeographyGrain.FACILITY not in GRAIN_TO_LEVEL


class TestNoHotspotClaimsACause:
    def test_the_interpretation_limit_denies_the_readings_it_invites(self) -> None:
        assert "area worth visiting" in INTERPRETATION_LIMIT
        assert "not a diagnosis" in INTERPRETATION_LIMIT
        assert "outbreak declaration" in INTERPRETATION_LIMIT
        assert "resistance" in INTERPRETATION_LIMIT


class TestHotspotConfigurationIsNamedNotShipped:
    def test_the_definition_code_is_only_a_name(self) -> None:
        assert HOTSPOT_DEFINITION_CODE == "hotspot_definition"

    def test_blueprint_037_parameters_are_all_required(self) -> None:
        assert REQUIRED_PARAMETERS == (
            "detection_method",
            "deviation_threshold",
            "minimum_case_count",
            "minimum_completeness",
        )

    def test_persistence_is_optional_and_separate(self) -> None:
        assert PERSISTENCE_PARAMETER not in REQUIRED_PARAMETERS
