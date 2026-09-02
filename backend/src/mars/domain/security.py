"""Security ORM models: users, roles, permissions and the two scope axes.

Lives in ``mars_security``, separate from ``mars_core``, so that operator access
to surveillance data does not imply access to the access-control model itself.

No password material is stored. Production authentication is delegated to an
OIDC provider; the development mode issues short-lived synthetic tokens signed
with a development-only secret that the settings layer refuses to accept in a
protected environment.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import SECURITY
from mars.security.permissions import Permission, SensitivityLevel

_sensitivity_enum = pg_enum(SensitivityLevel, name="sensitivity_level", schema=SECURITY)


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named collection of permissions."""

    __tablename__ = "role"
    __table_args__ = (
        UniqueConstraint("code", name="uq_role_code"),
        {"schema": SECURITY},
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_system_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Ceiling on what any assignment of this role may grant. An individual
    #: user's sensitivity scope is intersected with this value.
    max_sensitivity: Mapped[SensitivityLevel] = mapped_column(
        _sensitivity_enum, nullable=False, default=SensitivityLevel.AGGREGATE
    )

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[UserRole]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class RolePermission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Grant of a single permission to a role."""

    __tablename__ = "role_permission"
    __table_args__ = (
        UniqueConstraint("role_id", "permission", name="uq_role_permission_role_id_permission"),
        {"schema": SECURITY},
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.role.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission: Mapped[Permission] = mapped_column(
        pg_enum(Permission, name="permission_code", schema=SECURITY),
        nullable=False,
    )

    role: Mapped[Role] = relationship(back_populates="permissions")


class UserAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person or service that can authenticate to MARS.

    ``subject`` is the stable identifier issued by the identity provider. For
    synthetic development users it is prefixed ``dev:`` so a development
    principal can never be mistaken for a real one in the audit trail.
    """

    __tablename__ = "user_account"
    __table_args__ = (
        UniqueConstraint("subject", name="uq_user_account_subject"),
        UniqueConstraint("username", name="uq_user_account_username"),
        Index("ix_user_account_is_active", "is_active"),
        {"schema": SECURITY},
    )

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    organisation_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: True when this account exists only for the development authentication
    #: mode. Such accounts must never be created in a protected environment.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    geography_scopes: Mapped[list[UserGeographyScope]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    sensitivity_scope: Mapped[UserSensitivityScope | None] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )


class UserRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Assignment of a role to a user, with an optional validity window."""

    __tablename__ = "user_role"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role_user_id_role_id"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="validity_range_ordered",
        ),
        {"schema": SECURITY},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.role.id", ondelete="CASCADE"),
        nullable=False,
    )
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    granted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[UserAccount] = relationship(back_populates="roles")
    role: Mapped[Role] = relationship(back_populates="assignments", lazy="selectin")


class UserGeographyScope(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The geography subtree a user may read.

    A scope names a geography unit; the user may read that unit and everything
    beneath it. A user with no scope row has national scope only if a role
    grants it - see ``AuthorisationContext`` - because an empty scope must never
    silently mean "everywhere".
    """

    __tablename__ = "user_geography_scope"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "geography_unit_id",
            name="uq_user_geography_scope_user_id_geography_unit_id",
        ),
        Index("ix_user_geography_scope_user_id", "user_id"),
        {"schema": SECURITY},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Cross-schema reference to mars_core.geography_unit. Declared without a
    #: database-level foreign key so the security schema can be provisioned and
    #: migrated independently of reference data.
    geography_unit_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[UserAccount] = relationship(back_populates="geography_scopes")


class UserFacilityScope(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Facility-level restriction for facility users.

    A facility user is scoped to their own facility, not merely to its district.
    Present as a separate axis so that a facility user's district geography
    scope can still resolve names and hierarchy without exposing sibling
    facilities.
    """

    __tablename__ = "user_facility_scope"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "facility_id", name="uq_user_facility_scope_user_id_facility_id"
        ),
        Index("ix_user_facility_scope_user_id", "user_id"),
        {"schema": SECURITY},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    facility_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class UserSensitivityScope(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The maximum data sensitivity a user may reach.

    Separate from role permissions on purpose. Granting the direct-identity tier
    requires a recorded reason and a review date, because it is the one grant
    that exposes a patient.
    """

    __tablename__ = "user_sensitivity_scope"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_sensitivity_scope_user_id"),
        CheckConstraint(
            "max_sensitivity <> 'direct_identity' OR reason IS NOT NULL",
            name="direct_identity_requires_reason",
        ),
        {"schema": SECURITY},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    max_sensitivity: Mapped[SensitivityLevel] = mapped_column(
        _sensitivity_enum, nullable=False, default=SensitivityLevel.AGGREGATE
    )
    granted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_due_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    user: Mapped[UserAccount] = relationship(back_populates="sensitivity_scope")


class UserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A record of an authenticated session, for audit correlation.

    Holds no token material - only the identifiers needed to tie an audit event
    back to a login.
    """

    __tablename__ = "user_session"
    __table_args__ = (
        Index("ix_user_session_user_id_started_at", "user_id", "started_at"),
        {"schema": SECURITY},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_method: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
