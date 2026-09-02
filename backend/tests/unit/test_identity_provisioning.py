"""Building the identity component from configuration.

Key material is parsed here, once, at startup. A malformed key must stop the
component from starting rather than surfacing as a decryption failure on a
patient months later - and no parse error may ever contain the secret it failed
to parse.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import SecretStr

from mars.core.settings import Environment, Settings
from mars.identity.provisioning import (
    IdentityNotConfiguredError,
    build_encryptor,
    build_linkage_deriver,
    decode_key,
    get_identity_engine,
    parse_retired_keys,
    reset_identity_engine,
)

KEY = bytes(range(32))
KEY_HEX = KEY.hex()
KEY_B64 = base64.b64encode(KEY).decode()


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": Environment.LOCAL,
        "database_url": "postgresql+psycopg://mars:unused@localhost:5432/unused",
        "log_format": "console",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestKeyDecoding:
    @pytest.mark.parametrize("material", [KEY_HEX, KEY_B64])
    def test_both_encodings_decode_to_the_same_key(self, material: str) -> None:
        """Secret stores disagree about which they emit.

        Accepting one only would mean an operator re-encoding a key by hand,
        which is how a key gets truncated.
        """
        assert decode_key(material) == KEY

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert decode_key(f"  {KEY_HEX}\n") == KEY

    @pytest.mark.parametrize("material", ["", "   ", "not-a-key", "abcd", KEY_HEX[:40]])
    def test_anything_else_is_refused(self, material: str) -> None:
        with pytest.raises(ValueError):
            decode_key(material)

    def test_a_wrong_length_key_is_refused(self) -> None:
        """A 16-byte key would silently give AES-128 where AES-256 was intended."""
        with pytest.raises(ValueError, match="32 bytes"):
            decode_key(bytes(range(16)).hex())

    def test_the_error_does_not_contain_the_material(self) -> None:
        """This parser runs at startup, where an exception reaches a log."""
        secret = "d" * 63
        with pytest.raises(ValueError) as exc:
            decode_key(secret)
        assert secret not in str(exc.value)


class TestRetiredKeyParsing:
    def test_a_single_pair_is_parsed(self) -> None:
        assert parse_retired_keys(SecretStr(f"v1:{KEY_HEX}")) == {"v1": KEY}

    def test_several_pairs_are_parsed(self) -> None:
        other = bytes(range(32, 64))
        parsed = parse_retired_keys(
            SecretStr(f"v1:{KEY_HEX}, v2:{base64.b64encode(other).decode()}")
        )
        assert parsed == {"v1": KEY, "v2": other}

    def test_absent_material_yields_nothing(self) -> None:
        assert parse_retired_keys(None) == {}
        assert parse_retired_keys(SecretStr("  ")) == {}

    def test_a_missing_separator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="version:secret"):
            parse_retired_keys(SecretStr(KEY_HEX))

    def test_a_malformed_secret_names_the_version_not_the_secret(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_retired_keys(SecretStr("v1:deadbeef"))
        assert "'v1'" in str(exc.value)
        assert "deadbeef" not in str(exc.value)


class TestBuildingComponents:
    def test_an_unconfigured_deployment_builds_an_unready_component(self) -> None:
        """It must import and report unready, not fail to construct.

        A deployment running no identity component still has to start.
        """
        assert build_linkage_deriver(settings()).is_configured is False
        assert build_encryptor(settings()).is_configured is False

    def test_a_configured_deployment_is_ready(self) -> None:
        configured = settings(
            identity_linkage_key=SecretStr("linkage-secret"),
            identity_encryption_key=SecretStr(KEY_HEX),
        )
        assert build_linkage_deriver(configured).is_configured is True
        assert build_encryptor(configured).is_configured is True

    def test_retired_versions_are_available_to_both(self) -> None:
        other = bytes(range(32, 64))
        configured = settings(
            identity_linkage_key=SecretStr("new-secret"),
            identity_linkage_key_version="v2",
            identity_linkage_retired_keys=SecretStr("v1:old-secret"),
            identity_encryption_key=SecretStr(KEY_HEX),
            identity_encryption_key_version="v2",
            identity_encryption_retired_keys=SecretStr(f"v1:{other.hex()}"),
        )
        assert build_linkage_deriver(configured).known_versions() == frozenset({"v1", "v2"})
        assert build_encryptor(configured).known_versions() == frozenset({"v1", "v2"})

    def test_the_two_key_families_are_independent(self) -> None:
        """Compromising one must not yield the other.

        The linkage key lets you test a guessed identifier; the encryption key
        lets you read stored ones. Neither substitutes for the other, and they
        are configured separately so they can be stored separately.
        """
        configured = settings(
            identity_linkage_key=SecretStr("linkage-secret"),
            identity_encryption_key=SecretStr(KEY_HEX),
        )
        assert (
            configured.identity_linkage_key.get_secret_value()  # type: ignore[union-attr]
            != configured.identity_encryption_key.get_secret_value()  # type: ignore[union-attr]
        )


class TestNoFallbackToTheApplicationConnection:
    def test_an_unconfigured_identity_url_refuses_rather_than_falling_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fallback would run identity queries as the role that must not.

        The failure would then surface as a permission error far from the
        missing setting that caused it.
        """
        from mars.core import settings as settings_module

        monkeypatch.setattr(settings_module, "get_settings", lambda: settings())
        monkeypatch.setattr("mars.identity.provisioning.get_settings", lambda: settings())
        reset_identity_engine()
        with pytest.raises(IdentityNotConfiguredError, match="IDENTITY_DATABASE_URL"):
            get_identity_engine()
        reset_identity_engine()

    def test_the_identity_url_is_a_separate_setting(self) -> None:
        """Not derived from database_url. Separate credentials, separate role."""
        assert "identity_database_url" in Settings.model_fields
        assert Settings.model_fields["identity_database_url"].default is None


class TestSecretsDoNotLeak:
    def test_settings_repr_hides_key_material(self) -> None:
        configured = settings(
            identity_linkage_key=SecretStr("linkage-secret"),
            identity_encryption_key=SecretStr(KEY_HEX),
        )
        rendered = repr(configured)
        assert "linkage-secret" not in rendered
        assert KEY_HEX not in rendered

    def test_model_dump_hides_key_material(self) -> None:
        """A settings dump is a common thing to log at startup."""
        configured = settings(
            identity_linkage_key=SecretStr("linkage-secret"),
            identity_encryption_key=SecretStr(KEY_HEX),
        )
        rendered = str(configured.model_dump())
        assert "linkage-secret" not in rendered
        assert KEY_HEX not in rendered
