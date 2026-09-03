"""Integration status endpoints.

Read-only. Starting an exchange is a CLI or worker action, not an HTTP one: a
pull can run for minutes and must survive the client disconnecting, and an
endpoint that starts one invites a retry storm on a slow remote system.

Every route requires ``integration:manage``. Integration state is operational
detail - which remote systems a deployment talks to, and where its mappings are
incomplete - and is not part of the surveillance surface.

**No response carries a credential.** The status model reports whether
credentials are configured, never what they are.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mars.api.dependencies import IntegrationStatusDep, require_permissions
from mars.api.v1.schemas import (
    IntegrationRunSummary,
    IntegrationStatusSummary,
    MappingProposalSummary,
)
from mars.core.errors import NotFoundError
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/integrations", tags=["integrations"])

IntegrationOperator = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.INTEGRATION_MANAGE))
]


@router.get(
    "/{system}/status",
    response_model=IntegrationStatusSummary,
    summary="Whether an external exchange is configured, and what it has done",
)
def get_status(
    system: str,
    principal: IntegrationOperator,
    service: IntegrationStatusDep,
) -> IntegrationStatusSummary:
    """Configuration and activity for one external system.

    An unconfigured system is reported as unconfigured rather than 404: "MARS
    does not exchange with DHIS2" is a fact worth stating, and it is a
    different fact from "that system does not exist".
    """
    return IntegrationStatusSummary.model_validate(service.status(system))


@router.get(
    "/{system}/runs",
    response_model=list[IntegrationRunSummary],
    summary="Recent exchanges, most recent first",
)
def list_runs(
    system: str,
    principal: IntegrationOperator,
    service: IntegrationStatusDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IntegrationRunSummary]:
    return [
        IntegrationRunSummary(
            id=run.id,
            system=run.system,
            resource=run.resource.value,
            run_status=run.run_status.value,
            attempt=run.attempt,
            scope_description=run.scope_description,
            started_at=run.started_at,
            finished_at=run.finished_at,
            pages_fetched=run.pages_fetched,
            records_received=run.records_received,
            records_accepted=run.records_accepted,
            records_rejected=run.records_rejected,
            mappings_unresolved=run.mappings_unresolved,
            error_category=run.error_category,
        )
        for run in service.list_runs(system, limit=limit)
    ]


@router.get(
    "/{system}/runs/{run_id}",
    response_model=IntegrationRunSummary,
    summary="One exchange",
)
def get_run(
    system: str,
    run_id: uuid.UUID,
    principal: IntegrationOperator,
    service: IntegrationStatusDep,
) -> IntegrationRunSummary:
    run = service.get_run(run_id)
    # Checked rather than filtered: an identifier from another system is
    # refused, not quietly excluded, so a caller cannot probe for which run ids
    # exist by watching a list shrink.
    if run is None or run.system != system:
        raise NotFoundError("No such integration run.")
    return IntegrationRunSummary(
        id=run.id,
        system=run.system,
        resource=run.resource.value,
        run_status=run.run_status.value,
        attempt=run.attempt,
        scope_description=run.scope_description,
        started_at=run.started_at,
        finished_at=run.finished_at,
        pages_fetched=run.pages_fetched,
        records_received=run.records_received,
        records_accepted=run.records_accepted,
        records_rejected=run.records_rejected,
        mappings_unresolved=run.mappings_unresolved,
        error_category=run.error_category,
    )


@router.get(
    "/{system}/mapping-proposals",
    response_model=list[MappingProposalSummary],
    summary="Remote identifiers MARS could not place",
)
def list_mapping_proposals(
    system: str,
    principal: IntegrationOperator,
    service: IntegrationStatusDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[MappingProposalSummary]:
    """Unresolved mappings, most frequent first.

    These are configuration gaps, not failures. MARS refuses to match a remote
    identifier by name similarity: two districts with similar names are exactly
    the case a fuzzy match gets wrong, and the figures still look plausible
    afterwards.
    """
    return [
        MappingProposalSummary(
            id=proposal.id,
            system=proposal.system,
            remote_type=proposal.remote_type,
            remote_id=proposal.remote_id,
            remote_name=proposal.remote_name,
            proposal_status=proposal.proposal_status.value,
            occurrences=proposal.occurrences,
            first_seen_at=proposal.first_seen_at,
            last_seen_at=proposal.last_seen_at,
        )
        for proposal in service.list_unresolved_mappings(system, limit=limit)
    ]
