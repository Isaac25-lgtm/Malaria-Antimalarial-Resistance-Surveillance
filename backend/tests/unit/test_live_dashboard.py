from __future__ import annotations

from datetime import UTC, date, datetime

from mars.integrations.dhis2.live_dashboard import _assemble
from mars.integrations.ports import RemoteDataValue, RemoteEvent
from mars.security.live_session import InMemoryCredentialHolder
from mars.services.live_dashboard import LiveDashboardError, LiveDashboardService


def _mapping() -> dict[str, object]:
    return {
        "aggregate_data_elements": {
            "new_attendance": "new",
            "reattendance": "return",
            "suspected_malaria": "suspected",
            "tested_for_malaria": "tested",
            "confirmed_malaria": "confirmed",
            "rdt_days_out_of_stock": "rdt_oos",
            "al_days_out_of_stock": "al_oos",
            "artesunate_days_out_of_stock": "art_oos",
        },
        "tracker": {
            "data_elements": {
                "laboratory_test_type": "test_type",
                "laboratory_result": "result",
            },
            "options": {
                "test_types_malaria": ["Malaria Test RDT"],
                "positive_malaria_results": ["Malaria: Positive \N{EN DASH} Plasmodium falciparum"],
            },
        },
    }


def test_assembles_real_values_and_never_exposes_remote_patient_uid() -> None:
    values = [
        RemoteDataValue("new", "facility1", "202608", "10"),
        RemoteDataValue("return", "facility1", "202608", "4"),
        RemoteDataValue("suspected", "facility1", "202608", "8"),
        RemoteDataValue("tested", "facility1", "202608", "6"),
        RemoteDataValue("confirmed", "facility1", "202608", "3"),
        RemoteDataValue("rdt_oos", "facility1", "202608", "2"),
    ]
    moments = (datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC))
    events = [
        RemoteEvent(
            remote_id=f"event{index}",
            person_remote_id="remotePatientSecret",
            programme_remote_id="programme",
            programme_stage_remote_id="lab",
            organisation_unit_remote_id="facility1",
            occurred_at=moment,
            updated_at=moment,
            status="COMPLETED",
            data_values={
                "test_type": "Malaria Test RDT",
                "result": "Malaria: Positive - Plasmodium falciparum",
            },
        )
        for index, moment in enumerate(moments)
    ]

    result = _assemble(
        _mapping(),
        values,
        events,
        {"facility1": "Laguti HC III"},
        date(2026, 8, 1),
        date(2026, 8, 31),
        b"test-display-key",
        [],
        [],
    )

    assert result["status"] == "synchronized"
    assert result["synthetic_data_used"] is False
    assert result["kpis"][0]["numerator"] == 14
    assert result["kpis"][4]["value"] == "75.0%"
    assert result["commodity_alerts"]["rdt_stock_out_facilities"] == 1
    assert result["repeat_positive_patients"][0]["interval_days"] == 13
    assert "remotePatientSecret" not in str(result)


def test_live_service_requires_scope_and_caches_only_declared_real_result() -> None:
    holder = InMemoryCredentialHolder()
    holder.store("session", "user", "secret")
    calls: list[tuple[int, date, date]] = []

    def runner(
        _username: str,
        _password: str,
        facilities: list[dict[str, str]],
        period_start: date,
        period_end: date,
    ) -> dict[str, object]:
        calls.append((len(facilities), period_start, period_end))
        return {"synthetic_data_used": False, "status": "unavailable"}

    service = LiveDashboardService(holder, runner)
    result = service.synchronize(
        "session",
        facilities=[{"id": "facility1", "name": "Laguti HC III"}],
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
    )

    assert result["synthetic_data_used"] is False
    assert service.latest("session") == result
    assert calls == [(1, date(2026, 8, 1), date(2026, 8, 31))]


def test_unsafe_ratio_is_withheld_and_option_uids_are_mapped() -> None:
    mapping = _mapping()
    tracker = mapping["tracker"]
    assert isinstance(tracker, dict)
    options = tracker["options"]
    assert isinstance(options, dict)
    options["test_types_malaria"] = ["cTBHMKQ1DD7"]
    options["positive_malaria_results"] = ["NQwrJOiyYTm"]
    values = [
        RemoteDataValue("new", "facility1", "202608", "10"),
        RemoteDataValue("return", "facility1", "202608", "0"),
        RemoteDataValue("suspected", "facility1", "202608", "4"),
        RemoteDataValue("tested", "facility1", "202608", "9"),
        RemoteDataValue("confirmed", "facility1", "202608", "5"),
    ]
    events = [
        RemoteEvent(
            remote_id=f"event{index}",
            person_remote_id="remote-patient",
            programme_remote_id="programme",
            programme_stage_remote_id="lab",
            organisation_unit_remote_id="facility1",
            occurred_at=datetime(2026, 8, 1 + index, tzinfo=UTC),
            updated_at=None,
            status="COMPLETED",
            data_values={"test_type": "cTBHMKQ1DD7", "result": "NQwrJOiyYTm"},
        )
        for index in range(2)
    ]

    result = _assemble(
        mapping,
        values,
        events,
        {"facility1": "Pader HC III"},
        date(2026, 8, 1),
        date(2026, 8, 31),
        b"test-display-key",
        [],
        [],
    )

    testing_rate = next(item for item in result["kpis"] if item["code"] == "TESTING_RATE")
    assert testing_rate["status"] == "unavailable"
    assert testing_rate["value"] is None
    assert result["positive_malaria_event_count"] == 2
    assert result["repeat_positive_patients"][0]["positive_encounter_count"] == 2
    assert {item["kind"] for item in result["operational_alerts"]} == {"data_quality"}


def test_live_service_refuses_unbounded_or_unscoped_reads() -> None:
    service = LiveDashboardService(InMemoryCredentialHolder(), lambda *_args: {})

    try:
        service.synchronize(
            "missing",
            facilities=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 8, 31),
        )
    except LiveDashboardError as error:
        assert "1 to 62 days" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unbounded live read was accepted")
