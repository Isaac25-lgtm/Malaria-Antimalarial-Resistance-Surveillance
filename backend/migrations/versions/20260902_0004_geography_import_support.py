"""Geography import support: checksum lookup and single-publication guarantee.

Revision ID: 0004_geography_import_support
Revises: 0003_phase2_hardening
Created: 2026-09-02

Two guarantees the importer relies on, enforced by the database rather than by
the service that happens to be running:

**Checksum lookup.** Re-import detection asks whether these exact source bytes
are already published. That is a lookup on every import, so it gets an index.

**One published version.** Exactly one boundary version may be published at a
time. Without this, a concurrent import or a partially-completed run could leave
two competing hierarchies published, and no query could say which one the
facilities and user scopes belong to.

A partial unique index is used rather than a table constraint because only the
published row is constrained: superseded, failed and validating versions are
retained without limit, and those records are the import history.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_geography_import_support"
down_revision: str | None = "0003_phase2_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_boundary_version_source_checksum",
        "boundary_version",
        ["source_checksum"],
        unique=False,
        schema="mars_core",
    )

    # Indexed on the enum column directly. Casting it to text would make the
    # index expression non-IMMUTABLE, which PostgreSQL refuses. Since the WHERE
    # clause already restricts the index to rows whose status is 'published',
    # every indexed row holds the same value, so uniqueness on that column is
    # exactly the "at most one published version" guarantee.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_boundary_version_single_published
            ON mars_core.boundary_version (import_status)
         WHERE import_status = 'published'
        """
    )

    op.execute(
        "COMMENT ON INDEX mars_core.uq_boundary_version_single_published IS "
        "'Exactly one boundary version may be published at a time, so every "
        "geography unit belongs to one unambiguous published hierarchy.'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mars_core.uq_boundary_version_single_published")
    op.drop_index(
        "ix_boundary_version_source_checksum",
        table_name="boundary_version",
        schema="mars_core",
    )
