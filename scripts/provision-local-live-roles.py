"""Provision restricted MARS login roles on a local PostgreSQL cluster."""

from __future__ import annotations

import os
import sys

from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

ROLE_MEMBERSHIPS = (
    ("mars_app_login", "MARS_DATABASE_URL", "mars_app"),
    ("mars_identity_login", "MARS_IDENTITY_DATABASE_URL", "mars_identity_service"),
)


def _required_url(environment_variable: str) -> str:
    value = os.environ.get(environment_variable)
    if not value:
        raise RuntimeError(f"{environment_variable} is not set")
    return value


def main() -> int:
    try:
        migration_url = _required_url("MARS_MIGRATION_DATABASE_URL")
        runtime_roles = [
            (role, make_url(_required_url(environment_variable)).password, member_role)
            for role, environment_variable, member_role in ROLE_MEMBERSHIPS
        ]

        engine = create_engine(migration_url, connect_args={"connect_timeout": 5})
        try:
            raw_connection = engine.raw_connection()
            try:
                try:
                    with raw_connection.cursor() as cursor:
                        for member_role in ("mars_app", "mars_identity_service"):
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                                (member_role,),
                            )
                            if not cursor.fetchone()[0]:
                                cursor.execute(
                                    sql.SQL("CREATE ROLE {} NOLOGIN").format(
                                        sql.Identifier(member_role)
                                    )
                                )

                        for role, password, member_role in runtime_roles:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                                (role,),
                            )
                            if not cursor.fetchone()[0]:
                                cursor.execute(
                                    sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
                                )
                            cursor.execute(
                                sql.SQL(
                                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                    "NOREPLICATION NOBYPASSRLS"
                                ).format(sql.Identifier(role))
                            )
                            if password is not None:
                                cursor.execute(
                                    sql.SQL("ALTER ROLE {} PASSWORD %s").format(
                                        sql.Identifier(role)
                                    ),
                                    (password,),
                                )
                            cursor.execute(
                                sql.SQL("GRANT {} TO {}").format(
                                    sql.Identifier(member_role),
                                    sql.Identifier(role),
                                )
                            )
                    raw_connection.commit()
                except Exception:
                    raw_connection.rollback()
                    raise
            finally:
                raw_connection.close()
        finally:
            engine.dispose()
    except Exception as error:
        print(
            f"Local runtime role provisioning failed ({type(error).__name__}). "
            "Connection details were withheld.",
            file=sys.stderr,
        )
        return 1

    print("Local restricted runtime roles provisioned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
