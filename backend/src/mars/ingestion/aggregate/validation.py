"""Validating an inbound HMIS submission into something the model can hold.

The rules that matter here are the ones a spreadsheet would not catch.

**An unknown element code is never guessed.** HMIS 105 prints several hundred
codes across nine sub-sections; a code MARS does not hold is refused with the
form and the accepted set named, so a producer can fix the mapping. Guessing
which cell ``EP01`` means when the form prints ``EP01a`` through ``EP01e`` is
how a malaria figure lands in the wrong row.

**A blank cell is not a zero.** A null value is stored as null. HMIS 033b
instruction 7 requires reporting every week whether there are cases or not, so
a facility that answered "none" and a facility that did not answer are
different facts, and merging them turns a reporting gap into an apparent
improvement.

**Arithmetic impossibilities are transcription errors.** More positives than
tests, a negative count, more days out of stock than the period has days. None
of these is an unusual month, and accepting them puts a figure nobody wrote
into every downstream rate.

**MARS does not re-band.** An aggregate arrives already summarised; a
disaggregated element must carry one of the form's own bands and a
non-disaggregated one must not carry a band at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from mars.domain.enums import (
    AgeBand,
    AggregateForm,
    AggregatePeriodType,
    Sex,
    StockMetric,
    ValidationSeverity,
)
from mars.domain.hmis_elements import (
    COMMODITY_CODES,
    ELEMENTS_BY_CODE,
    HMIS_105_AGE_BANDS,
    LABORATORY_CODES,
    TRACER_CODES,
    elements_for,
)
from mars.ingestion.aggregate.contract import InboundSubmission, parse_form

#: Which period each form covers, from the forms themselves: 033b is "Every
#: Monday of the Week in a calendar year"; 105 is due "7th day of the following
#: month".
FORM_PERIODS: dict[AggregateForm, AggregatePeriodType] = {
    AggregateForm.HMIS_033B: AggregatePeriodType.WEEK,
    AggregateForm.HMIS_105: AggregatePeriodType.MONTH,
}

_AGE_BANDS = {band.value: band for band in AgeBand}
_SEXES = {value.value: value for value in Sex}
_SEXES.update({"m": Sex.MALE, "male": Sex.MALE, "f": Sex.FEMALE, "female": Sex.FEMALE})
_METRICS = {metric.value: metric for metric in StockMetric}
_STOCK_METRICS: dict[AggregateForm, frozenset[StockMetric]] = {
    # 033b section 7 prints one Balance column. Days out of stock and the
    # consumption/expiry columns belong to monthly HMIS 105 section 6.1.
    AggregateForm.HMIS_033B: frozenset({StockMetric.STOCK_ON_HAND}),
    AggregateForm.HMIS_105: frozenset(StockMetric),
}

#: Codes that are stock rather than cell observations, per form.
_STOCK_CODES: dict[AggregateForm, frozenset[str]] = {
    AggregateForm.HMIS_033B: TRACER_CODES,
    AggregateForm.HMIS_105: COMMODITY_CODES,
}


@dataclass(frozen=True, slots=True)
class Issue:
    """One finding about one submission."""

    code: str
    severity: ValidationSeverity
    message: str
    field_path: str | None = None
    context: dict[str, Any] | None = None

    @property
    def blocks_submission(self) -> bool:
        return self.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}


@dataclass(slots=True)
class ValidatedObservation:
    element_code: str
    age_band: AgeBand
    sex: Sex
    value: int | None
    raw_value: str | None = None


@dataclass(slots=True)
class ValidatedStock:
    commodity_code: str
    metric: StockMetric
    value: Decimal | None
    unit_of_issue: str | None = None
    raw_value: str | None = None


@dataclass(slots=True)
class ValidatedLaboratory:
    test_code: str
    number_done: int | None
    number_positive: int | None
    raw_done: str | None = None
    raw_positive: str | None = None


@dataclass(slots=True)
class ValidatedSubmission:
    """A submission that can be written, with whatever warnings it carried."""

    form: AggregateForm
    period_type: AggregatePeriodType
    facility_code: str
    period_start: date
    period_end: date
    revision: int
    period_label: str | None = None
    reported_on: date | None = None
    source_reference: str | None = None
    remarks: str | None = None
    observations: list[ValidatedObservation] = field(default_factory=list)
    stock: list[ValidatedStock] = field(default_factory=list)
    laboratory: list[ValidatedLaboratory] = field(default_factory=list)


@dataclass(slots=True)
class SubmissionValidation:
    inbound: InboundSubmission
    submission: ValidatedSubmission | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def is_loadable(self) -> bool:
        return self.submission is not None and not any(i.blocks_submission for i in self.issues)


class AggregateValidator:
    """Turns one inbound submission into something writable, or explains why not."""

    def validate(self, inbound: InboundSubmission) -> SubmissionValidation:
        issues: list[Issue] = []

        form = parse_form(inbound.form)
        if form is None:
            issues.append(
                Issue(
                    code="unknown_form",
                    severity=ValidationSeverity.ERROR,
                    message="MARS does not hold this form; the submission cannot be placed",
                    field_path="form",
                    context={"accepted": sorted(f.value for f in AggregateForm)},
                )
            )
            return SubmissionValidation(inbound=inbound, submission=None, issues=issues)

        period_type = FORM_PERIODS[form]
        if not self._period_is_well_formed(inbound, form, period_type, issues):
            return SubmissionValidation(inbound=inbound, submission=None, issues=issues)

        if inbound.revision < 1:
            issues.append(
                Issue(
                    code="revision_not_positive",
                    severity=ValidationSeverity.ERROR,
                    message="revision starts at 1; a correction increments it",
                    field_path="revision",
                )
            )

        for field_path, value, maximum in (
            ("period_label", inbound.period_label, 32),
            ("source_reference", inbound.source_reference, 128),
        ):
            if value is not None and len(value) > maximum:
                issues.append(
                    Issue(
                        code="text_exceeds_contract_limit",
                        severity=ValidationSeverity.ERROR,
                        message=f"{field_path} exceeds its {maximum}-character contract limit",
                        field_path=field_path,
                        context={"maximum": maximum},
                    )
                )

        submission = ValidatedSubmission(
            form=form,
            period_type=period_type,
            facility_code=inbound.facility_code,
            period_start=inbound.period_start,
            period_end=inbound.period_end,
            revision=inbound.revision,
            period_label=inbound.period_label,
            reported_on=inbound.reported_on,
            source_reference=inbound.source_reference,
            remarks=inbound.remarks,
        )

        submission.observations = self._observations(inbound, form, issues)
        submission.stock = self._stock(inbound, form, issues)
        submission.laboratory = self._laboratory(inbound, form, issues)

        if not (submission.observations or submission.stock or submission.laboratory):
            # An empty submission is not a zero report: a zero report has cells
            # containing zero. An empty one carries no statement at all.
            issues.append(
                Issue(
                    code="submission_is_empty",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        "the submission carries no cells; a zero report has cells "
                        "containing zero, not no cells"
                    ),
                )
            )

        return SubmissionValidation(inbound=inbound, submission=submission, issues=issues)

    # -- Period ------------------------------------------------------------
    def _period_is_well_formed(
        self,
        inbound: InboundSubmission,
        form: AggregateForm,
        period_type: AggregatePeriodType,
        issues: list[Issue],
    ) -> bool:
        """The period must be the shape the form prints.

        Checked because a "weekly" submission covering a quarter would later be
        summed with real weeks, and the resulting figure would look ordinary.
        """
        if inbound.period_end < inbound.period_start:
            issues.append(
                Issue(
                    code="period_reversed",
                    severity=ValidationSeverity.ERROR,
                    message="period_end is before period_start",
                    field_path="period_end",
                )
            )
            return False

        span = (inbound.period_end - inbound.period_start).days
        if period_type is AggregatePeriodType.WEEK:
            if span != 6:
                issues.append(
                    Issue(
                        code="period_is_not_a_week",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "HMIS 033b covers one week, Monday to Sunday; this "
                            "period is a different length"
                        ),
                        field_path="period_end",
                        context={"days_spanned": span + 1},
                    )
                )
                return False
            if inbound.period_start.weekday() != 0:
                # The form says the period is Monday to Sunday. A week starting
                # on another day silently overlaps its neighbours.
                issues.append(
                    Issue(
                        code="week_does_not_start_on_monday",
                        severity=ValidationSeverity.ERROR,
                        message="HMIS 033b weeks run Monday to Sunday",
                        field_path="period_start",
                    )
                )
                return False
            return True

        first = inbound.period_start
        if first.day != 1:
            issues.append(
                Issue(
                    code="month_does_not_start_on_the_first",
                    severity=ValidationSeverity.ERROR,
                    message="HMIS 105 covers a calendar month",
                    field_path="period_start",
                )
            )
            return False
        if inbound.period_end != _month_end(first):
            issues.append(
                Issue(
                    code="month_does_not_end_on_the_last_day",
                    severity=ValidationSeverity.ERROR,
                    message="HMIS 105 covers a whole calendar month",
                    field_path="period_end",
                    context={"expected": _month_end(first).isoformat()},
                )
            )
            return False
        return True

    # -- Cells -------------------------------------------------------------
    def _observations(
        self, inbound: InboundSubmission, form: AggregateForm, issues: list[Issue]
    ) -> list[ValidatedObservation]:
        known = {element.code for element in elements_for(form)}
        stock_codes = _STOCK_CODES[form]
        seen: set[tuple[str, AgeBand, Sex]] = set()
        validated: list[ValidatedObservation] = []

        for index, entry in enumerate(inbound.observations):
            path = f"observations[{index}]"
            if entry.element in stock_codes or entry.element in LABORATORY_CODES:
                issues.append(
                    Issue(
                        code="element_in_the_wrong_block",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "this code is a stock or laboratory row and belongs in "
                            "its own block, where its own columns apply"
                        ),
                        field_path=f"{path}.element",
                        context={"element": entry.element},
                    )
                )
                continue

            if entry.element not in known:
                issues.append(
                    Issue(
                        code="unknown_element",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "MARS does not hold this element for this form; guessing "
                            "which cell it means is how a figure lands in the wrong row"
                        ),
                        field_path=f"{path}.element",
                        context={"element": entry.element, "form": form.value},
                    )
                )
                continue

            element = ELEMENTS_BY_CODE[entry.element]
            band = _AGE_BANDS.get(entry.age_band)
            if band is None:
                issues.append(
                    Issue(
                        code="unknown_age_band",
                        severity=ValidationSeverity.ERROR,
                        message="age_band is not one of the form's own bands",
                        field_path=f"{path}.age_band",
                        context={"accepted": [b.value for b in HMIS_105_AGE_BANDS]},
                    )
                )
                continue

            if element.disaggregated and band is AgeBand.UNSPECIFIED:
                issues.append(
                    Issue(
                        code="disaggregation_missing",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "this element is reported per age band and sex on the "
                            "form; MARS does not re-band a total it was given"
                        ),
                        field_path=f"{path}.age_band",
                    )
                )
                continue

            if not element.disaggregated and band is not AgeBand.UNSPECIFIED:
                issues.append(
                    Issue(
                        code="unexpected_disaggregation",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "the form reports this element as a single total; a band "
                            "here means the producer split a figure MARS cannot verify"
                        ),
                        field_path=f"{path}.age_band",
                    )
                )
                continue

            sex = _SEXES.get(entry.sex)
            if sex is None:
                issues.append(
                    Issue(
                        code="unknown_sex",
                        severity=ValidationSeverity.ERROR,
                        message="sex is not in the accepted set",
                        field_path=f"{path}.sex",
                        context={"accepted": sorted(_SEXES)},
                    )
                )
                continue

            if entry.value is None and entry.raw_value:
                # The cell had something in it that is not a number. Kept as a
                # warning with the raw text preserved: "nil" and an illegible
                # mark are different, and a transcriber should see which.
                issues.append(
                    Issue(
                        code="value_not_numeric",
                        severity=ValidationSeverity.WARNING,
                        message="the cell carried text rather than a number; stored as blank",
                        field_path=f"{path}.value",
                    )
                )
            elif entry.value is not None and entry.value < 0:
                issues.append(
                    Issue(
                        code="value_negative",
                        severity=ValidationSeverity.ERROR,
                        message="a count of people is never negative",
                        field_path=f"{path}.value",
                    )
                )
                continue

            key = (entry.element, band, sex)
            if key in seen:
                issues.append(
                    Issue(
                        code="duplicate_cell",
                        severity=ValidationSeverity.ERROR,
                        message="this cell appears twice in one submission",
                        field_path=f"{path}.element",
                        context={"element": entry.element},
                    )
                )
                continue
            seen.add(key)

            validated.append(
                ValidatedObservation(
                    element_code=entry.element,
                    age_band=band,
                    sex=sex,
                    value=entry.value,
                    raw_value=entry.raw_value,
                )
            )

        return validated

    def _stock(
        self, inbound: InboundSubmission, form: AggregateForm, issues: list[Issue]
    ) -> list[ValidatedStock]:
        allowed = _STOCK_CODES[form]
        period_days = (inbound.period_end - inbound.period_start).days + 1
        seen: set[tuple[str, StockMetric]] = set()
        validated: list[ValidatedStock] = []

        for index, entry in enumerate(inbound.stock):
            path = f"stock[{index}]"
            if entry.commodity not in allowed:
                issues.append(
                    Issue(
                        code="unknown_commodity",
                        severity=ValidationSeverity.ERROR,
                        message="MARS does not hold this commodity for this form",
                        field_path=f"{path}.commodity",
                        context={"commodity": entry.commodity, "form": form.value},
                    )
                )
                continue

            metric = _METRICS.get(entry.metric)
            if metric is None or metric not in _STOCK_METRICS[form]:
                issues.append(
                    Issue(
                        code="unknown_stock_metric",
                        severity=ValidationSeverity.ERROR,
                        message="stock metric is not one of the form's own columns",
                        field_path=f"{path}.metric",
                        context={
                            "accepted": sorted(metric.value for metric in _STOCK_METRICS[form])
                        },
                    )
                )
                continue

            if entry.value is None and entry.raw_value:
                issues.append(
                    Issue(
                        code="stock_value_not_numeric",
                        severity=ValidationSeverity.WARNING,
                        message="the stock cell carried text rather than a number; stored as blank",
                        field_path=f"{path}.value",
                    )
                )

            if entry.value is not None and entry.value < 0:
                issues.append(
                    Issue(
                        code="stock_value_negative",
                        severity=ValidationSeverity.ERROR,
                        message="a stock quantity is never negative",
                        field_path=f"{path}.value",
                    )
                )
                continue

            if entry.unit is not None and len(entry.unit) > 64:
                issues.append(
                    Issue(
                        code="text_exceeds_contract_limit",
                        severity=ValidationSeverity.ERROR,
                        message="stock unit exceeds its 64-character contract limit",
                        field_path=f"{path}.unit",
                        context={"maximum": 64},
                    )
                )
                continue

            if (
                metric is StockMetric.DAYS_OUT_OF_STOCK
                and entry.value is not None
                and entry.value > period_days
            ):
                # Checked against this submission's own period, which the
                # database constraint cannot do: a week and a month have
                # different maxima and only the row knows which it is.
                issues.append(
                    Issue(
                        code="days_out_of_stock_exceeds_period",
                        severity=ValidationSeverity.ERROR,
                        message="more days out of stock than the period contains",
                        field_path=f"{path}.value",
                        context={"period_days": period_days},
                    )
                )
                continue

            key = (entry.commodity, metric)
            if key in seen:
                issues.append(
                    Issue(
                        code="duplicate_stock_row",
                        severity=ValidationSeverity.ERROR,
                        message="this commodity and metric appear twice in one submission",
                        field_path=f"{path}.commodity",
                    )
                )
                continue
            seen.add(key)

            validated.append(
                ValidatedStock(
                    commodity_code=entry.commodity,
                    metric=metric,
                    value=entry.value,
                    unit_of_issue=entry.unit,
                    raw_value=entry.raw_value,
                )
            )

        return validated

    def _laboratory(
        self, inbound: InboundSubmission, form: AggregateForm, issues: list[Issue]
    ) -> list[ValidatedLaboratory]:
        seen: set[str] = set()
        validated: list[ValidatedLaboratory] = []

        for index, entry in enumerate(inbound.laboratory):
            path = f"laboratory[{index}]"
            if entry.test not in LABORATORY_CODES:
                issues.append(
                    Issue(
                        code="unknown_laboratory_test",
                        severity=ValidationSeverity.ERROR,
                        message="MARS does not hold this laboratory test",
                        field_path=f"{path}.test",
                        context={"test": entry.test, "accepted": sorted(LABORATORY_CODES)},
                    )
                )
                continue

            if form is not AggregateForm.HMIS_105:
                # Section 10 is on HMIS 105. A laboratory row on a weekly form
                # is a producer error, and accepting it would put a monthly
                # quantity into a weekly submission.
                issues.append(
                    Issue(
                        code="laboratory_block_on_the_wrong_form",
                        severity=ValidationSeverity.ERROR,
                        message="the laboratory section is on HMIS 105, not this form",
                        field_path=path,
                    )
                )
                continue

            for name, value, raw in (
                ("done", entry.done, entry.raw_done),
                ("positive", entry.positive, entry.raw_positive),
            ):
                if value is None and raw:
                    issues.append(
                        Issue(
                            code="laboratory_value_not_numeric",
                            severity=ValidationSeverity.WARNING,
                            message=(
                                "the laboratory cell carried text rather than a number; "
                                "stored as blank"
                            ),
                            field_path=f"{path}.{name}",
                        )
                    )

            for name, value in (("done", entry.done), ("positive", entry.positive)):
                if value is not None and value < 0:
                    issues.append(
                        Issue(
                            code="laboratory_value_negative",
                            severity=ValidationSeverity.ERROR,
                            message="a count of tests is never negative",
                            field_path=f"{path}.{name}",
                        )
                    )
                    break
            else:
                if (
                    entry.done is not None
                    and entry.positive is not None
                    and entry.positive > entry.done
                ):
                    issues.append(
                        Issue(
                            code="more_positive_than_done",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                "more positive results than tests performed; one of "
                                "the two was transcribed wrongly"
                            ),
                            field_path=path,
                            context={"done": entry.done, "positive": entry.positive},
                        )
                    )
                    continue

                if entry.test in seen:
                    issues.append(
                        Issue(
                            code="duplicate_laboratory_row",
                            severity=ValidationSeverity.ERROR,
                            message="this test appears twice in one submission",
                            field_path=f"{path}.test",
                        )
                    )
                    continue
                seen.add(entry.test)

                validated.append(
                    ValidatedLaboratory(
                        test_code=entry.test,
                        number_done=entry.done,
                        number_positive=entry.positive,
                        raw_done=entry.raw_done,
                        raw_positive=entry.raw_positive,
                    )
                )

        return validated


def _month_end(first: date) -> date:
    if first.month == 12:
        return date(first.year, 12, 31)
    return date(first.year, first.month + 1, 1) - timedelta(days=1)


__all__ = [
    "FORM_PERIODS",
    "AggregateValidator",
    "Issue",
    "SubmissionValidation",
    "ValidatedLaboratory",
    "ValidatedObservation",
    "ValidatedStock",
    "ValidatedSubmission",
]
