"""Rolling facility figures up to administrative units.

Two rules carry most of the weight.

**Recompute, never average.** A district's positivity is its positives divided
by its tests, not the mean of its facilities' rates. The two differ whenever
facilities are unequal in size, and they are always unequal in size. Averaging
gives a small clinic with four tests the same voice as a hospital with four
hundred, which is how a rural district acquires an alarming rate it does not
have.

**Where care was given is not where people live.** A patient may attend a
clinic outside their own district. Rolling up by facility location points at a
clinic; rolling up by residence points at a village. Both are useful, they
answer different questions, and the two bases are stored separately so nothing
can sum them together.

Every row carries how much of the unit reported. A district figure built from
three of its twenty facilities is not a district figure, and the completeness
travels on the row so a reader cannot mistake one for the other. Encounters
whose residence never resolved are counted too, because their absence always
makes a residence map look emptier than the truth.

Nothing here maps a patient. Aggregation is to administrative units only, and
the finest unit MARS will roll up to is the one the source coded.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from mars.core.logging import get_logger
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    IndicatorValueStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PeriodGrain,
    SpatialAggregationBasis,
    SpatialRunStatus,
    TestingMeasure,
)
from mars.domain.indicator import IndicatorResult
from mars.domain.organisation import Facility
from mars.domain.spatial import GeographicAggregationResult, SpatialRun
from mars.domain.surveillance import TestingSurveillanceResult, TreatmentSurveillanceResult

logger = get_logger(__name__)

#: Bumped when a change here could alter a rolled-up figure.
ENGINE_VERSION = "1.0.0"

#: The measures MARS can count directly from encounter residence. Others are
#: facility-level constructions - a facility's missing-prescription count has
#: no residence - and are produced on the facility-location basis only.
RESIDENCE_MEASURES = (TestingMeasure.TESTING_COVERAGE, TestingMeasure.TEST_POSITIVITY)


@dataclass(slots=True)
class AggregationReport:
    """What one aggregation run did."""

    run_id: uuid.UUID | None = None
    status: SpatialRunStatus = SpatialRunStatus.RUNNING
    units_examined: int = 0
    results_written: int = 0
    unavailable: int = 0
    unresolved_contributions: int = 0
    measures_not_available_on_this_basis: list[str] = field(default_factory=list)
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id) if self.run_id else None,
            "status": self.status.value,
            "units_examined": self.units_examined,
            "results_written": self.results_written,
            "unavailable": self.unavailable,
            "unresolved_contributions": self.unresolved_contributions,
            "measures_not_available_on_this_basis": sorted(
                self.measures_not_available_on_this_basis
            ),
            "notes": self.notes,
        }


@dataclass(slots=True)
class Part:
    """One unit's running totals while a roll-up is assembled."""

    numerator: int = 0
    denominator: int = 0
    contributing: set[uuid.UUID] = field(default_factory=set)
    any_denominator: bool = False


