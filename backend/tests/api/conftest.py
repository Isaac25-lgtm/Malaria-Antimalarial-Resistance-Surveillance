"""API test harness.

Builds the real application and overrides only the dependencies that reach a
database. The routers, middleware, exception handlers, authentication and
authorisation dependencies under test are the production ones.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mars.api.dependencies import (
    get_analytics_query_service,
    get_audit_service,
    get_configuration_service,
    get_current_principal,
    get_db_session,
    get_facility_service,
    get_geography_map_service,
    get_geography_service,
    get_investigation_service,
    get_method_registry_service,
    get_organisation_service,
    get_report_service,
    get_signal_query_service,
    get_surveillance_summary_service,
)
from mars.core.settings import Environment, Settings
from mars.investigations.service import InvestigationService
from mars.main import create_app
from mars.security.principal import AuthenticatedPrincipal
from mars.services.report_service import ReportService
from mars.services.surveillance_summary import SurveillanceSummaryService


class FakeAuditService:
    """Records calls in memory so tests can assert on what was audited."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def record_denial(self, **kwargs: Any) -> None:
        self.events.append({"denial": True, **kwargs})

    def query(self, **_kwargs: Any) -> list[Any]:
        return []

    def actions(self) -> list[str]:
        return [
            e["action"].value if hasattr(e.get("action"), "value") else str(e.get("action"))
            for e in self.events
        ]

    def denials(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("denial")]


