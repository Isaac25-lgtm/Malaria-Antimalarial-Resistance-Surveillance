"""Command-line entry point for the geography importer.

    mars-import-geography --data-dir "F:/.../MARS IMPLEMENTATION" --dry-run
    mars-import-geography --data-dir ... --imported-by "ops:initial-load"

The data directory is always supplied by the caller. Nothing here hard-codes a
filesystem path, so the same command runs in a container, in CI and on a
developer machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import get_settings
from mars.db.session import session_scope
from mars.ingestion.geography.importer import GeographyImporter, ImportOptions
from mars.ingestion.geography.reader import SourceRole
from mars.ingestion.geography.result import ImportOutcome

logger = get_logger(__name__)

#: Filenames for each source role. These are the supplied names; a different
#: deployment can override them on the command line.
DEFAULT_FILENAMES: dict[SourceRole, str] = {
    SourceRole.COUNTRY_BOUNDARY: "COUNTRY_BOUNDARY.json",
    SourceRole.DISTRICT_GEOMETRY: "UGANDA_DISTRICT.json",
    SourceRole.SUBCOUNTY_HIERARCHY: "UGANDA_SUBCOUNTIES.json",
}


def resolve_sources(
    data_dir: Path, overrides: dict[str, str] | None = None
) -> dict[SourceRole, Path]:
    """Map each source role onto a file in ``data_dir``.

    ``UGANDA_DISTRICTS.json`` - the Esri twin - is deliberately absent. It is
    the CRS and field-schema witness, and ADR 0004 records that it is never
    imported.
    """
    overrides = overrides or {}
    resolved: dict[SourceRole, Path] = {}
    for role, default in DEFAULT_FILENAMES.items():
        resolved[role] = data_dir / overrides.get(role.value, default)
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mars-import-geography",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Directory holding the supplied boundary files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report without writing anything",
    )
    parser.add_argument(
        "--skip-geometry",
        action="store_true",
        help="Build the hierarchy only. Units still scope correctly without geometry.",
    )
    parser.add_argument(
        "--skip-derived-geometry",
        action="store_true",
        help="Do not dissolve region and county geometry from their children",
    )
    parser.add_argument(
        "--skip-simplification",
        action="store_true",
        help="Do not build browser geometry. Raw geometry must never reach a client.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-import even when these exact source bytes are already published",
    )
    parser.add_argument(
        "--imported-by",
        default="geography-importer-cli",
        help="Service label recorded on the boundary version. Never a personal name.",
    )
    parser.add_argument("--note", help="Free-text note stored with the boundary version")
    parser.add_argument("--json", type=Path, help="Write the full result document here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())

    data_dir: Path = args.data_dir
    if not data_dir.is_dir():
        print(f"ERROR: {data_dir} is not a directory", file=sys.stderr)
        return 2

    sources = resolve_sources(data_dir)
    missing = [path.name for path in sources.values() if not path.exists()]
    if missing:
        print(f"ERROR: source file(s) not found in {data_dir}: {missing}", file=sys.stderr)
        return 2

    options = ImportOptions(
        dry_run=args.dry_run,
        load_geometry=not args.skip_geometry,
        derive_geometry=not args.skip_derived_geometry,
        simplify_geometry=not args.skip_simplification,
        force=args.force,
        imported_by=args.imported_by,
        note=args.note,
    )

    with session_scope() as session:
        importer = GeographyImporter(session, sources)
        result = importer.run(options)

        if result.outcome in (ImportOutcome.VALIDATION_FAILED, ImportOutcome.FAILED):
            # The failed attempt is retained by the importer inside this
            # transaction, so it is committed rather than rolled back. What is
            # never committed is a half-published hierarchy.
            pass

    print(result.summary_line())
    print()
    for level_name in ("country", "region", "district", "county", "subcounty"):
        counts = result.levels.get(level_name)
        if counts is None:
            continue
        print(
            f"  {level_name:10s} created={counts.created:5d} updated={counts.updated:5d} "
            f"geometry={counts.with_geometry:5d} repaired={counts.repaired_geometry:3d} "
            f"quarantined={counts.quarantined_geometry:3d}"
        )

    if result.control_totals:
        print()
        print("  control totals:")
        for key, value in result.control_totals.items():
            if key != "note":
                print(f"    {key}: {value}")

    blocking = result.blocking_issues
    if blocking:
        print(file=sys.stderr)
        print(f"  {len(blocking)} blocking issue(s):", file=sys.stderr)
        for issue in blocking[:20]:
            print(f"    {issue.code}: {issue.detail}", file=sys.stderr)

    advisory = [issue for issue in result.issues if not issue.blocking]
    if advisory:
        print()
        print(f"  {len(advisory)} advisory issue(s), by code:")
        by_code: dict[str, int] = {}
        for issue in advisory:
            by_code[issue.code] = by_code.get(issue.code, 0) + 1
        for code, count in sorted(by_code.items(), key=lambda item: -item[1]):
            print(f"    {code}: {count}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result.as_dict(), indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0 if result.succeeded else 1


def run() -> None:  # pragma: no cover - console script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
