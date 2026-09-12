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


def test_canonical_evidence_is_retained_only_after_the_session_is_rechecked(store):
    repository, _ = store
    holder = InMemoryCredentialHolder()
    holder.store("cookie", "officer", "never-persist-this")
    retained: list[tuple[str, date, date, object]] = []
    canonical = ({"encounters": "server-only"}, {"coverage": "complete"})

    def runner(*args, checkpoint, evidence_sink):
        checkpoint.check()
        evidence_sink(canonical)
        return snapshot()

    service = DurableLiveDashboardService(
        holder,
        runner,
        repository,
        lambda raw: {"subject": "officer", "facilities": [{"id": "f1"}]},
        dict,
        evidence_sink=lambda scope, start, end, payload: retained.append(
            (scope, start, end, payload)
        ),
    )
    service.submit_job("cookie", period_start=START, period_end=END)
    service.executor.shutdown(wait=True)

    assert len(retained) == 1
    assert retained[0][1:] == (START, END, canonical)
    assert "server-only" not in str(repository.latest(retained[0][0], START, END))


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


def _stall(factory, job_id, minutes=6):
    """Age one attempt past its lease without sleeping."""
    with factory() as db, db.begin():
        db.get(LiveSyncJob, job_id).updated_at = datetime.now(UTC) - timedelta(minutes=minutes)


@pytest.mark.parametrize("status", ["synchronized", "partial"])
def test_new_refresh_does_not_reuse_checkpoints_from_a_finished_retrieval(store, status):
    repository, _ = store
    failed, token = repository.submit("scope", START, END, 3)
    repository.checkpoint(failed["id"], token, {"aggregate": ["old"], "tracker:f1": []})
    repository.finish(failed["id"], token, None, "synchronization_failed")

    recovered, token = repository.submit("scope", START, END, 3)
    assert repository.checkpoint(recovered["id"], token)["aggregate"] == ["old"]
    result = snapshot(status, "100")
    result["retrieval_complete"] = True
    repository.finish(recovered["id"], token, result)

    fresh, token = repository.submit("scope", START, END, 3)
    assert fresh["completed_steps"] == 0
    assert repository.checkpoint(fresh["id"], token) == {}
    repository.checkpoint(fresh["id"], token, {"aggregate": ["new"]})
    repository.finish(fresh["id"], token, None, "synchronization_failed")
    retried, token = repository.submit("scope", START, END, 3)
    assert repository.checkpoint(retried["id"], token) == {"aggregate": ["new"]}


def test_stalled_attempt_is_found_behind_a_newer_published_snapshot(store):
    """The ownership defect, at the level the integration suite caught it.

    Replacement used to select a single job ordered by ``updated_at``. A
    published snapshot finished *after* an attempt stalled therefore sorted
    ahead of it, so the stalled worker was never seen, never revoked, and its
    late result overwrote a good snapshot.
    """
    repository, factory = store
    complete, complete_token = repository.submit("scope", START, END, 3)
    repository.finish(complete["id"], complete_token, snapshot(value="100"))
    partial, partial_token = repository.submit("scope", START, END, 3)
    repository.finish(partial["id"], partial_token, snapshot("partial", "25"))
    assert repository.latest("scope", START, END)["value"] == "100"

    stale, stale_token = repository.submit("scope", START, END, 3)
    repository.checkpoint(stale["id"], stale_token, {"aggregate": []})
    _stall(factory, stale["id"])

    resumed, resumed_token = repository.submit("scope", START, END, 3)
    assert resumed["id"] != stale["id"]

    # The stalled worker finishing late must change nothing.
    repository.finish(stale["id"], stale_token, snapshot(value="999"))
    assert repository.latest("scope", START, END)["value"] == "100"
    # ...and its progress must still reach the replacement.
    assert repository.checkpoint(resumed["id"], resumed_token) == {"aggregate": []}


