"""Opaque live sessions and the in-memory DHIS2 credential holder.

Pilot limitation, stated plainly:

* Session records and upstream credentials live in **this process only**.
  A second API worker cannot see them. Restarting the process signs everyone
  out. This is acceptable for a single-process local pilot and is not a
  production session store.
* Credentials are never written to PostgreSQL, a file, Redis, a cookie, a
  JWT, an API response or a log.
* Python strings cannot be reliably zeroed. The holder drops references on
  logout and expiry so the values become unreachable; it does not claim to
  wipe the bytes from memory.

The session retains a sanitized remote-authorization context so MARS can
distinguish capture, data-view and Tracker-search scopes after login. That
context contains no credentials. Sessions created before this schema are
invalid and require a fresh login.

Replace ``InMemoryCredentialHolder`` with an approved encrypted store, and
``InMemorySessionStore`` with a shared store, when the Ministry identity
path is ready. Callers depend on the interfaces below, not the dicts.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TypeVar

from mars.security.principal import AuthenticatedPrincipal
from mars.security.remote_authorization import (
    AUTHORIZATION_SCHEMA_VERSION,
    LiveAuthorizationState,
)

SESSION_ID_BYTES = 32  # 256 bits of entropy
CSRF_TOKEN_BYTES = 32
_T = TypeVar("_T")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def hash_session_id(raw_session_id: str) -> str:
    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()


def new_session_id() -> str:
    return secrets.token_urlsafe(SESSION_ID_BYTES)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


@dataclass(frozen=True, slots=True)
class LiveSession:
    """Server-side session. The cookie carries the raw id, never this record."""

    id_hash: str
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    idle_expires_at: datetime
    principal: AuthenticatedPrincipal
    authorization: LiveAuthorizationState
    source_status: str

    @property
    def mapping_status(self) -> str:
        return self.authorization.mapping.status

    @property
    def scope_type(self) -> str:
        return self.authorization.workspace.scope_type

    @property
    def is_current_schema(self) -> bool:
        return self.authorization.schema_version == AUTHORIZATION_SCHEMA_VERSION


class InMemoryCredentialHolder:
    """Associates an active session with DHIS2 Basic credentials.

    Keys are raw session identifiers, which exist only in this process and in
    the caller's HttpOnly cookie. Values are never copied into a log, a
    response, or a database row by this class.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[str, tuple[str, str]] = {}

    def store(self, session_id: str, username: str, password: str) -> None:
        with self._lock:
            self._items[session_id] = (username, password)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._items.pop(session_id, None)

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._items

    def invoke(self, session_id: str, operation: Callable[[str, str], _T]) -> _T:
        """Run one server-side operation with a session's upstream credentials.

        The tuple is copied while holding the lock and is never returned to the
        caller.  This is the only escape hatch needed by live source adapters:
        routes and services receive the operation result, never the username or
        password.  The network operation deliberately runs outside the lock so
        logout and unrelated sessions are not blocked by a slow upstream call.
        """
        with self._lock:
            credentials = self._items.get(session_id)
        if credentials is None:
            raise KeyError("No live upstream credentials exist for this session")
        username, password = credentials
        try:
            return operation(username, password)
        finally:
            username = ""
            password = ""

    def transfer(self, old_session_id: str, new_session_id: str) -> None:
        """Move credentials to a rotated session id and drop the old key."""
        with self._lock:
            value = self._items.pop(old_session_id, None)
            if value is None:
                return
            self._items[new_session_id] = value

    def stored_session_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._items)


class InMemorySessionStore:
    """Hashed opaque sessions. The raw identifier is never stored."""

    def __init__(self, *, idle_seconds: int, absolute_seconds: int) -> None:
        self._idle = timedelta(seconds=idle_seconds)
        self._absolute = timedelta(seconds=absolute_seconds)
        self._lock = Lock()
        self._sessions: dict[str, LiveSession] = {}

    def create(
        self,
        principal: AuthenticatedPrincipal,
        authorization: LiveAuthorizationState,
        *,
        source_status: str = "connected",
        now: datetime | None = None,
    ) -> tuple[str, LiveSession]:
        """Return the raw session id (for the cookie) and the stored record."""
        moment = now or _utc_now()
        raw_id = new_session_id()
        record = LiveSession(
            id_hash=hash_session_id(raw_id),
            csrf_token=new_csrf_token(),
            created_at=moment,
            last_seen_at=moment,
            expires_at=moment + self._absolute,
            idle_expires_at=moment + self._idle,
            principal=principal,
            authorization=authorization,
            source_status=source_status,
        )
        with self._lock:
            self._sessions[record.id_hash] = record
        return raw_id, record

    def get(self, raw_session_id: str, *, now: datetime | None = None) -> LiveSession | None:
        moment = now or _utc_now()
        digest = hash_session_id(raw_session_id)
        with self._lock:
            record = self._sessions.get(digest)
            if record is None:
                return None
            if not record.is_current_schema:
                self._sessions.pop(digest, None)
                return None
            if moment >= record.expires_at or moment >= record.idle_expires_at:
                self._sessions.pop(digest, None)
                return None
            refreshed = LiveSession(
                id_hash=record.id_hash,
                csrf_token=record.csrf_token,
                created_at=record.created_at,
                last_seen_at=moment,
                expires_at=record.expires_at,
                idle_expires_at=moment + self._idle,
                principal=record.principal,
                authorization=record.authorization,
                source_status=record.source_status,
            )
            self._sessions[digest] = refreshed
            return refreshed

    def rotate(
        self,
        raw_session_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, LiveSession] | None:
        """Issue a new raw id for the same principal; drop the previous hash."""
        current = self.get(raw_session_id, now=now)
        if current is None:
            return None
        self.invalidate(raw_session_id)
        return self.create(
            current.principal,
            current.authorization,
            source_status=current.source_status,
            now=now,
        )

    def invalidate(self, raw_session_id: str) -> None:
        with self._lock:
            self._sessions.pop(hash_session_id(raw_session_id), None)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        moment = now or _utc_now()
        removed = 0
        with self._lock:
            stale = [
                digest
                for digest, record in self._sessions.items()
                if moment >= record.expires_at
                or moment >= record.idle_expires_at
                or not record.is_current_schema
            ]
            for digest in stale:
                self._sessions.pop(digest, None)
                removed += 1
        return removed

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


__all__ = [
    "CSRF_TOKEN_BYTES",
    "SESSION_ID_BYTES",
    "InMemoryCredentialHolder",
    "InMemorySessionStore",
    "LiveSession",
    "hash_session_id",
    "new_csrf_token",
    "new_session_id",
]
