"""FastAPI dependencies.

Authorisation lives here, at the router boundary, and nowhere else. A handler
receives an already-authorised principal; it never decides for itself whether
the caller was allowed in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from mars.core.context import set_actor_id, set_session_id
from mars.core.errors import (
    PermissionDeniedError,
    SensitivityScopeDeniedError,
    UnauthenticatedError,
)
from mars.core.settings import Settings, get_settings
from mars.db.session import get_db_session, get_session_factory
from mars.investigations.service import InvestigationService
from mars.security.permissions import PERMISSION_CATALOGUE, Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.security.providers import TokenVerifier, build_token_verifier
from mars.services.analytics_query import AnalyticsQueryService
from mars.services.audit_service import AuditService
from mars.services.auth_service import AuthService
from mars.services.geography_map_service import GeographyMapService
from mars.services.geography_service import GeographyService
from mars.services.governance_service import ConfigurationService, MethodRegistryService
from mars.services.indicator_query import IndicatorQueryService
from mars.services.integration_status import IntegrationStatusService
from mars.services.live_auth import LiveAuthService
from mars.services.organisation_service import FacilityService, OrganisationService
from mars.services.overview import OverviewService
from mars.services.patient_surveillance import PatientSurveillanceService
from mars.services.report_service import ReportService
from mars.services.signal_query import SignalQueryService
from mars.services.surveillance_summary import SurveillanceSummaryService

# auto_error=False so a missing credential produces our problem+json shape
# rather than FastAPI's default body.
_bearer = HTTPBearer(auto_error=False, scheme_name="MARS bearer token")

SessionDep = Annotated[Session, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_token_verifier(request: Request, settings: SettingsDep) -> TokenVerifier:
    """Resolve the configured authentication provider.

    Memoised on the application instance rather than on the module, so two
    applications configured differently in the same process - which is what the
    test suite does - never share a verifier. Built lazily so that a deployment
    with no provider configured still starts and can report itself unready,
    rather than failing to boot.
    """
    verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        verifier = build_token_verifier(settings)
        request.app.state.token_verifier = verifier
    return verifier


# -- Service dependencies -------------------------------------------------
def get_audit_service(session: SessionDep) -> AuditService:
    return AuditService(session, durable_session_factory=get_session_factory())


AuditDep = Annotated[AuditService, Depends(get_audit_service)]


def get_auth_service(session: SessionDep) -> AuthService:
    return AuthService(session)


def get_geography_service(session: SessionDep) -> GeographyService:
    return GeographyService(session)


def get_geography_map_service(session: SessionDep) -> GeographyMapService:
    return GeographyMapService(session)


def get_organisation_service(session: SessionDep) -> OrganisationService:
    return OrganisationService(session)


def get_facility_service(session: SessionDep) -> FacilityService:
    return FacilityService(session)


def get_indicator_query_service(session: SessionDep) -> IndicatorQueryService:
    return IndicatorQueryService(session)


def get_analytics_query_service(session: SessionDep) -> AnalyticsQueryService:
    return AnalyticsQueryService(session)


def get_signal_query_service(session: SessionDep) -> SignalQueryService:
    return SignalQueryService(session)


def get_surveillance_summary_service(session: SessionDep) -> SurveillanceSummaryService:
    return SurveillanceSummaryService(session)


def get_overview_service(session: SessionDep, settings: SettingsDep) -> OverviewService:
    return OverviewService(session, settings)


def get_patient_surveillance_service(
    session: SessionDep, settings: SettingsDep
) -> PatientSurveillanceService:
    return PatientSurveillanceService(session, settings)


def get_report_service(session: SessionDep, audit: AuditDep) -> ReportService:
    return ReportService(session, audit)


def get_investigation_service(session: SessionDep, audit: AuditDep) -> InvestigationService:
    return InvestigationService(session, audit)


def get_integration_status_service(
    session: SessionDep, settings: SettingsDep
) -> IntegrationStatusService:
    return IntegrationStatusService(session, settings)


def get_configuration_service(session: SessionDep, audit: AuditDep) -> ConfigurationService:
    return ConfigurationService(session, audit)


def get_method_registry_service(session: SessionDep, audit: AuditDep) -> MethodRegistryService:
    return MethodRegistryService(session, audit)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
GeographyServiceDep = Annotated[GeographyService, Depends(get_geography_service)]
GeographyMapServiceDep = Annotated[GeographyMapService, Depends(get_geography_map_service)]
OrganisationServiceDep = Annotated[OrganisationService, Depends(get_organisation_service)]
FacilityServiceDep = Annotated[FacilityService, Depends(get_facility_service)]
ConfigurationServiceDep = Annotated[ConfigurationService, Depends(get_configuration_service)]
MethodRegistryDep = Annotated[MethodRegistryService, Depends(get_method_registry_service)]
IndicatorQueryDep = Annotated[IndicatorQueryService, Depends(get_indicator_query_service)]
AnalyticsQueryDep = Annotated[AnalyticsQueryService, Depends(get_analytics_query_service)]
SignalQueryDep = Annotated[SignalQueryService, Depends(get_signal_query_service)]
SurveillanceSummaryDep = Annotated[
    SurveillanceSummaryService, Depends(get_surveillance_summary_service)
]
OverviewServiceDep = Annotated[OverviewService, Depends(get_overview_service)]
PatientSurveillanceDep = Annotated[
    PatientSurveillanceService, Depends(get_patient_surveillance_service)
]
ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]
InvestigationServiceDep = Annotated[InvestigationService, Depends(get_investigation_service)]
IntegrationStatusDep = Annotated[IntegrationStatusService, Depends(get_integration_status_service)]


# -- Authentication -------------------------------------------------------
def get_live_auth_service(
    request: Request, settings: SettingsDep, session: SessionDep | None = None
) -> LiveAuthService:
    """The live login orchestrator for this application instance."""
    from mars.services.live_auth import LiveAuthService

    provider = getattr(request.app.state, "dhis2_login_provider", None)
    if provider is None:
        raise UnauthenticatedError("Live authentication is not initialised")
    lookup = getattr(request.app.state, "live_geography_lookup", None)
    if lookup is None:
        factory = getattr(request.app.state, "live_geography_lookup_factory", None)
        if factory is None or session is None:
            raise UnauthenticatedError("Live authentication is not initialised")
        lookup = factory(session)
    return LiveAuthService(
        settings=settings,
        provider=provider,
        sessions=request.app.state.live_session_store,
        credentials=request.app.state.live_credential_holder,
        throttle=request.app.state.login_throttle,
        lookup=lookup,
    )


def get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    auth_service: AuthServiceDep,
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    settings: SettingsDep,
) -> AuthenticatedPrincipal:
    """Resolve the caller into an authorisation context.

    Live mode uses the opaque session cookie and never accepts a demo bearer
    token. Demo mode uses the existing development/OIDC bearer path. There is
    no fallback from a failed live session to synthetic authentication.
    """
    if settings.is_live_auth_active:
        principal = _principal_from_live_session(request)
        if principal is None:
            raise UnauthenticatedError("A session cookie is required")
        set_actor_id(str(principal.user_id))
        set_session_id(principal.session_reference)
        request.state.principal = principal
        return principal

    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError("A bearer token is required")

    identity = verifier.verify(credentials.credentials)
    user = auth_service.find_user_by_subject(identity.subject)

    if user is None:
        raise UnauthenticatedError("No MARS account exists for this identity")
    if not user.is_active:
        raise UnauthenticatedError("This MARS account is deactivated")

    principal = auth_service.build_principal(user, identity)

    set_actor_id(str(principal.user_id))
    set_session_id(principal.session_reference)
    request.state.principal = principal
    return principal


def get_optional_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    auth_service: AuthServiceDep,
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    settings: SettingsDep,
) -> AuthenticatedPrincipal | None:
    """A principal if one can be resolved, otherwise None. Never 401."""
    if settings.is_live_auth_active:
        principal = _principal_from_live_session(request)
        if principal is not None:
            set_actor_id(str(principal.user_id))
            set_session_id(principal.session_reference)
            request.state.principal = principal
        return principal
    if credentials is None or not credentials.credentials:
        return None
    try:
        identity = verifier.verify(credentials.credentials)
    except UnauthenticatedError:
        return None
    user = auth_service.find_user_by_subject(identity.subject)
    if user is None or not user.is_active:
        return None
    principal = auth_service.build_principal(user, identity)
    set_actor_id(str(principal.user_id))
    set_session_id(principal.session_reference)
    request.state.principal = principal
    return principal


def _principal_from_live_session(request: Request) -> AuthenticatedPrincipal | None:
    live = getattr(request.state, "live_session", None)
    if live is not None:
        principal = live.principal
        return principal if isinstance(principal, AuthenticatedPrincipal) else None
    settings: Settings = request.app.state.settings
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    store = getattr(request.app.state, "live_session_store", None)
    holder = getattr(request.app.state, "live_credential_holder", None)
    if store is None:
        return None
    record = store.get(raw)
    if record is None:
        if holder is not None:
            holder.drop(raw)
        return None
    request.state.live_session = record
    principal = record.principal
    return principal if isinstance(principal, AuthenticatedPrincipal) else None


PrincipalDep = Annotated[AuthenticatedPrincipal, Depends(get_current_principal)]
OptionalPrincipalDep = Annotated[AuthenticatedPrincipal | None, Depends(get_optional_principal)]


# -- Authorisation --------------------------------------------------------
def require_permissions(
    *permissions: Permission,
) -> Callable[[AuthenticatedPrincipal, AuditService], AuthenticatedPrincipal]:
    """Dependency factory asserting that the caller holds every permission.

    The denial names the missing permission, not the resource, so a 403 never
    confirms that something exists.
    """

    def _dependency(
        principal: PrincipalDep,
        audit: AuditDep,
    ) -> AuthenticatedPrincipal:
        missing = [p for p in permissions if not principal.has_permission(p)]
        if missing:
            missing_codes = ", ".join(p.value for p in missing)
            audit.record_denial(
                principal=principal,
                reason=f"missing permission: {missing_codes}",
                context={"required": [p.value for p in permissions]},
            )
            raise PermissionDeniedError(f"This action requires: {missing_codes}")
        return principal

    return _dependency


def require_sensitivity(
    level: SensitivityLevel,
) -> Callable[[AuthenticatedPrincipal, AuditService], AuthenticatedPrincipal]:
    """Dependency factory asserting the caller's data sensitivity ceiling.

    Separate from permissions on purpose: holding a permission whose minimum
    sensitivity exceeds the caller's ceiling is a misconfiguration, and the
    request is refused rather than the caller being silently upgraded.
    """

    def _dependency(
        principal: PrincipalDep,
        audit: AuditDep,
    ) -> AuthenticatedPrincipal:
        if not principal.can_access_sensitivity(level):
            audit.record_denial(
                principal=principal,
                reason=f"sensitivity ceiling {principal.max_sensitivity.name} "
                f"below required {level.name}",
                context={"required_sensitivity": level.name},
            )
            raise SensitivityScopeDeniedError(
                f"This data requires the {level.name.lower().replace('_', ' ')} "
                "sensitivity scope, which your account does not hold."
            )
        return principal

    return _dependency


def permission_requires_sensitivity(permission: Permission) -> SensitivityLevel:
    """The sensitivity tier a permission is meaningless without."""
    return PERMISSION_CATALOGUE[permission].minimum_sensitivity
