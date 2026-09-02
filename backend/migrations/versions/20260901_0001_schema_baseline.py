"""Schema baseline: create the MARS schema boundaries.

Revision ID: 0001_schema_baseline
Revises:
Created: 2026-09-01

Creates the six PostgreSQL schemas and the pgcrypto extension that supplies
``gen_random_uuid()``.

``mars_identity`` is created here, empty, deliberately. It will hold direct
patient identifiers from Prompt 8 onwards. Creating the boundary before the data
exists means the separation is never retrofitted, and the role grants that
enforce it can be provisioned by an operator from the first deployment.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_schema_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMAS: tuple[str, ...] = (
    "mars_core",
    "mars_identity",
    "mars_audit",
    "mars_security",
    "mars_governance",
    "mars_analytics",
)

SCHEMA_COMMENTS: dict[str, str] = {
    "mars_core": "Canonical surveillance data. Contains no direct patient identifier.",
    "mars_identity": (
        "Direct patient identifiers. Separate database role; the application role "
        "holds no grant here. Empty until Prompt 8."
    ),
    "mars_audit": "Append-only audit events. No update or delete path is exposed.",
    "mars_security": "Users, roles, permissions, geography and sensitivity scopes.",
    "mars_governance": "Configuration versions and the analytical method registry.",
    "mars_analytics": "Derived and materialised analytical output. Rebuildable.",
}


def upgrade() -> None:
    # gen_random_uuid() backs every UUID primary key default.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        comment = SCHEMA_COMMENTS[schema].replace("'", "''")
        op.execute(f"COMMENT ON SCHEMA \"{schema}\" IS '{comment}'")


def downgrade() -> None:
    # RESTRICT, not CASCADE: dropping a schema that still holds surveillance
    # data must fail loudly rather than silently discarding it.
    for schema in reversed(SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" RESTRICT')
