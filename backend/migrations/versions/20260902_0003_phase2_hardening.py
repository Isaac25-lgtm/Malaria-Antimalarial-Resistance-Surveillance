"""Phase 2 hardening: PostGIS geometry and hierarchy-cycle guards.

Revision ID: 0003_phase2_hardening
Revises: 0002_core_domain
Created: 2026-09-02

The raw boundary files remain immutable. These columns hold the validated,
full-resolution analytical geometry and its simplified web representation.
Both are MultiPolygon/EPSG:4326 so the importer has one stable contract.

The hierarchy triggers complement the direct self-parent checks by rejecting
longer cycles such as A -> B -> A. A bounded reader avoids a hung request, but
only a write-time guard prevents corrupt hierarchy data from being stored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision: str = "0003_phase2_hardening"
down_revision: str | None = "0002_core_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


HIERARCHY_CYCLE_FUNCTION = """
CREATE OR REPLACE FUNCTION mars_core.reject_hierarchy_cycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    creates_cycle boolean;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION '% cannot be its own parent', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    EXECUTE format(
        'WITH RECURSIVE ancestors(id, parent_id) AS (
             SELECT id, parent_id FROM mars_core.%I WHERE id = $1
             UNION
             SELECT node.id, node.parent_id
             FROM mars_core.%I AS node
             JOIN ancestors ON node.id = ancestors.parent_id
         )
         SELECT EXISTS (SELECT 1 FROM ancestors WHERE id = $2)',
        TG_TABLE_NAME,
        TG_TABLE_NAME
    )
    INTO creates_cycle
    USING NEW.parent_id, NEW.id;

    IF creates_cycle THEN
        RAISE EXCEPTION 'parent assignment creates a cycle in %', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    # Geometry columns and GIST indexes require PostGIS. The Compose runtime
    # provisions the extension-capable image; this makes the requirement
    # explicit in the migration rather than postponing it to an importer.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.add_column(
        "geography_unit_geometry",
        sa.Column(
            "geom",
            Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
        schema="mars_core",
    )
    op.add_column(
        "geography_unit_geometry",
        sa.Column(
            "geom_web",
            Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_geometry_geom",
        "geography_unit_geometry",
        ["geom"],
        unique=False,
        schema="mars_core",
        postgresql_using="gist",
    )
    op.create_index(
        "ix_geography_unit_geometry_geom_web",
        "geography_unit_geometry",
        ["geom_web"],
        unique=False,
        schema="mars_core",
        postgresql_using="gist",
    )

    op.execute(HIERARCHY_CYCLE_FUNCTION)
    op.execute(
        "CREATE TRIGGER geography_unit_reject_cycle "
        "BEFORE INSERT OR UPDATE OF parent_id ON mars_core.geography_unit "
        "FOR EACH ROW EXECUTE FUNCTION mars_core.reject_hierarchy_cycle()"
    )
    op.execute(
        "CREATE TRIGGER organisation_unit_reject_cycle "
        "BEFORE INSERT OR UPDATE OF parent_id ON mars_core.organisation_unit "
        "FOR EACH ROW EXECUTE FUNCTION mars_core.reject_hierarchy_cycle()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS organisation_unit_reject_cycle ON mars_core.organisation_unit"
    )
    op.execute("DROP TRIGGER IF EXISTS geography_unit_reject_cycle ON mars_core.geography_unit")
    op.execute("DROP FUNCTION IF EXISTS mars_core.reject_hierarchy_cycle()")

    op.drop_index(
        "ix_geography_unit_geometry_geom_web",
        table_name="geography_unit_geometry",
        schema="mars_core",
    )
    op.drop_index(
        "ix_geography_unit_geometry_geom",
        table_name="geography_unit_geometry",
        schema="mars_core",
    )
    op.drop_column("geography_unit_geometry", "geom_web", schema="mars_core")
    op.drop_column("geography_unit_geometry", "geom", schema="mars_core")
    # PostGIS is shared infrastructure and may be used by other schemas. Never
    # remove the extension during an application downgrade.
