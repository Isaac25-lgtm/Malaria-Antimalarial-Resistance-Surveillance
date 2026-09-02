"""The authenticated principal and its authorisation decisions.

``AuthenticatedPrincipal`` is the single object every authorisation check reads.
It is assembled once per request from the database and is immutable thereafter,
so a handler cannot widen its own scope midway through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from mars.security.permissions import Permission, SensitivityLevel


@dataclass(frozen=True, slots=True)
class GeographyScope:
    """One geography subtree the principal may read."""

    geography_unit_id: uuid.UUID
    preferred_code: str
    level: str
    name: str
    #: Materialised ancestor path of the scope root, e.g. "UG/3/314". Descendant
    #: containment is a prefix test against this value.
    path: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Everything authorisation needs to know about the caller.

    Contains no name-adjacent patient data and no token material. ``display_name``
    is the operator's own name, which is theirs and is safe to log as an actor
    label.
    """

    user_id: uuid.UUID
    subject: str
    username: str
    display_name: str
    roles: frozenset[str]
    permissions: frozenset[Permission]
    max_sensitivity: SensitivityLevel
    geography_scopes: tuple[GeographyScope, ...] = ()
    facility_scopes: frozenset[uuid.UUID] = field(default_factory=frozenset)
    session_reference: str | None = None
    auth_method: str = "oidc"
    is_synthetic: bool = False

    # -- Permission ------------------------------------------------------
    def has_permission(self, permission: Permission) -> bool:
        return permission in self.permissions

    def has_all_permissions(self, *permissions: Permission) -> bool:
        return all(p in self.permissions for p in permissions)

    # -- Sensitivity -----------------------------------------------------
    def can_access_sensitivity(self, required: SensitivityLevel) -> bool:
        return self.max_sensitivity.covers(required)

    # -- Geography -------------------------------------------------------
    @property
    def has_national_scope(self) -> bool:
        """True when a scope covers the whole country.

        An empty scope list is *not* national scope. A principal with no scope
        can read nothing, which is the safe reading of a misconfigured account.
        """
        return any(scope.level == "country" for scope in self.geography_scopes)

    def covers_geography(self, unit_id: uuid.UUID, path: str | None = None) -> bool:
        """Whether the principal's scope covers a geography unit.

        Matches either the scope root itself or, when both paths are known, any
        descendant of it by prefix. When the target path is unknown the check
        falls back to exact identity, which errs towards denial.
        """
        if self.has_national_scope:
            return True
        for scope in self.geography_scopes:
            if scope.geography_unit_id == unit_id:
                return True
            if path and scope.path and _is_descendant_path(path, scope.path):
                return True
        return False

    def scope_path_prefixes(self) -> tuple[str, ...]:
        """Path prefixes usable as a SQL filter for scoped queries."""
        return tuple(scope.path for scope in self.geography_scopes if scope.path)

    def scope_unit_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(scope.geography_unit_id for scope in self.geography_scopes)

    # -- Facility --------------------------------------------------------
    @property
    def is_facility_restricted(self) -> bool:
        """True when the principal may see only named facilities."""
        return bool(self.facility_scopes)

    def covers_facility(self, facility_id: uuid.UUID) -> bool:
        if self.is_facility_restricted:
            return facility_id in self.facility_scopes
        return True


def _is_descendant_path(candidate: str, ancestor: str) -> bool:
    """Prefix containment on materialised paths, respecting segment boundaries.

    ``UG/3/314`` contains ``UG/3/314101`` only via a separator, so ``UG/3/31``
    never matches ``UG/3/314``.
    """
    if candidate == ancestor:
        return True
    return candidate.startswith(f"{ancestor}/")
