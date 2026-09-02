"""Ingestion lifecycle: what was received, what happened to it, and why.

These tables answer the question an operator actually asks after an import:
*which rows did not make it, and what was wrong with them?* A pipeline that
reports only a total is unusable the first time a district's figures look thin.

**Nothing here holds a direct identifier.** A quarantined row is stored with its
``identity`` object removed rather than masked - a masked value is still a
value, and a quarantine table is read by far more people than the vault. The
column is named ``payload_redacted`` so that a reader who has not read this
docstring still knows what they are looking at.

The design of the whole pipeline is documented in
``docs/data-dictionary/ereg-inbound-contract.md``.
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
    ImportBatchStatus,
    ImportStage,
    SourceRowOutcome,
    ValidationSeverity,
)


class ImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One artefact offered to MARS, and what became of it.

    The checksum is the identity of the *content*, not the file: two uploads of
    the same bytes under different names are one batch. That is what makes a
    re-send a no-op rather than a duplicate month of attendance.
    """

    __tablename__ = "import_batch"
    __table_args__ = (
        # The same content cannot create a second batch. Enforced here rather
        # than checked in the application, because two operators uploading the
        # same file at the same moment is exactly when a check-then-act race
        # produces two batches and twice the encounters.
        UniqueConstraint(
            "source_system", "artefact_checksum", name="uq_import_batch_source_checksum"
        ),
        CheckConstraint("length(artefact_checksum) = 64", name="checksum_is_sha256_hex"),
        CheckConstraint("declared_row_count >= 0", name="declared_row_count_not_negative"),
        Index("ix_import_batch_status", "import_status"),
        Index("ix_import_batch_facility", "facility_id"),
        Index("ix_import_batch_received", "received_at"),
        {
            "schema": CORE,
            "comment": (
                "One inbound artefact and its lifecycle. Holds no direct "
                "identifier: identity is consumed inside the identity boundary "
                "and never reaches this schema."
            ),
        },
    )

    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    #: SHA-256 of the artefact's bytes. The batch's real identity.
    artefact_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    artefact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artefact_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Null when the facility could not be resolved - which fails the batch
    #: rather than attaching a month of attendance to a guessed facility.
    facility_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.facility.id", ondelete="RESTRICT"),
        nullable=True,
    )
    facility_code_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Named ``import_status`` rather than ``status``: the schema convention
    #: is that each lifecycle carries its own named column, so a reader never
    #: has to ask *which* status a generic one means.
    import_status: Mapped[ImportBatchStatus] = mapped_column(
        pg_enum(ImportBatchStatus, name="import_batch_status", schema=CORE),
        nullable=False,
        default=ImportBatchStatus.RECEIVED,
    )

    extracted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    register_opened_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    register_closed_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    #: What the envelope claimed. Compared against what was read, because a
    #: truncated upload is the commonest way a batch goes wrong and a silently
    #: short import looks exactly like a quiet week.
    declared_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # -- Counters ----------------------------------------------------------
    #
    # Kept as columns rather than derived on demand: an operator asks "what
    # happened" long after the rows have been archived, and a count that
    # required the rows to still exist would stop answering.
    rows_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_loaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_linked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_unlinked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_geography: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ingest_method_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    initiated_by: Mapped[str | None] = mapped_column(String(160), nullable=True)

    #: Why a batch failed outright, when it did. Never contains row content.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    rows: Mapped[list[ImportSourceRow]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    stages: Mapped[list[ImportStageExecution]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )

    @property
    def is_terminal(self) -> bool:
        """Whether the batch has finished, successfully or not."""
        return self.import_status in {
            ImportBatchStatus.COMPLETED,
            ImportBatchStatus.PARTIALLY_COMPLETED,
            ImportBatchStatus.QUARANTINED,
            ImportBatchStatus.FAILED,
        }


