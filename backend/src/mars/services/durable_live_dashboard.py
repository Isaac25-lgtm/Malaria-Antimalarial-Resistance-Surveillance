"""Browser-independent live jobs using durable checkpoints and session credentials."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from datetime import date
from threading import BoundedSemaphore, Event
from typing import Any

from mars.core.logging import get_logger
from mars.security.live_session import InMemoryCredentialHolder
from mars.services.live_dashboard import (
    LiveDashboardConfigurationError,
    LiveDashboardError,
    LiveDashboardService,
)
from mars.services.live_sync_store import LiveSyncStore

logger = get_logger(__name__)

#: Receives ``(scope_key, period_start, period_end, retained)`` once a job's
#: canonical evidence may be retained for recurrence analysis.
EvidenceSink = Callable[[str, date, date, Any], None]


class SyncCheckpoint:
    def __init__(
        self, store: LiveSyncStore, job: str, token: str, active: Callable[[], bool]
    ) -> None:
        self.store, self.job, self.token, self.active = store, job, token, active
        self.values = store.checkpoint(job, token)

    def check(self) -> None:
        if not self.active():
            raise LiveDashboardError("session_required")
        self.store.heartbeat(self.job, self.token)

    def get(self, key: str) -> Any:
        self.check()
        return self.values.get(key)

    def save(self, key: str, value: Any) -> None:
        self.check()
        self.values[key] = value
        self.store.checkpoint(self.job, self.token, self.values)


class LiveDashboardBusyError(LiveDashboardError):
    """The bounded local pilot worker has no safe queue capacity."""


class DurableLiveDashboardService(LiveDashboardService):
    def __init__(
        self,
        credentials: InMemoryCredentialHolder,
        runner: Callable[..., Any],
        store: LiveSyncStore,
        context: Callable[[str], Mapping[str, Any] | None],
        validate: Callable[[Mapping[str, Any]], dict[str, Any]],
        *,
        max_workers: int = 2,
        max_pending_jobs: int = 4,
        evidence_sink: EvidenceSink | None = None,
    ) -> None:
        super().__init__(credentials, runner)
        if max_workers < 1 or max_pending_jobs < max_workers:
            raise ValueError("max_pending_jobs must be at least max_workers")
        self._job_runner = runner
        self.store, self.context, self.validate = store, context, validate
        #: Retains canonical evidence for recurrence analysis. Optional: without
        #: it the runner is called exactly as before.
        self.evidence_sink = evidence_sink
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="mars-live-sync"
        )
        self._capacity = BoundedSemaphore(max_pending_jobs)
        self._closing = Event()

    def _scope(self, raw_id: str) -> tuple[str, Mapping[str, Any]]:
        context = self.context(raw_id)
        if not context or not self._credentials.has(raw_id):
            raise LiveDashboardError("Sign in and discover the authorised facility scope first")
        key = hashlib.sha256(
            json.dumps(
                {**context, "key_version": self.store.key_version}, sort_keys=True, default=str
            ).encode()
        ).hexdigest()
        return key, context

    def recurrence_scope(self, raw_id: str) -> tuple[str, list[dict[str, Any]]] | None:
        """The caller's *current* live scope key and facilities, or ``None``.

        A recurrence run over live evidence is bound to this key; when the
        session ends or the scope changes, the key changes and the run is no
        longer readable.
        """
        try:
            scope, context = self._scope(raw_id)
        except LiveDashboardError:
            return None
        return scope, [dict(item) for item in context.get("facilities", [])]

    def submit_job(self, raw_id: str, *, period_start: date, period_end: date) -> dict[str, Any]:
        if self._closing.is_set():
            raise LiveDashboardBusyError("The synchronization worker is shutting down")
        scope, context = self._scope(raw_id)
        facilities = context["facilities"]
        if not 1 <= (period_end - period_start).days + 1 <= 62:
            raise LiveDashboardError("A synchronization must cover 1 to 62 days")
        if not 1 <= len(facilities) <= 500:
            raise LiveDashboardError("Discover between 1 and 500 authorised facilities first")
        receipt, token = self.store.submit(scope, period_start, period_end, len(facilities) + 2)
        if token:
            if not self._capacity.acquire(blocking=False):
                self.store.finish(receipt["id"], token, None, "worker_busy")
                raise LiveDashboardBusyError(
                    "The synchronization worker is at capacity; retry shortly"
                )
            try:
                future = self.executor.submit(
                    self._execute,
                    raw_id,
                    scope,
                    receipt["id"],
                    token,
                    facilities,
                    period_start,
                    period_end,
                    str(context.get("scope_name") or "Authorised scope"),
                )
                future.add_done_callback(
                    lambda completed: self._job_done(completed, receipt["id"], token)
                )
            except RuntimeError:
                self._capacity.release()
                self.store.finish(receipt["id"], token, None, "worker_unavailable")
                raise LiveDashboardBusyError(
                    "The synchronization worker is shutting down"
                ) from None
        return receipt

    def _job_done(self, future: Future[Any], job_id: str, token: str) -> None:
        """Release bounded capacity and make cancelled queued work resumable."""
        try:
            if future.cancelled():
                with suppress(Exception):
                    self.store.finish(job_id, token, None, "session_required")
        finally:
            self._capacity.release()

    def _execute(
        self,
        raw_id: str,
        scope: str,
        job_id: str,
        token: str,
        facilities: Sequence[Mapping[str, Any]],
        start: date,
        end: date,
        scope_name: str = "Authorised scope",
    ) -> None:
        def active() -> bool:
            if self._closing.is_set():
                return False
            try:
                return self._scope(raw_id)[0] == scope
            except LiveDashboardError:
                return False

        retained: list[Any] = []
        extra: dict[str, Any] = {"scope_name": scope_name}
        if self.evidence_sink:
            extra["evidence_sink"] = retained.append
        try:
            checkpoint = SyncCheckpoint(self.store, job_id, token, active)
            checkpoint.check()
            result = self._credentials.invoke(
                raw_id,
                lambda username, password: self._job_runner(
                    username, password, facilities, start, end, checkpoint=checkpoint, **extra
                ),
            )
            checkpoint.check()
            if result.get("synthetic_data_used") is not False:
                raise LiveDashboardError("invalid_source_result")
            if str(result.get("period_start")) != str(start) or str(
                result.get("period_end")
            ) != str(end):
                raise LiveDashboardError("invalid_source_period")
            snapshot = self.validate(result)
            if self.evidence_sink is not None and retained:
                # Retained only after the lease and session were re-checked, so
                # a fenced or signed-out worker cannot keep evidence for a scope
                # it no longer owns. A retention failure never blocks the
                # snapshot; a run over live evidence then says it is missing.
                try:
                    self.evidence_sink(scope, start, end, retained[-1])
                except Exception as error:
                    logger.warning("live_evidence_not_retained", error=type(error).__name__)
            self.store.finish(job_id, token, snapshot)
        except Exception as error:
            code = (
                "local_configuration"
                if isinstance(error, LiveDashboardConfigurationError)
                else "session_required"
                if not active()
                else "synchronization_failed"
            )
            # Persist only a controlled code; driver/upstream errors can contain secrets.
            with suppress(Exception):
                self.store.finish(job_id, token, None, code)

    def latest_job(
        self, raw_id: str, *, period_start: date | None = None, period_end: date | None = None
    ) -> dict[str, Any] | None:
        try:
            scope, _ = self._scope(raw_id)
        except LiveDashboardError:
            return None
        return self.store.read_job(scope, period_start, period_end)

    def latest(
        self,
        raw_session_id: str,
        *,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> dict[str, Any] | None:
        try:
            scope, _ = self._scope(raw_session_id)
        except LiveDashboardError:
            return None
        result = self.store.latest(scope, period_start, period_end)
        if result:
            result["latest_attempt"] = self.store.read_job(scope, period_start, period_end)
        return result

    def synchronize(
        self,
        raw_session_id: str,
        *,
        facilities: Sequence[Mapping[str, Any]],
        period_start: date,
        period_end: date,
    ) -> dict[str, Any]:
        """Compatibility endpoint; the UI uses submit_job and polls instead."""
        self.submit_job(raw_session_id, period_start=period_start, period_end=period_end)
        while True:
            job = self.latest_job(raw_session_id, period_start=period_start, period_end=period_end)
            if not job or job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.25)
        result = self.latest(raw_session_id, period_start=period_start, period_end=period_end)
        if result is None:
            raise LiveDashboardError("Synchronization did not produce an available snapshot")
        return result

    def close(self) -> None:
        self._closing.set()
        self.executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "DurableLiveDashboardService",
    "EvidenceSink",
    "LiveDashboardBusyError",
    "SyncCheckpoint",
]
