from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from mars.core.settings import Environment, Settings
from mars.integrations.dhis2.live_dashboard import _assemble, build_live_dashboard_runner
from mars.integrations.ports import RemoteDataValue, RemoteEvent
from mars.security.live_session import InMemoryCredentialHolder
from mars.services.live_dashboard import (
    LiveDashboardConfigurationError,
    LiveDashboardError,
    LiveDashboardService,
    snapshot_csv,
)


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


def test_missing_patient_display_key_fails_before_mapping_or_remote_reads() -> None:
    settings = Settings(
        environment=Environment.LOCAL,
        auth_mode="live",
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_live",
        dev_auth_enabled=False,
        demo_mode_enabled=False,
        dhis2_login_base_url="https://dhis2.example.org",
    )
    runner = build_live_dashboard_runner(settings, project_root=Path("unused-before-key-check"))

    try:
        runner("officer", "secret", [{"id": "facility1"}], date(2026, 8, 1), date(2026, 8, 31))
    except LiveDashboardConfigurationError as error:
        assert "MARS_PATIENT_DISPLAY_KEY" in str(error)
    else:
        raise AssertionError("missing patient display key did not fail closed")


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


def test_partial_tracker_cannot_claim_complete_sync_or_zero_recurrence() -> None:
    result = _assemble(
        _mapping(),
        [RemoteDataValue("confirmed", "f1", "202608", "1")],
        [
            RemoteEvent(
                remote_id="e1",
                person_remote_id="p1",
                programme_remote_id="p",
                programme_stage_remote_id="lab",
                organisation_unit_remote_id="f1",
                occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
                updated_at=None,
                status="COMPLETED",
                data_values={"test_type": "Malaria Test RDT", "result": "Negative"},
            )
        ],
        {"f1": "Facility", "f2": "Unavailable facility"},
        date(2026, 8, 1),
        date(2026, 8, 31),
        b"test",
        [],
        ["f2"],
    )
    assert result["status"] == "partial"
    assert (
        next(k for k in result["kpis"] if k["code"] == "ENC_REPEAT_POSITIVE_INPUT")["value"] is None
    )


def test_two_tests_on_same_visit_are_not_repeat_positive_encounters() -> None:
    mapping = _mapping()
    mapping["tracker"]["parent_event_data_element_uid"] = "visit"  # type: ignore[index]
    events = [
        RemoteEvent(
            remote_id=f"e{i}",
            person_remote_id="person",
            programme_remote_id="p",
            programme_stage_remote_id="lab",
            organisation_unit_remote_id="f1",
            occurred_at=datetime(2026, 8, 2, 8 + i, tzinfo=UTC),
            updated_at=None,
            status="COMPLETED",
            data_values={
                "visit": "one-visit",
                "test_type": "Malaria Test RDT",
                "result": "Malaria: Positive - Plasmodium falciparum",
            },
        )
        for i in range(2)
    ]
    result = _assemble(
        mapping,
        [],
        events + events,
        {"f1": "Facility"},
        date(2026, 8, 1),
        date(2026, 8, 31),
        b"test",
        [],
        [],
    )
    assert result["positive_malaria_event_count"] == 2
    assert result["repeat_positive_patients"] == []
    assert len(result["positive_patients"]) == 1
    assert len(result["positive_patients"][0]["tests"]) == 2
    assert "one-visit" not in str(result)


def test_zero_positivity_is_a_valid_trend_value() -> None:
    from mars.integrations.dhis2.live_dashboard import _trend_points

    result = _trend_points(
        _mapping()["aggregate_data_elements"],
        [  # type: ignore[arg-type]
            RemoteDataValue("tested", "f1", "202608", "10"),
            RemoteDataValue("confirmed", "f1", "202608", "0"),
        ],
    )
    assert result[0]["positivity_rate"] == 0


def test_snapshots_are_period_and_session_scoped_and_defensively_copied() -> None:
    credentials = InMemoryCredentialHolder()
    credentials.store("session", "user", "test-only")
    service = LiveDashboardService(
        credentials, lambda *_args: {"synthetic_data_used": False, "items": [1]}
    )
    result = service.synchronize(
        "session",
        facilities=[{"id": "f1"}],
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
    )
    result["items"].append(2)
    assert service.latest("session")["items"] == [1]  # type: ignore[index]
    assert (
        service.latest("session", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        is None
    )
    assert service.latest("other-session") is None
    service.drop("session")
    assert (
        service.latest("session", period_start=date(2026, 8, 1), period_end=date(2026, 8, 31))
        is None
    )


def test_export_escapes_formula_cells_and_excludes_patient_rows() -> None:
    result = snapshot_csv(
        {
            "facilities": [{"name": "=malicious()", "confirmed_malaria": 5}],
            "positive_patients": [{"mars_patient_id": "PRIVATE-ALIAS"}],
            "warnings": ["Partial coverage"],
        }
    )
    assert "'=malicious()" in result
    assert "PRIVATE-ALIAS" not in result
    assert "Partial coverage" in result
