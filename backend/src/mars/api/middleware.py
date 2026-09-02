"""HTTP middleware: request identity and access logging."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from mars.core.context import clear_context, get_request_id, new_request_id, set_request_id
from mars.core.logging import get_logger
from mars.core.settings import Settings

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
