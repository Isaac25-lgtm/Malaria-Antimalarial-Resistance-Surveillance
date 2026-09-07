"""Verify live runtime database credentials without disclosing connection details."""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import Connection, create_engine, text


def _check_live_sync_privileges(connection: Connection, label: str) -> None:
    # The identity role must not have USAGE on mars_analytics. Resolving a
    # schema-qualified table name as that role raises permission denied before
    # has_table_privilege can answer. Its catalog OID requires no schema access.
    table_oid = connection.execute(
        text(
            "SELECT c.oid FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'mars_analytics' AND c.relname = 'live_sync_job' "
            "AND c.relkind IN ('r', 'p')"
        )
    ).scalar_one_or_none()
    if table_oid is None:
        raise RuntimeError("durable live synchronization table is missing")
    permissions = [
        connection.execute(
            text("SELECT has_table_privilege(current_user, CAST(:oid AS oid), :privilege)"),
            {"oid": table_oid, "privilege": privilege},
        ).scalar_one()
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    ]
    # A comma-separated privilege argument means ANY, not ALL, in PostgreSQL.
    if label == "application" and not all(permissions):
        raise RuntimeError("application cannot use durable live synchronization")
    if label == "identity" and any(permissions):
        raise RuntimeError("identity role can reach live synchronization evidence")

DATABASES = (
    ("application", "MARS_DATABASE_URL", "mars_app", "mars_core", "mars_identity"),
    (
        "identity",
        "MARS_IDENTITY_DATABASE_URL",
        "mars_identity_service",
        "mars_identity",
        "mars_core",
    ),
)


def _check_database(
    label: str,
    environment_variable: str,
    member_role: str,
    allowed_schema: str,
    denied_schema: str,
    *,
    verify_boundaries: bool,
) -> None:
    database_url = os.environ.get(environment_variable)
    if not database_url:
        raise RuntimeError(f"{environment_variable} is not set")

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            if verify_boundaries:
                is_member = connection.execute(
                    text("SELECT pg_has_role(current_user, :role, 'MEMBER')"),
                    {"role": member_role},
                ).scalar_one()
                can_use_allowed_schema = connection.execute(
                    text("SELECT has_schema_privilege(current_user, :schema, 'USAGE')"),
                    {"schema": allowed_schema},
                ).scalar_one()
                can_use_denied_schema = connection.execute(
                    text("SELECT has_schema_privilege(current_user, :schema, 'USAGE')"),
                    {"schema": denied_schema},
                ).scalar_one()
                if not is_member or not can_use_allowed_schema or can_use_denied_schema:
                    raise RuntimeError("runtime database privilege boundary is invalid")
                _check_live_sync_privileges(connection, label)
    finally:
        engine.dispose()

    check_name = "database privilege boundary" if verify_boundaries else "database login"
    print(f"Live {label} {check_name}: OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-boundaries", action="store_true")
    arguments = parser.parse_args()

    for label, environment_variable, member_role, allowed_schema, denied_schema in DATABASES:
        try:
            _check_database(
                label,
                environment_variable,
                member_role,
                allowed_schema,
                denied_schema,
                verify_boundaries=arguments.verify_boundaries,
            )
        except Exception as error:
            print(
                f"Live {label} database validation failed ({type(error).__name__}). "
                "Connection details were withheld.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
