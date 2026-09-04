"""Investigations: the loop from a detected signal to a programme decision.

A signal nobody acts on is a signal that was never worth generating. These
tables are where MARS stops being an analysis and becomes a piece of
operational software, and they are shaped by three facts about that.

**The history is append-only.** ``investigation_event`` is never updated or
deleted. An investigation whose timeline can be rewritten cannot support the
decision it led to, and the decision is the whole point.

**The signal is never mutated.** An investigation points at a signal;
concluding an investigation does not edit the signal's evidence, score or
status. The analysis said what it said on the day it ran, and a later human
judgement is a separate record beside it rather than a correction of it. That
is what keeps the analytical audit trail worth having.

**A conclusion is a programme judgement, not a laboratory result.**
``validated_signal`` means the pattern held up and warrants action. It does not
mean resistance was confirmed. Confirmation reaches MARS from an external
reference laboratory through the separately governed evidence lane, and there
is no column here that could hold it.

``investigation_feedback`` exists so that outcomes can inform a later method
review. It records what a reviewer concluded against the method version that
produced the signal; it changes no threshold and no rule. Automatic
threshold-tuning from field outcomes is exactly the kind of quiet drift that
makes a surveillance system unauditable.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
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
from mars.db.schemas import ANALYTICS, CORE, GOVERNANCE, SECURITY
from mars.domain.enums import (
    EvidenceRequestStatus,
    InvestigationEventKind,
    InvestigationOutcome,
    InvestigationStatus,
    SignalPriority,
)


class Investigation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One programme response to one signal."""

    __tablename__ = "investigation"
    __table_args__ = (
        # One live investigation per signal. A second would split the timeline
        # and leave two people each believing the other had it.
        UniqueConstraint("signal_id", name="uq_investigation_signal"),
        UniqueConstraint("idempotency_key", name="uq_investigation_idempotency_key"),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("record_version >= 1", name="record_version_is_positive"),
        # A terminal state carries the reason it reached it. An investigation
        # closed without an outcome is a decision nobody can review.
        CheckConstraint(
            "investigation_status <> 'closed' OR (outcome IS NOT NULL AND closed_at IS NOT NULL)",
            name="closure_records_its_outcome",
        ),
        CheckConstraint(
            "investigation_status <> 'escalated' OR "
            "(escalation_reason IS NOT NULL AND closed_at IS NOT NULL)",
            name="escalation_records_its_reason",
        ),
        # Assignment is what turns a queue entry into someone's work.
        CheckConstraint(
            "investigation_status NOT IN ('assigned', 'under_investigation') OR "
            "assigned_to_user_id IS NOT NULL",
            name="assigned_work_has_an_owner",
        ),
        Index("ix_investigation_status", "investigation_status"),
        Index("ix_investigation_owner", "assigned_to_user_id", "investigation_status"),
        Index("ix_investigation_geography", "geography_unit_id"),
        Index("ix_investigation_facility", "facility_id"),
        {
            "schema": CORE,
            "comment": (
                "One programme response to one signal. The signal itself is "
                "never modified by anything recorded here."
            ),
        },
    )

    #: The signal under investigation. RESTRICT because an investigation
    #: outlives interest in the signal that started it, and deleting the
    #: analysis would orphan the decision.
    signal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.surveillance_signal.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: ``investigation_status``, not ``status``: every lifecycle in this schema
    #: carries its own named column.
    investigation_status: Mapped[InvestigationStatus] = mapped_column(
        pg_enum(InvestigationStatus, name="investigation_status", schema=CORE),
        nullable=False,
        default=InvestigationStatus.NEW,
    )

    #: Copied from the signal when the investigation opens. A later change to
    #: the signal's priority does not silently re-order somebody's queue.
    priority: Mapped[SignalPriority] = mapped_column(
        pg_enum(SignalPriority, name="signal_priority", schema=ANALYTICS), nullable=False
    )

    #: Scope, copied so a queue can be filtered without joining the signal.
    geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="RESTRICT"),
        nullable=True,
    )

    opened_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triaged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assigned_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    outcome: Mapped[InvestigationOutcome | None] = mapped_column(
        pg_enum(InvestigationOutcome, name="investigation_outcome", schema=CORE), nullable=True
    )
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Optimistic concurrency. Two reviewers opening the same investigation and
    #: both pressing "close" must not silently overwrite one another; the
    #: second write is rejected and the reviewer re-reads.
    record_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: Supplied by the caller so a retried command does not open a second
    #: investigation for the same signal.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # SQLAlchemy includes record_version in every UPDATE predicate. A second
    # session writing a stale object therefore updates zero rows instead of
    # silently overwriting the first reviewer's decision.
    __mapper_args__ = {  # noqa: RUF012
        "version_id_col": record_version,
        "version_id_generator": False,
    }

    events: Mapped[list[InvestigationEvent]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="InvestigationEvent.sequence",
    )
    evidence_requests: Mapped[list[InvestigationEvidenceRequest]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan"
    )


class InvestigationEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One entry in an investigation's append-only timeline.

    Never updated, never deleted. Who did what, when, and what they said.
    """

    __tablename__ = "investigation_event"
    __table_args__ = (
        UniqueConstraint("investigation_id", "sequence", name="uq_investigation_event_sequence"),
        CheckConstraint("sequence >= 1", name="sequence_is_positive"),
        # A note without text is an empty row in a record someone will later
        # rely on.
        CheckConstraint(
            "event_kind <> 'note_added' OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name="a_note_carries_text",
        ),
        Index("ix_investigation_event_investigation", "investigation_id", "sequence"),
        {
            "schema": CORE,
            "comment": (
                "Append-only investigation timeline. UPDATE and DELETE are rejected by trigger."
            ),
        },
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.investigation.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    event_kind: Mapped[InvestigationEventKind] = mapped_column(
        pg_enum(InvestigationEventKind, name="investigation_event_kind", schema=CORE),
        nullable=False,
    )

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SECURITY}.user_account.id", ondelete="RESTRICT"),
        nullable=True,
    )
    #: The operator's own name, which is theirs and safe to record as an actor
    #: label. No patient identifier ever appears in this table.
    actor_label: Mapped[str | None] = mapped_column(String(160), nullable=True)

    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Structured detail for the event kind - the status moved from and to, the
    #: outcome recorded, the reference supplied.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    investigation: Mapped[Investigation] = relationship(back_populates="events")


class InvestigationEvidenceRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A request for evidence MARS cannot produce itself.

    Typically a reference-laboratory result. The reference is recorded, never
    the result's clinical content: MARS holds a pointer to the external record
    under its own governance, which is what keeps the confirmed-evidence lane
    separate from this one.
    """

    __tablename__ = "investigation_evidence_request"
    __table_args__ = (
        CheckConstraint(
            "request_status <> 'received' OR "
            "(result_reference IS NOT NULL AND result_recorded_at IS NOT NULL)",
            name="result_has_a_reference",
        ),
        CheckConstraint("length(trim(description)) > 0", name="request_states_its_ask"),
        Index("ix_evidence_request_investigation", "investigation_id"),
        Index("ix_evidence_request_status", "request_status"),
        {
            "schema": CORE,
            "comment": (
                "A request for externally supplied evidence. Holds a reference "
                "to the external record, never its clinical content."
            ),
        },
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.investigation.id",
            ondelete="CASCADE",
            name="fk_evidence_request_investigation",
        ),
        nullable=False,
    )

    request_status: Mapped[EvidenceRequestStatus] = mapped_column(
        pg_enum(EvidenceRequestStatus, name="evidence_request_status", schema=CORE),
        nullable=False,
        default=EvidenceRequestStatus.AWAITING,
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{SECURITY}.user_account.id",
            ondelete="RESTRICT",
            name="fk_evidence_request_requested_by",
        ),
        nullable=True,
    )

    #: A pointer into the external system holding the result. Never the result.
    result_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_recorded_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    investigation: Mapped[Investigation] = relationship(back_populates="evidence_requests")


class InvestigationFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What a closed investigation says about the method that raised it.

    The learning loop, and deliberately an inert one. This records the outcome
    against the method version in force when the signal was generated, so a
    later method review has labelled evidence to work from. It changes no
    threshold, no weight and no rule: automatic tuning from field outcomes is
    the quiet drift that makes a surveillance system unauditable, and any
    change to a governed method still goes through governance.
    """

    __tablename__ = "investigation_feedback"
    __table_args__ = (
        UniqueConstraint("investigation_id", name="uq_investigation_feedback_investigation"),
        Index("ix_investigation_feedback_method", "method_version_id"),
        Index("ix_investigation_feedback_outcome", "outcome"),
        {
            "schema": CORE,
            "comment": (
                "Labelled outcomes for later method review. Informs a governed "
                "review; changes nothing on its own."
            ),
        },
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.investigation.id",
            ondelete="CASCADE",
            name="fk_investigation_feedback_investigation",
        ),
        nullable=False,
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.surveillance_signal.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The method version that produced the signal, captured as of detection.
    #: Feeding back against whichever version happens to be current later would
    #: label the wrong method.
    method_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.method_version.id",
            ondelete="RESTRICT",
            name="fk_investigation_feedback_method_version",
        ),
        nullable=False,
    )
    outcome: Mapped[InvestigationOutcome] = mapped_column(
        pg_enum(InvestigationOutcome, name="investigation_outcome", schema=CORE), nullable=False
    )
    #: The signal's own fingerprint, so the labelled example can be tied to the
    #: exact evidence set that produced it.
    signal_input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "Investigation",
    "InvestigationEvent",
    "InvestigationEvidenceRequest",
    "InvestigationFeedback",
]
