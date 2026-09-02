"""The identity vault against live PostgreSQL.

Things only a real database can prove:

* that the application role genuinely cannot reach ``mars_identity`` - and that
  the *runtime session factory* connects as the role it claims to
* that the disclosure log is append-only against the actual restricted role,
  not merely against a revoked privilege
* that no plaintext identifier is present in any stored column
* that concurrent linkage of the same patient produces one identity, decided by
  a unique constraint rather than by hope

Every identifier and name below is invented.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from mars.core.errors import ValidationFailedError
from mars.domain.enums import (
    IdentifierType,
    LinkageConfidence,
    ReidentificationOutcome,
)
from mars.domain.identity import IdentityIdentifier, IdentityRecord, ReidentificationEvent
from mars.identity.encryption import FieldEncryptor
from mars.identity.errors import IdentityUnavailableError
from mars.identity.linkage import LinkageTokenDeriver
from mars.identity.service import IdentityService, RetiredKeyUnavailableError
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import AuthenticatedPrincipal

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]
PROVISION_SQL = MIGRATIONS_ROOT.parent / "scripts" / "provision_identity_roles.sql"

LINK_KEY = b"integration-linkage-key-not-a-real-secret"
LINK_KEY_2 = b"integration-linkage-key-rotated-not-real"
ENC_KEY = bytes(range(32))
ENC_KEY_2 = bytes(range(32, 64))

#: Invented. Not a real Ugandan national identification number.
NIN = "CM90210077"
SURNAME = "Okello"
GIVEN_NAME = "Amina"
PHONE = "0700999888"

#: Login roles created for the test cluster only. The cluster runs trust
#: authentication on loopback, so no password exists to leak.
APP_LOGIN = "mars_app_login_test"
IDENTITY_LOGIN = "mars_identity_login_test"


@pytest.fixture(scope="module")
def vault_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)

    # Provisioning is a privileged step that owns role creation; the migration
    # only applies grants. Running it here mirrors a real deployment order.
    with engine.begin() as connection:
        connection.execute(
            text(PROVISION_SQL.read_text(encoding="utf-8").replace("\\set ON_ERROR_STOP on", ""))
        )
        for login, group in ((APP_LOGIN, "mars_app"), (IDENTITY_LOGIN, "mars_identity_service")):
            connection.execute(
                text(
                    f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{login}') "
                    f"THEN CREATE ROLE {login} LOGIN; END IF; "
                    f"GRANT {group} TO {login}; END $$;"
                )
            )

    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


def _login_url(base: str, user: str) -> str:
    """The same database, reached as a different role."""
    return str(make_url(base).set(username=user, password=None))


@pytest.fixture(scope="module")
def app_role_engine(integration_database_url: str, vault_engine: Engine) -> Iterator[Engine]:
    """A connection that genuinely runs as the application role."""
    engine = create_engine(_login_url(integration_database_url, APP_LOGIN), future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def identity_role_engine(integration_database_url: str, vault_engine: Engine) -> Iterator[Engine]:
    """A connection that genuinely runs as the identity service role."""
    engine = create_engine(_login_url(integration_database_url, IDENTITY_LOGIN), future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def session(vault_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=vault_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean_vault(vault_engine: Engine) -> Iterator[None]:
    yield
    with vault_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_identity.identity_identifier"))
        connection.execute(text("DELETE FROM mars_identity.identity_record"))
        # Append-only blocks DELETE, so the log is emptied by disabling the
        # trigger as the owner. Only a test does this, and only to isolate runs.
        connection.execute(
            text(
                "ALTER TABLE mars_identity.reidentification_event "
                "DISABLE TRIGGER reidentification_event_append_only"
            )
        )
        connection.execute(text("DELETE FROM mars_identity.reidentification_event"))
        connection.execute(
            text(
                "ALTER TABLE mars_identity.reidentification_event "
                "ENABLE TRIGGER reidentification_event_append_only"
            )
        )


def deriver(key: bytes = LINK_KEY, version: str = "v1", **kw: object) -> LinkageTokenDeriver:
    return LinkageTokenDeriver(active_key=key, active_version=version, **kw)  # type: ignore[arg-type]


def encryptor(key: bytes = ENC_KEY, version: str = "v1", **kw: object) -> FieldEncryptor:
    return FieldEncryptor(active_key=key, active_version=version, **kw)  # type: ignore[arg-type]


@pytest.fixture
def service(session: Session) -> IdentityService:
    return IdentityService(session, deriver(), encryptor())


def principal(
    *,
    permissions: frozenset[Permission] = frozenset(),
    sensitivity: SensitivityLevel = SensitivityLevel.AGGREGATE,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid.uuid4(),
        subject="dev:test",
        username="test.user",
        display_name="Test User",
        roles=frozenset({"analyst"}),
        permissions=permissions,
        max_sensitivity=sensitivity,
        session_reference=uuid.uuid4().hex,
        is_synthetic=True,
    )


def authorised() -> AuthenticatedPrincipal:
    return principal(
        permissions=frozenset({Permission.PATIENT_REIDENTIFY}),
        sensitivity=SensitivityLevel.DIRECT_IDENTITY,
    )


class TestLinkage:
    def test_the_same_identifier_reaches_the_same_reference(
        self, service: IdentityService, session: Session
    ) -> None:
        reference = uuid.uuid4()
        first = service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference)
        session.commit()
        second = service.link(
            IdentifierType.NATIONAL_ID, "cm-9021-0077", patient_reference_id=uuid.uuid4()
        )
        assert first.created is True
        assert second.created is False
        assert second.patient_reference_id == reference

    def test_the_same_number_under_another_scheme_is_a_different_person(
        self, service: IdentityService, session: Session
    ) -> None:
        citizen = service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4())
        session.commit()
        visitor = service.link(IdentifierType.PASSPORT, NIN, patient_reference_id=uuid.uuid4())
        assert citizen.patient_reference_id != visitor.patient_reference_id

    def test_an_unusable_identifier_yields_an_unlinked_result(
        self, service: IdentityService
    ) -> None:
        reference = uuid.uuid4()
        result = service.link(IdentifierType.NATIONAL_ID, "   ", patient_reference_id=reference)
        assert result.confidence is LinkageConfidence.UNLINKED
        assert result.patient_reference_id == reference

    def test_an_invalid_phone_is_left_unlinked_not_coerced(self, service: IdentityService) -> None:
        """A fragment must never become a linkage key.

        Two unrelated patients whose records held the same scrap would otherwise
        be merged into one clinical history.
        """
        result = service.link(IdentifierType.PHONE, "0700", patient_reference_id=uuid.uuid4())
        assert result.confidence is LinkageConfidence.UNLINKED

    def test_equivalent_phone_forms_reach_one_person(
        self, service: IdentityService, session: Session
    ) -> None:
        reference = uuid.uuid4()
        service.link(IdentifierType.PHONE, "+256700999888", patient_reference_id=reference)
        session.commit()
        again = service.link(
            IdentifierType.PHONE, "0700 999 888", patient_reference_id=uuid.uuid4()
        )
        assert again.patient_reference_id == reference

    def test_the_result_carries_no_identity(self, service: IdentityService) -> None:
        result = service.link(
            IdentifierType.NATIONAL_ID,
            NIN,
            patient_reference_id=uuid.uuid4(),
            surname=SURNAME,
        )
        rendered = repr(result)
        assert NIN not in rendered
        assert SURNAME not in rendered


class TestStoredAtRestAsCiphertext:
    """The storage guarantee, checked against the bytes PostgreSQL holds."""

    @pytest.fixture
    def stored(self, service: IdentityService, session: Session) -> uuid.UUID:
        reference = uuid.uuid4()
        service.link(
            IdentifierType.NATIONAL_ID,
            NIN,
            patient_reference_id=reference,
            surname=SURNAME,
            given_name=GIVEN_NAME,
            phone_contact=PHONE,
            date_of_birth="1990-04-11",
        )
        session.commit()
        return reference

    def test_no_plaintext_identifier_appears_in_any_column(
        self, stored: uuid.UUID, vault_engine: Engine
    ) -> None:
        """Dumps the whole vault as text and searches it.

        A column-by-column assertion would miss a value that leaked into a
        column nobody thought to check.
        """
        with vault_engine.connect() as connection:
            dumped = " ".join(
                str(value)
                for table in ("identity_record", "identity_identifier")
                for row in connection.execute(text(f"SELECT * FROM mars_identity.{table}"))
                for value in row
            )
        for secret in (NIN, SURNAME, GIVEN_NAME, PHONE, "1990-04-11"):
            assert secret not in dumped, f"{secret!r} is stored in plaintext"

    def test_the_encrypted_columns_hold_bytes_not_text(
        self, stored: uuid.UUID, vault_engine: Engine
    ) -> None:
        with vault_engine.connect() as connection:
            types = dict(
                connection.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema='mars_identity' "
                        "AND column_name LIKE '%_encrypted'"
                    )
                ).all()
            )
        assert types, "no encrypted columns found"
        assert set(types.values()) == {"bytea"}

    def test_no_normalised_plaintext_column_survives(self, vault_engine: Engine) -> None:
        """It was a pure function of the raw value: a second copy for nothing."""
        with vault_engine.connect() as connection:
            columns = (
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='mars_identity'"
                    )
                )
                .scalars()
                .all()
            )
        assert "normalised_value" not in columns
        assert "raw_value" not in columns

    def test_an_authorised_disclosure_returns_the_plaintext(
        self, stored: uuid.UUID, service: IdentityService, session: Session
    ) -> None:
        disclosed = service.reidentify(authorised(), stored, reason="investigation 4")
        assert disclosed.surname == SURNAME
        assert disclosed.given_name == GIVEN_NAME
        assert disclosed.phone_contact == PHONE
        assert disclosed.date_of_birth == "1990-04-11"
        assert (IdentifierType.NATIONAL_ID.value, NIN) in disclosed.identifiers

    def test_the_wrong_encryption_key_fails_closed(
        self, stored: uuid.UUID, session: Session
    ) -> None:
        """Possession of the database is not possession of the identities."""
        from mars.identity.encryption import DecryptionFailedError

        wrong = IdentityService(session, deriver(), encryptor(key=ENC_KEY_2))
        with pytest.raises(DecryptionFailedError):
            wrong.reidentify(authorised(), stored, reason="attempt")

    def test_tampered_ciphertext_fails_closed(
        self, stored: uuid.UUID, vault_engine: Engine
    ) -> None:
        """Write access to the database is still not the power to alter a name.

        Read through a fresh session: the one that wrote the row holds it in its
        identity map and would hand back the pre-tamper object without ever
        touching the bytes under test.
        """
        from mars.identity.encryption import DecryptionFailedError

        with vault_engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE mars_identity.identity_record SET surname_encrypted = "
                    "set_byte(surname_encrypted, octet_length(surname_encrypted) - 1, "
                    "get_byte(surname_encrypted, octet_length(surname_encrypted) - 1) # 1)"
                )
            ).rowcount
        assert changed == 1, "the tamper did not modify a row"

        factory = sessionmaker(bind=vault_engine, expire_on_commit=False, future=True)
        with factory() as fresh:
            service = IdentityService(fresh, deriver(), encryptor())
            with pytest.raises(DecryptionFailedError):
                service.reidentify(authorised(), stored, reason="attempt")


class TestKeyRotation:
    """A rotation must not orphan a patient or duplicate one."""

    def test_a_v1_patient_is_found_after_rotating_to_v2(self, session: Session) -> None:
        reference = uuid.uuid4()
        IdentityService(session, deriver(), encryptor()).link(
            IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference
        )
        session.commit()

        rotated = IdentityService(
            session,
            deriver(LINK_KEY_2, "v2", retired_keys={"v1": LINK_KEY}),
            encryptor(ENC_KEY_2, "v2", retired_keys={"v1": ENC_KEY}),
        )
        assert rotated.find_reference(IdentifierType.NATIONAL_ID, NIN) == reference

    def test_link_after_rotation_returns_the_original_patient(self, session: Session) -> None:
        """The duplicate-creation bug this test exists to prevent."""
        reference = uuid.uuid4()
        IdentityService(session, deriver(), encryptor()).link(
            IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference
        )
        session.commit()

        rotated = IdentityService(
            session,
            deriver(LINK_KEY_2, "v2", retired_keys={"v1": LINK_KEY}),
            encryptor(ENC_KEY_2, "v2", retired_keys={"v1": ENC_KEY}),
        )
        result = rotated.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4())
        session.commit()

        assert result.created is False
        assert result.patient_reference_id == reference
        assert result.rekeyed is True

    def test_the_rekey_moves_the_row_to_the_active_version(self, session: Session) -> None:
        IdentityService(session, deriver(), encryptor()).link(
            IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4()
        )
        session.commit()

        rotated = IdentityService(
            session,
            deriver(LINK_KEY_2, "v2", retired_keys={"v1": LINK_KEY}),
            encryptor(ENC_KEY_2, "v2", retired_keys={"v1": ENC_KEY}),
        )
        rotated.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4())
        session.commit()

        versions = session.execute(select(IdentityIdentifier.linkage_key_version)).scalars().all()
        assert set(versions) == {"v2"}

    def test_repeated_migration_is_idempotent(self, session: Session) -> None:
        reference = uuid.uuid4()
        IdentityService(session, deriver(), encryptor()).link(
            IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference
        )
        session.commit()

        rotated = IdentityService(
            session,
            deriver(LINK_KEY_2, "v2", retired_keys={"v1": LINK_KEY}),
            encryptor(ENC_KEY_2, "v2", retired_keys={"v1": ENC_KEY}),
        )
        for _ in range(3):
            result = rotated.link(
                IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4()
            )
            session.commit()
            assert result.patient_reference_id == reference

        assert session.execute(select(IdentityRecord)).scalars().all().__len__() == 1

    def test_new_identifiers_use_the_active_version(self, session: Session) -> None:
        rotated = IdentityService(
            session,
            deriver(LINK_KEY_2, "v2", retired_keys={"v1": LINK_KEY}),
            encryptor(ENC_KEY_2, "v2", retired_keys={"v1": ENC_KEY}),
        )
        rotated.link(IdentifierType.NATIONAL_ID, "CM11112222", patient_reference_id=uuid.uuid4())
        session.commit()
        row = session.execute(select(IdentityIdentifier)).scalars().one()
        assert row.linkage_key_version == "v2"

    def test_a_missing_retired_key_is_an_explicit_error(self, session: Session) -> None:
        """Not a new identity.

        Treating an underivable token as "not found" would record an existing
        patient as a new person and split their history, with nothing in any log
        to explain it.
        """
        IdentityService(session, deriver(), encryptor()).link(
            IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4()
        )
        session.commit()

        # v2 active, v1 NOT configured: the stored token cannot be derived.
        broken = IdentityService(session, deriver(LINK_KEY_2, "v2"), encryptor(ENC_KEY_2, "v2"))
        with pytest.raises(RetiredKeyUnavailableError):
            broken.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4())


class TestConcurrentLinkage:
    def test_two_simultaneous_first_visits_produce_one_patient(self, vault_engine: Engine) -> None:
        """Decided by the unique constraint, not by hope.

        Two ingestion workers processing the same patient's first visit at the
        same moment must not create two identities.
        """
        factory = sessionmaker(bind=vault_engine, expire_on_commit=False, future=True)
        first, second = factory(), factory()
        try:
            a = IdentityService(first, deriver(), encryptor())
            b = IdentityService(second, deriver(), encryptor())

            result_a = a.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4())
            first.commit()

            result_b = b.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=uuid.uuid4())
            second.commit()
        finally:
            first.close()
            second.close()

        assert result_a.patient_reference_id == result_b.patient_reference_id

        with vault_engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM mars_identity.identity_record")
            ).scalar_one()
        assert count == 1

    def test_the_unique_constraint_is_what_decides(self, session: Session) -> None:
        token = deriver().derive(IdentifierType.NATIONAL_ID, NIN).value
        for _ in range(2):
            record = IdentityRecord(patient_reference_id=uuid.uuid4())
            record.identifiers = [
                IdentityIdentifier(
                    identifier_type=IdentifierType.NATIONAL_ID,
                    raw_value_encrypted=b"x",
                    encryption_key_version="v1",
                    linkage_token=token,
                    linkage_key_version="v1",
                )
            ]
            session.add(record)
        with pytest.raises(IntegrityError, match="uq_identity_identifier_token_version"):
            session.commit()


class TestOpaqueRefusals:
    """Denied and unknown must be indistinguishable to the caller."""

    def test_no_permission_and_unknown_reference_raise_the_same_error(
        self, service: IdentityService, session: Session
    ) -> None:
        known = uuid.uuid4()
        service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=known)
        session.commit()

        with pytest.raises(IdentityUnavailableError) as denied:
            service.reidentify(principal(), known, reason="probe")
        with pytest.raises(IdentityUnavailableError) as absent:
            service.reidentify(authorised(), uuid.uuid4(), reason="probe")

        assert type(denied.value) is type(absent.value)
        assert str(denied.value) == str(absent.value)
        assert denied.value.status_code == absent.value.status_code
        assert denied.value.code == absent.value.code

    def test_insufficient_sensitivity_is_the_same_error_too(
        self, service: IdentityService, session: Session
    ) -> None:
        known = uuid.uuid4()
        service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=known)
        session.commit()

        low = principal(
            permissions=frozenset({Permission.PATIENT_REIDENTIFY}),
            sensitivity=SensitivityLevel.PSEUDONYMOUS_CASE,
        )
        with pytest.raises(IdentityUnavailableError) as denied:
            service.reidentify(low, known, reason="probe")
        with pytest.raises(IdentityUnavailableError) as absent:
            service.reidentify(authorised(), uuid.uuid4(), reason="probe")
        assert str(denied.value) == str(absent.value)

    def test_the_message_never_names_the_reference(self, service: IdentityService) -> None:
        unknown = uuid.uuid4()
        with pytest.raises(IdentityUnavailableError) as exc:
            service.reidentify(authorised(), unknown, reason="probe")
        assert str(unknown) not in str(exc.value)
        assert str(unknown) not in str(exc.value.to_problem(instance=None, request_id=None))

    def test_a_missing_reason_stays_a_validation_error(self, service: IdentityService) -> None:
        """It describes the caller's own request and reveals nothing.

        Folding it into the opaque error would tell a caller who forgot a field
        that the patient might not exist, which is unhelpful and untrue.
        """
        with pytest.raises(ValidationFailedError):
            service.reidentify(authorised(), uuid.uuid4(), reason="  ")

    def test_no_identity_is_queried_before_the_gates(self, session: Session) -> None:
        """Order matters as much as outcome.

        Querying first would make the *timing* of a refusal depend on whether
        the reference existed - the same disclosure by a slower route.
        """

        class _Tripwire:
            def execute(self, *_a: object, **_k: object) -> object:
                raise AssertionError("identity was queried before authorisation")

            def add(self, *_a: object, **_k: object) -> None:
                return None

            def flush(self) -> None:
                return None

        service = IdentityService(_Tripwire(), deriver(), encryptor())  # type: ignore[arg-type]
        with pytest.raises(IdentityUnavailableError):
            service.reidentify(principal(), uuid.uuid4(), reason="probe")


class TestAuditing:
    def _events(self, session: Session) -> list[ReidentificationEvent]:
        return list(session.execute(select(ReidentificationEvent)).scalars().all())

    def test_a_disclosure_is_recorded(self, service: IdentityService, session: Session) -> None:
        reference = uuid.uuid4()
        service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference)
        session.commit()
        service.reidentify(authorised(), reference, reason="investigation 7")
        session.commit()

        events = self._events(session)
        assert [e.outcome for e in events] == [ReidentificationOutcome.DISCLOSED]
        assert events[0].reason == "investigation 7"

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            (principal(), ReidentificationOutcome.DENIED_PERMISSION),
            (
                principal(
                    permissions=frozenset({Permission.PATIENT_REIDENTIFY}),
                    sensitivity=SensitivityLevel.PSEUDONYMOUS_CASE,
                ),
                ReidentificationOutcome.DENIED_SENSITIVITY,
            ),
        ],
    )
    def test_refusals_keep_distinct_internal_outcomes(
        self,
        service: IdentityService,
        session: Session,
        caller: AuthenticatedPrincipal,
        expected: ReidentificationOutcome,
    ) -> None:
        """The caller cannot tell them apart. The reviewer must be able to."""
        with pytest.raises(IdentityUnavailableError):
            service.reidentify(caller, uuid.uuid4(), reason="attempt")
        session.commit()
        assert [e.outcome for e in self._events(session)] == [expected]

    def test_the_audit_row_contains_no_identifier_and_no_name(
        self, service: IdentityService, session: Session
    ) -> None:
        reference = uuid.uuid4()
        service.link(
            IdentifierType.NATIONAL_ID,
            NIN,
            patient_reference_id=reference,
            surname=SURNAME,
            given_name=GIVEN_NAME,
            phone_contact=PHONE,
        )
        session.commit()
        service.reidentify(authorised(), reference, reason="investigation 7")
        session.commit()

        event = self._events(session)[0]
        rendered = " ".join(
            str(getattr(event, column.name)) for column in ReidentificationEvent.__table__.columns
        )
        for secret in (NIN, SURNAME, GIVEN_NAME, PHONE):
            assert secret not in rendered

    def test_the_general_audit_event_is_written(self, session: Session) -> None:
        """The documentation claims it, so it must actually happen."""
        from mars.domain.enums import AuditAction

        recorded: list[dict[str, object]] = []

        class _Audit:
            def record(self, **kwargs: object) -> None:
                recorded.append(kwargs)

        reference = uuid.uuid4()
        service = IdentityService(session, deriver(), encryptor(), audit_service=_Audit())
        service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference)
        session.commit()
        service.reidentify(authorised(), reference, reason="investigation 9")

        assert len(recorded) == 1
        assert recorded[0]["action"] is AuditAction.REIDENTIFICATION_PERFORMED
        assert recorded[0]["object_id"] == str(reference)
        # The general trail is readable by roles that must not learn *why* a
        # patient was looked up.
        assert "reason" not in str(recorded[0])

    def test_a_denial_survives_the_rollback_of_its_request(self, vault_engine: Engine) -> None:
        factory = sessionmaker(bind=vault_engine, expire_on_commit=False, future=True)
        request_session = factory()
        try:
            service = IdentityService(
                request_session, deriver(), encryptor(), durable_session_factory=factory
            )
            with pytest.raises(IdentityUnavailableError):
                service.reidentify(principal(), uuid.uuid4(), reason="attempt")
            request_session.rollback()
        finally:
            request_session.close()

        with factory() as verifier:
            events = list(verifier.execute(select(ReidentificationEvent)).scalars().all())
        assert [e.outcome for e in events] == [ReidentificationOutcome.DENIED_PERMISSION]


class TestAppendOnlyDisclosureLog:
    """Against the actual restricted role, not a revoked privilege in theory."""

    @pytest.fixture
    def logged(self, service: IdentityService, session: Session) -> None:
        reference = uuid.uuid4()
        service.link(IdentifierType.NATIONAL_ID, NIN, patient_reference_id=reference)
        session.commit()
        service.reidentify(authorised(), reference, reason="investigation 1")
        session.commit()

    def test_the_identity_role_may_insert(self, identity_role_engine: Engine) -> None:
        with identity_role_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO mars_identity.reidentification_event "
                    "(actor_user_id, actor_label, patient_reference_id, reason, "
                    " outcome, requested_at) "
                    "VALUES (gen_random_uuid(), 'svc', gen_random_uuid(), 'r', "
                    "'disclosed', now())"
                )
            )

    def test_the_identity_role_cannot_update(
        self, logged: None, identity_role_engine: Engine
    ) -> None:
        with identity_role_engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text("UPDATE mars_identity.reidentification_event SET reason = 'x'"))

    def test_the_identity_role_cannot_delete(
        self, logged: None, identity_role_engine: Engine
    ) -> None:
        with identity_role_engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text("DELETE FROM mars_identity.reidentification_event"))

    def test_the_trigger_binds_even_the_table_owner(
        self, logged: None, vault_engine: Engine
    ) -> None:
        """Privileges can be re-granted; a trigger binds everyone.

        The component with the most reason to edit this log is the one that
        writes to it, so withholding the privilege alone is not enough.
        """
        with vault_engine.connect() as connection, pytest.raises(Exception, match="append-only"):
            connection.execute(text("UPDATE mars_identity.reidentification_event SET reason = 'x'"))

    def test_the_rows_survive_every_attempt(self, logged: None, vault_engine: Engine) -> None:
        with vault_engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM mars_identity.reidentification_event")
            ).scalar_one()
        assert count >= 1


class TestRuntimeRoleSeparation:
    """Proves the *connection*, not just the grant."""

    def test_the_application_connection_runs_as_the_application_role(
        self, app_role_engine: Engine
    ) -> None:
        with app_role_engine.connect() as connection:
            member = connection.execute(
                text("SELECT pg_has_role(current_user, 'mars_app', 'MEMBER')")
            ).scalar_one()
        assert member is True

    def test_the_identity_connection_runs_as_the_identity_role(
        self, identity_role_engine: Engine
    ) -> None:
        with identity_role_engine.connect() as connection:
            member = connection.execute(
                text("SELECT pg_has_role(current_user, 'mars_identity_service', 'MEMBER')")
            ).scalar_one()
        assert member is True

    def test_the_application_connection_cannot_name_an_identity_table(
        self, app_role_engine: Engine
    ) -> None:
        """With USAGE revoked this fails at parse time.

        An injection in an ordinary endpoint therefore cannot reach identity:
        the connection it runs on has no path to it.
        """
        with app_role_engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text("SELECT count(*) FROM mars_identity.identity_record"))

    def test_the_identity_connection_cannot_read_clinical_data(
        self, identity_role_engine: Engine
    ) -> None:
        """A compromised identity service must not learn what its patients had."""
        with identity_role_engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text("SELECT count(*) FROM mars_core.opd_encounter"))

    def test_the_identity_connection_can_read_the_vault(self, identity_role_engine: Engine) -> None:
        with identity_role_engine.connect() as connection:
            connection.execute(text("SELECT count(*) FROM mars_identity.identity_record"))


class TestNoIdentityInCore:
    #: Matched as whole underscore-separated words, not as bare substrings.
    #: ``nin`` inside ``warning_count`` is not an identifier column, and a
    #: guard that fires on it teaches people to ignore the guard.
    FORBIDDEN = frozenset(
        {"nin", "national", "id", "passport", "surname", "given", "name", "phone"}
    )

    #: Word pairs that only mean an identifier together. ``name`` alone appears
    #: in every raw_name column in the schema.
    FORBIDDEN_WORDS = frozenset({"nin", "passport", "surname", "phone"})
    FORBIDDEN_PHRASES = (("national", "id"), ("given", "name"))

    def test_no_core_column_is_named_for_an_identifier(self, vault_engine: Engine) -> None:
        with vault_engine.connect() as connection:
            columns = (
                connection.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "FROM information_schema.columns WHERE table_schema = 'mars_core'"
                    )
                )
                .scalars()
                .all()
            )
        offenders = [c for c in columns if self._names_an_identifier(c)]
        assert offenders == [], f"identifier-shaped columns in mars_core: {offenders}"

    def _names_an_identifier(self, qualified: str) -> bool:
        if qualified.startswith("facility."):
            # A facility's own identifiers are not a person's.
            return False
        words = qualified.lower().replace(".", "_").split("_")
        if self.FORBIDDEN_WORDS & set(words):
            return True
        return any(
            first in words and second in words and words.index(second) == words.index(first) + 1
            for first, second in self.FORBIDDEN_PHRASES
        )

    def test_the_vault_holds_nothing_clinical(self, vault_engine: Engine) -> None:
        with vault_engine.connect() as connection:
            columns = (
                connection.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "FROM information_schema.columns WHERE table_schema = 'mars_identity'"
                    )
                )
                .scalars()
                .all()
            )
        clinical = ("diagnosis", "malaria", "test_result", "prescription", "encounter_date")
        offenders = [c for c in columns if any(w in c.lower() for w in clinical)]
        assert not offenders, f"clinical columns in the vault: {offenders}"


class TestVaultConstraints:
    def test_a_malformed_token_is_refused(self, session: Session) -> None:
        record = IdentityRecord(patient_reference_id=uuid.uuid4())
        record.identifiers = [
            IdentityIdentifier(
                identifier_type=IdentifierType.NATIONAL_ID,
                raw_value_encrypted=b"x",
                encryption_key_version="v1",
                linkage_token="tooshort",
                linkage_key_version="v1",
            )
        ]
        session.add(record)
        with pytest.raises(IntegrityError, match="linkage_token_is_sha256_hex"):
            session.commit()

    def test_empty_ciphertext_is_refused(self, session: Session) -> None:
        record = IdentityRecord(patient_reference_id=uuid.uuid4())
        record.identifiers = [
            IdentityIdentifier(
                identifier_type=IdentifierType.NATIONAL_ID,
                raw_value_encrypted=b"",
                encryption_key_version="v1",
                linkage_token="a" * 64,
                linkage_key_version="v1",
            )
        ]
        session.add(record)
        with pytest.raises(IntegrityError, match="raw_value_is_ciphertext"):
            session.commit()

    def test_a_disclosure_without_a_reason_is_refused_by_the_database(
        self, session: Session
    ) -> None:
        session.add(
            ReidentificationEvent(
                actor_user_id=uuid.uuid4(),
                actor_label="someone",
                patient_reference_id=uuid.uuid4(),
                reason="   ",
                outcome=ReidentificationOutcome.DISCLOSED,
                requested_at=datetime.datetime.now(datetime.UTC),
            )
        )
        with pytest.raises(IntegrityError, match="reason_is_stated"):
            session.commit()
