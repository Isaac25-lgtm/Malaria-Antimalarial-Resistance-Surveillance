"""Surveillance domain shapes and arithmetic, without a database.

The three engines share an envelope and nothing else, and these tests are what
keeps that true. They check the two things a later refactor is most likely to
break quietly:

* that heterogeneous evidence stays in its own table, so no column means one
  thing for testing and another for commodities;
* that a commodity alert cannot be mistaken for an epidemiological signal,
  because it has nowhere to put a score, a suspicion or a resistance claim.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from mars.analytics.surveillance import (
    COMMODITY_RULES_KEY,
    TESTING_COMMODITIES,
    TREATMENT_COMMODITIES,
    WATCHED_COMMODITIES,
    Envelope,
    SurveillanceReport,
    _proportion,
)
from mars.domain import enums as domain_enums
from mars.domain import surveillance as surveillance_models
from mars.domain.enums import (
    AlertSeverity,
    CommodityAlertKind,
    CommodityFactKind,
    IndicatorValueStatus,
    PeriodGrain,
    TreatmentMeasure,
)
from mars.domain.surveillance import (
    CommodityOperationalAlert,
    CommodityStockFact,
    TreatmentSurveillanceResult,
)

# ``TestingSurveillanceResult`` and ``TestingMeasure`` are reached through their
# modules rather than imported by name: pytest tries to collect any module-level
# name beginning with "Test", and both of those do.

ENVELOPE_COLUMNS = {
    "geography_grain",
    "geography_unit_id",
    "facility_id",
    "period_start",
    "period_end",
    "period_grain",
    "indicator_version_id",
    "method_version_id",
    "configuration_version_id",
    "boundary_version_id",
    "input_fingerprint",
    "source_cutoff",
    "engine_version",
    "computed_at",
    "contributing_units",
    "expected_units",
    "quality_context",
}


def columns(model: type) -> set[str]:
    return {column.name for column in model.__table__.columns}


class TestBlankIsNotZero:
    """The distinction the rest of MARS spends its effort keeping."""

    def test_no_denominator_yields_no_value(self) -> None:
        value, status = _proportion(0, 0)
        assert value is None
        assert status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_facility_that_tested_nobody_has_no_positivity(self) -> None:
        """Not a positivity of zero, which would read as no malaria here."""
        value, status = _proportion(0, None)
        assert value is None
        assert status is IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR

    def test_a_real_zero_numerator_is_kept(self) -> None:
        """Zero positives out of forty tests is a figure, not a gap."""
        value, status = _proportion(0, 40)
        assert value == Decimal("0.000000")
        assert status is IndicatorValueStatus.AVAILABLE

    def test_a_proportion_is_not_rounded_to_a_percentage(self) -> None:
        value, _ = _proportion(1, 3)
        assert value == Decimal("0.333333")


class TestEvidenceStaysInItsOwnTable:
    """Domain-specific facts, not one table of nullable columns."""

    @pytest.mark.parametrize(
        "model",
        [
            surveillance_models.TestingSurveillanceResult,
            TreatmentSurveillanceResult,
            CommodityStockFact,
        ],
        ids=["testing", "treatment", "commodity"],
    )
    def test_every_result_carries_the_shared_envelope(self, model: type) -> None:
        assert columns(model) >= ENVELOPE_COLUMNS

    def test_testing_evidence_is_not_treatment_evidence(self) -> None:
        testing = columns(surveillance_models.TestingSurveillanceResult) - ENVELOPE_COLUMNS
        treatment = columns(TreatmentSurveillanceResult) - ENVELOPE_COLUMNS
        assert "untested_encounters" in testing
        assert "untested_encounters" not in treatment
        assert "missing_treatment_information" in treatment
        assert "missing_treatment_information" not in testing

    def test_commodity_facts_carry_a_unit_of_issue_and_the_others_do_not(self) -> None:
        """Boxes and vials mean nothing to a positivity rate."""
        assert "unit_of_issue" in columns(CommodityStockFact)
        assert "unit_of_issue" not in columns(surveillance_models.TestingSurveillanceResult)
        assert "unit_of_issue" not in columns(TreatmentSurveillanceResult)

    def test_no_result_table_stores_a_direct_identifier(self) -> None:
        forbidden = {"patient_name", "name", "phone", "phone_number", "nin", "national_id"}
        for model in (
            surveillance_models.TestingSurveillanceResult,
            TreatmentSurveillanceResult,
            CommodityStockFact,
            CommodityOperationalAlert,
        ):
            assert not (columns(model) & forbidden), model.__tablename__


class TestAnAlertIsNotASignal:
    """Structural separation, not a naming convention.

    A stock-out needs a district pharmacist; a treatment-response signal needs
    an epidemiologist and a laboratory. If both lived in one table with a kind
    column, converting one into the other would be a one-line change - and that
    conversion is the claim MARS must never make silently.
    """

    def test_the_alert_table_is_separate_from_every_result_table(self) -> None:
        assert CommodityOperationalAlert.__tablename__ == "commodity_operational_alert"
        assert CommodityOperationalAlert.__tablename__ not in {
            surveillance_models.TestingSurveillanceResult.__tablename__,
            TreatmentSurveillanceResult.__tablename__,
            CommodityStockFact.__tablename__,
        }

    def test_an_alert_has_nowhere_to_record_a_score_or_a_suspicion(self) -> None:
        present = columns(CommodityOperationalAlert)
        for absent in (
            "score",
            "signal_score",
            "weight",
            "resistance",
            "suspected_resistance",
            "treatment_failure",
            "efficacy",
        ):
            assert absent not in present

    def test_an_alert_carries_no_analytical_value_column(self) -> None:
        """It restates a supply fact; it computes nothing."""
        assert "value" not in columns(CommodityOperationalAlert)
        assert "value_status" not in columns(CommodityOperationalAlert)

    def test_severity_starts_unclassified(self) -> None:
        assert AlertSeverity.UNCLASSIFIED.value == "unclassified"
        assert next(iter(AlertSeverity)) is AlertSeverity.UNCLASSIFIED


class TestOnlyReportedFactsAreRaisableWithoutGovernance:
    def test_one_alert_kind_restates_the_source_and_the_rest_are_judgements(self) -> None:
        judgements = {
            CommodityAlertKind.PROLONGED_STOCK_OUT,
            CommodityAlertKind.REPEATED_STOCK_OUT,
            CommodityAlertKind.MULTI_COMMODITY_STOCK_OUT,
            CommodityAlertKind.LOW_STOCK,
            CommodityAlertKind.IMMINENT_STOCK_OUT,
        }
        assert set(CommodityAlertKind) - judgements == {CommodityAlertKind.STOCK_OUT_REPORTED}

    def test_the_database_refuses_a_classified_alert_without_a_rule(self) -> None:
        """Read off the constraint rather than the engine, so an alert inserted
        by any future code path is held to the same rule."""
        names = {constraint.name for constraint in CommodityOperationalAlert.__table__.constraints}
        assert "ck_commodity_operational_alert_classified_alerts_need_config" in names
        assert "ck_commodity_operational_alert_severity_requires_configuration" in names

    def test_a_fact_must_carry_the_evidence_it_asserts(self) -> None:
        names = {constraint.name for constraint in CommodityStockFact.__table__.constraints}
        assert "ck_commodity_stock_fact_fact_carries_its_evidence" in names

    def test_not_reported_is_its_own_kind(self) -> None:
        """A blank stock column is a reporting gap, not a stock-out, and the
        difference matters most exactly when supply has failed."""
        assert CommodityFactKind.STOCK_NOT_REPORTED in set(CommodityFactKind)
        assert CommodityFactKind.STOCK_NOT_REPORTED is not CommodityFactKind.STOCK_ON_HAND_ZERO


class TestConfigurationIsNamedNotShipped:
    def test_the_rules_key_is_only_a_name(self) -> None:
        assert COMMODITY_RULES_KEY == "commodity_alert_rules"

    def test_no_threshold_values_are_defined_in_this_module(self) -> None:
        """The commodity constants are transcription facts - the codes HMIS 105
        prints - not judgements about how much stock is too little."""
        assert set(TESTING_COMMODITIES) | set(TREATMENT_COMMODITIES) == set(WATCHED_COMMODITIES)
        assert TESTING_COMMODITIES == ("SS34",)
        assert set(TREATMENT_COMMODITIES) == {"SS01", "SS02", "SS24"}


class TestReportShape:
    def test_a_skipped_classification_is_reported_not_hidden(self) -> None:
        report = SurveillanceReport(domain="commodity")
        report.classifications_skipped = ["low_stock", "imminent_stock_out"]
        assert report.as_dict()["classifications_skipped"] == [
            "imminent_stock_out",
            "low_stock",
        ]

    def test_the_envelope_names_every_column_the_tables_expect(self) -> None:
        envelope = Envelope(
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            period_grain=PeriodGrain.MONTH,
            source_cutoff=datetime(2026, 2, 1, tzinfo=UTC),
        )
        produced = set(envelope.as_columns())
        assert produced <= ENVELOPE_COLUMNS
        # The two the engine supplies per row rather than per run.
        assert ENVELOPE_COLUMNS - produced == {"input_fingerprint", "quality_context"}


class TestMeasuresAreDistinctBetweenDomains:
    def test_no_measure_name_is_shared(self) -> None:
        """Sharing one would let a testing figure be read as a treatment one."""
        assert not (
            {m.value for m in domain_enums.TestingMeasure} & {m.value for m in TreatmentMeasure}
        )
