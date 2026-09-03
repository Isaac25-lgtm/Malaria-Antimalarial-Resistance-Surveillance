"""Scope-safe signal and explanation read models."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from mars.core.errors import FieldError, NotFoundError, ValidationFailedError
from mars.domain.explanation import SignalExplanation
from mars.domain.signal import SurveillanceSignal
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService


class SignalQueryService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._scope = AnalyticsQueryService(session)

    def list(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_from: date | None,
        period_to: date | None,
        active_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        if period_from is not None and period_to is not None and period_to < period_from:
            raise ValidationFailedError(
                "period_to must be on or after period_from",
                errors=[
                    FieldError(
                        field="period_to",
                        message="must be on or after period_from",
                        code="period_ordered",
                    )
                ],
            )
        statement = select(SurveillanceSignal).options(selectinload(SurveillanceSignal.evidence))
        if period_from is not None:
            statement = statement.where(SurveillanceSignal.period_start >= period_from)
        if period_to is not None:
            statement = statement.where(SurveillanceSignal.period_end <= period_to)
        if active_only:
            from mars.domain.enums import SignalStatus

            statement = statement.where(SurveillanceSignal.signal_status == SignalStatus.ACTIVE)
        geographies = self._scope.geography_ids(principal)
        facilities = self._scope.facility_ids(principal)
        if principal.is_facility_restricted:
            geographies = set()
        if geographies is not None and facilities is not None:
            statement = statement.where(
                or_(
                    SurveillanceSignal.geography_unit_id.in_(geographies),
                    SurveillanceSignal.facility_id.in_(facilities),
                )
            )
        rows = self._session.execute(
            statement.order_by(SurveillanceSignal.generated_at.desc()).limit(limit)
        ).scalars()
        return [self._shape(row, include_evidence=False) for row in rows]

    def get(self, principal: AuthenticatedPrincipal, signal_id: uuid.UUID) -> dict[str, Any]:
        statement = (
            select(SurveillanceSignal)
            .options(selectinload(SurveillanceSignal.evidence))
            .where(SurveillanceSignal.id == signal_id)
        )
        geographies = self._scope.geography_ids(principal)
        facilities = self._scope.facility_ids(principal)
        if principal.is_facility_restricted:
            geographies = set()
        if geographies is not None and facilities is not None:
            statement = statement.where(
                or_(
                    SurveillanceSignal.geography_unit_id.in_(geographies),
                    SurveillanceSignal.facility_id.in_(facilities),
                )
            )
        signal = self._session.execute(statement).scalar_one_or_none()
        if signal is None:
            raise NotFoundError("signal not found or outside your assigned scope")
        return self._shape(signal, include_evidence=True)

    def explanation(
        self, principal: AuthenticatedPrincipal, signal_id: uuid.UUID
    ) -> dict[str, Any]:
        self.get(principal, signal_id)
        row = self._session.execute(
            select(SignalExplanation)
            .where(SignalExplanation.signal_id == signal_id)
            .order_by(SignalExplanation.generated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("no explanation has been generated for this signal")
        return {
            "id": row.id,
            "signal_id": row.signal_id,
            "method_version_id": row.method_version_id,
            "why_flagged": row.why_flagged,
            "evidence": row.evidence,
            "counter_evidence": row.counter_evidence,
            "data_quality": row.data_quality,
            "method_steps": row.method_steps,
            "uncertainty": row.uncertainty,
            "missing_information": row.missing_information,
            "recommended_actions": row.recommended_actions,
            "interpretation_limit": row.interpretation_limit,
            "signal_input_fingerprint": row.signal_input_fingerprint,
            "input_fingerprint": row.input_fingerprint,
            "generator_version": row.generator_version,
            "generated_at": row.generated_at,
        }

    @staticmethod
    def _shape(signal: SurveillanceSignal, *, include_evidence: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": signal.id,
            "signal_type": signal.signal_type.value,
            "status": signal.signal_status.value,
            "priority": signal.priority.value,
            "geography_unit_id": signal.geography_unit_id,
            "facility_id": signal.facility_id,
            "period_start": signal.period_start,
            "period_end": signal.period_end,
            "title": signal.title,
            "statement": signal.statement,
            "score": float(signal.score) if signal.score is not None else None,
            "evidence_count": signal.evidence_count,
            "counter_evidence_count": signal.counter_evidence_count,
            "data_quality": signal.data_quality,
            "uncertainty": signal.uncertainty,
            "recommended_action_codes": signal.recommended_action_codes,
            "method_version_id": signal.method_version_id,
            "rule_code": signal.rule_code,
            # Lineage a reviewer needs to reproduce the result. The
            # fingerprint is the identity of the evidence set that produced
            # this signal, so a reader can tell a recomputation from a
            # genuinely new finding.
            "input_fingerprint": signal.input_fingerprint,
            "group_key": signal.group_key,
            "source_cutoff": signal.source_cutoff,
            "generated_at": signal.generated_at,
            "supersedes_id": signal.supersedes_id,
            "superseded_by_id": signal.superseded_by_id,
        }
        if include_evidence:
            result["evidence"] = [
                {
                    "kind": item.evidence_kind.value,
                    "role": item.role.value,
                    "source_table": item.source_table,
                    "source_record_id": item.source_record_id,
                    "contribution": (
                        float(item.contribution) if item.contribution is not None else None
                    ),
                    "summary": item.summary,
                    "facts": item.facts,
                    "quality_context": item.quality_context,
                }
                for item in signal.evidence
            ]
        return result


__all__ = ["SignalQueryService"]
