"""Wiring the identity component from configuration.

Two things this module exists to guarantee.

**The identity service connects as its own database role.** Not the application
role with a promise not to touch identity - a separate engine, built from a
separate URL, with separate credentials. ``SET ROLE`` on a shared connection
would be reversible by anything that can execute SQL on it; a distinct
connection is not.

**Keys are parsed once, strictly, at construction.** A malformed key must stop
the identity component from starting, not surface as a decryption failure on a
patient months later. Nothing here logs, repeats or returns key material.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
from functools import lru_cache

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from mars.core.settings import Settings, get_settings
from mars.identity.encryption import KEY_LENGTH, FieldEncryptor
from mars.identity.linkage import LinkageTokenDeriver


class IdentityNotConfiguredError(RuntimeError):
    """The identity component was asked for but is not configured.

    Raised rather than falling back to the application connection. A fallback
    would mean identity queries running as the role that is specifically
    supposed to be unable to make them, and the failure would be a permission
    error somewhere far from the missing setting that caused it.
    """

    def __init__(self, missing: str) -> None:
        super().__init__(
            f"The identity component requires {missing}. It is not configured, "
            "and MARS will not fall back to the application database connection."
        )


def decode_key(material: str) -> bytes:
    """Decode a key from hex or base64, or refuse it.

    Both encodings are accepted because secret stores disagree about which they
    emit, and re-encoding a key by hand to satisfy a parser is how a key gets
    truncated. Length is checked here so a wrong key is a startup failure rather
    than a decryption failure later.
    """
    text = material.strip()
    if not text:
        raise ValueError("empty key material")

    # Try both encodings and keep whichever yields the right length. A key
    # that decodes as hex *and* as base64 to different lengths is not ambiguous
    # in practice: only one will be 32 bytes.
    candidates: list[bytes] = []
    with contextlib.suppress(ValueError):
        candidates.append(bytes.fromhex(text))
    with contextlib.suppress(binascii.Error, ValueError):
        candidates.append(base64.b64decode(text, validate=True))

    for candidate in candidates:
        if len(candidate) == KEY_LENGTH:
            return candidate

    raise ValueError(f"key material does not decode to {KEY_LENGTH} bytes from hex or base64")


def parse_retired_keys(material: SecretStr | None) -> dict[str, bytes]:
    """Parse ``version:secret,version:secret`` into a mapping.

    Errors name the *version* and never the secret: this parser runs at startup,
    where an exception is most likely to reach a log.
    """
    if material is None:
        return {}
    raw = material.get_secret_value().strip()
    if not raw:
        return {}

    keys: dict[str, bytes] = {}
    for entry in raw.split(","):
        piece = entry.strip()
        if not piece:
            continue
        version, separator, secret = piece.partition(":")
        if not separator or not version.strip():
            raise ValueError("retired key entries must be written as version:secret")
        try:
            keys[version.strip()] = decode_key(secret)
        except ValueError as exc:
            raise ValueError(f"retired key {version.strip()!r} is not a valid 32-byte key") from exc
    return keys


def build_linkage_deriver(settings: Settings) -> LinkageTokenDeriver:
    """The HMAC deriver, from configuration.

    Constructed even when no key is present, so a deployment can report itself
    unready rather than failing to import.
    """
    active = settings.identity_linkage_key
    return LinkageTokenDeriver(
        active_key=active.get_secret_value().encode("utf-8") if active else None,
        active_version=settings.identity_linkage_key_version,
        retired_keys=_retired_linkage_keys(settings),
    )


def _retired_linkage_keys(settings: Settings) -> dict[str, bytes]:
    material = settings.identity_linkage_retired_keys
    if material is None:
        return {}
    raw = material.get_secret_value().strip()
    if not raw:
        return {}
    keys: dict[str, bytes] = {}
    for entry in raw.split(","):
        piece = entry.strip()
        if not piece:
            continue
        version, separator, secret = piece.partition(":")
        if not separator or not version.strip():
            raise ValueError("retired linkage keys must be written as version:secret")
        keys[version.strip()] = secret.encode("utf-8")
    return keys


def build_encryptor(settings: Settings) -> FieldEncryptor:
    """The AES-256-GCM encryptor, from configuration."""
    active = settings.identity_encryption_key
    return FieldEncryptor(
        active_key=decode_key(active.get_secret_value()) if active else None,
        active_version=settings.identity_encryption_key_version,
        retired_keys=parse_retired_keys(settings.identity_encryption_retired_keys),
    )


@lru_cache(maxsize=1)
def get_identity_engine() -> Engine:
    """The engine the identity service connects on.

    A distinct engine, from ``identity_database_url``, so the identity component
    holds its own credentials and its own pool. The application engine is not
    reused and is not reachable from here.
    """
    settings = get_settings()
    if not settings.identity_database_url:
        raise IdentityNotConfiguredError("MARS_IDENTITY_DATABASE_URL")

    return create_engine(
        settings.identity_database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_identity_session_factory() -> sessionmaker[Session]:
    """Session factory bound to the identity engine."""
    return sessionmaker(
        bind=get_identity_engine(), expire_on_commit=False, autoflush=False, future=True
    )


def reset_identity_engine() -> None:
    """Drop the cached engine and factory.

    For tests, and for a process that reloads configuration. Disposes the pool
    so connections holding the identity credentials are closed rather than left
    open against a URL that is no longer current.
    """
    if get_identity_engine.cache_info().currsize:
        get_identity_engine().dispose()
    get_identity_engine.cache_clear()
    get_identity_session_factory.cache_clear()


__all__ = [
    "IdentityNotConfiguredError",
    "build_encryptor",
    "build_linkage_deriver",
    "decode_key",
    "get_identity_engine",
    "get_identity_session_factory",
    "parse_retired_keys",
    "reset_identity_engine",
]
