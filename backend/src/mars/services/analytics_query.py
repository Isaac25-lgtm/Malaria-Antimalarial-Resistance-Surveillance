"""Scope-safe read models for analytical results from Prompts 14-20."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from mars.core.errors import FieldError, ValidationFailedError
from mars.domain.anomaly import TemporalAnomalyResult
from mars.domain.baseline import BaselineResult
from mars.domain.clustering import SpatialClusterResult
from mars.domain.enums import RecurrenceScopeKind
from mars.domain.episode import EpisodeCandidate
from mars.domain.geography import GeographyUnit
from mars.domain.organisation import Facility
from mars.domain.recurrence import RecurrenceResult
from mars.domain.spatial import HotspotResult
from mars.domain.surveillance import (
    CommodityOperationalAlert,
    TestingSurveillanceResult,
    TreatmentSurveillanceResult,
)
from mars.security.principal import AuthenticatedPrincipal


class AnalyticsQueryService:
    """Return plain dictionaries after applying geography scope in SQL."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def geography_ids(self, principal: AuthenticatedPrincipal) -> set[uuid.UUID] | None:
        """All geography units inside the principal's roots, including descendants."""
        if principal.has_national_scope:
            return None
        paths = principal.scope_path_prefixes()
        if not paths:
            return set(principal.scope_unit_ids())
        predicates = [
            or_(GeographyUnit.path == path, GeographyUnit.path.like(f"{path}/%")) for path in paths
        ]
        return set(
            self._session.execute(select(GeographyUnit.id).where(or_(*predicates))).scalars()
        )

    def facility_ids(self, principal: AuthenticatedPrincipal) -> set[uuid.UUID] | None:
        """All facilities inside the principal's effective geography/facility scope."""
        if principal.is_facility_restricted:
            return set(principal.facility_scopes)
        geography_ids = self.geography_ids(principal)
        if geography_ids is None:
            return None
        if not geography_ids:
            return set()
        return set(
            self._session.execute(
                select(Facility.id).where(
                    or_(
                        Facility.district_geography_unit_id.in_(geography_ids),
                        Facility.subcounty_geography_unit_id.in_(geography_ids),
                    )
                )
            ).scalars()
        )

    @staticmethod
    def _period(
        statement: Select[Any], model: Any, start: date | None, end: date | None
    ) -> Select[Any]:
        if start is not None and end is not None and end < start:
            raise ValidationFailedError(
                "period_to must be on or after period_from",
                errors=[
                    FieldError(
                        field="period_to",
                        message="must be on or after period_from",
                        code="period_ordered",
                    )
                ],
            )
        if start is not None:
            statement = statement.where(model.period_start >= start)
        if end is not None:
            statement = statement.where(model.period_end <= end)
        return statement

    def episodes(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_from: date | None,
        period_to: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if period_from is not None and period_to is not None and period_to < period_from:
            raise ValidationFailedError(
                "period_to must be on or after period_from",
                errors=[
                    FieldError(
                        field="period_to",
                        message="must be on or after period_from",
                        code="period_ordered",
                    )
                ],
            )
        statement = select(EpisodeCandidate)
        if period_from is not None:
            statement = statement.where(EpisodeCandidate.last_encounter_date >= period_from)
        if period_to is not None:
            statement = statement.where(EpisodeCandidate.first_encounter_date <= period_to)
        facilities = self.facility_ids(principal)
        geographies = self.geography_ids(principal)
        if principal.is_facility_restricted:
            statement = statement.where(EpisodeCandidate.index_facility_id.in_(facilities or set()))
        elif facilities is not None and geographies is not None:
            statement = statement.where(
                or_(
                    EpisodeCandidate.index_facility_id.in_(facilities),
                    EpisodeCandidate.residence_district_id.in_(geographies),
                    EpisodeCandidate.residence_subcounty_id.in_(geographies),
                )
            )
        rows = self._session.execute(
            statement.order_by(EpisodeCandidate.first_encounter_date.desc()).limit(limit)
        ).scalars()
        return [
            {
                "id": row.id,
                "record_type": "episode_candidate",
                "code": row.episode_status.value,
                "geography_unit_id": row.residence_subcounty_id or row.residence_district_id,
                "facility_id": row.index_facility_id,
                "period_start": row.first_encounter_date,
                "period_end": row.last_encounter_date,
                "numerator": row.positive_encounter_count,
                "denominator": row.encounter_count,
                "value": None,
                "value_status": "candidate",
                "details": {
                    "patient_reference_id": str(row.patient_reference_id),
                    "episode_build_id": str(row.episode_build_id),
                    "episode_number": row.episode_number,
                    "span_days": row.span_days,
                    "tested_encounter_count": row.tested_encounter_count,
                    "treated_encounter_count": row.treated_encounter_count,
                    "uncertainty": row.uncertainty or {},
                },
            }
            for row in rows
        ]

    def aggregate_results(
        self,
        principal: AuthenticatedPrincipal,
        *,
        kind: str,
        period_from: date | None,
        period_to: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        dispatch: dict[str, tuple[Any, str]] = {
            "recurrence": (RecurrenceResult, "measure"),
            "testing": (TestingSurveillanceResult, "measure"),
            "treatment": (TreatmentSurveillanceResult, "measure"),
            "baseline": (BaselineResult, "series_key"),
            "anomaly": (TemporalAnomalyResult, "series_key"),
            "hotspot": (HotspotResult, "series_key"),
            "cluster": (SpatialClusterResult, "outcome"),
        }
        model, code_column = dispatch[kind]
        statement = self._period(select(model), model, period_from, period_to)
        geographies = self.geography_ids(principal)
        facilities = self.facility_ids(principal)
        if principal.is_facility_restricted:
            geographies = set()
        if kind == "recurrence":
            if principal.is_facility_restricted:
                statement = statement.where(
                    RecurrenceResult.scope_kind == RecurrenceScopeKind.FACILITY,
                    RecurrenceResult.scope_id.in_(facilities or set()),
                )
            else:
                allowed = set()
                if geographies:
                    allowed.update(geographies)
                if facilities:
                    allowed.update(facilities)
                if geographies is not None and facilities is not None:
                    statement = statement.where(RecurrenceResult.scope_id.in_(allowed))
        elif kind in {"hotspot", "cluster"}:
            if geographies is not None:
                statement = statement.where(model.geography_unit_id.in_(geographies))
        else:
            if geographies is not None and facilities is not None:
                statement = statement.where(
                    or_(
                        model.geography_unit_id.in_(geographies),
                        model.facility_id.in_(facilities),
                    )
                )
        rows = self._session.execute(
            statement.order_by(model.period_start.desc()).limit(limit)
        ).scalars()
        return [self._shape(kind, row, code_column) for row in rows]

    def commodity_alerts(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_from: date | None,
        period_to: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        statement = self._period(
            select(CommodityOperationalAlert),
            CommodityOperationalAlert,
            period_from,
            period_to,
        )
        facilities = self.facility_ids(principal)
        if facilities is not None:
            statement = statement.where(CommodityOperationalAlert.facility_id.in_(facilities))
        rows = self._session.execute(
            statement.order_by(CommodityOperationalAlert.period_start.desc()).limit(limit)
        ).scalars()
        return [
            {
                "id": row.id,
                "record_type": "commodity_alert",
                "code": row.alert_kind.value,
                "geography_unit_id": row.district_geography_unit_id,
                "facility_id": row.facility_id,
                "period_start": row.period_start,
                "period_end": row.period_end,
                "numerator": None,
                "denominator": None,
                "value": None,
                "value_status": row.severity.value,
                "details": {
                    "commodity_code": row.commodity_code,
                    "commodity_label": row.commodity_label,
                    "statement": row.statement,
                    "supporting_fact_ids": row.supporting_fact_ids,
                    "configuration_version_id": (
                        str(row.configuration_version_id) if row.configuration_version_id else None
                    ),
                    "method_version_id": (
                        str(row.method_version_id) if row.method_version_id else None
                    ),
                    "engine_version": row.engine_version,
                    "source_cutoff": row.source_cutoff.isoformat(),
                },
            }
            for row in rows
        ]

    @staticmethod
    def _shape(kind: str, row: Any, code_column: str) -> dict[str, Any]:
        code = getattr(row, code_column)
        geography_unit_id = getattr(row, "geography_unit_id", None)
        facility_id = getattr(row, "facility_id", None)
        if kind == "recurrence":
            if row.scope_kind is RecurrenceScopeKind.FACILITY:
                facility_id = row.scope_id
            else:
                geography_unit_id = row.scope_id
        numeric_value = getattr(row, "value", None)
        if numeric_value is None:
            numeric_value = getattr(row, "observed_value", None)
        if numeric_value is None:
            numeric_value = getattr(row, "expected_value", None)
        return {
            "id": row.id,
            "record_type": kind,
            "code": code.value if hasattr(code, "value") else str(code),
            "geography_unit_id": geography_unit_id,
            "facility_id": facility_id,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "numerator": getattr(row, "numerator", None),
            "denominator": getattr(row, "denominator", None),
            "value": float(numeric_value) if numeric_value is not None else None,
            "value_status": (
                row.value_status.value
                if getattr(row, "value_status", None) is not None
                else (
                    row.outcome.value if getattr(row, "outcome", None) is not None else "available"
                )
            ),
            "details": {
                "input_fingerprint": getattr(row, "input_fingerprint", None),
                "method_version_id": (
                    str(getattr(row, "method_version_id", None))
                    if getattr(row, "method_version_id", None)
                    else None
                ),
                "configuration_version_id": (
                    str(getattr(row, "configuration_version_id", None))
                    if getattr(row, "configuration_version_id", None)
                    else None
                ),
                "episode_rule_version_id": (
                    str(getattr(row, "episode_rule_version_id", None))
                    if getattr(row, "episode_rule_version_id", None)
                    else None
                ),
                "boundary_version_id": (
                    str(getattr(row, "boundary_version_id", None))
                    if getattr(row, "boundary_version_id", None)
                    else None
                ),
                "engine_version": getattr(row, "engine_version", None),
                "computed_at": (
                    row.computed_at.isoformat() if getattr(row, "computed_at", None) else None
                ),
                "source_cutoff": (
                    row.source_cutoff.isoformat() if getattr(row, "source_cutoff", None) else None
                ),
                "series_kind": (
                    row.series_kind.value if getattr(row, "series_kind", None) else None
                ),
                "geography_grain": (
                    row.geography_grain.value if getattr(row, "geography_grain", None) else None
                ),
                "period_grain": (
                    row.period_grain.value if getattr(row, "period_grain", None) else None
                ),
                "expected_value": (
                    str(row.expected_value)
                    if getattr(row, "expected_value", None) is not None
                    else None
                ),
                "observed_value": (
                    str(row.observed_value)
                    if getattr(row, "observed_value", None) is not None
                    else None
                ),
                "uncertainty_lower": (
                    str(row.uncertainty_lower)
                    if getattr(row, "uncertainty_lower", None) is not None
                    else None
                ),
                "uncertainty_upper": (
                    str(row.uncertainty_upper)
                    if getattr(row, "uncertainty_upper", None) is not None
                    else None
                ),
                "interpretation_context": getattr(row, "interpretation_context", None),
                "quality_context": getattr(row, "quality_context", None),
                "notes": getattr(row, "notes", None),
            },
        }


__all__ = ["AnalyticsQueryService"]
