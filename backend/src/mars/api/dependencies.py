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
from mars.security.permissions import PERMISSION_CATALOGUE, Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal
from mars.security.providers import TokenVerifier, build_token_verifier
from mars.services.audit_service import AuditService
from mars.services.auth_service import AuthService
from mars.services.geography_map_service import GeographyMapService
from mars.services.geography_service import GeographyService
from mars.services.governance_service import ConfigurationService, MethodRegistryService
from mars.services.organisation_service import FacilityService, OrganisationService

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


# -- Authentication -------------------------------------------------------
def get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    auth_service: AuthServiceDep,
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> AuthenticatedPrincipal:
    """Resolve the caller into an authorisation context.

    A valid token for an unknown or deactivated subject is rejected. Provisioning
    is an administrative act; MARS does not create an account because a provider
    vouched for someone.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError("A bearer token is required")

    identity = verifier.verify(credentials.credentials)
    user = auth_service.find_user_by_subject(identity.subject)

    if user is None:
        raise UnauthenticatedError("No MARS account exists for this identity")
    if not user.is_active:
        raise UnauthenticatedError("This MARS account is deactivated")

    principal = auth_service.build_principal(user, identity)

    # Bind to the logging and audit context for the rest of the request.
    set_actor_id(str(principal.user_id))
    set_session_id(principal.session_reference)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[AuthenticatedPrincipal, Depends(get_current_principal)]


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
