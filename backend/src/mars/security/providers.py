"""Authentication provider abstraction.

MARS authenticates against an OIDC provider in staging and production. The
domain never learns which one: it receives a ``VerifiedIdentity`` and looks the
subject up locally. Swapping Keycloak for Entra ID or a Ministry SSO changes one
implementation and nothing else.

Two implementations ship:

``OidcTokenVerifier``
    Verifies a signed JWT against the provider's published JWKS. The production
    path.

``DevelopmentTokenVerifier``
    Issues and verifies short-lived HMAC tokens for synthetic users. Guarded so
    it cannot run outside development: the settings model refuses the
    combination, and this class refuses it again at construction.
"""

from __future__ import annotations

import time
import uuid
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


class DevelopmentTokenVerifier(TokenVerifier):
    """Synthetic authentication for local development only.

    Every identity it produces is prefixed ``dev:`` and is flagged synthetic all
    the way through to the audit trail, so a development session can never be
    mistaken for a real one.
    """

    method = "development"
    ISSUER = "mars-development"
    SUBJECT_PREFIX = "dev:"

    def __init__(self, settings: Settings) -> None:
        if settings.environment.is_protected:
            raise RuntimeError("DevelopmentTokenVerifier refuses to run in a protected environment")
        if not settings.dev_auth_enabled:
            raise RuntimeError("Development authentication is not enabled")
        self._secret = settings.dev_auth_secret
        self._ttl = settings.dev_auth_token_ttl_seconds

    def issue(
        self,
        *,
        subject: str,
        username: str,
        display_name: str,
        email: str | None = None,
    ) -> tuple[str, str, int]:
        """Mint a development token.

        Returns the token, the session reference and the expiry instant as a
        Unix timestamp.
        """
        now = int(time.time())
        expires_at = now + self._ttl
        session_reference = uuid.uuid4().hex
        payload = {
            "iss": self.ISSUER,
            "sub": subject,
            "preferred_username": username,
            "name": display_name,
            "email": email,
            "sid": session_reference,
            "iat": now,
            "exp": expires_at,
            "mars_synthetic": True,
        }
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        return token, session_reference, expires_at

    def verify(self, token: str) -> VerifiedIdentity:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self.ISSUER,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError("Development token verification failed") from exc

        if not claims.get("mars_synthetic"):
            raise UnauthenticatedError("Token is not a MARS development token")

        subject = str(claims["sub"])
        if not subject.startswith(self.SUBJECT_PREFIX):
            raise UnauthenticatedError("Development subjects must be prefixed 'dev:'")

        return VerifiedIdentity(
            subject=subject,
            issuer=self.ISSUER,
            username=str(claims.get("preferred_username") or subject),
            display_name=str(claims.get("name") or subject),
            email=claims.get("email"),
            session_reference=claims.get("sid"),
            auth_method=self.method,
            expires_at=claims.get("exp"),
        )


def build_token_verifier(settings: Settings) -> TokenVerifier:
    """Select the verifier appropriate to this deployment."""
    if settings.is_development_auth_active:
        return DevelopmentTokenVerifier(settings)
    if settings.oidc_issuer:
        return OidcTokenVerifier(settings)
    raise RuntimeError(
        "No authentication provider is configured. Set MARS_OIDC_ISSUER, or "
        "enable MARS_DEV_AUTH_ENABLED in a non-protected environment."
    )
