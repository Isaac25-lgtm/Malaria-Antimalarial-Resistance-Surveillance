"""Authentication provider abstraction.

MARS authenticates against an OIDC provider in staging and production. The
domain never learns which one: it receives a ``VerifiedIdentity`` and looks the
subject up locally. Swapping Keycloak for Entra ID or a Ministry SSO changes one
implementation and nothing else.

``OidcTokenVerifier`` verifies a signed JWT against the provider's published
JWKS. In live mode users sign in with eRegisters credentials and hold a cookie
session, so no bearer token is accepted at all. There is no synthetic or
development token path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from mars.core.errors import UnauthenticatedError
from mars.core.settings import Settings


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """The provider's assertion about who is calling.

    Deliberately minimal. Roles, scopes and permissions come from the MARS
    database, not from token claims, so an identity provider misconfiguration
    cannot grant surveillance access.
    """

    subject: str
    issuer: str
    username: str
    display_name: str
    email: str | None = None
    session_reference: str | None = None
    auth_method: str = "oidc"
    expires_at: int | None = None


class TokenVerifier(ABC):
    """Verifies a bearer credential and returns the asserted identity."""

    method: str

    @abstractmethod
    def verify(self, token: str) -> VerifiedIdentity:
        """Validate ``token`` or raise :class:`UnauthenticatedError`."""


class OidcTokenVerifier(TokenVerifier):
    """Validates an OIDC access token against the issuer's JWKS."""

    method = "oidc"

    def __init__(self, settings: Settings) -> None:
        if not settings.oidc_issuer:
            raise ValueError("oidc_issuer must be configured to use OidcTokenVerifier")
        self._issuer = settings.oidc_issuer.rstrip("/")
        self._audience = settings.oidc_audience or settings.oidc_client_id
        self._jwks_client: PyJWKClient | None = None
        self._jwks_cache_seconds = settings.oidc_jwks_cache_seconds

    def _discover_jwks_uri(self) -> str:
        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UnauthenticatedError(
                "Identity provider discovery document is unavailable"
            ) from exc
        document: dict[str, Any] = response.json()
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise UnauthenticatedError("Identity provider did not advertise a jwks_uri")
        return jwks_uri

    def _client(self) -> PyJWKClient:
        if self._jwks_client is None:  # pragma: no cover - network dependent
            self._jwks_client = PyJWKClient(
                self._discover_jwks_uri(),
                cache_keys=True,
                lifespan=self._jwks_cache_seconds,
            )
        return self._jwks_client

    def verify(self, token: str) -> VerifiedIdentity:  # pragma: no cover - network dependent
        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Token verification failed") from exc

        subject = str(claims["sub"])
        return VerifiedIdentity(
            subject=subject,
            issuer=self._issuer,
            username=str(claims.get("preferred_username") or subject),
            display_name=str(claims.get("name") or claims.get("preferred_username") or subject),
            email=claims.get("email"),
            session_reference=claims.get("sid"),
            auth_method=self.method,
            expires_at=claims.get("exp"),
        )


def build_token_verifier(settings: Settings) -> TokenVerifier:
    """Select the verifier appropriate to this deployment."""
    if settings.is_live_auth_active:
        return LiveModeTokenVerifier()
    if settings.oidc_issuer:
        return OidcTokenVerifier(settings)
    raise RuntimeError(
        "No authentication provider is configured. Use MARS_AUTH_MODE=live for "
        "eRegisters sign-in, or set MARS_OIDC_ISSUER."
    )


class LiveModeTokenVerifier(TokenVerifier):
    """Bearer tokens are not an authentication path in live cookie mode."""

    method = "none"

    def verify(self, token: str) -> VerifiedIdentity:
        raise UnauthenticatedError("Bearer tokens are not accepted in live mode")
