"""Command-line entry point for HMIS 033b and 105 ingestion.

    mars-import-aggregate load       --file returns.jsonl
    mars-import-aggregate validate   --file returns.jsonl   # findings, no submissions
    mars-import-aggregate dry-run    --file returns.jsonl   # writes nothing
    mars-import-aggregate reconcile  --facility HF-401 --from 2026-03-01 --to 2026-03-31

Exit codes, so a scheduler can branch on them:

    0  loaded, and every comparison agreed
    1  loaded with quarantined submissions, or reconciliation found differences
    2  usage error
    3  the batch failed as a whole

1 covers both "a producer has work to do" and "a district has a discrepancy to
look at". Both need a person; neither is a system failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import get_settings
from mars.db.session import session_scope
from mars.domain.aggregate import AggregateSubmission
from mars.domain.enums import AggregateSubmissionStatus
from mars.domain.organisation import Facility
from mars.ingestion.aggregate.pipeline import (
    AggregateIngestionPipeline,
    AggregateIngestOptions,
    AggregateIngestReport,
)
from mars.services.reconciliation import (
    RECONCILIATION_METHOD_VERSION,
    ReconciliationService,
)

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2
EXIT_BATCH_FAILED = 3


def _iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mars-import-aggregate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("load", "validate", "dry-run", "reconcile"))
    parser.add_argument("--file", type=Path, help="The artefact to ingest")
    parser.add_argument("--facility", help="Facility code, for reconcile")
    parser.add_argument("--from", dest="period_from", type=_iso_date, help="Period start")
    parser.add_argument("--to", dest="period_to", type=_iso_date, help="Period end")
    parser.add_argument(
        "--tolerance",
        type=int,
        default=0,
        help=(
            "Absolute difference treated as agreement. Defaults to 0: no "
            "supplied source defines an acceptable transcription variance, so "
            "MARS does not invent one."
        ),
    )
    parser.add_argument(
        "--initiated-by",
        default="mars-import-aggregate",
        help="Service label recorded on the submission. Never a personal name.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-process a previously interrupted or completed artefact safely",
    )
    parser.add_argument("--json", type=Path, help="Write the full report document here")
    return parser


def _ingest(args: argparse.Namespace, options: AggregateIngestOptions) -> int:
    artefact: Path | None = args.file
    if artefact is None:
        print("ERROR: --file is required", file=sys.stderr)
        return EXIT_USAGE
    if not artefact.is_file():
        print(f"ERROR: {artefact} is not a file", file=sys.stderr)
        return EXIT_USAGE

    with session_scope() as session:
        pipeline = AggregateIngestionPipeline(session)
        report = pipeline.run(artefact, options)

    _print(report, options)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    if report.failure_reason:
        return EXIT_BATCH_FAILED
    if report.submissions_quarantined:
        return EXIT_ATTENTION
    return EXIT_OK


def _print(report: AggregateIngestReport, options: AggregateIngestOptions) -> None:
    mode = "DRY RUN" if options.dry_run else "VALIDATE" if options.validate_only else "LOAD"
    print(f"{mode}")
    print()
    print(f"  {'batch id':20s} {str(report.batch_id) if report.batch_id else '-'}")
    print(f"  {'status':20s} {report.status.value}")
    for label, value in (
        ("received", report.submissions_received),
        ("loaded", report.submissions_loaded),
        ("unchanged", report.submissions_unchanged),
        ("superseding", report.submissions_superseding),
        ("quarantined", report.submissions_quarantined),
        ("unresolved facility", report.unresolved_facility),
        ("observations", report.observations_loaded),
        ("stock rows", report.stock_rows_loaded),
        ("laboratory rows", report.laboratory_rows_loaded),
        ("blank cells", report.blank_cells),
        ("zero cells", report.zero_cells),
        ("warnings", report.warning_count),
        ("errors", report.error_count),
    ):
        print(f"  {label:20s} {value:6d}")

    # Printed side by side deliberately: a month of blanks and a month of zeros
    # look identical in a total and are opposite facts about whether the
    # facility reported.
    print()
    print("  blank means the facility made no statement; zero means it reported none")

    if report.issue_codes:
        print()
        print("  issues by code:")
        for code, count in sorted(report.issue_codes.items(), key=lambda item: -item[1]):
            print(f"    {code}: {count}")

    if report.failure_reason:
        print(file=sys.stderr)
        print(f"  FAILED: {report.failure_reason}", file=sys.stderr)


def _reconcile(args: argparse.Namespace) -> int:
    if not args.facility:
        print("ERROR: --facility is required for reconcile", file=sys.stderr)
        return EXIT_USAGE

    with session_scope() as session:
        facility = session.execute(
            select(Facility).where(Facility.code == args.facility)
        ).scalar_one_or_none()
        if facility is None:
            print(f"ERROR: no facility with code {args.facility!r}", file=sys.stderr)
            return EXIT_USAGE

        query = select(AggregateSubmission).where(
            AggregateSubmission.facility_id == facility.id,
            AggregateSubmission.submission_status != AggregateSubmissionStatus.SUPERSEDED,
        )
        if args.period_from:
            query = query.where(AggregateSubmission.period_start >= args.period_from)
        if args.period_to:
            query = query.where(AggregateSubmission.period_end <= args.period_to)

        submissions = (
            session.execute(query.order_by(AggregateSubmission.period_start)).scalars().all()
        )
        if not submissions:
            print("no submissions in that range")
            return EXIT_OK

        service = ReconciliationService(session, tolerance=args.tolerance)
        differences = 0
        print(f"RECONCILE  {facility.code}  method {RECONCILIATION_METHOD_VERSION}")
        print()
        for submission in submissions:
            report = service.reconcile(submission)
            differences += report.differs
            print(
                f"  {submission.form.value:10s} "
                f"{submission.period_start} → {submission.period_end}  "
                f"matched={report.matched} within_tolerance={report.within_tolerance} "
                f"differs={report.differs} uncomparable={report.uncomparable} "
                f"(from {report.encounters_in_period} encounters)"
            )

    print()
    print("  Neither figure is corrected. A difference is a finding for the district.")
    return EXIT_ATTENTION if differences else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())

    if args.command == "reconcile":
        return _reconcile(args)

    options = AggregateIngestOptions(
        dry_run=args.command == "dry-run",
        validate_only=args.command == "validate",
        resume=args.resume,
        initiated_by=args.initiated_by,
    )
    return _ingest(args, options)


def run() -> None:  # pragma: no cover - console script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
