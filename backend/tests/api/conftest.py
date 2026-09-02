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
    get_audit_service,
    get_configuration_service,
    get_current_principal,
    get_db_session,
    get_facility_service,
    get_geography_service,
    get_method_registry_service,
    get_organisation_service,
)
from mars.core.settings import Environment, Settings
from mars.main import create_app
from mars.security.principal import AuthenticatedPrincipal


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
    """Returns nothing. Phase 1-2 has no geography loaded."""

    def list_units(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def level_counts(self, *_a: Any, **_k: Any) -> dict[str, int]:
        from mars.domain.enums import GeographyLevel

        return {level.value: 0 for level in GeographyLevel}

    def list_boundary_versions(self) -> list[Any]:
        return []

    def find_by_alias(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


class FakeOrganisationService:
    def list_units(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


class FakeFacilityService:
    def list_facilities(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


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
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_test",
        dev_auth_enabled=True,
        dev_auth_secret="test-only-secret",
        log_format="console",
        cors_allow_origins=["http://localhost:5173"],
    )


@pytest.fixture
def audit_recorder() -> FakeAuditService:
    return FakeAuditService()


@pytest.fixture
def app(api_settings: Settings, audit_recorder: FakeAuditService) -> Iterator[FastAPI]:
    application = create_app(api_settings)

    application.dependency_overrides[get_db_session] = lambda: None
    application.dependency_overrides[get_audit_service] = lambda: audit_recorder
    application.dependency_overrides[get_geography_service] = FakeGeographyService
    application.dependency_overrides[get_organisation_service] = FakeOrganisationService
    application.dependency_overrides[get_facility_service] = FakeFacilityService
    application.dependency_overrides[get_configuration_service] = FakeConfigurationService
    application.dependency_overrides[get_method_registry_service] = FakeMethodRegistryService

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
