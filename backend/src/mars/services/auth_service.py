"""Assembling the authenticated principal, and seeding the access model.

Roles, permissions and scopes always come from the MARS database, never from
identity-provider token claims. A misconfigured provider can therefore fail to
authenticate someone, but cannot grant them surveillance access.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mars.core.errors import NotFoundError
from mars.core.timeutils import utc_now
from mars.domain.geography import GeographyUnit
from mars.domain.security import (
    Role,
    RolePermission,
    UserAccount,
    UserFacilityScope,
    UserGeographyScope,
    UserRole,
    UserSensitivityScope,
)
from mars.security.permissions import (
    PERMISSION_CATALOGUE,
    ROLE_DEFAULT_SENSITIVITY,
    ROLE_PERMISSIONS,
    Permission,
    SensitivityLevel,
    SystemRole,
)
from mars.security.principal import AuthenticatedPrincipal, GeographyScope
from mars.security.providers import VerifiedIdentity

_ROLE_DESCRIPTIONS: dict[SystemRole, tuple[str, str]] = {
    SystemRole.NATIONAL_PROGRAMME: (
        "National programme",
        "National malaria programme staff. Aggregate surveillance across Uganda, "
        "investigation workflow and reporting. No patient-level evidence by default.",
    ),
    SystemRole.DISTRICT_HSD: (
        "District / HSD",
        "District health team and health sub-district staff. Aggregate surveillance "
        "and pseudonymous case evidence within their assigned geography only.",
    ),
    SystemRole.FACILITY: (
        "Facility",
        "Facility staff. Their own facility's surveillance data, data quality and "
        "pseudonymous case evidence.",
    ),
    SystemRole.ANALYST: (
        "Analyst",
        "Manages indicator definitions, configuration and validated methods. "
        "Grants no access to patient-level data.",
    ),
    SystemRole.ADMINISTRATOR: (
        "Administrator",
        "Manages users, organisation units, geography and integrations. "
        "Grants no access to patient-level or aggregate surveillance data.",
    ),
}


class RoleSeeder:
    """Creates the baseline roles and their permission grants.

    Idempotent: existing roles keep their identity, and only missing permission
    grants are added. Run by migration tooling and by test fixtures.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def seed(self) -> dict[SystemRole, Role]:
        result: dict[SystemRole, Role] = {}
        for system_role, permissions in ROLE_PERMISSIONS.items():
            label, description = _ROLE_DESCRIPTIONS[system_role]
            role = self._session.execute(
                select(Role).where(Role.code == system_role.value)
            ).scalar_one_or_none()

            if role is None:
                role = Role(
                    code=system_role.value,
                    label=label,
                    description=description,
                    is_system_role=True,
                    max_sensitivity=ROLE_DEFAULT_SENSITIVITY[system_role],
                )
                self._session.add(role)
                self._session.flush()

            existing = {grant.permission for grant in role.permissions}
            for permission in permissions:
                if permission not in existing:
                    self._session.add(RolePermission(role_id=role.id, permission=permission))
            result[system_role] = role

        self._session.flush()
        return result


