"""The optional Ask MARS endpoint — Prompt 27.

Present in the API surface whether or not the assistant is configured, because
a client needs to be able to ask and be told no. A disabled deployment returns
an honest unavailable state; it never returns a fabricated answer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from mars.ai.assistant import AskMarsAssistant
from mars.api.dependencies import AuditDep, SessionDep, SettingsDep, require_permissions
from mars.api.v1.schemas import AskMarsAnswer, AskMarsAvailability, AskMarsRequest
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/ai", tags=["ai"])


def get_ask_mars(session: SessionDep, settings: SettingsDep, audit: AuditDep) -> AskMarsAssistant:
    """Build the assistant for one request.

    Lives here rather than in ``api.dependencies`` so that no core module
    imports ``mars.ai``. ADR 0008 requires the assistant to be a leaf: if the
    dependency graph reached into it, removing the package would break the API
    layer and the claim that surveillance works without AI would be false.
    """
    return AskMarsAssistant(session, enabled=settings.ai_assistant_enabled, audit=audit)


AskMarsDep = Annotated[AskMarsAssistant, Depends(get_ask_mars)]

Asker = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE)),
]


@router.get("/availability", response_model=AskMarsAvailability)
def availability(principal: Asker, assistant: AskMarsDep) -> AskMarsAvailability:
    """Whether Ask MARS can answer here, and why not when it cannot."""
    del principal
    return AskMarsAvailability.model_validate(assistant.availability())


@router.post("/ask", response_model=AskMarsAnswer)
def ask(body: AskMarsRequest, principal: Asker, assistant: AskMarsDep) -> AskMarsAnswer:
    """Answer a bounded question over records the caller may already read.

    Retrieval is scope-filtered in SQL by the same services the screens use, so
    a question cannot reach a district the asker cannot open. An answer with no
    supporting records is returned as exactly that.
    """
    answer = assistant.ask(
        principal,
        topic=body.topic,
        question=body.question,
        period_start=body.period_start,
        period_end=body.period_end,
        signal_id=body.signal_id,
    )
    return AskMarsAnswer.model_validate(answer.as_dict())


__all__ = ["router"]
