"""PostgreSQL integration tests.

Every test here is marked ``integration`` and requires ``MARS_TEST_DATABASE_URL``
to point at a disposable PostgreSQL database. Without it they skip, and the skip
is reported - an absent database never produces a false pass.

    set MARS_TEST_DATABASE_URL=postgresql+psycopg://mars:mars@localhost:5432/mars_test
    pytest tests/integration -m integration
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mars.db.schemas import ALL_SCHEMAS, IDENTITY
from mars.domain.audit import AuditEvent
from mars.domain.enums import (
    AuditAction,
    AuditOutcome,
    GeographyLevel,
    LifecycleStatus,
    MethodKind,
)
from mars.domain.geography import GeographyUnit
from mars.security.permissions import SystemRole
from mars.services.audit_service import AuditService
from mars.services.governance_service import ConfigurationService, MethodRegistryService
from tests.conftest import make_principal

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def engine(integration_database_url: str) -> Iterator[Engine]:
    """Apply migrations to a disposable database, then tear them down."""
    eng = create_engine(integration_database_url, future=True)

    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)

    command.upgrade(config, "head")
    yield eng
    command.downgrade(config, "base")
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


class TestSchemaBoundaries:
    def test_every_mars_schema_exists(self, engine: Engine) -> None:
        with engine.connect() as connection:
            present = set(
                connection.execute(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = ANY(:names)"
                    ),
                    {"names": list(ALL_SCHEMAS)},
                ).scalars()
            )
        assert present == set(ALL_SCHEMAS)

    def test_identity_schema_holds_only_the_vault(self, engine: Engine) -> None:
        """The boundary was created before the data, never retrofitted.

        Empty through phases 1-2; filled by Prompt 8 with exactly three tables
        and nothing else. Asserting the *contents* rather than emptiness keeps
        the check meaningful now that the schema is populated: a table appearing
        here that is not part of the vault would mean something had drifted
        across the identity boundary.
        """
        with engine.connect() as connection:
            tables = sorted(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = :s"),
                    {"s": IDENTITY},
                ).scalars()
            )
        assert tables == [
            "identity_identifier",
            "identity_record",
            "reidentification_event",
        ], f"unexpected tables in {IDENTITY}: {tables}"

    def test_pgcrypto_is_available(self, engine: Engine) -> None:
        """gen_random_uuid() backs every primary key default."""
        with engine.connect() as connection:
            value = connection.execute(text("SELECT gen_random_uuid()")).scalar_one()
        assert uuid.UUID(str(value))

    def test_investigation_timeline_is_database_enforced_append_only(self, engine: Engine) -> None:
        with engine.connect() as connection:
            trigger = connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'mars_core.investigation_event'::regclass "
                    "AND NOT tgisinternal"
                )
            ).scalar_one_or_none()
            event_kinds = set(
                connection.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum e "
                        "JOIN pg_type t ON t.oid = e.enumtypid "
                        "JOIN pg_namespace n ON n.oid = t.typnamespace "
                        "WHERE n.nspname = 'mars_core' "
                        "AND t.typname = 'investigation_event_kind'"
                    )
                ).scalars()
            )
        assert trigger == "investigation_event_append_only"
        assert "started" in event_kinds
        assert "outcome_recorded" not in event_kinds


class TestMigrationReversibility:
    def test_downgrade_then_upgrade_restores_the_schema(
        self, integration_database_url: str, engine: Engine
    ) -> None:
        """A migration that cannot round-trip cannot be safely deployed."""
        config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
        config.set_main_option("sqlalchemy.url", integration_database_url)

        def table_count() -> int:
            with engine.connect() as connection:
                return connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = ANY(:names)"
                    ),
                    {"names": list(ALL_SCHEMAS)},
                ).scalar_one()

        before = table_count()
        command.downgrade(config, "0001_schema_baseline")
        assert table_count() == 0
        command.upgrade(config, "head")
        assert table_count() == before


class TestAuditImmutabilityTrigger:
    """The database is the layer a raw-SQL path cannot bypass."""

    def _insert_event(self, session: Session) -> uuid.UUID:
        service = AuditService(session)
        principal = make_principal(role=SystemRole.ADMINISTRATOR)
        event = service.record(
            action=AuditAction.LOGIN_SUCCEEDED,
            principal=principal,
            object_type="user_session",
            object_id="test-session",
        )
        session.commit()
        return event.id

    def test_raw_sql_update_is_rejected(self, session: Session) -> None:
        event_id = self._insert_event(session)
        with pytest.raises(DatabaseError, match="append-only"):
            session.execute(
                text("UPDATE mars_audit.audit_event SET reason = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )
        session.rollback()

    def test_raw_sql_delete_is_rejected(self, session: Session) -> None:
        event_id = self._insert_event(session)
        with pytest.raises(DatabaseError, match="append-only"):
            session.execute(
                text("DELETE FROM mars_audit.audit_event WHERE id = :id"), {"id": event_id}
            )
        session.rollback()

    def test_insert_is_permitted(self, session: Session) -> None:
        event_id = self._insert_event(session)
        stored = session.get(AuditEvent, event_id)
        assert stored is not None
        assert stored.outcome is AuditOutcome.SUCCEEDED


class TestDurableDenialAudit:
    """A denial must outlive the rollback of the request that caused it.

    An authorisation denial ends the request with an exception, so the request
    transaction is rolled back by design. A denial written through that
    transaction would vanish with it - leaving no record that someone was
    refused, which is precisely the event blueprint section 066 requires to be
    reconstructable.

    The unit test covers this with fakes. Only a real transaction boundary
    proves it.
    """

    def test_denial_survives_the_rejected_request_rollback(self, engine: Engine) -> None:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        marker = f"integration-denial-{uuid.uuid4().hex[:12]}"

        principal = make_principal(role=SystemRole.DISTRICT_HSD)

        request_session = factory()
        try:
            audit = AuditService(request_session, durable_session_factory=factory)
            audit.record_denial(principal=principal, reason=f"missing permission: {marker}")
            # The rejected request aborts here, exactly as a 403 would.
            raise PermissionError("simulated authorisation denial")
        except PermissionError:
            request_session.rollback()
        finally:
            request_session.close()

        with factory() as verify:
            rows = verify.execute(
                text(
                    "SELECT action::text, outcome::text FROM mars_audit.audit_event "
                    "WHERE reason LIKE :marker"
                ),
                {"marker": f"%{marker}%"},
            ).all()

        assert len(rows) == 1, "the denial audit was lost when the request rolled back"
        assert rows[0][0] == AuditAction.ACCESS_DENIED.value
        assert rows[0][1] == AuditOutcome.DENIED.value

    def test_the_denial_commit_does_not_commit_the_rejected_request(self, engine: Engine) -> None:
        """The separate session must not carry the rejected request's writes.

        If the denial shared the request transaction, committing it would also
        persist whatever the refused request had already written - the opposite
        failure, and a worse one.
        """
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        marker = f"integration-isolation-{uuid.uuid4().hex[:12]}"

        request_session = factory()
        try:
            # Work the rejected request performed before being refused.
            request_session.add(
                GeographyUnit(
                    level=GeographyLevel.COUNTRY,
                    preferred_code=marker[:32],
                    raw_name=marker,
                    normalised_name=marker.upper(),
                    depth=0,
                    path=marker[:32],
                )
            )
            request_session.flush()

            audit = AuditService(request_session, durable_session_factory=factory)
            audit.record_denial(principal=make_principal(role=SystemRole.ANALYST), reason=marker)
            raise PermissionError("simulated authorisation denial")
        except PermissionError:
            request_session.rollback()
        finally:
            request_session.close()

        with factory() as verify:
            leaked = verify.execute(
                text("SELECT count(*) FROM mars_core.geography_unit WHERE normalised_name = :name"),
                {"name": marker.upper()},
            ).scalar_one()
            audited = verify.execute(
                text("SELECT count(*) FROM mars_audit.audit_event WHERE reason = :marker"),
                {"marker": marker},
            ).scalar_one()

        assert leaked == 0, "the denial commit persisted work from the rejected request"
        assert audited == 1, "the denial itself should still have been recorded"


class TestGeographyConstraints:
    def _country(self, session: Session) -> GeographyUnit:
        unit = GeographyUnit(
            level=GeographyLevel.COUNTRY,
            preferred_code="UG",
            raw_name="Uganda",
            normalised_name="UGANDA",
            depth=0,
            path="UG",
        )
        session.add(unit)
        session.flush()
        return unit

    def test_only_the_country_may_have_no_parent(self, session: Session) -> None:
        orphan = GeographyUnit(
            level=GeographyLevel.DISTRICT,
            preferred_code="999",
            raw_name="ORPHAN",
            normalised_name="ORPHAN",
            depth=2,
        )
        session.add(orphan)
        with pytest.raises(IntegrityError, match="only_country_is_rootless"):
            session.flush()
        session.rollback()

    def test_a_unit_cannot_be_its_own_parent(self, session: Session) -> None:
        country = self._country(session)
        unit = GeographyUnit(
            level=GeographyLevel.REGION,
            preferred_code="3",
            raw_name="NORTHERN",
            normalised_name="NORTHERN",
            parent_id=country.id,
            depth=1,
            path="UG/3",
        )
        session.add(unit)
        session.flush()

        unit.parent_id = unit.id
        # Two independent guards reject this: the ck_..._no_self_parent CHECK
        # constraint and the reject_hierarchy_cycle trigger. The trigger fires
        # first, because a BEFORE ROW trigger runs ahead of constraint
        # evaluation, so the message names the trigger. Either is a pass; what
        # matters is that the write is refused.
        with pytest.raises(DatabaseError, match="own parent|no_self_parent"):
            session.flush()
        session.rollback()

    def test_both_self_parent_guards_are_installed(self, engine: Engine) -> None:
        """The CHECK constraint must survive alongside the trigger.

        The trigger masks the constraint at run time by firing first. Without
        this test, someone could remove the constraint and every behavioural
        test would still pass - until the trigger was ever dropped or disabled.
        """
        with engine.connect() as connection:
            constraints = set(
                connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'mars_core.geography_unit'::regclass "
                        "AND contype = 'c'"
                    )
                ).scalars()
            )
            triggers = set(
                connection.execute(
                    text(
                        "SELECT trigger_name FROM information_schema.triggers "
                        "WHERE event_object_schema = 'mars_core' "
                        "AND event_object_table = 'geography_unit'"
                    )
                ).scalars()
            )

        assert "ck_geography_unit_no_self_parent" in constraints, (
            "the no_self_parent CHECK constraint is missing; the trigger alone "
            "would leave no protection if it were ever disabled"
        )
        assert "geography_unit_reject_cycle" in triggers

    def test_a_multi_node_cycle_is_rejected(self, session: Session) -> None:
        country = self._country(session)
        region = GeographyUnit(
            level=GeographyLevel.REGION,
            preferred_code="3",
            raw_name="NORTHERN",
            normalised_name="NORTHERN",
            parent_id=country.id,
            depth=1,
            path="UG/3",
        )
        district = GeographyUnit(
            level=GeographyLevel.DISTRICT,
            preferred_code="304",
            raw_name="GULU",
            normalised_name="GULU",
            parent=region,
            depth=2,
            path="UG/3/304",
        )
        session.add_all([region, district])
        session.flush()

        region.parent_id = district.id
        with pytest.raises(DatabaseError, match="creates a cycle"):
            session.flush()
        session.rollback()

    def test_preferred_code_is_unique_within_a_level(self, session: Session) -> None:
        country = self._country(session)
        for name in ("FIRST", "SECOND"):
            session.add(
                GeographyUnit(
                    level=GeographyLevel.REGION,
                    preferred_code="3",
                    raw_name=name,
                    normalised_name=name,
                    parent_id=country.id,
                    depth=1,
                )
            )
        with pytest.raises(IntegrityError, match="uq_geography_unit_level_preferred_code"):
            session.flush()
        session.rollback()

    def test_the_same_name_may_repeat_under_different_parents(self, session: Session) -> None:
        """CENTRAL DIVISION occurs twelve times across different districts."""
        country = self._country(session)
        regions = []
        for code in ("1", "2"):
            region = GeographyUnit(
                level=GeographyLevel.REGION,
                preferred_code=code,
                raw_name=f"REGION {code}",
                normalised_name=f"REGION {code}",
                parent_id=country.id,
                depth=1,
                path=f"UG/{code}",
            )
            session.add(region)
            regions.append(region)
        session.flush()

        for index, region in enumerate(regions):
            session.add(
                GeographyUnit(
                    level=GeographyLevel.DISTRICT,
                    preferred_code=f"10{index}",
                    raw_name="CENTRAL DIVISION",
                    normalised_name="CENTRAL DIVISION",
                    parent_id=region.id,
                    depth=2,
                )
            )
        session.flush()  # must not raise
        session.rollback()

    def test_the_same_name_cannot_repeat_under_one_parent(self, session: Session) -> None:
        country = self._country(session)
        for code in ("1", "2"):
            session.add(
                GeographyUnit(
                    level=GeographyLevel.REGION,
                    preferred_code=code,
                    raw_name="DUPLICATE",
                    normalised_name="DUPLICATE",
                    parent_id=country.id,
                    depth=1,
                )
            )
        with pytest.raises(
            IntegrityError, match="uq_geography_unit_parent_id_level_normalised_name"
        ):
            session.flush()
        session.rollback()


class TestGovernanceLifecycle:
    def test_activating_a_configuration_retires_the_previous_one(self, session: Session) -> None:
        """Exactly one version of a key may be active at a time."""
        from datetime import date

        audit = AuditService(session)
        service = ConfigurationService(session, audit)
        principal = make_principal(role=SystemRole.ANALYST)

        service.create_key(
            key="test.parameter",
            label="Test parameter",
            description="Exercises the lifecycle. Not a surveillance threshold.",
            category="test",
        )

        first = service.draft_version(
            key="test.parameter",
            value={"example": 1},
            reason_for_change="initial",
            provenance="test fixture - not programme approved",
        )
        for target in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED):
            service.transition(
                version_id=first.id, target=target, principal=principal, reason="test"
            )
        service.transition(
            version_id=first.id,
            target=LifecycleStatus.ACTIVE,
            principal=principal,
            reason="test",
            effective_from=date(2026, 1, 1),
        )
        assert service.active_version("test.parameter").id == first.id

        second = service.draft_version(
            key="test.parameter",
            value={"example": 2},
            reason_for_change="revision",
            provenance="test fixture - not programme approved",
        )
        for target in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED):
            service.transition(
                version_id=second.id, target=target, principal=principal, reason="test"
            )
        service.transition(
            version_id=second.id,
            target=LifecycleStatus.ACTIVE,
            principal=principal,
            reason="test",
            effective_from=date(2026, 6, 1),
        )

        session.refresh(first)
        assert first.status is LifecycleStatus.RETIRED
        assert first.effective_to == date(2026, 6, 1)
        assert service.active_version("test.parameter").id == second.id
        session.rollback()

    def test_method_rollback_restores_the_previous_version(self, session: Session) -> None:
        audit = AuditService(session)
        service = MethodRegistryService(session, audit)
        principal = make_principal(role=SystemRole.ANALYST)

        service.register_method(
            code="TEST-METHOD",
            label="Test method",
            kind=MethodKind.DATA_QUALITY_RULE,
            purpose="Exercises the registry lifecycle. Not an approved method.",
        )
        v1 = service.draft_version(code="TEST-METHOD", semantic_version="1.0.0", summary="first")
        v2 = service.draft_version(code="TEST-METHOD", semantic_version="2.0.0", summary="second")
        for version in (v1, v2):
            for target in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED):
                service.promote(
                    version_id=version.id,
                    target=target,
                    principal=principal,
                    reason="test",
                )
        service.promote(
            version_id=v2.id,
            target=LifecycleStatus.ACTIVE,
            principal=principal,
            reason="test",
        )
        assert [v.id for v in service.active_versions()] == [v2.id]

        service.rollback(
            from_version_id=v2.id,
            to_version_id=v1.id,
            principal=principal,
            reason="regression found in 2.0.0",
        )
        session.refresh(v1)
        session.refresh(v2)
        assert v1.status is LifecycleStatus.ACTIVE
        assert v2.status is LifecycleStatus.RETIRED
        assert v1.rolled_back_from_id == v2.id
        assert v1.rollback_reason == "regression found in 2.0.0"
        session.rollback()


class TestPostGisAvailability:
    @pytest.mark.postgis
    def test_postgis_extension_is_installed(self, engine: Engine) -> None:
        """Geography import (Prompt 5) requires PostGIS.

        Reported as a failure here rather than skipped, because a database
        intended for MARS without PostGIS is a provisioning defect.
        """
        with engine.connect() as connection:
            version = connection.execute(text("SELECT PostGIS_Lib_Version()")).scalar_one()
        assert version

    def test_readiness_probe_reports_postgis_state(self, engine: Engine) -> None:
        """Whether or not PostGIS is present, the probe must say which."""
        from mars.db.session import check_database

        with engine.connect() as connection:
            info = check_database(connection)
        assert "postgis_available" in info
        assert info["server_version"]
