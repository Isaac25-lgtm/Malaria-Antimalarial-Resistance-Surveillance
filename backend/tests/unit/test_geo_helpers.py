"""FScode interpretation and name normalisation.

The expected values here come from the structure observed in
``UGANDA_SUBCOUNTIES.json`` during reconnaissance. They are hand-checked rather
than copied from a run, so a change in the interpretation fails the test rather
than silently updating the expectation.
"""

from __future__ import annotations

import pytest

from mars.domain.enums import GeographyLevel, GeographyUnitKind
from mars.geo.fscode import (
    COUNTRY_CODE,
    InvalidFsCodeError,
    is_consistent_with_region,
    level_of,
    normalise_fscode,
    parse_fscode,
)
from mars.geo.naming import (
    extract_alias_names,
    infer_unit_kind,
    name_defects,
    normalise_name,
)


class TestFsCodeParsing:
    @pytest.mark.parametrize(
        ("code", "region", "district", "county", "subcounty"),
        [
            # ALEBTONG / AJURI COUNTY / ABAKO, from the supplied subcounty layer.
            ("323101", "3", "323", "3231", "323101"),
            # ZOMBO / OKORO COUNTY / ABANGA.
            ("330101", "3", "330", "3301", "330101"),
            # AMURIA / AMURIA COUNTY / ABARILELA.
            ("216101", "2", "216", "2161", "216101"),
            # KAMPALA is region 1, district 102.
            ("102501", "1", "102", "1025", "102501"),
        ],
    )
    def test_splits_into_four_levels(
        self, code: str, region: str, district: str, county: str, subcounty: str
    ) -> None:
        parts = parse_fscode(code)
        assert parts.region == region
        assert parts.district == district
        assert parts.county == county
        assert parts.subcounty == subcounty

    def test_code_for_level(self) -> None:
        parts = parse_fscode("323101")
        assert parts.code_for(GeographyLevel.REGION) == "3"
        assert parts.code_for(GeographyLevel.DISTRICT) == "323"
        assert parts.code_for(GeographyLevel.COUNTY) == "3231"
        assert parts.code_for(GeographyLevel.SUBCOUNTY) == "323101"

    def test_parent_code_for_level(self) -> None:
        parts = parse_fscode("323101")
        assert parts.parent_code_for(GeographyLevel.REGION) == COUNTRY_CODE
        assert parts.parent_code_for(GeographyLevel.DISTRICT) == "3"
        assert parts.parent_code_for(GeographyLevel.COUNTY) == "323"
        assert parts.parent_code_for(GeographyLevel.SUBCOUNTY) == "3231"

    def test_parish_and_village_are_not_derivable(self) -> None:
        """No parish or village segment exists in an FScode."""
        parts = parse_fscode("323101")
        with pytest.raises(InvalidFsCodeError):
            parts.code_for(GeographyLevel.PARISH)
        with pytest.raises(InvalidFsCodeError):
            parts.code_for(GeographyLevel.VILLAGE)


class TestFsCodeNormalisation:
    def test_pads_a_stripped_leading_zero(self) -> None:
        """A spreadsheet round trip drops a leading zero.

        Without padding, ``23101`` would be read as region 2 rather than
        region 0 - a silently wrong region assignment.
        """
        assert normalise_fscode("23101") == "023101"
        assert normalise_fscode(23101) == "023101"

    def test_accepts_an_integer(self) -> None:
        assert normalise_fscode(323101) == "323101"

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(InvalidFsCodeError, match="numeric"):
            normalise_fscode("32A101")

    def test_rejects_empty(self) -> None:
        with pytest.raises(InvalidFsCodeError, match="empty"):
            normalise_fscode("   ")

    def test_rejects_over_length(self) -> None:
        with pytest.raises(InvalidFsCodeError, match="six digits"):
            normalise_fscode("1234567")


class TestLevelOf:
    @pytest.mark.parametrize(
        ("code", "level"),
        [
            ("3", GeographyLevel.REGION),
            ("323", GeographyLevel.DISTRICT),
            ("3231", GeographyLevel.COUNTY),
            ("323101", GeographyLevel.SUBCOUNTY),
        ],
    )
    def test_infers_level_from_prefix_length(self, code: str, level: GeographyLevel) -> None:
        assert level_of(code) == level

    def test_rejects_an_unrecognised_length(self) -> None:
        with pytest.raises(InvalidFsCodeError):
            level_of("32310")


class TestRegionConsistency:
    def test_matching_region(self) -> None:
        assert is_consistent_with_region("323101", "3")

    def test_mismatched_region(self) -> None:
        assert not is_consistent_with_region("323101", "2")


class TestNameNormalisation:
    def test_collapses_repeated_whitespace(self) -> None:
        """The supplied data contains double and triple spaces."""
        assert normalise_name("LUBYA  TOWN COUNCIL") == "LUBYA TOWN COUNCIL"
        assert normalise_name("NTWETWE  TOWN  COUNCIL") == "NTWETWE TOWN COUNCIL"
        assert normalise_name("KAMULI  MUNICIPALITY") == "KAMULI MUNICIPALITY"

    def test_removes_parenthetical_alias(self) -> None:
        assert normalise_name("ANAKA (PAYIRA)") == "ANAKA"
        assert normalise_name("GREEK RIVER (KIRIKI)") == "GREEK RIVER"
        assert normalise_name("SIDOK (KOPOTH)") == "SIDOK"

    def test_uppercases_and_trims(self) -> None:
        assert normalise_name("  gulu city  ") == "GULU CITY"

    def test_preserves_apostrophes_and_hyphens(self) -> None:
        """These occur inside genuine Ugandan place names."""
        assert normalise_name("MADI-OKOLLO") == "MADI-OKOLLO"

    def test_rejects_none(self) -> None:
        with pytest.raises(ValueError, match="required"):
            normalise_name(None)  # type: ignore[arg-type]


class TestAliasExtraction:
    def test_extracts_parenthetical_names(self) -> None:
        assert extract_alias_names("ANAKA (PAYIRA)") == ["PAYIRA"]
        assert extract_alias_names("SIDOK (KOPOTH)") == ["KOPOTH"]

    def test_returns_empty_when_no_alias(self) -> None:
        assert extract_alias_names("GULU") == []


class TestUnitKindInference:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("LUBYA  TOWN COUNCIL", GeographyUnitKind.TOWN_COUNCIL),
            ("NAKAWA DIVISION", GeographyUnitKind.URBAN_DIVISION),
            ("KAMULI  MUNICIPALITY", GeographyUnitKind.MUNICIPALITY),
            ("ABAKO", GeographyUnitKind.RURAL_SUBCOUNTY),
        ],
    )
    def test_infers_from_suffix(self, name: str, kind: GeographyUnitKind) -> None:
        assert infer_unit_kind(name) == kind


class TestNameDefectReporting:
    def test_reports_repeated_whitespace(self) -> None:
        assert "repeated_whitespace" in name_defects("LUBYA  TOWN COUNCIL")

    def test_reports_parenthetical_alias(self) -> None:
        assert "parenthetical_alias" in name_defects("ANAKA (PAYIRA)")

    def test_reports_nothing_for_a_clean_name(self) -> None:
        assert name_defects("GULU") == []
