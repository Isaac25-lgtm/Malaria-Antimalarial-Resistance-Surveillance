"""Reapply restricted runtime grants after local role provisioning.

Revision ID: 0026_reapply_runtime_role_grants
Revises: 0025_runtime_role_grants
Created: 2026-09-05

The runtime roles are intentionally provisioned outside Alembic.  Databases
that reached revision 0025 before those roles existed therefore need one
idempotent pass that establishes the privilege boundary after provisioning.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_reapply_runtime_role_grants"
down_revision: str | None = "0025_runtime_role_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "mars_app"
IDENTITY_ROLE = "mars_identity_service"
READ_WRITE_SCHEMAS = (
    "mars_core",
    "mars_security",
    "mars_governance",
    "mars_analytics",
)
AUDIT_SCHEMA = "mars_audit"
IDENTITY_SCHEMA = "mars_identity"


def upgrade() -> None:
    op.execute(f"REVOKE ALL ON SCHEMA {IDENTITY_SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {IDENTITY_SCHEMA} FROM PUBLIC")

    app_schemas = ", ".join((*READ_WRITE_SCHEMAS, AUDIT_SCHEMA))
    app_statements = [
        f"GRANT USAGE ON SCHEMA {app_schemas} TO {APP_ROLE}",
        *(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} "
            f"TO {APP_ROLE}"
            for schema in READ_WRITE_SCHEMAS
        ),
        f"GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA {AUDIT_SCHEMA} TO {APP_ROLE}",
        *(
            f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {schema} TO {APP_ROLE}"
            for schema in (*READ_WRITE_SCHEMAS, AUDIT_SCHEMA)
        ),
        f"REVOKE ALL ON SCHEMA {IDENTITY_SCHEMA} FROM {APP_ROLE}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {IDENTITY_SCHEMA} FROM {APP_ROLE}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {IDENTITY_SCHEMA} FROM {APP_ROLE}",
        f"GRANT SELECT ON TABLE public.alembic_version TO {APP_ROLE}",
        *(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
            for schema in READ_WRITE_SCHEMAS
        ),
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {AUDIT_SCHEMA} "
        f"GRANT SELECT, INSERT ON TABLES TO {APP_ROLE}",
        *(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {APP_ROLE}"
            for schema in (*READ_WRITE_SCHEMAS, AUDIT_SCHEMA)
        ),
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {IDENTITY_SCHEMA} "
        f"REVOKE ALL ON TABLES FROM {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {IDENTITY_SCHEMA} "
        f"REVOKE ALL ON SEQUENCES FROM {APP_ROLE}",
    ]
    _execute_if_role_exists(APP_ROLE, app_statements)

    denied_identity_schemas = (
        "mars_core",
        "mars_security",
        "mars_governance",
        "mars_analytics",
        "mars_audit",
    )
    identity_statements = [
        f"GRANT USAGE ON SCHEMA {IDENTITY_SCHEMA} TO {IDENTITY_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {IDENTITY_SCHEMA}.identity_record "
        f"TO {IDENTITY_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {IDENTITY_SCHEMA}.identity_identifier "
        f"TO {IDENTITY_ROLE}",
        f"GRANT SELECT, INSERT ON {IDENTITY_SCHEMA}.reidentification_event "
        f"TO {IDENTITY_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {IDENTITY_SCHEMA} "
        f"GRANT SELECT, INSERT ON TABLES TO {IDENTITY_ROLE}",
        *(
            f"REVOKE ALL ON SCHEMA {schema} FROM {IDENTITY_ROLE}"
            for schema in denied_identity_schemas
        ),
        *(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {IDENTITY_ROLE}"
            for schema in denied_identity_schemas
        ),
        *(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {IDENTITY_ROLE}"
            for schema in denied_identity_schemas
        ),
    ]
    _execute_if_role_exists(IDENTITY_ROLE, identity_statements)


def downgrade() -> None:
    # Revisions 0006 and 0025 define this same boundary. Reversing the repair
    # would make a downgrade less secure than the target revision, so restore
    # that target revision's intended state idempotently.
    upgrade()


def _execute_if_role_exists(role: str, statements: list[str]) -> None:
    body = "\n".join(f"EXECUTE {statement!r};" for statement in statements)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                {body}
            END IF;
        END
        $$;
        """
    )
