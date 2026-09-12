from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from mars.core.settings import Environment, Settings
from mars.integrations.dhis2.live_dashboard import (
    _assemble,
    _load_mapping,
    build_live_dashboard_runner,
)
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


def test_missing_mapping_is_a_local_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(LiveDashboardConfigurationError, match="missing or unreadable"):
        _load_mapping(tmp_path / "not-present.json")


def test_incomplete_mapping_names_the_missing_local_field(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        '{"schema_version":"2.0","status":"approved","programme_uid":"program",'
        '"datasets":{},"aggregate_data_elements":{},"tracker":{}}',
        encoding="utf-8",
    )
    with pytest.raises(LiveDashboardConfigurationError, match="mapping is incomplete"):
        _load_mapping(mapping)


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
    # Option-UID mapping recognises both positives. They are one day apart, so
    # under the explicit exploratory definition (minimum gap 7 days) they are two
    # eligible positives, not a repeat-positive pattern. The former assertion
    # encoded the retired "any two positive visits" rule.
    assert result["positive_patients"][0]["positive_encounter_count"] == 2
    assert result["repeat_positive_patients"] == []
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


# ---------------------------------------------------------------------------
# Shared-engine wiring and staged retrieval
# ---------------------------------------------------------------------------
LIVE_CONFIG = {
    "schema_version": "2.0",
    "status": "approved",
    "programme_uid": "programme",
    "datasets": {"monthly_105_opd": "opd"},
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
        "parent_event_data_element_uid": "parent",
        "stages": {
            "laboratory_tests": "labStage",
            "medical_visit": "visitStage",
            "medicines_and_supplies": "medStage",
        },
        "data_elements": {
            "laboratory_test_type": "test_type",
            "laboratory_result": "result",
            "medicine_type": "medicine",
        },
        "options": {
            "test_types_malaria": ["Malaria Test RDT"],
            "positive_malaria_results": ["Malaria: Positive - Plasmodium falciparum"],
            "negative_result": "Negative",
        },
    },
}
POSITIVE_RESULT = "Malaria: Positive - Plasmodium falciparum"
FACILITIES = [{"id": "F1", "name": "Facility One"}]
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def _lab_event(remote_id: str, person: str, day: date, facility: str = "F1") -> RemoteEvent:
    return RemoteEvent(
        remote_id=remote_id,
        person_remote_id=person,
        programme_remote_id="programme",
        programme_stage_remote_id="labStage",
        organisation_unit_remote_id=facility,
        occurred_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
        updated_at=None,
        status="COMPLETED",
        data_values={"test_type": "Malaria Test RDT", "result": POSITIVE_RESULT},
    )


class _Checkpoint:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values: dict[str, object] = dict(values or {})

    def check(self) -> None:
        return None

    def get(self, key: str) -> object:
        return self.values.get(key)

    def save(self, key: str, value: object) -> None:
        self.values[key] = value


class _FakeAggregate:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _FakeAggregate:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def fetch_data_values(self, _scope: object, _cursor: object) -> object:
        from mars.integrations.ports import RemotePage

        return RemotePage(records=(), next_cursor=None)


def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracker: type) -> Any:
    import json

    from mars.integrations.dhis2 import live_dashboard as module

    (tmp_path / "config" / "dhis2").mkdir(parents=True)
    (tmp_path / "config" / "dhis2" / "pader-live-v1.json").write_text(
        json.dumps(LIVE_CONFIG), encoding="utf-8"
    )
    monkeypatch.setattr(module, "Dhis2Client", _FakeAggregate)
    monkeypatch.setattr(module, "BoundedTrackerEventClient", tracker)
    settings = Settings(
        environment=Environment.LOCAL,
        auth_mode="live",
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_live",
        dev_auth_enabled=False,
        demo_mode_enabled=False,
        dhis2_login_base_url="https://eregisters.health.go.ug",
        patient_display_key="test-display-key",
    )
    return build_live_dashboard_runner(settings, project_root=tmp_path)


