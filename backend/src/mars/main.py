"""FastAPI application factory and API entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mars.api.exception_handlers import register_exception_handlers
from mars.api.middleware import (
    AccessLogMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from mars.api.v1.router import build_v1_router
from mars.core.logging import configure_logging, get_logger
from mars.core.settings import Settings, get_settings

DESCRIPTION = """\
MARS converts routine malaria data into explainable surveillance signals that a
named person is accountable for investigating.

**Scientific boundary.** Signals produced from routine e-register and HMIS data
indicate patterns requiring investigation. They do not confirm antimalarial
resistance. Routine data cannot distinguish recrudescence from reinfection,
prove drug exposure or adherence, identify parasite genotype, or confirm
molecular markers. Externally confirmed findings - therapeutic efficacy studies
and molecular results - are handled in a separate, separately governed lane. See
`/api/v1/meta/evidence-lanes`.

**Implementation status.** This build covers the foundation through Prompt 22:
DHIS2 exchange, governed indicators, patient episode and recurrence analysis,
testing/treatment/commodity surveillance, historical baselines, temporal and
spatial detection, governed signals, and deterministic explanations. Fresh
deployments remain analytically unconfigured until programme-approved method
and configuration versions are activated. Investigation workflows and the
complete dashboard experience belong to later prompts.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    The database is deliberately not contacted at startup. A transient database
    outage must not prevent the API from starting and reporting itself unready;
    readiness is a probe, not a boot requirement.
    """
    settings: Settings = app.state.settings
    logger = get_logger("mars.lifespan")
    logger.info(
        "api_starting",
        environment=settings.environment.value,
        release_version=settings.release_version,
        development_auth=settings.is_development_auth_active,
        ai_assistant_enabled=settings.ai_assistant_enabled,
    )
    if settings.is_development_auth_active:
        logger.warning(
            "development_auth_active",
            detail="Synthetic authentication is enabled. Non-production only.",
        )
    yield
    logger.info("api_stopping")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_title,
        description=DESCRIPTION,
        version=settings.release_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        contact={"name": "MARS implementation team"},
    )
    app.state.settings = settings

    # Dependencies resolve settings through get_settings(), which reads the
    # environment. When an explicit Settings object is supplied - by tests, or by
    # an embedding process - it must be what the whole application sees, or the
    # app would silently run on a different configuration than it was given.
    app.dependency_overrides[get_settings] = lambda: settings

    # Order matters: request context must be established before access logging
    # so every log line carries the request identifier.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware, settings=settings)

    if settings.cors_allow_origins:
        # A wildcard origin with credentials is rejected by browsers and would
        # be a mistake in any case: it would let any site read a district
        # officer's surveillance data using their session. Refused loudly
        # rather than silently narrowed, so a misconfiguration cannot ship.
        if "*" in settings.cors_allow_origins:
            raise RuntimeError(
                "cors_allow_origins may not contain '*': MARS sends credentials "
                "with cross-origin requests, and a wildcard origin would let any "
                "site read surveillance data using a signed-in user's session."
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", settings.request_id_header],
            expose_headers=[settings.request_id_header],
        )

    register_exception_handlers(app)
    app.include_router(build_v1_router(settings), prefix=settings.api_v1_prefix)

    return app


app = create_app()


def run() -> None:  # pragma: no cover - process entry point
    """Console-script entry point for the API service."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "mars.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )
