"""Dynamic geographic scope from DHIS2 login metadata.

Scope is resolved from organisation-unit UIDs, codes, levels, groups and
hierarchy paths — never from a username and never from a silent name match.

Remote DHIS2 identifiers become MARS geography only through:

* a confirmed ``geography_unit_alias`` row for source_system ``dhis2``;
* a confirmed facility identifier for the same system;
* an exact preferred-code match at the classified administrative level.

If none of those hit, authentication may still succeed. The principal then
carries no usable geography and the interface shows mapping pending. Access
is not broadened and the user is not routed to national data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.domain.enums import AliasMatchStatus, GeographyLevel
from mars.domain.geography import GeographyUnit, GeographyUnitAlias
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal, GeographyScope
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


class GeographyLookup(Protocol):
    def resolve_organisation_unit(self, remote_id: str) -> MappedRemoteUnit: ...

    def geography_scope_for(self, unit_id: uuid.UUID) -> GeographyScope | None: ...

    def geography_scope_by_code(self, code: str, level: str) -> GeographyScope | None: ...


@dataclass(frozen=True, slots=True)
class MappedRemoteUnit:
    """A remote organisation-unit identifier mapped onto MARS geography."""

    geography_unit_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ResolvedLiveScope:
    scope_type: ScopeType
    mapping_status: Literal["mapped", "pending"]
    geography_scopes: tuple[GeographyScope, ...]
    facility_scopes: frozenset[uuid.UUID]
    national_access: bool
    org_unit_id: uuid.UUID | None
    org_unit_name: str | None

    @property
    def authorised_districts(self) -> tuple[GeographyScope, ...]:
        return tuple(scope for scope in self.geography_scopes if scope.level == "district")


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
        rows = (
            self._session.execute(
                select(GeographyUnit).where(
                    GeographyUnit.preferred_code == code,
                    GeographyUnit.level == GeographyLevel(level),
                    GeographyUnit.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != 1:
            return None
        # A confirmed alias at this code is required so a coincidental FScode
        # collision cannot widen access. The alias source_code may be the UID
        # (handled above) or the same preferred code.
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


class StaticGeographyLookup:
    """Deterministic lookup for tests. Still refuses name matching."""

    def __init__(
        self,
        *,
        uids: dict[str, GeographyScope] | None = None,
        facilities: dict[str, uuid.UUID] | None = None,
        codes: dict[tuple[str, str], GeographyScope] | None = None,
    ) -> None:
        self._uids = uids or {}
        self._facilities = facilities or {}
        self._codes = codes or {}

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


def resolve_live_scope(snapshot: LoginSnapshot, lookup: GeographyLookup) -> ResolvedLiveScope:
    mapped_geography: dict[uuid.UUID, GeographyScope] = {}
    mapped_facilities: set[uuid.UUID] = set()

    for remote in snapshot.all_assigned_units():
        kind = classify_level(remote, snapshot.organisation_unit_levels)
        resolved = lookup.resolve_organisation_unit(remote.uid)
        scope = None
        if resolved.geography_unit_id is not None:
            scope = lookup.geography_scope_for(resolved.geography_unit_id)
        if scope is None and remote.code and kind and kind != "facility":
            scope = lookup.geography_scope_by_code(remote.code, kind)
        if scope is not None:
            mapped_geography[scope.geography_unit_id] = scope
        if resolved.facility_id is not None:
            mapped_facilities.add(resolved.facility_id)

    geography = tuple(mapped_geography.values())
    facilities = frozenset(mapped_facilities)
    national = any(scope.level == "country" for scope in geography)
    districts = tuple(scope for scope in geography if scope.level == "district")

    if national:
        country = next(scope for scope in geography if scope.level == "country")
        return ResolvedLiveScope(
            scope_type="national",
            mapping_status="mapped",
            geography_scopes=geography,
            facility_scopes=facilities,
            national_access=True,
            org_unit_id=country.geography_unit_id,
            org_unit_name=country.name,
        )

    if len(districts) == 1:
        district = districts[0]
        return ResolvedLiveScope(
            scope_type="district",
            mapping_status="mapped",
            geography_scopes=geography,
            facility_scopes=facilities,
            national_access=False,
            org_unit_id=district.geography_unit_id,
            org_unit_name=district.name,
        )

    if len(districts) > 1:
        return ResolvedLiveScope(
            scope_type="multi_district",
            mapping_status="mapped",
            geography_scopes=geography,
            facility_scopes=facilities,
            national_access=False,
            org_unit_id=None,
            org_unit_name=None,
        )

    if facilities and not districts:
        facility_id = next(iter(facilities))
        return ResolvedLiveScope(
            scope_type="facility",
            mapping_status="mapped",
            geography_scopes=geography,
            facility_scopes=facilities,
            national_access=False,
            org_unit_id=facility_id,
            org_unit_name=None,
        )

    if geography:
        primary = geography[0]
        return ResolvedLiveScope(
            scope_type="other",
            mapping_status="mapped",
            geography_scopes=geography,
            facility_scopes=facilities,
            national_access=False,
            org_unit_id=primary.geography_unit_id,
            org_unit_name=primary.name,
        )

    return ResolvedLiveScope(
        scope_type="unresolved",
        mapping_status="pending",
        geography_scopes=(),
        facility_scopes=frozenset(),
        national_access=False,
        org_unit_id=None,
        org_unit_name=None,
    )


def landing_path_for_scope(scope: ResolvedLiveScope) -> str:
    """Route from resolved scope. Usernames never enter this decision."""
    if scope.scope_type == "national":
        return "/command-centre"
    if scope.scope_type == "district" and scope.org_unit_id is not None:
        return f"/district/{scope.org_unit_id}"
    if scope.scope_type == "facility" and scope.org_unit_id is not None:
        return f"/facility/{scope.org_unit_id}"
    if scope.scope_type in {"multi_district", "other"}:
        return "/authorised-scope"
    return "/no-authorised-scope"


def permissions_for_scope(scope: ResolvedLiveScope) -> frozenset[Permission]:
    """Translate login success into explicit MARS capabilities.

    Unknown DHIS2 authorities grant nothing. A mapped national or district
    scope receives the conservative aggregate-surveillance set. Case evidence
    and re-identification are never granted from login metadata.
    """
    if scope.scope_type == "unresolved":
        return frozenset({Permission.GEOGRAPHY_VIEW})
    granted = set(LIVE_BASE_PERMISSIONS)
    if scope.scope_type in {"national", "district", "multi_district"}:
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
    "landing_path_for_scope",
    "permissions_for_scope",
    "resolve_live_scope",
]
