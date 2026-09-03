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

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from mars.analytics.aggregation import IndicatorAggregationService
from mars.analytics.indicator_registry import IndicatorRegistryService
from mars.core.logging import get_logger
from mars.domain.enums import GeographyGrain, IndicatorUnit, PeriodGrain

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

    def as_dict(self) -> dict[str, object]:
        return {
            "written": self.written,
            "unchanged": self.unchanged,
            "skipped_unapproved": sorted(self.skipped_unapproved),
            "facilities": self.facilities,
        }


def materialise_period(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    district_id: object | None = None,
) -> JobReport:
    """Compute and store facility-grain indicator values for one period."""
    registry = IndicatorRegistryService(session)
    engine = IndicatorAggregationService(session)
    report = JobReport()

    active = registry.active_versions()
    cutoff = engine.latest_source_cutoff()
    facilities = engine.active_facilities(district_id)  # type: ignore[arg-type]
    report.facilities = len(facilities)

    for code, method_name in _ENCOUNTER_COUNTS.items():
        version = active.get(code)
        if version is None:
            # Registered but not approved. Recorded so an operator can see
            # exactly which definitions are waiting on the programme.
            report.skipped_unapproved.append(code)
            continue

        counter = getattr(engine, method_name)
        for facility in facilities:
            computed = engine.count_value(counter(facility.id, period_start, period_end))
            _result, created = engine.materialise(
                version,
                code,
                grain=GeographyGrain.FACILITY,
                period_start=period_start,
                period_end=period_end,
                period_grain=period_grain,
                computed=computed,
                facility_id=facility.id,
                source_cutoff=cutoff,
            )
            if created:
                report.written += 1
            else:
                report.unchanged += 1

    # Positivity is derived from two other indicators rather than counted, so
    # it is computed after them and only when both are approved.
    positivity = active.get("ENC_TEST_POSITIVITY")
    if positivity is None:
        report.skipped_unapproved.append("ENC_TEST_POSITIVITY")
    elif "ENC_CONFIRMED_MALARIA" in active and "ENC_TESTED_MALARIA" in active:
        for facility in facilities:
            computed = engine.proportion(
                engine.count_confirmed(facility.id, period_start, period_end),
                engine.count_tested(facility.id, period_start, period_end),
            )
            _result, created = engine.materialise(
                positivity,
                "ENC_TEST_POSITIVITY",
                grain=GeographyGrain.FACILITY,
                period_start=period_start,
                period_end=period_end,
                period_grain=period_grain,
                computed=computed,
                facility_id=facility.id,
                source_cutoff=cutoff,
            )
            if created:
                report.written += 1
            else:
                report.unchanged += 1

    session.flush()
    logger.info("indicator_materialisation_finished", **report.as_dict())
    return report


def roll_up_district(
    session: Session,
    *,
    district_id: object,
    code: str,
    unit: IndicatorUnit,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain = PeriodGrain.MONTH,
    boundary_version_id: object | None = None,
) -> object | None:
    """Roll facility figures up to a district figure.

    Proportions are recomputed from summed parts, never averaged. Returns
    ``None`` when the definition is not approved.
    """
    registry = IndicatorRegistryService(session)
    engine = IndicatorAggregationService(session)

    version = registry.active_version(code)
    if version is None:
        return None

    facilities = engine.active_facilities(district_id)  # type: ignore[arg-type]
    per_facility = {}
    for facility in facilities:
        if unit is IndicatorUnit.PROPORTION:
            per_facility[facility.id] = engine.proportion(
                engine.count_confirmed(facility.id, period_start, period_end),
                engine.count_tested(facility.id, period_start, period_end),
            )
        else:
            per_facility[facility.id] = engine.count_value(
                engine.count_tested(facility.id, period_start, period_end)
            )

    rolled = engine.roll_up(per_facility, unit=unit, expected_units=len(facilities))
    result, _created = engine.materialise(
        version,
        code,
        grain=GeographyGrain.DISTRICT,
        period_start=period_start,
        period_end=period_end,
        period_grain=period_grain,
        computed=rolled,
        geography_unit_id=district_id,  # type: ignore[arg-type]
        boundary_version_id=boundary_version_id,  # type: ignore[arg-type]
        source_cutoff=engine.latest_source_cutoff(),
    )
    return result


__all__ = ["JOB_NAME", "JobReport", "materialise_period", "roll_up_district"]
