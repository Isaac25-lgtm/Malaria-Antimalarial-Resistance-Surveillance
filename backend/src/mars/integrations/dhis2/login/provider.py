"""Authentication provider interface and the DHIS2 Basic-auth pilot.

Ministry OAuth, a personal access token, or a dedicated read-only service
account should replace ``Dhis2BasicAuthProvider`` without rewriting MARS
session or scope code. The rest of the application depends on
:class:`AuthenticationProvider` only.
"""

from __future__ import annotations

import httpx

from mars.core.settings import Settings
from mars.integrations.dhis2.login.allowlists import ALLOWED_HOSTS
from mars.integrations.dhis2.login.client import LoginClient
from mars.security.source_login import AuthenticationProvider, LoginSnapshot


class Dhis2BasicAuthProvider(AuthenticationProvider):
    """Pilot provider: HTTP Basic to an allowlisted DHIS2 origin over HTTPS.

    Temporary. Replace with PAT/OAuth/OIDC by implementing
    :class:`AuthenticationProvider`. Credentials are used for the metadata
    round-trip and then must be stored only in the in-memory credential
    holder, never in PostgreSQL, a file, a cookie or a log.
    """

    method = "dhis2_basic"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def authenticate(self, username: str, password: str) -> LoginSnapshot:
        with LoginClient(
            base_url=self._settings.dhis2_login_base_url,
            username=username,
            password=password,
            timeout_seconds=self._settings.dhis2_login_timeout_seconds,
            max_retries=self._settings.dhis2_login_max_retries,
            retry_backoff_seconds=self._settings.dhis2_login_retry_backoff_seconds,
            max_response_bytes=self._settings.dhis2_login_max_response_bytes,
            verify_tls=self._settings.dhis2_login_verify_tls,
            page_size=self._settings.dhis2_login_page_size,
            max_pages=self._settings.dhis2_login_max_pages,
            allowed_hosts=ALLOWED_HOSTS,
            transport=self._transport,
        ) as client:
            return client.fetch_login_metadata()


__all__ = ["AuthenticationProvider", "Dhis2BasicAuthProvider"]
