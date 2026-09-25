"""FastAPI application factory and API entry point."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
        auth_mode=settings.auth_mode,
        ai_assistant_enabled=settings.ai_assistant_enabled,
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
    dashboard = getattr(app.state, "live_dashboard", None)
    if dashboard is not None and hasattr(dashboard, "close"):
        dashboard.close()
    recurrence_executor = getattr(app.state, "recurrence_run_executor", None)
    if recurrence_executor is not None:
        recurrence_executor.close()
    live_sync_engine = getattr(app.state, "live_sync_engine", None)
    if live_sync_engine is not None:
        live_sync_engine.dispose()
    recurrence_engine = getattr(app.state, "recurrence_engine", None)
    if recurrence_engine is not None and recurrence_engine is not live_sync_engine:
        recurrence_engine.dispose()
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
        if settings.identity_encryption_key is not None:
            from mars.api.v1.schemas import LiveDashboardSnapshot
            from mars.db.session import create_session_factory
            from mars.services.clinical_evidence_store import ClinicalEvidenceStore, EvidenceCipher
            from mars.services.durable_live_dashboard import (
                DurableLiveDashboardService,
                EvidenceSink,
            )
            from mars.services.live_sync_store import LiveSyncStore
            from mars.services.recurrence_analysis_service import RecurrenceRunExecutor

            project_root = Path(__file__).resolve().parents[3]
            mapping_path = project_root / "config" / "dhis2" / "pader-live-v1.json"
            try:
                mapping_version = hashlib.sha256(mapping_path.read_bytes()).hexdigest()
            except OSError as error:
                raise RuntimeError(
                    "Live synchronization mapping is missing or unreadable: "
                    "config/dhis2/pader-live-v1.json"
                ) from error
            live_sync_engine, live_sync_sessions = create_session_factory(settings)
            app.state.live_sync_engine = live_sync_engine
            recurrence_cipher = EvidenceCipher(
                settings.identity_encryption_key.get_secret_value(),
                settings.identity_encryption_key_version,
            )
            app.state.recurrence_cipher = recurrence_cipher
            evidence_sink: EvidenceSink | None = None

            if settings.patient_display_key is not None:
                display_key = settings.patient_display_key.get_secret_value().encode("utf-8")
                app.state.recurrence_run_executor = RecurrenceRunExecutor(
                    live_sync_sessions,
                    settings,
                    recurrence_cipher,
                    display_key,
                    max_workers=settings.recurrence_run_workers,
                )

                def retain_live_evidence(
                    scope_key: str,
                    _period_start: Any,
                    _period_end: Any,
                    retained: Any,
                ) -> None:
                    evidence, coverage = retained
                    with live_sync_sessions() as session, session.begin():
                        ClinicalEvidenceStore(session, recurrence_cipher).publish(
                            encounters=evidence.encounters,
                            coverage=coverage,
                            source_kind="live_tracker",
                            namespace=evidence.namespace,
                            scope_key=scope_key,
                            facility_refs=sorted(coverage.requested_facility_refs),
                            mapping_version=evidence.mapping_version,
                            created_by="live-sync",
                            unlinked_medications=evidence.unlinked_medications,
                        )

                evidence_sink = retain_live_evidence

            def live_context(raw_id: str) -> dict[str, Any] | None:
                live_session = app.state.live_session_store.get(raw_id)
                facilities = app.state.live_metadata_discovery.tracker_facilities(raw_id)
                if live_session is None or not facilities:
                    return None
                principal = live_session.principal
                return {
                    "source": settings.dhis2_login_base_url,
                    "subject": principal.subject,
                    "permissions": sorted(str(p) for p in principal.permissions),
                    "sensitivity": str(principal.max_sensitivity),
                    "mapping_version": mapping_version,
                    "display_key_version": hashlib.sha256(
                        settings.patient_display_key.get_secret_value().encode()
                    ).hexdigest()
                    if settings.patient_display_key
                    else "missing",
                    "facilities": sorted(facilities, key=lambda row: row["id"]),
                }

            app.state.live_dashboard = DurableLiveDashboardService(
                app.state.live_credential_holder,
                build_live_dashboard_runner(settings, project_root=project_root),
                LiveSyncStore(
                    live_sync_sessions,
                    settings.identity_encryption_key.get_secret_value(),
                ),
                live_context,
                lambda result: LiveDashboardSnapshot.model_validate(result).model_dump(mode="json"),
                evidence_sink=evidence_sink,
            )
        app.state.live_geography_lookup_factory = lambda session: SqlAlchemyGeographyLookup(
            session, Dhis2Crosswalk(session)
        )

    # Stored-source recurrence analysis is also available in OIDC/demo
    # deployments when the protected keys are configured. Live mode already
    # owns a background session factory above, so do not create a second pool.
    if (
        settings.identity_encryption_key is not None
        and settings.patient_display_key is not None
        and not hasattr(app.state, "recurrence_run_executor")
    ):
        from mars.db.session import create_session_factory
        from mars.services.clinical_evidence_store import EvidenceCipher
        from mars.services.recurrence_analysis_service import RecurrenceRunExecutor

        recurrence_engine, recurrence_sessions = create_session_factory(settings)
        recurrence_cipher = EvidenceCipher(
            settings.identity_encryption_key.get_secret_value(),
            settings.identity_encryption_key_version,
        )
        app.state.recurrence_engine = recurrence_engine
        app.state.recurrence_cipher = recurrence_cipher
        app.state.recurrence_run_executor = RecurrenceRunExecutor(
            recurrence_sessions,
            settings,
            recurrence_cipher,
            settings.patient_display_key.get_secret_value().encode("utf-8"),
            max_workers=settings.recurrence_run_workers,
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