class AuthService:
    """Resolves a verified identity into an authorisation context."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_user_by_subject(self, subject: str) -> UserAccount | None:
        return self._session.execute(
            select(UserAccount)
            .where(UserAccount.subject == subject)
            .options(
                selectinload(UserAccount.roles).selectinload(UserRole.role),
                selectinload(UserAccount.geography_scopes),
                selectinload(UserAccount.sensitivity_scope),
            )
        ).scalar_one_or_none()

    def build_principal(
        self, user: UserAccount, identity: VerifiedIdentity | None = None
    ) -> AuthenticatedPrincipal:
        """Assemble the immutable authorisation context for a request."""
        today = date.today()

        active_assignments = [
            assignment
            for assignment in user.roles
            if _within_validity(assignment.valid_from, assignment.valid_to, today)
        ]

        role_codes = {assignment.role.code for assignment in active_assignments}

        permissions: set[Permission] = set()
        role_ceiling = SensitivityLevel.AGGREGATE
        for assignment in active_assignments:
            for grant in assignment.role.permissions:
                permissions.add(grant.permission)
            if assignment.role.max_sensitivity.value > role_ceiling.value:
                role_ceiling = assignment.role.max_sensitivity

        # The effective sensitivity is the lower of what the user was granted
        # and what their roles permit. A generous user grant cannot exceed the
        # role ceiling, and a generous role cannot exceed the user grant.
        user_ceiling = (
            user.sensitivity_scope.max_sensitivity
            if user.sensitivity_scope is not None
            else SensitivityLevel.AGGREGATE
        )
        effective_sensitivity = min(role_ceiling, user_ceiling, key=lambda level: level.value)

        # A permission whose minimum sensitivity exceeds the effective ceiling is
        # dropped rather than silently upgrading the caller.
        usable_permissions = frozenset(
            permission
            for permission in permissions
            if effective_sensitivity.covers(PERMISSION_CATALOGUE[permission].minimum_sensitivity)
        )

        geography_scopes = self._load_geography_scopes(user.id)
        facility_scopes = self._load_facility_scopes(user.id)

        return AuthenticatedPrincipal(
            user_id=user.id,
            subject=user.subject,
            username=user.username,
            display_name=user.display_name,
            roles=frozenset(role_codes),
            permissions=usable_permissions,
            max_sensitivity=effective_sensitivity,
            geography_scopes=geography_scopes,
            facility_scopes=facility_scopes,
            session_reference=identity.session_reference if identity else None,
            auth_method=identity.auth_method if identity else "unknown",
            is_synthetic=user.is_synthetic,
        )

    def _load_geography_scopes(self, user_id: uuid.UUID) -> tuple[GeographyScope, ...]:
        rows = self._session.execute(
            select(UserGeographyScope, GeographyUnit)
            .join(GeographyUnit, GeographyUnit.id == UserGeographyScope.geography_unit_id)
            .where(UserGeographyScope.user_id == user_id)
        ).all()
        return tuple(
            GeographyScope(
                geography_unit_id=unit.id,
                preferred_code=unit.preferred_code,
                level=unit.level.value,
                name=unit.raw_name,
                path=unit.path,
            )
            for _scope, unit in rows
        )

    def _load_facility_scopes(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        rows = (
            self._session.execute(
                select(UserFacilityScope.facility_id).where(UserFacilityScope.user_id == user_id)
            )
            .scalars()
            .all()
        )
        return frozenset(rows)

    def record_login(self, user: UserAccount) -> None:
        user.last_login_at = utc_now()
        self._session.flush()

    # -- Administration --------------------------------------------------
    def create_user(
        self,
        *,
        subject: str,
        username: str,
        display_name: str,
        email: str | None = None,
        issuer: str | None = None,
        is_synthetic: bool = False,
        organisation_label: str | None = None,
    ) -> UserAccount:
        user = UserAccount(
            subject=subject,
            username=username,
            display_name=display_name,
            email=email,
            issuer=issuer,
            is_synthetic=is_synthetic,
            organisation_label=organisation_label,
        )
        self._session.add(user)
        self._session.flush()
        return user

    def assign_role(
        self, *, user: UserAccount, role_code: str, granted_by: str | None = None
    ) -> UserRole:
        role = self._session.execute(
            select(Role).where(Role.code == role_code)
        ).scalar_one_or_none()
        if role is None:
            raise NotFoundError(f"role {role_code!r} not found")

        assignment = UserRole(user_id=user.id, role_id=role.id, granted_by=granted_by)
        self._session.add(assignment)
        self._session.flush()
        return assignment

    def grant_geography_scope(
        self,
        *,
        user: UserAccount,
        geography_unit_id: uuid.UUID,
        granted_by: str | None = None,
        reason: str | None = None,
    ) -> UserGeographyScope:
        scope = UserGeographyScope(
            user_id=user.id,
            geography_unit_id=geography_unit_id,
            granted_by=granted_by,
            reason=reason,
        )
        self._session.add(scope)
        self._session.flush()
        return scope

    def grant_facility_scope(
        self, *, user: UserAccount, facility_id: uuid.UUID, granted_by: str | None = None
    ) -> UserFacilityScope:
        scope = UserFacilityScope(user_id=user.id, facility_id=facility_id, granted_by=granted_by)
        self._session.add(scope)
        self._session.flush()
        return scope

    def set_sensitivity_scope(
        self,
        *,
        user: UserAccount,
        level: SensitivityLevel,
        granted_by: str | None = None,
        reason: str | None = None,
    ) -> UserSensitivityScope:
        """Set a user's sensitivity ceiling.

        The direct-identity tier requires a recorded reason; the database
        enforces this too, so the rule survives a future code path that forgets.
        """
        if level is SensitivityLevel.DIRECT_IDENTITY and not reason:
            raise ValueError("granting direct identity access requires a recorded reason")

        existing = self._session.execute(
            select(UserSensitivityScope).where(UserSensitivityScope.user_id == user.id)
        ).scalar_one_or_none()

        if existing is None:
            existing = UserSensitivityScope(user_id=user.id)
            self._session.add(existing)

        existing.max_sensitivity = level
        existing.granted_by = granted_by
        existing.reason = reason
        self._session.flush()
        return existing


def _within_validity(valid_from: date | None, valid_to: date | None, today: date) -> bool:
    if valid_from is not None and today < valid_from:
        return False
    return not (valid_to is not None and today > valid_to)
