"""Background worker entry point.

Runs as a separate process from the API so that heavy analytical work never
occupies a request-serving thread (blueprint section 061).

Geography import and indicator materialisation are registered here today.
Encounter and aggregate imports have explicit, synchronous CLI entry points;
the remaining analytical refresh and signal jobs arrive in later prompts. The
process starts cleanly, reports its configuration and holds when it has no
queued work.
"""

from __future__ import annotations

import signal
import sys
import threading
from types import FrameType

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import Settings, get_settings

#: Jobs the worker can run, added by the phases that own them.
#:
#: Prompt 9  - e-register encounter ingestion
#: Prompt 11 - aggregate ingestion and reconciliation
#: Prompt 12 - DHIS2 exchange
#: Prompt 13 - indicator materialisation
#: Prompt 14 - episode construction
#: Prompt 17 - historical baseline refresh
#: Prompt 18 - temporal anomaly and persistence
#: Prompt 19 - geographic aggregation
#: Prompt 20 - spatial clustering
#: Prompt 21 - signal generation
REGISTERED_JOBS: tuple[str, ...] = (
    "geography.import",
    "indicator.materialise",
    "episode.build",
)


class Worker:
    """Long-running worker process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = get_logger("mars.worker")
        self._stop = threading.Event()

    def request_stop(self, signum: int, _frame: FrameType | None) -> None:
        """Handle SIGTERM and SIGINT so a container stop is graceful."""
        self._logger.info("worker_stop_requested", signal=signum)
        self._stop.set()

    def run(self) -> int:
        self._logger.info(
            "worker_starting",
            environment=self._settings.environment.value,
            release_version=self._settings.release_version,
            registered_jobs=list(REGISTERED_JOBS),
        )

        if not REGISTERED_JOBS:
            self._logger.info(
                "worker_idle",
                detail="No jobs are registered yet. The process will exit cleanly on SIGTERM.",
            )
        else:
            # No scheduler yet: jobs are invoked explicitly by an operator or a
            # test. A queue consumer replaces this loop when the first
            # event-driven job lands in Prompt 9.
            self._logger.info(
                "worker_jobs_available",
                jobs=list(REGISTERED_JOBS),
                detail="Invoked on demand; no scheduler is running.",
            )

        # Wait rather than spin. A scheduler replaces this when the first job
        # lands in Prompt 9.
        while not self._stop.wait(timeout=30.0):
            self._logger.debug("worker_heartbeat", registered_jobs=len(REGISTERED_JOBS))

        self._logger.info("worker_stopped")
        return 0


def run() -> None:  # pragma: no cover - process entry point
    """Console-script entry point for the worker service."""
    settings = get_settings()
    configure_logging(settings)

    worker = Worker(settings)
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)

    sys.exit(worker.run())


if __name__ == "__main__":  # pragma: no cover
    run()
