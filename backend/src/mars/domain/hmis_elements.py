"""The HMIS data elements MARS ingests, transcribed from the printed forms.

Every code, label and disaggregation below is read from the supplied forms:

* ``HMIS 033b HEALTH UNIT WEEKLY EPIDEMIOLOGICAL-ka2024.pdf`` (Print Version
  July 2024)
* ``HMIS 105 Health Unit Outpatient Monthly Report-ka2024_IPS.pdf`` (Print
  Version July 2024)

**Nothing here is invented.** Where a form prints a code, that code is used
verbatim, including its trailing full stop on 033b (``MA.``) and its section
numbering on 105 (``EP01a``). Where a form prints no code for a field - the
malaria and stock summary blocks on 033b are unlabelled columns - MARS assigns
one under its own ``M033B_`` prefix and this file says so at that entry, so a
reader can tell a transcribed code from a MARS-assigned one at a glance.

**This is not the whole of either form.** HMIS 105 lists several hundred
diagnoses across nine sub-sections; MARS is a malaria surveillance system and
ingests the elements it uses, listed here. The storage model is keyed by
element code rather than by column, so adding an element is a registry entry
and not a migration - which is also why this file can be honest about its scope
instead of stubbing out hundreds of rows nobody reads.

Two rules govern every entry:

**A blank cell is not a zero.** 033b instruction 7 requires a health unit to
report every week "whether there are cases or not", so a reported zero is a
statement and a blank is a missing statement. They are stored differently and
must never be conflated: a facility reporting zero malaria deaths and a
facility that did not report are different facts about that facility.

**MARS does not re-band a reported figure.** An aggregate arrives already
summarised. Splitting ``29 days - 4 yrs`` into finer ages, or summing bands the
form keeps apart, would be inventing detail the facility never reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from mars.domain.enums import AgeBand, AggregateForm

#: The five age bands HMIS 105 prints, in the form's own order.
HMIS_105_AGE_BANDS: tuple[AgeBand, ...] = (
    AgeBand.DAYS_0_28,
    AgeBand.DAYS_29_TO_YEARS_4,
    AgeBand.YEARS_5_9,
    AgeBand.YEARS_10_19,
    AgeBand.YEARS_20_PLUS,
)

#: Prefix for codes MARS assigns where the form prints none.
MARS_ASSIGNED_PREFIX = "M033B_"


@dataclass(frozen=True, slots=True)
class HmisElement:
    """One cell, or one row of cells, on a printed form."""

    code: str
    label: str
    form: AggregateForm
    #: The form's own section heading, so a data-entry clerk and an engineer
    #: can find the same cell.
    section: str
    #: True when the form disaggregates this element by age band and sex.
    disaggregated: bool = False
    #: True when MARS assigned the code because the form prints none.
    code_assigned_by_mars: bool = False
    #: Why MARS ingests it. Absent for elements kept only for completeness of a
    #: block MARS otherwise uses.
    note: str = ""

    @property
    def age_bands(self) -> tuple[AgeBand, ...]:
        return HMIS_105_AGE_BANDS if self.disaggregated else (AgeBand.UNSPECIFIED,)


# ---------------------------------------------------------------------------
# HMIS 033b — weekly
# ---------------------------------------------------------------------------
# Section 1 "CASES AND DEATHS THIS WEEK" prints a code per condition. Row 1 is
# malaria; its "Tested Cases" and "Pos(+ve) cases" cells are blacked out on the
# printed form, because section 5 carries the malaria testing detail instead.
# MARS therefore ingests cases and deaths from section 1 and testing from
# section 5, exactly as the form divides them.
HMIS_033B_ELEMENTS: tuple[HmisElement, ...] = (
    HmisElement(
        code="MA.",
        label="Malaria (Confirmed) — total cases this week",
        form=AggregateForm.HMIS_033B,
        section="1. CASES",
        note=(
            "Confirmed only. The form's own qualifier, and the reason this "
            "figure is not the same quantity as HMIS 105 EP01e."
        ),
    ),
    HmisElement(
        code="MA.DEATHS",
        label="Malaria (Confirmed) — total deaths this week",
        form=AggregateForm.HMIS_033B,
        section="1. CASES / 2. DEATH",
        code_assigned_by_mars=True,
        note=(
            "The form prints one code per row and two value columns. MARS "
            "stores the death column under its own suffix rather than "
            "overloading MA. with two meanings."
        ),
    ),
    # Section 5 prints ten unlabelled columns under one MAT. block. The codes
    # below are MARS-assigned; the labels are the printed column headings
    # verbatim.
    HmisElement(
        code="M033B_MAT_SUSPECTED",
        label="Suspected malaria (fever)",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_TESTED_RDT",
        label="Cases tested with RDT",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_RDT_POSITIVE",
        label="RDT Positive Cases",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_TESTED_MICROSCOPY",
        label="Cases tested with Microscopy",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_MICROSCOPY_POSITIVE",
        label="Microscopy Positive Cases",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_NOT_TESTED_TREATED",
        label="Not tested cases treated",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
        note=(
            "Presumptive treatment, reported by the facility itself. A "
            "surveillance signal about testing practice, never about the "
            "parasite."
        ),
    ),
    HmisElement(
        code="M033B_MAT_RDT_NEGATIVE_TREATED",
        label="RDT Negative Cases Treated",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
        note=(
            "Treated after a negative test. The form collects it explicitly, "
            "so MARS does not have to infer it."
        ),
    ),
    HmisElement(
        code="M033B_MAT_RDT_POSITIVE_TREATED",
        label="RDT Positive Cases Treated",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_MICROSCOPY_NEGATIVE_TREATED",
        label="Microscopy Negative Cases Treated",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_MAT_MICROSCOPY_POSITIVE_TREATED",
        label="Microscopy Positive Cases Treated",
        form=AggregateForm.HMIS_033B,
        section="5. SUMMARY OF MALARIA CASES TESTED AND TREATED",
        code_assigned_by_mars=True,
    ),
    # Section 4, OPD and eMTCT summary. Only the attendance columns; the eMTCT
    # columns are outside what MARS holds.
    HmisElement(
        code="M033B_APT_OPD_NEW",
        label="OPD New Attendance",
        form=AggregateForm.HMIS_033B,
        section="4. OPD AND EMTCT SUMMARY",
        code_assigned_by_mars=True,
        note="The denominator a weekly malaria figure is read against.",
    ),
    HmisElement(
        code="M033B_APT_OPD_TOTAL",
        label="Total OPD Attendance",
        form=AggregateForm.HMIS_033B,
        section="4. OPD AND EMTCT SUMMARY",
        code_assigned_by_mars=True,
    ),
)

#: Section 7, tracer medicines - stock balance. The form prints eight columns
#: under one TRA. block; these are the malaria-relevant four. Labels are the
#: printed headings verbatim.
#:
#: The form collects a **balance**, not the four measures HMIS 105 collects, so
#: these are stored as stock observations with a single metric rather than
#: being made to look like a 105 stock row.
HMIS_033B_TRACER_ITEMS: tuple[HmisElement, ...] = (
    HmisElement(
        code="M033B_TRA_AL",
        label="Artemether/Lumefantrine 20/120 mg tablet",
        form=AggregateForm.HMIS_033B,
        section="7. TRACER MEDICINES - STOCK BALANCE",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_TRA_ARTESUNATE",
        label="Artesunate 60 mg vial",
        form=AggregateForm.HMIS_033B,
        section="7. TRACER MEDICINES - STOCK BALANCE",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_TRA_SP",
        label="Sulfadoxine/Pyrimethamine Tablet",
        form=AggregateForm.HMIS_033B,
        section="7. TRACER MEDICINES - STOCK BALANCE",
        code_assigned_by_mars=True,
    ),
    HmisElement(
        code="M033B_TRA_RDT",
        label="Malaria Rapid Diagnostic tests",
        form=AggregateForm.HMIS_033B,
        section="7. TRACER MEDICINES - STOCK BALANCE",
        code_assigned_by_mars=True,
        note=(
            "A weekly RDT balance. Paired with a testing decline, it is the "
            "difference between a commodity problem and an epidemiological one."
        ),
    ),
)


# ---------------------------------------------------------------------------
# HMIS 105 — monthly
# ---------------------------------------------------------------------------
#: Section 1.1 Outpatient attendance and 1.3.1 malaria. All disaggregated by
#: the form's five age bands and by sex.
HMIS_105_ELEMENTS: tuple[HmisElement, ...] = (
    HmisElement(
        code="OA01",
        label="New attendance",
        form=AggregateForm.HMIS_105,
        section="1.1 OUTPATIENT ATTENDANCE",
        disaggregated=True,
    ),
    HmisElement(
        code="OA02",
        label="Re-attendance",
        form=AggregateForm.HMIS_105,
        section="1.1 OUTPATIENT ATTENDANCE",
        disaggregated=True,
    ),
    HmisElement(
        code="EP01a",
        label="Suspected Malaria (fever)",
        form=AggregateForm.HMIS_105,
        section="1.3.1 Epidemic Prone Diseases — EP01 Malaria",
        disaggregated=True,
    ),
    HmisElement(
        code="EP01b",
        label="Malaria Tested (B/s & RDT)",
        form=AggregateForm.HMIS_105,
        section="1.3.1 Epidemic Prone Diseases — EP01 Malaria",
        disaggregated=True,
        note="The tested denominator. Positivity is read against this, never against attendance.",
    ),
    HmisElement(
        code="EP01c",
        label="Malaria confirmed (B/s & RDT)",
        form=AggregateForm.HMIS_105,
        section="1.3.1 Epidemic Prone Diseases — EP01 Malaria",
        disaggregated=True,
    ),
    HmisElement(
        code="EP01d",
        label="Confirmed Malaria cases treated",
        form=AggregateForm.HMIS_105,
        section="1.3.1 Epidemic Prone Diseases — EP01 Malaria",
        disaggregated=True,
    ),
    HmisElement(
        code="EP01e",
        label="Total malaria cases treated",
        form=AggregateForm.HMIS_105,
        section="1.3.1 Epidemic Prone Diseases — EP01 Malaria",
        disaggregated=True,
        note=(
            "EP01e minus EP01d is treatment without a confirmed result. The "
            "form collects both, so the difference is reported rather than "
            "inferred - and it is a statement about testing practice, never "
            "about the parasite."
        ),
    ),
)

#: Section 10.2.1, PARASITOLOGY (Blood). Two rows, each with Number Done and
#: Number Positive. Not disaggregated by age or sex on the form.
HMIS_105_LABORATORY_TESTS: tuple[HmisElement, ...] = (
    HmisElement(
        code="PS01",
        label="Malaria Microscopy",
        form=AggregateForm.HMIS_105,
        section="10.2.1 Routine Tests — PARASITOLOGY (Blood)",
    ),
    HmisElement(
        code="PS02",
        label="Malaria RDTs",
        form=AggregateForm.HMIS_105,
        section="10.2.1 Routine Tests — PARASITOLOGY (Blood)",
        note=(
            "The laboratory's own count, independent of the OPD diagnosis "
            "block. Where the two disagree, both are kept: a difference "
            "between the register and the laboratory is itself the finding."
        ),
    ),
)

#: Section 6.1 stock status. The malaria-relevant commodities, with the form's
#: own serial numbers and unit of issue. Every one carries all four metrics.
HMIS_105_COMMODITIES: tuple[HmisElement, ...] = (
    HmisElement(
        code="SS01",
        label="Artemether/Lumefantrine 20/120mg",
        form=AggregateForm.HMIS_105,
        section="6.1 STOCK STATUS",
        note="Unit of issue: Tablet",
    ),
    HmisElement(
        code="SS02",
        label="Artesunate 60mg",
        form=AggregateForm.HMIS_105,
        section="6.1 STOCK STATUS",
        note="Unit of issue: Vial",
    ),
    HmisElement(
        code="SS03",
        label="Long Lasting Insecticidal Nets (LLINs)",
        form=AggregateForm.HMIS_105,
        section="6.1 STOCK STATUS",
        note="Unit of issue: Piece",
    ),
    HmisElement(
        code="SS24",
        label="Sulfadoxine/ Pyrimethamine tablet 500/25mg",
        form=AggregateForm.HMIS_105,
        section="6.1 STOCK STATUS",
        note="Unit of issue: Tablet",
    ),
    HmisElement(
        code="SS34",
        label="Malaria Rapid Diagnostic",
        form=AggregateForm.HMIS_105,
        section="6.1 STOCK STATUS",
        note="Unit of issue: Tests",
    ),
)


ALL_ELEMENTS: tuple[HmisElement, ...] = (
    HMIS_033B_ELEMENTS
    + HMIS_033B_TRACER_ITEMS
    + HMIS_105_ELEMENTS
    + HMIS_105_LABORATORY_TESTS
    + HMIS_105_COMMODITIES
)

ELEMENTS_BY_CODE: dict[str, HmisElement] = {element.code: element for element in ALL_ELEMENTS}

#: Codes that carry a value per age band and sex.
DISAGGREGATED_CODES: frozenset[str] = frozenset(
    element.code for element in ALL_ELEMENTS if element.disaggregated
)

COMMODITY_CODES: frozenset[str] = frozenset(e.code for e in HMIS_105_COMMODITIES)
TRACER_CODES: frozenset[str] = frozenset(e.code for e in HMIS_033B_TRACER_ITEMS)
LABORATORY_CODES: frozenset[str] = frozenset(e.code for e in HMIS_105_LABORATORY_TESTS)


def elements_for(form: AggregateForm) -> tuple[HmisElement, ...]:
    """Every element MARS ingests from one form."""
    return tuple(element for element in ALL_ELEMENTS if element.form is form)


__all__ = [
    "ALL_ELEMENTS",
    "COMMODITY_CODES",
    "DISAGGREGATED_CODES",
    "ELEMENTS_BY_CODE",
    "HMIS_033B_ELEMENTS",
    "HMIS_033B_TRACER_ITEMS",
    "HMIS_105_AGE_BANDS",
    "HMIS_105_COMMODITIES",
    "HMIS_105_ELEMENTS",
    "HMIS_105_LABORATORY_TESTS",
    "LABORATORY_CODES",
    "MARS_ASSIGNED_PREFIX",
    "TRACER_CODES",
    "HmisElement",
    "elements_for",
]
