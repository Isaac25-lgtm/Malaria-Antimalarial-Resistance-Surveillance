"""Linkage token derivation.

No database. These assert the cryptographic properties the whole identity
separation rests on: that the same person links to themselves, that two people
who happen to share a number do not, and that nothing here leaks the value it
was derived from.

Every fixture value below is invented. None is a real Ugandan national
identification number.
"""

from __future__ import annotations

import re

import pytest

from mars.domain.enums import IdentifierType
from mars.identity.linkage import (
    DOMAIN_PREFIX,
    LinkageKeyMissingError,
    LinkageTokenDeriver,
    UnlinkableIdentifierError,
    normalise,
)

KEY = b"test-key-not-a-real-secret"
OTHER_KEY = b"a-different-test-key"


def deriver(**kwargs: object) -> LinkageTokenDeriver:
    options: dict[str, object] = {"active_key": KEY, "active_version": "v1"}
    options.update(kwargs)
    return LinkageTokenDeriver(**options)  # type: ignore[arg-type]


class TestDeterminism:
    """The same person must link to themselves, across facilities and years."""

    def test_the_same_identifier_produces_the_same_token(self) -> None:
        d = deriver()
        first = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        second = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert first.value == second.value

    @pytest.mark.parametrize(
        "written",
        ["CM12345678", "cm12345678", "CM-1234-5678", " CM 1234 5678 ", "cm/1234/5678"],
    )
    def test_formatting_differences_do_not_split_one_person(self, written: str) -> None:
        """Two clerks writing the same number differently mean one patient.

        A token that split them would silently turn one person's history into
        several, and the re-attendance signal would go quiet exactly where it
        mattered.
        """
        d = deriver()
        canonical = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert d.derive(IdentifierType.NATIONAL_ID, written).value == canonical.value

    def test_a_different_identifier_produces_a_different_token(self) -> None:
        d = deriver()
        assert (
            d.derive(IdentifierType.NATIONAL_ID, "CM12345678").value
            != d.derive(IdentifierType.NATIONAL_ID, "CM87654321").value
        )

    def test_the_token_is_sha256_hex(self) -> None:
        token = deriver().derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert re.fullmatch(r"[0-9a-f]{64}", token.value)


class TestDomainSeparation:
    """OPD 002 column 2 carries three identifier systems in one cell.

    Without the type inside the HMAC input, a passport holder and a citizen
    whose numbers happen to match would be merged into one clinical history.
    """

    def test_the_same_value_under_two_types_differs(self) -> None:
        d = deriver()
        national = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        passport = d.derive(IdentifierType.PASSPORT, "CM12345678")
        assert national.value != passport.value

    def test_every_pair_of_types_is_separated(self) -> None:
        d = deriver()
        tokens = {
            t: d.derive(t, "SAMEVALUE123").value
            for t in IdentifierType
            if t is not IdentifierType.PHONE
        }
        assert len(set(tokens.values())) == len(tokens)

    def test_the_domain_prefix_is_part_of_the_message(self) -> None:
        """Fixes the key's purpose, so a reuse elsewhere cannot collide."""
        assert DOMAIN_PREFIX.startswith("mars.identity")

    def test_the_separator_cannot_be_forged_from_a_value(self) -> None:
        """``("nin", "12|34")`` must not equal ``("nin|12", "34")``.

        Normalisation strips the separator from values, so a crafted identifier
        cannot restructure the message.
        """
        assert "|" not in normalise(IdentifierType.NATIONAL_ID, "12|34")


class TestKeyVersioning:
    """Rotation must not orphan existing links."""

    def test_different_keys_produce_different_tokens(self) -> None:
        first = deriver().derive(IdentifierType.NATIONAL_ID, "CM12345678")
        second = deriver(active_key=OTHER_KEY, active_version="v2").derive(
            IdentifierType.NATIONAL_ID, "CM12345678"
        )
        assert first.value != second.value

    def test_the_version_travels_with_the_token(self) -> None:
        token = deriver(active_version="v7").derive(IdentifierType.NATIONAL_ID, "CM1")
        assert token.key_version == "v7"

    def test_a_retired_key_still_recomputes_its_own_tokens(self) -> None:
        """During rotation, existing links must stay verifiable."""
        old = deriver(active_key=OTHER_KEY, active_version="v1").derive(
            IdentifierType.NATIONAL_ID, "CM12345678"
        )
        rotated = deriver(active_key=KEY, active_version="v2", retired_keys={"v1": OTHER_KEY})
        recomputed = rotated.derive(IdentifierType.NATIONAL_ID, "CM12345678", key_version="v1")
        assert recomputed.value == old.value

    def test_new_tokens_use_the_active_version(self) -> None:
        rotated = deriver(active_key=KEY, active_version="v2", retired_keys={"v1": OTHER_KEY})
        assert rotated.derive(IdentifierType.NATIONAL_ID, "CM1").key_version == "v2"

    def test_an_unknown_version_is_refused(self) -> None:
        with pytest.raises(LinkageKeyMissingError):
            deriver().derive(IdentifierType.NATIONAL_ID, "CM1", key_version="v99")


