"""Authenticated encryption for direct identifiers.

Schema separation keeps identity out of the application's reach. It does not
protect a backup, a replica, a stolen disk, or a superuser session. Direct
identifiers are therefore encrypted in the application before they reach the
database, so the ciphertext is what PostgreSQL stores and what a dump contains.

**AES-256-GCM**, an AEAD: one operation gives confidentiality *and*
authentication, so a modified ciphertext fails to decrypt rather than yielding
altered plaintext. There is no separate MAC to forget.

The envelope is self-describing so a key can be rotated without rewriting
history:

```
version_length : 1 byte
version        : ASCII, up to 16 bytes
nonce          : 12 bytes, fresh random per operation
ciphertext+tag : AES-256-GCM output
```

**Associated data** binds each ciphertext to where it lives. A surname
ciphertext moved into the phone column, or copied onto another patient's row,
fails to decrypt: the AAD names the table, the column, and a value that
identifies the row. Encryption alone would leave that shuffle undetectable.

**A nonce is never reused.** It is 96 random bits per operation, so encrypting
the same name twice yields different ciphertext and the database reveals nothing
by equality. Equality matching is the linkage token's job, and that is a
different key with a different purpose - see :mod:`mars.identity.linkage`.

Keys come from the environment. There is no default, and no fallback: a
deployment that forgets the key must fail loudly rather than write identifiers
under a value an attacker can read in the source.
"""

from __future__ import annotations

import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-256 requires exactly 32 bytes.
KEY_LENGTH: Final = 32

#: 96 bits, the size GCM is specified for.
NONCE_LENGTH: Final = 12

#: Bounds the version field so a malformed envelope cannot ask us to read an
#: arbitrary length before we have authenticated anything.
MAX_VERSION_LENGTH: Final = 16

#: Prefix on every AAD, fixing the purpose of the key.
AAD_PREFIX: Final = "mars.identity.enc.v1"


class EncryptionKeyMissingError(RuntimeError):
    """No encryption key is configured for the version required.

    Raised rather than falling back. Identity that cannot be encrypted must not
    be written at all: a silent plaintext fallback would be undetectable until a
    breach revealed it.
    """

    def __init__(self, version: str | None = None) -> None:
        detail = f" for key version {version!r}" if version else ""
        super().__init__(
            f"No identity encryption key is configured{detail}. "
            "Set MARS_IDENTITY_ENCRYPTION_KEY. MARS will not store direct "
            "identifiers unencrypted."
        )


class InvalidKeyLengthError(ValueError):
    """A configured key is not 32 bytes.

    Named separately from a missing key because the operational fix differs: one
    is an absent secret, the other a truncated or mis-encoded one.
    """

    def __init__(self, version: str, length: int) -> None:
        super().__init__(
            f"identity encryption key {version!r} is {length} bytes; "
            f"AES-256 requires exactly {KEY_LENGTH}"
        )


class DecryptionFailedError(RuntimeError):
    """Authentication failed: the ciphertext, the AAD or the key is wrong.

    Deliberately does not distinguish which. A caller that could tell a wrong
    key from a tampered ciphertext from a mismatched row could probe the vault's
    structure. The message names the field, never a value.
    """

    def __init__(self, context: str) -> None:
        self.context = context
        super().__init__(f"could not decrypt {context}: the ciphertext failed authentication")


def build_associated_data(table: str, column: str, binding: str) -> bytes:
    """Associated data for one field of one row.

    ``binding`` is a value that identifies the row and does not change: the
    patient reference for a vault record, the linkage token for an identifier.
    Including it means a ciphertext is only valid where it was written.
    """
    return "|".join([AAD_PREFIX, table, column, binding]).encode("utf-8")


