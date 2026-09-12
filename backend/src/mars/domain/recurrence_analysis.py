"""Retained clinical evidence, reusable definitions and reproducible analysis runs.

Five tables in ``mars_analytics``:

``clinical_evidence_dataset``
    One immutable canonical evidence dataset, encrypted at rest, identified by a
    deterministic fingerprint of its content and coverage. It records the exact
    authorised facility set, the stages and dates it covers, the source,
    mapping and contract versions, and the encryption key version. A definition
    change re-evaluates a retained dataset; it does not refetch the source.

``recurrence_definition`` and ``recurrence_definition_version``
    A named, reusable exploratory definition and its immutable versions:
    parameters, checksum, engine version, creator and time. These are not an
    approval system. Promotion to an official method goes through the existing
    governance registry (``method_definition`` / ``method_version``).

``recurrence_analysis_run``
    One Apply. Its manifest - definition parameters, dates, scope, filters and
    creator - is frozen when it is submitted, and a database trigger refuses any
    change to it. Lifecycle fields may move; a completed or failed run is frozen
    entirely.

``patient_recurrence_finding``
    One patient's finding within one run, under a namespace-keyed display alias.
    Filterable categories are stored in clear; the finding's detail and
    evidence references are encrypted and bound to the row.

No patient evidence is stored in clear. Aggregate projections are, because they
carry no patient row and must be readable by aggregate-only users.
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from mars.db.schemas import ANALYTICS, GOVERNANCE

RUN_STATUSES = ("queued", "running", "completed", "failed")
RUN_MODES = ("exploratory", "programme")
SOURCE_KINDS = ("stored_encounter", "live_tracker")
DETERMINATIONS = ("qualifies", "does_not_qualify", "indeterminate", "not_in_period")


class ClinicalEvidenceDataset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One retained, encrypted canonical evidence dataset."""

    __tablename__ = "clinical_evidence_dataset"
    __table_args__ = (
        UniqueConstraint(
            "source_kind",
            "scope_key",
            "mapping_version",
            "dataset_hash",
            "key_version",
            name="uq_clinical_evidence_dataset_identity",
        ),
        CheckConstraint("length(dataset_hash) = 64", name="dataset_hash_is_sha256"),
        CheckConstraint(
            "source_kind IN ('stored_encounter', 'live_tracker')", name="source_kind_known"
        ),
        CheckConstraint(
            "coverage_start IS NULL OR coverage_end IS NULL OR coverage_end >= coverage_start",
            name="coverage_ordered",
        ),
        CheckConstraint("encounter_count >= 0 AND patient_count >= 0", name="counts_not_negative"),
        Index("ix_clinical_evidence_dataset_scope", "scope_key", "coverage_end"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Immutable canonical evidence, encrypted at rest and identified by a "
                "content and coverage fingerprint. UPDATE is refused by trigger."
            ),
        },
    )

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    namespace: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Binds the dataset to the authorised source scope it was built for.
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The exact authorised facility set, as facility references.
    facility_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    coverage_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    coverage_notes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    stages: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    encounter_count: Mapped[int] = mapped_column(Integer, nullable=False)
    patient_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: AES-GCM ciphertext of the encoded dataset, bound to this row.
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)


class RecurrenceDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named, reusable exploratory definition owned by one user."""

    __tablename__ = "recurrence_definition"
    __table_args__ = (
        UniqueConstraint("owner_subject", "name", name="uq_recurrence_definition_owner_name"),
        CheckConstraint("mode IN ('exploratory')", name="mode_known"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        {"schema": ANALYTICS, "comment": "Reusable exploratory recurrence definitions."},
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="exploratory")
    owner_subject: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_username: Mapped[str] = mapped_column(String(160), nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    versions: Mapped[list[RecurrenceDefinitionVersion]] = relationship(
        back_populates="definition", order_by="RecurrenceDefinitionVersion.version_number"
    )


class RecurrenceDefinitionVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An immutable version of a definition. UPDATE and DELETE are refused."""

    __tablename__ = "recurrence_definition_version"
    __table_args__ = (
        UniqueConstraint(
            "definition_id", "version_number", name="uq_recurrence_definition_version_number"
        ),
        UniqueConstraint(
            "definition_id", "checksum", name="uq_recurrence_definition_version_checksum"
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        CheckConstraint("length(checksum) = 64", name="checksum_is_sha256"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Immutable recurrence definition versions. UPDATE/DELETE refused by trigger."
            ),
        },
    )

    definition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.recurrence_definition.id",
            ondelete="RESTRICT",
            name="fk_recurrence_definition_version_definition",
        ),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    definition: Mapped[RecurrenceDefinition] = relationship(back_populates="versions")


class RecurrenceAnalysisRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One reproducible Apply: a frozen manifest, a lifecycle and its results."""

    __tablename__ = "recurrence_analysis_run"
    __table_args__ = (
        UniqueConstraint(
            "created_by_subject",
            "idempotency_key",
            name="uq_recurrence_analysis_run_idempotency",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("mode IN ('exploratory', 'programme')", name="mode_known"),
        CheckConstraint(
            "run_status IN ('queued', 'running', 'completed', 'failed')", name="status_known"
        ),
        CheckConstraint(
            "source_kind IN ('stored_encounter', 'live_tracker')", name="source_kind_known"
        ),
        CheckConstraint(
            "run_status <> 'completed' OR (dataset_id IS NOT NULL AND completed_at IS NOT NULL)",
            name="completed_run_has_dataset",
        ),
        CheckConstraint(
            "mode <> 'programme' OR method_version_id IS NOT NULL",
            name="programme_run_names_its_method",
        ),
        CheckConstraint("length(manifest_checksum) = 64", name="manifest_is_sha256"),
        Index("ix_recurrence_analysis_run_scope", "scope_key", "created_at"),
        Index("ix_recurrence_analysis_run_creator", "created_by_subject", "created_at"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Recurrence analysis runs. The manifest is immutable by trigger; a "
                "completed or failed run is frozen entirely."
            ),
        },
    )

    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.recurrence_definition_version.id",
            ondelete="RESTRICT",
            name="fk_recurrence_run_definition_version",
        ),
        nullable=True,
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.method_version.id",
            ondelete="RESTRICT",
            name="fk_recurrence_run_method_version",
        ),
        nullable=True,
    )
    definition_name: Mapped[str] = mapped_column(String(160), nullable=False)
    definition_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    facility_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_subject: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_username: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.clinical_evidence_dataset.id",
            ondelete="RESTRICT",
            name="fk_recurrence_run_dataset",
        ),
        nullable=True,
    )
    dataset_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run_status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    progress_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Aggregate counts only; never a patient row.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Facility, interval, week, frequency, treatment and geography projections.
    projections: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Duplicate review rows carry encounter and source-event references.
    duplicate_payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PatientRecurrenceFinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One patient's finding within one run. Immutable; detail encrypted."""

    __tablename__ = "patient_recurrence_finding"
    __table_args__ = (
        UniqueConstraint("run_id", "patient_alias", name="uq_patient_recurrence_finding_alias"),
        CheckConstraint(
            "determination IN ('qualifies', 'does_not_qualify', 'indeterminate', 'not_in_period')",
            name="determination_known",
        ),
        Index("ix_patient_recurrence_finding_run", "run_id", "determination"),
        Index("ix_patient_recurrence_finding_order", "run_id", "latest_positive_on"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Pseudonymous per-patient findings. Detail is encrypted; UPDATE is "
                "refused by trigger."
            ),
        },
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.recurrence_analysis_run.id",
            ondelete="CASCADE",
            name="fk_patient_recurrence_finding_run",
        ),
        nullable=False,
    )
    patient_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    alias_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    determination: Mapped[str] = mapped_column(String(24), nullable=False)
    chain_length: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_positive_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_positive_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chain_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_positive_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_positive_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    final_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    final_facility_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    in_period_facility_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    facility_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    #: Normalised names of medicines linked before a later positive, for the
    #: regimen filter. Recorded names only; nothing is inferred.
    treatment_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    test_methods: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    cross_facility: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sex: Mapped[str] = mapped_column(String(16), nullable=False)
    age_group: Mapped[str] = mapped_column(String(16), nullable=False)
    #: AES-GCM ciphertext of the full finding and its evidence references.
    detail: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


__all__ = [
    "DETERMINATIONS",
    "RUN_MODES",
    "RUN_STATUSES",
    "SOURCE_KINDS",
    "ClinicalEvidenceDataset",
    "PatientRecurrenceFinding",
    "RecurrenceAnalysisRun",
    "RecurrenceDefinition",
    "RecurrenceDefinitionVersion",
]
