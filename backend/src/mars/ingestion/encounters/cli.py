"""Command-line entry point for e-register encounter ingestion.

    mars-import-encounters load     --file batch.jsonl
    mars-import-encounters validate --file batch.jsonl      # writes issues, no encounters
    mars-import-encounters dry-run  --file batch.jsonl      # writes nothing at all
    mars-import-encounters resume   --file batch.jsonl      # finish an interrupted batch
    mars-import-encounters status   --batch <uuid>          # what happened, and why

Exit codes are meant to be branched on by a scheduler, so they distinguish
"nothing loaded" from "some rows need a producer's attention":

    0  every row loaded (or was already loaded)
    1  loaded with quarantined rows - a producer has work to do
    2  usage error: bad arguments, missing file, unknown batch
    3  the batch failed as a whole: unknown schema version, unresolved
       facility, truncated artefact, unreadable file
    4  the identity component was required and is not available

3 and 4 are separate because they need different people: 3 is the producer's
problem, 4 is the operator's.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import get_settings
from mars.db.session import session_scope
from mars.domain.enums import ImportBatchStatus
from mars.domain.ingestion import ImportBatch, ImportValidationIssue
from mars.identity.provisioning import (
    IdentityNotConfiguredError,
    build_encryptor,
    build_linkage_deriver,
    get_identity_session_factory,
)
from mars.identity.service import IdentityService
from mars.ingestion.encounters.pipeline import (
    EncounterIngestionPipeline,
    IdentityLinker,
    IngestOptions,
    IngestReport,
    NullIdentityLinker,
    VaultIdentityLinker,
)

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_QUARANTINED = 1
EXIT_USAGE = 2
EXIT_BATCH_FAILED = 3
EXIT_IDENTITY_UNAVAILABLE = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mars-import-encounters",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=("load", "validate", "dry-run", "resume", "status"),
        help="What to do",
    )
    parser.add_argument("--file", type=Path, help="The artefact to ingest")
    parser.add_argument("--batch", help="Batch id, for the status command")
    parser.add_argument(
        "--initiated-by",
        default="mars-import-encounters",
        help="Service label recorded on the batch. Never a personal name.",
    )
    parser.add_argument(
        "--no-identity",
        action="store_true",
        help=(
            "Load without linkage. Every encounter is recorded unlinked, which "
            "is honest but makes re-attendance invisible."
        ),
    )
    parser.add_argument("--json", type=Path, help="Write the full report document here")
    return parser


def _build_linker(no_identity: bool) -> tuple[IdentityLinker | None, Session | None]:
    """The identity linker, and the session it holds open.

    Returns ``(None, None)`` when identity is configured but unusable, so the
    caller can exit 4 rather than loading a whole month unlinked by accident.
    A deployment that genuinely wants unlinked loading says ``--no-identity``.
    """
    if no_identity:
        return NullIdentityLinker(), None

    settings = get_settings()
    try:
        identity_session = get_identity_session_factory()()
    except IdentityNotConfiguredError:
        return None, None

    service = IdentityService(
        identity_session,
        build_linkage_deriver(settings),
        build_encryptor(settings),
    )
    if not service.is_ready:
        identity_session.close()
        return None, None

    return VaultIdentityLinker(service, uuid.uuid4), identity_session


def _run_ingest(args: argparse.Namespace, options: IngestOptions) -> int:
    artefact: Path | None = args.file
    if artefact is None:
        print("ERROR: --file is required", file=sys.stderr)
        return EXIT_USAGE
    if not artefact.is_file():
        print(f"ERROR: {artefact} is not a file", file=sys.stderr)
        return EXIT_USAGE

    linker, identity_session = _build_linker(args.no_identity)
    if linker is None:
        print(
            "ERROR: the identity component is not configured or not ready. "
            "Loading would silently record every encounter as a new person. "
            "Configure the identity keys, or pass --no-identity deliberately.",
            file=sys.stderr,
        )
        return EXIT_IDENTITY_UNAVAILABLE

    try:
        with session_scope() as session:
            pipeline = EncounterIngestionPipeline(session, identity_linker=linker)
            report = pipeline.run(artefact, options)
            if identity_session is not None:
                # The identity writes are on their own connection and their own
                # role, so they commit separately. Committed first: a linkage
                # without its encounter is recoverable on replay, an encounter
                # pointing at a reference the vault never recorded is not.
                identity_session.commit()
    finally:
        if identity_session is not None:
            identity_session.close()

    _print_report(report, options)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    if report.status is ImportBatchStatus.FAILED:
        return EXIT_BATCH_FAILED
    if report.rows_quarantined:
        return EXIT_QUARANTINED
    return EXIT_OK


def _print_report(report: IngestReport, options: IngestOptions) -> None:
    mode = "DRY RUN" if options.dry_run else "VALIDATE" if options.validate_only else "LOAD"
    print(f"{mode}  batch={report.batch_id}  status={report.status.value}")
    print()
    print(f"  received       {report.rows_received:6d}")
    if not options.dry_run and not options.validate_only:
        print(f"  loaded         {report.rows_loaded:6d}")
        print(f"  updated        {report.rows_updated:6d}")
        print(f"  unchanged      {report.rows_unchanged:6d}")
        print(f"  linked         {report.rows_linked:6d}")
        print(f"  unlinked       {report.rows_unlinked:6d}")
    print(f"  quarantined    {report.rows_quarantined:6d}")
    print(f"  unresolved geo {report.unresolved_geography:6d}")
    print(f"  warnings       {report.warning_count:6d}")
    print(f"  errors         {report.error_count:6d}")

    if report.issue_codes:
        print()
        print("  issues by code:")
        for code, count in sorted(report.issue_codes.items(), key=lambda item: -item[1]):
            print(f"    {code}: {count}")

    if report.failure_reason:
        print(file=sys.stderr)
        print(f"  FAILED: {report.failure_reason}", file=sys.stderr)


def _status(batch_id_text: str | None) -> int:
    if not batch_id_text:
        print("ERROR: --batch is required for status", file=sys.stderr)
        return EXIT_USAGE
    try:
        batch_id = uuid.UUID(batch_id_text)
    except ValueError:
        print(f"ERROR: {batch_id_text!r} is not a batch id", file=sys.stderr)
        return EXIT_USAGE

    with session_scope() as session:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            print(f"ERROR: no batch {batch_id}", file=sys.stderr)
            return EXIT_USAGE

        print(f"batch    {batch.id}")
        print(f"source   {batch.source_system} schema {batch.schema_version}")
        print(f"artefact {batch.artefact_name} ({batch.artefact_checksum[:16]}…)")
        print(f"status   {batch.import_status.value}")
        print(f"received {batch.received_at.isoformat()}")
        if batch.completed_at:
            print(f"finished {batch.completed_at.isoformat()}")
        print()
        for label, value in (
            ("received", batch.rows_received),
            ("loaded", batch.rows_loaded),
            ("updated", batch.rows_updated),
            ("unchanged", batch.rows_unchanged),
            ("quarantined", batch.rows_quarantined),
            ("linked", batch.rows_linked),
            ("unlinked", batch.rows_unlinked),
            ("unresolved geo", batch.unresolved_geography),
            ("warnings", batch.warning_count),
            ("errors", batch.error_count),
        ):
            print(f"  {label:15s} {value:6d}")

        codes = session.execute(
            select(ImportValidationIssue.code, ImportValidationIssue.severity).where(
                ImportValidationIssue.import_batch_id == batch.id
            )
        ).all()
        if codes:
            print()
            print("  issues by code:")
            counted: dict[str, int] = {}
            for code, severity in codes:
                counted[f"{code} ({severity.value})"] = (
                    counted.get(f"{code} ({severity.value})", 0) + 1
                )
            for key, count in sorted(counted.items(), key=lambda item: -item[1]):
                print(f"    {key}: {count}")

        if batch.failure_reason:
            print()
            print(f"  FAILED: {batch.failure_reason}", file=sys.stderr)

        if batch.import_status is ImportBatchStatus.FAILED:
            return EXIT_BATCH_FAILED
        if batch.rows_quarantined:
            return EXIT_QUARANTINED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())

    if args.command == "status":
        return _status(args.batch)

    options = IngestOptions(
        dry_run=args.command == "dry-run",
        validate_only=args.command == "validate",
        resume=args.command == "resume",
        initiated_by=args.initiated_by,
    )
    return _run_ingest(args, options)


def run() -> None:  # pragma: no cover - console script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
