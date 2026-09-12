"""Encrypted retention of canonical evidence datasets.

A recurrence definition can be re-applied to retained evidence without
refetching the source, which is what makes Apply cheap and a comparison
meaningful. The evidence is patient-level, so it is never stored in clear.

Encryption follows the live snapshot store's convention: AES-256-GCM under a
purpose-derived key from the deployment's identity encryption secret, a random
96-bit nonce per ciphertext, and associated data that binds each ciphertext to
its row, its fingerprint and the key version. A payload copied onto another row
fails authentication instead of decrypting as the wrong evidence.

A dataset is identified by :func:`~mars.domain.longitudinal.evidence_fingerprint`.
Publishing the same evidence twice returns the first row. Loading re-computes
the fingerprint and refuses a payload that no longer matches it.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mars.core.errors import ConflictError, NotFoundError
from mars.domain.longitudinal import (
    EVIDENCE_CONTRACT_VERSION,
    CanonicalEncounter,
    EvidenceCoverage,
    Medication,
    evidence_fingerprint,
)
from mars.domain.longitudinal_codec import decode_dataset, encode_dataset
from mars.domain.recurrence_analysis import ClinicalEvidenceDataset

_PURPOSE = b"mars-recurrence-evidence-v1:"


class EvidenceCipher:
    """Authenticated encryption for recurrence evidence, findings and duplicates."""

    def __init__(self, secret: str, key_version: str) -> None:
        if not secret:
            raise ValueError("Recurrence evidence requires an encryption secret")
        derived = hashlib.sha256(_PURPOSE + secret.encode()).digest()
        self._aead = AESGCM(derived)
        # Names the key without revealing it, so rotation is visible on every row.
        self.key_version = f"{key_version}:{hashlib.sha256(derived).hexdigest()[:16]}"

    def seal(self, value: Any, aad: str) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), aad.encode())

    def open(self, value: bytes, aad: str) -> Any:
        return json.loads(self._aead.decrypt(value[:12], value[12:], aad.encode()))


def dataset_aad(dataset_id: uuid.UUID, dataset_hash: str, key_version: str) -> str:
    return f"dataset:{dataset_id}:{dataset_hash}:{key_version}"


@dataclass(frozen=True, slots=True)
class RetainedEvidence:
    """A decrypted, fingerprint-verified dataset."""

    dataset: ClinicalEvidenceDataset
    encounters: tuple[CanonicalEncounter, ...]
    coverage: EvidenceCoverage
    unlinked_medications: Mapping[str, tuple[Medication, ...]]


class ClinicalEvidenceStore:
    def __init__(self, session: Session, cipher: EvidenceCipher) -> None:
        self._session = session
        self._cipher = cipher

    @property
    def key_version(self) -> str:
        return self._cipher.key_version

    def publish(
        self,
        *,
        encounters: Sequence[CanonicalEncounter],
        coverage: EvidenceCoverage,
        source_kind: str,
        namespace: str,
        scope_key: str,
        facility_refs: Sequence[str],
        mapping_version: str,
        created_by: str,
        unlinked_medications: Mapping[str, Sequence[Medication]] | None = None,
    ) -> ClinicalEvidenceDataset:
        """Retain evidence, or return the row already retaining the same evidence."""
        dataset_hash = evidence_fingerprint(encounters, coverage)
        existing = self._find(
            dataset_hash,
            source_kind=source_kind,
            scope_key=scope_key,
            mapping_version=mapping_version,
        )
        if existing is not None:
            return existing
        dataset_id = uuid.uuid4()
        payload = self._cipher.seal(
            encode_dataset(encounters, coverage, unlinked_medications),
            dataset_aad(dataset_id, dataset_hash, self._cipher.key_version),
        )
        dataset = ClinicalEvidenceDataset(
            id=dataset_id,
            source_kind=source_kind,
            namespace=namespace,
            scope_key=scope_key,
            facility_refs=sorted(facility_refs),
            coverage_start=coverage.earliest_date,
            coverage_end=coverage.latest_date,
            coverage_complete=coverage.complete and not coverage.missing_facilities,
            coverage_notes=list(coverage.exclusions),
            stages=sorted(coverage.stages),
            mapping_version=mapping_version,
            contract_version=EVIDENCE_CONTRACT_VERSION,
            dataset_hash=dataset_hash,
            key_version=self._cipher.key_version,
            encounter_count=len(encounters),
            patient_count=len({e.patient for e in encounters if e.patient is not None}),
            payload=payload,
            created_by=created_by,
        )
        try:
            with self._session.begin_nested():
                self._session.add(dataset)
                self._session.flush()
        except IntegrityError:
            # Another publisher retained the same evidence first.
            winner = self._find(
                dataset_hash,
                source_kind=source_kind,
                scope_key=scope_key,
                mapping_version=mapping_version,
            )
            if winner is None:
                raise
            return winner
        return dataset

    def load(self, dataset_id: uuid.UUID) -> RetainedEvidence:
        dataset = self._session.get(ClinicalEvidenceDataset, dataset_id)
        if dataset is None:
            raise NotFoundError("The retained evidence dataset was not found")
        return self.open(dataset)

    def open(self, dataset: ClinicalEvidenceDataset) -> RetainedEvidence:
        if dataset.key_version != self._cipher.key_version:
            raise ConflictError(
                "The retained evidence was encrypted under a different key version; "
                "retrieve the evidence again."
            )
        decoded = self._cipher.open(
            dataset.payload, dataset_aad(dataset.id, dataset.dataset_hash, dataset.key_version)
        )
        encounters, coverage, unlinked = decode_dataset(decoded)
        if evidence_fingerprint(encounters, coverage) != dataset.dataset_hash:
            raise ConflictError("The retained evidence no longer matches its fingerprint.")
        return RetainedEvidence(dataset, encounters, coverage, unlinked)

    def latest_live(
        self, scope_key: str, *, period_start: date, period_end: date
    ) -> ClinicalEvidenceDataset | None:
        """The newest live dataset for one authorised scope that spans the period."""
        return self._session.execute(
            select(ClinicalEvidenceDataset)
            .where(
                ClinicalEvidenceDataset.source_kind == "live_tracker",
                ClinicalEvidenceDataset.scope_key == scope_key,
                ClinicalEvidenceDataset.key_version == self._cipher.key_version,
                ClinicalEvidenceDataset.coverage_end >= period_end,
                ClinicalEvidenceDataset.coverage_start <= period_start,
            )
            .order_by(ClinicalEvidenceDataset.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _find(
        self,
        dataset_hash: str,
        *,
        source_kind: str,
        scope_key: str,
        mapping_version: str,
    ) -> ClinicalEvidenceDataset | None:
        return self._session.execute(
            select(ClinicalEvidenceDataset).where(
                ClinicalEvidenceDataset.dataset_hash == dataset_hash,
                ClinicalEvidenceDataset.key_version == self._cipher.key_version,
                ClinicalEvidenceDataset.source_kind == source_kind,
                ClinicalEvidenceDataset.scope_key == scope_key,
                ClinicalEvidenceDataset.mapping_version == mapping_version,
            )
        ).scalar_one_or_none()


__all__ = [
    "ClinicalEvidenceStore",
    "EvidenceCipher",
    "RetainedEvidence",
    "dataset_aad",
]
