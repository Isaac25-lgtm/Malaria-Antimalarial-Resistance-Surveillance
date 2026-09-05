"""FastAPI application factory and API entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mars.api.exception_handlers import register_exception_handlers
from mars.api.middleware import (
    AccessLogMiddleware,
    LiveRequestSecurityMiddleware,
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

**Implementation status.** DHIS2 exchange, governed indicators, patient episode
and recurrence analysis, testing/treatment/commodity surveillance, historical
baselines, temporal and spatial detection, governed signals, deterministic
explanations, the national command centre with district and facility
workspaces, governed reports, and the investigation workflow and action centre.
The optional Ask MARS assistant is present but switched off, with no model
provider registered.

Fresh deployments remain analytically unconfigured until programme-approved
method and configuration versions are activated: every measure reports as not
configured and names what is missing, rather than reporting zero.
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
    if settings.is_live_auth_active:
        logger.warning(
            "live_auth_active",
            detail=(
                "eRegisters password-pilot authentication is enabled. "
                "Sessions and upstream credentials are in-process memory only."
            ),
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
    if settings.is_live_auth_active:
        from mars.integrations.dhis2.discovery.live import build_live_discovery_runner
        from mars.integrations.dhis2.live_dashboard import build_live_dashboard_runner
        from mars.integrations.dhis2.login.provider import Dhis2BasicAuthProvider
        from mars.integrations.dhis2.mapping import Dhis2Crosswalk
        from mars.integrations.dhis2.tracker.live import build_live_tracker_preview_runner
        from mars.security.live_session import InMemoryCredentialHolder, InMemorySessionStore
        from mars.security.login_throttle import LoginThrottle
        from mars.services.live_dashboard import LiveDashboardService
        from mars.services.live_discovery import LiveMetadataDiscoveryService
        from mars.services.live_scope import SqlAlchemyGeographyLookup
        from mars.services.live_tracker import LiveTrackerPreviewService

        app.state.live_session_store = InMemorySessionStore(
            idle_seconds=settings.session_idle_seconds,
            absolute_seconds=settings.session_absolute_seconds,
        )
        app.state.live_credential_holder = InMemoryCredentialHolder()
        app.state.login_throttle = LoginThrottle(
            max_attempts=settings.login_throttle_max_attempts,
            window_seconds=settings.login_throttle_window_seconds,
            secret=settings.login_throttle_secret,
        )
        app.state.dhis2_login_provider = Dhis2BasicAuthProvider(settings)
        discovery_output = Path(__file__).resolve().parents[3] / "data" / "discovery"
        app.state.live_metadata_discovery = LiveMetadataDiscoveryService(
            app.state.live_credential_holder,
            build_live_discovery_runner(settings, output_dir=discovery_output),
        )
        app.state.live_tracker_preview = LiveTrackerPreviewService(
            app.state.live_credential_holder,
            build_live_tracker_preview_runner(
                settings,
                project_root=Path(__file__).resolve().parents[3],
            ),
        )
        app.state.live_dashboard = LiveDashboardService(
            app.state.live_credential_holder,
            build_live_dashboard_runner(
                settings,
                project_root=Path(__file__).resolve().parents[3],
            ),
        )
        app.state.live_geography_lookup_factory = lambda session: SqlAlchemyGeographyLookup(
            session, Dhis2Crosswalk(session)
        )

    # Dependencies resolve settings through get_settings(), which reads the
    # environment. When an explicit Settings object is supplied - by tests, or by
    # an embedding process - it must be what the whole application sees, or the
    # app would silently run on a different configuration than it was given.
    app.dependency_overrides[get_settings] = lambda: settings

    # Order matters: request context must be established before access logging
    # so every log line carries the request identifier.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(LiveRequestSecurityMiddleware, settings=settings)
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
            allow_headers=[
                "Authorization",
                "Content-Type",
                settings.request_id_header,
                settings.csrf_header_name,
            ],
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
