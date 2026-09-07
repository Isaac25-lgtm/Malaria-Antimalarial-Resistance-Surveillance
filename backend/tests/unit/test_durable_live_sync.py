"""Persistence, publication, scope isolation, recovery and credential lifetime."""

from datetime import UTC, date, datetime, timedelta
from threading import Event

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mars.domain.live_sync import LiveSyncJob
from mars.security.live_session import InMemoryCredentialHolder
from mars.services.durable_live_dashboard import (
    DurableLiveDashboardService,
    LiveDashboardBusyError,
)
from mars.services.live_sync_store import LiveSyncStore

START, END = date(2026, 8, 1), date(2026, 8, 31)


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"mars_analytics": None}},
    )
    LiveSyncJob.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield LiveSyncStore(factory, "test-encryption-key"), factory
    engine.dispose()


def snapshot(status="synchronized", value="17"):
    return {
        "status": status,
        "synthetic_data_used": False,
        "period_start": START.isoformat(),
        "period_end": END.isoformat(),
        "value": value,
    }


def test_snapshot_survives_service_recreation_and_is_encrypted(store):
    repository, factory = store
    job, token = repository.submit("scope-a", START, END, 3)
    repository.finish(job["id"], token, snapshot())
    restored = LiveSyncStore(factory, "test-encryption-key")
    assert restored.latest("scope-a", START, END)["value"] == "17"
    assert restored.latest("scope-b", START, END) is None
    assert restored.latest("scope-a", date(2026, 7, 1), date(2026, 7, 31)) is None
    with factory() as db:
        ciphertext = db.get(LiveSyncJob, job["id"]).snapshot
        assert b"synthetic_data_used" not in ciphertext
    with pytest.raises(InvalidTag):
        LiveSyncStore(factory, "different-key").latest("scope-a", START, END)


def test_failed_and_partial_refresh_preserve_complete_snapshot(store):
    repository, _ = store
    first, token = repository.submit("scope", START, END, 3)
    repository.finish(first["id"], token, snapshot(value="100"))
    second, token = repository.submit("scope", START, END, 3)
    repository.finish(second["id"], token, snapshot("partial", "25"))
    assert repository.latest("scope", START, END)["value"] == "100"
    repository.finish(second["id"], "wrong-token", None, "synchronization_failed")
    assert repository.read_job("scope", START, END)["status"] == "partial"
    retried, token = repository.submit("scope", START, END, 3)
    repository.finish(retried["id"], token, None, "synchronization_failed")
    assert repository.latest("scope", START, END)["value"] == "100"
    assert repository.read_job("scope", START, END)["status"] == "failed"


def test_duplicate_submission_and_expired_worker_fencing(store):
    repository, factory = store
    first, token = repository.submit("scope", START, END, 3)
    duplicate, duplicate_token = repository.submit("scope", START, END, 3)
    assert duplicate["id"] == first["id"]
    assert duplicate_token is None
    repository.checkpoint(first["id"], token, {"aggregate": [{"value": "12"}]})
    with factory() as db, db.begin():
        db.get(LiveSyncJob, first["id"]).updated_at = datetime.now(UTC) - timedelta(minutes=6)
    assert repository.read_job("scope", START, END)["status"] == "interrupted"
    resumed, fresh_token = repository.submit("scope", START, END, 3)
    assert resumed["id"] != first["id"]
    assert fresh_token != token
    assert repository.checkpoint(resumed["id"], fresh_token)["aggregate"] == [{"value": "12"}]
    with pytest.raises(RuntimeError, match="lease_lost"):
        repository.checkpoint(first["id"], token, {})
    repository.finish(first["id"], token, snapshot())
    assert repository.latest("scope", START, END) is None


def test_complete_empty_checkpoint_is_reusable_and_ciphertext_is_bound_to_job(store):
    repository, factory = store
    first, token = repository.submit("scope-a", START, END, 3)
    repository.checkpoint(first["id"], token, {"tracker:f1": []})
    repository.finish(first["id"], token, None, "session_required")
    resumed, token = repository.submit("scope-a", START, END, 3)
    assert repository.checkpoint(resumed["id"], token) == {"tracker:f1": []}
    second, second_token = repository.submit("scope-b", START, END, 3)
    with factory() as db, db.begin():
        db.get(LiveSyncJob, second["id"]).checkpoint = db.get(LiveSyncJob, first["id"]).checkpoint
    with pytest.raises(InvalidTag):
        repository.checkpoint(second["id"], second_token)


def test_job_outlives_submission_but_logout_prevents_publication(store):
    repository, _ = store
    holder = InMemoryCredentialHolder()
    holder.store("cookie", "officer", "never-persist-this")
    started, release, finished = Event(), Event(), Event()

    def runner(*args, checkpoint):
        checkpoint.save("aggregate", [])
        started.set()
        assert release.wait(5)
        finished.set()
        return snapshot()

    service = DurableLiveDashboardService(
        holder,
        runner,
        repository,
        lambda raw: {"subject": "officer", "facilities": [{"id": "f1"}]}
        if raw == "cookie"
        else None,
        dict,
    )
    receipt = service.submit_job("cookie", period_start=START, period_end=END)
    assert receipt["status"] == "queued"
    assert started.wait(5)
    holder.drop("cookie")
    release.set()
    service.executor.shutdown(wait=True)
    assert finished.is_set()
    assert service.latest("cookie", period_start=START, period_end=END) is None
    holder.store("cookie", "officer", "replacement")
    assert (
        service.latest_job("cookie", period_start=START, period_end=END)["status"] == "interrupted"
    )


