"""Authenticated encryption of direct identifiers.

No database. These assert the properties the storage guarantee rests on: that
ciphertext decrypts only under the right key in the right place, that a tampered
envelope fails rather than yielding altered plaintext, and that nothing here
leaks the value or the key.

Every value below is invented.
"""

from __future__ import annotations

import os

import pytest

from mars.identity.encryption import (
    KEY_LENGTH,
    NONCE_LENGTH,
    DecryptionFailedError,
    EncryptionKeyMissingError,
    FieldEncryptor,
    InvalidKeyLengthError,
    build_associated_data,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))
AAD = build_associated_data("identity_record", "surname", "ref-1")
SURNAME = "Okello"


def encryptor(**kwargs: object) -> FieldEncryptor:
    options: dict[str, object] = {"active_key": KEY, "active_version": "v1"}
    options.update(kwargs)
    return FieldEncryptor(**options)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_a_value_survives_encryption_and_decryption(self) -> None:
        e = encryptor()
        envelope = e.encrypt(SURNAME, aad=AAD)
        assert e.decrypt(envelope, aad=AAD, context="surname") == SURNAME

    def test_the_plaintext_does_not_appear_in_the_ciphertext(self) -> None:
        envelope = encryptor().encrypt(SURNAME, aad=AAD)
        assert envelope is not None
        assert SURNAME.encode() not in envelope

    def test_none_stays_none(self) -> None:
        """ "No phone number recorded" and "an empty one" are different facts."""
        e = encryptor()
        assert e.encrypt(None, aad=AAD) is None
        assert e.decrypt(None, aad=AAD, context="surname") is None

    def test_unicode_survives(self) -> None:
        e = encryptor()
        name = "Nakato Ssebbowa — Ö"
        assert e.decrypt(e.encrypt(name, aad=AAD), aad=AAD, context="s") == name

    def test_an_empty_string_is_distinguishable_from_none(self) -> None:
        e = encryptor()
        assert e.decrypt(e.encrypt("", aad=AAD), aad=AAD, context="s") == ""


class TestNonceUniqueness:
    def test_encrypting_the_same_value_twice_differs(self) -> None:
        """Otherwise the database reveals which patients share a surname.

        Equality matching is the linkage token's job, under a different key.
        """
        e = encryptor()
        first = e.encrypt(SURNAME, aad=AAD)
        second = e.encrypt(SURNAME, aad=AAD)
        assert first != second

    def test_many_encryptions_never_repeat(self) -> None:
        e = encryptor()
        seen = {e.encrypt(SURNAME, aad=AAD) for _ in range(200)}
        assert len(seen) == 200

    def test_the_nonce_is_the_specified_length(self) -> None:
        envelope = encryptor(active_version="v1").encrypt(SURNAME, aad=AAD)
        assert envelope is not None
        # 1 length byte + 2 version bytes + nonce + ciphertext
        assert len(envelope) > 1 + 2 + NONCE_LENGTH


class TestTamperDetection:
    """A modified ciphertext must fail, not decrypt to something else."""

    @pytest.mark.parametrize("position", [0, 1, 5, 20, -1])
    def test_flipping_any_byte_fails_closed(self, position: int) -> None:
        """No tampered envelope ever decrypts. That is the whole invariant.

        Which failure it is depends on where the flip lands, and that is
        expected: the version header is a routing hint read before anything is
        authenticated, so corrupting it produces "no such key version" while
        corrupting the ciphertext produces "authentication failed". Both refuse.
        Requiring one specific exception here would be asserting an
        implementation detail rather than the guarantee.
        """
        e = encryptor()
        envelope = bytearray(e.encrypt(SURNAME, aad=AAD) or b"")
        envelope[position] ^= 0x01
        with pytest.raises((DecryptionFailedError, EncryptionKeyMissingError)):
            e.decrypt(bytes(envelope), aad=AAD, context="surname")

    @pytest.mark.parametrize("position", [-1, -5, -12])
    def test_flipping_a_ciphertext_byte_fails_authentication(self, position: int) -> None:
        """Within the authenticated region the failure is specifically the tag."""
        e = encryptor()
        envelope = bytearray(e.encrypt(SURNAME, aad=AAD) or b"")
        envelope[position] ^= 0x01
        with pytest.raises(DecryptionFailedError):
            e.decrypt(bytes(envelope), aad=AAD, context="surname")

    def test_truncation_fails(self) -> None:
        e = encryptor()
        envelope = e.encrypt(SURNAME, aad=AAD) or b""
        with pytest.raises(DecryptionFailedError):
            e.decrypt(envelope[:-4], aad=AAD, context="surname")

    def test_an_empty_envelope_fails_cleanly(self) -> None:
        with pytest.raises(DecryptionFailedError):
            encryptor().decrypt(b"", aad=AAD, context="surname")

    def test_a_crafted_header_fails_cleanly(self) -> None:
        """A hostile length byte must not cause an index error or a slice leak."""
        for envelope in (b"\xff", b"\x00abc", b"\x10short"):
            with pytest.raises(DecryptionFailedError):
                encryptor().decrypt(envelope, aad=AAD, context="surname")


class TestAssociatedDataBinding:
    """Ciphertext is only valid where it was written."""

    def test_a_value_moved_to_another_column_fails(self) -> None:
        e = encryptor()
        envelope = e.encrypt(SURNAME, aad=build_associated_data("identity_record", "surname", "r1"))
        with pytest.raises(DecryptionFailedError):
            e.decrypt(
                envelope,
                aad=build_associated_data("identity_record", "phone_contact", "r1"),
                context="phone",
            )

    def test_a_value_moved_to_another_row_fails(self) -> None:
        """Copying one patient's surname onto another must not silently work."""
        e = encryptor()
        envelope = e.encrypt(SURNAME, aad=build_associated_data("identity_record", "surname", "r1"))
        with pytest.raises(DecryptionFailedError):
            e.decrypt(
                envelope,
                aad=build_associated_data("identity_record", "surname", "r2"),
                context="surname",
            )

    def test_a_value_moved_to_another_table_fails(self) -> None:
        e = encryptor()
        envelope = e.encrypt(SURNAME, aad=build_associated_data("identity_record", "surname", "r1"))
        with pytest.raises(DecryptionFailedError):
            e.decrypt(
                envelope,
                aad=build_associated_data("identity_identifier", "surname", "r1"),
                context="x",
            )

    def test_the_aad_is_namespaced(self) -> None:
        assert build_associated_data("t", "c", "b").startswith(b"mars.identity.enc")


class TestKeyHandling:
    def test_the_wrong_key_fails(self) -> None:
        envelope = encryptor().encrypt(SURNAME, aad=AAD)
        with pytest.raises(DecryptionFailedError):
            encryptor(active_key=OTHER_KEY).decrypt(envelope, aad=AAD, context="s")

    def test_a_short_key_is_refused_at_construction(self) -> None:
        """A truncated key must stop startup, not surface months later."""
        with pytest.raises(InvalidKeyLengthError):
            FieldEncryptor(active_key=b"tooshort", active_version="v1")

    def test_a_short_retired_key_is_refused(self) -> None:
        with pytest.raises(InvalidKeyLengthError):
            FieldEncryptor(active_key=KEY, active_version="v2", retired_keys={"v1": b"short"})

    def test_no_key_means_not_configured(self) -> None:
        assert encryptor(active_key=None).is_configured is False

    def test_encrypting_without_a_key_raises(self) -> None:
        """No silent plaintext fallback: it would be invisible until a breach."""
        with pytest.raises(EncryptionKeyMissingError):
            encryptor(active_key=None).encrypt(SURNAME, aad=AAD)

    def test_the_missing_key_error_names_no_default(self) -> None:
        with pytest.raises(EncryptionKeyMissingError) as exc:
            encryptor(active_key=None).encrypt(SURNAME, aad=AAD)
        assert "unencrypted" in str(exc.value)

    def test_an_oversized_version_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            FieldEncryptor(active_key=KEY, active_version="v" * 40)

    def test_the_key_length_constant_is_aes_256(self) -> None:
        assert KEY_LENGTH == 32


class TestRotation:
    def test_a_retired_key_still_decrypts_its_own_rows(self) -> None:
        old = FieldEncryptor(active_key=OTHER_KEY, active_version="v1")
        envelope = old.encrypt(SURNAME, aad=AAD)

        rotated = FieldEncryptor(
            active_key=KEY, active_version="v2", retired_keys={"v1": OTHER_KEY}
        )
        assert rotated.decrypt(envelope, aad=AAD, context="s") == SURNAME

    def test_new_values_use_the_active_key(self) -> None:
        rotated = FieldEncryptor(
            active_key=KEY, active_version="v2", retired_keys={"v1": OTHER_KEY}
        )
        assert rotated.version_of(rotated.encrypt(SURNAME, aad=AAD)) == "v2"

    def test_an_unknown_version_fails_with_a_key_error(self) -> None:
        """Distinct from tampering: the operational fix is to restore a key."""
        old = FieldEncryptor(active_key=OTHER_KEY, active_version="v9")
        envelope = old.encrypt(SURNAME, aad=AAD)
        with pytest.raises(EncryptionKeyMissingError):
            encryptor().decrypt(envelope, aad=AAD, context="s")

    def test_known_versions_reports_what_can_be_read(self) -> None:
        rotated = FieldEncryptor(
            active_key=KEY, active_version="v2", retired_keys={"v1": OTHER_KEY}
        )
        assert rotated.known_versions() == frozenset({"v1", "v2"})


class TestNothingLeaks:
    def test_the_error_names_the_field_not_the_value(self) -> None:
        e = encryptor()
        envelope = bytearray(e.encrypt(SURNAME, aad=AAD) or b"")
        envelope[-1] ^= 0x01
        with pytest.raises(DecryptionFailedError) as exc:
            e.decrypt(bytes(envelope), aad=AAD, context="identity_record.surname")
        message = str(exc.value)
        assert "identity_record.surname" in message
        assert SURNAME not in message

    def test_the_error_does_not_say_which_check_failed(self) -> None:
        """A caller able to tell a wrong key from a tampered ciphertext from a
        mismatched row could map the vault's structure without reading it."""
        e = encryptor()
        wrong_key = encryptor(active_key=OTHER_KEY)
        envelope = e.encrypt(SURNAME, aad=AAD)

        with pytest.raises(DecryptionFailedError) as tampered:
            bad = bytearray(envelope or b"")
            bad[-1] ^= 0x01
            e.decrypt(bytes(bad), aad=AAD, context="f")
        with pytest.raises(DecryptionFailedError) as wrong:
            wrong_key.decrypt(envelope, aad=AAD, context="f")

        assert str(tampered.value) == str(wrong.value)

    def test_the_encryptor_repr_does_not_contain_the_key(self) -> None:
        assert KEY.hex() not in repr(encryptor())

    def test_a_random_envelope_never_decrypts(self) -> None:
        e = encryptor()
        for _ in range(50):
            with pytest.raises((DecryptionFailedError, EncryptionKeyMissingError)):
                e.decrypt(os.urandom(40), aad=AAD, context="f")
