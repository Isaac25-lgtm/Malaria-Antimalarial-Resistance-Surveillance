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

    def test_identity_schema_exists_and_is_empty(self, engine: Engine) -> None:
        """The boundary is created before the data, never retrofitted."""
        with engine.connect() as connection:
            tables = list(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = :s"),
                    {"s": IDENTITY},
                ).scalars()
            )
        assert tables == [], f"{IDENTITY} should be empty in phases 1-2, found {tables}"

    def test_pgcrypto_is_available(self, engine: Engine) -> None:
        """gen_random_uuid() backs every primary key default."""
        with engine.connect() as connection:
            value = connection.execute(text("SELECT gen_random_uuid()")).scalar_one()
        assert uuid.UUID(str(value))


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
        with pytest.raises(IntegrityError, match="no_self_parent"):
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