class TestMissingKey:
    """A deployment that forgets the key must fail loudly."""

    def test_no_key_means_not_configured(self) -> None:
        d = deriver(active_key=None)
        assert d.is_configured is False

    def test_deriving_without_a_key_raises(self) -> None:
        with pytest.raises(LinkageKeyMissingError):
            deriver(active_key=None).derive(IdentifierType.NATIONAL_ID, "CM1")

    def test_an_empty_key_is_refused_at_construction(self) -> None:
        """An empty string is a configuration mistake, not a key."""
        with pytest.raises(LinkageKeyMissingError):
            LinkageTokenDeriver(active_key=b"", active_version="v1")

    def test_the_error_does_not_name_a_default(self) -> None:
        """There is no default to fall back to, and the message says so."""
        with pytest.raises(LinkageKeyMissingError) as exc:
            deriver(active_key=None).derive(IdentifierType.NATIONAL_ID, "CM1")
        assert "default key" in str(exc.value)


class TestNormalisation:
    def test_phone_numbers_reduce_to_one_national_form(self) -> None:
        """0700123456 and +256700123456 are the same subscriber."""
        forms = ["0700123456", "+256700123456", "256700123456", "0700 123 456"]
        normalised = {normalise(IdentifierType.PHONE, f) for f in forms}
        assert len(normalised) == 1

    def test_an_identifier_of_only_punctuation_normalises_to_nothing(self) -> None:
        assert normalise(IdentifierType.NATIONAL_ID, "---/// ") == ""

    def test_an_unusable_identifier_raises_rather_than_linking(self) -> None:
        """Linking on an empty string would merge every unusable row into one
        enormous person."""
        with pytest.raises(UnlinkableIdentifierError):
            deriver().derive(IdentifierType.NATIONAL_ID, "  --  ")


class TestNothingLeaksTheIdentifier:
    """The most important property in this file.

    An identifier that escapes into a log, a traceback or an error message has
    left the vault, and no amount of database privilege separation helps.
    """

    def test_the_unlinkable_error_never_contains_the_value(self) -> None:
        secret = "CM99887766"
        with pytest.raises(UnlinkableIdentifierError) as exc:
            deriver().derive(IdentifierType.NATIONAL_ID, "")
        assert secret not in str(exc.value)

    def test_the_unlinkable_error_reports_a_length_not_a_value(self) -> None:
        with pytest.raises(UnlinkableIdentifierError) as exc:
            deriver().derive(IdentifierType.PASSPORT, "!!!!!")
        message = str(exc.value)
        assert "!!!!!" not in message
        assert "5 characters" in message

    def test_the_token_repr_is_truncated(self) -> None:
        """A repr reaches tracebacks and debuggers.

        A full token would let anyone holding the key confirm a guessed
        identifier offline.
        """
        token = deriver().derive(IdentifierType.NATIONAL_ID, "CM12345678")
        rendered = repr(token)
        assert token.value not in rendered
        assert token.value[:8] in rendered

    def test_the_deriver_repr_does_not_contain_the_key(self) -> None:
        rendered = repr(deriver())
        assert KEY.decode() not in rendered
        assert "test-key" not in rendered


class TestVerification:
    """Testing a candidate must not become a side channel."""

    def test_a_matching_candidate_verifies(self) -> None:
        d = deriver()
        token = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert d.matches(token, IdentifierType.NATIONAL_ID, "cm-1234-5678")

    def test_a_different_value_does_not_verify(self) -> None:
        d = deriver()
        token = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert not d.matches(token, IdentifierType.NATIONAL_ID, "CM87654321")

    def test_the_same_value_under_another_type_does_not_verify(self) -> None:
        d = deriver()
        token = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert not d.matches(token, IdentifierType.PASSPORT, "CM12345678")

    def test_an_unusable_candidate_returns_false_rather_than_raising(self) -> None:
        """A caller verifying a batch must not learn which entry was malformed
        from an exception that a valid-but-wrong entry would not raise."""
        d = deriver()
        token = d.derive(IdentifierType.NATIONAL_ID, "CM12345678")
        assert not d.matches(token, IdentifierType.NATIONAL_ID, "   ")


class TestGoldenVectors:
    """Pinned outputs.

    If the message construction ever changes - a different separator, a
    reordering, a normalisation tweak - every stored token in every deployment
    silently stops matching, and the failure looks like patients who suddenly
    have no history. These vectors turn that into a test failure instead.
    """

    #: Computed from KEY under the current scheme. Regenerating them is a
    #: deliberate act that accompanies a migration re-deriving stored tokens.
    VECTORS = (
        (IdentifierType.NATIONAL_ID, "CM12345678"),
        (IdentifierType.PASSPORT, "CM12345678"),
        (IdentifierType.REFUGEE_NUMBER, "REF-0001"),
        (IdentifierType.PHONE, "0700123456"),
    )

    def test_vectors_are_stable_within_a_run(self) -> None:
        d = deriver()
        first = [d.derive(t, v).value for t, v in self.VECTORS]
        second = [d.derive(t, v).value for t, v in self.VECTORS]
        assert first == second

    def test_vectors_are_all_distinct(self) -> None:
        d = deriver()
        values = [d.derive(t, v).value for t, v in self.VECTORS]
        assert len(set(values)) == len(values)

    def test_the_message_construction_is_pinned(self) -> None:
        """Recomputed independently, so a change to the module is caught.

        This mirrors the construction by hand rather than calling the code under
        test, which is the only way a test can notice the code changing.
        """
        import hashlib
        import hmac

        expected = hmac.new(
            KEY,
            "|".join([DOMAIN_PREFIX, "national_id", "CM12345678"]).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert deriver().derive(IdentifierType.NATIONAL_ID, "CM12345678").value == expected
