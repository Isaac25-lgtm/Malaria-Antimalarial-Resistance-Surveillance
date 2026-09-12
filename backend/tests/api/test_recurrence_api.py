from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

from mars.api.dependencies import (
    get_recurrence_analysis_service,
    get_recurrence_run_executor,
)


def run_shape(run_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": "queued",
        "error_code": None,
        "mode": "exploratory",
        "source": "stored_encounter",
        "definition_name": "Exploratory definition",
        "definition": {"minimum_gap_days": 7, "maximum_window_days": 28},
        "definition_checksum": "d" * 64,
        "definition_version_id": None,
        "method_version_id": None,
        "exploratory": True,
        "engine_version": "positive-recurrence/1.0.0",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "timezone": "Africa/Kampala",
        "facility_count": 1,
        "national_scope": False,
        "filters": {},
        "manifest_checksum": "m" * 64,
        "created_by": "district.pader",
        "created_at": datetime(2026, 9, 11, tzinfo=UTC),
        "completed_at": None,
        "progress_completed": 0,
        "progress_total": 3,
        "dataset": None,
        "summary": None,
        "coverage": None,
        "interpretation": "Requires investigation; does not confirm resistance.",
    }


class FakeRecurrenceService:
    def __init__(self) -> None:
        self.submission: Any = None
        self.patient_filters: Any = None
        self.run_id = uuid.uuid4()

    def programme_status(self) -> dict[str, Any]:
        return {
            "method_code": "positive-recurrence",
            "active": False,
            "detail": "No approved programme method is active.",
        }

    def submit(self, _principal: Any, submission: Any, *, live_scope: Any = None):
        self.submission = submission
        return SimpleNamespace(id=self.run_id), "lease-token"

    def run_shape(self, _record: Any) -> dict[str, Any]:
        return run_shape(self.run_id)

    def patients_page(self, _principal: Any, run_id: uuid.UUID, **kwargs: Any):
        self.patient_filters = kwargs["filters"]
        return {
            "run_id": run_id,
            "items": [],
            "total": 0,
            "limit": kwargs["limit"],
            "next_cursor": None,
            "previous_cursor": None,
            "filters": kwargs["filters"].as_dict(),
        }


class FakeExecutor:
    def __init__(self) -> None:
        self.started: list[tuple[uuid.UUID, str]] = []

    def start(self, run_id: uuid.UUID, token: str, **_kwargs: Any) -> None:
        self.started.append((run_id, token))


def install(app: Any) -> tuple[FakeRecurrenceService, FakeExecutor]:
    service, executor = FakeRecurrenceService(), FakeExecutor()
    app.dependency_overrides[get_recurrence_analysis_service] = lambda: service
    app.dependency_overrides[get_recurrence_run_executor] = lambda: executor
    return service, executor


def test_programme_status_remains_available_without_an_approved_method(
    app: Any, authenticated_client: Any, pader_district_principal: Any
) -> None:
    install(app)
    response = authenticated_client(pader_district_principal).get("/api/v1/recurrence/programme")
    assert response.status_code == 200
    assert response.json() == {
        "method_code": "positive-recurrence",
        "active": False,
        "detail": "No approved programme method is active.",
        "method_version_id": None,
        "semantic_version": None,
        "parameters": None,
        "effective_from": None,
        "approved_by": None,
    }


def test_apply_returns_202_and_starts_the_fenced_worker(
    app: Any, authenticated_client: Any, pader_district_principal: Any
) -> None:
    service, executor = install(app)
    response = authenticated_client(pader_district_principal).post(
        "/api/v1/recurrence/runs",
        json={
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "source": "stored_encounter",
            "mode": "exploratory",
            "parameters": {"minimum_gap_days": 7, "maximum_window_days": 28},
        },
    )
    assert response.status_code == 202
    assert response.json()["id"] == str(service.run_id)
    assert executor.started == [(service.run_id, "lease-token")]
    assert service.submission.period_start == date(2026, 8, 1)


def test_patient_filters_are_server_side_and_preserve_repeated_query_values(
    app: Any, authenticated_client: Any, pader_district_principal: Any
) -> None:
    service, _ = install(app)
    run_id = uuid.uuid4()
    response = authenticated_client(pader_district_principal).get(
        f"/api/v1/recurrence/runs/{run_id}/patients",
        params=[("quality", "VALID"), ("quality", "INCOMPLETE_TREATMENT_EVIDENCE")],
    )
    assert response.status_code == 200
    assert service.patient_filters.quality == frozenset({"VALID", "INCOMPLETE_TREATMENT_EVIDENCE"})


def test_aggregate_permission_is_required_before_the_service_is_called(
    app: Any, authenticated_client: Any, pader_district_principal: Any
) -> None:
    install(app)
    denied = replace(pader_district_principal, permissions=frozenset())
    response = authenticated_client(denied).get("/api/v1/recurrence/programme")
    assert response.status_code == 403
