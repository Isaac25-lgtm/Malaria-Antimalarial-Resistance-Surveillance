"""Health-sector organisation units and facilities.

The organisational hierarchy is modelled separately from administrative
geography. A Health Sub-District is a health-sector management unit and is *not*
assumed to be equivalent to a county: where a correspondence exists it is
recorded as data (``primary_geography_unit_id``), never inferred from the level.

No facility master and no facility coordinates have been supplied. This module
defines the shape; it does not populate it. Synthetic facilities exist only in
tests and in explicitly labelled development fixtures.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
from mars.domain.enums import FacilityLevel, FacilityOwnership, OrganisationUnitType


class OrganisationUnit(UUIDPrimaryKeyMixin, TimestampMixin, SourceProvenanceMixin, Base):
    """A node in the health-sector management hierarchy.

    Typical shape: national -> district health office -> health sub-district ->
    facility. The hierarchy is data, not a hard-coded chain, because it varies
    and because regional referral structures cut across it.
    """

    __tablename__ = "organisation_unit"
    __table_args__ = (
        UniqueConstraint("code", name="uq_organisation_unit_code"),
        CheckConstraint("id <> parent_id", name="no_self_parent"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        CheckConstraint("depth >= 0 AND depth <= 8", name="depth_within_hierarchy"),
        Index("ix_organisation_unit_parent_id", "parent_id"),
        Index("ix_organisation_unit_unit_type", "unit_type"),
        Index("ix_organisation_unit_geography", "primary_geography_unit_id"),
        Index("ix_organisation_unit_effective", "effective_from", "effective_to"),
        {"schema": CORE},
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalised_name: Mapped[str] = mapped_column(String(255), nullable=False)

    unit_type: Mapped[OrganisationUnitType] = mapped_column(
        pg_enum(OrganisationUnitType, name="organisation_unit_type", schema=CORE),
        nullable=False,
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.organisation_unit.id", ondelete="RESTRICT"),
        nullable=True,
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: The administrative area this organisation unit is responsible for, where
    #: one applies. Recorded as an explicit link so that a Health Sub-District is
    #: never presumed to coincide with a county boundary.
    primary_geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="SET NULL"),
        nullable=True,
    )

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent: Mapped[OrganisationUnit | None] = relationship(
        remote_side="OrganisationUnit.id", back_populates="children"
    )
    children: Mapped[list[OrganisationUnit]] = relationship(back_populates="parent")
    facilities: Mapped[list[Facility]] = relationship(back_populates="organisation_unit")


class Facility(UUIDPrimaryKeyMixin, TimestampMixin, SourceProvenanceMixin, Base):
    """A health facility.

    Coordinates are nullable and carry their own validation state. A facility is
    never placed approximately: an unvalidated coordinate is stored as absent,
    and the map omits the point rather than showing it in the wrong place.
    """

    __tablename__ = "facility"
    __table_args__ = (
        UniqueConstraint("code", name="uq_facility_code"),
        CheckConstraint(
            "closed_on IS NULL OR opened_on IS NULL OR closed_on >= opened_on",
            name="operating_range_ordered",
        ),
        CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="coordinates_paired",
        ),
        CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name="latitude_in_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name="longitude_in_range",
        ),
        Index("ix_facility_organisation_unit_id", "organisation_unit_id"),
        Index("ix_facility_district_geography_unit_id", "district_geography_unit_id"),
        Index("ix_facility_subcounty_geography_unit_id", "subcounty_geography_unit_id"),
        Index("ix_facility_level", "facility_level"),
        Index("ix_facility_is_active", "is_active"),
        Index("ix_facility_normalised_name", "normalised_name"),
        {"schema": CORE},
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalised_name: Mapped[str] = mapped_column(String(255), nullable=False)

    facility_level: Mapped[FacilityLevel] = mapped_column(
        pg_enum(FacilityLevel, name="facility_level", schema=CORE),
        nullable=False,
        default=FacilityLevel.UNKNOWN,
    )
    ownership: Mapped[FacilityOwnership] = mapped_column(
        pg_enum(FacilityOwnership, name="facility_ownership", schema=CORE),
        nullable=False,
        default=FacilityOwnership.UNKNOWN,
    )

    organisation_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.organisation_unit.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Denormalised district and subcounty links. Geography scoping filters on
    #: the district link directly, which keeps the common authorisation query a
    #: single indexed predicate rather than a recursive hierarchy walk.
    district_geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subcounty_geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="RESTRICT"),
        nullable=True,
    )

    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: How the coordinate was established and checked. Null coordinates have a
    #: null state; a coordinate is never stored without one.
    coordinate_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coordinate_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    opened_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: True when the record exists only to support development or testing. Set
    #: on every fixture facility so a synthetic record can never be mistaken for
    #: an official one.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    organisation_unit: Mapped[OrganisationUnit | None] = relationship(back_populates="facilities")
    identifiers: Mapped[list[FacilityIdentifier]] = relationship(
        back_populates="facility", cascade="all, delete-orphan"
    )


class FacilityIdentifier(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An external system's identifier for a facility.

    Each source system may hold at most one active identifier per facility, and
    an identifier value is unique within its source system for any overlapping
    validity period.
    """

    __tablename__ = "facility_identifier"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "external_id",
            name="uq_facility_identifier_source_system_external_id",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        Index("ix_facility_identifier_facility_id", "facility_id"),
        Index("ix_facility_identifier_source", "source_system", "external_id"),
        {"schema": CORE},
    )

    facility_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.facility.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    facility: Mapped[Facility] = relationship(back_populates="identifiers")
