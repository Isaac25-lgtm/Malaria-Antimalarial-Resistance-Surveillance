"""Deterministic structured signal explanations — Prompt 22."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mars.domain.enums import SignalEvidenceRole
from mars.domain.explanation import SignalExplanation
from mars.domain.signal import SignalEvidence, SurveillanceSignal

GENERATOR_VERSION = "1.0.0"
INTERPRETATION_LIMIT = (
    "Routine surveillance data identifies patterns requiring investigation. "
    "It does not confirm antimalarial resistance, treatment failure, "
    "recrudescence, or reinfection."
)
INHERENT_MISSING_INFORMATION = (
    "Routine records do not prove that prescribed treatment was received or taken as directed.",
    "Routine records cannot distinguish recrudescence from reinfection.",
    "No parasite genotype or molecular resistance marker is established by this signal.",
)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _item(evidence: SignalEvidence) -> dict[str, Any]:
    return {
        "kind": evidence.evidence_kind.value,
        "role": evidence.role.value,
        "source": {
            "table": evidence.source_table,
            "record_id": str(evidence.source_record_id),
        },
        "contribution": str(evidence.contribution) if evidence.contribution is not None else None,
        "summary": evidence.summary,
        "facts": evidence.facts,
        "quality_context": evidence.quality_context or {},
    }


def _missing_information(evidence: list[SignalEvidence]) -> list[str]:
    """Describe material gaps already recorded by upstream engines.

    This converts keys into labels; it never infers that an unmentioned field
    is missing and never invents a count.
    """
    statements = list(INHERENT_MISSING_INFORMATION)
    seen = set(statements)
    for item in evidence:
        material = {**(item.quality_context or {}), **(item.facts or {})}
        for key, value in sorted(material.items()):
            lowered = key.lower()
            if not any(
                marker in lowered for marker in ("missing", "unresolved", "excluded", "unavailable")
            ):
                continue
            if value in (None, False, 0, "", [], {}):
                continue
            label = key.replace("_", " ")
            statement = f"Upstream evidence records {label}: {value}."
            if statement not in seen:
                seen.add(statement)
                statements.append(statement)
    return statements


class ExplanationEngine:
    """Build an explanation snapshot without an AI or external service."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build(self, signal_id: uuid.UUID) -> SignalExplanation:
        signal = self._session.execute(
            select(SurveillanceSignal)
            .options(selectinload(SurveillanceSignal.evidence))
            .where(SurveillanceSignal.id == signal_id)
        ).scalar_one()
        ordered = sorted(
            signal.evidence,
            key=lambda item: (
                item.role.value,
                item.evidence_kind.value,
                str(item.source_record_id),
            ),
        )
        material = {
            "signal_input_fingerprint": signal.input_fingerprint,
            "method_version_id": str(signal.method_version_id),
            "rule_snapshot": signal.rule_snapshot,
            "evidence": [
                [item.role.value, item.evidence_kind.value, str(item.source_record_id), item.facts]
                for item in ordered
            ],
            "generator_version": GENERATOR_VERSION,
        }
        input_fingerprint = _fingerprint(material)
        existing = self._session.execute(
            select(SignalExplanation).where(
                SignalExplanation.signal_id == signal.id,
                SignalExplanation.generator_version == GENERATOR_VERSION,
                SignalExplanation.input_fingerprint == input_fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        supporting = [
            _item(item) for item in ordered if item.role is not SignalEvidenceRole.COUNTER
        ]
        counter = [_item(item) for item in ordered if item.role is SignalEvidenceRole.COUNTER]
        snapshot = signal.rule_snapshot
        steps = [
            {
                "step": 1,
                "operation": "select_evidence",
                "detail": {
                    "rule_code": signal.rule_code,
                    "source_kinds": snapshot.get("source_kinds", []),
                    "minimum_evidence": snapshot.get("minimum_evidence"),
                },
            },
            {
                "step": 2,
                "operation": "apply_versioned_weights",
                "detail": {"weights": snapshot.get("weights", {})},
            },
            {
                "step": 3,
                "operation": "compare_score",
                "detail": {
                    "observed_score": str(signal.score) if signal.score is not None else None,
                    "minimum_score": snapshot.get("minimum_score"),
                },
            },
            {
                "step": 4,
                "operation": "classify_priority",
                "detail": {
                    "priority": signal.priority.value,
                    "bands": snapshot.get("priority_bands", []),
                },
            },
        ]
        explanation = SignalExplanation(
            signal_id=signal.id,
            method_version_id=signal.method_version_id,
            why_flagged=(
                f"{signal.statement} It met governed rule {signal.rule_code} with "
                f"{signal.evidence_count} supporting evidence item(s), score "
                f"{signal.score}, and priority {signal.priority.value}."
            ),
            evidence=supporting,
            counter_evidence=counter,
            data_quality=signal.data_quality,
            method_steps=steps,
            uncertainty=list(signal.uncertainty),
            missing_information=_missing_information(ordered),
            recommended_actions=[{"code": code} for code in signal.recommended_action_codes],
            interpretation_limit=INTERPRETATION_LIMIT,
            signal_input_fingerprint=signal.input_fingerprint,
            input_fingerprint=input_fingerprint,
            generator_version=GENERATOR_VERSION,
            generated_at=datetime.now(UTC),
        )
        self._session.add(explanation)
        self._session.flush()
        return explanation


__all__ = [
    "GENERATOR_VERSION",
    "INHERENT_MISSING_INFORMATION",
    "INTERPRETATION_LIMIT",
    "ExplanationEngine",
]
