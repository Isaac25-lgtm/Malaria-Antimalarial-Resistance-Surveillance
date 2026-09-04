"""Live eRegisters login: authenticate, resolve scope, issue an opaque session.

Credentials that survive this function exist only in the in-memory holder
keyed by the new session. The request model still holds the password until
FastAPI releases the request; this module drops its own references as soon
as the holder has them.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import Response

from mars.core.errors import (
    FeatureDisabledError,
    RateLimitedError,
    UnauthenticatedError,
    UpstreamUnavailableError,
)
from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.security.live_session import InMemoryCredentialHolder, InMemorySessionStore, LiveSession
from mars.security.login_throttle import LoginThrottle
from mars.security.origin import assert_approved_origin
from mars.security.source_login import (
    INVALID_CREDENTIALS_DETAIL,
    UPSTREAM_UNAVAILABLE_DETAIL,
    AuthenticationProvider,
    SourceLoginError,
)
from mars.services.live_scope import (
    GeographyLookup,
    ResolvedLiveScope,
    build_live_principal,
    resolve_live_scope,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LiveLoginResult:
    raw_session_id: str
    session: LiveSession
    scope: ResolvedLiveScope


class LiveAuthService:
    def __init__(
        self,
        *,
        settings: Settings,
        provider: AuthenticationProvider,
        sessions: InMemorySessionStore,
        credentials: InMemoryCredentialHolder,
        throttle: LoginThrottle,
        lookup: GeographyLookup,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._sessions = sessions
        self._credentials = credentials
        self._throttle = throttle
        self._lookup = lookup

    def login(self, request: Request, username: str, password: str) -> LiveLoginResult:
        if not self._settings.is_live_auth_active:
            raise FeatureDisabledError("Live eRegisters authentication is not enabled")

        assert_approved_origin(request, self._settings)
        client_key = request.client.host if request.client else "unknown"
        username_key = self._throttle.username_key(username)
        if self._throttle.is_blocked(client_key=client_key, username_key=username_key):
            logger.info("live_login_throttled")
            raise RateLimitedError("Too many sign-in attempts. Try again later.")

        try:
            snapshot = self._provider.authenticate(username, password)
        except SourceLoginError as error:
            self._throttle.record_failure(client_key=client_key, username_key=username_key)
            if error.is_invalid_credentials:
                logger.info("live_login_rejected")
                raise UnauthenticatedError(INVALID_CREDENTIALS_DETAIL) from error
            logger.info("live_login_upstream_unavailable", category=error.category.value)
            raise UpstreamUnavailableError(UPSTREAM_UNAVAILABLE_DETAIL) from error
        except Exception:
            self._throttle.record_failure(client_key=client_key, username_key=username_key)
            logger.info("live_login_upstream_unavailable", category="unexpected")
            raise UpstreamUnavailableError(UPSTREAM_UNAVAILABLE_DETAIL) from None

        self._throttle.clear(client_key=client_key, username_key=username_key)
        scope = resolve_live_scope(snapshot, self._lookup)
        session_reference = snapshot.remote_user_id
        principal = build_live_principal(snapshot, scope, session_reference=session_reference)
        previous = request.cookies.get(self._settings.session_cookie_name)
        if previous:
            self._sessions.invalidate(previous)
            self._credentials.drop(previous)
        raw_id, session = self._sessions.create(
            principal,
            mapping_status=scope.mapping_status,
            source_status="connected",
            scope_type=scope.scope_type,
        )
        self._credentials.store(raw_id, username, password)
        logger.info(
            "live_login_succeeded",
            scope_type=scope.scope_type,
            mapping_status=scope.mapping_status,
        )
        return LiveLoginResult(raw_session_id=raw_id, session=session, scope=scope)

    def logout(self, raw_session_id: str | None) -> None:
        if not raw_session_id:
            return
        self._sessions.invalidate(raw_session_id)
        self._credentials.drop(raw_session_id)

    def session_for(self, raw_session_id: str | None) -> LiveSession | None:
        if not raw_session_id:
            return None
        record = self._sessions.get(raw_session_id)
        if record is None:
            self._credentials.drop(raw_session_id)
        return record


def attach_session_cookies(
    response: Response,
    settings: Settings,
    *,
    raw_session_id: str,
    csrf_token: str,
) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        raw_session_id,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
        max_age=settings.session_absolute_seconds,
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        httponly=False,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
        max_age=settings.session_absolute_seconds,
    )
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"


__all__ = [
    "LiveAuthService",
    "LiveLoginResult",
    "attach_session_cookies",
    "clear_session_cookies",
]
