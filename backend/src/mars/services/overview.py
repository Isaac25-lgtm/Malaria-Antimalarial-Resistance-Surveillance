"""Coherent dashboard snapshot for the operational overview.

Every section carries availability, requested scope, period, source and
freshness. The browser must not compute a competing indicator formula.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from mars.core.settings import Settings
from mars.domain.enums import IntegrationRunStatus, SignalPriority
from mars.investigations.service import InvestigationService
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService
from mars.services.integration_status import IntegrationStatusService
from mars.services.signal_query import SignalQueryService
from mars.services.surveillance_summary import (
    INTERPRETATION_BOUNDARY,
    STATUS_NOT_CONFIGURED,
    SurveillanceSummaryService,
)

PRIORITY_ORDER: tuple[str, ...] = (
    SignalPriority.URGENT.value,
    SignalPriority.HIGH.value,
    SignalPriority.ATTENTION.value,
    SignalPriority.INFORMATIONAL.value,
    SignalPriority.UNCLASSIFIED.value,
)

INVESTIGATION_ORDER: tuple[str, ...] = (
    "new",
    "triaged",
    "assigned",
    "under_investigation",
    "closed",
    "escalated",
)


class OverviewService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._summary = SurveillanceSummaryService(session)
        self._signals = SignalQueryService(session)
        self._investigations = InvestigationService(session)
        self._analytics = AnalyticsQueryService(session)
        self._integrations = IntegrationStatusService(session, settings)

    def snapshot(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_start: date,
        period_end: date,
    ) -> dict[str, Any]:
        period = {"start": period_start, "end": period_end}
        scope = self._scope(principal)
        provenance = self._summary.provenance(
            principal, period_start=period_start, period_end=period_end
        )
        integration = self._integrations.status("dhis2")
        last_sync = integration.get("last_run_at")
        last_status = integration.get("last_run_status")
        data_mode = self._data_mode(last_status)
        kpis = self._summary.kpis(principal, period_start=period_start, period_end=period_end)
        districts = self._summary.priority_districts(
            principal, period_start=period_start, period_end=period_end, limit=8
        )
        signals = self._signals.list(
            principal,
            period_from=period_start,
            period_to=period_end,
            active_only=True,
            limit=12,
        )
        alerts = self._analytics.commodity_alerts(
            principal,
            period_from=period_start,
            period_to=period_end,
            limit=20,
        )
        investigation_counts = self._investigations.status_counts(principal)
        sla, sla_missing = self._investigations.sla_configuration()
        signal_buckets = _count_by(signals, "priority", PRIORITY_ORDER)
        return {
            "title": scope["title"],
            "subtitle": ("Malaria surveillance from routine health information systems."),
            "interpretation_boundary": INTERPRETATION_BOUNDARY,
            "data_mode": data_mode,
            "data_mode_detail": _data_mode_detail(data_mode, last_status),
            "demo_mode_enabled": self._settings.demo_mode_enabled,
            "requested_scope": scope["label"],
            "has_national_scope": principal.has_national_scope,
            "reporting_period": period,
            "provenance": provenance,
            "last_successful_synchronization": (
                last_sync if last_status == IntegrationRunStatus.COMPLETED.value else None
            ),
            "kpis": _section(
                items=kpis,
                availability=_measures_availability(kpis),
                scope=scope["label"],
                period=period,
                source="table:indicator_result",
                freshness=provenance.get("analytics_refreshed_at"),
                last_sync=last_sync,
            ),
            "signals_by_priority": _section(
                items=signal_buckets,
                availability=_bucket_availability(signal_buckets, provenance),
                scope=scope["label"],
                period=period,
                source="table:surveillance_signal",
                freshness=provenance.get("signals_generated_at"),
                last_sync=last_sync,
                method_version_id=_kpi_method(kpis, "ACTIVE_SIGNALS"),
                refusal_reason=_signal_refusal(provenance, kpis),
            ),
            "investigations_by_status": _section(
                items=[
                    {
                        "code": code,
                        "label": code.replace("_", " "),
                        "count": investigation_counts.get(code, 0),
                        "status": "available",
                    }
                    for code in INVESTIGATION_ORDER
                ],
                availability="available",
                scope=scope["label"],
                period=period,
                source="table:investigation",
                freshness=None,
                last_sync=last_sync,
            ),
            "districts_requiring_review": _section(
                items=districts,
                availability="available" if districts else "empty",
                scope=scope["label"],
                period=period,
                source="table:surveillance_signal",
                freshness=provenance.get("signals_generated_at"),
                last_sync=last_sync,
            ),
            "commodity_alerts": _section(
                items=alerts,
                availability="available" if alerts else "empty",
                scope=scope["label"],
                period=period,
                source="table:commodity_operational_alert",
                freshness=None,
                last_sync=last_sync,
            ),
            "needs_attention": _section(
                items=_needs_attention(signals, investigation_counts, alerts, sla, sla_missing),
                availability="available",
                scope=scope["label"],
                period=period,
                source="composed:overview",
                freshness=None,
                last_sync=last_sync,
                refusal_reason=(
                    None
                    if sla is not None
                    else "An overdue investigation count is omitted until an approved SLA exists."
                ),
            ),
            "recent_signals": _section(
                items=signals,
                availability="available" if signals else "empty",
                scope=scope["label"],
                period=period,
                source="table:surveillance_signal",
                freshness=provenance.get("signals_generated_at"),
                last_sync=last_sync,
            ),
            "confirmed_malaria_trend": _empty_chart_section(
                scope["label"],
                period,
                last_sync,
                "Confirmed malaria against a seasonal baseline is not assembled "
                "here. The dashboard renders the governed confirmed-malaria KPI "
                "instead of inventing a weekly series.",
            ),
            "testing_positivity": _empty_chart_section(
                scope["label"],
                period,
                last_sync,
                "Testing volume and positivity are shown as governed KPI records "
                "in the strip. A weekly combo chart is not assembled from a new formula.",
            ),
        }

    def _scope(self, principal: AuthenticatedPrincipal) -> dict[str, str]:
        if principal.has_national_scope:
            return {"title": "National Overview", "label": "national"}
        names = [scope.name for scope in principal.geography_scopes]
        if any("pader" in name.lower() for name in names):
            return {"title": "Pader Overview", "label": "pader"}
        if names:
            return {"title": f"{names[0].title()} Overview", "label": names[0].lower()}
        if principal.facility_scopes:
            return {"title": "Facility Overview", "label": "facility"}
        return {"title": "Scoped Overview", "label": "unscoped"}

    def _data_mode(self, last_status: object) -> str:
        if self._settings.demo_mode_enabled:
            return "synthetic"
        if last_status == IntegrationRunStatus.COMPLETED.value:
            return "live"
        return "unavailable"


def _section(
    *,
    items: list[Any],
    availability: str,
    scope: str,
    period: dict[str, date],
    source: str,
    freshness: Any,
    last_sync: Any,
    method_version_id: Any = None,
    refusal_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "availability": availability,
        "requested_scope": scope,
        "reporting_period": period,
        "source": source,
        "source_period": period,
        "freshness": freshness,
        "last_successful_synchronization": last_sync,
        "method_version_id": method_version_id,
        "refusal_reason": refusal_reason,
        "items": items,
    }


def _empty_chart_section(
    scope: str, period: dict[str, date], last_sync: Any, reason: str
) -> dict[str, Any]:
    return _section(
        items=[],
        availability="not_configured",
        scope=scope,
        period=period,
        source="not_assembled",
        freshness=None,
        last_sync=last_sync,
        refusal_reason=reason,
    )


def _measures_availability(kpis: list[dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in kpis}
    if statuses == {STATUS_NOT_CONFIGURED}:
        return "not_configured"
    if "available" in statuses:
        return "available"
    return "unavailable"


def _bucket_availability(buckets: list[dict[str, Any]], provenance: dict[str, Any]) -> str:
    if not provenance.get("analytically_configured"):
        return "not_configured"
    if any(item.get("count") for item in buckets):
        return "available"
    return "empty"


def _kpi_method(kpis: list[dict[str, Any]], code: str) -> Any:
    for item in kpis:
        if item.get("code") == code:
            return item.get("method_version_id")
    return None


def _signal_refusal(provenance: dict[str, Any], kpis: list[dict[str, Any]]) -> str | None:
    active = next((item for item in kpis if item.get("code") == "ACTIVE_SIGNALS"), None)
    if active and active.get("status") == STATUS_NOT_CONFIGURED:
        return active.get("status_detail")
    if not provenance.get("analytically_configured"):
        return provenance.get("configuration_detail")
    return None


def _count_by(
    rows: list[dict[str, Any]], field: str, order: tuple[str, ...]
) -> list[dict[str, Any]]:
    counts = dict.fromkeys(order, 0)
    for row in rows:
        key = str(row.get(field) or "")
        if key in counts:
            counts[key] += 1
    return [
        {
            "code": code,
            "label": code.replace("_", " "),
            "count": counts[code],
            "status": "available",
        }
        for code in order
    ]


def _needs_attention(
    signals: list[dict[str, Any]],
    investigation_counts: dict[str, int],
    alerts: list[dict[str, Any]],
    sla: dict[str, Any] | None,
    sla_missing: list[str],
) -> list[dict[str, Any]]:
    untriaged = sum(
        1
        for signal in signals
        if signal.get("priority") in {SignalPriority.HIGH.value, SignalPriority.URGENT.value}
    )
    items = [
        {
            "code": "high_priority_signals",
            "label": "High-priority signals in this period",
            "count": untriaged,
            "status": "available",
        },
        {
            "code": "open_investigations",
            "label": "Investigations not yet closed",
            "count": sum(
                investigation_counts.get(code, 0)
                for code in ("new", "triaged", "assigned", "under_investigation")
            ),
            "status": "available",
        },
        {
            "code": "commodity_alerts",
            "label": "Commodity operational alerts",
            "count": len(alerts),
            "status": "available",
        },
    ]
    if sla is None:
        items.append(
            {
                "code": "investigations_overdue",
                "label": "Investigations overdue",
                "count": None,
                "status": "not_configured",
                "detail": (
                    "Omitted: "
                    + ", ".join(sla_missing)
                    + ". An empty overdue count would say nothing is late."
                ),
            }
        )
    return items


def _data_mode_detail(mode: str, last_status: object) -> str:
    if mode == "synthetic":
        return (
            "This deployment is serving synthetic demonstration data. "
            "It is not a live Ministry feed."
        )
    if mode == "live":
        return "A bounded source synchronisation has completed for the configured origin."
    return "No completed source synchronisation is on record" + (
        f" (last run: {last_status})." if last_status else "."
    )