def _tracker(
    events_by_stage: dict[str, list[RemoteEvent]],
    calls: list[tuple[str, date, date]],
    *,
    fail_stage: str | None = None,
    cap_days: int | None = None,
) -> type:
    from mars.domain.enums import IntegrationErrorCategory
    from mars.integrations.dhis2.client import Dhis2Error
    from mars.integrations.ports import RemotePage

    class Fake:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Fake:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def fetch_events(self, scope: Any, _cursor: object) -> object:
            stage = scope.extra["program_stage_uid"]
            start, end = scope.period_start, scope.period_end
            calls.append((stage, start, end))
            if stage == fail_stage:
                # Not retryable, so the test does not sleep through back-off.
                raise Dhis2Error(IntegrationErrorCategory.AUTHORISATION, "denied")
            if cap_days is not None and (end - start).days + 1 > cap_days:
                raise Dhis2Error(IntegrationErrorCategory.RESPONSE_TOO_LARGE, "capped")
            records = tuple(
                e for e in events_by_stage.get(stage, []) if start <= e.occurred_at.date() <= end
            )
            return RemotePage(records=records, next_cursor=None)

    return Fake


def test_runner_reads_three_stages_over_the_recurrence_lookback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, date, date]] = []
    labs = [
        _lab_event("L1", "person-A", date(2026, 7, 25)),
        _lab_event("L2", "person-A", date(2026, 8, 5)),
    ]
    runner = _runner(tmp_path, monkeypatch, _tracker({"labStage": labs}, calls))
    result = runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Checkpoint())

    assert {stage for stage, _, _ in calls} == {"labStage", "visitStage", "medStage"}
    assert all(start == date(2026, 7, 4) and end == date(2026, 8, 31) for _, start, end in calls)
    assert result["tracker_lookback_start"] == "2026-07-04"
    assert result["repeat_positive_coverage"] == "complete"
    assert len(result["repeat_positive_patients"]) == 1
    kpi = next(k for k in result["kpis"] if k["code"] == "ENC_REPEAT_POSITIVE_INPUT")
    assert kpi["value"] == "1"
    assert "person-A" not in str(result)


def test_runner_retains_canonical_evidence_outside_the_public_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained: list[object] = []
    calls: list[tuple[str, date, date]] = []
    labs = [
        _lab_event("L1", "person-A", date(2026, 8, 2)),
        _lab_event("L2", "person-A", date(2026, 8, 12)),
    ]
    runner = _runner(tmp_path, monkeypatch, _tracker({"labStage": labs}, calls))

    result = runner(
        "u",
        "p",
        FACILITIES,
        *AUGUST,
        checkpoint=_Checkpoint(),
        evidence_sink=retained.append,
    )

    assert len(retained) == 1
    evidence, coverage = retained[0]
    assert len(evidence.encounters) == 2
    assert coverage.complete is True
    assert "person-A" not in str(result)
    assert "encounters" not in result


def test_an_old_laboratory_only_checkpoint_does_not_satisfy_the_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, date, date]] = []
    checkpoint = _Checkpoint({"tracker:F1": []})
    runner = _runner(tmp_path, monkeypatch, _tracker({}, calls))
    runner("u", "p", FACILITIES, *AUGUST, checkpoint=checkpoint)
    assert calls, "the stale laboratory-only checkpoint was treated as complete"
    assert any(key.startswith("tracker-plan/3:") for key in checkpoint.values)


def test_a_capped_window_is_split_rather_than_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, date, date]] = []
    labs = [
        _lab_event("L1", "person-A", date(2026, 7, 10)),
        _lab_event("L2", "person-A", date(2026, 8, 20)),
    ]
    runner = _runner(tmp_path, monkeypatch, _tracker({"labStage": labs}, calls, cap_days=20))
    result = runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Checkpoint())
    lab_windows = [(start, end) for stage, start, end in calls if stage == "labStage"]
    assert any((end - start).days + 1 > 20 for start, end in lab_windows)  # refused first
    assert any((end - start).days + 1 <= 20 for start, end in lab_windows)  # then split
    assert result["tracker_failed_facility_count"] == 0
    assert result["positive_malaria_event_count"] == 1  # only the in-period positive
    assert result["unique_positive_patient_count"] == 1


