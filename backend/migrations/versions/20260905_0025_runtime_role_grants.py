"""Grant the runtime application role access without exposing identity.

Revision ID: 0025_runtime_role_grants
Revises: 0024_investigation_hardening
Created: 2026-09-05

Migrations run as the object owner.  The API must not: an owner can bypass the
schema boundary and read ``mars_identity`` even when no grant was made.  This
revision gives the provisioned ``mars_app`` group only the clinical,
governance, security, analytics and append-only audit privileges it needs.

The role remains an optional cluster-provisioning concern.  A database can be
migrated before ``scripts/provision_identity_roles.sql`` is run; in that case
this revision is a no-op and must be re-applied by an operator after the role
exists.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_runtime_role_grants"
down_revision: str | None = "0024_investigation_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "mars_app"
READ_WRITE_SCHEMAS = (
    "mars_core",
    "mars_security",
    "mars_governance",
    "mars_analytics",
)
AUDIT_SCHEMA = "mars_audit"
IDENTITY_SCHEMA = "mars_identity"


def upgrade() -> None:
    schemas = ", ".join((*READ_WRITE_SCHEMAS, AUDIT_SCHEMA))
    statements = [
        f"GRANT USAGE ON SCHEMA {schemas} TO {APP_ROLE}",
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
    ]
    defaults = [
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
    _execute_if_role_exists((*statements, *defaults))


def downgrade() -> None:
    schemas = ", ".join((*READ_WRITE_SCHEMAS, AUDIT_SCHEMA))
    statements = [
        f"REVOKE ALL ON SCHEMA {schemas} FROM {APP_ROLE}",
        *(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {APP_ROLE}"
            for schema in (*READ_WRITE_SCHEMAS, AUDIT_SCHEMA)
        ),
        *(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {APP_ROLE}"
            for schema in (*READ_WRITE_SCHEMAS, AUDIT_SCHEMA)
        ),
        f"REVOKE ALL ON TABLE public.alembic_version FROM {APP_ROLE}",
        *(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON TABLES FROM {APP_ROLE}"
            for schema in (*READ_WRITE_SCHEMAS, AUDIT_SCHEMA)
        ),
        *(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON SEQUENCES FROM {APP_ROLE}"
            for schema in (*READ_WRITE_SCHEMAS, AUDIT_SCHEMA)
        ),
    ]
    _execute_if_role_exists(tuple(statements))


def _execute_if_role_exists(statements: tuple[str, ...]) -> None:
    body = "\n".join(f"EXECUTE {statement!r};" for statement in statements)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                {body}
            END IF;
        END
        $$;
        """
    )
