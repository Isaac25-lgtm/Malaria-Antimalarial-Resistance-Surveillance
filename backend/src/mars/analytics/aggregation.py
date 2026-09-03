"""Computing indicator values and materialising them.

The engine reads an **approved** definition version, computes the figure from
canonical source data, and writes an immutable result carrying everything
needed to explain it later.

Five rules govern every calculation here, and each one exists because its
opposite produces a number that looks right and is wrong.

**Only the latest accepted source revision counts.** A superseded aggregate
submission is history, not data. Summing every revision would count a corrected
month twice.

**Reported and derived stay apart.** An indicator names one source domain. A
figure summed from HMIS 105 and a figure computed from the e-register are two
measurements of the same thing; adding them double-counts, and choosing between
them silently hides a data-quality finding.

**An undefined denominator produces no value.** Never zero. A positivity of 0.0
and a positivity that could not be computed look identical in a chart and are
opposite statements about a facility.

**A blank input stays missing.** It does not contribute to a sum and it is
counted, so a district total from four reporting facilities is distinguishable
from one from forty.

**Rollups go upward only.** Facility to subcounty to district to national. A
figure a facility reported as a total is never split into detail the facility
never supplied.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.aggregate import (
    AggregateObservation,
    AggregateSubmission,
    CommodityStockObservation,
    LaboratoryTestObservation,
)
from mars.domain.encounter import OpdEncounter, OpdEncounterPrescription, OpdEncounterTest
from mars.domain.enums import (
    AgeBand,
    AggregateSubmissionStatus,
    FeverStatus,
    GeographyGrain,
    IndicatorUnit,
    IndicatorValueStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PeriodGrain,
    Sex,
)
from mars.domain.indicator import IndicatorDefinitionVersion, IndicatorResult
from mars.domain.organisation import Facility

logger = get_logger(__name__)

#: Bumped when a change here could alter a figure for unchanged inputs.
ENGINE_VERSION = "1.0.0"


class IndicatorNotApprovedError(RuntimeError):
    """A figure was requested for a definition with no active version.

    Raised rather than falling back to a draft. A draft is a proposal; using
    one would publish a figure computed by rules nobody signed, and the caller
    could not tell from the result that it had happened.
    """


@dataclass(slots=True)
class ComputedValue:
    """One figure, before it is written.

    ``value`` is ``None`` whenever ``status`` is not ``AVAILABLE``, and the
    two are kept in step by the database constraint as well as by this class.
    """

    numerator: int | None
    denominator: int | None
    value: Decimal | None
    status: IndicatorValueStatus
    contributing_units: int | None = None
    expected_units: int | None = None
    missing_inputs: int | None = None
    quality: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MaterialisationReport:
    """What a materialisation run produced."""

    indicator_code: str = ""
    results_written: int = 0
    results_unchanged: int = 0
    unavailable: int = 0
    periods: int = 0
    source_cutoff: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "indicator_code": self.indicator_code,
            "results_written": self.results_written,
            "results_unchanged": self.results_unchanged,
            "unavailable": self.unavailable,
            "periods": self.periods,
            "source_cutoff": self.source_cutoff.isoformat() if self.source_cutoff else None,
        }


class IndicatorAggregationService:
    """Computes and materialises indicator values."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Encounter-domain counts ------------------------------------------
    def _encounter_base(self, facility_id: uuid.UUID, start: date, end: date) -> Select[tuple[int]]:
        """Distinct encounters for a facility and period.

        Distinct because the test join fans out: an encounter with two test
        rows would otherwise count twice, and the inflation is invisible in a
        total.
        """
        return (
            select(func.count(func.distinct(OpdEncounter.id)))
            .select_from(OpdEncounter)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                OpdEncounter.facility_id == facility_id,
                OpdEncounter.encounter_date >= start,
                OpdEncounter.encounter_date <= end,
            )
        )

    def count_attendances(self, facility_id: uuid.UUID, start: date, end: date) -> int:
        return int(
            self._session.execute(self._encounter_base(facility_id, start, end)).scalar_one()
        )

    def count_suspected(self, facility_id: uuid.UUID, start: date, end: date) -> int:
        query = self._encounter_base(facility_id, start, end).where(
            OpdEncounter.fever_present == FeverStatus.YES
        )
        return int(self._session.execute(query).scalar_one())

    def count_tested(self, facility_id: uuid.UUID, start: date, end: date) -> int:
        """Encounters where a test was actually performed.

        ``not_done`` is excluded. A denominator inflated by untested
        attendances understates positivity everywhere, and does so worst where
        testing has broken down - which is exactly where the figure matters.
        """
        query = self._encounter_base(facility_id, start, end).where(
            OpdEncounterTest.method != MalariaTestMethod.NOT_DONE
        )
        return int(self._session.execute(query).scalar_one())

    def count_confirmed(self, facility_id: uuid.UUID, start: date, end: date) -> int:
        query = self._encounter_base(facility_id, start, end).where(
            OpdEncounterTest.result == MalariaTestResult.POSITIVE
        )
        return int(self._session.execute(query).scalar_one())

    def count_antimalarial_treated(self, facility_id: uuid.UUID, start: date, end: date) -> int:
        """Encounters carrying at least one antimalarial line.

        Matched on the normalised drug name against the artemisinin-based
        families the national guideline uses. Deliberately narrow: a broad
        substring match would count an antipyretic as treatment.
        """
        query = (
            select(func.count(func.distinct(OpdEncounter.id)))
            .select_from(OpdEncounter)
            .join(
                OpdEncounterPrescription,
                OpdEncounterPrescription.opd_encounter_id == OpdEncounter.id,
            )
            .where(
                OpdEncounter.facility_id == facility_id,
                OpdEncounter.encounter_date >= start,
                OpdEncounter.encounter_date <= end,
                OpdEncounterPrescription.drug_name_normalised.is_not(None),
                func.lower(OpdEncounterPrescription.drug_name_normalised).regexp_replace(
                    r".*(artemether|artesunate|dihydroartemisinin|amodiaquine|lumefantrine"
                    r"|piperaquine|sulfadoxine).*",
                    "MATCH",
                )
                == "MATCH",
            )
        )
        return int(self._session.execute(query).scalar_one())

    # -- Aggregate-domain sums ---------------------------------------------
    def _accepted_submission_ids(
        self, facility_id: uuid.UUID, start: date, end: date, form: str
    ) -> list[uuid.UUID]:
        """Accepted submissions only.

        A superseded revision is history. Summing every revision would count a
        corrected month twice, and the correction is exactly the case where
        someone is already looking closely at the number.
        """
        return list(
            self._session.execute(
                select(AggregateSubmission.id).where(
                    AggregateSubmission.facility_id == facility_id,
                    AggregateSubmission.form == form,
                    AggregateSubmission.period_start >= start,
                    AggregateSubmission.period_end <= end,
                    AggregateSubmission.submission_status == AggregateSubmissionStatus.ACCEPTED,
                )
            )
            .scalars()
            .all()
        )

    def sum_aggregate_element(
        self, facility_id: uuid.UUID, start: date, end: date, *, form: str, element: str
    ) -> tuple[int | None, int]:
        """The reported total for one element, and how many cells were blank.

        Returns ``(None, blanks)`` when **every** contributing cell was blank:
        a facility that did not answer has not reported none, and summing
        blanks as zero turns a reporting gap into a real-looking figure.
        """
        submission_ids = self._accepted_submission_ids(facility_id, start, end, form)
        if not submission_ids:
            return None, 0

        total, reported, blanks = self._session.execute(
            select(
                func.sum(AggregateObservation.value),
                func.count(AggregateObservation.value),
                func.count().filter(AggregateObservation.value.is_(None)),
            ).where(
                AggregateObservation.aggregate_submission_id.in_(submission_ids),
                AggregateObservation.element_code == element,
            )
        ).one()

        if not reported:
            return None, int(blanks or 0)
        return int(total), int(blanks or 0)

    def sum_commodity_metric(
        self, facility_id: uuid.UUID, start: date, end: date, *, commodity: str, metric: str
    ) -> tuple[Decimal | None, int]:
        submission_ids = self._accepted_submission_ids(facility_id, start, end, "hmis_105")
        if not submission_ids:
            return None, 0

        total, reported, blanks = self._session.execute(
            select(
                func.sum(CommodityStockObservation.value),
                func.count(CommodityStockObservation.value),
                func.count().filter(CommodityStockObservation.value.is_(None)),
            ).where(
                CommodityStockObservation.aggregate_submission_id.in_(submission_ids),
                CommodityStockObservation.commodity_code == commodity,
                CommodityStockObservation.metric == metric,
            )
        ).one()

        if not reported:
            return None, int(blanks or 0)
        return Decimal(total), int(blanks or 0)

    def sum_laboratory_done(
        self, facility_id: uuid.UUID, start: date, end: date, tests: Sequence[str]
    ) -> tuple[int | None, int]:
        submission_ids = self._accepted_submission_ids(facility_id, start, end, "hmis_105")
        if not submission_ids:
            return None, 0

        total, reported, blanks = self._session.execute(
            select(
                func.sum(LaboratoryTestObservation.number_done),
                func.count(LaboratoryTestObservation.number_done),
                func.count().filter(LaboratoryTestObservation.number_done.is_(None)),
            ).where(
                LaboratoryTestObservation.aggregate_submission_id.in_(submission_ids),
                LaboratoryTestObservation.test_code.in_(list(tests)),
            )
        ).one()

        if not reported:
            return None, int(blanks or 0)
        return int(total), int(blanks or 0)

    # -- Proportions --------------------------------------------------------
    @staticmethod
    def proportion(numerator: int | None, denominator: int | None) -> ComputedValue:
        """A proportion, or an explicit statement that there is none.

        Zero and null denominators both yield no value. A facility that tested
        nobody has no positivity - not a positivity of zero - and reporting the
        latter would put a real-looking 0% into every district average.
        """
        if denominator is None or denominator == 0 or numerator is None:
            return ComputedValue(
                numerator=numerator,
                denominator=denominator,
                value=None,
                status=IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR,
                quality={
                    "reason": (
                        "no denominator: the value is undefined, which is not the same as zero"
                    )
                },
            )
        return ComputedValue(
            numerator=numerator,
            denominator=denominator,
            # Six decimal places, matching the column. Stored as a fraction;
            # presentation multiplies.
            value=(Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")),
            status=IndicatorValueStatus.AVAILABLE,
        )

    @staticmethod
    def count_value(total: int | None, *, missing_inputs: int = 0) -> ComputedValue:
        if total is None:
            return ComputedValue(
                numerator=None,
                denominator=None,
                value=None,
                status=IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                missing_inputs=missing_inputs,
                quality={
                    "reason": (
                        "every contributing cell was blank; the facility did not "
                        "report, which is not the same as reporting none"
                    )
                },
            )
        return ComputedValue(
            numerator=total,
            denominator=None,
            value=Decimal(total),
            status=IndicatorValueStatus.AVAILABLE,
            missing_inputs=missing_inputs,
        )

    # -- Rollup -------------------------------------------------------------
    def roll_up(
        self,
        facility_values: dict[uuid.UUID, ComputedValue],
        *,
        unit: IndicatorUnit,
        expected_units: int | None = None,
    ) -> ComputedValue:
        """Combine facility figures into one higher-grain figure.

        Counts sum. Proportions are **recomputed** from summed numerators and
        denominators, never averaged: averaging facility proportions weights a
        clinic that tested four people the same as a hospital that tested four
        hundred, and the resulting district figure is a number no facility
        reported and nobody can reproduce.
        """
        available = [
            v for v in facility_values.values() if v.status is IndicatorValueStatus.AVAILABLE
        ]
        contributing = len(available)
        missing = sum(v.missing_inputs or 0 for v in facility_values.values())

        if not available:
            return ComputedValue(
                numerator=None,
                denominator=None,
                value=None,
                status=IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                contributing_units=0,
                expected_units=expected_units,
                missing_inputs=missing,
                quality={"reason": "no contributing unit produced a value"},
            )

        numerator = sum(v.numerator or 0 for v in available)

        if unit is IndicatorUnit.PROPORTION:
            denominator = sum(v.denominator or 0 for v in available)
            rolled = self.proportion(numerator, denominator)
        else:
            rolled = self.count_value(numerator)

        rolled.contributing_units = contributing
        rolled.expected_units = expected_units
        rolled.missing_inputs = missing
        if expected_units and contributing < expected_units:
            # Carried on the result rather than left for a reader to notice.
            # A total from four of forty facilities is not a small total.
            rolled.quality = {
                **rolled.quality,
                "partial_reporting": (
                    f"{contributing} of {expected_units} units contributed; the "
                    "figure is not comparable with a period in which more did"
                ),
            }
        return rolled

    # -- Materialisation ----------------------------------------------------
    def materialise(
        self,
        version: IndicatorDefinitionVersion,
        code: str,
        *,
        grain: GeographyGrain,
        period_start: date,
        period_end: date,
        period_grain: PeriodGrain,
        computed: ComputedValue,
        geography_unit_id: uuid.UUID | None = None,
        facility_id: uuid.UUID | None = None,
        boundary_version_id: uuid.UUID | None = None,
        source_cutoff: datetime | None = None,
        input_fingerprint: str | None = None,
        age_band: AgeBand = AgeBand.UNSPECIFIED,
        sex: Sex = Sex.UNKNOWN,
    ) -> tuple[IndicatorResult, bool]:
        """Write one result, or recognise that it is already there.

        Returns ``(result, created)``. Idempotent by construction: the
        uniqueness key includes the input fingerprint, so recomputing over
        unchanged inputs finds the existing row, while changed inputs produce a
        new row beside it rather than overwriting a figure someone acted on.
        """
        cutoff = source_cutoff or datetime.now(UTC)
        fingerprint = input_fingerprint or fingerprint_of(
            code=code,
            version_checksum=version.specification_checksum,
            grain=grain,
            geography_unit_id=geography_unit_id,
            facility_id=facility_id,
            period_start=period_start,
            period_end=period_end,
            numerator=computed.numerator,
            denominator=computed.denominator,
        )

        existing = self._session.execute(
            select(IndicatorResult).where(
                IndicatorResult.indicator_version_id == version.id,
                IndicatorResult.geography_grain == grain,
                IndicatorResult.geography_unit_id.is_(geography_unit_id)
                if geography_unit_id is None
                else IndicatorResult.geography_unit_id == geography_unit_id,
                IndicatorResult.facility_id.is_(facility_id)
                if facility_id is None
                else IndicatorResult.facility_id == facility_id,
                IndicatorResult.period_start == period_start,
                IndicatorResult.age_band == age_band,
                IndicatorResult.sex == sex,
                IndicatorResult.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        result = IndicatorResult(
            indicator_version_id=version.id,
            indicator_code=code,
            geography_grain=grain,
            geography_unit_id=geography_unit_id,
            facility_id=facility_id,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            age_band=age_band,
            sex=sex,
            numerator=computed.numerator,
            denominator=computed.denominator,
            value=computed.value,
            value_status=computed.status,
            input_fingerprint=fingerprint,
            source_cutoff=cutoff,
            boundary_version_id=boundary_version_id,
            computed_at=datetime.now(UTC),
            engine_version=ENGINE_VERSION,
            contributing_units=computed.contributing_units,
            expected_units=computed.expected_units,
            missing_inputs=computed.missing_inputs,
            quality_context=computed.quality or None,
        )
        self._session.add(result)
        self._session.flush()
        return result, True

    # -- Helpers ------------------------------------------------------------
    def active_facilities(self, district_id: uuid.UUID | None = None) -> list[Facility]:
        query = select(Facility).where(Facility.is_active.is_(True))
        if district_id is not None:
            query = query.where(Facility.district_geography_unit_id == district_id)
        return list(self._session.execute(query).scalars().all())

    def latest_source_cutoff(self) -> datetime:
        """The most recent moment any source row was written.

        A figure computed before a late submission arrived is not wrong; it is
        as-of a moment, and this is that moment.
        """
        encounter = self._session.execute(
            select(func.max(OpdEncounter.updated_at))
        ).scalar_one_or_none()
        submission = self._session.execute(
            select(func.max(AggregateSubmission.updated_at))
        ).scalar_one_or_none()
        candidates = [value for value in (encounter, submission) if value is not None]
        return max(candidates) if candidates else datetime.now(UTC)


def fingerprint_of(**material: object) -> str:
    """A stable identity for one computed figure's inputs."""
    return hashlib.sha256(
        json.dumps(
            {key: str(value) for key, value in sorted(material.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ENGINE_VERSION",
    "ComputedValue",
    "IndicatorAggregationService",
    "IndicatorNotApprovedError",
    "MaterialisationReport",
    "fingerprint_of",
]
