"""The investigation workflow API and action centre — Prompt 26.

Each command declares its own permission. Triage, assignment, updating and
closure are different acts by different people, and one blanket
``investigation:write`` would let whoever can add a note also close the case.

Every write takes an ``expected_version`` for optimistic concurrency. Two
reviewers who both loaded an investigation and both press close must not
silently overwrite one another: the second is refused and re-reads.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import InvestigationServiceDep, require_permissions
from mars.api.v1.schemas import (
    EvidenceRequestSummary,
    InvestigationDetail,
    InvestigationEventSummary,
    InvestigationQueueEntry,
    OpenInvestigationRequest,
    RecordExternalResultRequest,
    RequestEvidenceRequest,
    TransitionInvestigationRequest,
)
from mars.domain.enums import InvestigationStatus
from mars.investigations.service import QUEUES
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/investigations", tags=["investigations"])

Viewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.SURVEILLANCE_VIEW_AGGREGATE))
]
Triager = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.INVESTIGATION_TRIAGE))
]
Assigner = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.INVESTIGATION_ASSIGN))
]
Updater = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.INVESTIGATION_UPDATE))
]
Closer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.INVESTIGATION_CLOSE))
]


def _detail(service: Any, record: Any) -> InvestigationDetail:
    """Shape one investigation for the wire.

    The shaping itself lives in the service (ADR 0002): a router that could
    reach ``investigation.events`` would eventually issue a query, and that is
    invisible until it is slow.
    """
    return InvestigationDetail.model_validate(service.detail_shape(record))


QueueName = Literal[
    "new",
    "high_priority",
    "assigned_to_me",
    "under_investigation",
    "awaiting_external_result",
    "resolved",
]


@router.get("/queues/{name}", response_model=list[InvestigationQueueEntry])
def queue(
    name: QueueName,
    principal: Viewer,
    service: InvestigationServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[InvestigationQueueEntry]:
    """One action-centre queue, scoped to the caller.

    There is no ``overdue`` queue in this build. It needs an approved SLA, and
    an empty overdue list would read as "nothing is late" rather than "MARS has
    not been told what late means".
    """
    return [
        InvestigationQueueEntry.model_validate(service.queue_shape(item))
        for item in service.queue(principal, name=name, limit=limit)
    ]


@router.get("/queues", response_model=dict[str, object])
def queue_catalogue(principal: Viewer, service: InvestigationServiceDep) -> dict[str, object]:
    """Which queues exist, and why the overdue one does not."""
    sla, missing = service.sla_configuration()
    return {
        "queues": list(QUEUES),
        "overdue": {
            "available": sla is not None,
            "missing_configuration": missing,
            "detail": (
                None
                if sla is not None
                else (
                    "An overdue queue requires an approved investigation SLA. "
                    "How long a district has to triage a signal is a programme "
                    "commitment, and MARS will not invent one - showing an "
                    "empty overdue list would say nothing is late."
                )
            ),
        },
    }


@router.get("/{investigation_id}", response_model=InvestigationDetail)
def get_investigation(
    investigation_id: uuid.UUID, principal: Viewer, service: InvestigationServiceDep
) -> InvestigationDetail:
    """One investigation with its full timeline."""
    return _detail(service, service.get(principal, investigation_id))


@router.post("", response_model=InvestigationDetail, status_code=201)
def open_investigation(
    body: OpenInvestigationRequest, principal: Triager, service: InvestigationServiceDep
) -> InvestigationDetail:
    """Open an investigation against a signal. Idempotent."""
    return _detail(
        service,
        service.open(principal, signal_id=body.signal_id, idempotency_key=body.idempotency_key),
    )


@router.post("/{investigation_id}/triage", response_model=InvestigationDetail)
def triage(
    investigation_id: uuid.UUID,
    body: TransitionInvestigationRequest,
    principal: Triager,
    service: InvestigationServiceDep,
) -> InvestigationDetail:
    return _detail(
        service,
        service.transition(
            principal,
            investigation_id=investigation_id,
            to_status=InvestigationStatus.TRIAGED,
            expected_version=body.expected_version,
            note=body.note,
        ),
    )


@router.post("/{investigation_id}/assign", response_model=InvestigationDetail)
def assign(
    investigation_id: uuid.UUID,
    body: TransitionInvestigationRequest,
    principal: Assigner,
    service: InvestigationServiceDep,
) -> InvestigationDetail:
    """Assign or reassign. Reassignment is recorded as its own event kind."""
    return _detail(
        service,
        service.transition(
            principal,
            investigation_id=investigation_id,
            to_status=InvestigationStatus.ASSIGNED,
            expected_version=body.expected_version,
            assigned_to_user_id=body.assigned_to_user_id,
            note=body.note,
        ),
    )


@router.post("/{investigation_id}/start", response_model=InvestigationDetail)
def start(
    investigation_id: uuid.UUID,
    body: TransitionInvestigationRequest,
    principal: Updater,
    service: InvestigationServiceDep,
) -> InvestigationDetail:
    return _detail(
        service,
        service.transition(
            principal,
            investigation_id=investigation_id,
            to_status=InvestigationStatus.UNDER_INVESTIGATION,
            expected_version=body.expected_version,
            note=body.note,
        ),
    )


@router.post("/{investigation_id}/close", response_model=InvestigationDetail)
def close(
    investigation_id: uuid.UUID,
    body: TransitionInvestigationRequest,
    principal: Closer,
    service: InvestigationServiceDep,
) -> InvestigationDetail:
    """Close with a governed outcome.

    ``validated_signal`` means the pattern held up and warrants programme
    action. It does not mean resistance was confirmed.
    """
    return _detail(
        service,
        service.transition(
            principal,
            investigation_id=investigation_id,
            to_status=InvestigationStatus.CLOSED,
            expected_version=body.expected_version,
            outcome=body.outcome,
            note=body.note,
        ),
    )


@router.post("/{investigation_id}/escalate", response_model=InvestigationDetail)
def escalate(
    investigation_id: uuid.UUID,
    body: TransitionInvestigationRequest,
    principal: Closer,
    service: InvestigationServiceDep,
) -> InvestigationDetail:
    return _detail(
        service,
        service.transition(
            principal,
            investigation_id=investigation_id,
            to_status=InvestigationStatus.ESCALATED,
            expected_version=body.expected_version,
            escalation_reason=body.escalation_reason,
            note=body.note,
        ),
    )


@router.post("/{investigation_id}/notes", response_model=InvestigationEventSummary)
def add_note(
    investigation_id: uuid.UUID,
    body: RequestEvidenceRequest,
    principal: Updater,
    service: InvestigationServiceDep,
) -> InvestigationEventSummary:
    event = service.add_note(
        principal,
        investigation_id=investigation_id,
        note=body.description,
        expected_version=body.expected_version,
    )
    return InvestigationEventSummary.model_validate(service.event_shape(event))


@router.post("/{investigation_id}/evidence-requests", response_model=EvidenceRequestSummary)
def request_evidence(
    investigation_id: uuid.UUID,
    body: RequestEvidenceRequest,
    principal: Updater,
    service: InvestigationServiceDep,
) -> EvidenceRequestSummary:
    request = service.request_evidence(
        principal,
        investigation_id=investigation_id,
        description=body.description,
        expected_version=body.expected_version,
    )
    return EvidenceRequestSummary.model_validate(service.evidence_request_shape(request))


@router.post(
    "/{investigation_id}/evidence-requests/{evidence_request_id}/result",
    response_model=EvidenceRequestSummary,
)
def record_external_result(
    investigation_id: uuid.UUID,
    evidence_request_id: uuid.UUID,
    body: RecordExternalResultRequest,
    principal: Updater,
    service: InvestigationServiceDep,
) -> EvidenceRequestSummary:
    """Record that an external result came back, by reference only.

    MARS stores a pointer into the system holding the result under its own
    governance. It never stores the clinical content, which is what keeps the
    confirmed-evidence lane separate from routine surveillance.
    """
    request = service.record_external_result(
        principal,
        investigation_id=investigation_id,
        evidence_request_id=evidence_request_id,
        result_reference=body.result_reference,
        expected_version=body.expected_version,
    )
    return EvidenceRequestSummary.model_validate(service.evidence_request_shape(request))


__all__ = ["router"]