def test_superseded_worker_is_rejected_at_heartbeat_checkpoint_and_finish(store):
    repository, factory = store
    stale, stale_token = repository.submit("scope", START, END, 3)
    _stall(factory, stale["id"])
    repository.submit("scope", START, END, 3)

    with pytest.raises(RuntimeError, match="lease_lost"):
        repository.heartbeat(stale["id"], stale_token)
    with pytest.raises(RuntimeError, match="lease_lost"):
        repository.checkpoint(stale["id"], stale_token, {"aggregate": []})
    repository.finish(stale["id"], stale_token, snapshot(value="999"))
    assert repository.latest("scope", START, END) is None


def test_checkpoint_recovery_prefers_the_furthest_progressed_attempt(store):
    """Two stalled attempts: resume from the one that got further."""
    repository, factory = store
    behind, behind_token = repository.submit("scope", START, END, 3)
    repository.checkpoint(behind["id"], behind_token, {"a": []})
    _stall(factory, behind["id"], minutes=9)

    # A second attempt exists concurrently and gets further before stalling.
    ahead, ahead_token = repository.submit("scope", START, END, 3)
    repository.checkpoint(ahead["id"], ahead_token, {"a": [], "b": [], "c": []})
    _stall(factory, ahead["id"], minutes=7)

    resumed, resumed_token = repository.submit("scope", START, END, 3)
    assert repository.checkpoint(resumed["id"], resumed_token) == {"a": [], "b": [], "c": []}


def test_terminal_job_cannot_be_reactivated(store):
    repository, _ = store
    job, token = repository.submit("scope", START, END, 3)
    repository.finish(job["id"], token, snapshot(value="100"))
    # finish() clears the lease, so the settled job answers to nobody.
    with pytest.raises(RuntimeError, match="lease_lost"):
        repository.heartbeat(job["id"], token)
    with pytest.raises(RuntimeError, match="lease_lost"):
        repository.checkpoint(job["id"], token, {"aggregate": []})
    repository.finish(job["id"], token, snapshot(value="999"))
    assert repository.latest("scope", START, END)["value"] == "100"


def test_replacement_and_completion_race_resolves_the_same_way_in_both_orders(store):
    """Deterministic instead of threaded: both interleavings, stated explicitly."""
    repository, factory = store

    # Completion first, then replacement: the finished result stands and the
    # replacement starts clean rather than adopting a settled attempt.
    first, first_token = repository.submit("scope-a", START, END, 3)
    repository.finish(first["id"], first_token, snapshot(value="100"))
    successor, _ = repository.submit("scope-a", START, END, 3)
    assert successor["id"] != first["id"]
    assert repository.latest("scope-a", START, END)["value"] == "100"

    # Replacement first, then completion: the superseded worker writes nothing.
    second, second_token = repository.submit("scope-b", START, END, 3)
    _stall(factory, second["id"])
    replacement, replacement_token = repository.submit("scope-b", START, END, 3)
    repository.finish(second["id"], second_token, snapshot(value="999"))
    assert repository.latest("scope-b", START, END) is None
    repository.finish(replacement["id"], replacement_token, snapshot(value="42"))
    assert repository.latest("scope-b", START, END)["value"] == "42"


def test_replacement_does_not_reach_across_scope_or_period(store):
    """A stalled attempt elsewhere is not this window's business."""
    repository, factory = store
    other_period_end = date(2026, 9, 30)
    foreign, foreign_token = repository.submit("scope-a", START, other_period_end, 3)
    repository.checkpoint(foreign["id"], foreign_token, {"foreign": []})
    _stall(factory, foreign["id"])
    other_scope, other_token = repository.submit("scope-b", START, END, 3)
    repository.checkpoint(other_scope["id"], other_token, {"other": []})
    _stall(factory, other_scope["id"])

    fresh, fresh_token = repository.submit("scope-a", START, END, 3)
    # Neither foreign checkpoint may be adopted.
    assert repository.checkpoint(fresh["id"], fresh_token) == {}
    # And neither foreign attempt is revoked by this window's replacement:
    # each stays its own window's business until that window is resubmitted.
    assert repository.checkpoint(foreign["id"], foreign_token) == {"foreign": []}
    assert repository.checkpoint(other_scope["id"], other_token) == {"other": []}


