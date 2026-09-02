"""Historical boundary versioning: identity separated from representation.

Revision ID: 0008_geography_versioning
Revises: 0007_age_pair_invariant
Created: 2026-09-02

The importer matched units on ``(level, preferred_code)``, kept the UUID, then
overwrote name, parent, depth, path, boundary version, active state and
geometry. A second import therefore **destroyed the first**: the earlier
``BoundaryVersion`` row survived as metadata describing boundaries that nothing
could reconstruct, and an analysis pinned to that version could not be
reproduced. Geometry was worse - one row per stable unit, so a recut overwrote
the previous shape in place.

This migration separates the two ideas that were conflated:

``geography_unit``           the **stable identity**. A UUID that facilities,
                             user scopes and encounters point at. Never
                             renumbered, never re-parented, never deleted.

``geography_unit_revision``  what one **boundary version** said about that unit:
                             name, kind, parent, depth, path, presence, dates.

Geometry moves from one row per unit to one row per ``(unit, boundary version)``.

Committed migrations 0001-0004 are not edited; this is a forward correction that
backfills the current hierarchy into the revision belonging to its own boundary
version, preserving every existing identifier and foreign key.

The columns on ``geography_unit`` are **kept**, and are now a cache of the
currently published revision - fast to read, and structurally incapable of
answering a historical question. A comment on each says so, and the guard tests
assert that historical code paths read revisions instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_geography_versioning"
down_revision: str | None = "0007_age_pair_invariant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE = "mars_core"


def upgrade() -> None:
    # -- The revision table -------------------------------------------------
    op.create_table(
        "geography_unit_revision",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("geography_unit_id", sa.UUID(), nullable=False),
        sa.Column("boundary_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "level",
            postgresql.ENUM(name="geography_level", schema=CORE, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "unit_kind",
            postgresql.ENUM(name="geography_unit_kind", schema=CORE, create_type=False),
            nullable=False,
        ),
        sa.Column("preferred_code", sa.String(length=32), nullable=False),
        sa.Column("raw_name", sa.String(length=255), nullable=False),
        sa.Column("normalised_name", sa.String(length=255), nullable=False),
        sa.Column("parent_revision_id", sa.UUID(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("is_present", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geography_unit_revision")),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            [f"{CORE}.geography_unit.id"],
            name=op.f("fk_geography_unit_revision_geography_unit_id_geography_unit"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["boundary_version_id"],
            [f"{CORE}.boundary_version.id"],
            name=op.f("fk_geography_unit_revision_boundary_version_id_boundary_version"),
            ondelete="CASCADE",
        ),
        # Self-referential: the parent *within this version*. Pointing at the
        # stable unit instead would lose which parent a subcounty had at the
        # time, which is exactly what a recut changes.
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            [f"{CORE}.geography_unit_revision.id"],
            # 69 characters under the convention, which PostgreSQL truncates
            # at 63 - so it is named explicitly and stays what the model says.
            name="fk_geo_revision_parent_revision",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "geography_unit_id",
            "boundary_version_id",
            name="uq_geography_unit_revision_unit_version",
        ),
        # A code is unique within a level *within a version*. A recut may
        # reassign one, and both assignments are historical fact.
        sa.UniqueConstraint(
            "boundary_version_id",
            "level",
            "preferred_code",
            name="uq_geography_unit_revision_version_level_code",
        ),
        schema=CORE,
        comment=(
            "One geography unit as one boundary version described it. Immutable "
            "once that version is published: a later recut adds revisions, it "
            "never rewrites them."
        ),
    )
    for name, columns in (
        ("ix_geography_unit_revision_version", ["boundary_version_id"]),
        ("ix_geography_unit_revision_unit", ["geography_unit_id"]),
        ("ix_geography_unit_revision_parent", ["parent_revision_id"]),
        ("ix_geography_unit_revision_path", ["boundary_version_id", "path"]),
    ):
        op.create_index(name, "geography_unit_revision", columns, schema=CORE)

    # -- Geometry gains its version ----------------------------------------
    op.add_column(
        "geography_unit_geometry",
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        schema=CORE,
    )
    op.create_foreign_key(
        op.f("fk_geography_unit_geometry_boundary_version_id_boundary_version"),
        "geography_unit_geometry",
        "boundary_version",
        ["boundary_version_id"],
        ["id"],
        source_schema=CORE,
        referent_schema=CORE,
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_geography_unit_geometry_version",
        "geography_unit_geometry",
        ["boundary_version_id"],
        schema=CORE,
    )

    # -- Backfill -----------------------------------------------------------
    #
    # Every unit currently carries the version it was last imported under.
    # That state becomes its revision under that version, so the hierarchy
    # already loaded stays queryable and every existing UUID is preserved.
    op.execute(
        f"""
        INSERT INTO {CORE}.geography_unit_revision
            (id, geography_unit_id, boundary_version_id, level, unit_kind,
             preferred_code, raw_name, normalised_name, depth, path,
             is_present, effective_from, effective_to, created_at, updated_at)
        SELECT gen_random_uuid(), u.id, u.boundary_version_id, u.level,
               u.unit_kind, u.preferred_code, u.raw_name, u.normalised_name,
               u.depth, u.path, u.is_active, u.effective_from, u.effective_to,
               now(), now()
          FROM {CORE}.geography_unit AS u
         WHERE u.boundary_version_id IS NOT NULL
        """
    )

    # Parent links are resolved in a second pass: a revision's parent is the
    # revision of its parent *under the same version*, which cannot be known
    # until every revision exists.
    op.execute(
        f"""
        UPDATE {CORE}.geography_unit_revision AS child
           SET parent_revision_id = parent.id
          FROM {CORE}.geography_unit AS u
          JOIN {CORE}.geography_unit_revision AS parent
            ON parent.geography_unit_id = u.parent_id
         WHERE child.geography_unit_id = u.id
           AND parent.boundary_version_id = child.boundary_version_id
           AND u.parent_id IS NOT NULL
        """
    )

    op.execute(
        f"""
        UPDATE {CORE}.geography_unit_geometry AS g
           SET boundary_version_id = u.boundary_version_id
          FROM {CORE}.geography_unit AS u
         WHERE g.geography_unit_id = u.id
           AND u.boundary_version_id IS NOT NULL
        """
    )

    # -- Geometry uniqueness moves to (unit, version) -----------------------
    op.drop_constraint(
        "uq_geography_unit_geometry_geography_unit_id",
        "geography_unit_geometry",
        schema=CORE,
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_geography_unit_geometry_unit_version",
        "geography_unit_geometry",
        ["geography_unit_id", "boundary_version_id"],
        schema=CORE,
    )

    # -- Immutability of published history ----------------------------------
    #
    # The defect was a later import silently rewriting an earlier version's
    # description. Application discipline is not enough: the importer *is* the
    # code that would do it, so the database refuses.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {CORE}.reject_published_revision_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            version_status text;
        BEGIN
            -- A DELETE cascading from the geography unit itself is not a
            -- rewrite of history: the whole unit is going, and its record of
            -- what each version said goes with it. By the time this fires the
            -- parent row is already gone, which is how the two are told apart.
            IF TG_OP = 'DELETE' AND NOT EXISTS (
                SELECT 1 FROM {CORE}.geography_unit WHERE id = OLD.geography_unit_id
            ) THEN
                RETURN OLD;
            END IF;

            SELECT import_status::text INTO version_status
              FROM {CORE}.boundary_version
             WHERE id = COALESCE(OLD.boundary_version_id, NEW.boundary_version_id);

            IF version_status = 'published' THEN
                RAISE EXCEPTION
                    'boundary version is published; its geography revisions are '
                    'immutable (% attempted)', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER geography_unit_revision_immutable
            BEFORE UPDATE OR DELETE ON {CORE}.geography_unit_revision
            FOR EACH ROW
            EXECUTE FUNCTION {CORE}.reject_published_revision_change();
        """
    )

    # -- The cached current-state columns -----------------------------------
    #
    # Kept for speed, and now explicitly non-historical. Anything asking what a
    # unit looked like under a particular version must read a revision.
    for column, note in (
        ("raw_name", "name"),
        ("normalised_name", "normalised name"),
        ("parent_id", "parent"),
        ("depth", "depth"),
        ("path", "materialised path"),
        ("is_active", "active state"),
        ("boundary_version_id", "boundary version"),
    ):
        op.execute(
            f"COMMENT ON COLUMN {CORE}.geography_unit.{column} IS "
            f"'Cache of the currently published revision''s {note}. NOT "
            f"historical: a later import overwrites it. Query "
            f"geography_unit_revision for what any given boundary version said.'"
        )
    op.execute(
        f"COMMENT ON TABLE {CORE}.geography_unit IS "
        "'Stable geographic identity. The UUID facilities, user scopes and "
        "encounters reference; it survives every boundary recut. The columns "
        "here cache the currently published revision - history lives in "
        "geography_unit_revision.'"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS geography_unit_revision_immutable "
        f"ON {CORE}.geography_unit_revision"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {CORE}.reject_published_revision_change()")

    op.drop_constraint(
        "uq_geography_unit_geometry_unit_version",
        "geography_unit_geometry",
        schema=CORE,
        type_="unique",
    )
    # Restoring uniqueness on the unit alone can only succeed if no unit carries
    # geometry under two versions. That is the state before this migration, so a
    # downgrade after a second import fails loudly rather than deleting
    # somebody's geometry to make room.
    op.create_unique_constraint(
        "uq_geography_unit_geometry_geography_unit_id",
        "geography_unit_geometry",
        ["geography_unit_id"],
        schema=CORE,
    )

    op.drop_index("ix_geography_unit_geometry_version", "geography_unit_geometry", schema=CORE)
    op.drop_constraint(
        op.f("fk_geography_unit_geometry_boundary_version_id_boundary_version"),
        "geography_unit_geometry",
        schema=CORE,
        type_="foreignkey",
    )
    op.drop_column("geography_unit_geometry", "boundary_version_id", schema=CORE)

    op.drop_table("geography_unit_revision", schema=CORE)

    op.execute(f"COMMENT ON TABLE {CORE}.geography_unit IS NULL")
    for column in (
        "raw_name",
        "normalised_name",
        "parent_id",
        "depth",
        "path",
        "is_active",
        "boundary_version_id",
    ):
        op.execute(f"COMMENT ON COLUMN {CORE}.geography_unit.{column} IS NULL")