def test_a_failed_context_stage_is_reported_and_not_checkpointed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, date, date]] = []
    checkpoint = _Checkpoint()
    labs = [_lab_event("L1", "person-A", date(2026, 8, 2))]
    runner = _runner(
        tmp_path, monkeypatch, _tracker({"labStage": labs}, calls, fail_stage="medStage")
    )
    result = runner("u", "p", FACILITIES, *AUGUST, checkpoint=checkpoint)
    assert result["status"] == "partial"
    assert any("Visit or medicine records were unavailable" in w for w in result["warnings"])
    assert not any(key.startswith("tracker-plan/3:") for key in checkpoint.values)


def test_a_lost_session_stops_every_further_tracker_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, date, date]] = []

    class Lost(_Checkpoint):
        def get(self, key: str) -> object:
            if key.startswith("tracker-plan/3:"):
                raise LiveDashboardError("session_required")
            return None

    runner = _runner(tmp_path, monkeypatch, _tracker({}, calls))
    with pytest.raises(LiveDashboardError):
        runner("u", "p", FACILITIES, *AUGUST, checkpoint=Lost())
    assert calls == []


def test_repeat_patients_are_never_truncated() -> None:
    events: list[RemoteEvent] = []
    for index in range(30):
        events += [
            _lab_event(f"a{index}", f"p{index}", date(2026, 8, 1)),
            _lab_event(f"b{index}", f"p{index}", date(2026, 8, 12)),
        ]
    result = _assemble(_mapping(), [], events, {"F1": "Facility"}, *AUGUST, b"test", [], [])
    assert len(result["repeat_positive_patients"]) == 30


def test_rows_carry_named_intervals_and_no_source_references() -> None:
    events = [
        _lab_event("evt-SECRET-1", "person-SECRET", date(2026, 8, 1)),
        _lab_event("evt-SECRET-2", "person-SECRET", date(2026, 8, 10)),
        _lab_event("evt-SECRET-3", "person-SECRET", date(2026, 8, 24)),
    ]
    result = _assemble(_mapping(), [], events, {"F1": "Facility"}, *AUGUST, b"test", [], [])
    row = result["repeat_positive_patients"][0]
    assert row["adjacent_interval_days"] == [9, 14]
    assert row["anchor_interval_days"] == [9, 23]
    assert row["chain_interval_days"] == 23
    assert row["interval_days"] == 23
    assert row["determination"] == "qualifies"
    assert "SECRET" not in str(result)
    assert result["repeat_positive_definition"]["exploratory"] is True
    assert "not evidence of antimalarial resistance" in result["interpretation_limit"]


# ---------------------------------------------------------------------------
# Audit defect C: nothing is read after the job or session is lost
# ---------------------------------------------------------------------------
class _Revoked(_Checkpoint):
    """Active for ``allowed`` checks; the next check finds authority gone."""

    def __init__(self, allowed: int, log: list[str], error: Exception) -> None:
        super().__init__()
        self.allowed, self.log, self.error = allowed, log, error

    def check(self) -> None:
        if self.log.count("check") >= self.allowed:
            self.log.append("check:failed")
            raise self.error
        self.log.append("check")

    def get(self, key: str) -> object:
        self.check()
        return None

    def save(self, key: str, value: object) -> None:
        self.check()
        self.values[key] = value


def _logged_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    labs: list[RemoteEvent],
    log: list[str],
    aggregate_error: Exception | None = None,
) -> Any:
    from mars.integrations.dhis2 import live_dashboard as module
    from mars.integrations.ports import RemotePage

    class Aggregate(_FakeAggregate):
        def fetch_data_values(self, scope: Any, _cursor: object) -> object:
            log.append("aggregate:read" if scope.period_start == AUGUST[0] else "trend:read")
            if aggregate_error is not None:
                raise aggregate_error
            return RemotePage(records=(), next_cursor=None)

    class Tracker(_FakeAggregate):
        def fetch_events(self, scope: Any, _cursor: object) -> object:
            stage = scope.extra["program_stage_uid"]
            log.append(f"tracker:{stage}:read")
            records = tuple(labs) if stage == "labStage" else ()
            return RemotePage(records=records, next_cursor=None)

    runner = _runner(tmp_path, monkeypatch, Tracker)
    monkeypatch.setattr(module, "Dhis2Client", Aggregate)
    return runner


