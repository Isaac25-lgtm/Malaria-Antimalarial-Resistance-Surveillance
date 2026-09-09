"""Short database transactions, fenced job leases, and authenticated encryption.

Credentials and raw session identifiers never enter this store. A scope key
binds data to the source, authenticated subject, permissions, exact discovered
facility set, mapping version and encryption key version.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, load_only

from mars.domain.live_sync import LiveSyncJob

LEASE_SECONDS = 300

#: The states in which a worker still holds authority over a job. Anything else
#: is a result, not a competitor. Keeping the distinction explicit is the whole
#: point: a published snapshot that sorted ahead of a stalled attempt used to
#: hide it, leaving the stalled worker's lease valid to overwrite a good
#: snapshot minutes later.
ACTIVE_STATUSES = ("queued", "running")


class LiveSyncStore:
    def __init__(self, sessions: Callable[[], Session], secret: str) -> None:
        self._sessions = sessions
        self._cipher = AESGCM(hashlib.sha256(b"mars-live-snapshot-v1:" + secret.encode()).digest())
        self.key_version = hashlib.sha256(secret.encode()).hexdigest()

    def _seal(self, value: dict[str, Any], aad: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(
            nonce, json.dumps(value, default=str).encode(), aad.encode()
        )

    def _open(self, value: bytes | None, aad: str) -> dict[str, Any]:
        if value is None:
            return {}
        result: dict[str, Any] = json.loads(
            self._cipher.decrypt(value[:12], value[12:], aad.encode())
        )
        return result

    @staticmethod
    def _stale(job: LiveSyncJob) -> bool:
        moment = (
            job.updated_at.replace(tzinfo=UTC) if job.updated_at.tzinfo is None else job.updated_at
        )
        return moment < datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)

    def _resumable_checkpoint(self, attempts: list[LiveSyncJob], scope: str) -> dict[str, Any]:
        """The furthest-progressed checkpoint among previous attempts.

        Restricted to attempts the caller already filtered to one scope and
        reporting window, so a checkpoint can never cross either boundary. A
        completed attempt holds none - ``finish`` clears it - so success cannot
        pollute recovery, while an interrupted or failed attempt keeps whatever
        it managed.

        The most progressed attempt wins rather than the most recent: resuming
        from further along is strictly better, and a worker that stalled after
        more work should not lose it to one that stalled later having done less.
        """
        best: LiveSyncJob | None = None
        for attempt in attempts:
            if attempt.checkpoint is None:
                continue
            if best is None or (attempt.completed_steps, attempt.updated_at) > (
                best.completed_steps,
                best.updated_at,
            ):
                best = attempt
        if best is None:
            return {}
        return self._open(best.checkpoint, f"{scope}:{best.id}:checkpoint")

    @staticmethod
    def public(job: LiveSyncJob) -> dict[str, Any]:
        status = job.job_status
        if status in {"queued", "running"} and LiveSyncStore._stale(job):
            status = "interrupted"
        return {
            "id": job.id,
            "status": status,
            "period_start": job.period_start.isoformat(),
            "period_end": job.period_end.isoformat(),
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "completed_steps": job.completed_steps,
            "total_steps": job.total_steps,
            "error_code": "session_required" if status == "interrupted" else job.error_code,
        }

    def submit(
        self, scope: str, start: date, end: date, steps: int
    ) -> tuple[dict[str, Any], str | None]:
        with self._sessions() as db, db.begin():
            # Serialises competing submissions across API processes without a
            # network operation or long transaction under this lock.
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                lock_key = int.from_bytes(
                    hashlib.sha256(f"{scope}:{start}:{end}".encode()).digest()[:8],
                    "big",
                    signed=True,
                )
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            # Every attempt for this scope and window, locked together.
            # Selecting one row by recency was the ownership defect: a completed
            # or partial attempt updated more recently than a stalled one sorted
            # ahead of it, so the stalled worker was never seen, never revoked,
            # and its late result was still accepted by ``finish``.
            #
            # Authority and resumability are then two different questions.
            # Only an *active* attempt holds authority worth revoking, but any
            # earlier attempt may still hold progress worth resuming - a worker
            # that reported ``session_required`` at logout is interrupted, not
            # active, and its checkpoint is exactly what the next attempt needs.
            attempts = list(
                db.scalars(
                    select(LiveSyncJob)
                    .where(
                        LiveSyncJob.scope_key == scope,
                        LiveSyncJob.period_start == start,
                        LiveSyncJob.period_end == end,
                    )
                    .order_by(LiveSyncJob.created_at.asc(), LiveSyncJob.id.asc())
                    .with_for_update()
                )
            )
            active = [a for a in attempts if a.job_status in ACTIVE_STATUSES]
            live = [attempt for attempt in active if not self._stale(attempt)]
            if live:
                # A healthy attempt already owns this window; do not duplicate it.
                return self.public(live[-1]), None

            token = str(uuid.uuid4())
            now = datetime.now(UTC)
            # Taken before the retirement loop clears anything, and only from a
            # previous *attempt* at this same scope and window.
            saved = self._resumable_checkpoint(attempts, scope)
            for attempt in active:
                # Revoking the lease is what actually fences the old worker: its
                # next heartbeat or checkpoint raises, and a late finish writes
                # nothing.
                attempt.job_status = "interrupted"
                attempt.lease_token = None
                attempt.error_code = "session_required"
                attempt.updated_at = now
            # Every attempt has its own immutable published snapshot version.
            # Re-encrypt reusable checkpoints with the new job's associated data.
            job = LiveSyncJob(
                id=str(uuid.uuid4()),
                scope_key=scope,
                period_start=start,
                period_end=end,
                job_status="queued",
                created_at=now,
                updated_at=now,
                completed_steps=len(saved),
                total_steps=steps,
                lease_token=token,
            )
            if saved:
                job.checkpoint = self._seal(saved, f"{scope}:{job.id}:checkpoint")
            db.add(job)
            return self.public(job), token

    def read_job(self, scope: str, start: date | None, end: date | None) -> dict[str, Any] | None:
        with self._sessions() as db:
            query = select(LiveSyncJob).where(LiveSyncJob.scope_key == scope)
            query = query.options(
                load_only(
                    LiveSyncJob.id,
                    LiveSyncJob.job_status,
                    LiveSyncJob.period_start,
                    LiveSyncJob.period_end,
                    LiveSyncJob.created_at,
                    LiveSyncJob.updated_at,
                    LiveSyncJob.completed_steps,
                    LiveSyncJob.total_steps,
                    LiveSyncJob.error_code,
                )
            )
            if start is not None and end is not None:
                query = query.where(
                    LiveSyncJob.period_start == start, LiveSyncJob.period_end == end
                )
            job = db.scalar(
                query.order_by(LiveSyncJob.updated_at.desc(), LiveSyncJob.created_at.desc()).limit(
                    1
                )
            )
            return self.public(job) if job else None

    def heartbeat(self, job_id: str, token: str) -> None:
        """Renew the fenced lease without loading/decrypting patient checkpoints."""
        with self._sessions() as db, db.begin():
            changed = db.execute(
                update(LiveSyncJob)
                .where(
                    LiveSyncJob.id == job_id,
                    LiveSyncJob.lease_token == token,
                    # A terminal job never returns to running, even if a token
                    # somehow matched. Status is the second lock on authority.
                    LiveSyncJob.job_status.in_(ACTIVE_STATUSES),
                )
                .values(updated_at=datetime.now(UTC), job_status="running")
                .returning(LiveSyncJob.id)
            )
            if changed.scalar_one_or_none() is None:
                raise RuntimeError("job_lease_lost")

    def checkpoint(
        self, job_id: str, token: str, value: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._sessions() as db, db.begin():
            job = db.scalar(select(LiveSyncJob).where(LiveSyncJob.id == job_id).with_for_update())
            if job is None or job.lease_token != token or job.job_status not in ACTIVE_STATUSES:
                raise RuntimeError("job_lease_lost")
            aad = f"{job.scope_key}:{job.id}:checkpoint"
            if value is not None:
                job.checkpoint = self._seal(value, aad)
                job.completed_steps = len(value)
            job.updated_at = datetime.now(UTC)
            job.job_status = "running"
            return value if value is not None else self._open(job.checkpoint, aad)

    def finish(
        self, job_id: str, token: str, snapshot: dict[str, Any] | None, error: str | None = None
    ) -> None:
        with self._sessions() as db, db.begin():
            job = db.scalar(select(LiveSyncJob).where(LiveSyncJob.id == job_id).with_for_update())
            if job is None or job.lease_token != token or job.job_status not in ACTIVE_STATUSES:
                # A fenced or already-settled worker arriving late. Discard it
                # silently: this runs on the worker's own error paths, and
                # raising here would replace the real outcome with this one.
                return
            if snapshot is not None:
                job.snapshot = self._seal(snapshot, f"{job.scope_key}:{job.id}:snapshot")
                job.snapshot_status = snapshot["status"]
                job.job_status = "completed" if snapshot["status"] == "synchronized" else "partial"
                if job.job_status == "completed" or snapshot.get("retrieval_complete"):
                    job.checkpoint = None
            else:
                job.job_status = "interrupted" if error == "session_required" else "failed"
            job.error_code = error
            job.updated_at = datetime.now(UTC)
            job.lease_token = None

    def latest(self, scope: str, start: date | None, end: date | None) -> dict[str, Any] | None:
        with self._sessions() as db:
            query = select(LiveSyncJob).where(
                LiveSyncJob.scope_key == scope, LiveSyncJob.snapshot.is_not(None)
            )
            if start is not None and end is not None:
                query = query.where(
                    LiveSyncJob.period_start == start, LiveSyncJob.period_end == end
                )
            # A failed or partial refresh cannot displace a complete snapshot.
            job = db.scalar(
                query.order_by(
                    LiveSyncJob.period_end.desc(),
                    (LiveSyncJob.snapshot_status == "synchronized").desc(),
                    LiveSyncJob.created_at.desc(),
                ).limit(1)
            )
            if job is None:
                return None
            snapshot = self._open(job.snapshot, f"{job.scope_key}:{job.id}:snapshot")
            snapshot["snapshot_id"] = job.id
            return snapshot
