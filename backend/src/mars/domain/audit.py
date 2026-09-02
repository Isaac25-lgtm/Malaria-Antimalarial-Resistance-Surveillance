"""Append-only audit trail.

Blueprint section 066: every consequential action must be reconstructable, and
audit records are append-only from the application's perspective with restricted
administrative access.

Immutability is enforced in three places, because one is not enough:

1. No service in this codebase exposes an update or delete path for an event.
2. ORM-level ``before_update`` and ``before_delete`` listeners raise.
3. A database trigger installed by the migration rejects UPDATE and DELETE, so
   a future code path cannot quietly bypass the first two.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, Mapper, mapped_column
from sqlalchemy.orm.session import Session as ORMSession

from mars.db.base import Base, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import AUDIT
from mars.domain.enums import AuditAction, AuditOutcome


class AuditEventImmutableError(RuntimeError):
    """Raised when code attempts to modify or remove an audit event."""


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """A single auditable action.

    ``before_state`` and ``after_state`` hold structured summaries, not raw
    source rows. They must never contain a patient name, national identity
    number, telephone number or next-of-kin detail; the audit trail records that
    an access happened, not the content that was accessed.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_occurred_at", "occurred_at"),
        Index("ix_audit_event_actor_occurred", "actor_user_id", "occurred_at"),
        Index("ix_audit_event_object", "object_type", "object_id"),
        Index("ix_audit_event_action_occurred", "action", "occurred_at"),
        Index("ix_audit_event_request_id", "request_id"),
        CheckConstraint(
            "actor_user_id IS NOT NULL OR actor_kind <> 'user'",
            name="actor_user_required_for_user_events",
        ),
        {
            "schema": AUDIT,
            "comment": "Append-only. UPDATE and DELETE are rejected by trigger.",
        },
    )

    # -- When -------------------------------------------------------------
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # -- Who --------------------------------------------------------------
    # Only the internal user UUID and a display-safe label are stored. The
    # label is a username or service name, never a patient-derived value.
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # -- What -------------------------------------------------------------
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, name="audit_action", schema=AUDIT),
        nullable=False,
    )
    outcome: Mapped[AuditOutcome] = mapped_column(
        pg_enum(AuditOutcome, name="audit_outcome", schema=AUDIT),
        nullable=False,
        default=AuditOutcome.SUCCEEDED,
    )
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # -- Where ------------------------------------------------------------
    # Geography context is recorded as the stable internal unit id plus its
    # code, so an audit record remains meaningful after a boundary revision.
    geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    geography_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    # -- Change detail ----------------------------------------------------
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    before_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    after_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Correlation ------------------------------------------------------
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # -- Free-form, non-sensitive context ---------------------------------
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


@event.listens_for(AuditEvent, "before_update", propagate=True)
def _block_audit_update(_mapper: Mapper[Any], _connection: Any, _target: AuditEvent) -> None:
    raise AuditEventImmutableError("audit events are append-only and cannot be updated")


@event.listens_for(AuditEvent, "before_delete", propagate=True)
def _block_audit_delete(_mapper: Mapper[Any], _connection: Any, _target: AuditEvent) -> None:
    raise AuditEventImmutableError("audit events are append-only and cannot be deleted")


@event.listens_for(ORMSession, "before_flush")
def _block_audit_mutation_in_session(
    session: ORMSession, _flush_context: Any, _instances: Any
) -> None:
    """Catch attempts that would otherwise bypass the mapper-level listeners."""
    for obj in session.dirty:
        if isinstance(obj, AuditEvent) and session.is_modified(obj, include_collections=False):
            raise AuditEventImmutableError("audit events are append-only and cannot be updated")
    for obj in session.deleted:
        if isinstance(obj, AuditEvent):
            raise AuditEventImmutableError("audit events are append-only and cannot be deleted")
