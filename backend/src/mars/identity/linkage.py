"""Deterministic linkage tokens.

A linkage token lets MARS group one person's visits without ``mars_core`` ever
holding the identifier that links them.

```
token = HMAC-SHA256(key[version], "mars.identity.v1" | type | normalised_value)
```

Three properties follow, and each is load-bearing:

**Deterministic.** The same normalised identifier always yields the same token,
so the same patient's encounters group across facilities and across time.

**Domain-separated.** The identifier *type* is inside the HMAC input. OPD 002
column 2 carries a national ID, a refugee number or a passport number in one
cell with no type marker; without domain separation a passport `CM12345` and a
NIN `CM12345` would produce the same token and merge two unrelated people into
one clinical history.

**Not reversible.** A MAC is not an encryption. Holding a token yields nothing
about the identifier even with the key - the key only lets you *test* a
candidate you already have. Re-identification therefore has to go through the
vault, where it is audited, rather than through arithmetic an analyst could do
alone.

The key never touches the database, a log, an error message or a response. It is
supplied through the environment and held as a ``SecretStr``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Final

from mars.domain.enums import IdentifierType

#: Prefix mixed into every HMAC input.
#:
#: Fixes the purpose of the key. If the same secret were ever reused for
#: another MAC in MARS, tokens from the two uses could not be confused, because
#: the message space is disjoint. The ``v1`` is the *scheme* version, not the
#: key version: changing how a token is constructed changes this string.
DOMAIN_PREFIX: Final = "mars.identity.v1"

#: Separator between message parts. Chosen because it cannot appear in a
#: normalised identifier, so ``("nin", "12|34")`` and ``("nin|12", "34")``
#: cannot produce the same message.
_SEPARATOR: Final = "|"

#: Uganda's country calling code.
UGANDA_COUNTRY_CODE: Final = "256"

#: The national trunk prefix, dialled inside Uganda in place of the country code.
UGANDA_TRUNK_PREFIX: Final = "0"

#: Length of a Ugandan national significant number, excluding trunk and country
#: code. Every current Ugandan number - mobile and fixed - is nine digits.
UGANDA_NSN_LENGTH: Final = 9

#: Leading digits a Ugandan national significant number may start with:
#: 7 mobile, 2/3/4 fixed line. A number beginning otherwise is not a Ugandan
#: subscriber number and must not become a linkage key.
UGANDA_NSN_LEADING_DIGITS: Final = frozenset("2347")

_NON_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]")
_NON_DIGIT = re.compile(r"[^0-9]")


class LinkageKeyMissingError(RuntimeError):
    """No linkage key is configured.

    Raised rather than falling back to a default. A deployment that forgets the
    key would otherwise derive every token under a value an attacker can read in
    the source, and the failure would be silent until someone noticed that two
    deployments produced identical tokens.
    """

    def __init__(self) -> None:
        super().__init__(
            "No identity linkage key is configured. Set MARS_IDENTITY_LINKAGE_KEY. "
            "MARS will not derive linkage tokens under a default key."
        )


class UnlinkableIdentifierError(ValueError):
    """The identifier normalises to nothing usable.

    The message names the identifier *type* and the length of what was
    supplied, never the value: an exception is the most likely thing to reach a
    log, and a log is the most likely place for an identifier to escape.
    """

    def __init__(self, identifier_type: IdentifierType, raw_length: int) -> None:
        self.identifier_type = identifier_type
        self.raw_length = raw_length
        super().__init__(
            f"a {identifier_type.value} of {raw_length} characters normalised to "
            "nothing usable and cannot be linked"
        )


def normalise(identifier_type: IdentifierType, raw_value: str) -> str:
    """Reduce an identifier to the form the token is derived from.

    Deliberately lossy: two clerks writing ``CM-12345`` and ``cm 12345`` mean
    the same person, and a token that split them would defeat the point. The
    raw value is kept in the vault beside the normalised one, so a
    mis-normalisation can be diagnosed later without re-collecting anything.
    """
    if identifier_type is IdentifierType.PHONE:
        return normalise_uganda_phone(raw_value)

    return _NON_ALPHANUMERIC.sub("", raw_value).upper()


def normalise_uganda_phone(raw_value: str) -> str:
    """Reduce a Ugandan telephone number to one canonical form, or reject it.

    ``+256 700 123456``, ``256700123456``, ``0700123456`` and ``700123456`` are
    the same subscriber and must reach the same token, or one person's visits
    split across several records.

    Anything else returns ``""`` and is therefore **left unlinked** rather than
    coerced. That matters more than it looks: the previous implementation
    stripped every leading zero and accepted whatever remained, so a five-digit
    fragment became a valid linkage key and two unrelated patients whose records
    happened to hold the same fragment were merged into one clinical history.

    Exactly one prefix is removed - the country code or the trunk digit, never
    both in sequence and never repeatedly - so ``00700123456`` is rejected as
    malformed instead of quietly becoming a valid number.
    """
    digits = _NON_DIGIT.sub("", raw_value)
    if not digits:
        return ""

    if digits.startswith(UGANDA_COUNTRY_CODE):
        national = digits[len(UGANDA_COUNTRY_CODE) :]
    elif digits.startswith(UGANDA_TRUNK_PREFIX):
        national = digits[len(UGANDA_TRUNK_PREFIX) :]
    else:
        national = digits

    if len(national) != UGANDA_NSN_LENGTH:
        return ""
    if national[0] not in UGANDA_NSN_LEADING_DIGITS:
        return ""

    # Canonical form carries the country code, so a Ugandan number can never be
    # confused with a nine-digit number from somewhere else.
    return UGANDA_COUNTRY_CODE + national


@dataclass(frozen=True, slots=True)
class LinkageToken:
    """A derived token and the key version that produced it.

    The version travels with the token because rotation must not orphan
    existing links: a token derived under ``v1`` stays valid and comparable to
    other ``v1`` tokens, and is re-derived under ``v2`` only deliberately.
    """

    value: str
    key_version: str
    identifier_type: IdentifierType

    def __repr__(self) -> str:
        """Truncate the token.

        A ``repr`` reaches tracebacks and debuggers. The token is not an
        identifier, but it is linkage material, and a full one in a traceback
        would let anyone holding the key confirm a guessed identifier.
        """
        return (
            f"LinkageToken(value='{self.value[:8]}…', "
            f"key_version={self.key_version!r}, "
            f"identifier_type={self.identifier_type.value!r})"
        )


class LinkageTokenDeriver:
    """Derives linkage tokens under a versioned key.

    Constructed with the active key and, optionally, retired keys so that a
    token stored under an earlier version can still be recomputed during
    rotation. Retired keys can verify; only the active key is used for new
    tokens.
    """

    def __init__(
        self,
        *,
        active_key: bytes | None,
        active_version: str,
        retired_keys: dict[str, bytes] | None = None,
    ) -> None:
        if active_key is not None and not active_key:
            raise LinkageKeyMissingError()
        self._active_key = active_key
        self._active_version = active_version
        self._retired_keys = dict(retired_keys or {})

    @property
    def active_version(self) -> str:
        return self._active_version

    @property
    def is_configured(self) -> bool:
        """Whether a key is available.

        Lets a deployment report itself unready rather than failing at the first
        patient.
        """
        return self._active_key is not None

    def known_versions(self) -> frozenset[str]:
        versions = {self._active_version} if self._active_key is not None else set()
        return frozenset(versions | set(self._retired_keys))

    def derive(
        self,
        identifier_type: IdentifierType,
        raw_value: str,
        *,
        key_version: str | None = None,
    ) -> LinkageToken:
        """Derive the token for one identifier.

        ``key_version`` recomputes under a retired key, which is what rotation
        and verification need. New tokens always use the active version.
        """
        normalised = normalise(identifier_type, raw_value)
        if not normalised:
            raise UnlinkableIdentifierError(identifier_type, len(raw_value))

        version = key_version or self._active_version
        key = self._key_for(version)

        message = _SEPARATOR.join([DOMAIN_PREFIX, identifier_type.value, normalised]).encode(
            "utf-8"
        )
        digest = hmac.new(key, message, hashlib.sha256).hexdigest()
        return LinkageToken(value=digest, key_version=version, identifier_type=identifier_type)

    def matches(self, token: LinkageToken, identifier_type: IdentifierType, raw_value: str) -> bool:
        """Whether a candidate identifier produces this token.

        Compared in constant time. A timing difference would let a caller who
        can submit guesses narrow an identifier without ever being granted
        re-identification.
        """
        try:
            candidate = self.derive(identifier_type, raw_value, key_version=token.key_version)
        except (UnlinkableIdentifierError, LinkageKeyMissingError):
            return False
        return hmac.compare_digest(candidate.value, token.value)

    def _key_for(self, version: str) -> bytes:
        if version == self._active_version and self._active_key is not None:
            return self._active_key
        retired = self._retired_keys.get(version)
        if retired is not None:
            return retired
        raise LinkageKeyMissingError()


__all__ = [
    "DOMAIN_PREFIX",
    "UGANDA_COUNTRY_CODE",
    "UGANDA_NSN_LEADING_DIGITS",
    "UGANDA_NSN_LENGTH",
    "UGANDA_TRUNK_PREFIX",
    "LinkageKeyMissingError",
    "LinkageToken",
    "LinkageTokenDeriver",
    "UnlinkableIdentifierError",
    "normalise",
    "normalise_uganda_phone",
]
