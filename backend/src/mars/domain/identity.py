"""The identity vault: ``mars_identity``.

The only tables in MARS that hold a patient's name, national identifier or
phone number. Everything else works with a pseudonymous ``patient_reference_id``
from ``mars_core``.

The separation is enforced below the application as well as within it. The
database role the API and analytics run as has **no privileges on this schema at
all** - not even ``USAGE`` - so a query naming one of these tables fails at parse
time. An SQL injection in an ordinary endpoint cannot reach identity, because
the connection it runs on has no path to it.

**Nothing clinical belongs here.** The vault knows *who*; ``mars_core`` knows
*what happened*. A vault row carrying a diagnosis would mean one compromised
query returned both a name and a health condition, which is exactly what the
separation exists to prevent.

Next of kin (OPD 002 column 8) is stored **nowhere** - not here either.
Surveillance has no purpose for a third party's contact details, and this vault
links patients to themselves rather than serving as a contact book.

See ``docs/security/identity-vault.md`` for the threat review.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import IDENTITY
from mars.domain.enums import (
    IdentifierType,
    LinkageConfidence,
    ReidentificationOutcome,
)


class IdentityRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person, with the identifying detail the register carried.

    Points *out* to ``mars_core.patient_reference`` by id, with no foreign key.
    The absence is deliberate: a foreign key would require this schema's role to
    hold privileges on ``mars_core`` and would let a join cross the boundary in
    one query. Referential integrity across the boundary is the identity
    service's job, and it is the only thing that ever holds both halves.
    """

    __tablename__ = "identity_record"
    __table_args__ = (
        UniqueConstraint("patient_reference_id", name="uq_identity_record_reference"),
        Index("ix_identity_record_reference", "patient_reference_id"),
        {
            "schema": IDENTITY,
            "comment": (
                "Direct patient identity, encrypted at rest. No clinical data "
                "may be stored here: the vault knows who, mars_core knows what "
                "happened."
            ),
        },
    )

    #: The ``mars_core.patient_reference`` this identity resolves to. Not a
    #: foreign key, on purpose - see the class docstring.
    patient_reference_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # Every identifying field below is AES-256-GCM ciphertext, encrypted in the
    # application before it reaches PostgreSQL. Schema separation keeps identity
    # away from the application; encryption keeps it away from a backup, a
    # replica, a stolen disk and a superuser session.
    #
    # The associated data binds each ciphertext to this table, this column and
    # this patient reference, so a value moved between columns or between rows
    # fails to decrypt rather than silently misattributing a name.
    #
    # A null stays null. "No phone number was recorded" and "an empty phone
    # number was recorded" are different facts, and only the first is one the
    # register can express.
    surname_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    given_name_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    phone_contact_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    #: OPD 002 records an age, not a date of birth, so this stays null unless a
    #: source supplies one. Never derived from age: a derived date of birth is a
    #: fabricated fact that would then be used for linkage. Stored as ciphertext
    #: of an ISO date string rather than a DATE column, because a plaintext date
    #: of birth beside a village is close to a name.
    date_of_birth_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    #: Which key version wrote this row's ciphertext. Rotation re-encrypts and
    #: updates it; until then, the retired key decrypts.
    encryption_key_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

    identifiers: Mapped[list[IdentityIdentifier]] = relationship(
        back_populates="identity", cascade="all, delete-orphan"
    )


