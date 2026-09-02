"""Configuration and method governance.

Two registries, both versioned and change-controlled:

``ConfigurationKey`` / ``ConfigurationVersion``
    Operational parameters whose values are programme decisions rather than
    engineering choices - surveillance windows, minimum counts, signal weights,
    suppression thresholds, notification rules. Blueprint section 077.

``MethodDefinition`` / ``MethodVersion``
    The analytical methods and rule sets themselves, with validation references,
    approval status, effective dates and a rollback relationship. Blueprint
    section 078.

Neither registry is seeded with real surveillance thresholds or episode windows.
Those are supplied and approved by the malaria programme; MARS records which
version was in force when a result was produced, and refuses to invent one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import GOVERNANCE
from mars.domain.enums import LifecycleStatus, MethodKind


class ConfigurationKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A governed configuration parameter.

    The key is stable; its value lives in versions. ``requires_programme_approval``
    marks parameters that an engineer may not change alone - anything that
    affects what MARS flags or how it describes a finding.
    """

    __tablename__ = "configuration_key"
    __table_args__ = (
        UniqueConstraint("key", name="uq_configuration_key_key"),
        {"schema": GOVERNANCE},
    )

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    value_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    requires_programme_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)

    versions: Mapped[list[ConfigurationVersion]] = relationship(
        back_populates="configuration_key",
        cascade="all, delete-orphan",
        order_by="ConfigurationVersion.version_number",
    )


class ConfigurationVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An immutable version of a configuration value.

    A published version is never edited. A change creates a new version, which
    passes through the draft -> review -> approved -> active lifecycle. Analytical
    results retain the version identifier that was in force when they were
    computed, so history stays reproducible after a threshold change.
    """

    __tablename__ = "configuration_version"
    __table_args__ = (
        UniqueConstraint(
            "configuration_key_id",
            "version_number",
            name="uq_configuration_version_configuration_key_id_version_number",
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        CheckConstraint(
            "status <> 'active' OR effective_from IS NOT NULL",
            name="active_requires_effective_from",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'active') OR approved_by IS NOT NULL",
            name="approved_requires_approver",
        ),
        Index("ix_configuration_version_key_status", "configuration_key_id", "status"),
        {"schema": GOVERNANCE},
    )

    configuration_key_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.configuration_key.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, name="lifecycle_status", schema=GOVERNANCE),
        nullable=False,
        default=LifecycleStatus.DRAFT,
    )

    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: SHA-256 of the canonical JSON serialisation of ``value``. Lets a stored
    #: analytical result prove which configuration content it used.
    value_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    reason_for_change: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Where the value came from - a programme document, a technical working
    #: group decision, or an explicit statement that it is a MARS default
    #: pending approval. Never left blank for an active version.
    provenance: Mapped[str | None] = mapped_column(Text, nullable=True)

    configuration_key: Mapped[ConfigurationKey] = relationship(back_populates="versions")


class MethodDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A governed analytical method or rule set."""

    __tablename__ = "method_definition"
    __table_args__ = (
        UniqueConstraint("code", name="uq_method_definition_code"),
        {"schema": GOVERNANCE},
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[MethodKind] = mapped_column(
        pg_enum(MethodKind, name="method_kind", schema=GOVERNANCE),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)

    versions: Mapped[list[MethodVersion]] = relationship(
        back_populates="method",
        cascade="all, delete-orphan",
        order_by="MethodVersion.semantic_version",
    )


class MethodVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A specific version of a method.

    Promotion follows candidate -> shadow -> approved -> active -> retired.
    ``rolled_back_from_id`` points at the version this one replaced during a
    rollback, so the reason a method reverted is part of the record rather than
    an operational memory.
    """

    __tablename__ = "method_version"
    __table_args__ = (
        UniqueConstraint(
            "method_definition_id",
            "semantic_version",
            name="uq_method_version_method_definition_id_semantic_version",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'active') OR approved_by IS NOT NULL",
            name="approved_requires_approver",
        ),
        Index("ix_method_version_definition_status", "method_definition_id", "status"),
        {"schema": GOVERNANCE},
    )

    method_definition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_definition.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Semantic version, e.g. "1.2.0". Combined with the method code this forms
    #: the identifier stamped onto every analytical result: ``IND-TPR@1.2.0``.
    semantic_version: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, name="lifecycle_status", schema=GOVERNANCE),
        nullable=False,
        default=LifecycleStatus.DRAFT,
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)

    #: Parameters the method was configured with. Structure is method-specific
    #: and is validated by the owning engine, not here.
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Pointer to the validation report. A placeholder in phases 1-2; a method
    #: may not reach ``active`` without one once analytics land.
    validation_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    validation_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: SHA-256 of the method artifact (code module, model file or rule document).
    artifact_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rolled_back_from_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="SET NULL"),
        nullable=True,
    )
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    method: Mapped[MethodDefinition] = relationship(back_populates="versions")

    @property
    def qualified_version(self) -> str:
        """The identifier stamped onto analytical results."""
        return f"{self.method.code}@{self.semantic_version}"
