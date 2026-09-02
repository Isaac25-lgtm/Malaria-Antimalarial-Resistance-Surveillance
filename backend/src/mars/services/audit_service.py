"""Recording audit events.

The only supported way to write the audit trail. There is deliberately no
update or delete method: a mistake in an audit record is corrected by appending
a correcting event, not by editing history.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from mars.core.context import current_context
from mars.core.timeutils import utc_now
from mars.domain.audit import AuditEvent
from mars.domain.enums import AuditAction, AuditOutcome
from mars.security.principal import AuthenticatedPrincipal


class AuditService:
    """Append-only writer and reader for :class:`AuditEvent`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        action: AuditAction,
        outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
        principal: AuthenticatedPrincipal | None = None,
        actor_kind: str = "user",
        actor_label: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        geography_unit_id: uuid.UUID | None = None,
        geography_code: str | None = None,
        facility_id: uuid.UUID | None = None,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        before_ref: str | None = None,
        after_ref: str | None = None,
        reason: str | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append one audit event.

        ``before_state`` and ``after_state`` must be structured summaries. Never
        pass a raw source row: the audit trail records that something happened,
        not the patient content involved.
        """
        request_context = current_context()

        event = AuditEvent(
            occurred_at=utc_now(),
            actor_kind=actor_kind,
            actor_user_id=principal.user_id if principal else None,
            actor_label=actor_label or (principal.username if principal else None),
            action=action,
            outcome=outcome,
            object_type=object_type,
            object_id=object_id,
            geography_unit_id=geography_unit_id,
            geography_code=geography_code,
            facility_id=facility_id,
            before_state=before_state,
            after_state=after_state,
            before_ref=before_ref,
            after_ref=after_ref,
            reason=reason,
            request_id=request_context.request_id,
            session_id=(principal.session_reference if principal else request_context.session_id),
            source_ip=source_ip,
            user_agent=user_agent,
            context=context,
        )
        self._session.add(event)
        self._session.flush()
        return event

    def record_denial(
        self,
        *,
        principal: AuthenticatedPrincipal | None,
        reason: str,
        object_type: str | None = None,
        object_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append an ``access_denied`` event.

        Denials of sensitive actions are audited as required by blueprint
        section 066. The reason names the missing grant, never the resource,
        so an audit entry does not confirm that a resource exists.
        """
        return self.record(
            action=AuditAction.ACCESS_DENIED,
            outcome=AuditOutcome.DENIED,
            principal=principal,
            object_type=object_type,
            object_id=object_id,
            reason=reason,
            context=context,
        )

    def query(
        self,
        *,
        actor_user_id: uuid.UUID | None = None,
        action: AuditAction | None = None,
        object_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Read audit events. Requires the audit:view permission at the route."""
        statement: Select[tuple[AuditEvent]] = select(AuditEvent)
        if actor_user_id is not None:
            statement = statement.where(AuditEvent.actor_user_id == actor_user_id)
        if action is not None:
            statement = statement.where(AuditEvent.action == action)
        if object_type is not None:
            statement = statement.where(AuditEvent.object_type == object_type)
        statement = statement.order_by(AuditEvent.occurred_at.desc()).limit(limit)
        return list(self._session.execute(statement).scalars().all())