def test_checkpoint_lineage_does_not_depend_on_clock_resolution(store, monkeypatch):
    """Attempt order must be strict even when submissions share a timestamp.

    Under a frozen clock every attempt used to get an identical created_at, so
    ordering fell through to a random UUID and a fresh refresh recovered a
    checkpoint from before a completed retrieval about five times in six.
    """
    from datetime import tzinfo

    import mars.services.live_sync_store as module

    fixed = datetime(2026, 9, 1, 12, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return fixed

    monkeypatch.setattr(module, "datetime", Frozen)
    repository, _ = store
    for attempt in range(20):
        scope = f"frozen-{attempt}"
        failed, token = repository.submit(scope, START, END, 3)
        repository.checkpoint(failed["id"], token, {"aggregate": ["old"]})
        repository.finish(failed["id"], token, None, "synchronization_failed")
        recovered, token = repository.submit(scope, START, END, 3)
        assert repository.checkpoint(recovered["id"], token) == {"aggregate": ["old"]}
        result = snapshot("synchronized", "100")
        result["retrieval_complete"] = True
        repository.finish(recovered["id"], token, result)
        fresh, token = repository.submit(scope, START, END, 3)
        assert repository.checkpoint(fresh["id"], token) == {}
        repository.checkpoint(fresh["id"], token, {"aggregate": ["new"]})
        repository.finish(fresh["id"], token, None, "synchronization_failed")
        retried, token = repository.submit(scope, START, END, 3)
        assert repository.checkpoint(retried["id"], token) == {"aggregate": ["new"]}


def test_an_incomplete_plan_keeps_its_checkpoint_until_the_plan_completes(store):
    """Audit defect E: a failed context stage must not discard resumable work.

    A snapshot can be published while the three-stage retrieval plan is still
    incomplete - laboratory evidence complete, a medicine stage failed. The
    published snapshot is the last good result, but the plan is not settled,
    so its checkpoint survives and the next attempt resumes from it. Only a
    completed plan clears the checkpoint.
    """
    repository, _ = store
    progress = {"aggregate": ["kept"], "tracker-plan/3:f1": {"laboratory": []}}
    first, token = repository.submit("scope", START, END, 4)
    repository.checkpoint(first["id"], token, progress)
    partial = snapshot("partial", "25")
    partial["retrieval_complete"] = False
    repository.finish(first["id"], token, partial)
    assert repository.latest("scope", START, END)["value"] == "25"
    assert repository.read_job("scope", START, END)["status"] == "partial"

    retry, token = repository.submit("scope", START, END, 4)
    assert repository.checkpoint(retry["id"], token) == progress
    settled = snapshot("partial", "30")
    settled["retrieval_complete"] = True
    repository.finish(retry["id"], token, settled)

    fresh, token = repository.submit("scope", START, END, 4)
    assert fresh["completed_steps"] == 0
    assert repository.checkpoint(fresh["id"], token) == {}


def test_a_failed_attempt_after_an_incomplete_plan_still_resumes_it(store):
    repository, _ = store
    first, token = repository.submit("scope", START, END, 4)
    repository.checkpoint(first["id"], token, {"aggregate": ["kept"]})
    partial = snapshot("partial", "25")
    partial["retrieval_complete"] = False
    repository.finish(first["id"], token, partial)
    second, token = repository.submit("scope", START, END, 4)
    repository.finish(second["id"], token, None, "synchronization_failed")
    third, token = repository.submit("scope", START, END, 4)
    assert repository.checkpoint(third["id"], token) == {"aggregate": ["kept"]}
    # The last good snapshot is still the published one.
    assert repository.latest("scope", START, END)["value"] == "25"
