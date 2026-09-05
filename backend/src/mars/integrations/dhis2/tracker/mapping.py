"""Strict approved metadata mapping for DHIS2 Tracker events.

No UID or option code is inferred here. A human-reviewed JSON document must
name the programme, stage and every supported MARS concept. Nullable concepts
must still be present, so omission cannot be confused with an approved absence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_UID = re.compile(r"^[A-Za-z][A-Za-z0-9]{10}$")
_CONCEPTS = (
    "fever_present",
    "malaria_test_method",
    "malaria_test_result",
    "diagnosis",
    "treatment",
    "referral",
    "attendance_type",
    "age_value",
    "age_unit",
    "sex",
    "patient_category",
)


class TrackerMappingError(ValueError):
    """The mapping is absent, unapproved or structurally unsafe."""


@dataclass(frozen=True, slots=True)
class ApprovedTrackerMapping:
    schema_version: str
    programme_uid: str
    program_stage_uid: str
    data_elements: dict[str, str | None]
    option_maps: dict[str, dict[str, str]]
    approved_by: str
    approved_at: datetime
    source_sha256: str | None = None

    def data_element_for(self, concept: str) -> str | None:
        return self.data_elements.get(concept)

    def canonical_value(self, concept: str, source_value: str | None) -> str | None:
        if source_value is None or not source_value.strip():
            return None
        mapping = self.option_maps.get(concept)
        if mapping is None:
            return source_value.strip()
        try:
            return mapping[source_value]
        except KeyError as exc:
            raise TrackerMappingError(
                f"The approved {concept} option map does not contain the received source code"
            ) from exc


def load_approved_tracker_mapping(path: Path) -> ApprovedTrackerMapping:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrackerMappingError("The Tracker mapping file could not be read as JSON") from exc
    if not isinstance(raw, dict):
        raise TrackerMappingError("The Tracker mapping must be a JSON object")
    if raw.get("schema_version") != "1.0":
        raise TrackerMappingError("Only Tracker mapping schema_version 1.0 is supported")
    if raw.get("status") != "approved":
        raise TrackerMappingError("Tracker data cannot be retrieved from an unapproved mapping")

    programme_uid = _required_uid(raw.get("programme_uid"), "programme_uid")
    stage_uid = _required_uid(raw.get("program_stage_uid"), "program_stage_uid")
    elements_raw = raw.get("data_elements")
    if not isinstance(elements_raw, dict) or set(elements_raw) != set(_CONCEPTS):
        raise TrackerMappingError(
            "data_elements must name every supported concept exactly; use null for an "
            "approved absence"
        )
    elements: dict[str, str | None] = {}
    seen: set[str] = set()
    for concept in _CONCEPTS:
        value = elements_raw[concept]
        if value is None:
            elements[concept] = None
            continue
        uid = _required_uid(value, f"data_elements.{concept}")
        if uid in seen:
            raise TrackerMappingError("One data element UID cannot represent two MARS concepts")
        seen.add(uid)
        elements[concept] = uid

    option_maps = _option_maps(raw.get("option_maps"), elements)
    approved_by = raw.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise TrackerMappingError("approved_by is required")
    try:
        approved_at = datetime.fromisoformat(str(raw["approved_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise TrackerMappingError("approved_at must be an ISO-8601 timestamp") from exc
    source_sha256 = raw.get("discovery_report_sha256")
    if source_sha256 is not None and (
        not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
    ):
        raise TrackerMappingError("discovery_report_sha256 must be a lowercase SHA-256 digest")
    return ApprovedTrackerMapping(
        schema_version="1.0",
        programme_uid=programme_uid,
        program_stage_uid=stage_uid,
        data_elements=elements,
        option_maps=option_maps,
        approved_by=approved_by.strip(),
        approved_at=approved_at,
        source_sha256=source_sha256,
    )


def _required_uid(value: Any, field: str) -> str:
    if not isinstance(value, str) or _UID.fullmatch(value) is None:
        raise TrackerMappingError(f"{field} must be an 11-character DHIS2 UID")
    return value


def _option_maps(value: Any, elements: dict[str, str | None]) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not set(value).issubset(_CONCEPTS):
        raise TrackerMappingError("option_maps contains an unsupported concept")
    result: dict[str, dict[str, str]] = {}
    for concept, mapping in value.items():
        if elements[concept] is None:
            raise TrackerMappingError(f"option_maps.{concept} has no mapped data element")
        if not isinstance(mapping, dict) or not mapping:
            raise TrackerMappingError(f"option_maps.{concept} must be a non-empty object")
        converted: dict[str, str] = {}
        for source, canonical in mapping.items():
            if not isinstance(source, str) or not source or not isinstance(canonical, str):
                raise TrackerMappingError(f"option_maps.{concept} must map strings to strings")
            converted[source] = canonical
        result[concept] = converted
    return result


__all__ = [
    "ApprovedTrackerMapping",
    "TrackerMappingError",
    "load_approved_tracker_mapping",
]
