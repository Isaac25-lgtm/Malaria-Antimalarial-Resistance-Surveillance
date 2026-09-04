"""The investigation state machine — Prompt 26.

Every command here does the same four things: check the transition is legal,
apply it, append a timeline entry, and record an audit event. Nothing skips a
step, because the value of an investigation record is precisely that it can be
reconstructed afterwards.

**Transitions are validated, not advisory.** ``ALLOWED_TRANSITIONS`` is the
whole state machine and an illegal move is refused. An investigation that
jumped from new to closed would record a decision no reviewer made.

**Concurrency is optimistic.** Two reviewers who both open an investigation and
both press close must not silently overwrite one another: the second write is
rejected with a conflict and the reviewer re-reads. Losing one of two
contradictory conclusions is worse than making someone press the button again.

**Commands are idempotent where they create.** A retried open does not produce
a second investigation for the same signal.

**The signal is never touched.** Concluding an investigation writes to
``investigation`` and ``investigation_event`` and nowhere else. The analysis
said what it said on the day it ran.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.exc import StaleDataError

from mars.core.errors import ConflictError, NotFoundError, ValidationFailedError
from mars.domain.enums import (
    AuditAction,
    EvidenceRequestStatus,
    InvestigationEventKind,
    InvestigationOutcome,
    InvestigationStatus,
    SignalPriority,
)
from mars.domain.investigation import (
    Investigation,
    InvestigationEvent,
    InvestigationEvidenceRequest,
    InvestigationFeedback,
)
from mars.domain.signal import SurveillanceSignal
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService
from mars.services.audit_service import AuditService

#: The state machine, in full. Read it as: from this state, only these.
#:
#: Closed and escalated are terminal. Reopening is deliberately absent: a
#: conclusion that can be quietly withdrawn is not a conclusion, and a genuine
#: change of mind belongs in a new investigation that cites the old one.
ALLOWED_TRANSITIONS: dict[InvestigationStatus, frozenset[InvestigationStatus]] = {
    InvestigationStatus.NEW: frozenset({InvestigationStatus.TRIAGED}),
    InvestigationStatus.TRIAGED: frozenset({InvestigationStatus.ASSIGNED}),
    InvestigationStatus.ASSIGNED: frozenset(
        {
            InvestigationStatus.UNDER_INVESTIGATION,
            InvestigationStatus.ASSIGNED,  # reassignment
        }
    ),
    InvestigationStatus.UNDER_INVESTIGATION: frozenset(
        {
            InvestigationStatus.CLOSED,
            InvestigationStatus.ESCALATED,
            InvestigationStatus.ASSIGNED,  # reassignment mid-investigation
        }
    ),
    InvestigationStatus.CLOSED: frozenset(),
    InvestigationStatus.ESCALATED: frozenset(),
}

#: The governed configuration that makes an "overdue" queue possible. MARS
#: ships no value: how long a district has to triage a signal is a programme
#: commitment, and inventing one would put real people behind an imaginary
#: deadline.
SLA_CONFIGURATION_KEY = "investigation_sla"


class InvestigationService:
    """Commands and queries over the investigation workflow."""

    def __init__(self, session: Session, audit: AuditService | None = None) -> None:
        self._session = session
        self._audit = audit
        self._scope = AnalyticsQueryService(session)

    # -- Reading -------------------------------------------------------------
    def _scoped(
        self,
        principal: AuthenticatedPrincipal,
        statement: Select[tuple[Investigation]],
    ) -> Select[tuple[Investigation]]:
        """Apply the caller's geography and facility scope in SQL."""
        geographies = self._scope.geography_ids(principal)
        facilities = self._scope.facility_ids(principal)
        if principal.is_facility_restricted:
            return statement.where(Investigation.facility_id.in_(facilities or set()))
        if geographies is not None:
            return statement.where(Investigation.geography_unit_id.in_(geographies))
        return statement

    def get(self, principal: AuthenticatedPrincipal, investigation_id: uuid.UUID) -> Investigation:
        """One investigation, or a not-found that does not confirm existence."""
        statement = self._scoped(
            principal,
            select(Investigation)
            .options(
                selectinload(Investigation.events),
                selectinload(Investigation.evidence_requests),
            )
            .where(Investigation.id == investigation_id),
        )
        found: Investigation | None = self._session.execute(statement).scalar_one_or_none()
        if found is None:
            # Deliberately indistinguishable from absent. Confirming that an
            # investigation exists but is not yours to read would disclose that
            # something was flagged there.
            raise NotFoundError("investigation not found or outside your assigned scope")
        return found

    def queue(
        self,
        principal: AuthenticatedPrincipal,
        *,
        name: str,
        limit: int = 100,
    ) -> list[Investigation]:
        """One action-centre queue.

        ``overdue`` is absent from this build: it needs an approved SLA, and
        the caller is told that rather than shown an empty list, because an
        empty overdue queue reads as "nothing is late".
        """
        statement = select(Investigation)

        if name == "new":
            statement = statement.where(
                Investigation.investigation_status == InvestigationStatus.NEW
            )
        elif name == "high_priority":
            statement = statement.where(
                Investigation.priority.in_([SignalPriority.HIGH, SignalPriority.URGENT]),
                Investigation.investigation_status.notin_(
                    [InvestigationStatus.CLOSED, InvestigationStatus.ESCALATED]
                ),
            )
        elif name == "assigned_to_me":
            statement = statement.where(
                Investigation.assigned_to_user_id == principal.user_id,
                Investigation.investigation_status.notin_(
                    [InvestigationStatus.CLOSED, InvestigationStatus.ESCALATED]
                ),
            )
        elif name == "under_investigation":
            statement = statement.where(
                Investigation.investigation_status == InvestigationStatus.UNDER_INVESTIGATION
            )
        elif name == "awaiting_external_result":
            awaiting = (
                select(InvestigationEvidenceRequest.investigation_id)
                .where(
                    InvestigationEvidenceRequest.request_status == EvidenceRequestStatus.AWAITING
                )
                .distinct()
            )
            statement = statement.where(Investigation.id.in_(awaiting))
        elif name == "resolved":
            statement = statement.where(
                Investigation.investigation_status.in_(
                    [InvestigationStatus.CLOSED, InvestigationStatus.ESCALATED]
                )
            )
        else:
            raise ValidationFailedError(f"Unknown queue: {name}")

        statement = self._scoped(principal, statement)
        return list(
            self._session.execute(statement.order_by(Investigation.opened_at.desc()).limit(limit))
            .scalars()
            .all()
        )

    def sla_configuration(self) -> tuple[dict[str, Any] | None, list[str]]:
        """The approved SLA, or ``None`` with what is missing.

        Read from governance rather than shipped. Without it the overdue queue
        does not exist, which is the honest state: MARS has not been told how
        long a district has.
        """
        from mars.domain.enums import LifecycleStatus
        from mars.domain.governance import ConfigurationKey, ConfigurationVersion

        version = (
            self._session.execute(
                select(ConfigurationVersion)
                .join(
                    ConfigurationKey,
                    ConfigurationKey.id == ConfigurationVersion.configuration_key_id,
                )
                .where(
                    ConfigurationKey.key == SLA_CONFIGURATION_KEY,
                    ConfigurationVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if version is None or not isinstance(version.value, dict) or not version.value:
            return None, [f"configuration:{SLA_CONFIGURATION_KEY}"]
        return version.value, []

    # -- Commands ------------------------------------------------------------
    def open(
        self,
        principal: AuthenticatedPrincipal,
        *,
        signal_id: uuid.UUID,
        idempotency_key: str | None = None,
    ) -> Investigation:
        """Open an investigation against a signal.

        Idempotent: a retry with the same key, or a second open for a signal
        that already has one, returns the existing investigation rather than
        splitting the timeline in two.
        """
        if idempotency_key:
            existing = self._session.execute(
                self._scoped(
                    principal,
                    select(Investigation).where(Investigation.idempotency_key == idempotency_key),
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.signal_id != signal_id:
                    raise ConflictError(
                        "That idempotency key was already used for a different signal."
                    )
                return existing

        existing = self._session.execute(
            self._scoped(
                principal,
                select(Investigation).where(Investigation.signal_id == signal_id),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        signal_statement = select(SurveillanceSignal).where(SurveillanceSignal.id == signal_id)
        geographies = self._scope.geography_ids(principal)
        facilities = self._scope.facility_ids(principal)
        if principal.is_facility_restricted:
            geographies = set()
        if geographies is not None and facilities is not None:
            signal_statement = signal_statement.where(
                or_(
                    SurveillanceSignal.geography_unit_id.in_(geographies),
                    SurveillanceSignal.facility_id.in_(facilities),
                )
            )
        # Serialise two attempts to open work for the same signal. The second
        # transaction sees the investigation created by the first.
        signal = self._session.execute(signal_statement.with_for_update()).scalar_one_or_none()
        if signal is None:
            raise NotFoundError("signal not found or outside your assigned scope")

        existing = self._session.execute(
            self._scoped(
                principal,
                select(Investigation).where(Investigation.signal_id == signal_id),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        now = datetime.now(UTC)
        investigation = Investigation(
            signal_id=signal.id,
            investigation_status=InvestigationStatus.NEW,
            # Copied, not joined: a later change to the signal's priority must
            # not silently re-order somebody's queue.
            priority=signal.priority,
            geography_unit_id=signal.geography_unit_id,
            facility_id=signal.facility_id,
            period_start=signal.period_start,
            period_end=signal.period_end,
            opened_at=now,
            record_version=1,
            idempotency_key=idempotency_key,
        )
        self._session.add(investigation)
        self._session.flush()

        self._append(
            investigation,
            principal,
            kind=InvestigationEventKind.OPENED,
            payload={"signal_id": str(signal.id)},
        )
        self._record_audit(
            principal, investigation, AuditAction.SIGNAL_TRIAGED, {"action": "opened"}
        )
        return investigation

    def transition(
        self,
        principal: AuthenticatedPrincipal,
        *,
        investigation_id: uuid.UUID,
        to_status: InvestigationStatus,
        expected_version: int,
        assigned_to_user_id: uuid.UUID | None = None,
        outcome: InvestigationOutcome | None = None,
        note: str | None = None,
        escalation_reason: str | None = None,
    ) -> Investigation:
        """Move an investigation along, or refuse to."""
        investigation = self.get(principal, investigation_id)
        self._check_version(investigation, expected_version)

        current = investigation.investigation_status
        if to_status not in ALLOWED_TRANSITIONS[current]:
            raise ValidationFailedError(
                f"An investigation cannot move from {current.value} to {to_status.value}."
            )

        now = datetime.now(UTC)
        kind = _EVENT_FOR_STATUS[to_status]

        if to_status is InvestigationStatus.TRIAGED:
            investigation.triaged_at = now
        elif to_status is InvestigationStatus.ASSIGNED:
            if assigned_to_user_id is None:
                raise ValidationFailedError("An assignment needs an owner.")
            reassignment = investigation.assigned_to_user_id is not None
            investigation.assigned_to_user_id = assigned_to_user_id
            investigation.assigned_at = now
            if reassignment:
                kind = InvestigationEventKind.REASSIGNED
        elif to_status is InvestigationStatus.CLOSED:
            if outcome is None:
                raise ValidationFailedError("A closure records what the reviewer concluded.")
            investigation.outcome = outcome
            investigation.outcome_note = note
            investigation.closed_at = now
        elif to_status is InvestigationStatus.ESCALATED:
            if not escalation_reason or not escalation_reason.strip():
                raise ValidationFailedError("An escalation records why.")
            investigation.escalation_reason = escalation_reason
            investigation.closed_at = now

        investigation.investigation_status = to_status
        self._claim_version(investigation)

        self._append(
            investigation,
            principal,
            kind=kind,
            note=note,
            payload={
                "from": current.value,
                "to": to_status.value,
                **({"outcome": outcome.value} if outcome else {}),
                **(
                    {"assigned_to_user_id": str(assigned_to_user_id)} if assigned_to_user_id else {}
                ),
            },
        )
        self._record_audit(
            principal,
            investigation,
            AuditAction.INVESTIGATION_UPDATED,
            {"from": current.value, "to": to_status.value},
        )

        if to_status is InvestigationStatus.CLOSED and outcome is not None:
            self._record_feedback(investigation, outcome, note)

        return investigation

    def add_note(
        self,
        principal: AuthenticatedPrincipal,
        *,
        investigation_id: uuid.UUID,
        note: str,
        expected_version: int,
    ) -> InvestigationEvent:
        """Append a note. Notes never change the state machine."""
        if not note.strip():
            raise ValidationFailedError("A note needs text.")
        investigation = self.get(principal, investigation_id)
        self._check_version(investigation, expected_version)
        self._claim_version(investigation)
        return self._append(
            investigation, principal, kind=InvestigationEventKind.NOTE_ADDED, note=note
        )

    def request_evidence(
        self,
        principal: AuthenticatedPrincipal,
        *,
        investigation_id: uuid.UUID,
        description: str,
        expected_version: int,
    ) -> InvestigationEvidenceRequest:
        """Record a request for evidence MARS cannot produce itself."""
        if not description.strip():
            raise ValidationFailedError("An evidence request says what it wants.")
        investigation = self.get(principal, investigation_id)
        self._check_version(investigation, expected_version)
        self._claim_version(investigation)
        now = datetime.now(UTC)
        request = InvestigationEvidenceRequest(
            investigation_id=investigation.id,
            request_status=EvidenceRequestStatus.AWAITING,
            description=description,
            requested_at=now,
            requested_by_user_id=principal.user_id,
        )
        self._session.add(request)
        self._session.flush()
        self._append(
            investigation,
            principal,
            kind=InvestigationEventKind.EVIDENCE_REQUESTED,
            note=description,
            payload={"evidence_request_id": str(request.id)},
        )
        return request

    def record_external_result(
        self,
        principal: AuthenticatedPrincipal,
        *,
        investigation_id: uuid.UUID,
        evidence_request_id: uuid.UUID,
        result_reference: str,
        expected_version: int,
    ) -> InvestigationEvidenceRequest:
        """Record that an external result came back, by reference only.

        The reference points into the system that holds the result under its
        own governance. MARS stores the pointer, never the clinical content:
        that separation is what keeps the confirmed-evidence lane distinct
        from routine surveillance.
        """
        if not result_reference.strip():
            raise ValidationFailedError("A result needs a reference.")
        investigation = self.get(principal, investigation_id)
        self._check_version(investigation, expected_version)
        request = next(
            (r for r in investigation.evidence_requests if r.id == evidence_request_id),
            None,
        )
        if request is None:
            raise NotFoundError("evidence request not found for this investigation")
        if request.request_status is not EvidenceRequestStatus.AWAITING:
            raise ConflictError("That evidence request is no longer awaiting a result.")

        self._claim_version(investigation)
        request.request_status = EvidenceRequestStatus.RECEIVED
        request.result_reference = result_reference
        request.result_recorded_at = datetime.now(UTC)
        self._session.flush()

        self._append(
            investigation,
            principal,
            kind=InvestigationEventKind.EXTERNAL_RESULT_RECORDED,
            payload={
                "evidence_request_id": str(request.id),
                "result_reference": result_reference,
            },
        )
        return request

    # -- Presentation shapes -------------------------------------------------
    #
    # Returned as dictionaries so the API layer never holds an ORM row
    # (ADR 0002). A router that could reach ``investigation.events`` would
    # eventually issue a query, and that is invisible until it is slow.
    @staticmethod
    def event_shape(event: InvestigationEvent) -> dict[str, Any]:
        return {
            "sequence": event.sequence,
            "event_kind": event.event_kind.value,
            "actor_label": event.actor_label,
            "occurred_at": event.occurred_at,
            "note": event.note,
            "payload": event.payload,
        }

    @staticmethod
    def evidence_request_shape(request: InvestigationEvidenceRequest) -> dict[str, Any]:
        return {
            "id": request.id,
            "request_status": request.request_status.value,
            "description": request.description,
            "requested_at": request.requested_at,
            "result_reference": request.result_reference,
            "result_recorded_at": request.result_recorded_at,
        }

    @staticmethod
    def queue_shape(investigation: Investigation) -> dict[str, Any]:
        return {
            "id": investigation.id,
            "signal_id": investigation.signal_id,
            "investigation_status": investigation.investigation_status.value,
            "priority": investigation.priority.value,
            "geography_unit_id": investigation.geography_unit_id,
            "facility_id": investigation.facility_id,
            "period_start": investigation.period_start,
            "period_end": investigation.period_end,
            "assigned_to_user_id": investigation.assigned_to_user_id,
            "opened_at": investigation.opened_at,
            "record_version": investigation.record_version,
        }

    def detail_shape(self, investigation: Investigation) -> dict[str, Any]:
        return {
            **self.queue_shape(investigation),
            "triaged_at": investigation.triaged_at,
            "assigned_at": investigation.assigned_at,
            "closed_at": investigation.closed_at,
            "outcome": investigation.outcome.value if investigation.outcome else None,
            "outcome_note": investigation.outcome_note,
            "escalation_reason": investigation.escalation_reason,
            "events": [self.event_shape(event) for event in investigation.events],
            "evidence_requests": [
                self.evidence_request_shape(request) for request in investigation.evidence_requests
            ],
        }

    # -- Internals -----------------------------------------------------------
    @staticmethod
    def _check_version(investigation: Investigation, expected: int) -> None:
        if investigation.record_version != expected:
            raise ConflictError(
                "This investigation changed since you loaded it. Re-read it and "
                "try again - your view is out of date, and overwriting the "
                "other change would lose it."
            )

    def _claim_version(self, investigation: Investigation) -> None:
        """Atomically claim the version before appending to the timeline."""
        investigation.record_version += 1
        try:
            self._session.flush()
        except StaleDataError as exc:
            raise ConflictError(
                "This investigation changed since you loaded it. Re-read it and try again."
            ) from exc

    def _append(
        self,
        investigation: Investigation,
        principal: AuthenticatedPrincipal,
        *,
        kind: InvestigationEventKind,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> InvestigationEvent:
        next_sequence = int(
            self._session.execute(
                select(func.coalesce(func.max(InvestigationEvent.sequence), 0) + 1).where(
                    InvestigationEvent.investigation_id == investigation.id
                )
            ).scalar_one()
        )
        event = InvestigationEvent(
            investigation_id=investigation.id,
            sequence=next_sequence,
            event_kind=kind,
            actor_user_id=principal.user_id,
            # The operator's own name, which is theirs. No patient identifier
            # is ever written to this table.
            actor_label=principal.display_name,
            occurred_at=datetime.now(UTC),
            note=note,
            payload=payload,
        )
        self._session.add(event)
        self._session.flush()
        return event

    def _record_feedback(
        self,
        investigation: Investigation,
        outcome: InvestigationOutcome,
        note: str | None,
    ) -> None:
        """Label the method version that raised the signal, and change nothing.

        The learning loop is deliberately inert. A closed investigation becomes
        evidence for a later governed method review; it does not move a
        threshold.
        """
        signal = self._session.execute(
            select(SurveillanceSignal).where(SurveillanceSignal.id == investigation.signal_id)
        ).scalar_one_or_none()
        if signal is None:
            return
        self._session.add(
            InvestigationFeedback(
                investigation_id=investigation.id,
                signal_id=signal.id,
                method_version_id=signal.method_version_id,
                outcome=outcome,
                signal_input_fingerprint=signal.input_fingerprint,
                recorded_at=datetime.now(UTC),
                note=note,
            )
        )
        self._session.flush()

    def _record_audit(
        self,
        principal: AuthenticatedPrincipal,
        investigation: Investigation,
        action: AuditAction,
        context: dict[str, Any],
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            action=action,
            principal=principal,
            object_type="investigation",
            object_id=str(investigation.id),
            geography_unit_id=investigation.geography_unit_id,
            facility_id=investigation.facility_id,
            context=context,
        )


_EVENT_FOR_STATUS: dict[InvestigationStatus, InvestigationEventKind] = {
    InvestigationStatus.TRIAGED: InvestigationEventKind.TRIAGED,
    InvestigationStatus.ASSIGNED: InvestigationEventKind.ASSIGNED,
    InvestigationStatus.UNDER_INVESTIGATION: InvestigationEventKind.STARTED,
    InvestigationStatus.CLOSED: InvestigationEventKind.CLOSED,
    InvestigationStatus.ESCALATED: InvestigationEventKind.ESCALATED,
    InvestigationStatus.NEW: InvestigationEventKind.OPENED,
}

#: The queues the action centre offers. ``overdue`` is deliberately absent: it
#: requires an approved SLA, and an empty overdue queue reads as "nothing is
#: late" rather than "MARS has not been told what late means".
QUEUES: tuple[str, ...] = (
    "new",
    "high_priority",
    "assigned_to_me",
    "under_investigation",
    "awaiting_external_result",
    "resolved",
)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "QUEUES",
    "SLA_CONFIGURATION_KEY",
    "InvestigationService",
]
