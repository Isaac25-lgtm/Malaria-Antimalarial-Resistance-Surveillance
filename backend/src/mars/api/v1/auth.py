"""Authentication endpoints.

``/auth/me`` is the production surface. The development sign-in routes exist
only when synthetic authentication is active, and they are not registered at all
otherwise - so they cannot appear in a production OpenAPI document, let alone be
called.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response

from mars.api.dependencies import (
    AuditDep,
    AuthServiceDep,
    PrincipalDep,
    SettingsDep,
    get_token_verifier,
)
from mars.api.v1.schemas import (
    CurrentUserResponse,
    DevelopmentLoginRequest,
    DevelopmentLoginResponse,
    DevelopmentUserSummary,
    GeographyScopeSummary,
)
from mars.core.errors import FeatureDisabledError, UnauthenticatedError
from mars.core.logging import get_logger
from mars.domain.enums import AuditAction, AuditOutcome
from mars.security.providers import DevelopmentTokenVerifier

router = APIRouter(tags=["auth"])
logger = get_logger(__name__)


@router.get(
    "/auth/me",
    response_model=CurrentUserResponse,
    summary="The caller's profile and effective authorisation",
)
def current_user(principal: PrincipalDep) -> CurrentUserResponse:
    """Return the caller's own non-sensitive profile.

    The frontend uses this to decide what to render. It is a convenience, not a
    control: every endpoint re-checks server-side, so a client that ignores this
    response gains nothing.
    """
    return CurrentUserResponse(
        user_id=principal.user_id,
        username=principal.username,
        display_name=principal.display_name,
        email=None,
        organisation_label=None,
        roles=sorted(principal.roles),
        permissions=sorted(p.value for p in principal.permissions),
        max_sensitivity=principal.max_sensitivity.name.lower(),
        geography_scopes=[
            GeographyScopeSummary(
                geography_unit_id=scope.geography_unit_id,
                preferred_code=scope.preferred_code,
                level=scope.level,
                name=scope.name,
            )
            for scope in principal.geography_scopes
        ],
        facility_scope_ids=sorted(principal.facility_scopes),
        has_national_scope=principal.has_national_scope,
        auth_method=principal.auth_method,
        is_synthetic=principal.is_synthetic,
    )


@router.post(
    "/auth/logout",
    summary="End the current session",
    status_code=204,
    response_class=Response,
)
def logout(principal: PrincipalDep, audit: AuditDep) -> Response:
    """Record the logout.

    Token revocation is the identity provider's responsibility; MARS records the
    event so the audit trail has both ends of the session.
    """
    audit.record(
        action=AuditAction.LOGOUT,
        principal=principal,
        object_type="user_session",
        object_id=principal.session_reference,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Development-only routes.
#
# Registered by ``register_development_auth_routes`` and only when synthetic
# authentication is active. In staging or production these paths do not exist.
# ---------------------------------------------------------------------------
development_router = APIRouter(tags=["auth", "development"])


@development_router.get(
    "/auth/dev/users",
    response_model=list[DevelopmentUserSummary],
    summary="Synthetic users available for development sign-in",
)
def development_users(
    settings: SettingsDep, auth_service: AuthServiceDep
) -> list[DevelopmentUserSummary]:
    """List the synthetic accounts a developer may sign in as."""
    if not settings.is_development_auth_active:
        raise FeatureDisabledError("Development authentication is not enabled")

    from mars.security.dev_users import DEVELOPMENT_USERS

    return [
        DevelopmentUserSummary(
            username=spec.username,
            display_name=spec.display_name,
            role=spec.role.value,
            scope_description=spec.scope_description,
        )
        for spec in DEVELOPMENT_USERS
    ]


@development_router.post(
    "/auth/dev/login",
    response_model=DevelopmentLoginResponse,
    summary="Sign in as a synthetic development user",
)
def development_login(
    payload: DevelopmentLoginRequest,
    request: Request,
    settings: SettingsDep,
    auth_service: AuthServiceDep,
    audit: AuditDep,
) -> DevelopmentLoginResponse:
    """Issue a short-lived synthetic token.

    The account must already exist and be flagged synthetic. This route never
    creates an account, so it cannot be used to mint a principal that the seeded
    development fixture did not define.
    """
    if not settings.is_development_auth_active:
        raise FeatureDisabledError("Development authentication is not enabled")

    subject = f"{DevelopmentTokenVerifier.SUBJECT_PREFIX}{payload.username}"
    user = auth_service.find_user_by_subject(subject)

    if user is None or not user.is_synthetic or not user.is_active:
        audit.record(
            action=AuditAction.LOGIN_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_kind="anonymous",
            actor_label=payload.username,
            reason="unknown or non-synthetic development user",
        )
        raise UnauthenticatedError("No synthetic development user with that username")

    verifier = get_token_verifier(request, settings)
    assert isinstance(verifier, DevelopmentTokenVerifier)

    token, session_reference, expires_at = verifier.issue(
        subject=user.subject,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
    )

    auth_service.record_login(user)
    principal = auth_service.build_principal(user)
    audit.record(
        action=AuditAction.LOGIN_SUCCEEDED,
        principal=principal,
        actor_label=user.username,
        object_type="user_session",
        object_id=session_reference,
        source_ip=request.client.host if request.client else None,
        context={"auth_method": "development", "synthetic": True},
    )

    logger.info("development_login", username=user.username, synthetic=True)

    return DevelopmentLoginResponse(
        access_token=token,
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
    )


def register_development_auth_routes(app_router: APIRouter, settings: SettingsDep) -> None:
    """Attach the development routes, but only when they are permitted."""
    if settings.is_development_auth_active:
        app_router.include_router(development_router)