def test_worker_capacity_is_bounded_and_rejected_work_is_resumable(store):
    repository, _ = store
    holder = InMemoryCredentialHolder()
    holder.store("cookie", "officer", "never-persist-this")
    started, release = Event(), Event()

    def runner(*args, checkpoint):
        started.set()
        assert release.wait(5)
        return snapshot()

    service = DurableLiveDashboardService(
        holder,
        runner,
        repository,
        lambda raw: {"subject": "officer", "facilities": [{"id": "f1"}]},
        dict,
        max_workers=1,
        max_pending_jobs=1,
    )
    service.submit_job("cookie", period_start=START, period_end=END)
    assert started.wait(5)
    september = (date(2026, 9, 1), date(2026, 9, 30))
    with pytest.raises(LiveDashboardBusyError, match="at capacity"):
        service.submit_job("cookie", period_start=september[0], period_end=september[1])
    rejected = service.latest_job("cookie", period_start=september[0], period_end=september[1])
    assert rejected["status"] == "failed"
    assert rejected["error_code"] == "worker_busy"
    release.set()
    service.executor.shutdown(wait=True)


def test_adapter_resumes_only_failed_facilities_with_durable_checkpoints(store, monkeypatch):
    from pathlib import Path

    from mars.core.settings import Settings
    from mars.integrations.dhis2 import live_dashboard as adapter
    from mars.integrations.ports import RemoteDataValue, RemoteEvent, RemotePage
    from mars.services.durable_live_dashboard import SyncCheckpoint
    from tests.unit.test_live_dashboard import _mapping

    repository, _ = store
    mapping = _mapping()
    mapping.update({"programme_uid": "program", "datasets": {"monthly_105_opd": "dataset"}})
    mapping["tracker"]["stages"] = {"laboratory_tests": "lab"}
    monkeypatch.setattr(adapter, "_load_mapping", lambda path: mapping)
    calls = {"aggregate": 0, "f1": 0, "f2": 0}
    fail = True

    class AggregateClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch_data_values(self, scope, cursor):
            calls["aggregate"] += 1
            return RemotePage(records=(RemoteDataValue("confirmed", "f1", "202608", "1"),))

    class TrackerClient(AggregateClient):
        def fetch_events(self, scope, cursor):
            facility = scope.organisation_unit_remote_ids[0]
            calls[facility] += 1
            if facility == "f2" and fail:
                raise RuntimeError("simulated unavailable facility")
            return RemotePage(
                records=(
                    RemoteEvent(
                        remote_id=f"event-{facility}",
                        person_remote_id="source-person-reference",
                        programme_remote_id="program",
                        programme_stage_remote_id="lab",
                        organisation_unit_remote_id=facility,
                        occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
                        updated_at=None,
                        status="COMPLETED",
                        data_values={"test_type": "Malaria Test RDT", "result": "Negative"},
                    ),
                )
            )

    monkeypatch.setattr(adapter, "Dhis2Client", AggregateClient)
    monkeypatch.setattr(adapter, "BoundedTrackerEventClient", TrackerClient)
    runner = adapter.build_live_dashboard_runner(
        Settings(
            database_url="postgresql+psycopg://local/db",
            patient_display_key="stable-test-key",
        ),
        project_root=Path("unused"),
    )
    facilities = [{"id": "f1", "name": "Facility One"}, {"id": "f2", "name": "Facility Two"}]
    job, token = repository.submit("scope", START, END, 4)
    first = runner(
        "user",
        "credential",
        facilities,
        START,
        END,
        checkpoint=SyncCheckpoint(repository, job["id"], token, lambda: True),
    )
    assert first["retrieval_complete"] is False
    assert first["tracker_retrieved_facility_count"] == 1
    repository.finish(job["id"], token, first)
    fail = False
    resumed, token = repository.submit("scope", START, END, 4)
    second = runner(
        "user",
        "credential",
        facilities,
        START,
        END,
        checkpoint=SyncCheckpoint(repository, resumed["id"], token, lambda: True),
    )
    assert second["retrieval_complete"] is True
    assert second["tracker_retrieved_facility_count"] == 2
    assert second["tracker_event_count"] == 2
    assert calls == {"aggregate": 2, "f1": 1, "f2": 2}
    assert "source-person-reference" not in str(second)


def test_tracker_retries_transient_failures_but_not_denials(monkeypatch):
    from mars.domain.enums import IntegrationErrorCategory
    from mars.integrations.dhis2 import live_dashboard as adapter
    from mars.integrations.dhis2.client import Dhis2Error

    attempts = []
    monkeypatch.setattr(adapter.time, "sleep", lambda seconds: None)

    def transient():
        attempts.append(1)
        if len(attempts) < 3:
            raise Dhis2Error(IntegrationErrorCategory.TIMEOUT, "timeout")
        return "page"

    assert adapter._retry_tracker_read(transient, None) == "page"
    assert len(attempts) == 3
    attempts.clear()

    def denied():
        attempts.append(1)
        raise Dhis2Error(IntegrationErrorCategory.AUTHORISATION, "denied")

    with pytest.raises(Dhis2Error):
        adapter._retry_tracker_read(denied, None)
    assert len(attempts) == 1
