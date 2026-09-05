"""Controlled live-source data operations for the Pader pilot."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from mars.api.dependencies import AuditDep, SettingsDep, require_permissions, require_sensitivity
from mars.api.v1.schemas import (
    ControlledTrackerPreviewRequest,
    ControlledTrackerPreviewSummary,
    LiveDashboardSnapshot,
    LiveDashboardSyncRequest,
)
from mars.core.errors import FeatureDisabledError, UnauthenticatedError, UpstreamUnavailableError
from mars.core.logging import get_logger
from mars.domain.enums import AuditAction
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.services.live_dashboard import LiveDashboardError
from mars.services.live_tracker import LiveTrackerPreviewError

router = APIRouter(prefix="/live", tags=["live data"])
logger = get_logger(__name__)
PatientReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.CASE_EVIDENCE_VIEW)),
]


@router.get(
    "/dashboard",
    response_model=LiveDashboardSnapshot | None,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def latest_live_dashboard(
    request: Request,
    settings: SettingsDep,
    principal: PatientReader,
) -> LiveDashboardSnapshot | None:
    """Return only this session's last real source snapshot."""
    if not settings.is_live_auth_active:
        raise FeatureDisabledError("Live eRegisters authentication is not enabled")
    raw_id = request.cookies.get(settings.session_cookie_name)
    service = getattr(request.app.state, "live_dashboard", None)
    if not raw_id or service is None:
        return None
    result = service.latest(raw_id)
    return LiveDashboardSnapshot.model_validate(result) if result else None


@router.post(
    "/dashboard/synchronize",
    response_model=LiveDashboardSnapshot,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def synchronize_live_dashboard(
    payload: LiveDashboardSyncRequest,
    request: Request,
    settings: SettingsDep,
    principal: PatientReader,
    audit: AuditDep,
) -> LiveDashboardSnapshot:
    """Read scoped HMIS values and pseudonymous Tracker evidence server-to-server."""
    if not settings.is_live_auth_active:
        raise FeatureDisabledError("Live eRegisters authentication is not enabled")
    raw_id = request.cookies.get(settings.session_cookie_name)
    dashboard = getattr(request.app.state, "live_dashboard", None)
    discovery = getattr(request.app.state, "live_metadata_discovery", None)
    if not raw_id or dashboard is None or discovery is None:
        raise UnauthenticatedError("A live session and metadata discovery are required")
    facilities = discovery.tracker_facilities(raw_id)
    if not facilities:
        raise FeatureDisabledError(
            "Run live metadata discovery first; no exact facility scope has been proven."
        )
    try:
        result = dashboard.synchronize(
            raw_id,
            facilities=facilities,
            period_start=payload.period_start,
            period_end=payload.period_end,
        )
    except LiveDashboardError as error:
        raise FeatureDisabledError(str(error)) from error
    except Exception as error:
        logger.info("live_dashboard_sync_failed", error_type=type(error).__name__)
        raise UpstreamUnavailableError(
            "The scoped eRegisters synchronization could not complete. "
            "No synthetic values were substituted."
        ) from error
    audit.record(
        action=AuditAction.CASE_EVIDENCE_ACCESSED,
        principal=principal,
        object_type="live_dashboard_sync",
        object_id="pader",
        context={
            "period_start": payload.period_start.isoformat(),
            "period_end": payload.period_end.isoformat(),
            "facility_count": result["facility_count"],
            "tracker_event_count": result["tracker_event_count"],
            "synthetic_data_used": False,
        },
    )
    return LiveDashboardSnapshot.model_validate(result)


@router.get(
    "/tracker/preview",
    response_model=ControlledTrackerPreviewSummary | None,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def latest_tracker_preview(
    request: Request,
    settings: SettingsDep,
    principal: PatientReader,
) -> ControlledTrackerPreviewSummary | None:
    if not settings.is_live_auth_active:
        raise FeatureDisabledError("Live eRegisters authentication is not enabled")
    raw_id = request.cookies.get(settings.session_cookie_name)
    service = getattr(request.app.state, "live_tracker_preview", None)
    if not raw_id or service is None:
        return None
    result = service.latest(raw_id)
    return ControlledTrackerPreviewSummary.model_validate(result) if result else None


@router.post(
    "/tracker/preview",
    response_model=ControlledTrackerPreviewSummary,
    dependencies=[Depends(require_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE))],
)
def run_tracker_preview(
    payload: ControlledTrackerPreviewRequest,
    request: Request,
    settings: SettingsDep,
    principal: PatientReader,
    audit: AuditDep,
) -> ControlledTrackerPreviewSummary:
    """Validate one facility and at most fourteen days; persist no patient row."""
    if not settings.is_live_auth_active:
        raise FeatureDisabledError("Live eRegisters authentication is not enabled")
    raw_id = request.cookies.get(settings.session_cookie_name)
    preview = getattr(request.app.state, "live_tracker_preview", None)
    discovery = getattr(request.app.state, "live_metadata_discovery", None)
    if not raw_id or preview is None or discovery is None:
        raise UnauthenticatedError("A live session and metadata discovery are required")
    approved_facilities = discovery.tracker_facility_uids(raw_id)
    if not approved_facilities:
        raise FeatureDisabledError(
            "Run live metadata discovery first; no exact Tracker facility has been proven."
        )
    try:
        result = preview.preview(
            raw_id,
            facility_uid=payload.facility_uid,
            period_start=payload.period_start,
            period_end=payload.period_end,
            approved_facility_uids=approved_facilities,
        )
    except LiveTrackerPreviewError as error:
        raise FeatureDisabledError(str(error)) from error
    except Exception as error:
        logger.info("live_tracker_preview_failed", error_type=type(error).__name__)
        raise UpstreamUnavailableError(
            "The controlled Tracker preview could not complete. No patient row was returned "
            "to the browser or persisted in MARS."
        ) from error
    audit.record(
        action=AuditAction.CASE_EVIDENCE_ACCESSED,
        principal=principal,
        object_type="tracker_controlled_preview",
        object_id=payload.facility_uid,
        context={
            "period_start": payload.period_start.isoformat(),
            "period_end": payload.period_end.isoformat(),
            "event_count": result["retrieved_event_count"],
            "patient_rows_returned": False,
            "persisted": False,
        },
    )
    return ControlledTrackerPreviewSummary.model_validate(result)


__all__ = ["router"]