class IdentityIdentifier(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One identifier belonging to one person.

    The raw value is kept as ciphertext, because an authorised, audited
    disclosure has to return the identifier the clerk actually wrote.

    The **normalised** value is deliberately not stored at all. It is a pure
    function of the raw value and the normalisation rules, so a second copy -
    even encrypted - would widen exposure for nothing. Diagnosing a
    mis-normalisation means re-normalising the decrypted raw value under the old
    rules and the new, which needs no stored copy.

    The linkage token sits beside the ciphertext so a match can be made *without
    decrypting anything*. An ingestion run derives a token, looks it up here, and
    never needs the identifier or the key that protects it.
    """

    __tablename__ = "identity_identifier"
    __table_args__ = (
        # One person may hold a national ID and a passport, but not two national
        # IDs: a second one means a data-quality problem that must surface
        # rather than silently attach.
        UniqueConstraint(
            "identity_record_id",
            "identifier_type",
            name="uq_identity_identifier_one_per_type",
        ),
        # The token is what ingestion looks up, so it is indexed and unique
        # within a key version. Two people sharing a token under one version
        # would mean an HMAC collision or a mis-assignment; either is a fault,
        # not a state to tolerate.
        UniqueConstraint(
            "linkage_token",
            "linkage_key_version",
            name="uq_identity_identifier_token_version",
        ),
        CheckConstraint("length(linkage_token) = 64", name="linkage_token_is_sha256_hex"),
        CheckConstraint("octet_length(raw_value_encrypted) > 0", name="raw_value_is_ciphertext"),
        Index("ix_identity_identifier_token", "linkage_token"),
        Index("ix_identity_identifier_record", "identity_record_id"),
        {
            "schema": IDENTITY,
            "comment": (
                "Patient identifiers, encrypted, beside the linkage token "
                "derived from them. The token is a MAC under a separate key: it "
                "cannot be reversed to the identifier, and matching never "
                "decrypts anything."
            ),
        },
    )

    identity_record_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{IDENTITY}.identity_record.id", ondelete="CASCADE"),
        nullable=False,
    )

    identifier_type: Mapped[IdentifierType] = mapped_column(
        pg_enum(IdentifierType, name="identifier_type", schema=IDENTITY), nullable=False
    )

    #: AES-256-GCM ciphertext. The associated data binds it to this table, this
    #: column and this row's linkage token, so a ciphertext copied onto another
    #: identifier fails to decrypt.
    raw_value_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: The key version this ciphertext was written under.
    encryption_key_version: Mapped[str] = mapped_column(String(16), nullable=False)

    #: HMAC-SHA256 hex. 64 characters, enforced.
    linkage_token: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Which key derived it. Rotation adds a version rather than rewriting
    #: history, so an existing link stays valid and comparable.
    linkage_key_version: Mapped[str] = mapped_column(String(16), nullable=False)

    confidence: Mapped[LinkageConfidence] = mapped_column(
        pg_enum(LinkageConfidence, name="linkage_confidence", schema=IDENTITY),
        nullable=False,
        default=LinkageConfidence.DETERMINISTIC_IDENTIFIER,
    )

    identity: Mapped[IdentityRecord] = relationship(back_populates="identifiers")


class ReidentificationEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every attempt to turn a pseudonymous reference back into a person.

    Written whether the attempt succeeded, was refused, or found nothing. The
    row records **who asked, about which reference, why, and what happened** -
    and deliberately records **no identifier and no name**. An audit trail that
    stored what was disclosed would become a second copy of the vault, kept
    somewhere with weaker controls and read by more people.

    Kept here rather than in ``mars_audit`` because the fact that a particular
    reference was re-identified is itself sensitive, and ``mars_audit`` is
    readable by roles that must not learn it. The general audit trail records
    that a re-identification occurred; this records the detail.
    """

    __tablename__ = "reidentification_event"
    __table_args__ = (
        CheckConstraint("length(btrim(reason)) > 0", name="reason_is_stated"),
        Index("ix_reidentification_event_actor", "actor_user_id", "requested_at"),
        Index("ix_reidentification_event_reference", "patient_reference_id"),
        Index("ix_reidentification_event_outcome", "outcome"),
        {
            "schema": IDENTITY,
            "comment": (
                "Append-only. Re-identification attempts: who asked, about "
                "which reference, why, and with what outcome. Never the "
                "identifier or name that was disclosed."
            ),
        },
    )

    actor_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    actor_label: Mapped[str] = mapped_column(String(160), nullable=False)
    session_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    patient_reference_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    #: Required and non-empty, enforced by the database. A re-identification
    #: without a stated purpose is not reviewable after the fact.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    outcome: Mapped[ReidentificationOutcome] = mapped_column(
        pg_enum(ReidentificationOutcome, name="reidentification_outcome", schema=IDENTITY),
        nullable=False,
    )

    requested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "IdentityIdentifier",
    "IdentityRecord",
    "ReidentificationEvent",
]
