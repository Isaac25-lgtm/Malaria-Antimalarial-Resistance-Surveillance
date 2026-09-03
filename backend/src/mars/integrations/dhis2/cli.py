"""Command-line entry point for DHIS2 exchange.

    mars-dhis2 status
    mars-dhis2 sync-metadata  --dry-run
    mars-dhis2 sync-metadata
    mars-dhis2 pull-aggregate --org-unit <UID> --from 2026-03-01 --to 2026-03-31
    mars-dhis2 proposals

Exit codes:

    0  the exchange completed and every identifier resolved
    1  the exchange completed with unresolved mappings - a configuration gap
       someone has to close, not a system failure
    2  usage error
    3  the exchange failed
    4  DHIS2 is not configured, or is configured but disabled

4 is separate because it is the one an operator can fix without looking at
DHIS2 at all, and because a deployment that never talks to DHIS2 should be able
to run this command and be told so plainly.

**No credential is ever printed.** The status output names whether credentials
are present, never what they are.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from sqlalchemy import func, select

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import get_settings
from mars.db.session import session_scope
from mars.domain.enums import IntegrationRunStatus, MappingProposalStatus
from mars.domain.integration import IntegrationMappingProposal, IntegrationRun
from mars.integrations.dhis2.client import ADAPTER_VERSION, Dhis2Client, Dhis2Config
from mars.integrations.dhis2.service import Dhis2SyncService, SyncOptions, SyncReport
from mars.integrations.ports import RemoteScope

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2
EXIT_FAILED = 3
EXIT_NOT_CONFIGURED = 4


def _iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mars-dhis2",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", choices=("status", "sync-metadata", "pull-aggregate", "proposals")
    )
    parser.add_argument(
        "--org-unit",
        action="append",
        default=None,
        metavar="UID",
        help="Remote organisation unit UID, repeatable",
    )
    parser.add_argument("--dataset", action="append", default=None, metavar="UID")
    parser.add_argument("--from", dest="period_from", type=_iso_date)
    parser.add_argument("--to", dest="period_to", type=_iso_date)
    parser.add_argument("--descendants", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report, write nothing")
    parser.add_argument("--resume", action="store_true", help="Continue an unfinished run")
    parser.add_argument(
        "--force", action="store_true", help="Re-run a scope that already completed"
    )
    parser.add_argument("--initiated-by", default="mars-dhis2")
    parser.add_argument("--json", type=argparse.FileType("w", encoding="utf-8"))
    return parser


def _config_or_report() -> Dhis2Config | None:
    settings = get_settings()
    config = Dhis2Config.from_settings(settings)
    if config is None:
        if not settings.dhis2_enabled:
            print(
                "DHIS2 exchange is disabled. Set MARS_DHIS2_ENABLED=true and supply "
                "MARS_DHIS2_BASE_URL to enable it.",
                file=sys.stderr,
            )
        else:
            print(
                "DHIS2 exchange is enabled but no base URL is configured. Set MARS_DHIS2_BASE_URL.",
                file=sys.stderr,
            )
        return None
    return config


def _status() -> int:
    """Report configuration without disclosing any of it."""
    settings = get_settings()
    config = Dhis2Config.from_settings(settings)

    print(f"adapter version   {ADAPTER_VERSION}")
    print(f"enabled           {settings.dhis2_enabled}")
    print(f"base url          {config.base_url if config else '<not configured>'}")
    # Presence, never value. A status command is exactly where a token gets
    # pasted into a support ticket.
    print(f"credentials       {'present' if config and config.has_credentials else 'absent'}")
    print(f"tls verification  {settings.dhis2_verify_tls}")
    print(f"outbound push     {settings.dhis2_push_enabled} (disabled by default)")
    print()

    with session_scope() as session:
        rows = session.execute(
            select(
                IntegrationRun.resource,
                IntegrationRun.run_status,
                func.count(IntegrationRun.id),
                func.max(IntegrationRun.started_at),
            ).group_by(IntegrationRun.resource, IntegrationRun.run_status)
        ).all()
        unresolved = session.execute(
            select(func.count())
            .select_from(IntegrationMappingProposal)
            .where(IntegrationMappingProposal.proposal_status == MappingProposalStatus.PROPOSED)
        ).scalar_one()

    if rows:
        print("runs:")
        for resource, status, count, latest in rows:
            print(f"  {resource.value:28s} {status.value:10s} {count:4d}  latest {latest}")
    else:
        print("runs:            none recorded")

    print()
    print(f"unresolved mappings: {unresolved}")
    if unresolved:
        print("  These are configuration gaps. MARS will not guess a mapping by name.")

    if config is None:
        return EXIT_NOT_CONFIGURED
    if not config.has_credentials:
        print()
        print("WARNING: no credentials configured; requests will be unauthenticated.")
    return EXIT_ATTENTION if unresolved else EXIT_OK


def _proposals() -> int:
    with session_scope() as session:
        proposals = (
            session.execute(
                select(IntegrationMappingProposal)
                .where(IntegrationMappingProposal.proposal_status == MappingProposalStatus.PROPOSED)
                .order_by(IntegrationMappingProposal.occurrences.desc())
                .limit(200)
            )
            .scalars()
            .all()
        )

        if not proposals:
            print("no unresolved mappings")
            return EXIT_OK

        print(f"{len(proposals)} unresolved mapping(s), most frequent first:")
        print()
        for proposal in proposals:
            print(
                f"  {proposal.remote_type:20s} {proposal.remote_id:14s} "
                f"seen {proposal.occurrences:4d}x  {proposal.remote_name or ''}"
            )
        print()
        print("Resolve each by recording an accepted crosswalk entry. MARS does not")
        print("match on name similarity: two districts with similar names are exactly")
        print("the case a fuzzy match gets wrong, and the figures still look plausible.")
    return EXIT_ATTENTION


def _report_exit(report: SyncReport) -> int:
    if report.status is IntegrationRunStatus.FAILED:
        return EXIT_FAILED
    if report.mappings_unresolved:
        return EXIT_ATTENTION
    return EXIT_OK


def _print_report(report: SyncReport) -> None:
    print(f"run      {report.run_id}")
    print(f"resource {report.resource.value if report.resource else '-'}")
    print(f"status   {report.status.value}")
    print()
    for label, value in (
        ("pages fetched", report.pages_fetched),
        ("records received", report.records_received),
        ("records accepted", report.records_accepted),
        ("records rejected", report.records_rejected),
        ("unresolved mappings", report.mappings_unresolved),
    ):
        print(f"  {label:22s} {value:6d}")
    if report.payload_checksum:
        print(f"  {'payload checksum':22s} {report.payload_checksum[:16]}…")
    if report.error_summary:
        print(file=sys.stderr)
        category = report.error_category.value if report.error_category else "unknown"
        print(f"  FAILED [{category}]: {report.error_summary}", file=sys.stderr)


def _sync_metadata(args: argparse.Namespace) -> int:
    config = _config_or_report()
    if config is None:
        return EXIT_NOT_CONFIGURED

    options = SyncOptions(
        dry_run=args.dry_run,
        resume=args.resume,
        force=args.force,
        initiated_by=args.initiated_by,
    )
    with session_scope() as session, Dhis2Client(config) as client:
        report = Dhis2SyncService(session, client).sync_organisation_units(options)

    _print_report(report)
    if args.json:
        json.dump(report.as_dict(), args.json, indent=2)
    return _report_exit(report)


def _pull_aggregate(args: argparse.Namespace) -> int:
    if not args.org_unit:
        print(
            "ERROR: at least one --org-unit is required. MARS will not pull a "
            "whole DHIS2 instance implicitly.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not (args.period_from and args.period_to):
        print("ERROR: --from and --to are required", file=sys.stderr)
        return EXIT_USAGE

    config = _config_or_report()
    if config is None:
        return EXIT_NOT_CONFIGURED

    scope = RemoteScope(
        organisation_unit_remote_ids=tuple(args.org_unit),
        dataset_remote_ids=tuple(args.dataset or ()),
        period_start=args.period_from,
        period_end=args.period_to,
        include_descendants=args.descendants,
    )
    options = SyncOptions(
        dry_run=args.dry_run,
        resume=args.resume,
        force=args.force,
        initiated_by=args.initiated_by,
    )

    with session_scope() as session, Dhis2Client(config) as client:
        report, grouped = Dhis2SyncService(session, client).pull_aggregate_values(scope, options)

    _print_report(report)
    print()
    print(f"  {len(grouped)} facility-period group(s) ready for canonical ingestion")
    print("  Values are loaded through the ordinary aggregate pipeline, so DHIS2")
    print("  content meets the same validation and revision rules as a paper form.")
    if args.json:
        json.dump({"report": report.as_dict(), "groups": grouped}, args.json, indent=2)
    return _report_exit(report)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())

    if args.command == "status":
        return _status()
    if args.command == "proposals":
        return _proposals()
    if args.command == "sync-metadata":
        return _sync_metadata(args)
    return _pull_aggregate(args)


def run() -> None:  # pragma: no cover - console script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
