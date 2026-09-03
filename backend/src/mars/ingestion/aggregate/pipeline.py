"""Loading HMIS 033b and 105 submissions.

Simpler than the encounter pipeline in one respect and harder in another.

Simpler: an aggregate carries no identity. There is no vault, no linkage, no
second database role - a submission is a form, and a form is counts.

Harder: **a correction is not an overwrite.** Re-sending the same revision of
the same form is a no-op; sending a higher revision creates a new submission
and marks the old one superseded. The old figures stay readable, because the
district acted on them and a record that shows only the corrected number cannot
explain what anyone did.

Idempotency rests on ``(facility, form, period_start, period_end, revision)``
being unique in the database. As in the encounter pipeline, the constraint
decides and the pipeline absorbs the conflict, rather than checking first and
racing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.aggregate import (
    AggregateObservation,
    AggregateSubmission,
    CommodityStockObservation,
    LaboratoryTestObservation,
)
from mars.domain.enums import (
    AggregateSubmissionStatus,
    ImportBatchStatus,
    ImportStage,
    SourceRowOutcome,
    ValidationSeverity,
)
from mars.domain.ingestion import (
    ImportBatch,
    ImportSourceRow,
    ImportStageExecution,
    ImportValidationIssue,
)
from mars.domain.organisation import Facility, FacilityIdentifier
from mars.ingestion.aggregate.contract import (
    INGEST_METHOD_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    AggregateAdapter,
    AggregateContractError,
    InboundAggregateEnvelope,
    JsonLinesAggregateAdapter,
)
from mars.ingestion.aggregate.validation import (
    AggregateValidator,
    Issue,
    SubmissionValidation,
    ValidatedSubmission,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AggregateIngestOptions:
    """How to run one batch of submissions."""

    #: Read and validate, write nothing.
    dry_run: bool = False
    #: Record findings without writing submissions.
    validate_only: bool = False
    #: Re-process a batch with the same artefact checksum. Already written
    #: submissions are verified by content checksum and remain unchanged.
    resume: bool = False
    initiated_by: str | None = None


@dataclass(slots=True)
class AggregateIngestReport:
    """What happened, in numbers an operator can act on."""

    batch_id: uuid.UUID | None = None
    status: ImportBatchStatus = ImportBatchStatus.RECEIVED
    submissions_received: int = 0
    submissions_loaded: int = 0
    submissions_unchanged: int = 0
    submissions_superseding: int = 0
    submissions_quarantined: int = 0
    observations_loaded: int = 0
    stock_rows_loaded: int = 0
    laboratory_rows_loaded: int = 0
    blank_cells: int = 0
    zero_cells: int = 0
    unresolved_facility: int = 0
    warning_count: int = 0
    error_count: int = 0
    issue_codes: dict[str, int] = field(default_factory=dict)
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.failure_reason is None

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "status": self.status.value,
            "submissions_received": self.submissions_received,
            "submissions_loaded": self.submissions_loaded,
            "submissions_unchanged": self.submissions_unchanged,
            "submissions_superseding": self.submissions_superseding,
            "submissions_quarantined": self.submissions_quarantined,
            "observations_loaded": self.observations_loaded,
            "stock_rows_loaded": self.stock_rows_loaded,
            "laboratory_rows_loaded": self.laboratory_rows_loaded,
            "blank_cells": self.blank_cells,
            "zero_cells": self.zero_cells,
            "unresolved_facility": self.unresolved_facility,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "issue_codes": dict(sorted(self.issue_codes.items())),
            "failure_reason": self.failure_reason,
        }


class AggregateIngestionPipeline:
    """Loads one artefact of HMIS submissions."""

    def __init__(
        self,
        session: Session,
        *,
        adapter: AggregateAdapter | None = None,
        validator: AggregateValidator | None = None,
    ) -> None:
        self._session = session
        self._adapter = adapter or JsonLinesAggregateAdapter()
        self._validator = validator or AggregateValidator()
        self._persist = True

    def run(
        self, artefact: Path, options: AggregateIngestOptions | None = None
    ) -> AggregateIngestReport:
        options = options or AggregateIngestOptions()
        self._persist = not options.dry_run
        report = AggregateIngestReport()

        checksum, size = _checksum(artefact)

        try:
            envelope = self._adapter.envelope(artefact)
        except AggregateContractError as exc:
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = str(exc)
            logger.warning("aggregate_envelope_unreadable", artefact=artefact.name)
            return report

        batch, existing = self._open_batch(envelope, artefact, checksum, size, options, report)
        report.batch_id = batch.id

        if existing and batch.is_terminal and not options.resume:
            report.status = batch.import_status
            _copy_counters(batch, report)
            for code, count in self._session.execute(
                select(
                    ImportValidationIssue.code,
                    func.count(ImportValidationIssue.id),
                )
                .where(ImportValidationIssue.import_batch_id == batch.id)
                .group_by(ImportValidationIssue.code)
            ).all():
                report.issue_codes[code] = count
            # These bytes caused no writes in this invocation. Preserve the
            # stored terminal status and rejected count, but report every
            # previously loadable submission as unchanged rather than claiming
            # it was loaded again.
            if batch.import_status in {
                ImportBatchStatus.COMPLETED,
                ImportBatchStatus.PARTIALLY_COMPLETED,
            }:
                report.submissions_unchanged = (
                    batch.rows_loaded + batch.rows_updated + batch.rows_unchanged
                )
                report.submissions_loaded = 0
                report.submissions_superseding = 0
            report.failure_reason = batch.failure_reason
            return report

        if envelope.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            failure = (
                f"unsupported schema_version {envelope.schema_version!r}; "
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
            self._record_issue(
                batch,
                None,
                Issue(
                    code="unsupported_schema_version",
                    severity=ValidationSeverity.FATAL,
                    message="the batch declares a contract version this build cannot read",
                ),
                report,
            )
            batch.import_status = ImportBatchStatus.FAILED
            batch.failure_reason = failure
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = failure
            if self._persist:
                self._session.flush()
            return report

        batch.import_status = ImportBatchStatus.VALIDATING
        validation_started = datetime.now(UTC)
        try:
            validations = list(self._read(artefact))
        except AggregateContractError as exc:
            batch.import_status = ImportBatchStatus.FAILED
            batch.failure_reason = str(exc)
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = str(exc)
            if self._persist:
                self._session.flush()
            return report

        report.submissions_received = len(validations)
        if envelope.declared_submission_count != len(validations):
            # A truncated upload is the commonest way a batch goes wrong, and a
            # short month of aggregate returns looks like a reporting failure
            # at the facilities rather than at the transfer.
            failure = (
                f"declared {envelope.declared_submission_count} submissions, "
                f"read {len(validations)}"
            )
            self._record_issue(
                batch,
                None,
                Issue(
                    code="submission_count_mismatch",
                    severity=ValidationSeverity.FATAL,
                    message="the envelope declares a different number of submissions",
                    context={
                        "declared": envelope.declared_submission_count,
                        "read": len(validations),
                    },
                ),
                report,
            )
            batch.import_status = ImportBatchStatus.FAILED
            batch.failure_reason = failure
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = failure
            _apply_counters(batch, report)
            if self._persist:
                self._session.flush()
            return report

        rejected = sum(1 for validation in validations if not validation.is_loadable)
        self._record_stage(
            batch, ImportStage.VALIDATE, validation_started, len(validations), rejected
        )

        if options.validate_only or options.dry_run:
            for validation in validations:
                for issue in validation.issues:
                    self._count(issue, report)
                if not validation.is_loadable:
                    report.submissions_quarantined += 1
                    self._record_row(
                        batch, validation, None, SourceRowOutcome.QUARANTINED, persist_issues=True
                    )
            report.status = (
                ImportBatchStatus.QUARANTINED
                if validations and rejected == len(validations)
                else ImportBatchStatus.VALIDATING
            )
            batch.import_status = report.status
            _apply_counters(batch, report)
            if self._persist:
                self._session.flush()
            return report

        batch.import_status = ImportBatchStatus.LOADING
        loading_started = datetime.now(UTC)
        for validation in validations:
            self._load(batch, validation, envelope, report)

        self._record_stage(
            batch,
            ImportStage.WRITE_CANONICAL,
            loading_started,
            len(validations),
            report.submissions_quarantined,
        )
        if report.submissions_quarantined == 0:
            batch.import_status = ImportBatchStatus.COMPLETED
        elif report.submissions_loaded or report.submissions_unchanged:
            batch.import_status = ImportBatchStatus.PARTIALLY_COMPLETED
        else:
            batch.import_status = ImportBatchStatus.QUARANTINED
        batch.completed_at = datetime.now(UTC)
        report.status = batch.import_status
        _apply_counters(batch, report)
        self._session.flush()

        logger.info(
            "aggregate_batch_finished",
            artefact=artefact.name,
            **{k: v for k, v in report.as_dict().items() if isinstance(v, int)},
        )
        return report

    def _open_batch(
        self,
        envelope: InboundAggregateEnvelope,
        artefact: Path,
        checksum: str,
        size: int,
        options: AggregateIngestOptions,
        report: AggregateIngestReport,
    ) -> tuple[ImportBatch, bool]:
        existing = self._session.execute(
            select(ImportBatch).where(
                ImportBatch.import_domain == "aggregate",
                ImportBatch.source_system == envelope.source_system,
                ImportBatch.artefact_checksum == checksum,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True

        batch = ImportBatch(
            import_domain="aggregate",
            source_system=envelope.source_system,
            schema_version=envelope.schema_version,
            artefact_checksum=checksum,
            artefact_name=artefact.name,
            artefact_size_bytes=size,
            extracted_at=envelope.extracted_at,
            received_at=datetime.now(UTC),
            declared_row_count=envelope.declared_submission_count,
            ingest_method_version=INGEST_METHOD_VERSION,
            initiated_by=options.initiated_by,
            import_status=ImportBatchStatus.RECEIVED,
        )
        if options.dry_run:
            return batch, False

        savepoint = self._session.begin_nested()
        try:
            self._session.add(batch)
            self._session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            raced = self._session.execute(
                select(ImportBatch).where(
                    ImportBatch.import_domain == "aggregate",
                    ImportBatch.source_system == envelope.source_system,
                    ImportBatch.artefact_checksum == checksum,
                )
            ).scalar_one_or_none()
            if raced is None:
                raise
            return raced, True
        return batch, False

    # -- Reading -----------------------------------------------------------
    def _read(self, artefact: Path) -> Iterator[SubmissionValidation]:
        seen: set[tuple[str, str, str, str, int]] = set()
        for inbound in self._adapter.submissions(artefact):
            validation = self._validator.validate(inbound)
            if inbound.key in seen:
                validation.issues.append(
                    Issue(
                        code="duplicate_submission_in_batch",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            "the same facility, form, period and revision appears "
                            "twice; the two are indistinguishable on a replay"
                        ),
                    )
                )
            seen.add(inbound.key)
            yield validation

    # -- Writing -----------------------------------------------------------
    def _load(
        self,
        batch: ImportBatch,
        validation: SubmissionValidation,
        envelope: InboundAggregateEnvelope,
        report: AggregateIngestReport,
    ) -> None:
        for issue in validation.issues:
            self._count(issue, report)

        if not validation.is_loadable:
            report.submissions_quarantined += 1
            self._record_row(
                batch, validation, None, SourceRowOutcome.QUARANTINED, persist_issues=True
            )
            return

        data = validation.submission
        assert data is not None

        facility = self._resolve_facility(data.facility_code, envelope.source_system)
        if facility is None:
            # A month of a facility's returns must not be attached to a guessed
            # facility. Counted separately from other quarantines because it is
            # an organisation-register problem, not a transcription one.
            report.unresolved_facility += 1
            report.submissions_quarantined += 1
            issue = Issue(
                code="facility_unresolved",
                severity=ValidationSeverity.ERROR,
                message="the facility code does not resolve",
                field_path="facility_code",
                context={"facility_code": data.facility_code},
            )
            validation.issues.append(issue)
            self._count(issue, report)
            self._record_row(
                batch, validation, None, SourceRowOutcome.QUARANTINED, persist_issues=True
            )
            return

        self._count_cells(validation, report)

        # Serialize one logical form/period without requiring a separate lock
        # table. This also covers the empty-series race where SELECT FOR UPDATE
        # has no row to lock.
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _series_lock_key(facility.id, data)},
        )

        checksum = _submission_checksum(validation)
        series = (
            self._session.execute(
                select(AggregateSubmission)
                .where(
                    AggregateSubmission.facility_id == facility.id,
                    AggregateSubmission.form == data.form,
                    AggregateSubmission.period_start == data.period_start,
                    AggregateSubmission.period_end == data.period_end,
                )
                .order_by(AggregateSubmission.revision)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        existing = next((row for row in series if row.revision == data.revision), None)
        if existing is not None:
            if existing.payload_checksum == checksum:
                report.submissions_unchanged += 1
                self._record_row(
                    batch, validation, existing, SourceRowOutcome.UNCHANGED, persist_issues=True
                )
            else:
                issue = Issue(
                    code="revision_payload_conflict",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        "this revision already exists with different content; "
                        "increment revision instead of overwriting history"
                    ),
                    field_path="revision",
                    context={"revision": data.revision},
                )
                validation.issues.append(issue)
                self._count(issue, report)
                report.submissions_quarantined += 1
                self._record_row(
                    batch, validation, None, SourceRowOutcome.QUARANTINED, persist_issues=True
                )
            return

        prior = [row for row in series if row.revision < data.revision]
        superseded = prior[-1] if prior else None
        latest_revision = series[-1].revision if series else 0
        is_latest = data.revision > latest_revision

        submission = AggregateSubmission(
            facility_id=facility.id,
            form=data.form,
            period_type=data.period_type,
            period_start=data.period_start,
            period_end=data.period_end,
            period_label_raw=data.period_label,
            revision=data.revision,
            submission_status=AggregateSubmissionStatus.RECEIVED,
            supersedes_id=superseded.id if superseded is not None else None,
            source_system=envelope.source_system,
            source_reference=data.source_reference,
            source_batch_id=batch.id,
            ingest_method_version=INGEST_METHOD_VERSION,
            payload_checksum=checksum,
            received_at=datetime.now(UTC),
            reported_on=data.reported_on,
            remarks=data.remarks,
        )

        submission.observations = [
            AggregateObservation(
                element_code=observation.element_code,
                age_band=observation.age_band,
                sex=observation.sex,
                value=observation.value,
                raw_value=observation.raw_value,
            )
            for observation in data.observations
        ]
        submission.stock_observations = [
            CommodityStockObservation(
                commodity_code=row.commodity_code,
                metric=row.metric,
                value=row.value,
                unit_of_issue=row.unit_of_issue,
                raw_value=row.raw_value,
            )
            for row in data.stock
        ]
        submission.laboratory_observations = [
            LaboratoryTestObservation(
                test_code=row.test_code,
                number_done=row.number_done,
                number_positive=row.number_positive,
                raw_done=row.raw_done,
                raw_positive=row.raw_positive,
            )
            for row in data.laboratory
        ]

        self._session.add(submission)
        self._session.flush()

        if is_latest:
            for previous in series:
                if previous.submission_status is AggregateSubmissionStatus.ACCEPTED:
                    previous.submission_status = AggregateSubmissionStatus.SUPERSEDED
            # Flush demotions before promoting the new row so the partial
            # unique index always sees at most one accepted revision.
            self._session.flush()
            submission.submission_status = AggregateSubmissionStatus.ACCEPTED
        else:
            submission.submission_status = AggregateSubmissionStatus.SUPERSEDED

        if superseded is not None and is_latest:
            report.submissions_superseding += 1

        self._session.flush()

        report.submissions_loaded += 1
        report.observations_loaded += len(submission.observations)
        report.stock_rows_loaded += len(submission.stock_observations)
        report.laboratory_rows_loaded += len(submission.laboratory_observations)
        self._record_row(
            batch, validation, submission, SourceRowOutcome.LOADED, persist_issues=True
        )

    def _resolve_facility(self, code: str, source_system: str) -> Facility | None:
        """By the facility's own code, then by that source system's identifier.

        Never by name: facility names repeat across districts, and a name match
        would attribute one facility's month to another.
        """
        facility = self._session.execute(
            select(Facility).where(Facility.code == code, Facility.is_active.is_(True))
        ).scalar_one_or_none()
        if facility is not None:
            return facility

        return self._session.execute(
            select(Facility)
            .join(FacilityIdentifier, FacilityIdentifier.facility_id == Facility.id)
            .where(
                FacilityIdentifier.source_system == source_system,
                FacilityIdentifier.external_id == code,
                Facility.is_active.is_(True),
            )
        ).scalar_one_or_none()

    # -- Persistent lifecycle --------------------------------------------
    def _record_row(
        self,
        batch: ImportBatch,
        validation: SubmissionValidation,
        submission: AggregateSubmission | None,
        outcome: SourceRowOutcome,
        *,
        persist_issues: bool,
    ) -> None:
        if not self._persist:
            return

        reference = f"aggregate-line:{validation.inbound.line_number}"
        row = self._session.execute(
            select(ImportSourceRow).where(
                ImportSourceRow.import_batch_id == batch.id,
                ImportSourceRow.source_row_reference == reference,
            )
        ).scalar_one_or_none()
        if row is None:
            row = ImportSourceRow(
                import_batch_id=batch.id,
                source_row_reference=reference,
                source_line_number=validation.inbound.line_number,
                outcome=outcome,
            )
            self._session.add(row)
        row.outcome = outcome
        row.opd_encounter_id = None
        row.aggregate_submission_id = submission.id if submission is not None else None
        row.payload_redacted = validation.inbound.raw
        row.payload_checksum = _submission_checksum(validation)
        self._session.flush()

        if persist_issues:
            # Resume replaces the prior diagnostic snapshot; it must not
            # multiply identical issue rows every time an operator retries.
            self._session.execute(
                delete(ImportValidationIssue).where(
                    ImportValidationIssue.import_source_row_id == row.id
                )
            )
            for issue in validation.issues:
                self._persist_issue(batch, row, issue)

    def _record_issue(
        self,
        batch: ImportBatch,
        row: ImportSourceRow | None,
        issue: Issue,
        report: AggregateIngestReport,
    ) -> None:
        self._count(issue, report)
        if self._persist:
            self._persist_issue(batch, row, issue)

    def _persist_issue(self, batch: ImportBatch, row: ImportSourceRow | None, issue: Issue) -> None:
        self._session.add(
            ImportValidationIssue(
                import_batch_id=batch.id,
                import_source_row_id=row.id if row is not None else None,
                code=issue.code,
                severity=issue.severity,
                field_path=issue.field_path,
                message=issue.message,
                context=issue.context,
            )
        )

    def _record_stage(
        self,
        batch: ImportBatch,
        stage: ImportStage,
        started: datetime,
        rows_in: int,
        rejected: int,
    ) -> None:
        if not self._persist:
            return
        execution = self._session.execute(
            select(ImportStageExecution).where(
                ImportStageExecution.import_batch_id == batch.id,
                ImportStageExecution.stage == stage,
            )
        ).scalar_one_or_none()
        if execution is None:
            execution = ImportStageExecution(
                import_batch_id=batch.id,
                stage=stage,
                started_at=started,
            )
            self._session.add(execution)
        execution.finished_at = datetime.now(UTC)
        execution.rows_in = rows_in
        execution.rows_out = rows_in - rejected
        execution.rows_rejected = rejected
        self._session.flush()

    # -- Counting ----------------------------------------------------------
    def _count_cells(self, validation: SubmissionValidation, report: AggregateIngestReport) -> None:
        """Blank and zero counted separately, because they mean different things.

        A month of blanks and a month of zeros look identical in a total and
        are opposite facts about whether the facility reported.
        """
        data = validation.submission
        assert data is not None
        for observation in data.observations:
            if observation.value is None:
                report.blank_cells += 1
            elif observation.value == 0:
                report.zero_cells += 1

    def _count(self, issue: Issue, report: AggregateIngestReport) -> None:
        report.issue_codes[issue.code] = report.issue_codes.get(issue.code, 0) + 1
        if issue.severity is ValidationSeverity.WARNING:
            report.warning_count += 1
        else:
            report.error_count += 1


def _checksum(artefact: Path) -> tuple[str, int]:
    """SHA-256 and byte length of an artefact."""
    digest = hashlib.sha256()
    size = 0
    with artefact.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def artefact_checksum(artefact: Path) -> str:
    """SHA-256 of an artefact's bytes (public compatibility helper)."""
    return _checksum(artefact)[0]


