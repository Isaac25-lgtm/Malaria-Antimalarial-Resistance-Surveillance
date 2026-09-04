"""Resolved configuration for metadata-only DHIS2 discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mars.core.settings import Settings
from mars.core.urls import strip_url_credentials
from mars.integrations.dhis2.discovery.allowlists import ALLOWED_HOSTS


class DiscoveryConfigError(ValueError):
    """The discovery utility was asked to run with an unsafe configuration."""


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    """Everything the discovery client needs, with credentials held privately.

    Built through :meth:`from_settings` so the CLI and tests share one
    validation path: HTTPS, no userinfo, hostname allowlist, TLS on by default.
    """

    base_url: str
    username: str | None
    password: str | None
    token: str | None
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    page_size: int
    max_pages: int
    max_response_bytes: int
    verify_tls: bool
    output_dir: Path
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS

    def __repr__(self) -> str:
        return (
            f"DiscoveryConfig(base_url={self.base_url!r}, "
            f"auth={'token' if self.token else 'basic' if self.username else 'none'}, "
            f"verify_tls={self.verify_tls!r})"
        )

    @property
    def origin_host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.token or (self.username and self.password))

    @classmethod
    def from_settings(cls, settings: Settings) -> DiscoveryConfig | None:
        """The config, or ``None`` when discovery has not been given a URL."""
        if not settings.dhis2_discovery_base_url:
            return None
        return cls.from_url(
            settings.dhis2_discovery_base_url,
            username=settings.dhis2_discovery_username,
            password=(
                settings.dhis2_discovery_password.get_secret_value()
                if settings.dhis2_discovery_password
                else None
            ),
            token=(
                settings.dhis2_discovery_token.get_secret_value()
                if settings.dhis2_discovery_token
                else None
            ),
            timeout_seconds=settings.dhis2_discovery_timeout_seconds,
            max_retries=settings.dhis2_discovery_max_retries,
            retry_backoff_seconds=settings.dhis2_discovery_retry_backoff_seconds,
            page_size=settings.dhis2_discovery_page_size,
            max_pages=settings.dhis2_discovery_max_pages,
            max_response_bytes=settings.dhis2_discovery_max_response_bytes,
            verify_tls=settings.dhis2_discovery_verify_tls,
            output_dir=Path(settings.dhis2_discovery_output_dir),
        )

    @classmethod
    def from_url(
        cls,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        page_size: int = 200,
        max_pages: int = 40,
        max_response_bytes: int = 8 * 1024 * 1024,
        verify_tls: bool = True,
        output_dir: Path | str = "data/discovery",
        allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    ) -> DiscoveryConfig:
        stripped = strip_url_credentials(base_url) or ""
        parts = urlsplit(base_url)
        if parts.username or parts.password:
            raise DiscoveryConfigError(
                "The discovery base URL must not contain userinfo. Supply a token "
                "or username and password through environment variables instead."
            )
        if parts.scheme.lower() != "https":
            raise DiscoveryConfigError(
                "DHIS2 discovery is HTTPS-only. HTTP and other schemes are refused."
            )
        host = (parts.hostname or "").lower()
        if host not in allowed_hosts:
            raise DiscoveryConfigError(
                "The discovery hostname is not on the Ministry allowlist. "
                "Permitted hosts are hmis.health.go.ug and eregisters.health.go.ug."
            )
        if not verify_tls:
            raise DiscoveryConfigError(
                "DHIS2 discovery refuses to disable TLS verification. A metadata "
                "session without certificate checks is not a metadata session."
            )
        return cls(
            base_url=stripped,
            username=username,
            password=password,
            token=token,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            page_size=page_size,
            max_pages=max_pages,
            max_response_bytes=max_response_bytes,
            verify_tls=verify_tls,
            output_dir=Path(output_dir),
            allowed_hosts=allowed_hosts,
        )
