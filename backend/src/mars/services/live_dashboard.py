"""Session-scoped orchestration and cache for live dashboard reads."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from threading import Lock
from typing import Any

from mars.security.live_session import InMemoryCredentialHolder, hash_session_id

LiveDashboardRunner = Callable[
    [str, str, Sequence[Mapping[str, Any]], date, date], Mapping[str, Any]
]


class LiveDashboardError(RuntimeError):
    """A live dashboard request was unsafe, unscoped, or unavailable."""


class LiveDashboardService:
    def __init__(
        self,
        credentials: InMemoryCredentialHolder,
        runner: LiveDashboardRunner,
    ) -> None:
        self._credentials = credentials
        self._runner = runner
        self._lock = Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}

    def synchronize(
        self,
        raw_session_id: str,
        *,
        facilities: Sequence[Mapping[str, Any]],
        period_start: date,
        period_end: date,
    ) -> dict[str, Any]:
        days = (period_end - period_start).days + 1
        if days < 1 or days > 62:
            raise LiveDashboardError("A live dashboard synchronization must cover 1 to 62 days")
        if not facilities:
            raise LiveDashboardError("No Tracker-authorised facility scope was discovered")
        if len(facilities) > 500:
            raise LiveDashboardError("The discovered facility scope exceeds the pilot bound")

        def run(username: str, password: str) -> Mapping[str, Any]:
            return self._runner(username, password, facilities, period_start, period_end)

        try:
            result = dict(self._credentials.invoke(raw_session_id, run))
        except KeyError as error:
            raise LiveDashboardError(
                "The live session no longer has an upstream credential; sign in again"
            ) from error
        if result.get("synthetic_data_used") is not False:
            raise RuntimeError("The live dashboard runner did not prove the no-synthetic boundary")
        key = hash_session_id(raw_session_id)
        with self._lock:
            self._snapshots[key] = result
        return dict(result)

    def latest(self, raw_session_id: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(hash_session_id(raw_session_id))
            return dict(snapshot) if snapshot is not None else None

    def drop(self, raw_session_id: str) -> None:
        with self._lock:
            self._snapshots.pop(hash_session_id(raw_session_id), None)


__all__ = ["LiveDashboardError", "LiveDashboardRunner", "LiveDashboardService"]
