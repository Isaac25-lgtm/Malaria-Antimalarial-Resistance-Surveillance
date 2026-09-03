"""The composed reads behind the national command centre — Prompt 23.

Every figure a screen shows is assembled here, not in the browser. That is the
whole point of this module: a KPI computed in the frontend has no governed
definition, no method version, no period and no provenance, and once one exists
nobody can say afterwards what the number meant. So the API returns *records*,
not numbers - each carries its source, its period, its scope, the method
version that produced it, and an availability status that is allowed to say
"this is not configured".

The KPI codes are governed indicator codes from the Prompt 13 catalogue. A
fresh deployment has no approved indicator versions, so every KPI reports
``not_configured`` and names the code whose approval is missing. That is the
correct national screen for an unconfigured system: it explains itself rather
than showing zeroes that look like an absence of malaria.

Scope is applied in SQL by :class:`AnalyticsQueryService`, so this module adds
composition and provenance and never widens what a principal may see.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mars.core.errors import (
    FieldError,
    GeographyScopeDeniedError,
    ValidationFailedError,
)
from mars.domain.enums import (
    GeographyGrain,
    IndicatorValueStatus,
    LifecycleStatus,
    SignalStatus,
)
from mars.domain.geography import GeographyUnit
from mars.domain.indicator import (
    IndicatorDefinition,
    IndicatorDefinitionVersion,
    IndicatorResult,
)
from mars.domain.organisation import Facility
from mars.domain.signal import SurveillanceSignal
from mars.domain.surveillance import CommodityOperationalAlert
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService

#: The KPI strip, in display order. Each entry names a governed indicator from
#: the Prompt 13 catalogue; none is computed here. ``ENC_REPEAT_POSITIVE_INPUT``
#: is the repeat-positive input count - the encounters that feed recurrence
#: analysis - and is deliberately not described as a recurrence finding.
KPI_INDICATORS: tuple[tuple[str, str], ...] = (
    ("ENC_ATTENDANCE_TOTAL", "Outpatient attendances"),
    ("ENC_SUSPECTED_MALARIA", "Suspected malaria"),
    ("ENC_TESTED_MALARIA", "Tested for malaria"),
    ("ENC_CONFIRMED_MALARIA", "Confirmed malaria"),
    ("ENC_REPEAT_POSITIVE_INPUT", "Repeat-positive encounters"),
    ("RPT_COMPLETENESS", "Reporting completeness"),
)

#: Not an indicator: a count of governed signal records. Kept separate so it is
#: never mistaken for a measured quantity with a numerator and denominator.
ACTIVE_SIGNALS_CODE = "ACTIVE_SIGNALS"

#: The measure a facility ranking is read against. Attendances, because it is
#: the denominator every other facility figure is read against and the one
#: whose absence most clearly marks a non-reporting facility.
CONTRIBUTION_INDICATOR = "ENC_ATTENDANCE_TOTAL"

#: What a KPI record says about itself when it has no value.
STATUS_AVAILABLE = "available"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_UNAVAILABLE = "unavailable"
STATUS_OUTSIDE_SCOPE = "outside_scope"

INTERPRETATION_BOUNDARY = (
    "Routine surveillance data identifies patterns requiring investigation. "
    "It does not confirm antimalarial resistance, treatment failure, "
    "recrudescence, or reinfection."
)


@dataclass(frozen=True, slots=True)
class Period:
    """The reporting window a screen is showing."""

    start: date
    end: date

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end}


def _validate(period_start: date, period_end: date) -> Period:
    if period_end < period_start:
        raise ValidationFailedError(
            "period_end must be on or after period_start",
            errors=[
                FieldError(
                    field="period_end",
                    message="must be on or after period_start",
                    code="period_ordered",
                )
            ],
        )
    return Period(period_start, period_end)


def _decimal(value: object) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)))


class SurveillanceSummaryService:
    """Composes the national and district read models."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._scope = AnalyticsQueryService(session)

    # -- Governance ---------------------------------------------------------
    def _definitions(self) -> dict[str, IndicatorDefinition]:
        rows = self._session.execute(select(IndicatorDefinition)).scalars().all()
        return {row.code: row for row in rows}

    def _active_version(self, definition: IndicatorDefinition | None) -> uuid.UUID | None:
        if definition is None:
            return None
        active = next((v for v in definition.versions if v.status is LifecycleStatus.ACTIVE), None)
        return active.id if active else None

    # -- KPI strip ----------------------------------------------------------
    def kpis(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_start: date,
        period_end: date,
        geography_unit_id: uuid.UUID | None = None,
        facility_id: uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        """One record per KPI, each able to say it has no value and why.

        ``facility_id`` narrows every measure to one facility's own results.
        It is not combined with ``geography_unit_id``: a facility figure and a
        district figure are different quantities, and a workspace that showed
        one under the other's heading would be the inheritance this codebase
        has spent several commits removing.
        """
        period = _validate(period_start, period_end)
        definitions = self._definitions()
        if facility_id is not None:
            grain = GeographyGrain.FACILITY
        elif geography_unit_id is not None:
            grain = GeographyGrain.DISTRICT
        else:
            grain = GeographyGrain.NATIONAL
        records = [
            self._indicator_kpi(
                principal,
                code=code,
                label=label,
                definition=definitions.get(code),
                period=period,
                grain=grain,
                geography_unit_id=geography_unit_id,
                facility_id=facility_id,
            )
            for code, label in KPI_INDICATORS
        ]
        records.append(
            self._active_signal_kpi(
                principal,
                period=period,
                geography_unit_id=geography_unit_id,
                facility_id=facility_id,
            )
        )
        return records

    def _indicator_kpi(
        self,
        principal: AuthenticatedPrincipal,
        *,
        code: str,
        label: str,
        definition: IndicatorDefinition | None,
        period: Period,
        grain: GeographyGrain,
        geography_unit_id: uuid.UUID | None,
        facility_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "code": code,
            "label": label,
            "value": None,
            "unit": None,
            "numerator": None,
            "denominator": None,
            "period": period.as_dict(),
            "geography_grain": grain.value,
            "geography_unit_id": geography_unit_id,
            "facility_id": facility_id,
            "source": f"indicator:{code}",
            "method_version_id": None,
            "source_freshness": None,
            "comparison": None,
            "status": STATUS_NOT_CONFIGURED,
            "status_detail": None,
            "missing_configuration": [],
        }

        if definition is None:
            base["status_detail"] = (
                f"The indicator {code} is not registered. The command centre "
                "shows no figure rather than computing one of its own."
            )
            base["missing_configuration"] = [f"indicator:{code}"]
            return base

        base["unit"] = definition.unit.value if definition.unit else None
        version_id = self._active_version(definition)
        if version_id is None:
            base["status_detail"] = (
                f"{definition.label} is registered but has no programme-approved "
                "version, so no figure has been computed for it."
            )
            base["missing_configuration"] = [f"indicator_version:{code}"]
            return base

        base["method_version_id"] = version_id
        rows = self._indicator_results(
            principal,
            code=code,
            version_id=version_id,
            period=period,
            grain=grain,
            geography_unit_id=geography_unit_id,
            facility_id=facility_id,
        )
        if not rows:
            base["status"] = STATUS_UNAVAILABLE
            base["status_detail"] = (
                f"{definition.label} has an approved definition, but no result "
                "has been materialised for this period and scope. That is a "
                "statement about the analytical run, not about malaria."
            )
            return base

        numerator, denominator, freshness = self._totals(rows)
        base["source_freshness"] = freshness
        base["numerator"] = numerator
        base["denominator"] = denominator

        if denominator is not None:
            if denominator == 0:
                # No denominator is no rate. It is never a rate of zero.
                base["status"] = STATUS_UNAVAILABLE
                base["status_detail"] = (
                    "The denominator for this period is zero, so there is no proportion to report."
                )
                return base
            base["value"] = _decimal(
                (Decimal(numerator or 0) / Decimal(denominator)).quantize(Decimal("0.000001"))
            )
        else:
            base["value"] = _decimal(numerator)

        base["status"] = STATUS_AVAILABLE
        base["comparison"] = self._comparison(
            principal,
            code=code,
            version_id=version_id,
            period=period,
            grain=grain,
            geography_unit_id=geography_unit_id,
            facility_id=facility_id,
            current_numerator=numerator,
            current_denominator=denominator,
        )
        return base

    def _indicator_results(
        self,
        principal: AuthenticatedPrincipal,
        *,
        code: str,
        version_id: uuid.UUID,
        period: Period,
        grain: GeographyGrain,
        geography_unit_id: uuid.UUID | None,
        facility_id: uuid.UUID | None = None,
    ) -> list[IndicatorResult]:
        statement = select(IndicatorResult).where(
            IndicatorResult.indicator_code == code,
            IndicatorResult.indicator_version_id == version_id,
            IndicatorResult.period_start >= period.start,
            IndicatorResult.period_end <= period.end,
            IndicatorResult.value_status == IndicatorValueStatus.AVAILABLE,
        )

        if facility_id is not None:
            # One facility's own rows. A facility workspace never sums the
            # district it happens to sit in.
            facilities = self._scope.facility_ids(principal)
            if facilities is not None and facility_id not in facilities:
                return []
            statement = statement.where(
                IndicatorResult.facility_id == facility_id,
                IndicatorResult.geography_grain == GeographyGrain.FACILITY,
            )
            return list(self._session.execute(statement).scalars().all())

        if principal.is_facility_restricted:
            # The rule established in 64e3e21: a facility user's district scope
            # proves only that the facility sits inside that district. Neither
            # the national nor the district KPI strip is theirs to read.
            return []

        if geography_unit_id is not None:
            statement = statement.where(IndicatorResult.geography_unit_id == geography_unit_id)
        geographies = self._scope.geography_ids(principal)
        if geographies is not None:
            statement = statement.where(IndicatorResult.geography_unit_id.in_(geographies))
        return list(self._session.execute(statement).scalars().all())

    @staticmethod
    def _totals(
        rows: list[IndicatorResult],
    ) -> tuple[int | None, int | None, datetime | None]:
        """Sum the parts, never average the rates.

        Recomputation from numerators and denominators is the same rule the
        spatial engine keeps. A national positivity is the country's positives
        over the country's tests, not the mean of district rates.
        """
        numerator = 0
        denominator = 0
        any_denominator = False
        freshness: datetime | None = None
        for row in rows:
            if row.numerator is not None:
                numerator += row.numerator
            if row.denominator is not None:
                denominator += row.denominator
                any_denominator = True
            if freshness is None or row.computed_at > freshness:
                freshness = row.computed_at
        return numerator, (denominator if any_denominator else None), freshness

    def _comparison(
        self,
        principal: AuthenticatedPrincipal,
        *,
        code: str,
        version_id: uuid.UUID,
        period: Period,
        grain: GeographyGrain,
        geography_unit_id: uuid.UUID | None,
        current_numerator: int | None,
        current_denominator: int | None,
        facility_id: uuid.UUID | None = None,
    ) -> dict[str, Any] | None:
        """The same measure over the preceding window of equal length.

        Returned as its own record with its own period. A bare arrow with no
        stated comparison period is the kind of number nobody can check.
        """
        span = (period.end - period.start).days + 1
        previous = Period(
            period.start.fromordinal(period.start.toordinal() - span),
            period.start.fromordinal(period.start.toordinal() - 1),
        )
        rows = self._indicator_results(
            principal,
            code=code,
            version_id=version_id,
            period=previous,
            grain=grain,
            geography_unit_id=geography_unit_id,
            facility_id=facility_id,
        )
        if not rows:
            return {
                "period": previous.as_dict(),
                "value": None,
                "direction": None,
                "status": STATUS_UNAVAILABLE,
                "status_detail": "No comparable figure for the preceding period.",
            }
        numerator, denominator, _ = self._totals(rows)
        if denominator is not None and denominator == 0:
            return {
                "period": previous.as_dict(),
                "value": None,
                "direction": None,
                "status": STATUS_UNAVAILABLE,
                "status_detail": "The preceding period has no denominator.",
            }
        previous_value = (
            (Decimal(numerator or 0) / Decimal(denominator)).quantize(Decimal("0.000001"))
            if denominator is not None
            else Decimal(numerator or 0)
        )
        current_value = (
            (Decimal(current_numerator or 0) / Decimal(current_denominator)).quantize(
                Decimal("0.000001")
            )
            if current_denominator
            else Decimal(current_numerator or 0)
        )
        if current_value > previous_value:
            direction = "up"
        elif current_value < previous_value:
            direction = "down"
        else:
            direction = "unchanged"
        return {
            "period": previous.as_dict(),
            "value": str(previous_value),
            "direction": direction,
            "status": STATUS_AVAILABLE,
            "status_detail": None,
        }

    def _active_signal_kpi(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period: Period,
        geography_unit_id: uuid.UUID | None,
        facility_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        statement = select(func.count(SurveillanceSignal.id)).where(
            SurveillanceSignal.signal_status == SignalStatus.ACTIVE,
            SurveillanceSignal.period_start >= period.start,
            SurveillanceSignal.period_end <= period.end,
        )
        facilities = self._scope.facility_ids(principal)
        if facility_id is not None:
            if facilities is not None and facility_id not in facilities:
                statement = statement.where(SurveillanceSignal.facility_id.in_([]))
            else:
                statement = statement.where(SurveillanceSignal.facility_id == facility_id)
        else:
            if geography_unit_id is not None:
                statement = statement.where(
                    SurveillanceSignal.geography_unit_id == geography_unit_id
                )
            geographies = self._scope.geography_ids(principal)
            if principal.is_facility_restricted:
                statement = statement.where(SurveillanceSignal.facility_id.in_(facilities or set()))
            elif geographies is not None:
                statement = statement.where(SurveillanceSignal.geography_unit_id.in_(geographies))
        count = int(self._session.execute(statement).scalar_one())
        return {
            "code": ACTIVE_SIGNALS_CODE,
            "label": "Active signals",
            "value": str(count),
            "unit": "count",
            "numerator": count,
            "denominator": None,
            "period": period.as_dict(),
            "geography_grain": (
                GeographyGrain.FACILITY.value
                if facility_id
                else GeographyGrain.DISTRICT.value
                if geography_unit_id
                else GeographyGrain.NATIONAL.value
            ),
            "geography_unit_id": geography_unit_id,
            "facility_id": facility_id,
            "source": "table:surveillance_signal",
            "method_version_id": None,
            "source_freshness": self._session.execute(
                select(func.max(SurveillanceSignal.generated_at))
            ).scalar_one_or_none(),
            "comparison": None,
            # A count of governed records is always available, including zero.
            # Zero active signals is a real answer; it is not "unconfigured".
            "status": STATUS_AVAILABLE,
            "status_detail": (
                "Signals currently active for the period and scope. A count of "
                "zero means no signal is active, which is not the same as no "
                "analysis having run."
            ),
            "missing_configuration": [],
        }

    # -- Priority districts -------------------------------------------------
    def priority_districts(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_start: date,
        period_end: date,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Districts ordered by active signal count, then commodity alerts.

        The ordering is a count, not a score: MARS has no approved way to rank
        districts against each other, so it presents what it can count and
        leaves the judgement to the reader.
        """
        period = _validate(period_start, period_end)
        if principal.is_facility_restricted:
            return []
        geographies = self._scope.geography_ids(principal)

        counted = select(
            SurveillanceSignal.geography_unit_id.label("unit_id"),
            func.count(SurveillanceSignal.id).label("signals"),
        ).where(
            SurveillanceSignal.signal_status == SignalStatus.ACTIVE,
            SurveillanceSignal.period_start >= period.start,
            SurveillanceSignal.period_end <= period.end,
            SurveillanceSignal.geography_unit_id.is_not(None),
        )
        if geographies is not None:
            counted = counted.where(SurveillanceSignal.geography_unit_id.in_(geographies))
        signal_counts = counted.group_by(SurveillanceSignal.geography_unit_id).subquery()

        rows = self._session.execute(
            select(
                GeographyUnit.id,
                GeographyUnit.preferred_code,
                GeographyUnit.raw_name,
                signal_counts.c.signals,
            )
            .join(signal_counts, signal_counts.c.unit_id == GeographyUnit.id)
            .order_by(signal_counts.c.signals.desc(), GeographyUnit.raw_name)
            .limit(limit)
        ).all()

        alerts = self._commodity_alert_counts(principal, period)
        return [
            {
                "geography_unit_id": unit_id,
                "preferred_code": code,
                "name": name,
                "active_signals": int(signals),
                "commodity_alerts": alerts.get(unit_id, 0),
                "period": period.as_dict(),
                "ordering": "active_signal_count",
                "ordering_detail": (
                    "Ordered by the number of active signals. This is a count "
                    "of records, not a governed priority score."
                ),
            }
            for unit_id, code, name, signals in rows
        ]

    def _commodity_alert_counts(
        self, principal: AuthenticatedPrincipal, period: Period
    ) -> dict[uuid.UUID, int]:
        statement = select(
            CommodityOperationalAlert.district_geography_unit_id,
            func.count(CommodityOperationalAlert.id),
        ).where(
            CommodityOperationalAlert.period_start >= period.start,
            CommodityOperationalAlert.period_end <= period.end,
            CommodityOperationalAlert.district_geography_unit_id.is_not(None),
        )
        geographies = self._scope.geography_ids(principal)
        if geographies is not None:
            statement = statement.where(
                CommodityOperationalAlert.district_geography_unit_id.in_(geographies)
            )
        statement = statement.group_by(CommodityOperationalAlert.district_geography_unit_id)
        return {
            unit_id: int(count)
            for unit_id, count in self._session.execute(statement).all()
            if unit_id is not None
        }

    # -- Facility contribution ----------------------------------------------
    def facility_contributions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        geography_unit_id: uuid.UUID,
        period_start: date,
        period_end: date,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Which facilities in a district reported, and how much they carried.

        A district figure is only as good as the facilities behind it, and the
        commonest way a district total misleads is that a large facility
        stopped reporting. This lists the contributors so that reading is
        available rather than inferred.

        Facilities that reported nothing are included with a null count. Their
        absence is the finding; dropping them from the list would hide it.
        """
        period = _validate(period_start, period_end)
        if principal.is_facility_restricted:
            # A facility account gets its own workspace, not the roster of its
            # neighbours.
            return []
        geographies = self._scope.geography_ids(principal)
        if geographies is not None and geography_unit_id not in geographies:
            raise GeographyScopeDeniedError("That district is outside your authorised scope.")

        facilities = list(
            self._session.execute(
                select(Facility.id, Facility.code, Facility.raw_name)
                .where(
                    Facility.is_active.is_(True),
                    or_(
                        Facility.district_geography_unit_id == geography_unit_id,
                        Facility.subcounty_geography_unit_id == geography_unit_id,
                    ),
                )
                .order_by(Facility.raw_name)
                .limit(limit)
            ).all()
        )
        if not facilities:
            return []

        identifiers = [row[0] for row in facilities]
        counts = {
            facility_id: (int(total), computed)
            for facility_id, total, computed in self._session.execute(
                select(
                    IndicatorResult.facility_id,
                    func.sum(IndicatorResult.numerator),
                    func.max(IndicatorResult.computed_at),
                )
                .where(
                    IndicatorResult.facility_id.in_(identifiers),
                    IndicatorResult.indicator_code == CONTRIBUTION_INDICATOR,
                    IndicatorResult.period_start >= period.start,
                    IndicatorResult.period_end <= period.end,
                    IndicatorResult.value_status == IndicatorValueStatus.AVAILABLE,
                )
                .group_by(IndicatorResult.facility_id)
            ).all()
            if total is not None
        }

        return [
            {
                "facility_id": facility_id,
                "code": code,
                "name": name,
                "period": period.as_dict(),
                "indicator_code": CONTRIBUTION_INDICATOR,
                "value": counts.get(facility_id, (None, None))[0],
                "source_freshness": counts.get(facility_id, (None, None))[1],
                "status": (STATUS_AVAILABLE if facility_id in counts else STATUS_UNAVAILABLE),
                "status_detail": (
                    None
                    if facility_id in counts
                    else (
                        "No governed result for this facility in this period. A "
                        "facility that reported nothing is a reporting fact, not "
                        "a count of zero."
                    )
                ),
            }
            for facility_id, code, name in facilities
        ]

    # -- Provenance ---------------------------------------------------------
    def provenance(
        self, principal: AuthenticatedPrincipal, *, period_start: date, period_end: date
    ) -> dict[str, Any]:
        """What the screen is built from, and how stale it is."""
        period = _validate(period_start, period_end)
        approved = int(
            self._session.execute(
                select(func.count(IndicatorDefinitionVersion.id)).where(
                    IndicatorDefinitionVersion.status == LifecycleStatus.ACTIVE
                )
            ).scalar_one()
        )
        registered = int(
            self._session.execute(select(func.count(IndicatorDefinition.id))).scalar_one()
        )
        return {
            "period": period.as_dict(),
            "indicators_registered": registered,
            "indicators_approved": approved,
            "analytics_refreshed_at": self._session.execute(
                select(func.max(IndicatorResult.computed_at))
            ).scalar_one_or_none(),
            "signals_generated_at": self._session.execute(
                select(func.max(SurveillanceSignal.generated_at))
            ).scalar_one_or_none(),
            "interpretation_boundary": INTERPRETATION_BOUNDARY,
            "analytically_configured": approved > 0,
            "configuration_detail": (
                None
                if approved > 0
                else (
                    "No indicator definition has a programme-approved version, "
                    "so no governed figure has been computed. Every measure "
                    "reports as not configured rather than as zero."
                )
            ),
        }


__all__ = [
    "ACTIVE_SIGNALS_CODE",
    "CONTRIBUTION_INDICATOR",
    "INTERPRETATION_BOUNDARY",
    "KPI_INDICATORS",
    "STATUS_AVAILABLE",
    "STATUS_NOT_CONFIGURED",
    "STATUS_OUTSIDE_SCOPE",
    "STATUS_UNAVAILABLE",
    "Period",
    "SurveillanceSummaryService",
]