def _submission_checksum(validation: SubmissionValidation) -> str:
    """Canonical content identity for one logical submission revision."""
    return hashlib.sha256(
        json.dumps(
            validation.inbound.raw,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _series_lock_key(facility_id: uuid.UUID, data: ValidatedSubmission) -> int:
    identity = "|".join(
        (
            str(facility_id),
            data.form.value,
            data.period_start.isoformat(),
            data.period_end.isoformat(),
        )
    )
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)


def _apply_counters(batch: ImportBatch, report: AggregateIngestReport) -> None:
    batch.rows_received = report.submissions_received
    batch.rows_loaded = report.submissions_loaded
    batch.rows_updated = report.submissions_superseding
    batch.rows_unchanged = report.submissions_unchanged
    batch.rows_quarantined = report.submissions_quarantined
    batch.unresolved_geography = report.unresolved_facility
    batch.warning_count = report.warning_count
    batch.error_count = report.error_count


def _copy_counters(batch: ImportBatch, report: AggregateIngestReport) -> None:
    report.submissions_received = batch.rows_received
    report.submissions_loaded = batch.rows_loaded
    report.submissions_superseding = batch.rows_updated
    report.submissions_unchanged = batch.rows_unchanged
    report.submissions_quarantined = batch.rows_quarantined
    report.unresolved_facility = batch.unresolved_geography
    report.warning_count = batch.warning_count
    report.error_count = batch.error_count


__all__ = [
    "AggregateIngestOptions",
    "AggregateIngestReport",
    "AggregateIngestionPipeline",
    "artefact_checksum",
]
