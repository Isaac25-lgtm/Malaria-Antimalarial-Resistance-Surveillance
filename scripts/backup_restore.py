"""Backup and restore for a MARS database — Prompt 30.

A backup nobody has restored is a hope, not a backup. This script exists so the
restore is a routine command rather than an improvisation at the worst possible
moment, and so the drill can be run on a disposable copy without touching
anything real.

Three subcommands:

``backup``   pg_dump in custom format, which restores selectively and in
             parallel. Plain SQL would restore only in full and only in order.
``restore``  pg_restore into a database that must already exist, and must be
             named as a target explicitly.
``drill``    back up a source, restore into a fresh disposable copy, compare
             table counts, and drop the copy. The whole point: evidence the
             backup can actually be read back.

Credentials never appear here. The connection is read from ``MARS_DATABASE_URL``
or supplied as libpq environment variables, and no password is printed, logged
or written into a dump filename.

The restore path refuses a target whose name does not look disposable unless
``--i-know-this-is-not-a-drill`` is passed. Restoring over the wrong database is
the mistake this guard exists to make difficult.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

#: A target whose name starts with one of these is treated as disposable.
DISPOSABLE_PREFIXES = ("mars_drill_", "mars_restore_", "mars_test_", "mars_tmp_")


def _connection() -> tuple[str, str, str, str]:
    """Host, port, user and database from the environment.

    The password is deliberately not returned: libpq reads ``PGPASSWORD`` or a
    ``.pgpass`` file directly, so it never has to pass through this process's
    argument list, where it would be visible in ``ps``.
    """
    url = os.environ.get("MARS_DATABASE_URL")
    if url:
        parsed = urlparse(url)
        return (
            parsed.hostname or "localhost",
            str(parsed.port or 5432),
            parsed.username or "mars",
            (parsed.path or "/mars").lstrip("/"),
        )
    return (
        os.environ.get("PGHOST", "localhost"),
        os.environ.get("PGPORT", "5432"),
        os.environ.get("PGUSER", "mars"),
        os.environ.get("PGDATABASE", "mars"),
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a libpq tool, surfacing its own error text rather than hiding it."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"{command[0]} failed with exit code {result.returncode}")
    return result


def backup(destination: Path) -> Path:
    """Dump the configured database in custom format."""
    host, port, user, database = _connection()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "pg_dump",
            "--host", host,
            "--port", port,
            "--username", user,
            "--dbname", database,
            # Custom format: selective and parallel restore. Plain SQL restores
            # only in full and only in order, which is the wrong shape for a
            # database where one schema may need recovering on its own.
            "--format", "custom",
            "--no-owner",
            "--no-privileges",
            "--file", str(destination),
        ]
    )
    print(f"backup written: {destination} ({destination.stat().st_size} bytes)")
    return destination


def restore(archive: Path, target: str, *, force: bool = False) -> None:
    """Restore an archive into a named target database."""
    if not force and not target.startswith(DISPOSABLE_PREFIXES):
        raise SystemExit(
            f"Refusing to restore into {target!r}: the name does not look "
            f"disposable (expected one of {DISPOSABLE_PREFIXES}). Restoring "
            "over the wrong database is the mistake this guard exists to make "
            "difficult. Pass --i-know-this-is-not-a-drill to override."
        )
    host, port, user, _ = _connection()
    _run(
        [
            "pg_restore",
            "--host", host,
            "--port", port,
            "--username", user,
            "--dbname", target,
            "--no-owner",
            "--no-privileges",
            str(archive),
        ]
    )
    print(f"restored into {target}")


def _psql(database: str, sql: str) -> str:
    host, port, user, _ = _connection()
    result = _run(
        [
            "psql",
            "--host", host,
            "--port", port,
            "--username", user,
            "--dbname", database,
            "--tuples-only",
            "--no-align",
            "--command", sql,
        ]
    )
    return result.stdout.strip()


TABLE_COUNT_SQL = (
    "SELECT count(*) FROM information_schema.tables "
    "WHERE table_schema LIKE 'mars\\_%' AND table_type = 'BASE TABLE'"
)


def drill(keep: bool = False) -> int:
    """Back up, restore into a fresh copy, compare, and clean up.

    Returns the number of tables verified. The comparison is deliberately
    structural rather than a row count: a drill that passed only because both
    databases were empty would be worthless, so the table count is asserted
    equal *and* non-zero.
    """
    host, port, user, source = _connection()
    stamp = int(time.time())
    target = f"mars_drill_{stamp}"
    archive = Path(os.environ.get("MARS_BACKUP_DIR", ".")) / f"{source}-{stamp}.dump"

    print(f"drill: source={source} target={target}")
    backup(archive)

    _run(["psql", "--host", host, "--port", port, "--username", user,
          "--dbname", "postgres", "--command", f'CREATE DATABASE "{target}"'])
    try:
        _psql(target, "CREATE EXTENSION IF NOT EXISTS postgis")
        restore(archive, target)

        expected = int(_psql(source, TABLE_COUNT_SQL) or 0)
        actual = int(_psql(target, TABLE_COUNT_SQL) or 0)
        if expected == 0:
            raise SystemExit(
                "drill failed: the source database has no MARS tables, so a "
                "matching restore would prove nothing."
            )
        if expected != actual:
            raise SystemExit(f"drill failed: {expected} tables expected, {actual} restored")

        head = _psql(target, "SELECT version_num FROM alembic_version")
        print(f"drill passed: {actual} tables restored, migration head {head}")
        return actual
    finally:
        if keep:
            print(f"drill copy kept: {target}")
        else:
            _run(["psql", "--host", host, "--port", port, "--username", user,
                  "--dbname", "postgres", "--command", f'DROP DATABASE IF EXISTS "{target}"'])
            archive.unlink(missing_ok=True)
            print(f"drill copy dropped: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="dump the configured database")
    backup_parser.add_argument("destination", type=Path)

    restore_parser = subparsers.add_parser("restore", help="restore into a named database")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument("target")
    restore_parser.add_argument(
        "--i-know-this-is-not-a-drill",
        action="store_true",
        dest="force",
        help="restore into a target whose name does not look disposable",
    )

    drill_parser = subparsers.add_parser("drill", help="back up, restore, compare, clean up")
    drill_parser.add_argument("--keep", action="store_true", help="keep the restored copy")

    args = parser.parse_args()
    if args.command == "backup":
        backup(args.destination)
    elif args.command == "restore":
        restore(args.archive, args.target, force=args.force)
    else:
        drill(keep=args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
