"""Reconciling a reported aggregate against the encounters MARS holds.

A facility submits HMIS 033b weekly and HMIS 105 monthly. MARS separately holds
the e-register encounters those forms summarise, so it can compute the same
quantities itself. Where the two disagree, the disagreement is the finding.

**Neither number is corrected.** Preferring the aggregate would hide the
register's detail; preferring the derived figure would mean MARS publishing
numbers no facility ever submitted, which is not MARS's to do. Both are stored,
with their difference, and resolving it belongs to the district.

**A comparison is only made where one is possible.** With no e-register data for
that facility and period there is nothing to compare against, and reporting a
difference of everything would flood a reconciliation screen with findings that
say only "we have no register data" - which is a completeness problem, reported
as ``UNCOMPARABLE`` rather than as a discrepancy.

**Every derived figure states its denominator.** A difference of four computed
from four encounters and one computed from four hundred deserve different
attention, and a bare difference hides which is which.

The mapping from a form cell to an encounter query is the whole substance of
this module, so each one is written out with the reason it is that query and not
a similar one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.aggregate import (
    AggregateObservation,
    AggregateSubmission,
    ReconciliationFinding,
)
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    AttendanceType,
    MalariaTestMethod,
    MalariaTestResult,
    ReconciliationStatus,
)

logger = get_logger(__name__)

#: Bumped whenever a mapping below changes. A finding is read against the rule
#: that was in force when it was made, so an old finding does not silently
#: acquire a new meaning.
RECONCILIATION_METHOD_VERSION = "1.0.0"

#: How far a reported figure may differ from the derived one before it is worth
#: a district's attention.
#:
#: **Not a programme threshold.** No supplied source defines an acceptable
#: transcription variance, so MARS does not invent one: the default is exact
#: agreement, and any tolerance is a deployment's explicit choice recorded on
#: the finding's method version.
DEFAULT_ABSOLUTE_TOLERANCE = 0


@dataclass(frozen=True, slots=True)
class DerivationRule:
    """How one form cell is computed from encounters."""

    element_code: str
    label: str
    #: Why this query and not a similar one. Kept with the rule because the
    #: near-miss alternatives are all plausible and all wrong.
    rationale: str
    predicate: Callable[[Select[tuple[int]]], Select[tuple[int]]]


def _tested(query: Select[tuple[int]]) -> Select[tuple[int]]:
    return query.where(OpdEncounterTest.method != MalariaTestMethod.NOT_DONE)


def _positive(query: Select[tuple[int]]) -> Select[tuple[int]]:
    return query.where(OpdEncounterTest.result == MalariaTestResult.POSITIVE)


def _rdt(query: Select[tuple[int]]) -> Select[tuple[int]]:
    return query.where(OpdEncounterTest.method == MalariaTestMethod.RDT)


def _microscopy(query: Select[tuple[int]]) -> Select[tuple[int]]:
    return query.where(OpdEncounterTest.method == MalariaTestMethod.MICROSCOPY)


#: HMIS 105, monthly.
HMIS_105_RULES: tuple[DerivationRule, ...] = (
    DerivationRule(
        element_code="OA01",
        label="New attendance",
        rationale=(
            "Encounters recorded as a new attendance. Counted from the "
            "attendance type the register carries, not inferred from whether "
            "the patient was seen before - a patient's first visit to this "
            "facility can be a re-attendance elsewhere."
        ),
        predicate=lambda q: q.where(OpdEncounter.attendance_type == AttendanceType.NEW_ATTENDANCE),
    ),
    DerivationRule(
        element_code="OA02",
        label="Re-attendance",
        rationale="Encounters the register marks as a re-attendance.",
        predicate=lambda q: q.where(OpdEncounter.attendance_type == AttendanceType.RE_ATTENDANCE),
    ),
    DerivationRule(
        element_code="EP01b",
        label="Malaria Tested (B/s & RDT)",
        rationale=(
            "Encounters with a malaria test actually performed. Encounters "
            "recording 'not done' are excluded: the form counts tests, and a "
            "denominator inflated by untested attendances understates "
            "positivity everywhere."
        ),
        predicate=_tested,
    ),
    DerivationRule(
        element_code="EP01c",
        label="Malaria confirmed (B/s & RDT)",
        rationale=(
            "Encounters with a positive malaria result. Confirmed means a read "
            "test, so a clinical malaria diagnosis with no test does not count "
            "here - that difference is exactly what EP01e minus EP01d shows."
        ),
        predicate=_positive,
    ),
)

#: HMIS 033b, weekly. Section 5 splits testing by method, so the derived
#: figures are split the same way rather than being compared against a total.
HMIS_033B_RULES: tuple[DerivationRule, ...] = (
    DerivationRule(
        element_code="MA.",
        label="Malaria (Confirmed) — total cases this week",
        rationale=(
            "Encounters with a positive result. The form says 'Confirmed', so "
            "presumptively treated cases are not counted - which is why this "
            "figure is not the same quantity as HMIS 105 EP01e."
        ),
        predicate=_positive,
    ),
    DerivationRule(
        element_code="M033B_MAT_TESTED_RDT",
        label="Cases tested with RDT",
        rationale="Encounters whose recorded method is RDT.",
        predicate=_rdt,
    ),
    DerivationRule(
        element_code="M033B_MAT_RDT_POSITIVE",
        label="RDT Positive Cases",
        rationale="Encounters tested by RDT with a positive result.",
        predicate=lambda q: _positive(_rdt(q)),
    ),
    DerivationRule(
        element_code="M033B_MAT_TESTED_MICROSCOPY",
        label="Cases tested with Microscopy",
        rationale="Encounters whose recorded method is microscopy.",
        predicate=_microscopy,
    ),
    DerivationRule(
        element_code="M033B_MAT_MICROSCOPY_POSITIVE",
        label="Microscopy Positive Cases",
        rationale="Encounters tested by microscopy with a positive result.",
        predicate=lambda q: _positive(_microscopy(q)),
    ),
    DerivationRule(
        element_code="M033B_APT_OPD_NEW",
        label="OPD New Attendance",
        rationale="Encounters the register marks as a new attendance.",
        predicate=lambda q: q.where(OpdEncounter.attendance_type == AttendanceType.NEW_ATTENDANCE),
    ),
)

RULES_BY_FORM = {
    "hmis_105": HMIS_105_RULES,
    "hmis_033b": HMIS_033B_RULES,
}


@dataclass(slots=True)
class ReconciliationReport:
    """What one reconciliation run found."""

    submission_id: uuid.UUID | None = None
    matched: int = 0
    within_tolerance: int = 0
    differs: int = 0
    reported_only: int = 0
    derived_only: int = 0
    uncomparable: int = 0
    encounters_in_period: int = 0

    @property
    def comparisons(self) -> int:
        return self.matched + self.within_tolerance + self.differs

    def as_dict(self) -> dict[str, object]:
        return {
            "submission_id": str(self.submission_id) if self.submission_id else None,
            "matched": self.matched,
            "within_tolerance": self.within_tolerance,
            "differs": self.differs,
            "reported_only": self.reported_only,
            "derived_only": self.derived_only,
            "uncomparable": self.uncomparable,
            "encounters_in_period": self.encounters_in_period,
        }


class ReconciliationService:
    """Compares a submission with the encounters behind it."""

    def __init__(self, session: Session, *, tolerance: int = DEFAULT_ABSOLUTE_TOLERANCE) -> None:
        if tolerance < 0:
            raise ValueError("tolerance cannot be negative")
        self._session = session
        self._tolerance = tolerance

    def reconcile(self, submission: AggregateSubmission) -> ReconciliationReport:
        """Compare one submission and record the findings.

        A finding is **never rewritten**. It is keyed by the submission, the
        element, the method version, the tolerance and a fingerprint of the
        exact evidence it was computed from, so re-running the same rules at
        the same tolerance over unchanged encounters is idempotent, while a
        later import writes *new* findings alongside the old ones.

        That is the point: a district reviews a finding and acts on it. If a
        correction to the register could silently change what that finding
        said, the record would no longer explain the decision anyone made.
        """
        report = ReconciliationReport(submission_id=submission.id)
        rules = RULES_BY_FORM.get(submission.form.value, ())
        if not rules:
            return report

        encounters = self._encounters_in_period(submission)
        report.encounters_in_period = encounters

        reported = self._reported_totals(submission)
        input_checksum = self._input_checksum(submission)
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {
                "lock_key": _reconciliation_lock_key(
                    submission.id,
                    input_checksum,
                    self._tolerance,
                )
            },
        )
        existing = {
            finding.element_code: finding
            for finding in self._session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.aggregate_submission_id == submission.id,
                    ReconciliationFinding.method_version == RECONCILIATION_METHOD_VERSION,
                    ReconciliationFinding.input_checksum == input_checksum,
                    ReconciliationFinding.absolute_tolerance == self._tolerance,
                )
            )
            .scalars()
            .all()
        }

        for rule in rules:
            reported_value = reported.get(rule.element_code)
            derived_value = self._derive(submission, rule) if encounters else None
            status, difference, detail = self._compare(
                reported_value, derived_value, encounters, rule
            )

            finding = existing.get(rule.element_code)
            if finding is None:
                finding = ReconciliationFinding(
                    aggregate_submission_id=submission.id,
                    element_code=rule.element_code,
                    method_version=RECONCILIATION_METHOD_VERSION,
                    input_checksum=input_checksum,
                    absolute_tolerance=self._tolerance,
                    reconciliation_status=status,
                    reported_value=reported_value,
                    derived_value=derived_value,
                    difference=difference,
                    derived_denominator=encounters,
                    detail=detail,
                )
                self._session.add(finding)

            setattr(report, status.value, getattr(report, status.value) + 1)

        self._session.flush()
        logger.info("reconciliation_finished", **report.as_dict())
        return report

    def _input_checksum(self, submission: AggregateSubmission) -> str:
        """Fingerprint exactly the evidence read by this run.

        A method version identifies the rule, not the mutable encounter
        snapshot. Including record ids, update timestamps and test values makes
        a rerun idempotent while ensuring later source corrections create a new
        immutable finding instead of changing history.
        """
        rows = self._session.execute(
            select(
                OpdEncounter.id,
                OpdEncounter.updated_at,
                OpdEncounter.attendance_type,
                OpdEncounterTest.id,
                OpdEncounterTest.updated_at,
                OpdEncounterTest.method,
                OpdEncounterTest.result,
            )
            .select_from(OpdEncounter)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                OpdEncounter.facility_id == submission.facility_id,
                OpdEncounter.encounter_date >= submission.period_start,
                OpdEncounter.encounter_date <= submission.period_end,
            )
            .order_by(OpdEncounter.id, OpdEncounterTest.id)
        ).all()
        evidence = {
            "submission": submission.payload_checksum,
            "encounters": [
                [
                    str(encounter_id),
                    updated_at.isoformat(),
                    attendance_type.value,
                    str(test_id) if test_id else None,
                    test_updated_at.isoformat() if test_updated_at else None,
                    method.value if method else None,
                    result.value if result else None,
                ]
                for (
                    encounter_id,
                    updated_at,
                    attendance_type,
                    test_id,
                    test_updated_at,
                    method,
                    result,
                ) in rows
            ],
        }
        return hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    # -- Comparison --------------------------------------------------------
    def _compare(
        self,
        reported: int | None,
        derived: int | None,
        encounters: int,
        rule: DerivationRule,
    ) -> tuple[ReconciliationStatus, int | None, dict[str, object] | None]:
        if encounters == 0:
            return (
                ReconciliationStatus.UNCOMPARABLE,
                None,
                {
                    "reason": (
                        "MARS holds no encounters for this facility and period, so "
                        "there is nothing to compare against. This is a reporting "
                        "completeness question, not a discrepancy."
                    ),
                    "rule": rule.rationale,
                },
            )

        if reported is None and derived is None:
            return (
                ReconciliationStatus.UNCOMPARABLE,
                None,
                {"reason": "the cell was blank and no encounters matched the rule"},
            )

        if reported is None:
            # The cell was blank. Not a zero: the facility made no statement,
            # and reporting a difference against a statement nobody made would
            # be a finding about MARS, not about the facility.
            return (
                ReconciliationStatus.DERIVED_ONLY,
                None,
                {
                    "reason": (
                        "the form cell was blank. A blank is not a zero: the "
                        "facility made no statement about this figure."
                    ),
                    "rule": rule.rationale,
                },
            )

        if derived is None:
            return (
                ReconciliationStatus.REPORTED_ONLY,
                None,
                {"reason": "no encounter matched the rule for this period"},
            )

        difference = reported - derived
        if difference == 0:
            return ReconciliationStatus.MATCHED, 0, None
        if abs(difference) <= self._tolerance:
            return (
                ReconciliationStatus.WITHIN_TOLERANCE,
                difference,
                {"tolerance": self._tolerance},
            )
        return (
            ReconciliationStatus.DIFFERS,
            difference,
            {"rule": rule.rationale, "tolerance": self._tolerance},
        )

    # -- Reported side -----------------------------------------------------
    def _reported_totals(self, submission: AggregateSubmission) -> dict[str, int | None]:
        """Each element's reported total, summed over its disaggregation.

        A cell with any reported value contributes; an element whose cells are
        **all** blank stays ``None``. Summing blanks as zero would turn a
        facility that did not report into a facility that reported none.
        """
        rows = self._session.execute(
            select(
                AggregateObservation.element_code,
                func.sum(AggregateObservation.value),
                func.count(AggregateObservation.value),
            )
            .where(AggregateObservation.aggregate_submission_id == submission.id)
            .group_by(AggregateObservation.element_code)
        ).all()

        totals: dict[str, int | None] = {}
        for element_code, total, reported_cells in rows:
            totals[element_code] = int(total) if reported_cells else None
        return totals

    # -- Derived side ------------------------------------------------------
    def _base_query(self, submission: AggregateSubmission) -> Select[tuple[int]]:
        """Encounters for this facility in this period.

        Joined to tests with an outer join so an encounter with no test row
        still counts towards attendance. An inner join here would quietly make
        every attendance figure a testing figure.
        """
        return (
            select(func.count(func.distinct(OpdEncounter.id)))
            .select_from(OpdEncounter)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                OpdEncounter.facility_id == submission.facility_id,
                OpdEncounter.encounter_date >= submission.period_start,
                OpdEncounter.encounter_date <= submission.period_end,
            )
        )

    def _derive(self, submission: AggregateSubmission, rule: DerivationRule) -> int:
        return int(self._session.execute(rule.predicate(self._base_query(submission))).scalar_one())

    def _encounters_in_period(self, submission: AggregateSubmission) -> int:
        return int(
            self._session.execute(
                select(func.count())
                .select_from(OpdEncounter)
                .where(
                    OpdEncounter.facility_id == submission.facility_id,
                    OpdEncounter.encounter_date >= submission.period_start,
                    OpdEncounter.encounter_date <= submission.period_end,
                )
            ).scalar_one()
        )


def period_contains(submission: AggregateSubmission, day: date) -> bool:
    """Whether a date falls in a submission's period."""
    return submission.period_start <= day <= submission.period_end


def _reconciliation_lock_key(submission_id: uuid.UUID, input_checksum: str, tolerance: int) -> int:
    identity = f"{submission_id}|{input_checksum}|{tolerance}"
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)


__all__ = [
    "DEFAULT_ABSOLUTE_TOLERANCE",
    "HMIS_033B_RULES",
    "HMIS_105_RULES",
    "RECONCILIATION_METHOD_VERSION",
    "RULES_BY_FORM",
    "DerivationRule",
    "ReconciliationReport",
    "ReconciliationService",
    "period_contains",
]