class FieldEncryptor:
    """Encrypts and decrypts one field at a time under a versioned key.

    Retired keys decrypt but never encrypt, which is what lets a rotation run
    while old rows are still readable.
    """

    def __init__(
        self,
        *,
        active_key: bytes | None,
        active_version: str,
        retired_keys: dict[str, bytes] | None = None,
    ) -> None:
        if active_key is not None and len(active_key) != KEY_LENGTH:
            raise InvalidKeyLengthError(active_version, len(active_key))
        for version, key in (retired_keys or {}).items():
            if len(key) != KEY_LENGTH:
                raise InvalidKeyLengthError(version, len(key))
        if len(active_version.encode("utf-8")) > MAX_VERSION_LENGTH:
            raise ValueError(f"key version {active_version!r} exceeds {MAX_VERSION_LENGTH} bytes")

        self._active_key = active_key
        self._active_version = active_version
        self._retired_keys = dict(retired_keys or {})

    @property
    def is_configured(self) -> bool:
        """Whether identity can be written.

        Lets a deployment report itself unready rather than discovering the
        problem at the first patient.
        """
        return self._active_key is not None

    @property
    def active_version(self) -> str:
        return self._active_version

    def known_versions(self) -> frozenset[str]:
        active = {self._active_version} if self._active_key is not None else set()
        return frozenset(active | set(self._retired_keys))

    def encrypt(self, plaintext: str | None, *, aad: bytes) -> bytes | None:
        """Encrypt one value. ``None`` stays ``None``.

        A null stays null rather than becoming ciphertext of an empty string:
        "no phone number was recorded" and "an empty phone number was recorded"
        are different facts, and the second is not one the register can express.
        """
        if plaintext is None:
            return None
        if self._active_key is None:
            raise EncryptionKeyMissingError()

        nonce = os.urandom(NONCE_LENGTH)
        ciphertext = AESGCM(self._active_key).encrypt(nonce, plaintext.encode("utf-8"), aad)
        version = self._active_version.encode("utf-8")
        return bytes([len(version)]) + version + nonce + ciphertext

    def decrypt(self, envelope: bytes | None, *, aad: bytes, context: str) -> str | None:
        """Decrypt one value, or fail closed.

        ``context`` names the field for the error message - never a value.
        """
        if envelope is None:
            return None

        version, nonce, ciphertext = self._unpack(envelope, context)
        key = self._key_for(version)

        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise DecryptionFailedError(context) from exc
        return plaintext.decode("utf-8")

    def version_of(self, envelope: bytes | None) -> str | None:
        """The key version an envelope was written under.

        Read from the unauthenticated header, so it is a hint for choosing a key
        and never a fact to act on: a wrong version simply fails to decrypt.
        """
        if envelope is None:
            return None
        version, _, _ = self._unpack(envelope, "envelope")
        return version

    def _unpack(self, envelope: bytes, context: str) -> tuple[str, bytes, bytes]:
        # Every length is checked before it is used. A truncated or crafted
        # envelope must produce a clean failure, not an index error that leaks a
        # stack trace or a slice of somebody's ciphertext.
        if len(envelope) < 1:
            raise DecryptionFailedError(context)
        version_length = envelope[0]
        if version_length == 0 or version_length > MAX_VERSION_LENGTH:
            raise DecryptionFailedError(context)

        start = 1 + version_length
        if len(envelope) < start + NONCE_LENGTH + 1:
            raise DecryptionFailedError(context)

        try:
            version = envelope[1:start].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecryptionFailedError(context) from exc

        nonce = envelope[start : start + NONCE_LENGTH]
        ciphertext = envelope[start + NONCE_LENGTH :]
        return version, nonce, ciphertext

    def _key_for(self, version: str) -> bytes:
        if version == self._active_version and self._active_key is not None:
            return self._active_key
        retired = self._retired_keys.get(version)
        if retired is None:
            raise EncryptionKeyMissingError(version)
        return retired


__all__ = [
    "AAD_PREFIX",
    "KEY_LENGTH",
    "NONCE_LENGTH",
    "DecryptionFailedError",
    "EncryptionKeyMissingError",
    "FieldEncryptor",
    "InvalidKeyLengthError",
    "build_associated_data",
]
