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

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # -- Identity linkage -------------------------------------------------
    #
    # The HMAC secret that derives patient linkage tokens. Held as a SecretStr
    # so it does not appear in a repr, a log line, a traceback or a settings
    # dump, and defaulted to None rather than to a placeholder: a deployment
    # that forgets it must fail loudly, not derive every token in the country
    # under a value an attacker can read in the source.
    identity_linkage_key: SecretStr | None = Field(
        default=None,
        description=(
            "HMAC secret for patient linkage tokens. Supplied through the "
            "environment only. Never stored, logged or returned."
        ),
    )
    #: Recorded on every token so rotation does not orphan existing links.
    identity_linkage_key_version: str = Field(default="v1", min_length=1, max_length=16)

    #: Retired keys, as "version:secret" entries, so a token derived under an
    #: earlier version can still be recomputed while rotation is in progress.
    identity_linkage_retired_keys: SecretStr | None = Field(
        default=None,
        description=(
            "Retired linkage keys as version:secret pairs separated by commas. "
            "Used to verify existing tokens during rotation, never to derive new ones."
        ),
    )

    #: Connection URL for the identity service, using the identity database
    #: role. Deliberately separate from ``database_url``: the ordinary
    #: application must not merely decline to query identity, it must connect as
    #: a role that cannot. Absent in a deployment that runs no identity
    #: component, which then reports identity unready rather than falling back
    #: to the application connection.
    identity_database_url: str | None = Field(
        default=None,
        description=(
            "SQLAlchemy URL for the identity service, as the identity database "
            "role. Never the same credentials as database_url."
        ),
    )

    #: AES-256 key for identity encryption at rest, as 64 hex characters or 44
    #: base64 characters. Separate from the linkage key above: one lets you read
    #: stored identifiers, the other lets you test a guessed one, and neither
    #: should substitute for the other.
    identity_encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "AES-256 key for identity encryption at rest, hex or base64. "
            "Supplied through the environment only."
        ),
    )
    identity_encryption_key_version: str = Field(default="v1", min_length=1, max_length=16)
    #: Retired encryption keys, as "version:secret" pairs, so rows written under
    #: an earlier key stay readable while rotation runs.
    identity_encryption_retired_keys: SecretStr | None = Field(
        default=None,
        description=(
            "Retired encryption keys as version:secret pairs separated by "
            "commas. Used to decrypt existing rows, never to encrypt new ones."
        ),
    )

    # -- Feature boundaries ----------------------------------------------
    # The optional AI assistant is out of scope for phases 1-2 and is disabled.
    # The core surveillance product must remain fully functional without it.
    ai_assistant_enabled: bool = False

    # -- Demonstration data ----------------------------------------------
    # When true the UI must visibly mark every screen as carrying synthetic
    # data. Refused in protected environments.
    demo_mode_enabled: bool = False

    # -- DHIS2 exchange ---------------------------------------------------
    # Disabled by default and unconfigured by default. A deployment that has
    # not been given a URL and credentials must report the integration as
    # unconfigured, not fail at the first request and not quietly do nothing.
    dhis2_enabled: bool = False

    dhis2_base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the DHIS2 instance, e.g. https://dhis2.example.org. "
            "Supplied through the environment only."
        ),
    )
    dhis2_username: str | None = Field(default=None, max_length=128)
    #: Held as a SecretStr so it cannot be printed by an accidental repr, and
    #: never written to a log, an audit record or an integration run row.
    dhis2_password: SecretStr | None = Field(default=None)
    #: Personal access token, an alternative to username/password. If both are
    #: present the token wins, because a token can be scoped and revoked.
    dhis2_token: SecretStr | None = Field(default=None)

    dhis2_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    dhis2_max_retries: int = Field(default=3, ge=0, le=10)
    dhis2_retry_backoff_seconds: float = Field(default=1.0, ge=0, le=60)
    dhis2_page_size: int = Field(default=500, ge=1, le=10_000)

    #: Refuse a response larger than this rather than loading it into memory. A
    #: DHIS2 analytics request with a careless dimension can return hundreds of
    #: megabytes, and an ingestion process that dies on memory takes the whole
    #: worker with it.
    dhis2_max_response_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    #: TLS verification. True by default and separately settable so that a
    #: deployment disabling it has to say so explicitly, in writing, in its
    #: environment - where a reviewer can see it.
    dhis2_verify_tls: bool = True

    #: Outbound writes to DHIS2. Off by default and independent of
    #: ``dhis2_enabled``: reading another system's data and writing into it are
    #: different authorities, and MARS must not acquire the second by being
    #: granted the first.
    dhis2_push_enabled: bool = False
    dhis2_push_dataset_uid: str | None = Field(default=None, max_length=32)

    # -- DHIS2 metadata discovery ---------------------------------------
    # Separate from exchange. A GET-restricted discovery token must not be
    # confused with credentials that can pull patient collections or push data.
    dhis2_discovery_base_url: str | None = Field(
        default=None,
        description="HTTPS origin for metadata-only DHIS2 discovery.",
    )
    dhis2_discovery_username: str | None = Field(default=None, max_length=128)
    dhis2_discovery_password: SecretStr | None = Field(default=None)
    dhis2_discovery_token: SecretStr | None = Field(default=None)
    dhis2_discovery_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    dhis2_discovery_max_retries: int = Field(default=2, ge=0, le=5)
    dhis2_discovery_retry_backoff_seconds: float = Field(default=1.0, ge=0, le=60)
    dhis2_discovery_page_size: int = Field(default=200, ge=1, le=1000)
    dhis2_discovery_max_pages: int = Field(default=40, ge=1, le=200)
    dhis2_discovery_max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    dhis2_discovery_verify_tls: bool = True
    dhis2_discovery_output_dir: str = "data/discovery"

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
