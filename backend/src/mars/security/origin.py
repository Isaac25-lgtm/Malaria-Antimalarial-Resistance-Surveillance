"""Origin and Referer checks for cookie-authenticated requests."""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request

from mars.core.errors import OriginRejectedError
from mars.core.settings import Settings


def request_origin(request: Request) -> str | None:
    """The caller origin, from Origin or, failing that, Referer."""
    origin = request.headers.get("origin")
    if origin:
        return _origin_of(origin)
    referer = request.headers.get("referer")
    if referer:
        return _origin_of(referer)
    return None


def assert_approved_origin(request: Request, settings: Settings) -> str:
    """Return the approved origin or raise :class:`OriginRejectedError`."""
    origin = request_origin(request)
    if origin is None or origin not in settings.cors_allow_origins:
        raise OriginRejectedError("This request was not made from an approved MARS origin.")
    return origin


def _origin_of(value: str) -> str | None:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port:
        return f"{parts.scheme}://{host}:{parts.port}"
    return f"{parts.scheme}://{host}"


__all__ = ["assert_approved_origin", "request_origin"]
