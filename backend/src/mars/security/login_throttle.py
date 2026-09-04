"""Conservative login throttling.

Failures are counted by client address and a non-reversible HMAC of the
username. The username itself is not stored on the throttle record.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from threading import Lock
from time import monotonic


class LoginThrottle:
    def __init__(self, *, max_attempts: int, window_seconds: int, secret: str) -> None:
        self._max_attempts = max_attempts
        self._window = float(window_seconds)
        self._secret = secret.encode("utf-8")
        self._lock = Lock()
        self._failures: dict[str, list[float]] = {}

    def username_key(self, username: str) -> str:
        return hmac.new(self._secret, username.strip().lower().encode("utf-8"), sha256).hexdigest()

    def is_blocked(self, *, client_key: str, username_key: str) -> bool:
        self._prune()
        key = self._bucket(client_key, username_key)
        with self._lock:
            stamps = self._failures.get(key, [])
            return len(stamps) >= self._max_attempts

    def record_failure(self, *, client_key: str, username_key: str) -> None:
        key = self._bucket(client_key, username_key)
        now = monotonic()
        with self._lock:
            stamps = self._failures.setdefault(key, [])
            stamps.append(now)

    def clear(self, *, client_key: str, username_key: str) -> None:
        key = self._bucket(client_key, username_key)
        with self._lock:
            self._failures.pop(key, None)

    def _bucket(self, client_key: str, username_key: str) -> str:
        return f"{client_key}:{username_key}"

    def _prune(self) -> None:
        cutoff = monotonic() - self._window
        with self._lock:
            stale = [
                key
                for key, stamps in self._failures.items()
                if not any(stamp >= cutoff for stamp in stamps)
            ]
            for key in stale:
                self._failures.pop(key, None)
            for key, stamps in list(self._failures.items()):
                kept = [stamp for stamp in stamps if stamp >= cutoff]
                if kept:
                    self._failures[key] = kept
                else:
                    self._failures.pop(key, None)


__all__ = ["LoginThrottle"]
