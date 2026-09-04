"""Assembly of the v1 API surface."""

from __future__ import annotations

from fastapi import APIRouter

from mars.api.v1 import (
    analytics,
    auth,
    geography,
    governance,
    health,
    indicators,
    integrations,
    investigations,
    meta,
    organisation,
    reports,
    signals,
    surveillance,
)
from mars.core.settings import Settings


def build_v1_router(settings: Settings) -> APIRouter:
    """Compose the v1 router for this deployment.

    Development authentication routes are attached only when synthetic
    authentication is active, so they never appear in a production OpenAPI
    document.
    """
    router = APIRouter()

    router.include_router(health.router)
    router.include_router(meta.router)
    router.include_router(auth.router)
    router.include_router(geography.router)
    router.include_router(organisation.router)
    router.include_router(governance.router)
    router.include_router(indicators.router)
    router.include_router(integrations.router)
    router.include_router(analytics.router)
    router.include_router(surveillance.router)
    router.include_router(reports.router)
    router.include_router(signals.router)
    router.include_router(investigations.router)

    if settings.ai_assistant_enabled:
        # Imported inside the branch so that a deployment with the
        # assistant switched off never loads ``mars.ai`` at all. ADR 0008
        # asks for a leaf; this makes the claim testable at runtime rather
        # than only in the import graph.
        from mars.ai.api import router as ai_router

        router.include_router(ai_router)

    if settings.is_development_auth_active:
        router.include_router(auth.development_router)

    return router
