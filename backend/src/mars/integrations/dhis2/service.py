"""Orchestrating a DHIS2 exchange: run bookkeeping, paging, and where data goes.

The adapter knows how to talk to DHIS2. This module knows what MARS does with
what comes back, and the two are separate because the second is where the rules
live.

**Aggregate values do not get their own model.** They are translated into the
Prompt 11 inbound submission contract and loaded by the Prompt 11 pipeline, so
DHIS2 content meets exactly the same validation, the same revision rules and the
same blank-versus-zero handling as a transcribed paper form. A parallel path
would drift, and the first sign would be two different numbers for one month.

**A run is resumable because it is recorded.** Pages fetched, cursor, counts and
a payload checksum are written as the run progresses, so an interrupted pull
continues rather than restarting - and so a re-pull of identical bytes is
recognisably the same exchange rather than a second import.

**Metadata sync proposes; it does not decide.** An organisation unit MARS
cannot place becomes a mapping proposal. Nothing is matched on name similarity,
and nothing is promoted automatically.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.enums import (
    IntegrationErrorCategory,
    IntegrationResource,
    IntegrationRunStatus,
)
from mars.domain.integration import IntegrationRun
from mars.integrations.dhis2.client import (
    ADAPTER_VERSION,
    SYSTEM_NAME,
    Dhis2Error,
)
from mars.integrations.dhis2.mapping import (
    REMOTE_TYPE_ORGANISATION_UNIT,
    Dhis2Crosswalk,
)
from mars.integrations.ports import RemoteScope

logger = get_logger(__name__)

#: Cap on pages per run. A remote system that keeps offering a next cursor
#: would otherwise pull until the process dies, and a run that never finishes
#: looks exactly like one that hung.
MAX_PAGES_PER_RUN = 5_000


@dataclass(frozen=True, slots=True)
class SyncOptions:
    """How to run one exchange."""

    #: Read and report, write nothing - not the run row, not the proposals.
    dry_run: bool = False
    #: Fetch and validate, record findings and proposals, load no canonical data.
    validate_only: bool = False
    #: Continue an unfinished run from its recorded cursor.
    resume: bool = False
    #: Re-run a completed exchange. Without it, a terminal run for the same
    #: scope is reported rather than repeated.
    force: bool = False
    initiated_by: str | None = None
    max_pages: int = MAX_PAGES_PER_RUN


@dataclass(slots=True)
class SyncReport:
    """What an exchange did, in numbers an operator can act on."""

    run_id: uuid.UUID | None = None
    resource: IntegrationResource | None = None
    status: IntegrationRunStatus = IntegrationRunStatus.PENDING
    pages_fetched: int = 0
    records_received: int = 0
    records_accepted: int = 0
    records_rejected: int = 0
    records_unchanged: int = 0
    mappings_unresolved: int = 0
    payload_checksum: str | None = None
    import_batch_id: uuid.UUID | None = None
    error_category: IntegrationErrorCategory | None = None
    error_summary: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {IntegrationRunStatus.COMPLETED, IntegrationRunStatus.PARTIAL}

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id) if self.run_id else None,
            "resource": self.resource.value if self.resource else None,
            "status": self.status.value,
            "pages_fetched": self.pages_fetched,
            "records_received": self.records_received,
            "records_accepted": self.records_accepted,
            "records_rejected": self.records_rejected,
            "records_unchanged": self.records_unchanged,
            "mappings_unresolved": self.mappings_unresolved,
            "payload_checksum": self.payload_checksum,
            "import_batch_id": str(self.import_batch_id) if self.import_batch_id else None,
            "error_category": self.error_category.value if self.error_category else None,
            "error_summary": self.error_summary,
        }


class IntegrationNotConfiguredError(RuntimeError):
    """The exchange was requested but the deployment has no DHIS2 configured.

    Raised rather than returning an empty result: "no DHIS2 configured" and "no
    data in DHIS2" are opposite facts, and a caller that cannot tell them apart
    will report a quiet month.
    """


class Dhis2SyncService:
    """Runs one exchange and records what happened."""

    def __init__(
        self,
        session: Session,
        client: Any,
        *,
        crosswalk: Dhis2Crosswalk | None = None,
        system: str = SYSTEM_NAME,
    ) -> None:
        self._session = session
        self._client = client
        self._crosswalk = crosswalk or Dhis2Crosswalk(session, system=system)
        self._system = system

    # -- Metadata ----------------------------------------------------------
    def sync_organisation_units(self, options: SyncOptions | None = None) -> SyncReport:
        """Read the remote organisation hierarchy and reconcile it.

        Reconcile, not import. MARS's geography comes from the authoritative
        boundary files; DHIS2 supplies identifiers for units MARS already
        holds. A UID with no accepted mapping becomes a proposal.
        """
        options = options or SyncOptions()
        scope = RemoteScope()
        report = SyncReport(resource=IntegrationResource.ORGANISATION_UNIT_METADATA)

        run = self._open_run(IntegrationResource.ORGANISATION_UNIT_METADATA, scope, options, report)
        if run is None:
            return report

        digest = hashlib.sha256()
        cursor = run.cursor if options.resume else None

        try:
            for _ in range(options.max_pages):
                page = self._client.fetch_organisation_units(cursor)
                report.pages_fetched += 1
                report.records_received += len(page.records)

                for unit in page.records:
                    digest.update(
                        f"{unit.remote_id}|{unit.name}|{unit.level}|{unit.parent_remote_id}\n".encode()
                    )
                    resolved = self._crosswalk.resolve_organisation_unit(unit.remote_id)
                    if resolved.is_resolved:
                        report.records_accepted += 1
                        continue

                    report.mappings_unresolved += 1
                    if self._persisting(options):
                        self._crosswalk.record_unresolved(
                            remote_type=REMOTE_TYPE_ORGANISATION_UNIT,
                            remote_id=unit.remote_id,
                            remote_name=unit.name,
                            remote_parent_id=unit.parent_remote_id,
                            run_id=run.id,
                            detail={"level": unit.level, "code": unit.code},
                        )

                cursor = page.next_cursor
                if self._persisting(options):
                    run.cursor = cursor
                    run.pages_fetched = report.pages_fetched
                    run.records_received = report.records_received
                    run.mappings_unresolved = report.mappings_unresolved
                    self._session.flush()
                if page.is_last:
                    break
            else:
                raise Dhis2Error(
                    IntegrationErrorCategory.MALFORMED_RESPONSE,
                    f"pagination did not terminate within {options.max_pages} pages",
                )
        except Dhis2Error as error:
            return self._fail(run, report, error, options)

        report.payload_checksum = digest.hexdigest()
        return self._finish(run, report, options)

    # -- Aggregate data ----------------------------------------------------
    def pull_aggregate_values(
        self,
        scope: RemoteScope,
        options: SyncOptions | None = None,
    ) -> tuple[SyncReport, list[dict[str, Any]]]:
        """Fetch reported values and return them in MARS's inbound shape.

        Returns the translated submissions rather than loading them here: the
        canonical Prompt 11 pipeline owns loading, and this service must not
        acquire a second way to write aggregate figures.
        """
        options = options or SyncOptions()
        report = SyncReport(resource=IntegrationResource.AGGREGATE_DATA_VALUES)

        run = self._open_run(IntegrationResource.AGGREGATE_DATA_VALUES, scope, options, report)
        if run is None:
            return report, []

        digest = hashlib.sha256()
        cursor = run.cursor if options.resume else None
        collected: list[Any] = []

        try:
            for _ in range(options.max_pages):
                page = self._client.fetch_data_values(scope, cursor)
                report.pages_fetched += 1
                report.records_received += len(page.records)

                for value in page.records:
                    digest.update(
                        (
                            f"{value.data_element_remote_id}|"
                            f"{value.organisation_unit_remote_id}|{value.period}|"
                            f"{value.category_option_combo_remote_id}|{value.value}\n"
                        ).encode()
                    )
                    collected.append(value)

                cursor = page.next_cursor
                if self._persisting(options):
                    run.cursor = cursor
                    run.pages_fetched = report.pages_fetched
                    run.records_received = report.records_received
                    self._session.flush()
                if page.is_last:
                    break
            else:
                raise Dhis2Error(
                    IntegrationErrorCategory.MALFORMED_RESPONSE,
                    f"pagination did not terminate within {options.max_pages} pages",
                )
        except Dhis2Error as error:
            return self._fail(run, report, error, options), []

        report.payload_checksum = digest.hexdigest()
        self._finish(run, report, options)
        return report, self._describe_values(collected, report, run, options)

    def _describe_values(
        self,
        values: list[Any],
        report: SyncReport,
        run: IntegrationRun,
        options: SyncOptions,
    ) -> list[dict[str, Any]]:
        """Group values by resolved facility and period.

        Values whose organisation unit does not resolve are **dropped from the
        result and counted**, not attached to a nearby facility. An unresolved
        UID is a configuration gap; loading its figures somewhere plausible is
        how a district acquires attendance it never had.
        """
        grouped: dict[tuple[str, str], list[Any]] = {}
        for value in values:
            resolved = self._crosswalk.resolve_organisation_unit(value.organisation_unit_remote_id)
            if resolved.facility_id is None:
                report.mappings_unresolved += 1
                report.records_rejected += 1
                if self._persisting(options):
                    self._crosswalk.record_unresolved(
                        remote_type=REMOTE_TYPE_ORGANISATION_UNIT,
                        remote_id=value.organisation_unit_remote_id,
                        run_id=run.id,
                        detail={"seen_in": "data_values", "period": value.period},
                    )
                continue
            grouped.setdefault((str(resolved.facility_id), value.period), []).append(value)

        report.records_accepted = sum(len(rows) for rows in grouped.values())
        if self._persisting(options):
            run.records_accepted = report.records_accepted
            run.records_rejected = report.records_rejected
            run.mappings_unresolved = report.mappings_unresolved
            self._session.flush()

        return [
            {
                "facility_id": facility_id,
                "period": period,
                "values": [
                    {
                        "data_element": row.data_element_remote_id,
                        "category_option_combo": row.category_option_combo_remote_id,
                        "value": row.value,
                    }
                    for row in rows
                ],
            }
            for (facility_id, period), rows in sorted(grouped.items())
        ]

    # -- Run bookkeeping ---------------------------------------------------
    def _open_run(
        self,
        resource: IntegrationResource,
        scope: RemoteScope,
        options: SyncOptions,
        report: SyncReport,
    ) -> IntegrationRun | None:
        fingerprint = scope_fingerprint(resource, scope)
        existing = (
            self._session.execute(
                select(IntegrationRun)
                .where(
                    IntegrationRun.system == self._system,
                    IntegrationRun.resource == resource,
                    IntegrationRun.scope_fingerprint == fingerprint,
                )
                .order_by(IntegrationRun.attempt.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        # A partial run is terminal for *reporting* - the exchange is over - but
        # resumable for *continuation*: it has a cursor and the pages before it
        # are good. Conflating the two makes --resume silently start again,
        # which is the one thing it exists not to do.
        resumable = (
            existing is not None
            and existing.cursor is not None
            and existing.run_status in {IntegrationRunStatus.RUNNING, IntegrationRunStatus.PARTIAL}
        )

        if existing is not None and options.resume and resumable:
            existing.run_status = IntegrationRunStatus.RUNNING
            report.run_id = existing.id
            report.pages_fetched = existing.pages_fetched
            report.records_received = existing.records_received
            report.records_accepted = existing.records_accepted
            report.mappings_unresolved = existing.mappings_unresolved
            return existing

        if existing is not None and existing.is_terminal and not (options.force or options.resume):
            # The same scope has already been exchanged. Reporting the previous
            # outcome is what makes a scheduled daily pull safe to re-run.
            report.run_id = existing.id
            report.status = existing.run_status
            report.pages_fetched = existing.pages_fetched
            report.records_received = existing.records_received
            report.records_accepted = existing.records_accepted
            report.records_rejected = existing.records_rejected
            report.records_unchanged = existing.records_received
            report.mappings_unresolved = existing.mappings_unresolved
            report.payload_checksum = existing.payload_checksum
            report.import_batch_id = existing.import_batch_id
            return None

        attempt = (existing.attempt + 1) if existing is not None else 1
        run = IntegrationRun(
            system=self._system,
            resource=resource,
            scope_fingerprint=fingerprint,
            scope_description=describe_scope(scope),
            period_start=scope.period_start,
            period_end=scope.period_end,
            attempt=attempt,
            run_status=IntegrationRunStatus.RUNNING,
            started_at=datetime.now(UTC),
            adapter_version=ADAPTER_VERSION,
            correlation_id=uuid.uuid4().hex,
            initiated_by=options.initiated_by,
        )
        if self._persisting(options):
            self._session.add(run)
            self._session.flush()
        report.run_id = run.id
        return run

    def _finish(self, run: IntegrationRun, report: SyncReport, options: SyncOptions) -> SyncReport:
        status = (
            IntegrationRunStatus.PARTIAL
            if report.mappings_unresolved
            else IntegrationRunStatus.COMPLETED
        )
        run.run_status = status
        run.finished_at = datetime.now(UTC)
        run.pages_fetched = report.pages_fetched
        run.records_received = report.records_received
        run.records_accepted = report.records_accepted
        run.records_rejected = report.records_rejected
        run.mappings_unresolved = report.mappings_unresolved
        run.payload_checksum = report.payload_checksum
        report.status = status

        if self._persisting(options):
            self._session.flush()

        logger.info(
            "dhis2_run_finished",
            system=self._system,
            resource=run.resource.value,
            correlation_id=run.correlation_id,
            **{k: v for k, v in report.as_dict().items() if isinstance(v, int)},
        )
        return report

    def _fail(
        self,
        run: IntegrationRun,
        report: SyncReport,
        error: Dhis2Error,
        options: SyncOptions,
    ) -> SyncReport:
        """Record the failure without recording anything the remote said.

        ``str(error)`` is MARS's own sentence; the remote body never reaches
        here, because a DHIS2 error can quote the request that caused it and
        that request carries an Authorization header.
        """
        # A run that read pages before failing keeps them: resuming from page
        # twelve is cheaper and more honest than discarding eleven good pages.
        status = (
            IntegrationRunStatus.PARTIAL if report.pages_fetched else IntegrationRunStatus.FAILED
        )
        run.run_status = status
        run.finished_at = datetime.now(UTC)
        run.pages_fetched = report.pages_fetched
        run.records_received = report.records_received
        run.mappings_unresolved = report.mappings_unresolved
        run.error_category = error.category.value
        run.error_summary = str(error)

        report.status = status
        report.error_category = error.category
        report.error_summary = str(error)

        if self._persisting(options):
            self._session.flush()

        logger.warning(
            "dhis2_run_failed",
            system=self._system,
            resource=run.resource.value,
            correlation_id=run.correlation_id,
            category=error.category.value,
            pages_fetched=report.pages_fetched,
        )
        return report

    @staticmethod
    def _persisting(options: SyncOptions) -> bool:
        return not options.dry_run


def scope_fingerprint(resource: IntegrationResource, scope: RemoteScope) -> str:
    """A stable identity for one request.

    Sorted, so the same org units in a different order are the same scope. This
    is what makes a scheduled pull idempotent instead of creating a new run
    every time the caller happens to build its list differently.
    """
    material = {
        "resource": resource.value,
        "organisation_units": sorted(scope.organisation_unit_remote_ids),
        "datasets": sorted(scope.dataset_remote_ids),
        "data_elements": sorted(scope.data_element_remote_ids),
        "period_start": scope.period_start.isoformat() if scope.period_start else None,
        "period_end": scope.period_end.isoformat() if scope.period_end else None,
        "include_descendants": scope.include_descendants,
        "extra": dict(sorted(scope.extra.items())),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def describe_scope(scope: RemoteScope) -> str:
    """A short human-readable scope. Never carries a credential."""
    parts: list[str] = []
    if scope.period_start and scope.period_end:
        parts.append(f"{scope.period_start.isoformat()}..{scope.period_end.isoformat()}")
    if scope.organisation_unit_remote_ids:
        parts.append(f"{len(scope.organisation_unit_remote_ids)} org unit(s)")
    if scope.dataset_remote_ids:
        parts.append(f"{len(scope.dataset_remote_ids)} dataset(s)")
    if scope.include_descendants:
        parts.append("including descendants")
    return "; ".join(parts) or "whole resource"


def parse_period(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


__all__ = [
    "MAX_PAGES_PER_RUN",
    "Dhis2SyncService",
    "IntegrationNotConfiguredError",
    "SyncOptions",
    "SyncReport",
    "describe_scope",
    "parse_period",
    "scope_fingerprint",
]
