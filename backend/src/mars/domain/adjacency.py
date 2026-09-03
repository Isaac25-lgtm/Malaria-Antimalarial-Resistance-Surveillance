"""Which administrative areas touch which.

A fact about a boundary version, not about malaria, so it lives in
``mars_core`` beside the geography it describes. Analytics reads it and never
writes it: adjacency is derived from the supplied boundaries, and letting an
analytical engine invent a neighbour would let it invent a cluster.

Stored rather than computed per query for two reasons. It is expensive - a
polygon-touches-polygon test across every district in the country - and it is
a **versioned** fact: districts split and merge, and a cluster reported under
last year's boundaries must remain readable after this year's are published.

Pairs are stored in both directions. Adjacency is symmetric, and storing it
once would make every query a union of two lookups, which is the kind of thing
that eventually gets one of them wrong.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from mars.db.schemas import CORE

#: How a pair was established. A plain string with a check constraint rather
#: than an enum: one derivation exists today, and a second would be a code
#: change here rather than a type change across three schemas.
SHARED_BOUNDARY = "shared_boundary"


class GeographyAdjacency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One ordered pair of areas that share a boundary."""

    __tablename__ = "geography_adjacency"
    __table_args__ = (
        UniqueConstraint(
            "boundary_version_id",
            "geography_unit_id",
            "neighbour_unit_id",
            name="uq_geography_adjacency_version_pair",
        ),
        # An area is not its own neighbour. Without this a concentration
        # method would compare a district against itself and find it ordinary.
        CheckConstraint(
            "geography_unit_id <> neighbour_unit_id", name="an_area_is_not_its_own_neighbour"
        ),
        CheckConstraint("derivation IN ('shared_boundary')", name="derivation_is_known"),
        Index("ix_geography_adjacency_unit", "geography_unit_id"),
        Index("ix_geography_adjacency_version", "boundary_version_id"),
        {
            "schema": CORE,
            "comment": (
                "Which administrative areas touch which, derived from the "
                "supplied boundaries of one boundary version."
            ),
        },
    )

    boundary_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.boundary_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.geography_unit.id", ondelete="CASCADE", name="fk_geography_adjacency_unit"
        ),
        nullable=False,
    )
    neighbour_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.geography_unit.id",
            ondelete="CASCADE",
            name="fk_geography_adjacency_neighbour",
        ),
        nullable=False,
    )

    #: How the pair was established, so a later derivation from a different
    #: source cannot be mistaken for this one.
    derivation: Mapped[str] = mapped_column(String(32), nullable=False, default=SHARED_BOUNDARY)
    derived_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["SHARED_BOUNDARY", "GeographyAdjacency"]
