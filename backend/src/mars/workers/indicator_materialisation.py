"""The indicator materialisation job.

Recomputes indicator values for a period and writes any that are new. Safe to
run repeatedly: the result table's uniqueness key includes the input
fingerprint, so a run over unchanged data writes nothing and a run after a
correction writes a new row beside the old one.

**It computes nothing for a definition the programme has not approved.** That
is not a failure mode to work around - an unapproved definition is a proposal,
and publishing a figure computed by rules nobody signed would be worse than
publishing none.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.analytics.aggregation import ComputedValue, IndicatorAggregationService
from mars.analytics.indicator_registry import IndicatorRegistryService
from mars.core.logging import get_logger
from mars.domain.enums import GeographyGrain, GeographyLevel, IndicatorUnit, PeriodGrain
from mars.domain.geography import GeographyUnit
from mars.domain.indicator import IndicatorDefinitionVersion
from mars.domain.organisation import Facility

logger = get_logger(__name__)

JOB_NAME = "indicator.materialise"

#: Codes this job computes directly from encounters. Each maps to a counting
#: method on the aggregation service; adding one is a deliberate act, because
#: a code with no method would silently produce nothing.
_ENCOUNTER_COUNTS = {
    "ENC_ATTENDANCE_TOTAL": "count_attendances",
    "ENC_SUSPECTED_MALARIA": "count_suspected",
    "ENC_TESTED_MALARIA": "count_tested",
    "ENC_CONFIRMED_MALARIA": "count_confirmed",
    "ENC_ANTIMALARIAL_TREATED": "count_antimalarial_treated",
}


@dataclass(slots=True)
class JobReport:
    """What one materialisation run did."""

    written: int = 0
    unchanged: int = 0
    skipped_unapproved: list[str] = field(default_factory=list)
    facilities: int = 0
    rolled_up: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "written": self.written,
            "unchanged": self.unchanged,
            "rolled_up": self.rolled_up,
            "skipped_unapproved": sorted(self.skipped_unapproved),
            "facilities": self.facilities,
        }


def materialise_period(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    district_id: uuid.UUID | None = None,
) -> JobReport:
    """Compute and store indicator values for one period.

    Facility figures are written first. The same run then rolls them up to
    district grain and, when every facility was in scope, to national grain:
    the overview reads those grains and never sums facility rows itself.
    """
    registry = IndicatorRegistryService(session)
    engine = IndicatorAggregationService(session)
    report = JobReport()

    active = registry.active_versions()
    cutoff = engine.latest_source_cutoff()
    facilities = engine.active_facilities(district_id)
    report.facilities = len(facilities)
    computed: dict[str, dict[uuid.UUID, ComputedValue]] = {}

    for code, method_name in _ENCOUNTER_COUNTS.items():
        version = active.get(code)
        if version is None:
            # Registered but not approved. Recorded so an operator can see
            # exactly which definitions are waiting on the programme.
            report.skipped_unapproved.append(code)
            continue

        counter = getattr(engine, method_name)
        values = computed.setdefault(code, {})
        for facility in facilities:
            values[facility.id] = engine.count_value(counter(facility.id, period_start, period_end))
            _write(
                engine,
                report,
                version,
                code,
                values[facility.id],
                grain=GeographyGrain.FACILITY,
                period=(period_start, period_end, period_grain),
                facility_id=facility.id,
                source_cutoff=cutoff,
            )

    # Positivity is derived from two other indicators rather than counted, so
    # it is computed after them and only when both are approved.
    positivity = active.get("ENC_TEST_POSITIVITY")
    if positivity is None:
        report.skipped_unapproved.append("ENC_TEST_POSITIVITY")
    elif "ENC_CONFIRMED_MALARIA" in active and "ENC_TESTED_MALARIA" in active:
        values = computed.setdefault("ENC_TEST_POSITIVITY", {})
        for facility in facilities:
            values[facility.id] = engine.proportion(
                engine.count_confirmed(facility.id, period_start, period_end),
                engine.count_tested(facility.id, period_start, period_end),
            )
            _write(
                engine,
                report,
                positivity,
                "ENC_TEST_POSITIVITY",
                values[facility.id],
                grain=GeographyGrain.FACILITY,
                period=(period_start, period_end, period_grain),
                facility_id=facility.id,
                source_cutoff=cutoff,
            )

    _roll_up(
        session,
        engine,
        report,
        active,
        computed,
        facilities,
        period=(period_start, period_end, period_grain),
        source_cutoff=cutoff,
        include_national=district_id is None,
    )

    session.flush()
    logger.info("indicator_materialisation_finished", **report.as_dict())
    return report


def _roll_up(
    session: Session,
    engine: IndicatorAggregationService,
    report: JobReport,
    active: dict[str, IndicatorDefinitionVersion],
    computed: dict[str, dict[uuid.UUID, ComputedValue]],
    facilities: list[Facility],
    *,
    period: tuple[date, date, PeriodGrain],
    source_cutoff: datetime,
    include_national: bool,
) -> None:
    """Roll every facility figure computed in this run up to district and national grain.

    Each code is rolled up from its own facility values. Proportions are
    recomputed from summed parts by ``roll_up``, never averaged, and a facility
    that produced no value is counted as missing rather than as zero.
    """
    by_district: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for facility in facilities:
        if facility.district_geography_unit_id is not None:
            by_district[facility.district_geography_unit_id].append(facility.id)

    districts = {
        unit.id: unit
        for unit in session.execute(
            select(GeographyUnit).where(GeographyUnit.id.in_(list(by_district)))
        ).scalars()
    }
    country = (
        session.execute(
            select(GeographyUnit)
            .where(
                GeographyUnit.level == GeographyLevel.COUNTRY,
                GeographyUnit.is_active.is_(True),
            )
            .order_by(GeographyUnit.created_at)
        )
        .scalars()
        .first()
        if include_national
        else None
    )

    for code, values in computed.items():
        version = active[code]
        unit = IndicatorUnit.PROPORTION if code == "ENC_TEST_POSITIVITY" else IndicatorUnit.COUNT
        for district_id, facility_ids in by_district.items():
            district = districts.get(district_id)
            rolled = engine.roll_up(
                {facility_id: values[facility_id] for facility_id in facility_ids},
                unit=unit,
                expected_units=len(facility_ids),
            )
            report.rolled_up += _write(
                engine,
                report,
                version,
                code,
                rolled,
                grain=GeographyGrain.DISTRICT,
                period=period,
                geography_unit_id=district_id,
                boundary_version_id=district.boundary_version_id if district else None,
                source_cutoff=source_cutoff,
            )
        if include_national and values:
            rolled = engine.roll_up(values, unit=unit, expected_units=len(values))
            report.rolled_up += _write(
                engine,
                report,
                version,
                code,
                rolled,
                grain=GeographyGrain.NATIONAL,
                period=period,
                geography_unit_id=country.id if country else None,
                boundary_version_id=country.boundary_version_id if country else None,
                source_cutoff=source_cutoff,
            )


def _write(
    engine: IndicatorAggregationService,
    report: JobReport,
    version: IndicatorDefinitionVersion,
    code: str,
    value: ComputedValue,
    *,
    grain: GeographyGrain,
    period: tuple[date, date, PeriodGrain],
    source_cutoff: datetime,
    facility_id: uuid.UUID | None = None,
    geography_unit_id: uuid.UUID | None = None,
    boundary_version_id: uuid.UUID | None = None,
) -> int:
    """Write one figure and count it. Returns 1 when a new row was written."""
    start, end, period_grain = period
    _result, created = engine.materialise(
        version,
        code,
        grain=grain,
        period_start=start,
        period_end=end,
        period_grain=period_grain,
        computed=value,
        facility_id=facility_id,
        geography_unit_id=geography_unit_id,
        boundary_version_id=boundary_version_id,
        source_cutoff=source_cutoff,
    )
    if created:
        report.written += 1
    else:
        report.unchanged += 1
    return int(created)


__all__ = ["JOB_NAME", "JobReport", "materialise_period"]
