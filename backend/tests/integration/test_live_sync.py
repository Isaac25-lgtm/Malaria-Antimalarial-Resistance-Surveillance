"""Durable live synchronization guarantees against disposable PostgreSQL."""

from __future__ import annotations

import runpy
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from mars.domain.live_sync import LiveSyncJob
from mars.services.live_sync_store import LiveSyncStore

pytestmark = pytest.mark.integration

MIGRATIONS_ROOT = Path(__file__).resolve().parents[2]
PROVISION_SQL = MIGRATIONS_ROOT.parent / "scripts" / "provision_identity_roles.sql"
START, END = date(2026, 8, 1), date(2026, 8, 31)


@pytest.fixture(scope="module")
def sync_engine(integration_database_url: str) -> Iterator[Engine]:
    engine = create_engine(integration_database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            text(PROVISION_SQL.read_text(encoding="utf-8").replace("\\set ON_ERROR_STOP on", ""))
        )
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(config, "0026_reapply_runtime_role_grants")
    assert not inspect(engine).has_table("live_sync_job", schema="mars_analytics")
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture
def factory(sync_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(sync_engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def clean_jobs(sync_engine: Engine) -> Iterator[None]:
    yield
    with sync_engine.begin() as connection:
        connection.execute(text("DELETE FROM mars_analytics.live_sync_job"))


def _snapshot(status: str = "synchronized", value: int = 17) -> dict[str, object]:
    return {
        "status": status,
        "synthetic_data_used": False,
        "period_start": START.isoformat(),
        "period_end": END.isoformat(),
        "value": value,
    }


def test_migration_and_runtime_role_boundaries(sync_engine: Engine) -> None:
    with sync_engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        app_can_read = connection.scalar(
            text("SELECT has_table_privilege('mars_app', 'mars_analytics.live_sync_job', 'SELECT')")
        )
        identity_can_read = connection.scalar(
            text(
                "SELECT has_table_privilege('mars_identity_service', "
                "'mars_analytics.live_sync_job', 'SELECT')"
            )
        )
    assert revision == "0027_live_sync"
    assert app_can_read is True
    assert identity_can_read is False


def test_startup_probe_under_restricted_roles(sync_engine: Engine) -> None:
    check = runpy.run_path(str(MIGRATIONS_ROOT.parent / "scripts/check-live-runtime-databases.py"))[
        "_check_live_sync_privileges"
    ]
    for role, label in (("mars_app", "application"), ("mars_identity_service", "identity")):
        with sync_engine.connect() as connection, connection.begin():
            connection.execute(text(f"SET LOCAL ROLE {role}"))
            if label == "identity":
                assert (
                    connection.scalar(
                        text("SELECT has_schema_privilege(current_user, 'mars_analytics', 'USAGE')")
                    )
                    is False
                )
            check(connection, label)


def test_snapshot_persists_encrypted_across_sessions(
    factory: sessionmaker[Session],
) -> None:
    scope = f"scope-{uuid.uuid4()}"
    store = LiveSyncStore(factory, "integration-encryption-key")
    receipt, token = store.submit(scope, START, END, 3)
    assert token is not None
    store.finish(receipt["id"], token, _snapshot())
    restored = LiveSyncStore(factory, "integration-encryption-key")
    assert restored.latest(scope, START, END)["value"] == 17
    with factory() as session:
        ciphertext = session.get(LiveSyncJob, receipt["id"]).snapshot
    assert b"synthetic_data_used" not in ciphertext


def test_concurrent_duplicate_submission_has_one_active_job(
    factory: sessionmaker[Session],
) -> None:
    scope = f"scope-{uuid.uuid4()}"
    store = LiveSyncStore(factory, "integration-encryption-key")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.submit(scope, START, END, 3), range(2)))
    assert len({result[0]["id"] for result in results}) == 1
    assert sum(result[1] is not None for result in results) == 1
    with factory() as session:
        jobs = session.scalars(select(LiveSyncJob).where(LiveSyncJob.scope_key == scope)).all()
    assert len(jobs) == 1


def test_stale_worker_is_fenced_and_complete_snapshot_survives_partial_refresh(
    factory: sessionmaker[Session],
) -> None:
    scope = f"scope-{uuid.uuid4()}"
    store = LiveSyncStore(factory, "integration-encryption-key")
    complete, complete_token = store.submit(scope, START, END, 3)
    store.finish(complete["id"], complete_token, _snapshot(value=100))
    partial, partial_token = store.submit(scope, START, END, 3)
    store.finish(partial["id"], partial_token, _snapshot("partial", 25))
    assert store.latest(scope, START, END)["value"] == 100

    stale, stale_token = store.submit(scope, START, END, 3)
    store.checkpoint(stale["id"], stale_token, {"aggregate": []})
    with factory() as session, session.begin():
        session.get(LiveSyncJob, stale["id"]).updated_at = datetime.now(UTC) - timedelta(minutes=6)
    resumed, resumed_token = store.submit(scope, START, END, 3)
    assert resumed["id"] != stale["id"]
    store.finish(stale["id"], stale_token, _snapshot(value=999))
    assert store.latest(scope, START, END)["value"] == 100
    assert store.checkpoint(resumed["id"], resumed_token) == {"aggregate": []}


def test_rolled_back_job_is_never_visible(factory: sessionmaker[Session]) -> None:
    job_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="rollback"), factory() as session, session.begin():
        session.add(
            LiveSyncJob(
                id=job_id,
                scope_key=f"scope-{uuid.uuid4()}",
                period_start=START,
                period_end=END,
                job_status="queued",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                completed_steps=0,
                total_steps=3,
            )
        )
        raise RuntimeError("rollback")
    with factory() as session:
        assert session.get(LiveSyncJob, job_id) is None
