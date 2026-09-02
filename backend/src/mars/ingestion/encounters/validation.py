"""Validating an inbound row into something the canonical model can hold.

Two rules shape everything here.

**Never coerce an unknown code.** A ``B/S`` that quietly becomes ``unknown``
loses a test that was performed, and nobody finds out until a testing-coverage
figure looks wrong months later. An unrecognised value quarantines the row and
names the field and the value set, so a producer can fix the mapping.

**Blank is not zero.** Absent, ``null`` and ``""`` all mean *not recorded*. None
of them becomes ``0``, ``false`` or a default, because "no test was done" and "a
test was done and found nothing" are different clinical facts.

Every issue carries a machine-readable code, a dotted field path and a message
written to be safe to display: it names the field and the code that was not
understood, never a patient value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from mars.domain.enums import (
    AgeUnit,
    AttendanceType,
    DateAssignmentMethod,
    FeverStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PatientCategory,
    ReferralDirection,
    Sex,
    ValidationSeverity,
)
from mars.ingestion.encounters.contract import InboundRow

#: Codes the register prints, mapped to what MARS stores. The keys are what a
#: producer may send; anything else is refused rather than guessed.
_SEX = {
    "m": Sex.MALE,
    "male": Sex.MALE,
    "f": Sex.FEMALE,
    "female": Sex.FEMALE,
    "unknown": Sex.UNKNOWN,
}
_CATEGORY = {
    "n": PatientCategory.NATIONAL,
    "national": PatientCategory.NATIONAL,
    "r": PatientCategory.REFUGEE,
    "refugee": PatientCategory.REFUGEE,
    "f": PatientCategory.FOREIGNER,
    "foreigner": PatientCategory.FOREIGNER,
    "unknown": PatientCategory.UNKNOWN,
}
_AGE_UNIT = {unit.value: unit for unit in AgeUnit}
_ATTENDANCE = {value.value: value for value in AttendanceType}
_FEVER = {value.value: value for value in FeverStatus}
_TEST_METHOD = {value.value: value for value in MalariaTestMethod}
_TEST_RESULT = {value.value: value for value in MalariaTestResult}
_DIRECTION = {value.value: value for value in ReferralDirection}
_DATE_SOURCE = {value.value: value for value in DateAssignmentMethod}

#: The form's own unit rules. An age in months belongs under a year, in days
#: under a month; a value outside these is a transcription error rather than an
#: unusual patient.
_AGE_BOUNDS = {AgeUnit.YEARS: 130, AgeUnit.MONTHS: 11, AgeUnit.DAYS: 30}


@dataclass(frozen=True, slots=True)
class Issue:
    """One finding about one row, or about the batch."""

    code: str
    severity: ValidationSeverity
    message: str
    field_path: str | None = None
    context: dict[str, Any] | None = None

    @property
    def blocks_row(self) -> bool:
        return self.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}


@dataclass(slots=True)
class ValidatedEncounter:
    """A row that can be written, with whatever warnings it carried.

    Holds no identity: the identity block is handled separately, inside the
    identity boundary, and never reaches this object.
    """

    source_row_id: str
    encounter_date: date
    date_assignment_method: DateAssignmentMethod
    sex: Sex
    patient_category: PatientCategory
    attendance_type: AttendanceType
    fever_present: FeverStatus
    serial_number: str | None = None
    age_value: int | None = None
    age_unit: AgeUnit | None = None
    age_days_approx: int | None = None
    presenting_complaint: str | None = None
    notifiable_marked: bool = False
    residence_district_raw: str | None = None
    residence_subcounty_raw: str | None = None
    residence_parish_raw: str | None = None
    residence_village_raw: str | None = None
    tests: list[tuple[MalariaTestMethod, MalariaTestResult]] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)
    prescriptions: list[dict[str, Any]] = field(default_factory=list)
    referrals: list[tuple[ReferralDirection, str]] = field(default_factory=list)


@dataclass(slots=True)
class RowValidation:
    """The outcome of validating one row."""

    row: InboundRow
    encounter: ValidatedEncounter | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def is_loadable(self) -> bool:
        return self.encounter is not None and not any(i.blocks_row for i in self.issues)


class EncounterValidator:
    """Turns one inbound row into a writable encounter, or explains why not."""

    def validate(self, row: InboundRow) -> RowValidation:
        issues: list[Issue] = []
        payload = row.raw

        encounter_date = self._date(payload.get("encounter_date"), "encounter_date", issues)
        sex = self._coded(payload.get("sex"), _SEX, "sex", issues, Sex.UNKNOWN)

        if encounter_date is None:
            # Without a date the row cannot be placed in time, and an encounter
            # that cannot be placed in time is not a surveillance record.
            return RowValidation(row=row, encounter=None, issues=issues)

        age_value, age_unit = self._age(payload.get("age"), issues)
        raw_residence = payload.get("residence")
        residence: dict[str, Any] = raw_residence if isinstance(raw_residence, dict) else {}

        encounter = ValidatedEncounter(
            source_row_id=row.source_row_id,
            encounter_date=encounter_date,
            date_assignment_method=self._coded(
                payload.get("date_source"),
                _DATE_SOURCE,
                "date_source",
                issues,
                DateAssignmentMethod.SOURCE_SUPPLIED,
            ),
            sex=sex,
            patient_category=self._coded(
                payload.get("patient_category"),
                _CATEGORY,
                "patient_category",
                issues,
                PatientCategory.UNKNOWN,
            ),
            attendance_type=self._coded(
                payload.get("attendance_type"),
                _ATTENDANCE,
                "attendance_type",
                issues,
                AttendanceType.UNKNOWN,
            ),
            fever_present=self._coded(
                payload.get("fever_present"),
                _FEVER,
                "fever_present",
                issues,
                FeverStatus.UNKNOWN,
            ),
            serial_number=_text(payload.get("serial_number"), 16),
            age_value=age_value,
            age_unit=age_unit,
            age_days_approx=_approximate_days(age_value, age_unit),
            presenting_complaint=_text(payload.get("presenting_complaint"), 4000),
            notifiable_marked=bool(payload.get("notifiable_marked", False)),
            residence_district_raw=_text(residence.get("district"), 160),
            residence_subcounty_raw=_text(residence.get("subcounty"), 160),
            residence_parish_raw=_text(residence.get("parish"), 160),
            residence_village_raw=_text(residence.get("village"), 160),
        )

        encounter.tests = self._tests(payload.get("tests"), issues)
        encounter.diagnoses = self._diagnoses(payload.get("diagnoses"), issues)
        encounter.prescriptions = self._prescriptions(payload.get("prescriptions"), issues)
        encounter.referrals = self._referrals(payload.get("referrals"), issues)

        return RowValidation(row=row, encounter=encounter, issues=issues)

    # -- Field helpers -----------------------------------------------------
    def _date(self, value: Any, path: str, issues: list[Issue]) -> date | None:
        if value in (None, ""):
            issues.append(
                Issue(
                    code="encounter_date_missing",
                    severity=ValidationSeverity.ERROR,
                    message="encounter_date is required; a row without a date cannot "
                    "be placed in time",
                    field_path=path,
                )
            )
            return None
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            issues.append(
                Issue(
                    code="encounter_date_unparsable",
                    severity=ValidationSeverity.ERROR,
                    message="encounter_date is not an ISO date (YYYY-MM-DD)",
                    field_path=path,
                )
            )
            return None

        if parsed > datetime.now().date():
            issues.append(
                Issue(
                    code="encounter_date_in_future",
                    severity=ValidationSeverity.ERROR,
                    message="encounter_date is in the future",
                    field_path=path,
                )
            )
            return None
        return parsed

    def _coded(
        self,
        value: Any,
        allowed: dict[str, Any],
        path: str,
        issues: list[Issue],
        default: Any,
    ) -> Any:
        """Map a printed code, or refuse it.

        An absent value takes the documented default. An *unrecognised* value
        does not: it records an error naming the accepted set, because silently
        defaulting is how a whole source system's mapping error stays invisible.
        """
        if value in (None, ""):
            return default
        key = str(value).strip().lower()
        if key in allowed:
            return allowed[key]

        issues.append(
            Issue(
                code="unrecognised_code",
                severity=ValidationSeverity.ERROR,
                message=f"{path} carries a value that is not in the accepted set",
                field_path=path,
                context={"accepted": sorted(allowed), "received": key[:32]},
            )
        )
        return default

    def _age(self, block: Any, issues: list[Issue]) -> tuple[int | None, AgeUnit | None]:
        """Both or neither.

        A number with no unit is not an age: read as years by anything assuming
        a default, a three-day-old becomes a three-year-old and the error
        survives into every age-banded figure without ever looking wrong.
        """
        if not isinstance(block, dict):
            return None, None

        raw_value = block.get("value")
        raw_unit = block.get("unit")
        if raw_value in (None, "") and raw_unit in (None, ""):
            return None, None

        if raw_value in (None, "") or raw_unit in (None, ""):
            issues.append(
                Issue(
                    code="age_pair_incomplete",
                    severity=ValidationSeverity.ERROR,
                    message="age requires both a value and a unit, or neither",
                    field_path="age",
                )
            )
            return None, None

        try:
            value = int(str(raw_value))
        except (TypeError, ValueError):
            issues.append(
                Issue(
                    code="age_value_unparsable",
                    severity=ValidationSeverity.ERROR,
                    message="age.value is not a whole number",
                    field_path="age.value",
                )
            )
            return None, None

        unit = _AGE_UNIT.get(str(raw_unit).strip().lower())
        if unit is None:
            issues.append(
                Issue(
                    code="unrecognised_code",
                    severity=ValidationSeverity.ERROR,
                    message="age.unit is not in the accepted set",
                    field_path="age.unit",
                    context={"accepted": sorted(_AGE_UNIT)},
                )
            )
            return None, None

        if value < 0:
            issues.append(
                Issue(
                    code="age_negative",
                    severity=ValidationSeverity.ERROR,
                    message="age.value is negative",
                    field_path="age.value",
                )
            )
            return None, None

        if value > _AGE_BOUNDS[unit]:
            issues.append(
                Issue(
                    code="age_outside_unit_bounds",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"age.value exceeds what {unit.value} can express on this "
                        "form; it should have been written in the next unit up"
                    ),
                    field_path="age.value",
                    context={"unit": unit.value, "maximum": _AGE_BOUNDS[unit]},
                )
            )
            return None, None

        return value, unit

    def _tests(
        self, block: Any, issues: list[Issue]
    ) -> list[tuple[MalariaTestMethod, MalariaTestResult]]:
        if block in (None, ""):
            return []
        if not isinstance(block, list):
            issues.append(
                Issue(
                    code="tests_not_a_list",
                    severity=ValidationSeverity.ERROR,
                    message="tests must be a list",
                    field_path="tests",
                )
            )
            return []

        tests: list[tuple[MalariaTestMethod, MalariaTestResult]] = []
        for index, entry in enumerate(block):
            if not isinstance(entry, dict):
                issues.append(
                    Issue(
                        code="test_not_an_object",
                        severity=ValidationSeverity.ERROR,
                        message="each test must be an object",
                        field_path=f"tests[{index}]",
                    )
                )
                continue

            method = self._coded(
                entry.get("method"),
                _TEST_METHOD,
                f"tests[{index}].method",
                issues,
                MalariaTestMethod.UNKNOWN,
            )
            result = self._coded(
                entry.get("result"),
                _TEST_RESULT,
                f"tests[{index}].result",
                issues,
                MalariaTestResult.UNKNOWN,
            )

            # The paper register permits writing a result with no test. MARS
            # refuses to store the contradiction: a phantom result would enter
            # every positivity rate downstream.
            if method is MalariaTestMethod.NOT_DONE and result in {
                MalariaTestResult.POSITIVE,
                MalariaTestResult.NEGATIVE,
            }:
                issues.append(
                    Issue(
                        code="result_without_a_test",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "a read result is recorded although no test was "
                            "performed; one of the two is a transcription error"
                        ),
                        field_path=f"tests[{index}]",
                        context={"method": method.value, "result": result.value},
                    )
                )
                continue

            tests.append((method, result))
        return tests

    def _diagnoses(self, block: Any, issues: list[Issue]) -> list[str]:
        if block in (None, ""):
            return []
        if not isinstance(block, list):
            issues.append(
                Issue(
                    code="diagnoses_not_a_list",
                    severity=ValidationSeverity.ERROR,
                    message="diagnoses must be a list of strings",
                    field_path="diagnoses",
                )
            )
            return []

        diagnoses: list[str] = []
        for index, entry in enumerate(block):
            text = _text(entry, 300)
            if text is None:
                issues.append(
                    Issue(
                        code="diagnosis_blank",
                        severity=ValidationSeverity.WARNING,
                        message="a blank diagnosis entry was ignored",
                        field_path=f"diagnoses[{index}]",
                    )
                )
                continue
            diagnoses.append(text)
        return diagnoses

    def _prescriptions(self, block: Any, issues: list[Issue]) -> list[dict[str, Any]]:
        if block in (None, ""):
            return []
        if not isinstance(block, list):
            issues.append(
                Issue(
                    code="prescriptions_not_a_list",
                    severity=ValidationSeverity.ERROR,
                    message="prescriptions must be a list",
                    field_path="prescriptions",
                )
            )
            return []

        prescriptions: list[dict[str, Any]] = []
        for index, entry in enumerate(block):
            if isinstance(entry, str):
                text = _text(entry, 300)
                if text:
                    prescriptions.append({"prescription_raw": text})
                continue
            if not isinstance(entry, dict):
                issues.append(
                    Issue(
                        code="prescription_not_an_object",
                        severity=ValidationSeverity.ERROR,
                        message="each prescription must be a string or an object",
                        field_path=f"prescriptions[{index}]",
                    )
                )
                continue

            raw = _text(entry.get("text"), 300)
            if raw is None:
                issues.append(
                    Issue(
                        code="prescription_blank",
                        severity=ValidationSeverity.WARNING,
                        message="a prescription with no text was ignored",
                        field_path=f"prescriptions[{index}]",
                    )
                )
                continue

            parsed: dict[str, Any] = {
                "prescription_raw": raw,
                "drug_name_raw": _text(entry.get("drug_name"), 200),
                "is_device": bool(entry.get("is_device", False)),
            }

            factors: dict[str, float] = {}
            for name in ("units_per_dose", "doses_per_day", "days"):
                value = entry.get(name)
                if value in (None, ""):
                    continue
                try:
                    number = float(str(value))
                except (TypeError, ValueError):
                    issues.append(
                        Issue(
                            code="prescription_factor_unparsable",
                            severity=ValidationSeverity.ERROR,
                            message=f"{name} is not a number",
                            field_path=f"prescriptions[{index}].{name}",
                        )
                    )
                    continue
                if number <= 0:
                    issues.append(
                        Issue(
                            code="prescription_factor_not_positive",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"{name} is not positive; a blank is how the form "
                                "expresses 'not recorded', never a zero"
                            ),
                            field_path=f"prescriptions[{index}].{name}",
                        )
                    )
                    continue
                factors[name] = number

            parsed.update(factors)
            # The total is either fully derivable from what was written or it is
            # not known. A partial product would be a number nobody wrote.
            if len(factors) == 3:
                parsed["total_units"] = (
                    factors["units_per_dose"] * factors["doses_per_day"] * factors["days"]
                )
            prescriptions.append(parsed)
        return prescriptions

    def _referrals(self, block: Any, issues: list[Issue]) -> list[tuple[ReferralDirection, str]]:
        if block in (None, ""):
            return []
        if not isinstance(block, list):
            issues.append(
                Issue(
                    code="referrals_not_a_list",
                    severity=ValidationSeverity.ERROR,
                    message="referrals must be a list",
                    field_path="referrals",
                )
            )
            return []

        referrals: list[tuple[ReferralDirection, str]] = []
        for index, entry in enumerate(block):
            if not isinstance(entry, dict):
                issues.append(
                    Issue(
                        code="referral_not_an_object",
                        severity=ValidationSeverity.ERROR,
                        message="each referral must be an object",
                        field_path=f"referrals[{index}]",
                    )
                )
                continue
            number = _text(entry.get("number"), 64)
            if number is None:
                issues.append(
                    Issue(
                        code="referral_number_missing",
                        severity=ValidationSeverity.WARNING,
                        message="a referral with no number was ignored",
                        field_path=f"referrals[{index}]",
                    )
                )
                continue
            direction = self._coded(
                entry.get("direction"),
                _DIRECTION,
                f"referrals[{index}].direction",
                issues,
                ReferralDirection.OUTBOUND,
            )
            referrals.append((direction, number.upper()))
        return referrals


def _text(value: Any, limit: int) -> str | None:
    """Trim to a stored length, treating blank as absent."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _approximate_days(value: int | None, unit: AgeUnit | None) -> int | None:
    """Age in days, for banding only.

    Approximate because the register carries no date of birth: "3 years" is
    anywhere in a 365-day window. Never presented as an exact age.
    """
    if value is None or unit is None:
        return None
    return {AgeUnit.YEARS: value * 365, AgeUnit.MONTHS: value * 30, AgeUnit.DAYS: value}[unit]


__all__ = ["EncounterValidator", "Issue", "RowValidation", "ValidatedEncounter"]
