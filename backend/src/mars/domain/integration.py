"""Integration runs: what MARS asked an external system for, and what came back.

An exchange with another system is not a function call. It is paginated, it
fails halfway, it is retried tomorrow, and someone will later ask *which* pull
produced a figure. So a run is a record, not a log line.

Three things this module is careful about.

**No secret is ever stored here.** Not the URL's credentials, not a token, not
an ``Authorization`` header echoed back in an error. ``error_summary`` is a
category and a message MARS composed, never the raw response body - a DHIS2
error can quote the request that caused it.

**A remote identifier is never a MARS key.** DHIS2 organisation-unit UIDs live
in the existing ``geography_unit_alias`` and ``facility_identifier``
crosswalks. If a UID does not resolve, the mapping stays unresolved and visible;
nothing is matched by name similarity, because two districts with similar names
are exactly the case a fuzzy match gets wrong.

**A changed remote payload cannot keep its old meaning.** Every run records a
checksum of what it received. Re-pulling the same bytes is idempotent; different
bytes make a new run, and the canonical layer's own revision rules decide what
that means for the figures.
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
from mars.db.schemas import CORE
from mars.domain.enums import (
    IntegrationResource,
    IntegrationRunStatus,
    MappingProposalStatus,
)


class IntegrationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One exchange with one external system for one scope.

    Identified by ``(system, resource, scope_fingerprint, attempt)``. The
    attempt is part of the key because a retry after a partial failure is a
    different run: it read different pages, and conflating the two would make
    the record of what was fetched unreadable.
    """

    __tablename__ = "integration_run"
    __table_args__ = (
        UniqueConstraint(
            "system",
            "resource",
            "scope_fingerprint",
            "attempt",
            name="uq_integration_run_system_resource_scope_attempt",
        ),
        CheckConstraint("attempt >= 1", name="attempt_is_positive"),
        CheckConstraint(
            "payload_checksum IS NULL OR length(payload_checksum) = 64",
            name="payload_checksum_is_sha256",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finish_after_start",
        ),
        CheckConstraint("pages_fetched >= 0", name="pages_not_negative"),
        Index("ix_integration_run_system_resource", "system", "resource"),
        Index("ix_integration_run_status", "run_status"),
        Index("ix_integration_run_started", "started_at"),
        {
            "schema": CORE,
            "comment": (
                "One exchange with an external system. Holds no credential and "
                "no raw remote error body: error_summary is composed by MARS."
            ),
        },
    )

    #: Which external system. A string rather than an enum because the set of
    #: systems a deployment talks to is an operational fact, not a code change.
    system: Mapped[str] = mapped_column(String(64), nullable=False)

    resource: Mapped[IntegrationResource] = mapped_column(
        pg_enum(IntegrationResource, name="integration_resource", schema=CORE), nullable=False
    )

    #: SHA-256 over the *requested* scope - periods, org units, dataset. What
    #: makes "the same request" the same request across days.
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Human-readable form of the same scope, for an operator reading a list.
    #: Never contains a credential; the URL is stored without its userinfo.
    scope_description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    period_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    run_status: Mapped[IntegrationRunStatus] = mapped_column(
        pg_enum(IntegrationRunStatus, name="integration_run_status", schema=CORE),
        nullable=False,
        default=IntegrationRunStatus.PENDING,
    )

    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Where a resumed run picks up. Opaque to MARS - DHIS2 pages by page
    #: number, another system might use a token - so it is stored as text and
    #: interpreted only by the adapter that wrote it.
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    records_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mappings_unresolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: SHA-256 of the canonicalised payload received. Null while a run is still
    #: open, or when it failed before reading anything.
    payload_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: The batch the payload was routed into, when it produced one. Aggregate
    #: content goes through the canonical aggregate pipeline rather than a
    #: parallel model, so this points at the ordinary import lifecycle.
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.import_batch.id", ondelete="SET NULL", name="fk_integration_batch"),
        nullable=True,
    )

    #: What MARS was running. Recorded so a run can be read against the code
    #: that made it.
    adapter_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Correlates every HTTP request of this run in the logs.
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(160), nullable=True)

    #: A category and a MARS-composed message. Never a raw remote body.
    error_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    mapping_proposals: Mapped[list[IntegrationMappingProposal]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def is_terminal(self) -> bool:
        return self.run_status in {
            IntegrationRunStatus.COMPLETED,
            IntegrationRunStatus.PARTIAL,
            IntegrationRunStatus.FAILED,
        }


class IntegrationMappingProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A remote identifier MARS could not resolve, kept where someone will see it.

    The alternative - matching on a similar name - is how one district's
    figures end up under another's. A proposal is a question for a human, and
    it stays a question until a human answers it.

    ``proposed`` is the only status this module writes. Promotion to an actual
    crosswalk row is a governance action, not an import side effect.
    """

    __tablename__ = "integration_mapping_proposal"
    __table_args__ = (
        UniqueConstraint(
            "system",
            "remote_type",
            "remote_id",
            name="uq_integration_mapping_system_type_remote",
        ),
        Index("ix_integration_mapping_status", "proposal_status"),
        Index("ix_integration_mapping_run", "integration_run_id"),
        {
            "schema": CORE,
            "comment": (
                "Remote identifiers with no MARS mapping. Never resolved by "
                "name similarity; promotion is a governance action."
            ),
        },
    )

    integration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.integration_run.id", ondelete="SET NULL", name="fk_mapping_run"),
        nullable=True,
    )

    system: Mapped[str] = mapped_column(String(64), nullable=False)
    #: "organisation_unit", "data_element", "category_option_combo", "dataset".
    remote_type: Mapped[str] = mapped_column(String(48), nullable=False)
    remote_id: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: The remote parent, when the source gives one. Context for whoever
    #: resolves this, not something MARS acts on.
    remote_parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    proposal_status: Mapped[MappingProposalStatus] = mapped_column(
        pg_enum(MappingProposalStatus, name="mapping_proposal_status", schema=CORE),
        nullable=False,
        default=MappingProposalStatus.PROPOSED,
    )

    #: How many times this unresolved identifier has been seen. A UID appearing
    #: in every weekly pull is a more urgent configuration gap than one that
    #: appeared once.
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Structured context - the remote level, the period it appeared in. Never
    #: a credential and never a patient value.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    run: Mapped[IntegrationRun | None] = relationship(back_populates="mapping_proposals")


__all__ = ["IntegrationMappingProposal", "IntegrationRun"]
