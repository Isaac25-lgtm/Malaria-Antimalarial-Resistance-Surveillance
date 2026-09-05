"""Authenticated, metadata-only source discovery orchestration.

The service knows nothing about DHIS2 or HTTP.  A wiring-layer callback performs
the source-specific work and must return an already-sanitized report.  Keeping
that callback outside ``mars.services`` preserves ADR 0003 while allowing a
live user's in-memory credential to drive the same safe discovery utility used
by the command line.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from mars.security.live_session import InMemoryCredentialHolder, hash_session_id

DiscoveryRunner = Callable[[str, str], Mapping[str, Any]]


class LiveDiscoveryUnavailableError(RuntimeError):
    """The session or live discovery wiring is unavailable."""


class LiveMetadataDiscoveryService:
    """Run and retain a sanitized metadata snapshot for one live session."""

    def __init__(
        self,
        credentials: InMemoryCredentialHolder,
        runner: DiscoveryRunner,
    ) -> None:
        self._credentials = credentials
        self._runner = runner
        self._lock = Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}

    def discover(self, raw_session_id: str) -> dict[str, Any]:
        try:
            report = dict(self._credentials.invoke(raw_session_id, self._runner))
        except KeyError as error:
            raise LiveDiscoveryUnavailableError(
                "The live session no longer has an upstream credential; sign in again."
            ) from error
        if report.get("stop_before_patient_data") is not True:
            raise RuntimeError("metadata runner did not prove the patient-data stop boundary")
        snapshot = _summary(report)
        with self._lock:
            self._snapshots[hash_session_id(raw_session_id)] = snapshot
        return dict(snapshot)

    def latest(self, raw_session_id: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(hash_session_id(raw_session_id))
            return dict(snapshot) if snapshot is not None else None

    def drop(self, raw_session_id: str) -> None:
        with self._lock:
            self._snapshots.pop(hash_session_id(raw_session_id), None)

    def tracker_facility_uids(self, raw_session_id: str) -> frozenset[str]:
        """Exact facility UIDs proven by this session's metadata discovery."""
        snapshot = self.latest(raw_session_id)
        if snapshot is None:
            return frozenset()
        facilities = snapshot.get("tracker_facilities")
        if not isinstance(facilities, list):
            return frozenset()
        return frozenset(
            item["id"]
            for item in facilities
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )

    def tracker_facilities(self, raw_session_id: str) -> list[dict[str, Any]]:
        """Sanitized facility identifiers and labels proven in Tracker scope."""
        snapshot = self.latest(raw_session_id)
        if snapshot is None:
            return []
        facilities = snapshot.get("tracker_facilities")
        if not isinstance(facilities, list):
            return []
        return [dict(item) for item in facilities if isinstance(item, dict)]


def _summary(report: Mapping[str, Any]) -> dict[str, Any]:
    mappings = report.get("candidate_mappings")
    candidates = (
        [item for item in mappings if isinstance(item, dict)] if isinstance(mappings, list) else []
    )
    programmes = report.get("programmes")
    stages = report.get("program_stages")
    data_elements = report.get("data_elements")
    facilities = report.get("accessible_facilities")
    tracker_scope = report.get("tracker_search_organisation_units")
    system_raw = report.get("system")
    system: dict[str, Any] = system_raw if isinstance(system_raw, dict) else {}
    files_raw = report.get("report_files")
    files: dict[str, Any] = files_raw if isinstance(files_raw, dict) else {}
    unresolved = report.get("unresolved_questions")
    errors = report.get("errors")
    tracker_facilities = _tracker_facilities(facilities, tracker_scope)
    return {
        "status": "completed",
        "generated_at": report.get("generated_at"),
        "dhis2_version": system.get("version"),
        "api_generation": report.get("api_generation", "indeterminate"),
        "programme_count": len(programmes) if isinstance(programmes, list) else 0,
        "program_stage_count": len(stages) if isinstance(stages, list) else 0,
        "data_element_count": len(data_elements) if isinstance(data_elements, list) else 0,
        "candidate_mapping_count": len(candidates),
        "accessible_facility_count": (
            len(facilities)
            if isinstance(facilities, list)
            else report.get("accessible_facility_count")
        ),
        "tracker_scope_root_count": len(tracker_scope) if isinstance(tracker_scope, list) else 0,
        "tracker_facilities": tracker_facilities,
        "unresolved_questions": [str(item) for item in unresolved]
        if isinstance(unresolved, list)
        else [],
        "errors": [str(item) for item in errors] if isinstance(errors, list) else [],
        "json_report": files.get("json"),
        "markdown_report": files.get("markdown"),
        "patient_data_retrieved": False,
    }


def _tracker_facilities(facilities: Any, tracker_scope: Any) -> list[dict[str, Any]]:
    if not isinstance(facilities, list) or not isinstance(tracker_scope, list):
        return []
    roots = [
        item for item in tracker_scope if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    result: list[dict[str, Any]] = []
    for facility in facilities:
        if not isinstance(facility, dict) or not isinstance(facility.get("id"), str):
            continue
        facility_path = facility.get("path")
        covered = any(
            facility["id"] == root["id"]
            or (
                isinstance(facility_path, str)
                and isinstance(root.get("path"), str)
                and facility_path.startswith(f"{root['path'].rstrip('/')}/")
            )
            for root in roots
        )
        if covered:
            result.append(
                {
                    "id": facility["id"],
                    "name": str(facility.get("name")) if facility.get("name") else None,
                    "code": str(facility.get("code")) if facility.get("code") else None,
                    "parent_id": (
                        str(facility.get("parent_id")) if facility.get("parent_id") else None
                    ),
                    "path": str(facility.get("path")) if facility.get("path") else None,
                    "latitude": facility.get("latitude"),
                    "longitude": facility.get("longitude"),
                }
            )
    return sorted(result, key=lambda item: ((item["name"] or "").casefold(), item["id"]))


__all__ = ["LiveDiscoveryUnavailableError", "LiveMetadataDiscoveryService"]