class FakeGeographyService:
    """An empty hierarchy.

    Every lookup raises the same NotFoundError the real service raises for an
    out-of-scope unit, which is what lets the API tests assert that "hidden"
    and "absent" are indistinguishable without needing a database.
    """

    def list_units(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def level_counts(self, *_a: Any, **_k: Any) -> dict[str, int]:
        from mars.domain.enums import GeographyLevel

        return {level.value: 0 for level in GeographyLevel}

    def list_boundary_versions(self) -> list[Any]:
        return []

    def find_by_alias(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def get_unit(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("geography unit not found or outside your assigned scope")

    def get_unit_by_code(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("no visible unit with that code in your assigned scope")

    def children_of(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def ancestors_of(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


class FakeGeographyMapService:
    """No boundary version published.

    This is the state a fresh deployment is in before the importer has run, and
    the API must answer it as "nothing loaded" rather than as an error.
    """

    def map_metadata(self, *_a: Any, **_k: Any) -> Any:
        from datetime import UTC, datetime

        from mars.services.geography_map_service import MAX_FEATURES, MapMetadata

        return MapMetadata(
            boundary_version_id=None,
            boundary_version_code=None,
            boundary_version_label=None,
            source_name=None,
            source_checksum=None,
            imported_at=None,
            initial_bounds=None,
            initial_unit_id=None,
            initial_unit_name=None,
            initial_unit_level=None,
            levels=[],
            geometry_resolution="simplified",
            max_features=MAX_FEATURES,
            is_available=False,
            generated_at=datetime.now(UTC),
        )

    def feature_collection(self, *_a: Any, **_k: Any) -> Any:
        from mars.services.geography_map_service import FeatureCollection

        return FeatureCollection(level=_k.get("level").value if _k.get("level") else None)

    def context_collection(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import FieldError, ValidationFailedError
        from mars.domain.enums import GeographyLevel
        from mars.services.geography_map_service import (
            NATIONAL_LAYER_LEVELS,
            FeatureCollection,
        )

        level = _k.get("level")
        if level is not None and level not in NATIONAL_LAYER_LEVELS:
            raise ValidationFailedError(
                "The context layer is country, region or district only.",
                errors=[
                    FieldError(
                        field="level",
                        message="Request district (or region/country) context, not a finer grain.",
                        code="unsupported_context_level",
                    )
                ],
            )
        return FeatureCollection(level=level.value if isinstance(level, GeographyLevel) else None)

    def unit_geometry(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("geography unit not found or outside your assigned scope")

    def unit_bounds(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("geography unit not found or outside your assigned scope")


class FakeOrganisationService:
    def list_units(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


class FakeFacilityService:
    def list_facilities(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def get_facility(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("facility not found or outside your assigned scope")


class FakeAnalyticsQueryService:
    def geography_ids(self, principal: AuthenticatedPrincipal) -> set[Any] | None:
        return None if principal.has_national_scope else set(principal.scope_unit_ids())

    def facility_ids(self, principal: AuthenticatedPrincipal) -> set[Any] | None:
        return None if principal.has_national_scope else set(principal.facility_scopes)

    def episodes(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def aggregate_results(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def commodity_alerts(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


class FakeSignalQueryService:
    def list(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def get(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("signal not found or outside your assigned scope")

    def explanation(self, *_a: Any, **_k: Any) -> Any:
        from mars.core.errors import NotFoundError

        raise NotFoundError("no explanation has been generated for this signal")


class FakeConfigurationService:
    def list_keys(self) -> list[Any]:
        return []


class FakeMethodRegistryService:
    def active_versions(self) -> list[Any]:
        return []

    def list_methods(self) -> list[Any]:
        return []


@pytest.fixture
def api_settings() -> Settings:
    return Settings(
        environment=Environment.LOCAL,
        auth_mode="demo",
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_test",
        dev_auth_enabled=True,
        dev_auth_secret="test-only-secret",
        log_format="console",
        cors_allow_origins=["http://localhost:5173"],
    )


@pytest.fixture
def audit_recorder() -> FakeAuditService:
    return FakeAuditService()


class EmptyResult:
    """A query result over an empty database.

    Used so the composed services can run their real code in API tests. An
    empty database is the state a fresh deployment is in, and it is the state
    whose honest reporting these tests are checking.
    """

    def scalars(self) -> EmptyResult:
        return self

    def all(self) -> list[object]:
        return []

    def scalar_one(self) -> int:
        return 0

    def scalar_one_or_none(self) -> object | None:
        return None

    def __iter__(self):
        return iter(())


class EmptySession:
    def execute(self, _statement: object) -> EmptyResult:
        return EmptyResult()

    def add(self, _instance: object) -> None:
        return None

    def flush(self) -> None:
        return None


@pytest.fixture
def app(api_settings: Settings, audit_recorder: FakeAuditService) -> Iterator[FastAPI]:
    application = create_app(api_settings)

    application.dependency_overrides[get_db_session] = lambda: None
    application.dependency_overrides[get_audit_service] = lambda: audit_recorder
    application.dependency_overrides[get_geography_service] = FakeGeographyService
    application.dependency_overrides[get_geography_map_service] = FakeGeographyMapService
    application.dependency_overrides[get_organisation_service] = FakeOrganisationService
    application.dependency_overrides[get_facility_service] = FakeFacilityService
    application.dependency_overrides[get_configuration_service] = FakeConfigurationService
    application.dependency_overrides[get_method_registry_service] = FakeMethodRegistryService
    application.dependency_overrides[get_analytics_query_service] = FakeAnalyticsQueryService
    application.dependency_overrides[get_signal_query_service] = FakeSignalQueryService
    # The composed services run their real logic against an empty stub
    # session, so the API tests exercise the genuine "not configured"
    # answers rather than a fake's idea of them.
    application.dependency_overrides[get_surveillance_summary_service] = (
        lambda: SurveillanceSummaryService(EmptySession())
    )
    application.dependency_overrides[get_report_service] = lambda: ReportService(
        EmptySession(), audit_recorder
    )
    application.dependency_overrides[get_investigation_service] = lambda: InvestigationService(
        EmptySession(), audit_recorder
    )

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(app: FastAPI):
    """Return a factory that authenticates the client as a given principal."""

    def _as(principal: AuthenticatedPrincipal) -> TestClient:
        app.dependency_overrides[get_current_principal] = lambda: principal
        return TestClient(app, raise_server_exceptions=False)

    return _as


# ---------------------------------------------------------------------------
# Database-state fixtures.
#
# Readiness is the one endpoint whose answer depends on a real connection.
# These fixtures force each condition explicitly, so the test result never
# depends on whether the developer happens to have a database running.
# ---------------------------------------------------------------------------
class _StubConnection:
    """Connection stand-in returning fixed answers to the readiness probe."""

    def __init__(self, *, postgis: bool) -> None:
        self._postgis = postgis

    def __enter__(self) -> _StubConnection:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        sql = str(statement)
        if "PostGIS_Lib_Version" in sql:
            if not self._postgis:
                raise RuntimeError("function postgis_lib_version() does not exist")
            return _StubResult("3.4.2")
        if "server_version" in sql:
            return _StubResult("16.4")
        return _StubResult(None)


class _StubResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _StubEngine:
    def __init__(self, *, reachable: bool, postgis: bool) -> None:
        self._reachable = reachable
        self._postgis = postgis

    def connect(self) -> _StubConnection:
        if not self._reachable:
            raise OSError("connection refused")
        return _StubConnection(postgis=self._postgis)


@pytest.fixture
def unreachable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The database cannot be reached at all."""
    monkeypatch.setattr(
        "mars.api.v1.health.get_engine", lambda: _StubEngine(reachable=False, postgis=False)
    )


@pytest.fixture
def reachable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostgreSQL and PostGIS both answer."""
    monkeypatch.setattr(
        "mars.api.v1.health.get_engine", lambda: _StubEngine(reachable=True, postgis=True)
    )


@pytest.fixture
def database_without_postgis(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostgreSQL answers; the PostGIS extension is not installed."""
    monkeypatch.setattr(
        "mars.api.v1.health.get_engine", lambda: _StubEngine(reachable=True, postgis=False)
    )
