"""The shipped indicator catalogue, without a database.

The catalogue is a set of promises about what MARS's numbers mean. These tests
check the promises that are easy to break silently:

* nothing carries a threshold, target or alert level - those are programme
  decisions, and an indicator that shipped with one would make every consumer
  inherit a judgement nobody signed;
* every definition cites where it came from;
* every proportion states what happens when its denominator is undefined;
* the blank rule is stated everywhere it applies, and says the same thing.
"""

from __future__ import annotations

import json

import pytest

from mars.analytics.indicator_catalogue import (
    CATALOGUE,
    CATALOGUE_BY_CODE,
    CatalogueEntry,
    entries_for_domain,
)
from mars.domain.enums import (
    EvidenceLane,
    IndicatorSourceDomain,
    IndicatorUnit,
)

#: Words that would mean the catalogue had taken a programme decision.
THRESHOLD_WORDS = (
    "threshold",
    "target",
    "alert_level",
    "cutoff",
    "trigger_at",
    "severity",
    "must_exceed",
)


class TestNoProgrammeDecisionIsShipped:
    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.code)
    def test_no_specification_carries_a_threshold(self, entry: CatalogueEntry) -> None:
        """A definition says how to compute a figure. What counts as too high
        is a decision the programme makes, held in the configuration registry
        and absent until approved."""
        rendered = json.dumps(entry.specification).lower()
        for word in THRESHOLD_WORDS:
            assert word not in rendered, f"{entry.code} ships a {word}"

    def test_the_only_numeric_parameter_is_arithmetic_and_named_as_such(self) -> None:
        """ENC_REPEAT_POSITIVE_INPUT carries a 2. It is what 'more than one'
        means, not a clinical window.

        Named ``minimum_occurrences`` rather than ``threshold`` deliberately:
        the guard above is strict about that word, and a definition that used
        it for an arithmetic constant would teach the next person to relax the
        guard rather than rename the key.
        """
        entry = CATALOGUE_BY_CODE["ENC_REPEAT_POSITIVE_INPUT"]
        assert entry.numerator["minimum_occurrences"] == 2
        assert "threshold" not in entry.numerator
        assert entry.notes is not None
        assert "not a clinical threshold" in entry.notes
        assert "governed configuration" in entry.notes

    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.code)
    def test_no_entry_claims_confirmed_resistance(self, entry: CatalogueEntry) -> None:
        rendered = " ".join((entry.purpose, entry.interpretation, entry.notes or "")).lower()
        if "resistance" in rendered:
            assert "never" in rendered or "not" in rendered, entry.code
        for forbidden in ("confirms resistance", "proves treatment failure"):
            assert forbidden not in rendered, entry.code


class TestEveryDefinitionIsTraceable:
    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.code)
    def test_it_cites_a_source(self, entry: CatalogueEntry) -> None:
        """An indicator with no citation is an opinion."""
        assert entry.definition_source.strip()
        assert len(entry.definition_source) > 20

    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.code)
    def test_it_states_how_a_blank_is_treated(self, entry: CatalogueEntry) -> None:
        """An indicator whose blank rule is unstated is one two people will
        compute differently."""
        assert entry.blank_handling.strip()
        assert len(entry.blank_handling) > 40

    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.code)
    def test_it_says_how_to_read_it(self, entry: CatalogueEntry) -> None:
        assert entry.interpretation.strip()
        assert entry.purpose.strip()

    def test_codes_are_unique(self) -> None:
        codes = [entry.code for entry in CATALOGUE]
        assert len(codes) == len(set(codes))

    def test_every_entry_is_routine_surveillance(self) -> None:
        """Nothing MARS computes from routine data is a confirmed finding. A
        confirmed-evidence indicator would have to come from outside."""
        for entry in CATALOGUE:
            assert entry.evidence_lane is EvidenceLane.ROUTINE_SURVEILLANCE


class TestProportionsDeclareTheirDenominator:
    @pytest.mark.parametrize(
        "entry",
        [e for e in CATALOGUE if e.unit is IndicatorUnit.PROPORTION],
        ids=lambda e: e.code,
    )
    def test_a_proportion_has_a_denominator_specification(self, entry: CatalogueEntry) -> None:
        """A proportion without a denominator specification is not a proportion."""
        assert entry.denominator is not None, entry.code

    @pytest.mark.parametrize(
        "entry",
        [e for e in CATALOGUE if e.unit is IndicatorUnit.PROPORTION],
        ids=lambda e: e.code,
    )
    def test_it_says_an_undefined_denominator_yields_no_value(self, entry: CatalogueEntry) -> None:
        """Never zero. A positivity of 0.0 and a positivity that could not be
        computed look identical in a chart and are opposite statements."""
        rule = entry.blank_handling.lower()
        assert "unavailable" in rule or "no value" in rule, entry.code
        assert "never" in rule or "not" in rule, entry.code

    @pytest.mark.parametrize(
        "entry",
        [e for e in CATALOGUE if e.unit is IndicatorUnit.COUNT],
        ids=lambda e: e.code,
    )
    def test_a_count_has_no_denominator(self, entry: CatalogueEntry) -> None:
        assert entry.denominator is None, entry.code