@pytest.mark.parametrize(
    "error", [LiveDashboardError("session_required"), RuntimeError("job_lease_lost")]
)
def test_no_source_read_follows_the_first_failed_active_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Every failure point, across aggregate, trend and each Tracker stage."""
    log: list[str] = []
    runner = _logged_runner(
        tmp_path, monkeypatch, [_lab_event("L1", "person-A", date(2026, 8, 2))], log
    )
    runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Revoked(10_000, log, error))
    total = log.count("check")
    assert {entry for entry in log if entry.endswith(":read")} == {
        "aggregate:read",
        "trend:read",
        "tracker:labStage:read",
        "tracker:visitStage:read",
        "tracker:medStage:read",
    }
    for allowed in range(total):
        log.clear()
        with pytest.raises(type(error)):
            runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Revoked(allowed, log, error))
        failed = log.index("check:failed")
        assert not [entry for entry in log[failed:] if entry.endswith(":read")], (allowed, log)


def test_the_first_aggregate_read_is_preceded_by_an_active_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's reproduction: a revoked session used to allow a second read."""
    log: list[str] = []
    runner = _logged_runner(tmp_path, monkeypatch, [], log)
    with pytest.raises(LiveDashboardError):
        runner(
            "u",
            "p",
            FACILITIES,
            *AUGUST,
            checkpoint=_Revoked(1, log, LiveDashboardError("session_required")),
        )
    assert log.count("aggregate:read") == 0
    assert not [entry for entry in log if entry.endswith(":read")]


def test_upstream_authentication_failure_stops_but_a_denial_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mars.domain.enums import IntegrationErrorCategory
    from mars.integrations.dhis2.client import Dhis2Error

    log: list[str] = []
    expired = Dhis2Error(IntegrationErrorCategory.AUTHENTICATION, "credentials rejected")
    runner = _logged_runner(tmp_path, monkeypatch, [], log, aggregate_error=expired)
    with pytest.raises(Dhis2Error):
        runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Checkpoint())
    assert log == ["aggregate:read"]

    log.clear()
    denied = Dhis2Error(IntegrationErrorCategory.AUTHORISATION, "not permitted")
    runner = _logged_runner(tmp_path / "denied", monkeypatch, [], log, aggregate_error=denied)
    result = runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Checkpoint())
    assert result["aggregate_retrieval_complete"] is False
    assert "tracker:labStage:read" in log


# ---------------------------------------------------------------------------
# Audit defect E: coverage dimensions and plan completion are separate
# ---------------------------------------------------------------------------
def test_a_failed_medicine_stage_leaves_recurrence_complete_but_the_plan_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mars.api.v1.schemas import LiveDashboardSnapshot

    calls: list[tuple[str, date, date]] = []
    labs = [
        _lab_event("L1", "person-A", date(2026, 8, 2)),
        _lab_event("L2", "person-A", date(2026, 8, 12)),
    ]
    runner = _runner(
        tmp_path, monkeypatch, _tracker({"labStage": labs}, calls, fail_stage="medStage")
    )
    result = runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Checkpoint())
    assert result["repeat_positive_coverage"] == "complete"
    assert result["laboratory_retrieval_complete"] is True
    assert result["treatment_context_coverage"] == "partial"
    assert result["retrieval_complete"] is False
    assert result["status"] == "partial"
    snapshot = LiveDashboardSnapshot.model_validate(result)
    assert snapshot.treatment_context_coverage == "partial"
    assert snapshot.retrieval_complete is False
    assert snapshot.retrieval_plan == "tracker-plan/3"


def test_a_complete_three_stage_plan_is_complete_in_every_dimension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, date, date]] = []
    labs = [_lab_event("L1", "person-A", date(2026, 8, 2))]
    runner = _runner(tmp_path, monkeypatch, _tracker({"labStage": labs}, calls))
    result = runner("u", "p", FACILITIES, *AUGUST, checkpoint=_Checkpoint())
    assert result["retrieval_complete"] is True
    assert result["treatment_context_coverage"] == "complete"
    assert result["laboratory_retrieval_complete"] is True
