"""Per-request context.

Carries the request and session identifiers that every log line and every audit
event must reference, without threading them through call signatures.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_request_id: ContextVar[str | None] = ContextVar("mars_request_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("mars_session_id", default=None)
_actor_id: ContextVar[str | None] = ContextVar("mars_actor_id", default=None)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Snapshot of the ambient request context."""

    request_id: str | None
    session_id: str | None
    actor_id: str | None


def new_request_id() -> str:
    return str(uuid.uuid4())


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def set_session_id(value: str | None) -> None:
    _session_id.set(value)


def get_session_id() -> str | None:
    return _session_id.get()


def set_actor_id(value: str | None) -> None:
    """Record who is acting.

    Only the internal user UUID is stored. Names, national identity numbers and
    contact details must never enter the logging or audit context.
    """
    _actor_id.set(value)


def get_actor_id() -> str | None:
    return _actor_id.get()


def current_context() -> RequestContext:
    return RequestContext(
        request_id=_request_id.get(),
        session_id=_session_id.get(),
        actor_id=_actor_id.get(),
    )


def clear_context() -> None:
    _request_id.set(None)
    _session_id.set(None)
    _actor_id.set(None)