def _fingerprint(**material: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {k: str(v) for k, v in sorted(material.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class GeographicAggregationEngine:
    """Rolls facility results up to districts and subcounties."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Geography ----------------------------------------------------------
    def _facility_unit_column(
        self, grain: GeographyGrain
    ) -> InstrumentedAttribute[uuid.UUID | None]:
        if grain is GeographyGrain.SUBCOUNTY:
            return Facility.subcounty_geography_unit_id
        return Facility.district_geography_unit_id

    def _residence_column(self, grain: GeographyGrain) -> InstrumentedAttribute[uuid.UUID | None]:
        if grain is GeographyGrain.SUBCOUNTY:
            return OpdEncounter.residence_subcounty_id
        return OpdEncounter.residence_district_id

    def expected_facilities(self, grain: GeographyGrain) -> dict[uuid.UUID, int]:
        """How many active facilities each unit has.

        The denominator of reporting completeness. Counted from the facility
        register rather than from who reported, because the facilities that did
        not report are exactly the ones a completeness figure exists to reveal.
        """
        column = self._facility_unit_column(grain)
        rows = self._session.execute(
            select(column, func.count(Facility.id))
            .where(Facility.is_active.is_(True), column.is_not(None))
            .group_by(column)
        ).all()
        return {unit_id: count for unit_id, count in rows if unit_id is not None}

    # -- Source reading -----------------------------------------------------
    def _facility_series_query(
        self, series_kind: BaselineSeriesKind, period_start: date
    ) -> Select[tuple]:
        if series_kind is BaselineSeriesKind.INDICATOR:
            return select(
                IndicatorResult.indicator_code,
                IndicatorResult.facility_id,
                IndicatorResult.numerator,
                IndicatorResult.denominator,
                IndicatorResult.value_status,
                IndicatorResult.computed_at,
                IndicatorResult.period_end,
            ).where(
                IndicatorResult.period_start == period_start,
                IndicatorResult.geography_grain == GeographyGrain.FACILITY,
            )
        if series_kind is BaselineSeriesKind.TESTING_MEASURE:
            return select(
                TestingSurveillanceResult.measure,
                TestingSurveillanceResult.facility_id,
                TestingSurveillanceResult.numerator,
                TestingSurveillanceResult.denominator,
                TestingSurveillanceResult.value_status,
                TestingSurveillanceResult.computed_at,
                TestingSurveillanceResult.period_end,
            ).where(
                TestingSurveillanceResult.period_start == period_start,
                TestingSurveillanceResult.geography_grain == GeographyGrain.FACILITY,
            )
        return select(
            TreatmentSurveillanceResult.measure,
            TreatmentSurveillanceResult.facility_id,
            TreatmentSurveillanceResult.numerator,
            TreatmentSurveillanceResult.denominator,
            TreatmentSurveillanceResult.value_status,
            TreatmentSurveillanceResult.computed_at,
            TreatmentSurveillanceResult.period_end,
        ).where(
            TreatmentSurveillanceResult.period_start == period_start,
            TreatmentSurveillanceResult.geography_grain == GeographyGrain.FACILITY,
        )

    def _facility_units(self, grain: GeographyGrain) -> dict[uuid.UUID, uuid.UUID]:
        column = self._facility_unit_column(grain)
        rows = self._session.execute(
            select(Facility.id, column).where(Facility.is_active.is_(True))
        ).all()
        return {facility_id: unit_id for facility_id, unit_id in rows if unit_id is not None}

    # -- Running ------------------------------------------------------------
    def aggregate(
        self,
        period_start: date,
        period_end: date,
        *,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain = GeographyGrain.DISTRICT,
        basis: SpatialAggregationBasis = SpatialAggregationBasis.FACILITY_LOCATION,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
        boundary_version_id: uuid.UUID | None = None,
    ) -> AggregationReport:
        """Roll one period's figures up to one administrative grain."""
        if geography_grain is GeographyGrain.FACILITY:
            raise ValueError("a facility is not an aggregate; choose subcounty or above")

        started = datetime.now(UTC)
        run = SpatialRun(
            run_kind="aggregation",
            run_status=SpatialRunStatus.RUNNING,
            series_kind=series_kind,
            aggregation_basis=basis,
            geography_grain=geography_grain,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            boundary_version_id=boundary_version_id,
            engine_version=ENGINE_VERSION,
            started_at=started,
        )
        self._session.add(run)
        self._session.flush()

        report = AggregationReport(run_id=run.id, status=SpatialRunStatus.RUNNING)

        if basis is SpatialAggregationBasis.RESIDENCE:
            self._aggregate_by_residence(
                run=run,
                report=report,
                period_start=period_start,
                period_end=period_end,
                series_kind=series_kind,
                geography_grain=geography_grain,
                period_grain=period_grain,
                boundary_version_id=boundary_version_id,
            )
        else:
            self._aggregate_by_facility_location(
                run=run,
                report=report,
                period_start=period_start,
                period_end=period_end,
                series_kind=series_kind,
                geography_grain=geography_grain,
                period_grain=period_grain,
                boundary_version_id=boundary_version_id,
            )

        run.run_status = SpatialRunStatus.COMPLETED
        run.units_examined = report.units_examined
        run.results_written = report.results_written
        run.not_evaluated = report.unavailable
        run.finished_at = datetime.now(UTC)
        run.notes = report.notes
        report.status = SpatialRunStatus.COMPLETED
        self._session.flush()
        logger.info("geographic_aggregation_finished", **report.as_dict())
        return report

    # -- Facility-location basis -------------------------------------------
    def _aggregate_by_facility_location(
        self,
        *,
        run: SpatialRun,
        report: AggregationReport,
        period_start: date,
        period_end: date,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain,
        period_grain: PeriodGrain,
        boundary_version_id: uuid.UUID | None,
    ) -> None:
        units = self._facility_units(geography_grain)
        expected = self.expected_facilities(geography_grain)
        rows = self._session.execute(self._facility_series_query(series_kind, period_start)).all()

        # One period can hold several rows per facility and series; the latest
        # is the one in force. Summing all of them would count a corrected
        # figure and the figure it corrected.
        latest: dict[tuple[str, uuid.UUID], tuple[datetime, int | None, int | None, object]] = {}
        source_cutoff = run.started_at
        for key, facility_id, numerator, denominator, status, computed_at, _ in rows:
            if facility_id is None:
                continue
            series_key = key.value if hasattr(key, "value") else str(key)
            seen = latest.get((series_key, facility_id))
            if seen is not None and seen[0] >= computed_at:
                continue
            latest[(series_key, facility_id)] = (computed_at, numerator, denominator, status)
            source_cutoff = max(source_cutoff, computed_at)

        parts: dict[tuple[str, uuid.UUID], Part] = defaultdict(Part)
        for (series_key, facility_id), (
            _,
            numerator,
            denominator,
            status,
        ) in latest.items():
            unit_id = units.get(facility_id)
            if unit_id is None:
                # A facility with no unit at this grain contributes to nothing.
                # Counted, so a district total that looks small can be
                # explained rather than trusted.
                report.unresolved_contributions += 1
                continue
            part = parts[(series_key, unit_id)]
            part.contributing.add(facility_id)
            if numerator is not None:
                part.numerator += numerator
            if denominator is not None:
                part.denominator += denominator
                part.any_denominator = True
            if status is IndicatorValueStatus.AVAILABLE and denominator is None:
                # A count measure, which sums but has no rate.
                part.any_denominator = part.any_denominator or False

        for (series_key, unit_id), part in parts.items():
            self._write(
                run=run,
                report=report,
                series_kind=series_kind,
                series_key=series_key,
                unit_id=unit_id,
                geography_grain=geography_grain,
                basis=SpatialAggregationBasis.FACILITY_LOCATION,
                period_start=period_start,
                period_end=period_end,
                period_grain=period_grain,
                boundary_version_id=boundary_version_id,
                numerator=part.numerator,
                denominator=part.denominator if part.any_denominator else None,
                contributing=len(part.contributing),
                expected=expected.get(unit_id, len(part.contributing)),
                unresolved=None,
                source_cutoff=source_cutoff,
            )

    # -- Residence basis ----------------------------------------------------
    def _aggregate_by_residence(
        self,
        *,
        run: SpatialRun,
        report: AggregationReport,
        period_start: date,
        period_end: date,
        series_kind: BaselineSeriesKind,
        geography_grain: GeographyGrain,
        period_grain: PeriodGrain,
        boundary_version_id: uuid.UUID | None,
    ) -> None:
        """Count encounters by where the patient lives.

        Recomputed from encounters rather than rolled up from facility figures,
        because a facility's figures carry no residence. Only the measures that
        are countable this way are produced; the rest are named in the report
        rather than silently omitted.
        """
        if series_kind is not BaselineSeriesKind.TESTING_MEASURE:
            report.notes = (
                "Residence aggregation is computed from encounters, which carry "
                "a residence. Only testing measures are countable this way; "
                "indicator and treatment series are produced on the "
                "facility-location basis."
            )
            report.measures_not_available_on_this_basis = [series_kind.value]
            return

        column = self._residence_column(geography_grain)
        base = (
            select(
                column.label("unit_id"),
                func.count(func.distinct(OpdEncounter.id)).label("total"),
            )
            .select_from(OpdEncounter)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                OpdEncounter.encounter_date >= period_start,
                OpdEncounter.encounter_date <= period_end,
            )
            .group_by(column)
        )

        def counts(query: Select[tuple]) -> dict[uuid.UUID | None, int]:
            return {row[0]: int(row[1]) for row in self._session.execute(query).all()}

        attendances = counts(base)
        tested = counts(base.where(OpdEncounterTest.method != MalariaTestMethod.NOT_DONE))
        positive = counts(base.where(OpdEncounterTest.result == MalariaTestResult.POSITIVE))

        # Encounters whose residence never resolved. Their absence always makes
        # a residence map look emptier than the truth, so it is reported.
        unresolved = int(attendances.pop(None, 0))
        tested.pop(None, None)
        positive.pop(None, None)
        report.unresolved_contributions = unresolved

        source_cutoff = self._session.execute(
            select(func.max(OpdEncounter.updated_at)).where(
                OpdEncounter.encounter_date >= period_start,
                OpdEncounter.encounter_date <= period_end,
            )
        ).scalar_one_or_none()

        expected = self.expected_facilities(geography_grain)
        for unit_id in [key for key in attendances if key is not None]:
            for measure in RESIDENCE_MEASURES:
                if measure is TestingMeasure.TESTING_COVERAGE:
                    numerator, denominator = tested.get(unit_id, 0), attendances[unit_id]
                else:
                    numerator, denominator = positive.get(unit_id, 0), tested.get(unit_id, 0)
                self._write(
                    run=run,
                    report=report,
                    series_kind=series_kind,
                    series_key=measure.value,
                    unit_id=unit_id,
                    geography_grain=geography_grain,
                    basis=SpatialAggregationBasis.RESIDENCE,
                    period_start=period_start,
                    period_end=period_end,
                    period_grain=period_grain,
                    boundary_version_id=boundary_version_id,
                    numerator=numerator,
                    denominator=denominator,
                    # Residence figures are not built facility by facility, so
                    # facility completeness does not describe them. Recorded as
                    # the unit's facility count for context only.
                    contributing=0,
                    expected=expected.get(unit_id, 0),
                    unresolved=unresolved,
                    source_cutoff=source_cutoff or run.started_at,
                )

    # -- Writing ------------------------------------------------------------
    def _write(
        self,
        *,
        run: SpatialRun,
        report: AggregationReport,
        series_kind: BaselineSeriesKind,
        series_key: str,
        unit_id: uuid.UUID,
        geography_grain: GeographyGrain,
        basis: SpatialAggregationBasis,
        period_start: date,
        period_end: date,
        period_grain: PeriodGrain,
        boundary_version_id: uuid.UUID | None,
        numerator: int | None,
        denominator: int | None,
        contributing: int,
        expected: int,
        unresolved: int | None,
        source_cutoff: datetime,
    ) -> GeographicAggregationResult:
        report.units_examined += 1

        # Recomputed from the parts. Never the mean of the facility rates.
        if denominator is None or denominator == 0 or numerator is None:
            value: Decimal | None = None
            status = IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR
        else:
            value = (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001"))
            status = IndicatorValueStatus.AVAILABLE

        completeness = (
            (Decimal(contributing) / Decimal(expected)).quantize(Decimal("0.0001"))
            if expected
            else None
        )

        result = GeographicAggregationResult(
            spatial_run_id=run.id,
            series_kind=series_kind,
            series_key=series_key,
            geography_grain=geography_grain,
            geography_unit_id=unit_id,
            boundary_version_id=boundary_version_id,
            aggregation_basis=basis,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            numerator=numerator,
            denominator=denominator,
            value=value,
            value_status=status,
            contributing_facilities=contributing,
            expected_facilities=max(expected, contributing),
            reporting_completeness=completeness,
            unresolved_contributions=unresolved,
            input_fingerprint=_fingerprint(
                series=series_key,
                kind=series_kind.value,
                unit=unit_id,
                basis=basis.value,
                period=period_start,
                numerator=numerator,
                denominator=denominator,
                contributing=contributing,
            ),
            source_cutoff=source_cutoff,
            engine_version=ENGINE_VERSION,
            computed_at=datetime.now(UTC),
            quality_context={
                "method": (
                    "Recomputed from the numerators and denominators underneath. "
                    "Not the mean of the facility values, which would give a "
                    "four-test clinic the same weight as a four-hundred-test "
                    "hospital."
                ),
                "basis_limit": (
                    "Counted by where people live. A facility concentration and "
                    "a residence concentration point at different things."
                    if basis is SpatialAggregationBasis.RESIDENCE
                    else "Counted by where care was given, not where patients live."
                ),
            },
        )
        self._session.add(result)
        self._session.flush()
        report.results_written += 1
        if status is not IndicatorValueStatus.AVAILABLE:
            report.unavailable += 1
        return result


__all__ = [
    "ENGINE_VERSION",
    "RESIDENCE_MEASURES",
    "AggregationReport",
    "GeographicAggregationEngine",
]
