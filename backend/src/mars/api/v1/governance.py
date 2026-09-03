"""Governance endpoints: configuration keys and the method registry.

Read-only in phases 1-2. Configuration *values* are not exposed: the response
carries the active version number and its checksum, which is what a reader needs
to establish which rules were in force, without publishing thresholds that have
not yet been approved.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from mars.api.dependencies import (
    ConfigurationServiceDep,
    MethodRegistryDep,
    require_permissions,
)
from mars.api.v1.schemas import (
    ConfigurationKeySummary,
    MethodDefinitionSummary,
    MethodVersionSummary,
)
from mars.domain.enums import LifecycleStatus
from mars.security.permissions import Permission
from mars.security.principal import AuthenticatedPrincipal

router = APIRouter(prefix="/governance", tags=["governance"])

ConfigurationViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.CONFIGURATION_VIEW))
]
MethodViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.METHOD_VIEW))
]


@router.get(
    "/configuration-keys",
    response_model=list[ConfigurationKeySummary],
    summary="Governed configuration keys and which version is active",
)
def list_configuration_keys(
    principal: ConfigurationViewer,
    service: ConfigurationServiceDep,
) -> list[ConfigurationKeySummary]:
    """List configuration keys.

    A key with no active version is returned with nulls rather than omitted: an
    ungoverned parameter is a fact the programme needs to see.
    """
    summaries: list[ConfigurationKeySummary] = []
    for key in service.list_keys():
        active = next((v for v in key.versions if v.status is LifecycleStatus.ACTIVE), None)
        summaries.append(
            ConfigurationKeySummary(
                id=key.id,
                key=key.key,
                label=key.label,
                description=key.description,
                category=key.category,
                requires_programme_approval=key.requires_programme_approval,
                active_version_number=active.version_number if active else None,
                active_version_checksum=active.value_checksum if active else None,
                active_effective_from=active.effective_from if active else None,
            )
        )
    return summaries


@router.get(
    "/methods",
    response_model=list[MethodDefinitionSummary],
    summary="The analytical method registry",
)
def list_methods(
    principal: MethodViewer,
    service: MethodRegistryDep,
) -> list[MethodDefinitionSummary]:
    """List registered methods and their versions.

    Definitions may exist without an active version. An empty or entirely
    inactive registry is an honest unconfigured state, not permission to use a
    hidden default.
    """
    return [
        MethodDefinitionSummary(
            id=method.id,
            code=method.code,
            label=method.label,
            kind=method.kind.value,
            purpose=method.purpose,
            versions=[
                MethodVersionSummary(
                    id=version.id,
                    semantic_version=version.semantic_version,
                    status=version.status.value,
                    summary=version.summary,
                    effective_from=version.effective_from,
                    validation_reference=version.validation_reference,
                    artifact_checksum=version.artifact_checksum,
                )
                for version in method.versions
            ],
        )
        for method in service.list_methods()
    ]
