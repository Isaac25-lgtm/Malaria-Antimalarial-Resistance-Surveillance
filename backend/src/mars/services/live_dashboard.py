"""Session-scoped orchestration and cache for live dashboard reads."""

from __future__ import annotations

import csv
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import date
from io import StringIO
from threading import Lock
from typing import Any

from mars.security.live_session import InMemoryCredentialHolder, hash_session_id

LiveDashboardRunner = Callable[
    [str, str, Sequence[Mapping[str, Any]], date, date], Mapping[str, Any]
]


class LiveDashboardError(RuntimeError):
    """A live dashboard request was unsafe, unscoped, or unavailable."""


class LiveDashboardConfigurationError(LiveDashboardError):
    """Local live-mode configuration is missing or unsafe."""


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
        self._period_snapshots: dict[tuple[str, date, date], dict[str, Any]] = {}
        self._running: set[str] = set()

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

        key = hash_session_id(raw_session_id)
        with self._lock:
            if key in self._running:
                raise LiveDashboardError("A synchronization is already running for this session")
            self._running.add(key)
        try:
            try:
                result = dict(self._credentials.invoke(raw_session_id, run))
            except KeyError as error:
                raise LiveDashboardError(
                    "The live session no longer has an upstream credential; sign in again"
                ) from error
            if result.get("synthetic_data_used") is not False:
                raise RuntimeError(
                    "The live dashboard runner did not prove the no-synthetic boundary"
                )
            with self._lock:
                if not self._credentials.has(raw_session_id):
                    raise LiveDashboardError(
                        "The session ended during synchronization; sign in again"
                    )
                self._snapshots[key] = deepcopy(result)
                self._period_snapshots[(key, period_start, period_end)] = deepcopy(result)
            return deepcopy(result)
        finally:
            with self._lock:
                self._running.discard(key)

    def latest(
        self,
        raw_session_id: str,
        *,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            key = hash_session_id(raw_session_id)
            snapshot = (
                self._period_snapshots.get((key, period_start, period_end))
                if period_start is not None and period_end is not None
                else self._snapshots.get(key)
            )
            return deepcopy(snapshot) if snapshot is not None else None

    def drop(self, raw_session_id: str) -> None:
        with self._lock:
            key = hash_session_id(raw_session_id)
            self._snapshots.pop(key, None)
            for period_key in list(self._period_snapshots):
                if period_key[0] == key:
                    self._period_snapshots.pop(period_key, None)


def snapshot_csv(snapshot: Mapping[str, Any]) -> str:
    """Export scoped aggregate figures only; no patient evidence in a brief."""
    output = StringIO(newline="")
    writer = csv.writer(output)

    def safe(value: Any) -> str:
        text = "" if value is None else str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

    writer.writerow(["Scope", "Period start", "Period end", "Synchronized", "Status"])
    writer.writerow(
        [
            safe(snapshot.get(key))
            for key in ("scope", "period_start", "period_end", "synchronized_at", "status")
        ]
    )
    writer.writerow(["Measure", "Value", "Source", "Availability"])
    for item in snapshot.get("kpis", []):
        writer.writerow([safe(item.get(key)) for key in ("label", "value", "source", "status")])
    writer.writerow(
        [
            "Facility",
            "Confirmed",
            "Tested",
            "RDT stock-out days",
            "AL stock-out days",
            "Artesunate stock-out days",
        ]
    )
    for item in snapshot.get("facilities", []):
        writer.writerow(
            [
                safe(item.get(key))
                for key in (
                    "name",
                    "confirmed_malaria",
                    "tested_for_malaria",
                    "rdt_days_out_of_stock",
                    "al_days_out_of_stock",
                    "artesunate_days_out_of_stock",
                )
            ]
        )
    for warning in snapshot.get("warnings", []):
        writer.writerow(["Coverage warning", safe(warning)])
    writer.writerow(
        [
            "Interpretation",
            "MARS signals require investigation and do not confirm antimalarial resistance.",
        ]
    )
    return output.getvalue()


__all__ = [
    "LiveDashboardConfigurationError",
    "LiveDashboardError",
    "LiveDashboardRunner",
    "LiveDashboardService",
    "snapshot_csv",
]
