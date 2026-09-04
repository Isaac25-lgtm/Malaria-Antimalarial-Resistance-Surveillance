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

Replace ``InMemoryCredentialHolder`` with an approved encrypted store, and
``InMemorySessionStore`` with a shared store, when the Ministry identity
path is ready. Callers depend on the interfaces below, not the dicts.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from mars.security.principal import AuthenticatedPrincipal

SESSION_ID_BYTES = 32  # 256 bits of entropy
CSRF_TOKEN_BYTES = 32


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
    mapping_status: str
    source_status: str
    scope_type: str


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
        *,
        mapping_status: str,
        source_status: str,
        scope_type: str,
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
            mapping_status=mapping_status,
            source_status=source_status,
            scope_type=scope_type,
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
                mapping_status=record.mapping_status,
                source_status=record.source_status,
                scope_type=record.scope_type,
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
            mapping_status=current.mapping_status,
            source_status=current.source_status,
            scope_type=current.scope_type,
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
                if moment >= record.expires_at or moment >= record.idle_expires_at
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
