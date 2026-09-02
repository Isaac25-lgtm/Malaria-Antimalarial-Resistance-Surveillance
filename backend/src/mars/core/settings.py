"""Central application settings.

All configuration arrives through environment variables so the same image runs
unchanged in development, staging and production. Nothing here may contain a
machine-specific path, a credential default that would work in production, or a
value that silently enables a development affordance outside development.
"""

from __future__ import annotations

import enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, enum.Enum):
    """Deployment environment.

    ``production`` and ``staging`` are treated as protected: development-only
    affordances (notably the synthetic authentication mode) are refused there.
    """

    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_protected(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class Settings(BaseSettings):
    """Runtime configuration for the API and worker processes."""

    model_config = SettingsConfigDict(
        env_prefix="MARS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Identity of this deployment -------------------------------------
    environment: Environment = Environment.LOCAL
    app_name: str = "MARS"
    app_title: str = "MARS - Malaria Antimalarial Resistance Surveillance"
    api_v1_prefix: str = "/api/v1"

    # Populated by the build pipeline; never inferred from the local checkout.
    release_version: str = "0.1.0"
    git_sha: str = "unknown"
    build_timestamp: str | None = None

    # -- Database ---------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://mars:mars@localhost:5432/mars",
        description="SQLAlchemy URL. Must use the psycopg (v3) driver.",
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_pool_timeout_seconds: int = Field(default=10, ge=1, le=120)
    database_statement_timeout_ms: int = Field(default=30_000, ge=1_000)
    #: Seconds to wait for a TCP connection before giving up. Bounded so a
    #: readiness probe fails fast and legibly when the database is
    #: unreachable, rather than hanging until the orchestrator times it out.
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    database_echo: bool = False

    # -- Cache / queue ----------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # -- HTTP -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    request_id_header: str = "X-Request-ID"

    # -- Logging ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # -- Authentication ---------------------------------------------------
    # OIDC is the production path. The discovery document is read lazily so the
    # service starts even when the provider is briefly unreachable.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_cache_seconds: int = Field(default=3600, ge=60)

    # Development-only synthetic authentication. Guarded three ways: it must be
    # explicitly enabled, the environment must not be protected, and the
    # validator below refuses the combination outright.
    dev_auth_enabled: bool = False
    dev_auth_secret: str = Field(
        default="dev-only-not-a-production-secret",
        description="HMAC secret for synthetic development tokens. Never used in production.",
    )
    dev_auth_token_ttl_seconds: int = Field(default=28_800, ge=60)

    # -- Feature boundaries ----------------------------------------------
    # The optional AI assistant is out of scope for phases 1-2 and is disabled.
    # The core surveillance product must remain fully functional without it.
    ai_assistant_enabled: bool = False

    # -- Demonstration data ----------------------------------------------
    # When true the UI must visibly mark every screen as carrying synthetic
    # data. Refused in protected environments.
    demo_mode_enabled: bool = False

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            # Silently defaulting to psycopg2 would break the async-capable
            # driver assumptions elsewhere, so be explicit rather than helpful.
            raise ValueError(
                "database_url must name the driver explicitly, e.g. "
                "postgresql+psycopg://user:pass@host:5432/db"
            )
        if not value.startswith("postgresql+psycopg"):
            raise ValueError("MARS requires PostgreSQL with the psycopg (v3) driver")
        return value

    @model_validator(mode="after")
    def _guard_protected_environments(self) -> Settings:
        if self.environment.is_protected:
            if self.dev_auth_enabled:
                raise ValueError(
                    "dev_auth_enabled must be false in staging and production. "
                    "Synthetic authentication is a development affordance only."
                )
            if self.demo_mode_enabled:
                raise ValueError("demo_mode_enabled must be false in staging and production.")
            if not self.oidc_issuer:
                raise ValueError(
                    "oidc_issuer is required in staging and production; "
                    "there is no fallback authentication path."
                )
        return self

    @property
    def is_development_auth_active(self) -> bool:
        """True only when synthetic authentication may legitimately be used."""
        return self.dev_auth_enabled and not self.environment.is_protected

    @property
    def docs_enabled(self) -> bool:
        """Interactive API docs are not exposed in protected environments."""
        return not self.environment.is_protected


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Used by tests that vary the environment."""
    get_settings.cache_clear()
