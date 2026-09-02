"""Shared test fixtures.

Two tiers of test run here:

* **Unit and API tests** need no database. They exercise pure logic and, for the
  API, an application whose data dependencies are overridden with in-memory
  fakes. These run everywhere.
* **Integration tests** need a live PostgreSQL. They are marked ``integration``
  and skip automatically when ``MARS_TEST_DATABASE_URL`` is unset, so an absent
  database produces a reported skip rather than a false pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from mars.core.settings import Environment, Settings, reset_settings_cache
from mars.security.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    SensitivityLevel,
    SystemRole,
)
from mars.security.principal import AuthenticatedPrincipal, GeographyScope

#: Set to a PostgreSQL URL to enable integration tests.
TEST_DATABASE_URL_ENV = "MARS_TEST_DATABASE_URL"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a live PostgreSQL/PostGIS database")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep settings isolated between tests that vary the environment."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def dev_settings() -> Settings:
    """Settings for a local development deployment with synthetic auth on."""
    return Settings(
        environment=Environment.LOCAL,
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_test",
        dev_auth_enabled=True,
        dev_auth_secret="test-only-secret",
        log_format="console",
    )


@pytest.fixture(scope="session")
def integration_database_url() -> str:
    """Skip the test when no integration database is configured."""
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip(
            f"{TEST_DATABASE_URL_ENV} is not set; PostgreSQL integration tests are skipped."
        )
    return url


# ---------------------------------------------------------------------------
# Principal builders.
#
# Every principal produced here is synthetic. The geography paths mirror the
# hierarchy the Prompt 5 importer will build (UG / region / district / ...) so
# that scope containment is exercised against realistic values.
# ---------------------------------------------------------------------------
COUNTRY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
GULU_ID = uuid.UUID("00000000-0000-4000-8000-000000000304")
PADER_ID = uuid.UUID("00000000-0000-4000-8000-000000000312")
GULU_FACILITY_ID = uuid.UUID("00000000-0000-4000-8000-00000000f001")
PADER_FACILITY_ID = uuid.UUID("00000000-0000-4000-8000-00000000f002")

COUNTRY_SCOPE = GeographyScope(
    geography_unit_id=COUNTRY_ID,
    preferred_code="UG",
    level="country",
    name="Uganda",
    path="UG",
)
GULU_SCOPE = GeographyScope(
    geography_unit_id=GULU_ID,
    preferred_code="304",
    level="district",
    name="GULU",
    path="UG/3/304",
)
PADER_SCOPE = GeographyScope(
    geography_unit_id=PADER_ID,
    preferred_code="312",
    level="district",
    name="PADER",
    path="UG/3/312",
)


def make_principal(
    *,
    role: SystemRole,
    scopes: tuple[GeographyScope, ...] = (),
    facility_scopes: frozenset[uuid.UUID] = frozenset(),
    max_sensitivity: SensitivityLevel | None = None,
    permissions: frozenset[Permission] | None = None,
    username: str | None = None,
) -> AuthenticatedPrincipal:
    """Build a synthetic principal with the default grants for ``role``."""
    from mars.security.permissions import PERMISSION_CATALOGUE, ROLE_DEFAULT_SENSITIVITY

    sensitivity = max_sensitivity or ROLE_DEFAULT_SENSITIVITY[role]
    granted = permissions if permissions is not None else ROLE_PERMISSIONS[role]

    # Mirror AuthService: a permission above the caller's sensitivity ceiling is
    # dropped rather than silently upgrading them.
    usable = frozenset(
        p for p in granted if sensitivity.covers(PERMISSION_CATALOGUE[p].minimum_sensitivity)
    )

    return AuthenticatedPrincipal(
        user_id=uuid.uuid4(),
        subject=f"dev:{username or role.value}",
        username=username or role.value,
        display_name=f"{role.value} (synthetic)",
        roles=frozenset({role.value}),
        permissions=usable,
        max_sensitivity=sensitivity,
        geography_scopes=scopes,
        facility_scopes=facility_scopes,
        session_reference=uuid.uuid4().hex,
        auth_method="development",
        is_synthetic=True,
    )


@pytest.fixture
def national_principal() -> AuthenticatedPrincipal:
    return make_principal(
        role=SystemRole.NATIONAL_PROGRAMME,
        scopes=(COUNTRY_SCOPE,),
        username="national.programme",
    )


@pytest.fixture
def gulu_district_principal() -> AuthenticatedPrincipal:
    return make_principal(
        role=SystemRole.DISTRICT_HSD, scopes=(GULU_SCOPE,), username="district.gulu"
    )


@pytest.fixture
def pader_district_principal() -> AuthenticatedPrincipal:
    return make_principal(
        role=SystemRole.DISTRICT_HSD, scopes=(PADER_SCOPE,), username="district.pader"
    )


@pytest.fixture
def gulu_facility_principal() -> AuthenticatedPrincipal:
    return make_principal(
        role=SystemRole.FACILITY,
        scopes=(GULU_SCOPE,),
        facility_scopes=frozenset({GULU_FACILITY_ID}),
        username="facility.gulu",
    )


@pytest.fixture
def analyst_principal() -> AuthenticatedPrincipal:
    return make_principal(role=SystemRole.ANALYST, scopes=(COUNTRY_SCOPE,), username="analyst")


@pytest.fixture
def administrator_principal() -> AuthenticatedPrincipal:
    return make_principal(
        role=SystemRole.ADMINISTRATOR, scopes=(COUNTRY_SCOPE,), username="administrator"
    )


@pytest.fixture
def unscoped_principal() -> AuthenticatedPrincipal:
    """A provisioned account with no geography scope.

    Represents a misconfiguration. Must be able to read nothing.
    """
    return make_principal(role=SystemRole.DISTRICT_HSD, scopes=(), username="unscoped.user")


@pytest.fixture
def all_principals(
    national_principal: AuthenticatedPrincipal,
    gulu_district_principal: AuthenticatedPrincipal,
    gulu_facility_principal: AuthenticatedPrincipal,
    analyst_principal: AuthenticatedPrincipal,
    administrator_principal: AuthenticatedPrincipal,
) -> dict[str, Any]:
    return {
        SystemRole.NATIONAL_PROGRAMME: national_principal,
        SystemRole.DISTRICT_HSD: gulu_district_principal,
        SystemRole.FACILITY: gulu_facility_principal,
        SystemRole.ANALYST: analyst_principal,
        SystemRole.ADMINISTRATOR: administrator_principal,
    }