class ImportStageExecution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One stage of one batch, timed and counted.

    Separate rows per stage because a run that slows down is usually slow in one
    stage, and an aggregate duration hides which. Also how a resumed run knows
    what it already did.
    """

    __tablename__ = "import_stage_execution"
    __table_args__ = (
        UniqueConstraint("import_batch_id", "stage", name="uq_import_stage_execution_batch_stage"),
        Index("ix_import_stage_execution_batch", "import_batch_id"),
        {"schema": CORE},
    )

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.import_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[ImportStage] = mapped_column(
        pg_enum(ImportStage, name="import_stage", schema=CORE), nullable=False
    )

    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    rows_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    batch: Mapped[ImportBatch] = relationship(back_populates="stages")


class ImportSourceRow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row from one batch, and what became of it.

    ``payload_redacted`` holds the row **with its identity object removed** -
    not masked, removed. A masked identifier is still an identifier, and this
    table is read by operators, analysts and anyone debugging an import, none of
    whom hold the re-identification permission.

    Kept for every row, loaded or not. A loaded row's payload is what lets an
    operator answer "what did the source actually say" without going back to the
    facility, and a quarantined row's payload is the whole point.
    """

    __tablename__ = "import_source_row"
    __table_args__ = (
        # A row appears once per batch. The pipeline can therefore be re-run
        # over a partially processed batch without recording anything twice.
        UniqueConstraint(
            "import_batch_id",
            "source_row_reference",
            name="uq_import_source_row_batch_reference",
        ),
        Index("ix_import_source_row_batch", "import_batch_id"),
        Index("ix_import_source_row_outcome", "import_batch_id", "outcome"),
        Index("ix_import_source_row_encounter", "opd_encounter_id"),
        {
            "schema": CORE,
            "comment": (
                "One inbound row and its outcome. payload_redacted has the "
                "identity object removed rather than masked: a masked value is "
                "still a value, and this table is not the vault."
            ),
        },
    )

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.import_batch.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: The source system's own row identifier. Must be stable for the life of
    #: the row in that system: it is what makes a replay idempotent.
    source_row_reference: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Line number in the artefact, for pointing an operator at the right place.
    source_line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    outcome: Mapped[SourceRowOutcome] = mapped_column(
        pg_enum(SourceRowOutcome, name="source_row_outcome", schema=CORE), nullable=False
    )

    #: The encounter this row produced, when it produced one.
    opd_encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.opd_encounter.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: The row as received, minus identity. See the class docstring.
    payload_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: SHA-256 of the row's bytes as received. Lets a re-send tell "unchanged"
    #: from "revised" without comparing every field.
    payload_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")
    issues: Mapped[list[ImportValidationIssue]] = relationship(
        back_populates="row", cascade="all, delete-orphan"
    )


class ImportValidationIssue(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One thing wrong with one row, or with the batch.

    Every issue carries a machine-readable ``code`` and a ``field_path`` so a
    producer can fix a mapping programmatically, and a ``message`` a human can
    read. The message is written to be safe to display: it names the field and
    the code that was not understood, never a patient value.
    """

    __tablename__ = "import_validation_issue"
    __table_args__ = (
        Index("ix_import_validation_issue_batch", "import_batch_id"),
        Index("ix_import_validation_issue_row", "import_source_row_id"),
        Index("ix_import_validation_issue_code", "import_batch_id", "code"),
        {
            "schema": CORE,
            "comment": (
                "Validation findings. Messages are written to be safe to "
                "display: they name the field and the unrecognised code, never "
                "a patient value."
            ),
        },
    )

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.import_batch.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Null for a batch-level issue: an unknown schema version belongs to the
    #: batch, not to any row.
    import_source_row_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        # Named explicitly: the convention would produce a 65-character
        # identifier, and PostgreSQL truncates silently at 63 - which turns a
        # readable constraint name into a mystery in an error message.
        ForeignKey(
            f"{CORE}.import_source_row.id",
            ondelete="CASCADE",
            name="fk_import_issue_source_row",
        ),
        nullable=True,
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[ValidationSeverity] = mapped_column(
        pg_enum(ValidationSeverity, name="validation_severity", schema=CORE), nullable=False
    )

    #: Dotted path into the inbound row, e.g. ``tests[0].result``. Empty for a
    #: batch-level issue.
    field_path: Mapped[str | None] = mapped_column(String(160), nullable=True)

    message: Mapped[str] = mapped_column(Text, nullable=False)

    #: Structured detail, e.g. the accepted value set. Never a patient value.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    row: Mapped[ImportSourceRow | None] = relationship(back_populates="issues")


__all__ = [
    "ImportBatch",
    "ImportSourceRow",
    "ImportStageExecution",
    "ImportValidationIssue",
]
