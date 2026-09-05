"""Source-neutral orchestration of a controlled live patient-data preview."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from threading import Lock
from typing import Any

from mars.security.live_session import InMemoryCredentialHolder, hash_session_id

TrackerPreviewRunner = Callable[[str, str, str, date, date], Mapping[str, Any]]


class LiveTrackerPreviewError(RuntimeError):
    """The preview is unavailable or the requested scope is not approved."""


class LiveTrackerPreviewService:
    """Run and retain only non-identifying validation metrics for one session."""

    def __init__(
        self,
        credentials: InMemoryCredentialHolder,
        runner: TrackerPreviewRunner,
    ) -> None:
        self._credentials = credentials
        self._runner = runner
        self._lock = Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}

    def preview(
        self,
        raw_session_id: str,
        *,
        facility_uid: str,
        period_start: date,
        period_end: date,
        approved_facility_uids: frozenset[str],
    ) -> dict[str, Any]:
        if facility_uid not in approved_facility_uids:
            raise LiveTrackerPreviewError(
                "The facility was not proven inside this session's Tracker-search scope"
            )
        if period_end < period_start or (period_end - period_start).days + 1 > 14:
            raise LiveTrackerPreviewError("The controlled preview must cover 1 to 14 days")

        def run(username: str, password: str) -> Mapping[str, Any]:
            return self._runner(username, password, facility_uid, period_start, period_end)

        try:
            result = dict(self._credentials.invoke(raw_session_id, run))
        except KeyError as error:
            raise LiveTrackerPreviewError(
                "The live session no longer has an upstream credential; sign in again"
            ) from error
        if result.get("patient_data_retrieved") is not True:
            raise RuntimeError("Tracker runner did not declare its patient-data boundary")
        snapshot = _safe_snapshot(result)
        with self._lock:
            self._snapshots[hash_session_id(raw_session_id)] = snapshot
        return dict(snapshot)

    def latest(self, raw_session_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._snapshots.get(hash_session_id(raw_session_id))
            return dict(item) if item is not None else None

    def drop(self, raw_session_id: str) -> None:
        with self._lock:
            self._snapshots.pop(hash_session_id(raw_session_id), None)


def _safe_snapshot(result: Mapping[str, Any]) -> dict[str, Any]:
    coverage_raw = result.get("field_coverage")
    coverage = coverage_raw if isinstance(coverage_raw, dict) else {}
    return {
        "status": str(result.get("status", "completed")),
        "facility_uid": str(result.get("facility_uid", "")),
        "period_start": result.get("period_start"),
        "period_end": result.get("period_end"),
        "retrieved_event_count": int(result.get("retrieved_event_count", 0)),
        "unique_patient_count": int(result.get("unique_patient_count", 0)),
        "loadable_event_count": int(result.get("loadable_event_count", 0)),
        "invalid_event_count": int(result.get("invalid_event_count", 0)),
        "positive_event_count": int(result.get("positive_event_count", 0)),
        "field_coverage": {
            str(key): int(value)
            for key, value in coverage.items()
            if isinstance(key, str) and isinstance(value, int)
        },
        "mapping_schema_version": str(result.get("mapping_schema_version", "")),
        "patient_data_retrieved": True,
        "patient_rows_returned": False,
        "persisted": False,
    }


__all__ = ["LiveTrackerPreviewError", "LiveTrackerPreviewService", "TrackerPreviewRunner"]
