from __future__ import annotations

from datetime import date

import pytest

from mars.security.live_session import InMemoryCredentialHolder
from mars.services.live_tracker import LiveTrackerPreviewError, LiveTrackerPreviewService


def _result() -> dict[str, object]:
    return {
        "status": "validated_preview",
        "facility_uid": "fAcility1234",
        "period_start": "2026-08-01",
        "period_end": "2026-08-07",
        "retrieved_event_count": 12,
        "unique_patient_count": 10,
        "loadable_event_count": 11,
        "invalid_event_count": 1,
        "positive_event_count": 4,
        "field_coverage": {"malaria_test_result": 12},
        "mapping_schema_version": "1.0",
        "patient_data_retrieved": True,
        "unexpected_patient_rows": [{"name": "must not survive"}],
    }


def test_preview_retains_counts_and_never_patient_rows() -> None:
    holder = InMemoryCredentialHolder()
    holder.store("sid", "officer", "sentinel-secret")
    observed: list[tuple[str, str]] = []

    def runner(
        username: str,
        password: str,
        _facility: str,
        _start: date,
        _end: date,
    ) -> dict[str, object]:
        observed.append((username, password))
        return _result()

    service = LiveTrackerPreviewService(holder, runner)
    result = service.preview(
        "sid",
        facility_uid="fAcility1234",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 7),
        approved_facility_uids=frozenset({"fAcility1234"}),
    )
    assert result["retrieved_event_count"] == 12
    assert result["patient_rows_returned"] is False
    assert result["persisted"] is False
    assert "unexpected_patient_rows" not in result
    assert "must not survive" not in repr(result)
    assert observed == [("officer", "sentinel-secret")]
    assert service.latest("sid") == result
    service.drop("sid")
    assert service.latest("sid") is None


def test_preview_refuses_facility_not_proven_by_discovery() -> None:
    holder = InMemoryCredentialHolder()
    holder.store("sid", "officer", "secret")
    service = LiveTrackerPreviewService(holder, lambda *_args: _result())
    with pytest.raises(LiveTrackerPreviewError, match="not proven"):
        service.preview(
            "sid",
            facility_uid="fAcility1234",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 7),
            approved_facility_uids=frozenset({"oTher123456"}),
        )


def test_preview_refuses_more_than_fourteen_days_before_network() -> None:
    called = False

    def runner(*_args: object) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    holder = InMemoryCredentialHolder()
    holder.store("sid", "officer", "secret")
    service = LiveTrackerPreviewService(holder, runner)
    with pytest.raises(LiveTrackerPreviewError, match="1 to 14 days"):
        service.preview(
            "sid",
            facility_uid="fAcility1234",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 15),
            approved_facility_uids=frozenset({"fAcility1234"}),
        )
    assert called is False
