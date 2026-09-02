"""Administrative geography.

Design constraints, all from the reconnaissance of the supplied boundary files
and from blueprint sections 024 and 139:

* Internal identity is a UUID. ``FScode`` is a source code recorded as an alias,
  not a primary key, pending authoritative confirmation from the Ministry or
  UBOS. ``FID`` and ``OBJECTID`` are never durable identifiers - the supplied
  subcounty layer contains a duplicated ``OBJECTID``.
* Raw names are preserved exactly; a separately maintained normalised name
  supports lookup. The supplied data contains double spaces and parenthetical
  aliases, and those defects are recorded rather than edited away.
* Subcounty names repeat across districts - ``CENTRAL DIVISION`` occurs twelve
  times - so uniqueness is asserted on (parent, normalised name), never on name
  alone.
* All seven levels exist. Parish and village remain empty because no parish or
  village boundary data has been supplied.
* Boundaries are versioned with effective dates so a later re-cut of Uganda's
  districts cannot silently rewrite historical analysis.

No geometry is imported here. Prompt 5 owns the importer; this module owns the
shape the importer will write into.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import (
    Base,
    SourceProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)
from mars.db.schemas import CORE
from mars.domain.enums import (
    AliasMatchStatus,
    BoundaryImportStatus,
    GeographyLevel,
    GeographyUnitKind,
    GeometryValidityState,
)

_level_enum = pg_enum(GeographyLevel, name="geography_level", schema=CORE)


class BoundaryVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A dated release of an administrative boundary dataset.

    One row per source file per import. The checksum ties a version to the exact
    bytes it came from, so a claim about geography can be re-verified rather than
    trusted.
    """

    __tablename__ = "boundary_version"
    __table_args__ = (
        UniqueConstraint("code", name="uq_boundary_version_code"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        Index("ix_boundary_version_status", "import_status"),
        # Both created by migration 0004 and declared here so autogenerate does
        # not propose dropping them. Re-import detection is a checksum lookup on
        # every import, and the partial unique index is what guarantees at most
        # one published version - the constraint that lets every geography unit
        # belong to one unambiguous hierarchy.
        Index("ix_boundary_version_source_checksum", "source_checksum"),
        Index(
            "uq_boundary_version_single_published",
            "import_status",
            unique=True,
            postgresql_where=text("import_status = 'published'"),
        ),
        {"schema": CORE},
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: SHA-256 of the source file, matching data/manifests/geography.sha256.
    source_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_retrieved_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: EPSG code of the source geometry. The supplied GeoJSON declares none and
    #: therefore defaults to 4326 under RFC 7946; the Esri twin declares 4326
    #: explicitly. Recorded, never assumed silently.
    source_crs: Mapped[str | None] = mapped_column(String(32), nullable=True)
    storage_crs: Mapped[str] = mapped_column(String(32), nullable=False, default="EPSG:4326")

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    import_status: Mapped[BoundaryImportStatus] = mapped_column(
        pg_enum(BoundaryImportStatus, name="boundary_import_status", schema=CORE),
        nullable=False,
        default=BoundaryImportStatus.REGISTERED,
    )
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Structured outcome of import validation: feature counts, geometry
    #: defects found, duplicate codes, parent-consistency results, area control
    #: totals. Written by the Prompt 5 importer.
    validation_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    lineage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    units: Mapped[list[GeographyUnit]] = relationship(back_populates="boundary_version")


#: Carried by every cached column on GeographyUnit. The columns are fast to
#: read and structurally incapable of answering a historical question, so
#: the warning travels with them into psql, into a schema dump, and into
#: whatever tool an analyst points at the database.
_CACHE_NOTE = (
    "Cache of the currently published revision's {}. NOT historical: a "
    "later import overwrites it. Query geography_unit_revision for what "
    "any given boundary version said."
)


class GeographyUnit(UUIDPrimaryKeyMixin, TimestampMixin, SourceProvenanceMixin, Base):
    """A single administrative area at any level of the hierarchy."""

    __tablename__ = "geography_unit"
    __table_args__ = (
        UniqueConstraint("level", "preferred_code", name="uq_geography_unit_level_preferred_code"),
        UniqueConstraint(
            "parent_id",
            "level",
            "normalised_name",
            name="uq_geography_unit_parent_id_level_normalised_name",
        ),
        CheckConstraint("id <> parent_id", name="no_self_parent"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        CheckConstraint("depth >= 0 AND depth <= 6", name="depth_within_hierarchy"),
        CheckConstraint(
            "(level = 'country' AND parent_id IS NULL) OR "
            "(level <> 'country' AND parent_id IS NOT NULL)",
            name="only_country_is_rootless",
        ),
        Index("ix_geography_unit_parent_id", "parent_id"),
        Index("ix_geography_unit_level", "level"),
        Index("ix_geography_unit_normalised_name", "normalised_name"),
        Index("ix_geography_unit_boundary_version_id", "boundary_version_id"),
        Index("ix_geography_unit_effective", "effective_from", "effective_to"),
        Index("ix_geography_unit_is_active", "is_active"),
        {
            "schema": CORE,
            "comment": (
                "Stable geographic identity. The UUID facilities, user scopes "
                "and encounters reference; it survives every boundary recut. "
                "The columns here cache the currently published revision - "
                "history lives in geography_unit_revision."
            ),
        },
    )

    level: Mapped[GeographyLevel] = mapped_column(_level_enum, nullable=False)
    unit_kind: Mapped[GeographyUnitKind] = mapped_column(
        pg_enum(GeographyUnitKind, name="geography_unit_kind", schema=CORE),
        nullable=False,
        default=GeographyUnitKind.UNSPECIFIED,
    )

    #: The code MARS presents and joins on. Derived from the source code during
    #: import (for the supplied files, from FScode) but owned by MARS: an
    #: authoritative national code, when supplied, replaces the value here while
    #: the internal UUID stays fixed.
    preferred_code: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The name exactly as supplied, including any defect.
    raw_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment=_CACHE_NOTE.format("name"),
    )
    #: Uppercased, whitespace-collapsed, punctuation-normalised. For lookup only;
    #: never displayed in preference to the raw name.
    normalised_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment=_CACHE_NOTE.format("normalised name"),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="RESTRICT"),
        nullable=True,
        comment=_CACHE_NOTE.format("parent"),
    )
    #: Denormalised hierarchy depth, kept consistent by the service layer. Makes
    #: level-bounded traversal cheap without a recursive query.
    depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=_CACHE_NOTE.format("depth"),
    )
    #: Materialised ancestor path of preferred codes, e.g. "UG/3/314".
    path: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment=_CACHE_NOTE.format("materialised path"),
    )

    boundary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.boundary_version.id", ondelete="RESTRICT"),
        nullable=True,
        comment=_CACHE_NOTE.format("boundary version"),
    )

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment=_CACHE_NOTE.format("active state"),
    )

    parent: Mapped[GeographyUnit | None] = relationship(
        remote_side="GeographyUnit.id", back_populates="children"
    )
    children: Mapped[list[GeographyUnit]] = relationship(back_populates="parent")
    boundary_version: Mapped[BoundaryVersion | None] = relationship(back_populates="units")
    revisions: Mapped[list[GeographyUnitRevision]] = relationship(
        back_populates="unit", cascade="all, delete-orphan"
    )
    aliases: Mapped[list[GeographyUnitAlias]] = relationship(
        back_populates="geography_unit", cascade="all, delete-orphan"
    )
    geometry: Mapped[GeographyUnitGeometry | None] = relationship(
        back_populates="geography_unit",
        cascade="all, delete-orphan",
        uselist=False,
    )


class GeographyUnitRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One geography unit as one boundary version described it.

    The historical record. ``GeographyUnit`` is a stable identity - a UUID that
    facilities, user scopes and encounters point at, and that must survive every
    recut. Everything a recut can *change* lives here, once per boundary version.

    Before this table existed, a second import overwrote the first: names,
    parents, paths and active state were updated in place on the stable row, so
    the previous ``BoundaryVersion`` remained as metadata describing boundaries
    nothing could reconstruct. An analysis pinned to last year's hierarchy could
    not be reproduced, and nobody would have noticed until they tried.

    **Immutable once its boundary version is published.** A trigger rejects
    UPDATE and DELETE on a revision belonging to a published version, so history
    cannot be quietly rewritten by a later import - which is precisely what was
    happening.

    A unit absent from a newer version simply has no revision under it, or has
    one with ``is_present`` false. Either way its earlier revisions are
    untouched, and a query pinned to the earlier version still returns it.
    """

    __tablename__ = "geography_unit_revision"
    __table_args__ = (
        # One description per unit per version. This is the whole point: the
        # same stable unit may be described differently by two versions, and
        # both descriptions survive.
        UniqueConstraint(
            "geography_unit_id",
            "boundary_version_id",
            name="uq_geography_unit_revision_unit_version",
        ),
        # A code is unique within a level *within a version*, not globally: a
        # recut may reassign a code, and both assignments are historical fact.
        UniqueConstraint(
            "boundary_version_id",
            "level",
            "preferred_code",
            name="uq_geography_unit_revision_version_level_code",
        ),
        Index("ix_geography_unit_revision_version", "boundary_version_id"),
        Index("ix_geography_unit_revision_unit", "geography_unit_id"),
        Index("ix_geography_unit_revision_parent", "parent_revision_id"),
        Index("ix_geography_unit_revision_path", "boundary_version_id", "path"),
        {
            "schema": CORE,
            "comment": (
                "One geography unit as one boundary version described it. "
                "Immutable once that version is published: a later recut adds "
                "revisions, it never rewrites them."
            ),
        },
    )

    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="CASCADE"),
        nullable=False,
    )
    boundary_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.boundary_version.id", ondelete="CASCADE"),
        nullable=False,
    )

    # -- Everything a recut can change ------------------------------------
    level: Mapped[GeographyLevel] = mapped_column(
        pg_enum(GeographyLevel, name="geography_level", schema=CORE), nullable=False
    )
    unit_kind: Mapped[GeographyUnitKind] = mapped_column(
        pg_enum(GeographyUnitKind, name="geography_unit_kind", schema=CORE),
        nullable=False,
        default=GeographyUnitKind.UNSPECIFIED,
    )
    preferred_code: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalised_name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: The parent *within this version*, as a revision rather than a unit: a
    #: recut may re-parent a subcounty, and pointing at the stable unit would
    #: lose which parent it had at the time.
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        # Named explicitly. The convention would build
        # "fk_geography_unit_revision_parent_revision_id_geography_unit_revision",
        # 69 characters, which PostgreSQL silently truncates to 63 - leaving the
        # model and the database disagreeing about the constraint's name.
        ForeignKey(
            f"{CORE}.geography_unit_revision.id",
            ondelete="SET NULL",
            name="fk_geo_revision_parent_revision",
        ),
        nullable=True,
    )

    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: Whether this version contains the unit at all. False records that a
    #: version dropped it, without touching what earlier versions said.
    is_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    unit: Mapped[GeographyUnit] = relationship(back_populates="revisions")
    boundary_version: Mapped[BoundaryVersion] = relationship()
    parent_revision: Mapped[GeographyUnitRevision | None] = relationship(
        remote_side="GeographyUnitRevision.id"
    )


class GeographyUnitAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A source system's identifier for a MARS geography unit.

    This is the crosswalk that keeps ``FScode`` useful without making it load
    bearing. DHIS2 organisation unit UIDs, UBOS codes and HMIS codes land here
    alongside it as they become available.

    A mapping starts as ``proposed``. Blueprint appendix 120: unresolved or
    ambiguous source text stays unresolved; nothing is silently promoted.
    """

    __tablename__ = "geography_unit_alias"
    __table_args__ = (
        # Shortened by hand: the naming convention would generate a
        # 67-character identifier, above PostgreSQL's 63-character limit.
        UniqueConstraint(
            "source_system",
            "source_code",
            "geography_unit_id",
            name="uq_geography_unit_alias_source_and_unit",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        Index("ix_geography_unit_alias_source", "source_system", "source_code"),
        Index("ix_geography_unit_alias_geography_unit_id", "geography_unit_id"),
        Index("ix_geography_unit_alias_match_status", "match_status"),
        {"schema": CORE},
    )

    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: e.g. "ubos_fscode", "dhis2", "hmis105", "opd002_text".
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_code: Mapped[str] = mapped_column(String(128), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_level: Mapped[str | None] = mapped_column(String(32), nullable=True)

    match_status: Mapped[AliasMatchStatus] = mapped_column(
        pg_enum(AliasMatchStatus, name="alias_match_status", schema=CORE),
        nullable=False,
        default=AliasMatchStatus.PROPOSED,
    )
    #: How the mapping was established: "source_code_derivation", "exact_name",
    #: "manual_review". Never "fuzzy" without an explicit review decision.
    match_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    geography_unit: Mapped[GeographyUnit] = relationship(back_populates="aliases")


class GeographyUnitGeometry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Geometry for a geography unit.

    Three representations, deliberately separated:

    ``geom``        Authoritative geometry as supplied, in EPSG:4326. Never
                    altered - defects are recorded, not edited.
    ``geom_web``    Topology-preserving simplification for browser rendering.
                    The subcounty layer carries 1.67 million vertices; the raw
                    geometry must never reach a client.
    ``area_sq_km``  Measured server-side on the geography type. The supplied
                    ``Shape_Area`` attribute is wrong on four subcounties and is
                    not trusted.

    The PostGIS columns are nullable until Prompt 5 validates and imports the
    supplied source boundaries. This model fixes the storage contract now so
    that the importer is additive rather than a redesign.
    """

    __tablename__ = "geography_unit_geometry"
    __table_args__ = (
        # Keyed by unit *and* version. It was keyed by unit alone, so a recut
        # overwrote the previous geometry in place and the earlier boundary
        # version described shapes that no longer existed anywhere.
        UniqueConstraint(
            "geography_unit_id",
            "boundary_version_id",
            name="uq_geography_unit_geometry_unit_version",
        ),
        Index("ix_geography_unit_geometry_validity", "validity_state"),
        Index("ix_geography_unit_geometry_version", "boundary_version_id"),
        Index("ix_geography_unit_geometry_geom", "geom", postgresql_using="gist"),
        Index("ix_geography_unit_geometry_geom_web", "geom_web", postgresql_using="gist"),
        {"schema": CORE},
    )

    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Which boundary version drew this shape. Nullable only so the corrective
    #: migration can backfill; every row written since carries it.
    boundary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.boundary_version.id", ondelete="CASCADE"),
        nullable=True,
    )

    validity_state: Mapped[GeometryValidityState] = mapped_column(
        pg_enum(GeometryValidityState, name="geometry_validity_state", schema=CORE),
        nullable=False,
        default=GeometryValidityState.NOT_ASSESSED,
    )

    # Polygon inputs are promoted to MultiPolygon during import so every level
    # has one stable database type. Raw source bytes remain immutable outside
    # the database; ``geom`` is the validated full-resolution analytical copy.
    geom: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    geom_web: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )

    #: What was found in the raw geometry: unclosed rings, degenerate slivers,
    #: self-intersections, duplicate vertices. Recorded per unit so a map can
    #: explain why a boundary was repaired.
    validity_issues: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    repair_method: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ring_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vertex_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    part_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Measured server-side, not read from the source attribute table.
    area_sq_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    perimeter_km: Mapped[float | None] = mapped_column(Float, nullable=True)

    bbox_min_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_min_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_max_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_max_lat: Mapped[float | None] = mapped_column(Float, nullable=True)

    simplification_tolerance_deg: Mapped[float | None] = mapped_column(Float, nullable=True)

    geography_unit: Mapped[GeographyUnit] = relationship(back_populates="geometry")
