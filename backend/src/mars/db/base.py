"""Declarative base and shared column conventions.

Blueprint appendix 159: snake_case tables and columns, UUID internal keys,
source identifiers kept separately, ``*_at`` timestamps, ``*_id`` foreign keys,
and no overloaded generic ``status`` field.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, String, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names keep Alembic autogenerate diffs stable and make
# a failing constraint identifiable from the error message alone.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base for every MARS ORM model."""

    metadata = metadata_obj

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    """Stable internal identifier.

    Every entity is keyed by an application-generated UUID. Source system
    identifiers - FID, OBJECTID, DHIS2 UID, facility code - are stored in their
    own columns and are never promoted to a primary key, because they change
    when the source changes.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Creation and modification instants, always timezone-aware UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SourceProvenanceMixin:
    """Where this record came from.

    Kept as separate columns rather than folded into the natural key so that a
    record can be traced to its origin without that origin becoming load-bearing
    for referential integrity.
    """

    source_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


def utc_column(**kwargs: Any) -> Mapped[datetime]:
    """Helper for additional timezone-aware timestamp columns."""
    return mapped_column(DateTime(timezone=True), **kwargs)


def pg_enum(
    enum_class: type[enum.Enum],
    *,
    name: str,
    schema: str,
) -> SAEnum:
    """Build a native PostgreSQL enum that stores the member *value*.

    SQLAlchemy stores the member *name* by default. MARS stores the value, so
    that what is in the database, what the API returns and what the TypeScript
    contract declares are the same lowercase string. Without this the three
    drift apart silently and only a manual query reveals it.

    Integer-valued enums fall back to the lowercased member name, since an
    integer is not a usable PostgreSQL enum label.
    """

    def _labels(cls: type[enum.Enum]) -> Sequence[str]:
        return [
            member.value if isinstance(member.value, str) else member.name.lower() for member in cls
        ]

    return SAEnum(
        enum_class,
        name=name,
        schema=schema,
        native_enum=True,
        create_type=False,
        values_callable=_labels,
        validate_strings=True,
    )


def enum_labels(enum_class: type[enum.Enum]) -> list[str]:
    """The PostgreSQL labels for an enum, in declaration order.

    Used by migrations so the type definition and the model cannot disagree.
    """
    return [
        member.value if isinstance(member.value, str) else member.name.lower()
        for member in enum_class
    ]
