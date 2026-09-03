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
    meta,
    organisation,
    signals,
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
    router.include_router(signals.router)

    if settings.is_development_auth_active:
        router.include_router(auth.development_router)

    return router
