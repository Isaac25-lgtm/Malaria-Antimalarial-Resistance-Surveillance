"""Deployment metadata.

Reports what this build is and which governed methods and configuration keys are
active, so a figure on a screen can always be traced to the rules that produced
it. Configuration *values* are never exposed here - only which version is in
force and its checksum.
"""

from __future__ import annotations

from fastapi import APIRouter

from mars.api.dependencies import ConfigurationServiceDep, MethodRegistryDep, SettingsDep
from mars.api.v1.schemas import VersionResponse
from mars.core.logging import get_logger
from mars.core.timeutils import DISPLAY_TIMEZONE_NAME
from mars.domain.enums import LifecycleStatus

router = APIRouter(tags=["metadata"])
logger = get_logger(__name__)


@router.get(
    "/meta/version",
    response_model=VersionResponse,
    summary="Build and governance identity of this deployment",
)
def version(
    settings: SettingsDep,
    methods: MethodRegistryDep,
    configuration: ConfigurationServiceDep,
) -> VersionResponse:
    """Return build identity plus the active method and configuration registry.

    Registry lookups are best-effort: this endpoint must keep answering during a
    database outage, because it is what an operator reaches for first when
    something is wrong.
    """
    active_methods: list[str] = []
    active_config_keys: list[str] = []

    try:
        active_methods = sorted(
            f"{version.method.code}@{version.semantic_version}"
            for version in methods.active_versions()
        )
    except Exception:
        logger.warning("meta_version_methods_unavailable")

    try:
        active_config_keys = sorted(
            key.key
            for key in configuration.list_keys()
            if any(v.status is LifecycleStatus.ACTIVE for v in key.versions)
        )
    except Exception:
        logger.warning("meta_version_configuration_unavailable")

    return VersionResponse(
        name=settings.app_name,
        release_version=settings.release_version,
        git_sha=settings.git_sha,
        build_timestamp=settings.build_timestamp,
        environment=settings.environment.value,
        api_version="v1",
        display_timezone=DISPLAY_TIMEZONE_NAME,
        ai_assistant_enabled=settings.ai_assistant_enabled,
        demo_mode_enabled=settings.demo_mode_enabled,
        development_auth_active=settings.is_development_auth_active,
        active_method_versions=active_methods,
        active_configuration_keys=active_config_keys,
    )


@router.get(
    "/meta/permissions",
    summary="The permission catalogue this build recognises",
)
def permission_catalogue() -> dict[str, object]:
    """Describe every permission and the sensitivity tier it requires.

    Read by the administration interface so that permission descriptions come
    from one place rather than being re-typed in the frontend.
    """
    from mars.security.permissions import PERMISSION_CATALOGUE, ROLE_PERMISSIONS

    return {
        "permissions": [
            {
                "code": spec.permission.value,
                "label": spec.label,
                "description": spec.description,
                "minimum_sensitivity": spec.minimum_sensitivity.name.lower(),
            }
            for spec in PERMISSION_CATALOGUE.values()
        ],
        "system_roles": [
            {
                "code": role.value,
                "permissions": sorted(p.value for p in permissions),
            }
            for role, permissions in ROLE_PERMISSIONS.items()
        ],
    }


@router.get(
    "/meta/evidence-lanes",
    summary="How MARS separates routine signals from confirmed findings",
)
def evidence_lanes() -> dict[str, object]:
    """Describe the two-lane evidence model.

    Served by the API rather than written into the interface so that the
    scientific boundary has one authoritative statement, used identically by the
    dashboard, generated reports and documentation.
    """
    return {
        "lanes": [
            {
                "id": "routine_surveillance",
                "label": "Routine-derived surveillance signal",
                "sources": [
                    "HMIS OPD 002 patient-level encounters",
                    "HMIS 033b weekly surveillance",
                    "HMIS 105 monthly outpatient reporting",
                    "Administrative geography",
                ],
                "produces": "Scored, explained, investigable surveillance signals.",
                "permitted_language": [
                    "potential treatment-response signal",
                    "repeat-positive pattern",
                    "unusual recurrence pattern",
                    "epidemiological signal requiring investigation",
                    "priority resistance-surveillance signal",
                ],
                "boundary": (
                    "Routine data cannot distinguish recrudescence from reinfection, "
                    "prove drug exposure or adherence, identify parasite genotype, or "
                    "confirm molecular resistance markers. A signal from this lane "
                    "means investigate this pattern, not resistance confirmed."
                ),
            },
            {
                "id": "confirmed_evidence",
                "label": "Externally confirmed finding",
                "sources": [
                    "Therapeutic efficacy studies",
                    "Molecular marker results",
                    "Validated laboratory confirmation",
                ],
                "produces": (
                    "Confirmed findings under separate governance, attached to a "
                    "routine signal as corroborating evidence."
                ),
                "boundary": (
                    "This is the only lane whose findings may use confirmatory "
                    "language. It is never populated from routine data. Not "
                    "implemented in phases 1-2."
                ),
            },
        ],
        "implementation_status": {
            "routine_surveillance": "implemented_requires_approved_configuration",
            "confirmed_evidence": "not_implemented",
            "note": (
                "Routine-data engines, signals and deterministic explanations are "
                "implemented. They refuse governed judgements until approved method "
                "and configuration versions exist. The separate confirmed-evidence "
                "lane is not implemented."
            ),
        },
    }


@router.get(
    "/meta/assistant",
    response_model=dict[str, object],
    summary="Whether the optional Ask MARS assistant is available",
)
def assistant_availability(settings: SettingsDep) -> dict[str, object]:
    """Whether Ask MARS can answer on this deployment.

    Lives in ``meta`` rather than in the assistant's own router, and reads
    nothing but the feature flag. ADR 0008 requires ``mars.ai`` to be a leaf
    that a deployment can disable entirely, so the endpoint a client uses to
    discover the assistant must not itself import it - otherwise asking
    whether AI is available would load AI.

    When the flag is off the assistant's own routes are not registered at all.
    That is the honest contract: MARS does not advertise an endpoint it will
    not answer.
    """
    enabled = settings.ai_assistant_enabled
    return {
        "enabled": enabled,
        "endpoint": "/api/v1/ai/ask" if enabled else None,
        "detail": (
            "Ask MARS is enabled. Whether it can answer also depends on an "
            "approved model provider being registered; query /api/v1/ai/"
            "availability for that."
            if enabled
            else (
                "Ask MARS is switched off for this deployment. Every dashboard, "
                "signal, explanation, investigation and report works without "
                "it, and no assistant route is registered."
            )
        ),
    }
