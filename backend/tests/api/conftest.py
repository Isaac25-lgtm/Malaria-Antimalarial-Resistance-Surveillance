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