class TestReportedAndDerivedStayApart:
    def test_the_same_quantity_from_two_sources_has_two_codes(self) -> None:
        """A figure summed from HMIS 105 and one computed from the e-register
        are two measurements of the same thing. One code for both would make
        them addable, and adding them double-counts."""
        derived = CATALOGUE_BY_CODE["ENC_CONFIRMED_MALARIA"]
        reported = CATALOGUE_BY_CODE["AGG105_CONFIRMED_MALARIA"]

        assert derived.source_domain is IndicatorSourceDomain.ENCOUNTER
        assert reported.source_domain is IndicatorSourceDomain.AGGREGATE_MONTHLY
        assert derived.code != reported.code

    def test_the_reported_one_says_it_is_what_the_facility_reported(self) -> None:
        reported = CATALOGUE_BY_CODE["AGG105_CONFIRMED_MALARIA"]
        assert "reported" in reported.interpretation.lower()
        assert "separate" in reported.interpretation.lower()

    def test_each_entry_names_exactly_one_source_domain(self) -> None:
        for entry in CATALOGUE:
            assert isinstance(entry.source_domain, IndicatorSourceDomain)

    def test_every_domain_that_has_entries_can_be_listed(self) -> None:
        covered = {entry.source_domain for entry in CATALOGUE}
        for domain in covered:
            assert entries_for_domain(domain)


class TestTheChecksumIdentifiesTheSpecification:
    def test_the_same_specification_gives_the_same_checksum(self) -> None:
        left = CATALOGUE_BY_CODE["ENC_TESTED_MALARIA"]
        right = CatalogueEntry(**{**{f: getattr(left, f) for f in left.__slots__}})
        assert left.checksum == right.checksum

    def test_a_changed_specification_gives_a_different_checksum(self) -> None:
        """It is what lets the seeder tell a re-registration from a revision."""
        import dataclasses

        left = CATALOGUE_BY_CODE["ENC_TESTED_MALARIA"]
        right = dataclasses.replace(left, numerator={"source": "encounter", "filter": {}})
        assert left.checksum != right.checksum

    def test_a_relabelling_does_not_change_the_checksum(self) -> None:
        """The checksum identifies what is computed, not how it is described.
        A clearer label should not look like a methodological change."""
        import dataclasses

        left = CATALOGUE_BY_CODE["ENC_TESTED_MALARIA"]
        right = dataclasses.replace(left, label="Malaria tests carried out")
        assert left.checksum == right.checksum


class TestTheCatalogueCoversWhatThePromptAskedFor:
    @pytest.mark.parametrize(
        "code",
        [
            "ENC_SUSPECTED_MALARIA",
            "ENC_TESTED_MALARIA",
            "ENC_CONFIRMED_MALARIA",
            "ENC_TEST_POSITIVITY",
            "ENC_ANTIMALARIAL_TREATED",
            "ENC_REPEAT_POSITIVE_INPUT",
            "RPT_COMPLETENESS",
            "COM_RDT_DAYS_OUT_OF_STOCK",
            "COM_AL_DAYS_OUT_OF_STOCK",
        ],
    )
    def test_the_required_definition_is_present(self, code: str) -> None:
        assert code in CATALOGUE_BY_CODE

    def test_positivity_is_read_against_tests_not_attendance(self) -> None:
        """A denominator inflated by untested attendances understates
        positivity everywhere, and worst where testing has broken down."""
        positivity = CATALOGUE_BY_CODE["ENC_TEST_POSITIVITY"]
        assert positivity.denominator == {"indicator": "ENC_TESTED_MALARIA"}
        assert positivity.numerator == {"indicator": "ENC_CONFIRMED_MALARIA"}

    def test_completeness_refuses_to_assume_the_reporters_were_everyone(self) -> None:
        """That assumption always yields 100%."""
        entry = CATALOGUE_BY_CODE["RPT_COMPLETENESS"]
        assert "unavailable" in entry.blank_handling.lower()
        assert "100%" in entry.blank_handling
