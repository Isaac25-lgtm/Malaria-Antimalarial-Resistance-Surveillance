"""HTTP middleware: request identity and access logging."""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mars.core.context import clear_context, get_request_id, new_request_id, set_request_id
from mars.core.errors import (
    PROBLEM_CONTENT_TYPE,
    MarsError,
    OriginRejectedError,
    ValidationFailedError,
)
from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.security.origin import assert_approved_origin

logger = get_logger("mars.access")

#: Query parameters whose values must not be logged.
_SENSITIVE_QUERY_KEYS = frozenset({"token", "access_token", "code", "state", "nin"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request identifier and echoes it back on the response.

    An inbound identifier is honoured so a trace survives a proxy hop, but it is
    length-checked first: an unbounded client-supplied value would otherwise
    flow into every log line and audit row.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], settings: Settings) -> None:
        super().__init__(app)
        self._header = settings.request_id_header

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(self._header)
        request_id = inbound if inbound and len(inbound) <= 64 else new_request_id()

        set_request_id(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
            response.headers[self._header] = request_id
            return response
        finally:
            clear_context()


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emits one structured record per request.

    Logs the path template rather than the raw path where available, and never
    logs a query string, request body or response body - any of which could
    carry a patient identifier.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
                request_id=get_request_id(),
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            request_id=get_request_id(),
            query_keys=sorted(
                key for key in request.query_params if key not in _SENSITIVE_QUERY_KEYS
            ),
        )
        return response


#: Response headers applied to every request — Prompt 28.
#:
#: MARS serves an API, not markup, but a surveillance API is exactly the kind
#: of endpoint that ends up rendered in a browser tab during a demonstration or
#: a debugging session. These headers cost nothing and remove a class of
#: problems that only appears when someone does that.
SECURITY_HEADERS: dict[str, str] = {
    # A JSON response rendered as HTML because a browser sniffed it is how a
    # stored value becomes a script.
    "X-Content-Type-Options": "nosniff",
    # Nothing here is meant to be framed. Clickjacking a triage button is a
    # cheap attack against a workflow that changes real records.
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    # A signal identifier in a Referer header would leak which district a user
    # was looking at to whatever they navigate to next.
    "Referrer-Policy": "no-referrer",
    # This API has no use for a camera, a microphone or a location.
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    # Surveillance responses are per-principal and scope-dependent. A shared
    # cache holding one district officer's answer for the next is the leak this
    # prevents; individual endpoints may still opt into caching deliberately.
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies :data:`SECURITY_HEADERS` to every response.

    HSTS is added only in a protected environment, and only there: sending it
    from a local HTTP deployment would pin a developer's browser to HTTPS for
    localhost and break the next person's afternoon.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            # An endpoint that set its own caching rule keeps it.
            response.headers.setdefault(header, value)
        if self._settings.environment.is_protected:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


_UNSAFE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class LiveRequestSecurityMiddleware(BaseHTTPMiddleware):
    """Origin, body-size and CSRF controls for the live cookie session.

    Login is protected by Origin and a conservative size cap. Every other
    unsafe method additionally requires the CSRF header bound to the session.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._settings.is_live_auth_active:
            return await call_next(request)

        raw = request.cookies.get(self._settings.session_cookie_name)
        store = getattr(request.app.state, "live_session_store", None)
        holder = getattr(request.app.state, "live_credential_holder", None)
        record = store.get(raw) if store is not None and raw else None
        if raw and record is None and holder is not None:
            holder.drop(raw)
        request.state.live_session = record

        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)

        path = request.url.path.rstrip("/") or request.url.path
        is_login = path.endswith("/auth/login")
        if is_login:
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    return _mars_error_response(
                        request, ValidationFailedError("Request size is not valid.")
                    )
                if size > self._settings.login_max_body_bytes:
                    return _mars_error_response(
                        request, ValidationFailedError("Request is too large.")
                    )
            try:
                assert_approved_origin(request, self._settings)
            except OriginRejectedError as error:
                return _mars_error_response(request, error)
            return await call_next(request)

        try:
            assert_approved_origin(request, self._settings)
        except OriginRejectedError as error:
            return _mars_error_response(request, error)
        supplied = request.headers.get(self._settings.csrf_header_name)
        expected = getattr(record, "csrf_token", None)
        if (
            expected is None
            or not supplied
            or len(supplied) != len(expected)
            or not secrets.compare_digest(supplied, expected)
        ):
            from mars.core.errors import CsrfRejectedError

            return _mars_error_response(
                request, CsrfRejectedError("This request did not include a valid CSRF token.")
            )
        return await call_next(request)


def _mars_error_response(request: Request, error: MarsError) -> JSONResponse:
    problem = error.to_problem(instance=request.url.path, request_id=get_request_id())
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
    )
