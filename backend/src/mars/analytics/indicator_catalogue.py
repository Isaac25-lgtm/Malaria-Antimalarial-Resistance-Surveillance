"""The indicator catalogue: definitions MARS ships, grounded in the forms.

Every definition here traces to a printed field on a supplied form or to a
column of the canonical encounter model. Nothing is invented, and the
``definition_source`` on each entry says exactly where it came from so a
reviewer can check it against the paper.

**No entry carries a threshold, a target or an alert level.** A definition says
how to compute a figure; what counts as too high is a programme decision that
lives in the configuration registry and is absent until approved. An indicator
that shipped with a threshold would make every consumer inherit a judgement
nobody signed.

**Every entry ships as a draft.** Registering a definition and putting it in
force are different acts. A deployment that has not had these reviewed gets a
complete, readable registry that computes nothing - which is the correct
behaviour for a system whose figures a district will act on.

The seeder is idempotent: re-running it registers what is missing and leaves
alone anything already present, including anything a programme has since
approved. It never demotes an approved version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mars.domain.enums import (
    EvidenceLane,
    GeographyGrain,
    IndicatorSourceDomain,
    IndicatorUnit,
    PeriodGrain,
)

#: Bumped when a specification below changes in a way that alters a figure.
CATALOGUE_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """One shipped indicator definition and its first version."""

    code: str
    label: str
    purpose: str
    interpretation: str
    unit: IndicatorUnit
    source_domain: IndicatorSourceDomain
    period_grain: PeriodGrain
    base_geography_grain: GeographyGrain
    definition_source: str
    numerator: dict[str, Any]
    blank_handling: str
    denominator: dict[str, Any] | None = None
    exclusions: dict[str, Any] | None = None
    permitted_dimensions: dict[str, Any] | None = None
    evidence_lane: EvidenceLane = EvidenceLane.ROUTINE_SURVEILLANCE
    notes: str | None = None
    reason_for_change: str = "Initial definition shipped with the indicator registry."

    @property
    def specification(self) -> dict[str, Any]:
        """The executable half, as the aggregation engine reads it."""
        return {
            "catalogue_version": CATALOGUE_VERSION,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "exclusions": self.exclusions,
            "permitted_dimensions": self.permitted_dimensions,
            "blank_handling": self.blank_handling,
        }

    @property
    def checksum(self) -> str:
        return hashlib.sha256(
            json.dumps(self.specification, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


# ---------------------------------------------------------------------------
# Shared wording
# ---------------------------------------------------------------------------
#: The blank rule for anything summed from aggregate returns.
#:
#: Stated identically everywhere it applies, because the alternative is two
#: indicators that disagree about a facility that did not report - and a
#: national total that changes depending on which one a report happened to use.
_BLANK_IS_NOT_ZERO = (
    "A blank source cell is missing, not zero, and does not contribute to the "
    "sum. The count of blank inputs is carried on the result so a total from "
    "four reporting facilities is distinguishable from one from forty. A "
    "reported zero is a statement the facility made and does contribute."
)

_ENCOUNTER_BLANK = (
    "An encounter with no recorded test contributes to attendance and not to "
    "any tested or confirmed count. 'Not done' is recorded as not done and is "
    "never read as a negative result."
)

_NO_DENOMINATOR = (
    "Where the denominator is zero, blank or not reported, no value is "
    "produced and the result is marked unavailable. It is never reported as "
    "zero: a positivity of 0.0 and a positivity that could not be computed "
    "look identical in a chart and are opposite statements about a facility."
)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
CATALOGUE: tuple[CatalogueEntry, ...] = (
    # -- Encounter-derived ------------------------------------------------
    CatalogueEntry(
        code="ENC_ATTENDANCE_TOTAL",
        label="Outpatient attendances",
        purpose="The denominator most other facility figures are read against.",
        interpretation=(
            "Every canonical outpatient encounter in the period. Counts visits, "
            "not people: one patient attending three times is three attendances, "
            "which is what the register records and what a workload figure means."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS OPD 002 outpatient register; canonical opd_encounter rows.",
        numerator={"source": "encounter", "filter": {}, "count": "encounters"},
        blank_handling=_ENCOUNTER_BLANK,
        permitted_dimensions={"age_band": True, "sex": True},
    ),
    CatalogueEntry(
        code="ENC_SUSPECTED_MALARIA",
        label="Suspected malaria (fever)",
        purpose="Who was assessed as possibly having malaria.",
        interpretation=(
            "Encounters with fever recorded as present. This is a clinical "
            "suspicion, not a case: it says who should have been tested, which "
            "is why testing coverage is read against it."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source=(
            "HMIS OPD 002 fever column; HMIS 105 EP01a 'Suspected Malaria (fever)'."
        ),
        numerator={
            "source": "encounter",
            "filter": {"fever_present": ["yes"]},
            "count": "encounters",
        },
        blank_handling=(
            "Fever recorded as 'unknown' is not counted as suspected. Counting "
            "it would inflate the denominator of testing coverage with "
            "encounters nobody assessed."
        ),
        permitted_dimensions={"age_band": True, "sex": True},
    ),
    CatalogueEntry(
        code="ENC_TESTED_MALARIA",
        label="Malaria tests performed",
        purpose="The tested denominator. Positivity is read against this, never attendance.",
        interpretation=(
            "Encounters where a malaria test was actually performed, by either "
            "RDT or microscopy. Encounters recording 'not done' are excluded: a "
            "denominator inflated by untested attendances understates positivity "
            "everywhere, and does so most where testing is worst."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS OPD 002 test columns; HMIS 105 EP01b 'Malaria Tested (B/s & RDT)'.",
        numerator={
            "source": "encounter",
            "filter": {"test_method_not": ["not_done"]},
            "count": "encounters",
        },
        blank_handling=_ENCOUNTER_BLANK,
        permitted_dimensions={"age_band": True, "sex": True, "test_method": True},
    ),
    CatalogueEntry(
        code="ENC_CONFIRMED_MALARIA",
        label="Confirmed malaria cases",
        purpose="Cases with a positive parasitological result.",
        interpretation=(
            "Encounters with a positive RDT or microscopy result. 'Confirmed' "
            "means a read test: a clinical malaria diagnosis with no test is not "
            "counted here, and that difference is itself worth watching."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS OPD 002 result columns; HMIS 105 EP01c; HMIS 033b MA.",
        numerator={
            "source": "encounter",
            "filter": {"test_result": ["positive"]},
            "count": "encounters",
        },
        blank_handling=_ENCOUNTER_BLANK,
        permitted_dimensions={"age_band": True, "sex": True, "test_method": True},
    ),
    CatalogueEntry(
        code="ENC_TEST_POSITIVITY",
        label="Test positivity",
        purpose="The share of malaria tests that were positive.",
        interpretation=(
            "Confirmed cases divided by tests performed. Reads as a property of "
            "the tested population, not of the district: a positivity rise with "
            "falling test volume can mean testing narrowed to the sickest "
            "patients rather than that transmission rose. Always read beside "
            "the test count."
        ),
        unit=IndicatorUnit.PROPORTION,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="Derived from ENC_CONFIRMED_MALARIA and ENC_TESTED_MALARIA.",
        numerator={"indicator": "ENC_CONFIRMED_MALARIA"},
        denominator={"indicator": "ENC_TESTED_MALARIA"},
        blank_handling=_NO_DENOMINATOR,
        permitted_dimensions={"age_band": True, "sex": True, "test_method": True},
        notes=(
            "Stored as a fraction between 0 and 1. Presentation multiplies; the "
            "stored value is never pre-multiplied."
        ),
    ),
    CatalogueEntry(
        code="ENC_ANTIMALARIAL_TREATED",
        label="Encounters with an antimalarial recorded",
        purpose="Treatment volume, and the input to treatment-consistency work.",
        interpretation=(
            "Encounters carrying at least one antimalarial prescription line. "
            "Counts what the register records as prescribed, which is not the "
            "same as what a patient received or took - routine data cannot "
            "establish adherence or drug exposure."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS OPD 002 treatment columns; canonical prescription rows.",
        numerator={
            "source": "encounter",
            "filter": {"has_antimalarial": True},
            "count": "encounters",
        },
        blank_handling=(
            "An encounter with no prescription recorded is counted as not "
            "treated *here* and separately reported as missing treatment "
            "information. The two are different facts and a single count would "
            "merge them."
        ),
        permitted_dimensions={"age_band": True, "sex": True},
    ),
    CatalogueEntry(
        code="ENC_REPEAT_POSITIVE_INPUT",
        label="Linked patients with more than one positive result",
        purpose="The input to recurrence surveillance. Not a recurrence measure itself.",
        interpretation=(
            "Pseudonymously linked patients with two or more positive results in "
            "the period. A counting input, deliberately not an interval measure: "
            "what interval constitutes recurrence is a governed clinical "
            "parameter that this registry does not supply. Repeat positivity in "
            "routine data is a reason to investigate, never evidence of "
            "treatment failure, recrudescence or resistance."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.ENCOUNTER,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source=(
            "Derived from linked opd_encounter rows and their malaria results. "
            "Linkage is pseudonymous; no direct identifier is read."
        ),
        numerator={
            "source": "encounter",
            "filter": {"test_result": ["positive"], "linked": True},
            "count": "patients_with_at_least",
            # Named for what it is. "threshold" would read as a tunable
            # parameter, and this is simply what "more than one" means.
            "minimum_occurrences": 2,
        },
        blank_handling=(
            "Unlinked encounters cannot contribute and are counted separately as "
            "unlinked. Their absence is a limit on this figure, not a zero."
        ),
        notes=(
            "The '2 or more' here is arithmetic - what 'more than one' means - "
            "not a clinical threshold. Recurrence windows and interval bands are "
            "governed configuration and are absent until approved."
        ),
    ),
    # -- Aggregate-derived -------------------------------------------------
    CatalogueEntry(
        code="AGG105_CONFIRMED_MALARIA",
        label="Confirmed malaria cases (HMIS 105, as reported)",
        purpose="What facilities officially reported, kept apart from what MARS derives.",
        interpretation=(
            "The facility's own reported figure from HMIS 105 EP01c, summed over "
            "its age and sex disaggregation. Deliberately separate from "
            "ENC_CONFIRMED_MALARIA: the two are different measurements of the "
            "same thing, and where they disagree the difference is the finding."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.AGGREGATE_MONTHLY,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS 105 section 1.3.1, EP01c 'Malaria confirmed (B/s & RDT)'.",
        numerator={"source": "aggregate", "element": "EP01c", "form": "hmis_105"},
        blank_handling=_BLANK_IS_NOT_ZERO,
        permitted_dimensions={"age_band": True, "sex": True},
    ),
    CatalogueEntry(
        code="AGG105_TESTED_MALARIA",
        label="Malaria tests performed (HMIS 105, as reported)",
        purpose="The reported tested denominator.",
        interpretation="The facility's own reported figure from HMIS 105 EP01b.",
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.AGGREGATE_MONTHLY,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS 105 section 1.3.1, EP01b 'Malaria Tested (B/s & RDT)'.",
        numerator={"source": "aggregate", "element": "EP01b", "form": "hmis_105"},
        blank_handling=_BLANK_IS_NOT_ZERO,
        permitted_dimensions={"age_band": True, "sex": True},
    ),
    CatalogueEntry(
        code="AGG105_PRESUMPTIVE_TREATED",
        label="Malaria treated without a confirmed result (HMIS 105)",
        purpose="Treatment given without parasitological confirmation.",
        interpretation=(
            "EP01e total treated minus EP01d confirmed-and-treated. The form "
            "collects both, so this difference is reported rather than inferred. "
            "It is a statement about testing and prescribing practice, never "
            "about the parasite."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.AGGREGATE_MONTHLY,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source=(
            "HMIS 105 EP01e 'Total malaria cases treated' minus EP01d "
            "'Confirmed Malaria cases treated'."
        ),
        numerator={
            "source": "aggregate",
            "form": "hmis_105",
            "difference": {"minuend": "EP01e", "subtrahend": "EP01d"},
        },
        blank_handling=(
            "If either element is blank the difference is not computed and the "
            "result is unavailable. Treating a blank EP01d as zero would report "
            "the whole treated total as presumptive, which is a serious and "
            "invisible overstatement."
        ),
        exclusions={
            "negative_difference": (
                "A negative difference means EP01d exceeds EP01e, which is "
                "arithmetically impossible. The period is excluded and flagged "
                "as a transcription problem rather than clamped to zero."
            )
        },
        permitted_dimensions={"age_band": True, "sex": True},
    ),
    CatalogueEntry(
        code="AGG033B_NEGATIVE_TREATED",
        label="Negative-tested cases treated (HMIS 033b)",
        purpose="Treatment given after a negative test, as the facility reported it.",
        interpretation=(
            "The sum of the form's own RDT-negative-treated and "
            "microscopy-negative-treated columns. The form collects these "
            "explicitly, so MARS does not have to infer them. A prescribing and "
            "testing-confidence measure; it says nothing about the parasite."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.AGGREGATE_WEEKLY,
        period_grain=PeriodGrain.EPIDEMIOLOGICAL_WEEK,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source=(
            "HMIS 033b section 5, 'RDT Negative Cases Treated' and "
            "'Microscopy Negative Cases Treated'."
        ),
        numerator={
            "source": "aggregate",
            "form": "hmis_033b",
            "sum": ["M033B_MAT_RDT_NEGATIVE_TREATED", "M033B_MAT_MICROSCOPY_NEGATIVE_TREATED"],
        },
        blank_handling=(
            "Each column contributes only when reported. If both are blank the "
            "result is unavailable rather than zero: a facility that did not "
            "answer has not reported none."
        ),
    ),
    # -- Laboratory --------------------------------------------------------
    CatalogueEntry(
        code="LAB105_MALARIA_TESTS_DONE",
        label="Malaria laboratory tests performed (HMIS 105 section 10)",
        purpose="The laboratory's own count, independent of the OPD diagnosis block.",
        interpretation=(
            "PS01 microscopy plus PS02 RDT, as the laboratory reported them. "
            "Kept apart from the OPD figures on purpose: the laboratory counts "
            "tests it performed and the OPD block counts patients it diagnosed. "
            "Where the two disagree, the disagreement is the finding."
        ),
        unit=IndicatorUnit.COUNT,
        source_domain=IndicatorSourceDomain.LABORATORY,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source="HMIS 105 section 10.2.1 PARASITOLOGY (Blood), PS01 and PS02.",
        numerator={"source": "laboratory", "sum_done": ["PS01", "PS02"]},
        blank_handling=_BLANK_IS_NOT_ZERO,
    ),
    # -- Commodity ---------------------------------------------------------
    CatalogueEntry(
        code="COM_RDT_DAYS_OUT_OF_STOCK",
        label="Days out of stock: malaria RDTs",
        purpose="Commodity context for any change in testing volume.",
        interpretation=(
            "Days the facility reported having no malaria RDTs in its store, as "
            "the form defines out of stock. Context, not a signal on its own: a "
            "testing decline that coincides with days out of stock has a "
            "commodity explanation rather than an epidemiological one."
        ),
        unit=IndicatorUnit.DAYS,
        source_domain=IndicatorSourceDomain.COMMODITY,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source=(
            "HMIS 105 section 6.1, SS34 'Malaria Rapid Diagnostic', days out of "
            "stock column. Out of stock is defined on the form as none left in "
            "the health unit store."
        ),
        numerator={
            "source": "commodity",
            "commodity": "SS34",
            "metric": "days_out_of_stock",
        },
        blank_handling=(
            "A blank is not zero days. A facility that did not report its stock "
            "position is not a facility that was never out of stock, and the "
            "difference matters most exactly when supply has broken down."
        ),
    ),
    CatalogueEntry(
        code="COM_AL_DAYS_OUT_OF_STOCK",
        label="Days out of stock: Artemether/Lumefantrine",
        purpose="Commodity context for any change in treatment volume.",
        interpretation=(
            "Days the facility reported having no AL in its store. Context for "
            "treatment figures: a fall in recorded antimalarial treatment during "
            "an AL stock-out is a supply finding, not a decline in malaria."
        ),
        unit=IndicatorUnit.DAYS,
        source_domain=IndicatorSourceDomain.COMMODITY,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.FACILITY,
        definition_source=(
            "HMIS 105 section 6.1, SS01 'Artemether/Lumefantrine 20/120mg', days "
            "out of stock column."
        ),
        numerator={
            "source": "commodity",
            "commodity": "SS01",
            "metric": "days_out_of_stock",
        },
        blank_handling=("A blank is not zero days, for the same reason as the RDT measure."),
    ),
    # -- Reporting metadata ------------------------------------------------
    CatalogueEntry(
        code="RPT_COMPLETENESS",
        label="Reporting completeness",
        purpose="How much of a district's expected reporting actually arrived.",
        interpretation=(
            "Facilities that submitted an accepted return for the period, over "
            "facilities expected to. The single most important figure for "
            "reading every other one: a district total that rises because more "
            "facilities began reporting is not an epidemiological rise, and "
            "without this figure the two are indistinguishable."
        ),
        unit=IndicatorUnit.PROPORTION,
        source_domain=IndicatorSourceDomain.REPORTING_METADATA,
        period_grain=PeriodGrain.MONTH,
        base_geography_grain=GeographyGrain.DISTRICT,
        definition_source=(
            "Accepted aggregate_submission rows against the active facility "
            "master for the district."
        ),
        numerator={"source": "reporting", "count": "facilities_with_accepted_submission"},
        denominator={"source": "reporting", "count": "active_facilities_expected"},
        blank_handling=(
            "A facility that submitted nothing is a non-reporter, which is the "
            "quantity being measured. Where the expected count is unknown the "
            "result is unavailable rather than assuming the reporting facilities "
            "were all of them - that assumption always yields 100%."
        ),
        notes=(
            "The expected set is the active facility master. Whether a facility "
            "*should* have reported in a given period is a programme question "
            "MARS does not answer on its own."
        ),
    ),
)

CATALOGUE_BY_CODE: dict[str, CatalogueEntry] = {entry.code: entry for entry in CATALOGUE}


def entries_for_domain(domain: IndicatorSourceDomain) -> tuple[CatalogueEntry, ...]:
    return tuple(entry for entry in CATALOGUE if entry.source_domain is domain)


__all__ = [
    "CATALOGUE",
    "CATALOGUE_BY_CODE",
    "CATALOGUE_VERSION",
    "CatalogueEntry",
    "entries_for_domain",
]
