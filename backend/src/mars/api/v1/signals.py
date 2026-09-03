"""Scope-safe signal and deterministic explanation API."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import SignalQueryDep, require_permissions
from mars.api.v1.schemas import SignalExplanationSummary, SignalSummary
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/signals", tags=["signals"])
SignalReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]


@router.get("", response_model=list[SignalSummary])
def list_signals(
    principal: SignalReader,
    service: SignalQueryDep,
    period_from: date | None = None,
    period_to: date | None = None,
    active_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[SignalSummary]:
    return [
        SignalSummary.model_validate(item)
        for item in service.list(
            principal,
            period_from=period_from,
            period_to=period_to,
            active_only=active_only,
            limit=limit,
        )
    ]


@router.get("/{signal_id}", response_model=SignalSummary)
def get_signal(
    signal_id: uuid.UUID, principal: SignalReader, service: SignalQueryDep
) -> SignalSummary:
    return SignalSummary.model_validate(service.get(principal, signal_id))


@router.get("/{signal_id}/explanation", response_model=SignalExplanationSummary)
def get_explanation(
    signal_id: uuid.UUID, principal: SignalReader, service: SignalQueryDep
) -> SignalExplanationSummary:
    return SignalExplanationSummary.model_validate(service.explanation(principal, signal_id))


__all__ = ["router"]
