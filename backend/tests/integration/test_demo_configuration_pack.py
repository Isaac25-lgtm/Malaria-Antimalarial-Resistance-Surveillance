"""The demonstration configuration pack against live PostgreSQL.

What needs a real database:

* that every engine finds an active, usable version after the pack runs;
* that running it twice changes nothing;
* that a version somebody else approved is never replaced by the pack.

Requires ``MARS_TEST_DATABASE_URL``. Without it every test here skips.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.analytics.anomaly import AnomalyEngine
from mars.analytics.baseline import BaselineEngine
from mars.analytics.clustering import SpatialClusterEngine
from mars.analytics.episodes import EPISODE_RULE_CODE, EpisodeEngine
from mars.analytics.hotspot import HotspotEngine
from mars.analytics.indicator_registry import IndicatorRegistryService
from mars.analytics.recurrence import RecurrenceEngine
from mars.core.settings import Settings
from mars.demo.configuration_pack import (
    CONFIGURATIONS,
    DEMO_APPROVER,
    METHODS,
    DemoPackRefused,
    apply_pack,
    demo_principal,
)
from mars.domain.enums import LifecycleStatus, MethodKind
from mars.domain.governance import MethodVersion
from mars.services.audit_service import AuditService
from mars.services.governance_service import MethodRegistryService
from mars.services.spatial_availability import privacy_policy
from mars.signals.engine import SignalEngine

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]

DEMO_SETTINGS = Settings(
    database_url="postgresql+psycopg://mars:test@localhost:5432/mars_test",
    demo_mode_enabled=True,
)


@pytest.fixture(scope="module")
def pack_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def session(pack_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=pack_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
        db.rollback()


@pytest.fixture(autouse=True)
def clean(pack_engine: Engine) -> Iterator[None]:
    yield
    with pack_engine.begin() as connection:
        for table in (
            "mars_governance.configuration_version",
            "mars_governance.configuration_key",
            "mars_governance.method_version",
            "mars_governance.method_definition",
            "mars_governance.indicator_definition_version",
            "mars_governance.indicator_definition",
        ):
            connection.execute(text(f"DELETE FROM {table}"))


class TestEveryEngineFindsItsVersion:
    def test_after_the_pack_every_engine_is_configured(self, session: Session) -> None:
        report = apply_pack(session, DEMO_SETTINGS)
        session.commit()

        assert report.kept_existing == []
        for method in METHODS:
            assert f"method:{method.code}" in report.activated
        for configuration in CONFIGURATIONS:
            assert f"configuration:{configuration.key}" in report.activated

        rule = EpisodeEngine(session).active_rule()
        assert rule is not None
        assert BaselineEngine(session).specification()[0] is not None
        assert AnomalyEngine(session).rule()[0] is not None
        assert HotspotEngine(session).definition()[0] is not None
        assert SpatialClusterEngine(session).definition()[0] is not None
        method_id, rules, missing = SignalEngine(session).rules()
        assert method_id is not None, missing
        assert rules
        bands, _version = RecurrenceEngine(session).interval_bands()
        assert bands
        assert privacy_policy(session)[0] is not None

    def test_every_indicator_is_active_and_approved_by_the_pack(self, session: Session) -> None:
        apply_pack(session, DEMO_SETTINGS)
        session.commit()
        registry = IndicatorRegistryService(session)
        active = registry.active_versions()
        assert set(active) == {definition.code for definition in registry.list_definitions()}
        assert {version.approved_by for version in active.values()} == {DEMO_APPROVER}

    def test_every_method_version_names_the_pack_as_approver(self, session: Session) -> None:
        apply_pack(session, DEMO_SETTINGS)
        session.commit()
        approvers = set(
            session.execute(
                select(MethodVersion.approved_by).where(
                    MethodVersion.status == LifecycleStatus.ACTIVE
                )
            )
            .scalars()
            .all()
        )
        assert approvers == {DEMO_APPROVER}


class TestThePackIsSafeToRepeat:
    def test_a_second_run_activates_nothing(self, session: Session) -> None:
        apply_pack(session, DEMO_SETTINGS)
        session.commit()
        again = apply_pack(session, DEMO_SETTINGS)
        session.commit()
        assert again.activated == []
        assert again.kept_existing == []
        assert len(again.unchanged) == len(
            IndicatorRegistryService(session).list_definitions()
        ) + len(METHODS) + len(CONFIGURATIONS)


class TestARealApprovalAlwaysWins:
    def test_a_version_approved_by_someone_else_is_left_in_force(self, session: Session) -> None:
        registry = MethodRegistryService(session, AuditService(session))
        principal = demo_principal()
        registry.register_method(
            code=EPISODE_RULE_CODE,
            label="Malaria episode rule",
            kind=MethodKind.EPISODE_RULE,
            purpose="The programme's own rule.",
        )
        version = registry.draft_version(
            code=EPISODE_RULE_CODE,
            semantic_version="1.0.0",
            summary="Programme rule",
            parameters={"episode_window_days": 21},
        )
        for target in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE):
            registry.promote(
                version_id=version.id, target=target, principal=principal, reason="test"
            )
        version.approved_by = "programme:uganda-nmcd"
        session.commit()

        report = apply_pack(session, DEMO_SETTINGS)
        session.commit()

        assert f"method:{EPISODE_RULE_CODE}" in report.kept_existing
        rule = EpisodeEngine(session).active_rule()
        assert rule is not None
        assert rule.window_days == 21


class TestThePackRefusesOutsideADemo:
    def test_nothing_is_written_when_demo_mode_is_off(self, session: Session) -> None:
        settings = Settings(
            database_url="postgresql+psycopg://mars:test@localhost:5432/mars_test",
            demo_mode_enabled=False,
        )
        with pytest.raises(DemoPackRefused):
            apply_pack(session, settings)
        assert IndicatorRegistryService(session).list_definitions() == []
