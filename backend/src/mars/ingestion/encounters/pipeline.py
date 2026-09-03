"""The e-register ingestion pipeline.

Four stages, and the boundaries between them are the design:

```
read → validate → link identity → write canonical
                  (identity role)  (application role)
```

**The identity stage is the only one that sees an identifier.** It hands the
canonical stage a ``patient_reference_id`` and a linkage confidence, and nothing
else crosses. A quarantined row is stored with its identity block removed rather
than masked, because a masked identifier is still an identifier and the
quarantine table is read by everyone debugging an import.

**The two stages use different database roles**, so a single transaction cannot
span them. That is deliberate - a cross-role transaction would need one
connection holding both sets of privileges, which is the arrangement the whole
boundary exists to prevent. Failure between the stages is handled by idempotency
instead: the linkage is already recorded in the vault, the canonical write is
retried, and re-running the batch reaches the same state.

**Idempotency is enforced by constraints, not by checks.** Every place a
check-then-act race would produce duplicates has a unique constraint behind it,
and the pipeline absorbs the conflict rather than pre-empting it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterDiagnosis,
    OpdEncounterPrescription,
    OpdEncounterReferral,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.enums import (
    GeographyLevel,
    IdentifierType,
    ImportBatchStatus,
    ImportStage,
    LinkageConfidence,
    SourceRowOutcome,
    ValidationSeverity,
)
from mars.domain.geography import GeographyUnit
from mars.domain.ingestion import (
    ImportBatch,
    ImportSourceRow,
    ImportStageExecution,
    ImportValidationIssue,
)
from mars.domain.organisation import Facility, FacilityIdentifier
from mars.ingestion.encounters.contract import (
    INGEST_METHOD_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    ContractError,
    InboundAdapter,
    InboundEnvelope,
    InboundIdentity,
    InboundRow,
    JsonLinesAdapter,
)
from mars.ingestion.encounters.validation import (
    EncounterValidator,
    Issue,
    RowValidation,
    ValidatedEncounter,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestOptions:
    """How to run one batch."""

    #: Read and validate, write **nothing at all** - not even the batch. Reports
    #: how many rows would be rejected and why. It does not report ``rows_loaded``,
    #: because nothing was loaded and a counter that says otherwise is a lie an
    #: operator will act on.
    dry_run: bool = False
    #: Stop after validation, but record the batch, the rejected rows and their
    #: issues, so a producer can be sent an actionable list before a load.
    validate_only: bool = False
    #: Continue a batch that was interrupted. Rows already written are
    #: recognised and counted as unchanged rather than rewritten.
    resume: bool = False
    initiated_by: str | None = None


@dataclass(slots=True)
class IngestReport:
    """What happened, in numbers an operator can act on.

    Every counter is separate on purpose. "1,000 rows processed" tells nobody
    whether the month loaded; "820 loaded, 140 unchanged, 40 quarantined" does.
    """

    batch_id: uuid.UUID | None = None
    status: ImportBatchStatus = ImportBatchStatus.RECEIVED
    rows_received: int = 0
    rows_loaded: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_quarantined: int = 0
    rows_linked: int = 0
    rows_unlinked: int = 0
    unresolved_geography: int = 0
    warning_count: int = 0
    error_count: int = 0
    issue_codes: dict[str, int] = field(default_factory=dict)
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {
            ImportBatchStatus.COMPLETED,
            ImportBatchStatus.PARTIALLY_COMPLETED,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "status": self.status.value,
            "rows_received": self.rows_received,
            "rows_loaded": self.rows_loaded,
            "rows_updated": self.rows_updated,
            "rows_unchanged": self.rows_unchanged,
            "rows_quarantined": self.rows_quarantined,
            "rows_linked": self.rows_linked,
            "rows_unlinked": self.rows_unlinked,
            "unresolved_geography": self.unresolved_geography,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "issue_codes": dict(sorted(self.issue_codes.items())),
            "failure_reason": self.failure_reason,
        }


class IdentityLinker:
    """What the pipeline needs from the identity boundary.

    Deliberately this small. The pipeline is handed something that turns an
    identity block into a reference, and can do nothing else with it - so no
    amount of pipeline code can read a name back out.
    """

    def link(self, identity: InboundIdentity) -> tuple[uuid.UUID | None, LinkageConfidence]:
        raise NotImplementedError


class NullIdentityLinker(IdentityLinker):
    """Used when no identity component is configured.

    Every row loads unlinked, which is honest: without the vault MARS cannot
    say two encounters belong to one person, and pretending otherwise by
    inventing per-row references would make every visitor look like a new
    patient and destroy re-attendance analysis.
    """

    def link(self, identity: InboundIdentity) -> tuple[uuid.UUID | None, LinkageConfidence]:
        return None, LinkageConfidence.UNLINKED


class LinkageService(Protocol):
    """The one method the pipeline may call on the identity service.

    Structural, not an import of ``IdentityService``. The pipeline must not be
    able to reach ``reidentify`` even by accident, and a Protocol that names
    only ``link`` makes that a type error rather than a code-review question.
    """

    def link(
        self,
        identifier_type: IdentifierType,
        raw_value: str,
        *,
        patient_reference_id: uuid.UUID,
        surname: str | None = None,
        given_name: str | None = None,
        phone_contact: str | None = None,
    ) -> LinkageOutcome: ...


class LinkageOutcome(Protocol):
    """What ``link`` returns, in the terms the pipeline is allowed to see."""

    @property
    def patient_reference_id(self) -> uuid.UUID: ...

    @property
    def confidence(self) -> LinkageConfidence: ...


class VaultIdentityLinker(IdentityLinker):
    """Links through the identity service, on the identity role's connection.

    Holds the only reference to the identity session in the whole pipeline. It
    creates the pseudonymous ``patient_reference`` row in ``mars_core`` through
    a supplied callback, because the vault's own role cannot write there - which
    is the boundary working as intended rather than an inconvenience.
    """

    def __init__(
        self,
        identity_service: LinkageService,
        reference_factory: Callable[[], uuid.UUID],
    ) -> None:
        self._service = identity_service
        self._reference_factory = reference_factory

    def link(self, identity: InboundIdentity) -> tuple[uuid.UUID | None, LinkageConfidence]:
        if identity.is_empty or not identity.identifier_value:
            return None, LinkageConfidence.UNLINKED

        try:
            identifier_type = IdentifierType(identity.identifier_type or "unspecified_scheme")
        except ValueError:
            # An unrecognised scheme name is not a reason to refuse the row, but
            # it must not merge with a known scheme either. UNSPECIFIED_SCHEME
            # is its own linkage domain, so the value links only to others like
            # it - conservative, and visible in the recorded confidence.
            identifier_type = IdentifierType.UNSPECIFIED_SCHEME

        result = self._service.link(
            identifier_type,
            identity.identifier_value,
            patient_reference_id=self._reference_factory(),
            surname=identity.surname,
            given_name=identity.given_name,
            phone_contact=identity.phone_contact,
        )
        return result.patient_reference_id, result.confidence


class EncounterIngestionPipeline:
    """Loads one artefact into canonical encounters."""

    def __init__(
        self,
        session: Session,
        *,
        adapter: InboundAdapter | None = None,
        identity_linker: IdentityLinker | None = None,
        validator: EncounterValidator | None = None,
    ) -> None:
        self._session = session
        self._adapter = adapter or JsonLinesAdapter()
        self._identity = identity_linker or NullIdentityLinker()
        self._validator = validator or EncounterValidator()
        #: Whether this run may write. A dry run reports what a load would do
        #: and must leave no trace, so every recording method checks this rather
        #: than each caller remembering to.
        self._persist = True

    # -- Entry point -------------------------------------------------------
    def run(self, artefact: Path, options: IngestOptions | None = None) -> IngestReport:
        options = options or IngestOptions()
        self._persist = not options.dry_run
        report = IngestReport()

        checksum, size = _checksum(artefact)

        try:
            envelope = self._adapter.envelope(artefact)
        except ContractError as exc:
            # No envelope means no batch identity, so there is nothing to
            # record against. Reported, not stored.
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = str(exc)
            logger.warning("ingest_envelope_unreadable", artefact=artefact.name)
            return report

        batch, existing = self._open_batch(envelope, artefact, checksum, size, options, report)
        if batch is None:
            return report

        report.batch_id = batch.id

        if existing and batch.is_terminal and not options.resume:
            # The same bytes have already been processed. Returning the previous
            # outcome is the whole point of keying a batch on its content.
            report.status = batch.import_status
            _copy_counters(batch, report)
            logger.info(
                "ingest_batch_already_processed",
                batch_id=str(batch.id),
                status=batch.import_status.value,
            )
            return report

        fatal = self._check_batch_preconditions(envelope, batch, report)
        if fatal is not None:
            batch.import_status = ImportBatchStatus.FAILED
            batch.failure_reason = fatal
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = fatal
            if not options.dry_run:
                self._session.flush()
            return report

        self._process(artefact, batch, options, report)
        return report

    # -- Batch bookkeeping -------------------------------------------------
    def _open_batch(
        self,
        envelope: InboundEnvelope,
        artefact: Path,
        checksum: str,
        size: int,
        options: IngestOptions,
        report: IngestReport,
    ) -> tuple[ImportBatch | None, bool]:
        """Find or create the batch for this artefact.

        Creation races on the unique constraint rather than checking first: two
        operators uploading the same file at the same moment is exactly when a
        check-then-act produces two batches and twice the encounters.
        """
        existing = self._session.execute(
            select(ImportBatch).where(
                ImportBatch.import_domain == "encounter",
                ImportBatch.source_system == envelope.source_system,
                ImportBatch.artefact_checksum == checksum,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True

        if options.dry_run:
            # A dry run must not create a batch: it reports what a load would do.
            return (
                ImportBatch(
                    import_domain="encounter",
                    source_system=envelope.source_system,
                    schema_version=envelope.schema_version,
                    artefact_checksum=checksum,
                    artefact_name=artefact.name,
                    artefact_size_bytes=size,
                    facility_code_raw=envelope.facility_code,
                    declared_row_count=envelope.declared_row_count,
                    received_at=datetime.now(UTC),
                    import_status=ImportBatchStatus.RECEIVED,
                ),
                False,
            )

        batch = ImportBatch(
            import_domain="encounter",
            source_system=envelope.source_system,
            schema_version=envelope.schema_version,
            artefact_checksum=checksum,
            artefact_name=artefact.name,
            artefact_size_bytes=size,
            facility_code_raw=envelope.facility_code,
            declared_row_count=envelope.declared_row_count,
            extracted_at=envelope.extracted_at,
            received_at=datetime.now(UTC),
            register_opened_on=envelope.register_opened_on,
            register_closed_on=envelope.register_closed_on,
            ingest_method_version=INGEST_METHOD_VERSION,
            initiated_by=options.initiated_by,
            import_status=ImportBatchStatus.RECEIVED,
        )
        savepoint = self._session.begin_nested()
        try:
            self._session.add(batch)
            self._session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            raced = self._session.execute(
                select(ImportBatch).where(
                    ImportBatch.import_domain == "encounter",
                    ImportBatch.source_system == envelope.source_system,
                    ImportBatch.artefact_checksum == checksum,
                )
            ).scalar_one_or_none()
            if raced is None:
                raise
            return raced, True
        return batch, False

    def _check_batch_preconditions(
        self, envelope: InboundEnvelope, batch: ImportBatch, report: IngestReport
    ) -> str | None:
        """Everything that makes a whole batch unusable.

        Each of these fails the batch rather than quarantining rows, because
        none of them can be true of only some rows.
        """
        if envelope.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            self._record_issue(
                batch,
                None,
                Issue(
                    code="unsupported_schema_version",
                    severity=ValidationSeverity.FATAL,
                    message=(
                        "the batch declares a schema version this build does not "
                        "support; guessing a mapping is how a field lands silently "
                        "in the wrong column"
                    ),
                    field_path="schema_version",
                    context={
                        "received": envelope.schema_version,
                        "supported": sorted(SUPPORTED_SCHEMA_VERSIONS),
                    },
                ),
                report,
            )
            return (
                f"unsupported schema_version {envelope.schema_version!r}; "
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )

        facility = self._resolve_facility(envelope.facility_code, envelope.source_system)
        if facility is None:
            self._record_issue(
                batch,
                None,
                Issue(
                    code="facility_unresolved",
                    severity=ValidationSeverity.FATAL,
                    message=(
                        "the facility code does not resolve; a month of attendance "
                        "must not be attached to a guessed facility"
                    ),
                    field_path="facility_code",
                    context={"facility_code": envelope.facility_code},
                ),
                report,
            )
            return f"facility code {envelope.facility_code!r} does not resolve"

        batch.facility_id = facility.id
        return None

    def _resolve_facility(self, code: str, source_system: str) -> Facility | None:
        """By the facility's own code, then by that source system's identifier.

        Never by name. Facility names repeat across districts, and a name match
        would attach one facility's attendance to another.

        The identifier lookup is constrained to the batch's own source system.
        ``(source_system, external_id)`` is unique, so the match is exactly one
        facility; matching on ``external_id`` alone would let one system's code
        collide with another's and silently reassign a month of attendance.
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

    # -- The stages --------------------------------------------------------
    def _process(
        self,
        artefact: Path,
        batch: ImportBatch,
        options: IngestOptions,
        report: IngestReport,
    ) -> None:
        batch.import_status = ImportBatchStatus.VALIDATING
        started = datetime.now(UTC)

        try:
            validations = list(self._read_and_validate(artefact, report))
        except ContractError as exc:
            batch.import_status = ImportBatchStatus.FAILED
            batch.failure_reason = str(exc)
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = str(exc)
            if not options.dry_run:
                self._session.flush()
            return

        report.rows_received = len(validations)

        if batch.declared_row_count != len(validations):
            # A truncated upload is the commonest way a batch goes wrong, and a
            # silently short import looks exactly like a quiet week.
            self._record_issue(
                batch,
                None,
                Issue(
                    code="row_count_mismatch",
                    severity=ValidationSeverity.FATAL,
                    message=(
                        "the envelope declares a different number of rows than the "
                        "file contains; the artefact may be truncated"
                    ),
                    context={
                        "declared": batch.declared_row_count,
                        "read": len(validations),
                    },
                ),
                report,
            )
            batch.import_status = ImportBatchStatus.FAILED
            batch.failure_reason = (
                f"declared {batch.declared_row_count} rows, read {len(validations)}"
            )
            report.status = ImportBatchStatus.FAILED
            report.failure_reason = batch.failure_reason
            if not options.dry_run:
                self._session.flush()
            return

        rejected = sum(1 for validation in validations if not validation.is_loadable)
        self._record_stage(batch, ImportStage.VALIDATE, started, len(validations), rejected)

        if options.validate_only or options.dry_run:
            # Only the rejected rows are recorded. A row that merely *would*
            # load has no truthful outcome to store yet - claiming "loaded"
            # before anything was written is the kind of counter that makes an
            # import report worthless.
            for validation in validations:
                if not validation.is_loadable:
                    self._record_row(batch, validation, None, report)
            report.status = (
                ImportBatchStatus.QUARANTINED
                if validations and rejected == len(validations)
                else ImportBatchStatus.VALIDATING
            )
            batch.import_status = report.status
            _apply_counters(batch, report)
            if self._persist:
                self._session.flush()
            return

        batch.import_status = ImportBatchStatus.LOADING
        loading_started = datetime.now(UTC)

        for validation in validations:
            self._load_row(batch, validation, options, report)

        self._record_stage(
            batch,
            ImportStage.WRITE_CANONICAL,
            loading_started,
            len(validations),
            report.rows_quarantined,
        )

        if report.rows_quarantined == 0:
            batch.import_status = ImportBatchStatus.COMPLETED
        elif report.rows_loaded or report.rows_updated or report.rows_unchanged:
            batch.import_status = ImportBatchStatus.PARTIALLY_COMPLETED
        else:
            batch.import_status = ImportBatchStatus.QUARANTINED

        batch.completed_at = datetime.now(UTC)
        report.status = batch.import_status
        _apply_counters(batch, report)
        self._session.flush()

        logger.info(
            "ingest_batch_finished",
            batch_id=str(batch.id),
            status=batch.import_status.value,
            **{key: value for key, value in report.as_dict().items() if isinstance(value, int)},
        )

    def _read_and_validate(self, artefact: Path, report: IngestReport) -> Iterator[RowValidation]:
        seen: set[str] = set()
        for row in self._adapter.rows(artefact):
            if row.source_row_id in seen:
                # Within one artefact a duplicate id is a producer error: the
                # two rows would be indistinguishable to every later replay.
                yield RowValidation(
                    row=row,
                    encounter=None,
                    issues=[
                        Issue(
                            code="duplicate_source_row_id",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                "source_row_id appears more than once in this "
                                "artefact; it must be unique within a batch"
                            ),
                            field_path="source_row_id",
                        )
                    ],
                )
                continue
            seen.add(row.source_row_id)
            yield self._validator.validate(row)

    def _load_row(
        self,
        batch: ImportBatch,
        validation: RowValidation,
        options: IngestOptions,
        report: IngestReport,
    ) -> None:
        if not validation.is_loadable:
            self._record_row(batch, validation, None, report)
            return

        encounter_data = validation.encounter
        assert encounter_data is not None

        existing = self._existing_encounter(batch, encounter_data.source_row_id)

        if existing is not None:
            # Already loaded. Whether this is a replay or a revision is decided
            # by the row's checksum, not by comparing every field.
            checksum = _row_checksum(validation.row)
            recorded = (
                self._session.execute(
                    select(ImportSourceRow).where(ImportSourceRow.opd_encounter_id == existing.id)
                )
                .scalars()
                .first()
            )

            if recorded is not None and recorded.payload_checksum == checksum:
                report.rows_unchanged += 1
                self._record_row(
                    batch, validation, existing, report, outcome=SourceRowOutcome.UNCHANGED
                )
                return

            self._apply(existing, encounter_data, batch)
            report.rows_updated += 1
            self._record_row(batch, validation, existing, report, outcome=SourceRowOutcome.UPDATED)
            return

        # -- Identity stage, on its own connection and its own role ---------
        patient_reference_id, confidence = self._identity.link(validation.row.identity)
        if patient_reference_id is not None:
            report.rows_linked += 1
        else:
            report.rows_unlinked += 1

        encounter = OpdEncounter(
            facility_id=batch.facility_id,
            source_system=batch.source_system,
            source_row_reference=encounter_data.source_row_id,
            source_batch_id=batch.id,
            ingest_method_version=INGEST_METHOD_VERSION,
            patient_reference_id=self._ensure_reference(patient_reference_id, confidence),
        )
        self._apply(encounter, encounter_data, batch)

        savepoint = self._session.begin_nested()
        try:
            self._session.add(encounter)
            self._session.flush()
            savepoint.commit()
        except IntegrityError:
            # Another worker wrote this row between the lookup and the insert.
            # The constraint decided; re-read and count it as unchanged.
            savepoint.rollback()
            raced = self._existing_encounter(batch, encounter_data.source_row_id)
            if raced is None:
                raise
            report.rows_unchanged += 1
            self._record_row(batch, validation, raced, report, outcome=SourceRowOutcome.UNCHANGED)
            return

        report.rows_loaded += 1
        self._record_row(batch, validation, encounter, report, outcome=SourceRowOutcome.LOADED)

    def _existing_encounter(self, batch: ImportBatch, source_row_id: str) -> OpdEncounter | None:
        """The encounter this source row already produced, if any.

        A single method so both the pre-insert lookup and the post-conflict
        recovery read go through one place. They must agree: a recovery read
        that could disagree with the lookup would turn an absorbed conflict
        back into a crash.
        """
        return self._session.execute(
            select(OpdEncounter).where(
                OpdEncounter.source_system == batch.source_system,
                OpdEncounter.source_row_reference == source_row_id,
            )
        ).scalar_one_or_none()

    def _ensure_reference(
        self, patient_reference_id: uuid.UUID | None, confidence: LinkageConfidence
    ) -> uuid.UUID | None:
        """Make sure ``mars_core`` has the pseudonymous row the vault points at.

        The vault's role cannot write to ``mars_core``, so the reference row is
        created here. That is the boundary working, not a workaround.
        """
        if patient_reference_id is None:
            return None

        existing = self._session.get(PatientReference, patient_reference_id)
        if existing is None:
            self._session.add(PatientReference(id=patient_reference_id, linkage_token_id=None))
            self._session.flush()
        return patient_reference_id

    def _apply(self, encounter: OpdEncounter, data: ValidatedEncounter, batch: ImportBatch) -> None:
        """Write the validated values onto an encounter, new or existing."""
        encounter.encounter_date = data.encounter_date
        encounter.date_assignment_method = data.date_assignment_method
        encounter.serial_number = data.serial_number
        encounter.sex = data.sex
        encounter.patient_category = data.patient_category
        encounter.attendance_type = data.attendance_type
        encounter.fever_present = data.fever_present
        encounter.age_value = data.age_value
        encounter.age_unit = data.age_unit
        encounter.age_days_approx = data.age_days_approx
        encounter.presenting_complaint_raw = data.presenting_complaint
        encounter.notifiable_marked = data.notifiable_marked
        encounter.source_batch_id = batch.id

        self._apply_residence(encounter, data)

        # A revision replaces the child rows rather than merging into them: the
        # source has no stable identity for an individual diagnosis or drug
        # line, so there is nothing to match an old row against.
        #
        # Cleared and flushed *before* the new rows are added. Both collections
        # are keyed by ``(encounter_id, sequence)``, and SQLAlchemy orders
        # inserts before deletes within one flush - so replacing sequence 1
        # would collide with the sequence 1 that is about to be deleted.
        if inspect(encounter).persistent:
            encounter.diagnoses.clear()
            encounter.prescriptions.clear()
            encounter.tests.clear()
            encounter.referrals.clear()
            self._session.flush()

        encounter.diagnoses = [
            OpdEncounterDiagnosis(
                sequence=index,
                diagnosis_raw=text,
                diagnosis_normalised=" ".join(text.lower().split()),
            )
            for index, text in enumerate(data.diagnoses, start=1)
        ]
        encounter.prescriptions = [
            OpdEncounterPrescription(
                sequence=index,
                prescription_raw=entry["prescription_raw"],
                drug_name_raw=entry.get("drug_name_raw"),
                drug_name_normalised=(
                    " ".join(entry["drug_name_raw"].lower().split())
                    if entry.get("drug_name_raw")
                    else None
                ),
                units_per_dose=entry.get("units_per_dose"),
                doses_per_day=entry.get("doses_per_day"),
                days=entry.get("days"),
                total_units=entry.get("total_units"),
                is_device=bool(entry.get("is_device", False)),
            )
            for index, entry in enumerate(data.prescriptions, start=1)
        ]
        encounter.tests = [
            OpdEncounterTest(sequence=index, method=method, result=result)
            for index, (method, result) in enumerate(data.tests, start=1)
        ]
        encounter.referrals = [
            OpdEncounterReferral(direction=direction, referral_number=number)
            for direction, number in data.referrals
        ]

    def _apply_residence(self, encounter: OpdEncounter, data: ValidatedEncounter) -> None:
        """Resolve residence by code or exact normalised name only.

        Never fuzzy. A near-match would attribute a case to the wrong district,
        and a wrong district is worse than an unresolved one - an unresolved
        residence is visible, a wrong one is not.
        """
        encounter.residence_parish_raw = data.residence_parish_raw
        encounter.residence_village_raw = data.residence_village_raw
        encounter.residence_district_id = None
        encounter.residence_subcounty_id = None

        unresolved: list[str] = []
        for level, raw, attribute in (
            (GeographyLevel.DISTRICT, data.residence_district_raw, "residence_district_id"),
            (
                GeographyLevel.SUBCOUNTY,
                data.residence_subcounty_raw,
                "residence_subcounty_id",
            ),
        ):
            if not raw:
                continue
            unit = (
                self._session.execute(
                    select(GeographyUnit).where(
                        GeographyUnit.level == level,
                        GeographyUnit.normalised_name == " ".join(raw.lower().split()),
                        GeographyUnit.is_active.is_(True),
                    )
                )
                .scalars()
                .first()
            )
            if unit is None:
                unresolved.append(f"{level.value}={raw}")
            else:
                setattr(encounter, attribute, unit.id)

        encounter.residence_unresolved_raw = "; ".join(unresolved)[:320] or None

    # -- Recording ---------------------------------------------------------
    def _record_row(
        self,
        batch: ImportBatch,
        validation: RowValidation,
        encounter: OpdEncounter | None,
        report: IngestReport,
        *,
        outcome: SourceRowOutcome | None = None,
    ) -> None:
        resolved = outcome or SourceRowOutcome.QUARANTINED
        if resolved is SourceRowOutcome.QUARANTINED:
            report.rows_quarantined += 1
        if encounter is not None and encounter.residence_unresolved_raw:
            report.unresolved_geography += 1

        if not self._persist:
            # A dry run still counts and still reports issues; it just does not
            # write them. Issues are attributed to the batch rather than a row,
            # because in a dry run no stored row exists to attribute them to.
            for issue in validation.issues:
                self._count_issue(issue, report)
            return

        existing = self._session.execute(
            select(ImportSourceRow).where(
                ImportSourceRow.import_batch_id == batch.id,
                ImportSourceRow.source_row_reference == validation.row.source_row_id,
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = ImportSourceRow(
                import_batch_id=batch.id,
                source_row_reference=validation.row.source_row_id,
                source_line_number=validation.row.line_number,
                outcome=resolved,
                # Identity removed, not masked. See ImportSourceRow's docstring.
                payload_redacted=validation.row.redacted,
                payload_checksum=_row_checksum(validation.row),
            )
            self._session.add(existing)
        else:
            existing.outcome = resolved
            existing.payload_redacted = validation.row.redacted
            existing.payload_checksum = _row_checksum(validation.row)

        existing.opd_encounter_id = encounter.id if encounter is not None else None
        self._session.flush()

        for issue in validation.issues:
            self._record_issue(batch, existing, issue, report)

    def _count_issue(self, issue: Issue, report: IngestReport) -> None:
        report.issue_codes[issue.code] = report.issue_codes.get(issue.code, 0) + 1
        if issue.severity is ValidationSeverity.WARNING:
            report.warning_count += 1
        else:
            report.error_count += 1

    def _record_issue(
        self,
        batch: ImportBatch,
        row: ImportSourceRow | None,
        issue: Issue,
        report: IngestReport,
    ) -> None:
        self._count_issue(issue, report)
        if not self._persist:
            return

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

        existing = self._session.execute(
            select(ImportStageExecution).where(
                ImportStageExecution.import_batch_id == batch.id,
                ImportStageExecution.stage == stage,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = ImportStageExecution(
                import_batch_id=batch.id, stage=stage, started_at=started
            )
            self._session.add(existing)
        existing.finished_at = datetime.now(UTC)
        existing.rows_in = rows_in
        existing.rows_out = rows_in - rejected
        existing.rows_rejected = rejected
        self._session.flush()


def _checksum(artefact: Path) -> tuple[str, int]:
    """SHA-256 of the artefact, read in chunks.

    The batch's identity is its content, not its filename: two uploads of the
    same bytes under different names are one batch.
    """
    digest = hashlib.sha256()
    size = 0
    with artefact.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _row_checksum(row: InboundRow) -> str:
    """Checksum of a row **without its identity block**.

    Identity is not part of what makes an encounter unchanged, and hashing it
    would mean the checksum could not be computed outside the identity
    boundary.
    """
    return hashlib.sha256(
        json.dumps(row.redacted, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _apply_counters(batch: ImportBatch, report: IngestReport) -> None:
    batch.rows_received = report.rows_received
    batch.rows_loaded = report.rows_loaded
    batch.rows_updated = report.rows_updated
    batch.rows_unchanged = report.rows_unchanged
    batch.rows_quarantined = report.rows_quarantined
    batch.rows_linked = report.rows_linked
    batch.rows_unlinked = report.rows_unlinked
    batch.unresolved_geography = report.unresolved_geography
    batch.warning_count = report.warning_count
    batch.error_count = report.error_count


def _copy_counters(batch: ImportBatch, report: IngestReport) -> None:
    report.rows_received = batch.rows_received
    report.rows_loaded = batch.rows_loaded
    report.rows_updated = batch.rows_updated
    report.rows_unchanged = batch.rows_unchanged
    report.rows_quarantined = batch.rows_quarantined
    report.rows_linked = batch.rows_linked
    report.rows_unlinked = batch.rows_unlinked
    report.unresolved_geography = batch.unresolved_geography
    report.warning_count = batch.warning_count
    report.error_count = batch.error_count


__all__ = [
    "EncounterIngestionPipeline",
    "IdentityLinker",
    "IngestOptions",
    "IngestReport",
    "NullIdentityLinker",
    "VaultIdentityLinker",
]
