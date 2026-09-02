"""The identity service: the only path across the identity boundary.

Two operations with very different characters.

**Linkage** runs during ingestion, thousands of times an hour. It derives a
token from an identifier and finds or records the person it belongs to. It never
decrypts anything, never returns a name, and never needs one: matching is done
on the token alone.

**Re-identification** turns a pseudonymous reference back into a person. It is
rare, deliberate, gated on a permission no role holds by default *and* on a
sensitivity ceiling almost no account has, and every attempt is recorded -
granted, refused or fruitless - with the reason the caller stated and without
the value they were given.

Direct identifiers are AES-256-GCM ciphertext in the database
(:mod:`mars.identity.encryption`), under a key held separately from the HMAC key
that derives linkage tokens. Compromising one does not yield the other: the
linkage key lets you *test* a guessed identifier, the encryption key lets you
read stored ones, and neither substitutes for the other.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mars.core.errors import ValidationFailedError
from mars.core.logging import get_logger
from mars.domain.enums import (
    AuditAction,
    AuditOutcome,
    IdentifierType,
    LinkageConfidence,
    ReidentificationOutcome,
)
from mars.domain.identity import (
    IdentityIdentifier,
    IdentityRecord,
    ReidentificationEvent,
)
from mars.identity.encryption import FieldEncryptor, build_associated_data
from mars.identity.errors import IdentityUnavailableError
from mars.identity.linkage import (
    LinkageKeyMissingError,
    LinkageToken,
    LinkageTokenDeriver,
    UnlinkableIdentifierError,
)
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal

logger = get_logger(__name__)

_RECORD_TABLE: Final = "identity_record"
_IDENTIFIER_TABLE: Final = "identity_identifier"


class RetiredKeyUnavailableError(RuntimeError):
    """A stored token was written under a key that is no longer configured.

    Raised rather than treated as "not found". Silently missing a retired key
    would make an existing patient look new, and the next ingestion run would
    create a second identity for them - splitting one clinical history in two,
    with no error anywhere to explain it.
    """

    def __init__(self, versions: frozenset[str]) -> None:
        self.versions = versions
        super().__init__(
            "Identifier lookup cannot complete: linkage key versions "
            f"{sorted(versions)} are configured, but the vault holds tokens "
            "under other versions. Restore the retired key before ingesting, or "
            "existing patients will be recorded as new people."
        )


@dataclass(frozen=True, slots=True)
class LinkageResult:
    """What linkage established, in terms ``mars_core`` may hold.

    A reference and a confidence. No identifier, no name - so an ingestion
    pipeline can hold this object and log it whole.
    """

    patient_reference_id: uuid.UUID
    confidence: LinkageConfidence
    #: True when this identifier had not been seen before.
    created: bool
    #: True when the match came from a retired key and was re-derived under the
    #: active one. Reported so a rotation's progress is observable.
    rekeyed: bool = False


@dataclass(frozen=True, slots=True)
class DisclosedIdentity:
    """The result of an authorised, audited re-identification.

    Deliberately not a domain object. It is a one-shot disclosure built at the
    moment of release, so it cannot be attached to a session and re-read later
    by something that never passed the gates.
    """

    patient_reference_id: uuid.UUID
    surname: str | None
    given_name: str | None
    phone_contact: str | None
    date_of_birth: str | None
    identifiers: tuple[tuple[str, str], ...]

    def __repr__(self) -> str:
        """Never render what was disclosed.

        Someone was authorised to see a name. That authorisation does not
        extend to a traceback, a debugger or a log line.
        """
        return f"DisclosedIdentity(patient_reference_id={self.patient_reference_id!r}, …)"


class IdentityService:
    """Linkage and re-identification against ``mars_identity``.

    Constructed with a session bound to the **identity** database role. The
    ordinary application role has ``USAGE`` revoked on the schema, so a session
    handed over by mistake fails at the database rather than quietly working.
    """

    def __init__(
        self,
        session: Session,
        deriver: LinkageTokenDeriver,
        encryptor: FieldEncryptor,
        *,
        durable_session_factory: Callable[[], Session] | None = None,
        audit_service: object | None = None,
    ) -> None:
        self._session = session
        self._deriver = deriver
        self._encryptor = encryptor
        #: Records a refusal even though the request that caused it is about to
        #: roll back. A denial that disappears with its own transaction is not
        #: an audit trail.
        self._durable_session_factory = durable_session_factory
        #: The application's audit service, bound to ``mars_audit``. Optional
        #: because linkage does not need it; supplied by the caller that owns
        #: the application session when re-identification is possible.
        self._audit_service = audit_service

    # -- Readiness ---------------------------------------------------------
    @property
    def is_ready(self) -> bool:
        """Whether identity can be written at all.

        Both keys must be present. A deployment missing either reports itself
        unready rather than discovering the problem at the first patient - and
        rather than writing identifiers in plaintext.
        """
        return self._deriver.is_configured and self._encryptor.is_configured

    # -- Linkage -----------------------------------------------------------
    def link(
        self,
        identifier_type: IdentifierType,
        raw_value: str,
        *,
        patient_reference_id: uuid.UUID,
        surname: str | None = None,
        given_name: str | None = None,
        phone_contact: str | None = None,
        date_of_birth: str | None = None,
    ) -> LinkageResult:
        """Find the person this identifier belongs to, or record a new one.

        Searches **every configured key version**, not just the active one, so a
        patient first seen under ``v1`` is still found after rotation to ``v2``.
        A match under a retired key is re-derived under the active key in place,
        which is how a rotation completes without a migration job and without
        ever creating a second identity for the same person.

        Returns a reference and a confidence, never a name: the caller is an
        ingestion pipeline that must not learn who it is ingesting.
        """
        self._require_ready()

        try:
            active = self._deriver.derive(identifier_type, raw_value)
        except UnlinkableIdentifierError:
            # The register row is real and must be kept. Nothing ties it to any
            # other row, and saying so is more useful than inventing a link.
            logger.info(
                "identity_linkage_unusable",
                identifier_type=identifier_type.value,
                patient_reference_id=str(patient_reference_id),
            )
            return LinkageResult(
                patient_reference_id=patient_reference_id,
                confidence=LinkageConfidence.UNLINKED,
                created=False,
            )

        confidence = (
            LinkageConfidence.DETERMINISTIC_UNSPECIFIED_SCHEME
            if identifier_type is IdentifierType.UNSPECIFIED_SCHEME
            else LinkageConfidence.DETERMINISTIC_IDENTIFIER
        )

        found = self._find_identifier(identifier_type, raw_value)
        if found is not None:
            existing, matched_version = found
            record = self._session.get(IdentityRecord, existing.identity_record_id)
            if record is None:  # pragma: no cover - a foreign key guarantees this
                raise RuntimeError("identifier row without an identity record")

            rekeyed = False
            if matched_version != active.key_version:
                rekeyed = self._rekey(existing, active)

            logger.info(
                "identity_linkage_matched",
                identifier_type=identifier_type.value,
                patient_reference_id=str(record.patient_reference_id),
                rekeyed=rekeyed,
            )
            return LinkageResult(
                patient_reference_id=record.patient_reference_id,
                confidence=confidence,
                created=False,
                rekeyed=rekeyed,
            )

        return self._create(
            identifier_type,
            raw_value,
            active_token=active.value,
            active_version=active.key_version,
            confidence=confidence,
            patient_reference_id=patient_reference_id,
            surname=surname,
            given_name=given_name,
            phone_contact=phone_contact,
            date_of_birth=date_of_birth,
        )

    def find_reference(self, identifier_type: IdentifierType, raw_value: str) -> uuid.UUID | None:
        """The reference for an identifier, if one is known.

        A lookup, not a disclosure: the caller already holds the identifier, so
        this reveals nothing they did not bring. Searches every configured key
        version for the same reason ``link`` does.
        """
        self._require_ready()
        found = self._find_identifier(identifier_type, raw_value)
        if found is None:
            return None
        record = self._session.get(IdentityRecord, found[0].identity_record_id)
        return record.patient_reference_id if record else None

    def _find_identifier(
        self, identifier_type: IdentifierType, raw_value: str
    ) -> tuple[IdentityIdentifier, str] | None:
        """Look the identifier up under every key version MARS knows.

        The active version is tried first because it is the common case after a
        rotation settles. A hit under a retired version returns that version, so
        the caller can re-derive.
        """
        versions = self._ordered_versions()
        candidates: dict[str, str] = {}
        for version in versions:
            try:
                candidates[version] = self._deriver.derive(
                    identifier_type, raw_value, key_version=version
                ).value
            except UnlinkableIdentifierError:
                return None

        if not candidates:
            raise LinkageKeyMissingError()

        for version, token in candidates.items():
            row = self._session.execute(
                select(IdentityIdentifier).where(
                    IdentityIdentifier.linkage_token == token,
                    IdentityIdentifier.linkage_key_version == version,
                )
            ).scalar_one_or_none()
            if row is not None:
                return row, version

        # Nothing matched under any configured version. If the vault holds
        # tokens under a version we cannot derive, this "not found" is a lie
        # that would create a duplicate patient - so check before believing it.
        self._assert_no_underivable_versions(versions)
        return None

    def _ordered_versions(self) -> list[str]:
        active = self._deriver.active_version
        known = self._deriver.known_versions()
        return [active, *sorted(known - {active})] if active in known else sorted(known)

    def _assert_no_underivable_versions(self, configured: list[str]) -> None:
        stored = set(
            self._session.execute(select(IdentityIdentifier.linkage_key_version).distinct())
            .scalars()
            .all()
        )
        missing = stored - set(configured)
        if missing:
            raise RetiredKeyUnavailableError(frozenset(configured))

    def _rekey(self, row: IdentityIdentifier, active: LinkageToken) -> bool:
        """Re-derive a matched identifier under the active key, idempotently.

        Concurrency is handled by the database rather than by a lock: two
        workers may re-key the same row at once, and the unique constraint on
        ``(linkage_token, linkage_key_version)`` means the loser sees an
        IntegrityError. That is a success from the caller's point of view - the
        row now carries the active version either way - so it is absorbed rather
        than raised.
        """
        savepoint = self._session.begin_nested()
        try:
            row.linkage_token = active.value
            row.linkage_key_version = active.key_version
            self._session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            self._session.expire(row)
            return False
        return True

    def _create(
        self,
        identifier_type: IdentifierType,
        raw_value: str,
        *,
        active_token: str,
        active_version: str,
        confidence: LinkageConfidence,
        patient_reference_id: uuid.UUID,
        surname: str | None,
        given_name: str | None,
        phone_contact: str | None,
        date_of_birth: str | None,
    ) -> LinkageResult:
        """Record a person not seen before.

        A concurrent worker may be doing the same thing with the same
        identifier. The unique constraint decides; the loser re-reads and
        returns the winner's reference, so two simultaneous first visits produce
        one patient rather than two.
        """
        record = IdentityRecord(
            patient_reference_id=patient_reference_id,
            encryption_key_version=self._encryptor.active_version,
        )
        binding = str(patient_reference_id)
        record.surname_encrypted = self._encrypt(surname, "surname", binding)
        record.given_name_encrypted = self._encrypt(given_name, "given_name", binding)
        record.phone_contact_encrypted = self._encrypt(phone_contact, "phone_contact", binding)
        record.date_of_birth_encrypted = self._encrypt(date_of_birth, "date_of_birth", binding)

        record.identifiers = [
            IdentityIdentifier(
                identifier_type=identifier_type,
                raw_value_encrypted=self._encryptor.encrypt(
                    raw_value,
                    aad=build_associated_data(_IDENTIFIER_TABLE, "raw_value", active_token),
                )
                or b"",
                encryption_key_version=self._encryptor.active_version,
                linkage_token=active_token,
                linkage_key_version=active_version,
                confidence=confidence,
            )
        ]

        savepoint = self._session.begin_nested()
        try:
            self._session.add(record)
            self._session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            existing = self._session.execute(
                select(IdentityIdentifier).where(
                    IdentityIdentifier.linkage_token == active_token,
                    IdentityIdentifier.linkage_key_version == active_version,
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            winner = self._session.get(IdentityRecord, existing.identity_record_id)
            if winner is None:  # pragma: no cover
                raise
            logger.info(
                "identity_linkage_raced",
                identifier_type=identifier_type.value,
                patient_reference_id=str(winner.patient_reference_id),
            )
            return LinkageResult(
                patient_reference_id=winner.patient_reference_id,
                confidence=confidence,
                created=False,
            )

        logger.info(
            "identity_linkage_created",
            identifier_type=identifier_type.value,
            patient_reference_id=str(patient_reference_id),
        )
        return LinkageResult(
            patient_reference_id=patient_reference_id, confidence=confidence, created=True
        )

    def _encrypt(self, value: str | None, column: str, binding: str) -> bytes | None:
        return self._encryptor.encrypt(
            value, aad=build_associated_data(_RECORD_TABLE, column, binding)
        )

    def _require_ready(self) -> None:
        if not self._deriver.is_configured:
            raise LinkageKeyMissingError()
        if not self._encryptor.is_configured:
            from mars.identity.encryption import EncryptionKeyMissingError

            raise EncryptionKeyMissingError()

    # -- Re-identification -------------------------------------------------
    def reidentify(
        self,
        principal: AuthenticatedPrincipal,
        patient_reference_id: uuid.UUID,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> DisclosedIdentity:
        """Turn a pseudonymous reference back into a person.

        Gates, in order, and **no identity is queried until all of them pass**:

        1. ``patient:reidentify``
        2. ``DIRECT_IDENTITY`` sensitivity
        3. A stated, non-empty reason
        4. A reference that resolves

        Gates 1, 2 and 4 raise the *same* :class:`IdentityUnavailableError`, so a
        refused caller cannot tell "you may not" from "there is nobody" and
        cannot use the service to discover which references are real. Gate 3
        stays a validation error: it describes the caller's own request and
        reveals nothing about any patient.

        The order matters as much as the outcome. Querying before checking would
        make the *timing* of a refusal depend on whether the reference existed,
        which is the same disclosure by a slower route.
        """
        stated = (reason or "").strip()

        if not principal.has_permission(Permission.PATIENT_REIDENTIFY):
            self._record_attempt(
                principal,
                patient_reference_id,
                stated or "(no reason stated)",
                ReidentificationOutcome.DENIED_PERMISSION,
                request_id,
            )
            raise IdentityUnavailableError()

        if not principal.can_access_sensitivity(SensitivityLevel.DIRECT_IDENTITY):
            self._record_attempt(
                principal,
                patient_reference_id,
                stated or "(no reason stated)",
                ReidentificationOutcome.DENIED_SENSITIVITY,
                request_id,
            )
            raise IdentityUnavailableError()

        if not stated:
            self._record_attempt(
                principal,
                patient_reference_id,
                "(no reason stated)",
                ReidentificationOutcome.DENIED_NO_REASON,
                request_id,
            )
            raise ValidationFailedError("A stated reason is required to re-identify a patient.")

        record = self._session.execute(
            select(IdentityRecord).where(
                IdentityRecord.patient_reference_id == patient_reference_id
            )
        ).scalar_one_or_none()

        if record is None:
            self._record_attempt(
                principal,
                patient_reference_id,
                stated,
                ReidentificationOutcome.NOT_FOUND,
                request_id,
            )
            raise IdentityUnavailableError()

        disclosed = self._decrypt_record(record)

        self._record_attempt(
            principal,
            patient_reference_id,
            stated,
            ReidentificationOutcome.DISCLOSED,
            request_id,
        )
        self._record_general_audit(principal, patient_reference_id, request_id)

        # Records that a disclosure happened and to whom. Never what was
        # disclosed: a log is read by more people, kept longer and shipped
        # further than the vault ever is.
        logger.warning(
            "identity_reidentified",
            actor_user_id=str(principal.user_id),
            patient_reference_id=str(patient_reference_id),
            reason_length=len(stated),
        )
        return disclosed

    def _decrypt_record(self, record: IdentityRecord) -> DisclosedIdentity:
        binding = str(record.patient_reference_id)

        def field(envelope: bytes | None, column: str) -> str | None:
            return self._encryptor.decrypt(
                envelope,
                aad=build_associated_data(_RECORD_TABLE, column, binding),
                context=f"{_RECORD_TABLE}.{column}",
            )

        identifiers: list[tuple[str, str]] = []
        for identifier in record.identifiers:
            value = self._encryptor.decrypt(
                identifier.raw_value_encrypted,
                aad=build_associated_data(_IDENTIFIER_TABLE, "raw_value", identifier.linkage_token),
                context=f"{_IDENTIFIER_TABLE}.raw_value",
            )
            if value is not None:
                identifiers.append((identifier.identifier_type.value, value))

        return DisclosedIdentity(
            patient_reference_id=record.patient_reference_id,
            surname=field(record.surname_encrypted, "surname"),
            given_name=field(record.given_name_encrypted, "given_name"),
            phone_contact=field(record.phone_contact_encrypted, "phone_contact"),
            date_of_birth=field(record.date_of_birth_encrypted, "date_of_birth"),
            identifiers=tuple(identifiers),
        )

    def _record_attempt(
        self,
        principal: AuthenticatedPrincipal,
        patient_reference_id: uuid.UUID,
        reason: str,
        outcome: ReidentificationOutcome,
        request_id: str | None,
    ) -> None:
        """Write the attempt on a transaction that survives the request.

        A refusal rolls back the request that caused it. Recorded on the same
        transaction, the audit row would roll back too, and the only trace of an
        attempted re-identification would be gone.
        """
        event = ReidentificationEvent(
            actor_user_id=principal.user_id,
            actor_label=principal.username,
            session_reference=principal.session_reference,
            request_id=request_id,
            patient_reference_id=patient_reference_id,
            reason=reason,
            outcome=outcome,
            requested_at=datetime.now(UTC),
        )

        if self._durable_session_factory is None:
            self._session.add(event)
            self._session.flush()
            return

        durable = self._durable_session_factory()
        try:
            durable.add(event)
            durable.commit()
        except Exception:
            durable.rollback()
            raise
        finally:
            durable.close()

    def _record_general_audit(
        self,
        principal: AuthenticatedPrincipal,
        patient_reference_id: uuid.UUID,
        request_id: str | None,
    ) -> None:
        """Also record the disclosure in the general audit trail.

        ``mars_identity`` holds the detail - the stated reason, the outcome of
        every refusal - and is readable only by the identity role. ``mars_audit``
        holds the fact that a re-identification happened, so a reviewer looking
        at one actor's activity sees it beside everything else they did.

        The context carries the pseudonymous reference and nothing else. It
        deliberately omits the reason: ``mars_audit`` is readable by roles that
        must not learn why a particular patient was looked up.
        """
        if self._audit_service is None:
            return
        record = getattr(self._audit_service, "record", None)
        if record is None:  # pragma: no cover - defensive
            return
        record(
            action=AuditAction.REIDENTIFICATION_PERFORMED,
            outcome=AuditOutcome.SUCCEEDED,
            principal=principal,
            object_type="patient_reference",
            object_id=str(patient_reference_id),
            context={"request_id": request_id} if request_id else None,
        )


__all__ = [
    "DisclosedIdentity",
    "IdentityService",
    "LinkageResult",
    "RetiredKeyUnavailableError",
]
