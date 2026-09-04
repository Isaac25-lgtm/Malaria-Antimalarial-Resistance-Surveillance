"""Authentication endpoints.

``GET /auth/session`` is public: an anonymous caller receives
``{"authenticated": false}`` rather than a 401, so the browser does not log
an expected failure.

``POST /auth/login`` is the live eRegisters path. Development sign-in remains
on ``/auth/dev/*`` and is registered only when synthetic authentication is
active. The two never fall back into each other.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from mars.api.dependencies import (
    AuditDep,
    AuthServiceDep,
    OptionalPrincipalDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    get_live_auth_service,
    get_token_verifier,
)
from mars.api.v1.schemas import (
    AuthorisedDistrictSummary,
    CurrentUserResponse,
    DataReadinessSummary,
    DevelopmentLoginRequest,
    DevelopmentLoginResponse,
    DevelopmentUserSummary,
    GeographyScopeSummary,
    LiveLoginRequest,
    LocalMappingSummary,
    RemoteWorkspaceSummary,
    SessionScopeSummary,
    SessionStatusResponse,
    SessionUserSummary,
    SourceStatusSummary,
)
from mars.core.errors import (
    CsrfRejectedError,
    FeatureDisabledError,
    UnauthenticatedError,
    ValidationFailedError,
)
from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.domain.enums import AuditAction, AuditOutcome
from mars.security.origin import assert_approved_origin
from mars.security.principal import AuthenticatedPrincipal
from mars.security.providers import DevelopmentTokenVerifier
from mars.security.remote_authorization import LiveAuthorizationState
from mars.services.live_auth import attach_session_cookies, clear_session_cookies

router = APIRouter(tags=["auth"])
logger = get_logger(__name__)


@router.get(
    "/auth/session",
    response_model=SessionStatusResponse,
    summary="Whether this browser has a MARS session",
)
def session_status(
    request: Request,
    settings: SettingsDep,
    principal: OptionalPrincipalDep,
) -> SessionStatusResponse:
    """Return a sanitized session snapshot, or authenticated=false.

    Never 401. Live cookie sessions and demo bearer tokens both surface here
    so the frontend can bootstrap without a noisy expected error.
    """
    if principal is None:
        return SessionStatusResponse(
            authenticated=False,
            auth_mode=settings.auth_mode,
        )
    live = None
    if settings.is_live_auth_active:
        live = getattr(request.state, "live_session", None)
    authorization = getattr(live, "authorization", None)
    profile = _current_user_from_principal(principal, authorization=authorization)
    source = _source_status(settings, mapping=profile.mapping_status)
    return SessionStatusResponse(
        authenticated=True,
        auth_mode=settings.auth_mode,
        csrf_token=getattr(live, "csrf_token", None),
        user=SessionUserSummary(display_name=principal.display_name, username=principal.username),
        scope=_scope_summary(principal, profile.scope_type, authorization),
        permissions=sorted(p.value for p in principal.permissions),
        source_status=source,
        profile=profile,
        workspace=profile.workspace,
        mapping=profile.mapping,
        data_readiness=profile.data_readiness,
    )


@router.get(
    "/auth/me",
    response_model=CurrentUserResponse,
    summary="The caller's profile and effective authorisation",
)
def current_user(request: Request, principal: PrincipalDep) -> CurrentUserResponse:
    """Return the caller's own non-sensitive profile.

    The frontend uses this to decide what to render. It is a convenience, not a
    control: every endpoint re-checks server-side, so a client that ignores this
    response gains nothing.
    """
    live = getattr(request.state, "live_session", None)
    return _current_user_from_principal(
        principal,
        authorization=getattr(live, "authorization", None),
    )


@router.post(
    "/auth/login",
    response_model=SessionStatusResponse,
    summary="Sign in with an authorised eRegisters account",
)
def live_login(
    payload: LiveLoginRequest,
    request: Request,
    settings: SettingsDep,
    audit: AuditDep,
    session: SessionDep,
) -> JSONResponse:
    """Authenticate server-to-server against eRegisters and issue a cookie session.

    The browser never receives a DHIS2 credential. Failed live authentication
    does not fall back to demo authentication.
    """
    if not settings.is_live_auth_active:
        raise FeatureDisabledError("Live eRegisters authentication is not enabled")
    _assert_login_content_type(request)
    _assert_login_size(request, settings)

    service = get_live_auth_service(request, settings, session)
    result = service.login(request, payload.username, payload.password)

    audit.record(
        action=AuditAction.LOGIN_SUCCEEDED,
        principal=result.session.principal,
        actor_label=result.session.principal.username,
        object_type="user_session",
        object_id=result.session.principal.session_reference,
        source_ip=request.client.host if request.client else None,
        context={
            "auth_method": "dhis2_pilot",
            "scope_type": result.scope.scope_type,
            "mapping_status": result.scope.mapping_status,
            "authorization_status": result.scope.workspace.status,
        },
    )

    profile = _current_user_from_principal(
        result.session.principal,
        authorization=result.session.authorization,
    )
    body = SessionStatusResponse(
        authenticated=True,
        auth_mode=settings.auth_mode,
        csrf_token=result.session.csrf_token,
        user=SessionUserSummary(
            display_name=result.session.principal.display_name,
            username=result.session.principal.username,
        ),
        scope=_scope_summary(
            result.session.principal, result.scope.scope_type, result.session.authorization
        ),
        permissions=sorted(p.value for p in result.session.principal.permissions),
        source_status=_source_status(settings, mapping=result.scope.mapping_status),
        profile=profile,
        workspace=profile.workspace,
        mapping=profile.mapping,
        data_readiness=profile.data_readiness,
    )
    response = JSONResponse(
        content=body.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )
    attach_session_cookies(
        response,
        settings,
        raw_session_id=result.raw_session_id,
        csrf_token=result.session.csrf_token,
    )
    return response


@router.post(
    "/auth/logout",
    summary="End the current session",
    status_code=204,
    response_class=Response,
)
def logout(
    request: Request,
    settings: SettingsDep,
    audit: AuditDep,
    principal: OptionalPrincipalDep,
) -> Response:
    """Invalidate a live cookie session, or record a demo logout.

    CSRF is required in live mode. Demo bearer logout remains a recorded event;
    token drop is the client's responsibility.
    """
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if settings.is_live_auth_active:
        assert_approved_origin(request, settings)
        live = getattr(request.state, "live_session", None)
        raw_id = request.cookies.get(settings.session_cookie_name)
        expected = getattr(live, "csrf_token", None)
        supplied = request.headers.get(settings.csrf_header_name)
        if expected is None or not supplied or not _tokens_match(expected, supplied):
            raise CsrfRejectedError("This request did not include a valid CSRF token.")
        store = getattr(request.app.state, "live_session_store", None)
        holder = getattr(request.app.state, "live_credential_holder", None)
        if store is not None and raw_id:
            store.invalidate(raw_id)
        if holder is not None and raw_id:
            holder.drop(raw_id)
        clear_session_cookies(response, settings)
    if principal is not None:
        audit.record(
            action=AuditAction.LOGOUT,
            principal=principal,
            object_type="user_session",
            object_id=principal.session_reference,
        )
    return response


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
    if settings.is_live_auth_active:
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


def _current_user_from_principal(
    principal: AuthenticatedPrincipal,
    *,
    authorization: LiveAuthorizationState | None = None,
) -> CurrentUserResponse:
    resolved_type: str
    mapping_status: str
    if authorization is not None:
        resolved_type = authorization.workspace.scope_type
        mapping_status = authorization.mapping.status
        landing_path = authorization.landing_path
        workspace = _workspace_summary(authorization)
        mapping = LocalMappingSummary(
            status=authorization.mapping.status,
            geography_unit_id=authorization.mapping.geography_unit_id,
            facility_id=authorization.mapping.facility_id,
            evidence=list(authorization.mapping.evidence),
        )
        readiness = DataReadinessSummary(
            geography=authorization.readiness.geography,
            malaria_metadata=authorization.readiness.malaria_metadata,
            aggregate_sync=authorization.readiness.aggregate_sync,
            tracker_sync=authorization.readiness.tracker_sync,
        )
    else:
        resolved_type = _infer_scope_type(principal)
        mapping_status = "mapped" if resolved_type != "unresolved" else "pending"
        landing_path = _landing_from_type(principal, resolved_type)
        workspace = None
        mapping = None
        readiness = None
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
        scope_type=resolved_type,
        mapping_status=mapping_status,
        landing_path=landing_path,
        workspace=workspace,
        mapping=mapping,
        data_readiness=readiness,
    )


def _workspace_summary(authorization: LiveAuthorizationState) -> RemoteWorkspaceSummary:
    workspace = authorization.workspace
    remote = authorization.remote_authorization
    return RemoteWorkspaceSummary(
        authorization_status=workspace.status,
        scope_type=workspace.scope_type,
        source=workspace.source,
        external_uid=workspace.external_uid,
        name=workspace.name,
        code=workspace.code,
        level=workspace.level,
        path=workspace.path,
        parent_uid=workspace.parent_uid,
        capture_count=len(remote.capture_scope),
        data_view_count=len(remote.data_view_scope),
        tracker_search_count=len(remote.tracker_search_scope),
        fallback_used=remote.fallback_used,
    )


def _infer_scope_type(principal: AuthenticatedPrincipal) -> str:
    if principal.has_national_scope:
        return "national"
    districts = [scope for scope in principal.geography_scopes if scope.level == "district"]
    if len(districts) == 1:
        return "district"
    if len(districts) > 1:
        return "multi_district"
    if principal.facility_scopes:
        return "facility"
    if principal.geography_scopes:
        return "other"
    return "unresolved"


def _landing_from_type(principal: AuthenticatedPrincipal, scope_type: str) -> str:
    if scope_type == "national":
        return "/command-centre"
    if scope_type == "district":
        districts = [scope for scope in principal.geography_scopes if scope.level == "district"]
        if districts:
            return f"/district/{districts[0].geography_unit_id}"
    if scope_type == "facility" and principal.facility_scopes:
        return f"/facility/{next(iter(sorted(principal.facility_scopes)))}"
    if scope_type in {"multi_district", "other"}:
        return "/authorised-scope"
    return "/no-authorised-scope"


def _scope_summary(
    principal: AuthenticatedPrincipal,
    scope_type: str,
    authorization: LiveAuthorizationState | None = None,
) -> SessionScopeSummary:
    districts = [scope for scope in principal.geography_scopes if scope.level == "district"]
    primary = districts[0] if len(districts) == 1 else None
    org_unit_id = primary.geography_unit_id if primary else None
    org_unit_name = primary.name if primary else None
    if authorization is not None:
        org_unit_name = authorization.workspace.name or org_unit_name
        if authorization.mapping.geography_unit_id is not None:
            org_unit_id = authorization.mapping.geography_unit_id
        if authorization.mapping.facility_id is not None and scope_type == "facility":
            org_unit_id = authorization.mapping.facility_id
    if scope_type == "facility" and principal.facility_scopes and org_unit_id is None:
        org_unit_id = next(iter(sorted(principal.facility_scopes)))
    if scope_type == "national":
        country = next(
            (scope for scope in principal.geography_scopes if scope.level == "country"),
            None,
        )
        if country is not None:
            org_unit_id = country.geography_unit_id
            org_unit_name = country.name
    return SessionScopeSummary(
        scope_type=scope_type,
        org_unit_id=org_unit_id,
        org_unit_name=org_unit_name,
        national_access=principal.has_national_scope,
        authorised_districts=[
            AuthorisedDistrictSummary(
                org_unit_id=scope.geography_unit_id,
                org_unit_name=scope.name,
                preferred_code=scope.preferred_code,
            )
            for scope in districts
        ],
    )


def _source_status(settings: Settings, *, mapping: str) -> SourceStatusSummary:
    if settings.is_live_auth_active:
        return SourceStatusSummary(
            mode="live",
            source="eRegisters",
            authentication="connected",
            mapping=mapping,
            last_sync=None,
        )
    return SourceStatusSummary(
        mode="demo",
        source="synthetic",
        authentication="connected",
        mapping="mapped",
        last_sync=None,
    )


def _assert_login_content_type(request: Request) -> None:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise ValidationFailedError("Content-Type must be application/json.")


def _assert_login_size(request: Request, settings: Settings) -> None:
    length = request.headers.get("content-length")
    if length is None:
        return
    try:
        size = int(length)
    except ValueError:
        raise ValidationFailedError("Request size is not valid.") from None
    if size > settings.login_max_body_bytes:
        raise ValidationFailedError("Request is too large.")


def _tokens_match(expected: str, supplied: str) -> bool:
    if len(expected) != len(supplied):
        return False
    result = 0
    for left, right in zip(expected.encode("utf-8"), supplied.encode("utf-8"), strict=True):
        result |= left ^ right
    return result == 0
