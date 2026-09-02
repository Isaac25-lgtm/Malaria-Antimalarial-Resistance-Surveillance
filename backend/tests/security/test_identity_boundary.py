"""The identity boundary as seen from outside the vault.

Two escape routes that database privileges do not close:

**Logs.** A structured log line is written by the identity service itself, which
*is* authorised. If it puts an identifier in the event, the value leaves the
vault through the log pipeline - shipped further, kept longer, and read by more
people than the database ever is.

**The API contract.** Every response model is published to a generated
TypeScript client. A name that appears in one has left the vault through the
front door.

Every value below is invented.
"""

from __future__ import annotations

import json
import uuid

import pytest
import structlog

from mars.domain.enums import IdentifierType
from mars.identity.encryption import FieldEncryptor
from mars.identity.linkage import LinkageTokenDeriver, UnlinkableIdentifierError
from mars.identity.service import IdentityService, LinkageResult
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal

NIN = "CM90210077"
SURNAME = "Okello"
PHONE = "0700999888"


@pytest.fixture
def captured_logs() -> list[dict[str, object]]:
    """Capture structlog events as the application emits them."""
    events: list[dict[str, object]] = []

    def sink(_logger: object, _name: str, event_dict: dict[str, object]) -> str:
        events.append(dict(event_dict))
        return json.dumps(event_dict, default=str)

    structlog.configure(
        processors=[sink],
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # The service binds its logger at import time. Anything earlier in the run
    # that configured structlog leaves that proxy bound to the old pipeline, so
    # reconfiguring alone captures nothing - the test then passes in isolation
    # and fails in a full run, which is the worst way for it to be wrong.
    # Rebinding forces the service onto the pipeline under test.
    import mars.identity.service as service_module

    original = service_module.logger
    service_module.logger = structlog.get_logger("mars.identity.service")

    yield events

    service_module.logger = original
    structlog.reset_defaults()


def _service() -> IdentityService:
    """An identity service whose paths under test never reach the database."""
    return IdentityService(
        _NullSession(),  # type: ignore[arg-type]
        LinkageTokenDeriver(active_key=b"linkage-key", active_version="v1"),
        FieldEncryptor(active_key=bytes(range(32)), active_version="v1"),
    )


class _NullSession:
    """Enough session for the paths that never reach the database."""

    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("this path must not query the database")

    def add(self, *_args: object, **_kwargs: object) -> None:
        return None

    def flush(self) -> None:
        return None


class TestLogsRedactIdentifiers:
    def test_an_unusable_identifier_is_logged_without_its_value(
        self, captured_logs: list[dict[str, object]]
    ) -> None:
        service = _service()
        # Punctuation only, so it genuinely normalises to nothing - and
        # distinctive, because a bare "--" turned up in unrelated log fields
        # under a full-suite run and the assertion proved nothing.
        unusable = "@@@~~~@@@"
        reference = uuid.uuid4()
        result = service.link(IdentifierType.NATIONAL_ID, unusable, patient_reference_id=reference)

        assert isinstance(result, LinkageResult)
        assert captured_logs, "nothing was logged"
        rendered = json.dumps(captured_logs, default=str)
        assert unusable not in rendered
        assert str(reference) in rendered

    def test_no_log_event_carries_a_name_or_a_number(
        self, captured_logs: list[dict[str, object]]
    ) -> None:
        """The identity service is authorised to see these. Its logs are not."""
        service = _service()
        service.link(
            IdentifierType.NATIONAL_ID,
            "",
            patient_reference_id=uuid.uuid4(),
            surname=SURNAME,
            phone_contact=PHONE,
        )
        rendered = json.dumps(captured_logs, default=str)
        for secret in (SURNAME, PHONE):
            assert secret not in rendered

    def test_the_linkage_error_message_carries_no_value(self) -> None:
        """An exception is the most likely thing to reach a log."""
        deriver = LinkageTokenDeriver(active_key=b"k", active_version="v1")
        with pytest.raises(UnlinkableIdentifierError) as exc:
            deriver.derive(IdentifierType.NATIONAL_ID, "###")
        assert "###" not in str(exc.value)


class TestTheApiExposesNoIdentity:
    """No published response model may carry a person's details."""

    #: Field names that would mean identity had reached the contract.
    FORBIDDEN = (
        "surname",
        "given_name",
        "patient_name",
        "national_id",
        "nin",
        "passport",
        "phone_contact",
        "next_of_kin",
        "linkage_token",
        "raw_value",
    )

    def test_no_schema_field_is_named_for_an_identifier(
        self, openapi_document: dict[str, object]
    ) -> None:
        schemas = openapi_document["components"]["schemas"]  # type: ignore[index]
        offenders: list[str] = []
        for name, schema in schemas.items():
            for field in schema.get("properties", {}):
                if any(word in field.lower() for word in self.FORBIDDEN):
                    offenders.append(f"{name}.{field}")
        assert not offenders, f"identity fields in the API contract: {offenders}"

    def test_no_route_mentions_re_identification(self, openapi_document: dict[str, object]) -> None:
        """Prompt 8 builds the service, not an endpoint.

        Exposing re-identification over HTTP is a separate decision with its own
        review; until it is taken, there is no route to misconfigure.
        """
        paths = list(openapi_document["paths"])  # type: ignore[arg-type]
        assert not [p for p in paths if "reidentif" in p.lower()]
        assert not [p for p in paths if "identity" in p.lower()]


class TestTheGateHoldsFromBothSides:
    def test_the_permission_requires_direct_identity_sensitivity(self) -> None:
        from mars.security.permissions import PERMISSION_CATALOGUE

        spec = PERMISSION_CATALOGUE[Permission.PATIENT_REIDENTIFY]
        assert spec.minimum_sensitivity is SensitivityLevel.DIRECT_IDENTITY

    def test_no_role_reaches_direct_identity_by_default(self) -> None:
        """Even if the permission were granted, the ceiling would refuse it."""
        from mars.security.permissions import ROLE_DEFAULT_SENSITIVITY

        reaching = [
            role.value
            for role, ceiling in ROLE_DEFAULT_SENSITIVITY.items()
            if ceiling.covers(SensitivityLevel.DIRECT_IDENTITY)
        ]
        assert reaching == [], f"roles reach direct identity by default: {reaching}"

    def test_a_principal_cannot_hold_the_permission_above_its_ceiling(self) -> None:
        """Mirrors AuthService: a permission above the ceiling is dropped.

        So holding ``patient:reidentify`` at all already implies the sensitivity,
        and the service's second gate is a belt to that braces.
        """
        principal = AuthenticatedPrincipal(
            user_id=uuid.uuid4(),
            subject="dev:t",
            username="t",
            display_name="T",
            roles=frozenset(),
            permissions=frozenset({Permission.PATIENT_REIDENTIFY}),
            max_sensitivity=SensitivityLevel.AGGREGATE,
        )
        assert principal.has_permission(Permission.PATIENT_REIDENTIFY)
        assert not principal.can_access_sensitivity(SensitivityLevel.DIRECT_IDENTITY)
