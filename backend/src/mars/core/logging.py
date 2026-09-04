"""Structured logging.

Two rules govern this module:

1. Every log record carries the request identifier, so a line in production can
   be traced back to the call that produced it.
2. Nothing that could identify a patient is ever logged. A redaction processor
   drops known-sensitive keys rather than trusting call sites to remember.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from mars.core.context import get_actor_id, get_request_id, get_session_id
from mars.core.settings import Settings

# Keys that must never reach a log sink. Blueprint sections 021 and 065:
# application logs carry no names, contact details or national identifiers.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "patient_name",
        "full_name",
        "first_name",
        "last_name",
        "surname",
        "nin",
        "national_id",
        "phone",
        "phone_number",
        "telephone",
        "contact",
        "next_of_kin",
        "next_of_kin_name",
        "next_of_kin_phone",
        "address",
        "village_text",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "client_secret",
        "dhis2_password",
        "current_password",
        "basic_auth",
        "cookie",
        "set_cookie",
        "mars_session",
    }
)

REDACTED_PLACEHOLDER = "[redacted]"


def redact_sensitive(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Replace the value of any known-sensitive key.

    The key is kept so that the shape of the record stays diagnosable; only the
    value is removed.
    """
    for key in list(event_dict.keys()):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = REDACTED_PLACEHOLDER
    return event_dict


def bind_request_context(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Attach the ambient request, session and actor identifiers."""
    request_id = get_request_id()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    session_id = get_session_id()
    if session_id is not None:
        event_dict.setdefault("session_id", session_id)
    actor_id = get_actor_id()
    if actor_id is not None:
        event_dict.setdefault("actor_id", actor_id)
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the standard library root logger."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        bind_request_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_sensitive,
    ]

    renderer: Processor
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelNamesMapping()[settings.log_level],
        force=True,
    )
    # Uvicorn duplicates access logging; the middleware emits a richer record.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
