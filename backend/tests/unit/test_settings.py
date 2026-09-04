"""Settings guardrails.

The protected-environment checks are the mechanism that stops a development
affordance reaching production. They are tested here rather than trusted.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from mars.core.settings import Environment, Settings

_VALID_URL = "postgresql+psycopg://mars:pw@db:5432/mars"
_LIVE_URL = "postgresql+psycopg://mars:pw@db:5432/mars_live"


@pytest.fixture(autouse=True)
def _clear_mars_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor tests must not inherit the operator's process environment."""
    for key in list(os.environ):
        if key.startswith("MARS_") and key != "MARS_TEST_DATABASE_URL":
            monkeypatch.delenv(key, raising=False)


class TestDatabaseUrlValidation:
    def test_accepts_psycopg_driver(self) -> None:
        settings = Settings(database_url=_VALID_URL)
        assert settings.database_url == _VALID_URL

    def test_rejects_bare_postgresql_scheme(self) -> None:
        """An unnamed driver would silently resolve to psycopg2."""
        with pytest.raises(ValidationError, match="name the driver explicitly"):
            Settings(database_url="postgresql://mars:pw@db:5432/mars")

    def test_rejects_non_postgresql_database(self) -> None:
        with pytest.raises(ValidationError, match="requires PostgreSQL"):
            Settings(database_url="sqlite:///mars.db")


class TestProtectedEnvironmentGuards:
    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_development_auth_is_refused(self, environment: Environment) -> None:
        with pytest.raises(ValidationError, match="dev_auth_enabled must be false"):
            Settings(
                environment=environment,
                database_url=_VALID_URL,
                oidc_issuer="https://id.example.org",
                dev_auth_enabled=True,
            )

    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_demo_mode_is_refused(self, environment: Environment) -> None:
        with pytest.raises(ValidationError, match="demo_mode_enabled must be false"):
            Settings(
                environment=environment,
                database_url=_VALID_URL,
                oidc_issuer="https://id.example.org",
                demo_mode_enabled=True,
            )

    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_oidc_issuer_is_required(self, environment: Environment) -> None:
        """There is no fallback authentication path in a protected environment."""
        with pytest.raises(ValidationError, match="oidc_issuer is required"):
            Settings(environment=environment, database_url=_VALID_URL)

    def test_protected_environments_disable_interactive_docs(self) -> None:
        settings = Settings(
            environment=Environment.PRODUCTION,
            database_url=_VALID_URL,
            oidc_issuer="https://id.example.org",
        )
        assert not settings.docs_enabled


class TestDevelopmentEnvironments:
    @pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.DEVELOPMENT])
    def test_development_auth_is_permitted(self, environment: Environment) -> None:
        settings = Settings(environment=environment, database_url=_VALID_URL, dev_auth_enabled=True)
        assert settings.is_development_auth_active

    def test_development_auth_inactive_when_not_enabled(self) -> None:
        settings = Settings(environment=Environment.LOCAL, database_url=_VALID_URL)
        assert not settings.is_development_auth_active

    def test_docs_enabled_locally(self) -> None:
        assert Settings(environment=Environment.LOCAL, database_url=_VALID_URL).docs_enabled


class TestLiveModeGuards:
    def test_live_mode_requires_mars_live_database(self) -> None:
        with pytest.raises(ValidationError, match="mars_local"):
            Settings(
                auth_mode="live",
                database_url="postgresql+psycopg://mars:pw@db:5432/mars_local",
                dev_auth_enabled=False,
                demo_mode_enabled=False,
                cors_allow_origins=["http://127.0.0.1:5173"],
            )

    def test_live_mode_refuses_demo_and_dev_auth(self) -> None:
        with pytest.raises(ValidationError, match="development authentication"):
            Settings(
                auth_mode="live",
                database_url=_LIVE_URL,
                dev_auth_enabled=True,
                cors_allow_origins=["http://127.0.0.1:5173"],
            )

    def test_live_mode_is_refused_in_production(self) -> None:
        with pytest.raises(ValidationError, match="local DHIS2 password pilot"):
            Settings(
                environment=Environment.PRODUCTION,
                auth_mode="live",
                database_url=_LIVE_URL,
                oidc_issuer="https://id.example.org",
                dev_auth_enabled=False,
                demo_mode_enabled=False,
                cors_allow_origins=["https://mars.example.org"],
            )

    def test_live_mode_is_active_locally(self) -> None:
        settings = Settings(
            auth_mode="live",
            database_url=_LIVE_URL,
            dev_auth_enabled=False,
            demo_mode_enabled=False,
            cors_allow_origins=["http://127.0.0.1:5173"],
        )
        assert settings.is_live_auth_active
        assert not settings.is_development_auth_active
        assert not settings.session_cookie_secure


class TestFeatureDefaults:
    def test_ai_assistant_is_off_by_default(self) -> None:
        """Core surveillance must work without generative AI."""
        assert not Settings(database_url=_VALID_URL).ai_assistant_enabled

    def test_demo_mode_is_off_by_default(self) -> None:
        assert not Settings(database_url=_VALID_URL).demo_mode_enabled
