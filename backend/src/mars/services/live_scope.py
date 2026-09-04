"""Remote DHIS2 authorization, then local MARS mapping, then data readiness.

These are sequential, separate facts:

1. Authenticated source identity.
2. Remote authorization (capture, data-view, Tracker-search kept distinct).
3. Effective remote workspace classified from data-view units.
4. Local geography/facility mapping against confirmed crosswalks.
5. Surveillance-data readiness.

Dashboard authorization uses ``dataViewOrganisationUnits``. Tracker-search
never widens aggregate visibility. Capture never grants write access and is
not a silent substitute for an empty data-view list.

A remote workspace can be resolved without a local ``GeographyUnitAlias``.
Local surveillance queries still require a confirmed mapping. Usernames never
enter any of these decisions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.domain.enums import AliasMatchStatus, GeographyLevel
from mars.domain.geography import GeographyUnit, GeographyUnitAlias
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal, GeographyScope
from mars.security.remote_authorization import (
    AUTHORIZATION_SCHEMA_VERSION,
    PENDING_MAPPING,
    SOURCE_DHIS2,
    UNRESOLVED_WORKSPACE,
    AuthenticatedSourceIdentity,
    DataReadiness,
    LiveAuthorizationState,
    LocalMappingResult,
    RemoteAuthorizationContext,
    RemoteWorkspaceScope,
    is_dhis2_uid,
    parent_uid_from_path,
)
from mars.security.source_login import LoginSnapshot, RemoteOrgUnit, RemoteOrgUnitLevel

ScopeType = Literal["national", "district", "multi_district", "facility", "other", "unresolved"]

DHIS2_SUBJECT_NAMESPACE = uuid.UUID("8f2c1d6a-4b7e-4c91-9a33-0d6e5b8f1a70")

LIVE_BASE_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.SURVEILLANCE_VIEW_AGGREGATE,
        Permission.GEOGRAPHY_VIEW,
        Permission.ORGANISATION_VIEW,
        Permission.FACILITY_VIEW,
        Permission.DATA_QUALITY_VIEW,
    }
)
LIVE_INVESTIGATION_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.INVESTIGATION_TRIAGE,
        Permission.INVESTIGATION_ASSIGN,
        Permission.INVESTIGATION_UPDATE,
        Permission.INVESTIGATION_CLOSE,
        Permission.REPORT_GENERATE,
    }
)

_LEVEL_TOKENS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("country", "national"), "country"),
    (("region",), "region"),
    (("district",), "district"),
    (("county", "hsd", "health sub"), "county"),
    (("subcounty", "sub-county", "sub county"), "subcounty"),
    (("facility", "clinic", "hospital", "health centre", "health center"), "facility"),
)

_FALLBACK_POLICY = (
    "Use dataViewOrganisationUnits when present and non-empty. "
    "If the field is present but empty, do not substitute capture or Tracker "
    "scope. If the field is absent from /api/me, a compatibility fallback may "
    "classify capture organisationUnits. teiSearchOrganisationUnits is never "
    "a dashboard fallback."
)


class GeographyLookup(Protocol):
    def resolve_organisation_unit(self, remote_id: str) -> MappedRemoteUnit: ...

    def geography_scope_for(self, unit_id: uuid.UUID) -> GeographyScope | None: ...

    def geography_scope_by_code(self, code: str, level: str) -> GeographyScope | None: ...

    def unconfirmed_code_candidates(self, code: str, level: str) -> tuple[GeographyScope, ...]: ...

    def record_unresolved_mapping(
        self,
        *,
        remote_id: str,
        remote_name: str | None,
        remote_parent_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MappedRemoteUnit:
    """A remote organisation-unit identifier mapped onto MARS geography."""

    geography_unit_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ResolvedLiveScope:
    """Resolved live access: remote workspace plus local mapping.

    ``scope_type`` is the remote workspace type. ``mapping_status`` is the
    local crosswalk result. They are not the same field.
    """

    identity: AuthenticatedSourceIdentity
    remote_authorization: RemoteAuthorizationContext
    workspace: RemoteWorkspaceScope
    mapping: LocalMappingResult
    readiness: DataReadiness
    geography_scopes: tuple[GeographyScope, ...]
    facility_scopes: frozenset[uuid.UUID]
    landing_path: str

    @property
    def scope_type(self) -> ScopeType:
        return self.workspace.scope_type

    @property
    def mapping_status(self) -> str:
        return self.mapping.status

    @property
    def national_access(self) -> bool:
        return self.workspace.scope_type == "national"

    @property
    def org_unit_id(self) -> uuid.UUID | None:
        return self.mapping.geography_unit_id or self.mapping.facility_id

    @property
    def org_unit_name(self) -> str | None:
        return self.workspace.name

    @property
    def authorised_districts(self) -> tuple[GeographyScope, ...]:
        return tuple(scope for scope in self.geography_scopes if scope.level == "district")

    def authorization_state(self) -> LiveAuthorizationState:
        return LiveAuthorizationState(
            schema_version=AUTHORIZATION_SCHEMA_VERSION,
            identity=self.identity,
            remote_authorization=self.remote_authorization,
            workspace=self.workspace,
            mapping=self.mapping,
            readiness=self.readiness,
            landing_path=self.landing_path,
        )


class RemoteIdResolver(Protocol):
    def resolve_organisation_unit(self, remote_id: str) -> object: ...


class SqlAlchemyGeographyLookup:
    """Looks remote identifiers up through confirmed crosswalks and geography rows."""

    def __init__(self, session: Session, resolver: RemoteIdResolver) -> None:
        self._session = session
        self._resolver = resolver

    def resolve_organisation_unit(self, remote_id: str) -> MappedRemoteUnit:
        resolved = self._resolver.resolve_organisation_unit(remote_id)
        return MappedRemoteUnit(
            geography_unit_id=getattr(resolved, "geography_unit_id", None),
            facility_id=getattr(resolved, "facility_id", None),
        )

    def geography_scope_for(self, unit_id: uuid.UUID) -> GeographyScope | None:
        unit = self._session.get(GeographyUnit, unit_id)
        if unit is None or not unit.is_active:
            return None
        return _scope_from_unit(unit)

    def geography_scope_by_code(self, code: str, level: str) -> GeographyScope | None:
        rows = self._code_level_rows(code, level)
        if len(rows) != 1:
            return None
        confirmed = self._session.execute(
            select(GeographyUnitAlias.id).where(
                GeographyUnitAlias.geography_unit_id == rows[0].id,
                GeographyUnitAlias.source_system == "dhis2",
                GeographyUnitAlias.source_code == code,
                GeographyUnitAlias.match_status == AliasMatchStatus.CONFIRMED,
            )
        ).first()
        if confirmed is None:
            return None
        return _scope_from_unit(rows[0])

    def unconfirmed_code_candidates(self, code: str, level: str) -> tuple[GeographyScope, ...]:
        """Local units sharing a preferred code and level. Not an authorization grant."""
        return tuple(_scope_from_unit(row) for row in self._code_level_rows(code, level))

    def record_unresolved_mapping(
        self,
        *,
        remote_id: str,
        remote_name: str | None,
        remote_parent_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        recorder = getattr(self._resolver, "record_unresolved", None)
        if recorder is None:
            return
        recorder(
            remote_type="organisation_unit",
            remote_id=remote_id,
            remote_name=remote_name,
            remote_parent_id=remote_parent_id,
            detail=detail,
        )

    def _code_level_rows(self, code: str, level: str) -> list[GeographyUnit]:
        try:
            geography_level = GeographyLevel(level)
        except ValueError:
            return []
        return list(
            self._session.execute(
                select(GeographyUnit).where(
                    GeographyUnit.preferred_code == code,
                    GeographyUnit.level == geography_level,
                    GeographyUnit.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )


class StaticGeographyLookup:
    """Deterministic lookup for tests. Still refuses name matching."""

    def __init__(
        self,
        *,
        uids: dict[str, GeographyScope] | None = None,
        facilities: dict[str, uuid.UUID] | None = None,
        codes: dict[tuple[str, str], GeographyScope] | None = None,
        unconfirmed_codes: dict[tuple[str, str], tuple[GeographyScope, ...]] | None = None,
    ) -> None:
        self._uids = uids or {}
        self._facilities = facilities or {}
        self._codes = codes or {}
        self._unconfirmed = unconfirmed_codes or {}
        self.recorded: list[dict[str, Any]] = []

    def resolve_organisation_unit(self, remote_id: str) -> MappedRemoteUnit:
        unit = self._uids.get(remote_id)
        return MappedRemoteUnit(
            geography_unit_id=unit.geography_unit_id if unit else None,
            facility_id=self._facilities.get(remote_id),
        )

    def geography_scope_for(self, unit_id: uuid.UUID) -> GeographyScope | None:
        for scope in self._uids.values():
            if scope.geography_unit_id == unit_id:
                return scope
        for scope in self._codes.values():
            if scope.geography_unit_id == unit_id:
                return scope
        return None

    def geography_scope_by_code(self, code: str, level: str) -> GeographyScope | None:
        return self._codes.get((code, level))

    def unconfirmed_code_candidates(self, code: str, level: str) -> tuple[GeographyScope, ...]:
        if (code, level) in self._unconfirmed:
            return self._unconfirmed[(code, level)]
        confirmed = self._codes.get((code, level))
        return (confirmed,) if confirmed is not None else ()

    def record_unresolved_mapping(
        self,
        *,
        remote_id: str,
        remote_name: str | None,
        remote_parent_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.recorded.append(
            {
                "remote_id": remote_id,
                "remote_name": remote_name,
                "remote_parent_id": remote_parent_id,
                "detail": detail,
            }
        )


def classify_level(unit: RemoteOrgUnit, levels: tuple[RemoteOrgUnitLevel, ...]) -> str | None:
    """Map a DHIS2 organisation-unit level number onto a MARS administrative kind.

    Uses confirmed level metadata names, then the numeric level on the unit.
    Does not inspect the unit's own name, and does not treat every leaf as a
    facility.
    """
    number = unit.level
    if number is None and unit.path:
        number = unit.path.strip("/").count("/") + 1
    if number is None:
        return None
    for level in levels:
        if level.number == number:
            return _kind_from_level_name(level.name)
    return None


def _kind_from_level_name(name: str) -> str | None:
    lowered = name.strip().lower()
    for tokens, kind in _LEVEL_TOKENS:
        if any(token in lowered for token in tokens):
            return kind
    return None


def select_dashboard_units(
    snapshot: LoginSnapshot,
) -> tuple[tuple[RemoteOrgUnit, ...], bool, str | None, str | None]:
    """Data-view units for aggregate dashboard authorization.

    Returns ``(units, fallback_used, fallback_source, fallback_reason)``.
    """
    if snapshot.data_view_field_present:
        if snapshot.data_view_organisation_units:
            return snapshot.data_view_organisation_units, False, None, None
        return (
            (),
            False,
            None,
            "dataViewOrganisationUnits was present but empty; capture and Tracker "
            "scopes were not substituted",
        )
    return (
        snapshot.organisation_units,
        True,
        "organisationUnits",
        "dataViewOrganisationUnits was absent from /api/me; classifying capture "
        "organisationUnits as a documented compatibility fallback",
    )


def resolve_live_scope(snapshot: LoginSnapshot, lookup: GeographyLookup) -> ResolvedLiveScope:
    identity = AuthenticatedSourceIdentity(
        source_system=SOURCE_DHIS2,
        remote_user_id=snapshot.remote_user_id,
        username=snapshot.username,
        display_name=snapshot.display_name,
    )
    dashboard_units, fallback_used, fallback_source, fallback_reason = select_dashboard_units(
        snapshot
    )
    remote_authorization = RemoteAuthorizationContext(
        capture_scope=snapshot.organisation_units,
        data_view_scope=snapshot.data_view_organisation_units,
        tracker_search_scope=snapshot.tei_search_organisation_units,
        authorities=snapshot.authorities,
        fallback_policy=_FALLBACK_POLICY,
        fallback_used=fallback_used,
        fallback_source=fallback_source,
        fallback_reason=fallback_reason,
        data_view_field_present=snapshot.data_view_field_present,
    )
    workspace = classify_remote_workspace(dashboard_units, snapshot.organisation_unit_levels)
    mapping, geography, facilities = map_workspace(
        workspace, dashboard_units, snapshot.organisation_unit_levels, lookup
    )
    readiness = readiness_for(workspace, mapping)
    landing = landing_path_for(workspace, mapping)
    return ResolvedLiveScope(
        identity=identity,
        remote_authorization=remote_authorization,
        workspace=workspace,
        mapping=mapping,
        readiness=readiness,
        geography_scopes=geography,
        facility_scopes=facilities,
        landing_path=landing,
    )


def classify_remote_workspace(
    units: tuple[RemoteOrgUnit, ...],
    levels: tuple[RemoteOrgUnitLevel, ...],
) -> RemoteWorkspaceScope:
    if not units:
        return UNRESOLVED_WORKSPACE

    classified = [(unit, classify_level(unit, levels)) for unit in units]
    countries = [unit for unit, kind in classified if kind == "country"]
    districts = [unit for unit, kind in classified if kind == "district"]
    facilities = [unit for unit, kind in classified if kind == "facility"]

    if countries:
        return _workspace_from_unit(countries[0], "national")
    if len(districts) == 1:
        return _workspace_from_unit(districts[0], "district")
    if len(districts) > 1:
        primary = districts[0]
        return RemoteWorkspaceScope(
            status="resolved",
            scope_type="multi_district",
            source=SOURCE_DHIS2,
            external_uid=None,
            name=None,
            code=None,
            level=primary.level,
            path=None,
            parent_uid=None,
        )
    if len(facilities) == 1 and not districts:
        return _workspace_from_unit(facilities[0], "facility")
    if facilities and not districts:
        return RemoteWorkspaceScope(
            status="resolved",
            scope_type="other",
            source=SOURCE_DHIS2,
            external_uid=None,
            name=None,
            code=None,
            level=None,
            path=None,
            parent_uid=None,
        )
    return _workspace_from_unit(units[0], "other")


def _workspace_from_unit(unit: RemoteOrgUnit, scope_type: ScopeType) -> RemoteWorkspaceScope:
    parent = unit.parent_uid or parent_uid_from_path(unit.uid, unit.path)
    return RemoteWorkspaceScope(
        status="resolved",
        scope_type=scope_type,
        source=SOURCE_DHIS2,
        external_uid=unit.uid,
        name=unit.name,
        code=unit.code,
        level=unit.level,
        path=unit.path,
        parent_uid=parent,
    )


def map_workspace(
    workspace: RemoteWorkspaceScope,
    dashboard_units: tuple[RemoteOrgUnit, ...],
    levels: tuple[RemoteOrgUnitLevel, ...],
    lookup: GeographyLookup,
) -> tuple[LocalMappingResult, tuple[GeographyScope, ...], frozenset[uuid.UUID]]:
    if workspace.status != "resolved":
        return PENDING_MAPPING, (), frozenset()

    mapped_geography: dict[uuid.UUID, GeographyScope] = {}
    mapped_facilities: set[uuid.UUID] = set()
    evidence: list[str] = []

    for remote in dashboard_units:
        kind = classify_level(remote, levels)
        resolved = lookup.resolve_organisation_unit(remote.uid)
        scope = None
        if resolved.geography_unit_id is not None:
            scope = lookup.geography_scope_for(resolved.geography_unit_id)
            if scope is not None:
                evidence.append(f"confirmed dhis2 alias for {remote.uid}")
        if scope is None and remote.code and kind and kind != "facility":
            scope = lookup.geography_scope_by_code(remote.code, kind)
            if scope is not None:
                evidence.append(f"confirmed dhis2 code {remote.code} at {kind}")
        if scope is not None:
            mapped_geography[scope.geography_unit_id] = scope
        if resolved.facility_id is not None:
            mapped_facilities.add(resolved.facility_id)
            evidence.append(f"confirmed dhis2 facility identifier for {remote.uid}")

    geography = tuple(mapped_geography.values())
    facilities = frozenset(mapped_facilities)

    if workspace.scope_type == "national":
        country = next((scope for scope in geography if scope.level == "country"), None)
        if country is not None:
            return (
                LocalMappingResult(
                    status="resolved",
                    geography_unit_id=country.geography_unit_id,
                    facility_id=None,
                    evidence=tuple(evidence) or ("confirmed national geography alias",),
                ),
                geography,
                facilities,
            )
    if workspace.scope_type == "district":
        districts = [scope for scope in geography if scope.level == "district"]
        if len(districts) == 1:
            return (
                LocalMappingResult(
                    status="resolved",
                    geography_unit_id=districts[0].geography_unit_id,
                    facility_id=None,
                    evidence=tuple(evidence) or ("confirmed district geography alias",),
                ),
                geography,
                facilities,
            )
        if len(districts) > 1:
            return (
                LocalMappingResult(
                    status="ambiguous",
                    geography_unit_id=None,
                    facility_id=None,
                    evidence=("multiple confirmed local districts for one remote data-view unit",),
                ),
                (),
                frozenset(),
            )
    if workspace.scope_type == "multi_district":
        districts = [scope for scope in geography if scope.level == "district"]
        if districts and len(districts) == len(
            [unit for unit in dashboard_units if classify_level(unit, levels) == "district"]
        ):
            return (
                LocalMappingResult(
                    status="resolved",
                    geography_unit_id=None,
                    facility_id=None,
                    evidence=tuple(evidence) or ("confirmed aliases for each data-view district",),
                ),
                geography,
                facilities,
            )
    if workspace.scope_type == "facility" and len(facilities) == 1:
        facility_id = next(iter(facilities))
        geography_id = geography[0].geography_unit_id if geography else None
        return (
            LocalMappingResult(
                status="resolved",
                geography_unit_id=geography_id,
                facility_id=facility_id,
                evidence=tuple(evidence) or ("confirmed facility identifier",),
            ),
            geography,
            facilities,
        )

    if geography or facilities:
        primary = geography[0] if geography else None
        return (
            LocalMappingResult(
                status="resolved" if workspace.scope_type == "other" else "pending",
                geography_unit_id=primary.geography_unit_id if primary else None,
                facility_id=next(iter(facilities), None),
                evidence=tuple(evidence),
            ),
            geography,
            facilities,
        )

    extra = _unconfirmed_evidence(workspace, lookup)
    return (
        LocalMappingResult(
            status="pending",
            geography_unit_id=None,
            facility_id=None,
            evidence=extra,
        ),
        (),
        frozenset(),
    )


def _unconfirmed_evidence(
    workspace: RemoteWorkspaceScope, lookup: GeographyLookup
) -> tuple[str, ...]:
    notes = [
        "no confirmed geography_unit_alias or facility identifier",
        "name matching was not used",
    ]
    if workspace.code:
        kind = {
            "national": "country",
            "district": "district",
            "facility": "facility",
        }.get(workspace.scope_type)
        if kind:
            candidates = lookup.unconfirmed_code_candidates(workspace.code, kind)
            if len(candidates) == 1:
                notes.append(
                    f"one local {kind} shares preferred_code {workspace.code}; "
                    "left pending for explicit approval"
                )
            elif len(candidates) > 1:
                notes.append(
                    f"{len(candidates)} local {kind} rows share preferred_code "
                    f"{workspace.code}; ambiguous"
                )
    return tuple(notes)


def readiness_for(workspace: RemoteWorkspaceScope, mapping: LocalMappingResult) -> DataReadiness:
    if workspace.status != "resolved":
        return DataReadiness(
            geography="pending",
            malaria_metadata="pending",
            aggregate_sync="unavailable",
            tracker_sync="not_authorized",
        )
    geography: Literal["resolved", "pending", "ambiguous"]
    if mapping.status == "resolved":
        geography = "resolved"
    elif mapping.status == "ambiguous":
        geography = "ambiguous"
    else:
        geography = "pending"
    return DataReadiness(
        geography=geography,
        malaria_metadata="pending",
        aggregate_sync="pending",
        tracker_sync="not_started",
    )


def landing_path_for(workspace: RemoteWorkspaceScope, mapping: LocalMappingResult) -> str:
    """Route from remote authorization first, then local mapping.

    Usernames never enter this decision. A DHIS2 UID is used in a live
    namespace only when local mapping is still pending.
    """
    if workspace.status != "resolved" or workspace.scope_type == "unresolved":
        return "/no-authorised-scope"
    if mapping.status == "resolved":
        if workspace.scope_type == "national":
            return "/command-centre"
        if workspace.scope_type == "district" and mapping.geography_unit_id is not None:
            return f"/district/{mapping.geography_unit_id}"
        if workspace.scope_type == "facility" and mapping.facility_id is not None:
            return f"/facility/{mapping.facility_id}"
        return "/authorised-scope"
    if (
        workspace.scope_type == "district"
        and workspace.external_uid
        and is_dhis2_uid(workspace.external_uid)
    ):
        return f"/live/dhis2/district/{workspace.external_uid}"
    if (
        workspace.scope_type == "facility"
        and workspace.external_uid
        and is_dhis2_uid(workspace.external_uid)
    ):
        return f"/live/dhis2/facility/{workspace.external_uid}"
    if workspace.scope_type == "national":
        return "/live/dhis2/national"
    return "/authorised-scope"


def landing_path_for_scope(scope: ResolvedLiveScope) -> str:
    return scope.landing_path


def permissions_for_scope(scope: ResolvedLiveScope) -> frozenset[Permission]:
    """Translate login success into explicit MARS capabilities.

    Unknown DHIS2 authorities grant nothing. Remote authorization without a
    confirmed local mapping grants no local surveillance permission: the
    caller may see the workspace shell, not KPIs.
    """
    if scope.workspace.status != "resolved":
        return frozenset()
    if scope.mapping.status != "resolved":
        return frozenset()
    granted = set(LIVE_BASE_PERMISSIONS)
    if scope.workspace.scope_type in {"national", "district", "multi_district"}:
        granted.update(LIVE_INVESTIGATION_PERMISSIONS)
    return frozenset(granted)


def build_live_principal(
    snapshot: LoginSnapshot,
    scope: ResolvedLiveScope,
    *,
    session_reference: str,
) -> AuthenticatedPrincipal:
    subject = f"dhis2:{snapshot.remote_user_id}"
    user_id = uuid.uuid5(DHIS2_SUBJECT_NAMESPACE, subject)
    return AuthenticatedPrincipal(
        user_id=user_id,
        subject=subject,
        username=snapshot.username,
        display_name=snapshot.display_name,
        roles=frozenset(),
        permissions=permissions_for_scope(scope),
        max_sensitivity=SensitivityLevel.AGGREGATE,
        geography_scopes=scope.geography_scopes,
        facility_scopes=scope.facility_scopes,
        session_reference=session_reference,
        auth_method="dhis2_pilot",
        is_synthetic=False,
    )


def mapping_proposal_detail(scope: ResolvedLiveScope) -> dict[str, Any]:
    """Sanitized evidence for an administrator. No credentials."""
    workspace = scope.workspace
    remote = {
        "uid": workspace.external_uid,
        "name": workspace.name,
        "code": workspace.code,
        "level": workspace.level,
        "path": workspace.path,
        "parent_uid": workspace.parent_uid,
        "source": workspace.source,
        "scope_type": workspace.scope_type,
    }
    local = {
        "geography_unit_id": str(scope.mapping.geography_unit_id)
        if scope.mapping.geography_unit_id
        else None,
        "facility_id": str(scope.mapping.facility_id) if scope.mapping.facility_id else None,
        "status": scope.mapping.status,
    }
    return {
        "remote_dhis2": remote,
        "local_mars": local,
        "result": {
            "proposed_status": scope.mapping.status,
            "confidence": "confirmed_alias"
            if scope.mapping.status == "resolved"
            else "pending_review",
            "evidence": list(scope.mapping.evidence),
            "automatic_confirmation": scope.mapping.status == "resolved",
        },
        "fallback": {
            "used": scope.remote_authorization.fallback_used,
            "source": scope.remote_authorization.fallback_source,
            "reason": scope.remote_authorization.fallback_reason,
        },
        "data_readiness": {
            "geography": scope.readiness.geography,
            "malaria_metadata": scope.readiness.malaria_metadata,
            "aggregate_sync": scope.readiness.aggregate_sync,
            "tracker_sync": scope.readiness.tracker_sync,
        },
    }


def record_pending_mapping(scope: ResolvedLiveScope, lookup: GeographyLookup) -> None:
    if scope.mapping.status == "resolved":
        return
    if not scope.workspace.external_uid:
        return
    lookup.record_unresolved_mapping(
        remote_id=scope.workspace.external_uid,
        remote_name=scope.workspace.name,
        remote_parent_id=scope.workspace.parent_uid,
        detail=mapping_proposal_detail(scope),
    )


def _scope_from_unit(unit: GeographyUnit) -> GeographyScope:
    return GeographyScope(
        geography_unit_id=unit.id,
        preferred_code=unit.preferred_code,
        level=unit.level.value,
        name=unit.raw_name,
        path=unit.path,
    )


__all__ = [
    "GeographyLookup",
    "MappedRemoteUnit",
    "ResolvedLiveScope",
    "SqlAlchemyGeographyLookup",
    "StaticGeographyLookup",
    "build_live_principal",
    "classify_level",
    "classify_remote_workspace",
    "landing_path_for",
    "landing_path_for_scope",
    "mapping_proposal_detail",
    "permissions_for_scope",
    "record_pending_mapping",
    "resolve_live_scope",
    "select_dashboard_units",
]
