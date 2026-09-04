"""Separated live-authorization layers.

Authenticated identity, remote DHIS2 authorization, the effective remote
workspace, local MARS mapping and surveillance-data readiness are different
facts. They must not share one "scope" object whose meaning changes when a
local crosswalk row is missing.

A valid remote DHIS2 organisation-unit authorization does not require a local
MARS UUID. Local surveillance queries still do.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from mars.security.source_login import RemoteOrgUnit

SOURCE_DHIS2 = "dhis2"

AuthorizationStatus = Literal["resolved", "unresolved"]
WorkspaceType = Literal["national", "district", "multi_district", "facility", "other", "unresolved"]
MappingStatus = Literal["resolved", "pending", "ambiguous", "rejected"]
ReadinessState = Literal["resolved", "pending", "ambiguous", "ready", "unavailable", "not_started"]
TrackerSyncState = Literal["not_started", "mapping_pending", "not_authorized"]

DHIS2_UID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{10}$")


def is_dhis2_uid(value: str) -> bool:
    """Whether ``value`` matches DHIS2's 11-character UID syntax."""
    return bool(DHIS2_UID_PATTERN.fullmatch(value.strip()))


def parent_uid_from_path(uid: str, path: str | None) -> str | None:
    """Parent UID from a DHIS2 path such as ``/root/region/district``."""
    if not path:
        return None
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    if parts[-1] == uid:
        return parts[-2]
    return parts[-1]


@dataclass(frozen=True, slots=True)
class AuthenticatedSourceIdentity:
    source_system: str
    remote_user_id: str
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RemoteAuthorizationContext:
    """Capture, data-view and Tracker-search scopes kept distinct.

    Dashboard authorization reads ``data_view_scope`` only. Tracker-search
    never widens aggregate visibility, and capture never grants write access.
    """

    capture_scope: tuple[RemoteOrgUnit, ...]
    data_view_scope: tuple[RemoteOrgUnit, ...]
    tracker_search_scope: tuple[RemoteOrgUnit, ...]
    authorities: tuple[str, ...]
    fallback_policy: str
    fallback_used: bool
    fallback_source: str | None
    fallback_reason: str | None
    data_view_field_present: bool


@dataclass(frozen=True, slots=True)
class RemoteWorkspaceScope:
    """Effective remote workspace from DHIS2 data-view authorization.

    Status is ``resolved`` when DHIS2 supplied a usable data-view (or a
    documented compatibility fallback). Mapping into local MARS geography is
    a separate field on :class:`LocalMappingResult`.
    """

    status: AuthorizationStatus
    scope_type: WorkspaceType
    source: str
    external_uid: str | None
    name: str | None
    code: str | None
    level: int | None
    path: str | None
    parent_uid: str | None


@dataclass(frozen=True, slots=True)
class LocalMappingResult:
    status: MappingStatus
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DataReadiness:
    geography: Literal["resolved", "pending", "ambiguous"]
    malaria_metadata: Literal["resolved", "pending"]
    aggregate_sync: Literal["ready", "pending", "unavailable"]
    tracker_sync: TrackerSyncState


@dataclass(frozen=True, slots=True)
class LiveAuthorizationState:
    """Sanitized server-side authorization context for an active live session.

    Credentials are not a field. Pre-refactor sessions that lack this object
    must not be treated as authorized.
    """

    schema_version: int
    identity: AuthenticatedSourceIdentity
    remote_authorization: RemoteAuthorizationContext
    workspace: RemoteWorkspaceScope
    mapping: LocalMappingResult
    readiness: DataReadiness
    landing_path: str


AUTHORIZATION_SCHEMA_VERSION = 1

UNRESOLVED_WORKSPACE = RemoteWorkspaceScope(
    status="unresolved",
    scope_type="unresolved",
    source=SOURCE_DHIS2,
    external_uid=None,
    name=None,
    code=None,
    level=None,
    path=None,
    parent_uid=None,
)

PENDING_MAPPING = LocalMappingResult(
    status="pending",
    geography_unit_id=None,
    facility_id=None,
    evidence=("no confirmed geography_unit_alias or facility identifier",),
)

UNAVAILABLE_READINESS = DataReadiness(
    geography="pending",
    malaria_metadata="pending",
    aggregate_sync="pending",
    tracker_sync="not_started",
)


__all__ = [
    "AUTHORIZATION_SCHEMA_VERSION",
    "DHIS2_UID_PATTERN",
    "PENDING_MAPPING",
    "SOURCE_DHIS2",
    "UNAVAILABLE_READINESS",
    "UNRESOLVED_WORKSPACE",
    "AuthenticatedSourceIdentity",
    "AuthorizationStatus",
    "DataReadiness",
    "LiveAuthorizationState",
    "LocalMappingResult",
    "MappingStatus",
    "RemoteAuthorizationContext",
    "RemoteWorkspaceScope",
    "TrackerSyncState",
    "WorkspaceType",
    "is_dhis2_uid",
    "parent_uid_from_path",
]
