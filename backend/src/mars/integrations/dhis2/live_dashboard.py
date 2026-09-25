"""Read-only live dashboard assembly from approved DHIS2 metadata.

This module is application wiring, not a surveillance rule engine. It retrieves
reported HMIS values and the laboratory, medical-visit and medicine Tracker
stages over the reporting period plus the recurrence lookback, and hands them to
the shared canonical adapter and recurrence engine
(``docs/methods/configurable-recurrence.md``). It never requests tracked-entity
attributes and never returns a DHIS2 tracked-entity UID to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from mars.core.settings import Settings
from mars.domain.enums import IntegrationErrorCategory
from mars.domain.longitudinal import (
    EXPLORATORY_PRESET,
    INTERPRETATION_LIMIT,
    UNRESOLVED_OUTCOMES,
    AdaptedEvidence,
    EvidenceCoverage,
    LabOutcome,
)
from mars.integrations.dhis2.client import Dhis2Client, Dhis2Config, Dhis2Error
from mars.integrations.dhis2.tracker.client import BoundedTrackerEventClient, TrackerClientConfig
from mars.integrations.dhis2.tracker.clinical_adapter import (
    LAB,
    MEDICINE,
    VISIT,
    LiveClinicalMapping,
    adapt_live_events,
    live_namespace,
)
from mars.integrations.ports import RemoteDataValue, RemoteEvent, RemoteScope, iterate_pages
from mars.services.durable_live_dashboard import SyncCheckpoint
from mars.services.live_dashboard import LiveDashboardConfigurationError, LiveDashboardError
from mars.services.live_recurrence import evaluate_live_recurrence

#: The snapshot's repeat-positive question. An exploratory preset, labelled as
#: such in every snapshot, until a programme method is approved and in force.
SNAPSHOT_DEFINITION = EXPLORATORY_PRESET

#: Versioned so a checkpoint written under an older retrieval plan - the former
#: laboratory-only one included - can never satisfy this one. Plan 3 carries each
#: event's recorded date precision, which plan-2 checkpoints lack.
TRACKER_PLAN_VERSION = "tracker-plan/3"

#: Matches the Tracker client's bounded request window.
TRACKER_WINDOW_DAYS = 62

#: How many times a capped window may be halved before it is a genuine failure.
MAX_WINDOW_SPLITS = 6


def _is_fatal(error: BaseException) -> bool:
    """Whether an error must end the whole retrieval rather than degrade it.

    Cancellation and a lost MARS session (``LiveDashboardError``), a lost job
    lease (``job_lease_lost``) and an upstream authentication failure all mean
    that no further request is authorised. Only a bounded upstream availability
    failure may become a partial snapshot.
    """
    if isinstance(error, Dhis2Error):
        return error.category is IntegrationErrorCategory.AUTHENTICATION
    if isinstance(error, LiveDashboardError):
        return True
    return isinstance(error, RuntimeError) and str(error) == "job_lease_lost"


def _guarded(checkpoint: SyncCheckpoint | None, operation: Callable[[], Any]) -> Any:
    """Check the job and session immediately before one remote request."""
    if checkpoint:
        checkpoint.check()
    return operation()


def build_live_dashboard_runner(
    settings: Settings, *, project_root: Path
) -> Callable[[str, str, Sequence[Mapping[str, Any]], date, date], dict[str, Any]]:
    mapping_path = project_root / "config" / "dhis2" / "eregisters-live-v1.json"

    def run(
        username: str,
        password: str,
        facilities: Sequence[Mapping[str, Any]],
        period_start: date,
        period_end: date,
        *,
        checkpoint: SyncCheckpoint | None = None,
        evidence_sink: Callable[[tuple[AdaptedEvidence, EvidenceCoverage]], None] | None = None,
        scope_name: str = "Authorised scope",
    ) -> dict[str, Any]:
        display_key = settings.patient_display_key
        if display_key is None:
            # Check before any Ministry endpoint is contacted. A missing local
            # alias key must never cause authenticated reads whose results are
            # then discarded.
            raise LiveDashboardConfigurationError(
                "Patient evidence requires MARS_PATIENT_DISPLAY_KEY. "
                "Start MARS with the live launcher so a stable protected key is loaded."
            )
        mapping = _load_mapping(mapping_path)
        mapping["_runtime_facilities"] = [dict(item) for item in facilities]
        mapping["_runtime_scope_name"] = scope_name
        facility_names = {
            str(item["id"]): str(item.get("name") or item["id"])
            for item in facilities
            if isinstance(item.get("id"), str)
        }
        facility_uids = tuple(facility_names)
        aggregate_values: list[RemoteDataValue] = []
        trend_values: list[RemoteDataValue] = []
        warnings: list[str] = []

        config = Dhis2Config(
            base_url=settings.dhis2_login_base_url,
            username=username,
            password=password,
            token=None,
            timeout_seconds=max(settings.dhis2_login_timeout_seconds, 30.0),
            max_retries=1,
            retry_backoff_seconds=0.5,
            page_size=50,
            max_response_bytes=32 * 1024 * 1024,
            verify_tls=settings.dhis2_login_verify_tls,
        )
        aggregate_scope = RemoteScope(
            organisation_unit_remote_ids=facility_uids,
            dataset_remote_ids=tuple(mapping["datasets"].values()),
            data_element_remote_ids=tuple(mapping["aggregate_data_elements"].values()),
            period_start=period_start,
            period_end=period_end,
        )
        aggregate_complete = False
        trend_complete = False
        try:
            with Dhis2Client(config) as client:
                cached = checkpoint.get("aggregate") if checkpoint else None
                if cached is not None:
                    aggregate_values = [RemoteDataValue(**item) for item in cached]
                else:
                    for page in iterate_pages(
                        lambda cursor: _guarded(
                            checkpoint,
                            lambda: client.fetch_data_values(aggregate_scope, cursor),
                        ),
                        max_pages=10,
                    ):
                        if checkpoint:
                            checkpoint.check()
                        aggregate_values.extend(
                            value for value in page.records if isinstance(value, RemoteDataValue)
                        )
                    if checkpoint:
                        checkpoint.save(
                            "aggregate", [_aggregate_checkpoint(v) for v in aggregate_values]
                        )
                aggregate_complete = True
        except Exception as error:
            if _is_fatal(error):
                # Cancellation, session loss and lost job authority are not a
                # source outage: no further request may follow.
                raise
            aggregate_values = []
            warnings.append(f"Aggregate HMIS request unavailable ({type(error).__name__})")

        try:
            with Dhis2Client(config) as client:
                trend_scope = RemoteScope(
                    organisation_unit_remote_ids=facility_uids,
                    dataset_remote_ids=(mapping["datasets"]["monthly_105_opd"],),
                    data_element_remote_ids=tuple(
                        mapping["aggregate_data_elements"][name]
                        for name in (
                            "new_attendance",
                            "reattendance",
                            "suspected_malaria",
                            "tested_for_malaria",
                            "confirmed_malaria",
                        )
                    ),
                    period_start=_month_start(period_end, months_before=11),
                    period_end=period_end,
                )
                cached = checkpoint.get("trend") if checkpoint else None
                if cached is not None:
                    trend_values = [RemoteDataValue(**item) for item in cached]
                else:
                    for page in iterate_pages(
                        lambda cursor: _guarded(
                            checkpoint, lambda: client.fetch_data_values(trend_scope, cursor)
                        ),
                        max_pages=10,
                    ):
                        if checkpoint:
                            checkpoint.check()
                        trend_values.extend(
                            value for value in page.records if isinstance(value, RemoteDataValue)
                        )
                    if checkpoint:
                        checkpoint.save("trend", [_aggregate_checkpoint(v) for v in trend_values])
                trend_complete = True
        except Exception as error:
            if _is_fatal(error):
                raise
            trend_values = []
            warnings.append(f"Historical HMIS request unavailable ({type(error).__name__})")

        clinical = LiveClinicalMapping.from_config(mapping)
        extent_start = period_start - timedelta(days=SNAPSHOT_DEFINITION.maximum_window_days)
        stages: dict[str, str] = {LAB: str(clinical.lab_stage)}
        if clinical.retrieves_context:
            stages[VISIT] = str(clinical.visit_stage)
            stages[MEDICINE] = str(clinical.medicine_stage)
        plan = _tracker_plan_key(clinical, stages, extent_start, period_end)
        events_by_stage: dict[str, list[RemoteEvent]] = {stage: [] for stage in stages}
        tracker_failed: list[str] = []
        context_failed: list[str] = []
        tracker_config = TrackerClientConfig(
            base_url=settings.dhis2_login_base_url,
            username=username,
            password=password,
            timeout_seconds=max(settings.dhis2_login_timeout_seconds, 30.0),
            page_size=100,
            max_records=10_000,
            max_window_days=TRACKER_WINDOW_DAYS,
            max_response_bytes=8 * 1024 * 1024,
        )
        programme = str(mapping["programme_uid"])
        with BoundedTrackerEventClient(
            tracker_config,
            authorized_org_unit_uids=frozenset(facility_uids),
        ) as client:
            for facility_uid in facility_uids:
                key = f"{plan}:{facility_uid}"
                cached = checkpoint.get(key) if checkpoint else None
                if cached is not None:
                    for stage, items in cached.items():
                        events_by_stage.setdefault(stage, []).extend(
                            _event_from_checkpoint(item) for item in items
                        )
                    continue
                fetched: dict[str, list[RemoteEvent]] = {}
                try:
                    fetched[LAB] = _fetch_stage(
                        client,
                        facility_uid,
                        programme,
                        stages[LAB],
                        extent_start,
                        period_end,
                        checkpoint,
                    )
                except Exception as error:
                    if _is_fatal(error):
                        # A lost session, cancelled job or lost lease stops
                        # every further read; it is never a facility failure.
                        raise
                    tracker_failed.append(facility_uid)
                    continue
                context_complete = True
                for stage in (VISIT, MEDICINE):
                    if stage not in stages:
                        continue
                    try:
                        fetched[stage] = _fetch_stage(
                            client,
                            facility_uid,
                            programme,
                            stages[stage],
                            extent_start,
                            period_end,
                            checkpoint,
                        )
                    except Exception as error:
                        if _is_fatal(error):
                            raise
                        context_complete = False
                if context_complete:
                    if checkpoint:
                        checkpoint.save(
                            key,
                            {stage: [asdict(e) for e in items] for stage, items in fetched.items()},
                        )
                else:
                    # Not checkpointed, so a retry fetches this facility again.
                    context_failed.append(facility_uid)
                for stage, items in fetched.items():
                    events_by_stage[stage].extend(items)
        if tracker_failed:
            warnings.append(
                f"Tracker events were unavailable for {len(tracker_failed)} of "
                f"{len(facility_uids)} authorised facilities"
            )

        result = _assemble(
            mapping,
            aggregate_values,
            events_by_stage[LAB],
            facility_names,
            period_start,
            period_end,
            display_key.get_secret_value().encode("utf-8"),
            warnings,
            tracker_failed,
            trend_values,
            visit_events=events_by_stage.get(VISIT, []),
            medicine_events=events_by_stage.get(MEDICINE, []),
            tracker_extent_start=extent_start,
            context_retrieved=clinical.retrieves_context,
            context_failed=context_failed,
            namespace=live_namespace(
                urlsplit(settings.dhis2_login_base_url).hostname or "unknown-host",
                clinical.programme,
            ),
            retain=evidence_sink,
        )
        # Laboratory recurrence evidence, clinical context and the aggregate
        # sources are separate coverage dimensions. The retrieval *plan* is
        # complete only when every requested stage of every facility arrived;
        # until then the durable store keeps resumable progress.
        context_status = (
            "not_mapped"
            if not clinical.retrieves_context
            else "partial"
            if tracker_failed or context_failed
            else "complete"
        )
        result.update(
            retrieval_complete=(
                aggregate_complete and trend_complete and not tracker_failed and not context_failed
            ),
            aggregate_retrieval_complete=aggregate_complete,
            trend_retrieval_complete=trend_complete,
            laboratory_retrieval_complete=not tracker_failed,
            treatment_context_coverage=context_status,
            tracker_retrieved_facility_count=len(facility_uids) - len(tracker_failed),
            retrieval_plan=TRACKER_PLAN_VERSION,
        )
        return result

    return run


def _aggregate_checkpoint(value: RemoteDataValue) -> dict[str, Any]:
    row = asdict(value)
    row.pop("stored_by", None)
    row.pop("comment", None)
    return row


def _retry_tracker_read(operation: Callable[[], Any], checkpoint: SyncCheckpoint | None) -> Any:
    for attempt in range(3):
        if checkpoint:
            checkpoint.check()
        try:
            return operation()
        except Dhis2Error as error:
            if not error.is_retryable or attempt == 2:
                raise
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError("Tracker retry bound exhausted")


def _tracker_plan_key(
    clinical: LiveClinicalMapping, stages: Mapping[str, str], start: date, end: date
) -> str:
    stage_part = "+".join(f"{name}={uid}" for name, uid in sorted(stages.items()))
    return (
        f"{TRACKER_PLAN_VERSION}:{clinical.version}:{stage_part}:"
        f"{start.isoformat()}:{end.isoformat()}"
    )


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Bounded windows covering ``start``..``end`` inclusive.

    Consecutive windows share one day, so an inclusive-or-exclusive boundary in
    the source cannot drop it; that day's events arrive twice and
    source-revision classification keeps one.
    """
    chunks: list[tuple[date, date]] = []
    cursor = start
    while True:
        chunk_end = min(end, cursor + timedelta(days=max_days - 1))
        chunks.append((cursor, chunk_end))
        if chunk_end >= end:
            return chunks
        cursor = chunk_end if max_days > 1 else chunk_end + timedelta(days=1)


def _fetch_window(
    client: BoundedTrackerEventClient,
    facility: str,
    programme: str,
    stage: str,
    start: date,
    end: date,
    checkpoint: SyncCheckpoint | None,
    depth: int = 0,
) -> list[RemoteEvent]:
    """One stage, one facility, one bounded window.

    A capped response is halved and retried rather than accepted as complete.
    A single day that still exceeds the cap is a genuine failure.
    """
    scope = RemoteScope(
        organisation_unit_remote_ids=(facility,),
        period_start=start,
        period_end=end,
        extra={"programme_uid": programme, "program_stage_uid": stage},
    )
    try:
        events: list[RemoteEvent] = []
        for page in iterate_pages(
            lambda cursor: _retry_tracker_read(
                lambda: client.fetch_events(scope, cursor), checkpoint
            ),
            max_pages=100,
        ):
            if checkpoint:
                checkpoint.check()
            events.extend(event for event in page.records if isinstance(event, RemoteEvent))
        return events
    except Dhis2Error as error:
        if (
            error.category is IntegrationErrorCategory.RESPONSE_TOO_LARGE
            and start < end
            and depth < MAX_WINDOW_SPLITS
        ):
            middle = start + timedelta(days=(end - start).days // 2)
            return [
                *_fetch_window(
                    client, facility, programme, stage, start, middle, checkpoint, depth + 1
                ),
                *_fetch_window(
                    client,
                    facility,
                    programme,
                    stage,
                    middle + timedelta(days=1),
                    end,
                    checkpoint,
                    depth + 1,
                ),
            ]
        raise


def _fetch_stage(
    client: BoundedTrackerEventClient,
    facility: str,
    programme: str,
    stage: str,
    start: date,
    end: date,
    checkpoint: SyncCheckpoint | None,
) -> list[RemoteEvent]:
    events: list[RemoteEvent] = []
    for chunk_start, chunk_end in _date_chunks(start, end, TRACKER_WINDOW_DAYS):
        events.extend(
            _fetch_window(client, facility, programme, stage, chunk_start, chunk_end, checkpoint)
        )
    return events


def _event_from_checkpoint(value: Mapping[str, Any]) -> RemoteEvent:
    row = dict(value)
    for key in ("occurred_at", "updated_at"):
        if isinstance(row.get(key), str):
            row[key] = datetime.fromisoformat(row[key])
    return RemoteEvent(**row)


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveDashboardConfigurationError(
            "The approved eRegisters live mapping is missing or unreadable"
        ) from error
    if raw.get("schema_version") != "2.0" or raw.get("status") != "approved":
        raise LiveDashboardConfigurationError(
            "The eRegisters live mapping is absent or not approved"
        )
    required = ("programme_uid", "datasets", "aggregate_data_elements", "tracker")
    if any(not raw.get(key) for key in required):
        raise LiveDashboardConfigurationError("The eRegisters live mapping is incomplete")
    nested_requirements = {
        "datasets": ("monthly_105_opd",),
        "aggregate_data_elements": (
            "new_attendance",
            "reattendance",
            "suspected_malaria",
            "tested_for_malaria",
            "confirmed_malaria",
            "rdt_days_out_of_stock",
            "al_days_out_of_stock",
            "artesunate_days_out_of_stock",
        ),
    }
    tracker = raw.get("tracker")
    if not isinstance(tracker, dict):
        raise LiveDashboardConfigurationError("The eRegisters live Tracker mapping is incomplete")
    nested_requirements.update(
        {
            "tracker.stages": ("laboratory_tests",),
            "tracker.data_elements": ("laboratory_test_type", "laboratory_result"),
            "tracker.options": (
                "test_types_malaria",
                "positive_malaria_results",
                "negative_result",
            ),
        }
    )
    containers: dict[str, Any] = {
        "datasets": raw.get("datasets"),
        "aggregate_data_elements": raw.get("aggregate_data_elements"),
        "tracker.stages": tracker.get("stages"),
        "tracker.data_elements": tracker.get("data_elements"),
        "tracker.options": tracker.get("options"),
    }
    missing = [
        f"{container}.{key}"
        for container, keys in nested_requirements.items()
        for key in keys
        if not isinstance(containers.get(container), dict) or not containers[container].get(key)
    ]
    if missing:
        raise LiveDashboardConfigurationError(
            "The eRegisters live mapping is incomplete: " + ", ".join(missing)
        )
    return cast(dict[str, Any], raw)


def _assemble(
    mapping: Mapping[str, Any],
    values: Sequence[RemoteDataValue],
    events: Sequence[RemoteEvent],
    facility_names: Mapping[str, str],
    period_start: date,
    period_end: date,
    display_key: bytes,
    warnings: list[str],
    tracker_failed: Sequence[str],
    trend_values: Sequence[RemoteDataValue] = (),
    *,
    visit_events: Sequence[RemoteEvent] = (),
    medicine_events: Sequence[RemoteEvent] = (),
    tracker_extent_start: date | None = None,
    context_retrieved: bool = False,
    context_failed: Sequence[str] = (),
    namespace: str = "dhis2:live",
    retain: Callable[[tuple[AdaptedEvidence, EvidenceCoverage]], None] | None = None,
) -> dict[str, Any]:
    elements: Mapping[str, str] = mapping["aggregate_data_elements"]
    by_element: dict[str, int] = defaultdict(int)
    facility_values: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    invalid_value_count = 0
    latest_source_update: datetime | None = None
    unique_values = _deduplicate_values(values)
    for item in unique_values:
        parsed = _reported_count(item.value)
        if parsed is None:
            invalid_value_count += 1
            continue
        by_element[item.data_element_remote_id] += parsed
        facility_values[item.organisation_unit_remote_id][item.data_element_remote_id] += parsed
        candidate = _parse_source_moment(item.last_updated)
        if candidate and (latest_source_update is None or candidate > latest_source_update):
            latest_source_update = candidate

    def total(name: str) -> int | None:
        uid = elements[name]
        return (
            by_element.get(uid)
            if any(v.data_element_remote_id == uid for v in unique_values)
            else None
        )

    new_attendance = total("new_attendance")
    reattendance = total("reattendance")
    encounters = (
        new_attendance + reattendance
        if new_attendance is not None and reattendance is not None
        else new_attendance
        if reattendance is None
        else reattendance
    )
    suspected = total("suspected_malaria")
    tested = total("tested_for_malaria")
    confirmed = total("confirmed_malaria")

    clinical = LiveClinicalMapping.from_config(mapping)
    extent_start = tracker_extent_start or period_start
    lab_events = list(events)
    evidence = adapt_live_events(
        lab_events,
        clinical,
        namespace=namespace,
        visit_events=visit_events,
        medicine_events=medicine_events,
        context_retrieved=context_retrieved,
        context_missing_facilities=frozenset(context_failed),
    )
    raw_events = [*lab_events, *visit_events, *medicine_events]
    tracker_reporting_facilities = {
        event.organisation_unit_remote_id
        for event in raw_events
        if period_start <= event.occurred_at.date() <= period_end
    }
    latest_tracker_update: datetime | None = max(
        (event.updated_at for event in raw_events if event.updated_at is not None), default=None
    )
    # Each test is counted by its own date, never by the date of its visit.
    period_tests = [
        test
        for encounter in evidence.encounters
        for test in encounter.tests
        if (test_day := test.observed_on or encounter.encounter_date) is not None
        and period_start <= test_day <= period_end
    ]
    malaria_lab_events = len(period_tests)
    positive_malaria_events = sum(1 for t in period_tests if t.outcome is LabOutcome.POSITIVE)
    unmapped_malaria_results = sum(1 for t in period_tests if t.outcome in UNRESOLVED_OUTCOMES)
    if unmapped_malaria_results:
        warnings = [
            *warnings,
            f"{unmapped_malaria_results} malaria-test events have missing or unmapped "
            "results; recurrence counts may be incomplete",
        ]
    if context_failed:
        warnings = [
            *warnings,
            f"Visit or medicine records were unavailable for {len(context_failed)} authorised "
            "facilities; treatment evidence there is reported as not returned",
        ]

    recurrence = evaluate_live_recurrence(
        evidence,
        definition=SNAPSHOT_DEFINITION,
        period_start=period_start,
        period_end=period_end,
        extent_start=extent_start,
        facility_names=facility_names,
        tracker_failed=tracker_failed,
        coverage_stages=frozenset({LAB, VISIT, MEDICINE} if context_retrieved else {LAB}),
        display_key=display_key,
        alias=_patient_alias,
    )
    if retain is not None:
        retain((evidence, recurrence.coverage))
    positive_patient_rows = recurrence.positive_rows
    patient_rows = recurrence.repeat_rows
    indeterminate = recurrence.indeterminate
    duplicate_positive_groups = recurrence.duplicate_positive_groups

    primary_uids = {
        elements[name]
        for name in (
            "new_attendance",
            "reattendance",
            "suspected_malaria",
            "tested_for_malaria",
            "confirmed_malaria",
        )
    }
    aggregate_reporting = {
        item.organisation_unit_remote_id
        for item in values
        if item.data_element_remote_id in primary_uids and _reported_count(item.value) is not None
    }
    facilities_payload: list[dict[str, Any]] = []
    for uid, name in sorted(facility_names.items(), key=lambda item: item[1].casefold()):
        item_values = facility_values.get(uid, {})
        facilities_payload.append(
            {
                "uid": uid,
                "name": name,
                "confirmed_malaria": item_values.get(elements["confirmed_malaria"]),
                "tested_for_malaria": item_values.get(elements["tested_for_malaria"]),
                "rdt_days_out_of_stock": item_values.get(elements["rdt_days_out_of_stock"]),
                "al_days_out_of_stock": item_values.get(elements["al_days_out_of_stock"]),
                "artesunate_days_out_of_stock": item_values.get(
                    elements["artesunate_days_out_of_stock"]
                ),
                "aggregate_reported": uid in aggregate_reporting,
                "tracker_reported": uid in tracker_reporting_facilities,
            }
        )

    # Coordinates are carried by the already-sanitized metadata facility records.
    facilities_by_uid = {
        str(item["id"]): item
        for item in mapping.get("_runtime_facilities", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    for facility_payload in facilities_payload:
        metadata = facilities_by_uid.get(facility_payload["uid"], {})
        facility_payload["latitude"] = _optional_float(metadata.get("latitude"))
        facility_payload["longitude"] = _optional_float(metadata.get("longitude"))
        facility_payload["parent_remote_id"] = (
            str(metadata.get("parent_id")) if metadata.get("parent_id") else None
        )
        facility_payload["ancestor_names"] = [
            str(name) for name in metadata.get("ancestor_names", []) if isinstance(name, str)
        ]

    trend = _trend_points(elements, trend_values or unique_values)
    operational_alerts = _operational_alerts(
        facilities_payload,
        suspected,
        tested,
        confirmed,
        scope_name=str(mapping.get("_runtime_scope_name") or "Authorised scope"),
    )

    source_moments = [moment for moment in (latest_source_update, latest_tracker_update) if moment]
    source_updated_at = max(source_moments) if source_moments else None
    synchronized_at = datetime.now(tz=UTC)
    has_aggregate = bool(unique_values)
    has_tracker = bool(events)
    status = (
        "synchronized"
        if has_aggregate and has_tracker and not warnings and not tracker_failed
        else "partial"
        if has_aggregate or has_tracker
        else "unavailable"
    )
    return {
        "status": status,
        "scope": str(mapping.get("_runtime_scope_name") or "Authorised scope"),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "synchronized_at": synchronized_at.isoformat(),
        "source_updated_at": source_updated_at.isoformat() if source_updated_at else None,
        "facility_count": len(facility_names),
        "aggregate_reporting_facility_count": len(aggregate_reporting),
        "tracker_reporting_facility_count": len(tracker_reporting_facilities),
        "tracker_failed_facility_count": len(tracker_failed),
        "aggregate_value_count": len(unique_values),
        "tracker_event_count": len(events),
        "malaria_lab_event_count": malaria_lab_events,
        "positive_malaria_event_count": positive_malaria_events,
        "unique_positive_patient_count": len(positive_patient_rows),
        "invalid_aggregate_value_count": invalid_value_count,
        "kpis": [
            _kpi("ENC_ATTENDANCE_TOTAL", "Patient encounters", encounters, "HMIS 105:01"),
            _kpi("ENC_SUSPECTED_MALARIA", "Suspected malaria", suspected, "HMIS 105:01"),
            _kpi("ENC_TESTED_MALARIA", "Tested for malaria", tested, "HMIS 105:01"),
            _kpi("ENC_CONFIRMED_MALARIA", "Confirmed malaria", confirmed, "HMIS 105:01"),
            _ratio_kpi("TESTING_RATE", "Testing rate", tested, suspected, "HMIS 105:01"),
            _ratio_kpi("POSITIVITY_RATE", "Positivity rate", confirmed, tested, "HMIS 105:01"),
            _kpi(
                "ENC_REPEAT_POSITIVE_INPUT",
                "Repeat-positive patients",
                len(patient_rows)
                if has_tracker
                and not tracker_failed
                and recurrence.coverage_status == "complete"
                and not indeterminate
                else None,
                "eRegisters Tracker",
            ),
        ],
        "commodity_alerts": {
            "rdt_stock_out_facilities": _facilities_above_zero(
                facility_values, elements["rdt_days_out_of_stock"]
            ),
            "al_stock_out_facilities": _facilities_above_zero(
                facility_values, elements["al_days_out_of_stock"]
            ),
            "artesunate_stock_out_facilities": _facilities_above_zero(
                facility_values, elements["artesunate_days_out_of_stock"]
            ),
        },
        "facilities": facilities_payload,
        "trend": trend,
        "operational_alerts": operational_alerts,
        # Every qualifying patient, not a truncated sample: the overview reads
        # this list's length, and a cap here silently understated the count.
        "repeat_positive_patients": patient_rows,
        "positive_patients": positive_patient_rows,
        "repeat_positive_definition": {
            "name": SNAPSHOT_DEFINITION.name,
            "minimum_gap_days": SNAPSHOT_DEFINITION.minimum_gap_days,
            "maximum_window_days": SNAPSHOT_DEFINITION.maximum_window_days,
            "minimum_positive_encounters": SNAPSHOT_DEFINITION.minimum_positive_encounters,
            "same_day_policy": SNAPSHOT_DEFINITION.same_day_policy.value,
            "engine_version": SNAPSHOT_DEFINITION.engine_version,
            "exploratory": True,
        },
        "repeat_positive_coverage": recurrence.coverage_status,
        "repeat_positive_coverage_notes": list(recurrence.coverage_notes),
        "repeat_positive_denominator": recurrence.denominator,
        "repeat_positive_indeterminate": indeterminate,
        "possible_duplicate_positive_groups": duplicate_positive_groups,
        "tracker_lookback_start": extent_start.isoformat(),
        "interpretation_limit": INTERPRETATION_LIMIT,
        "warnings": warnings,
        "synthetic_data_used": False,
    }


def _kpi(code: str, label: str, value: int | None, source: str) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "value": f"{value:,}" if value is not None else None,
        "numerator": value,
        "denominator": None,
        "unit": "count",
        "source": source,
        "status": "available" if value is not None else "unavailable",
    }


def _ratio_kpi(
    code: str,
    label: str,
    numerator: int | None,
    denominator: int | None,
    source: str,
) -> dict[str, Any]:
    valid = (
        numerator is not None
        and denominator is not None
        and denominator > 0
        and 0 <= numerator <= denominator
    )
    value = None
    if valid and numerator is not None and denominator is not None:
        value = f"{(100 * numerator / denominator):.1f}%"
    return {
        "code": code,
        "label": label,
        "value": value,
        "numerator": numerator if valid else None,
        "denominator": denominator if valid else None,
        "unit": "percent",
        "source": source,
        "status": "available" if valid else "unavailable",
    }


def _reported_count(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        return None
    return int(value)


def _parse_source_moment(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _normalise_option(value: str | None) -> str:
    return (value or "").replace("\u2013", "-").replace("\u2014", "-").strip().casefold()


def _patient_alias(key: bytes, tracked_entity_uid: str) -> str:
    digest = hmac.new(
        key, b"mars-live-patient-v1\0" + tracked_entity_uid.encode(), hashlib.sha256
    ).digest()
    token = base64.b32encode(digest[:13]).decode("ascii").rstrip("=")
    return f"MARS-PT-{token}"


def _facilities_above_zero(values: Mapping[str, Mapping[str, int]], element_uid: str) -> int:
    return sum(1 for item in values.values() if item.get(element_uid, 0) > 0)


def _deduplicate_values(values: Sequence[RemoteDataValue]) -> list[RemoteDataValue]:
    """Collapse an exact DHIS2 data-value coordinate before summing it."""
    unique: dict[tuple[str, str, str, str | None, str | None], RemoteDataValue] = {}
    for item in values:
        key = (
            item.data_element_remote_id,
            item.organisation_unit_remote_id,
            item.period,
            item.category_option_combo_remote_id,
            item.attribute_option_combo_remote_id,
        )
        previous = unique.get(key)
        if previous is None or (item.last_updated or "") >= (previous.last_updated or ""):
            unique[key] = item
    return list(unique.values())


def _month_start(day: date, *, months_before: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months_before
    return date(month_index // 12, month_index % 12 + 1, 1)


def _trend_points(
    elements: Mapping[str, str], values: Sequence[RemoteDataValue]
) -> list[dict[str, Any]]:
    by_period: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in _deduplicate_values(values):
        if len(item.period) < 6 or not item.period[:6].isdigit():
            continue
        parsed = _reported_count(item.value)
        if parsed is not None:
            by_period[item.period[:6]][item.data_element_remote_id] += parsed

    result: list[dict[str, Any]] = []
    for period, counts in sorted(by_period.items()):
        new_attendance = counts.get(elements["new_attendance"])
        reattendance = counts.get(elements["reattendance"])
        encounters = (
            (new_attendance or 0) + (reattendance or 0)
            if new_attendance is not None or reattendance is not None
            else None
        )
        suspected = counts.get(elements["suspected_malaria"])
        tested = counts.get(elements["tested_for_malaria"])
        confirmed = counts.get(elements["confirmed_malaria"])
        positivity = (
            round(100 * confirmed / tested, 1)
            if confirmed is not None
            and tested is not None
            and tested > 0
            and 0 <= confirmed <= tested
            else None
        )
        result.append(
            {
                "period": period,
                "encounters": encounters,
                "suspected_malaria": suspected,
                "tested_for_malaria": tested,
                "confirmed_malaria": confirmed,
                "positivity_rate": positivity,
            }
        )
    return result


def _operational_alerts(
    facilities: Sequence[Mapping[str, Any]],
    suspected: int | None,
    tested: int | None,
    confirmed: int | None,
    *,
    scope_name: str = "Authorised scope",
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    commodity_fields = (
        ("rdt_days_out_of_stock", "RDT stock-out"),
        ("al_days_out_of_stock", "AL stock-out"),
        ("artesunate_days_out_of_stock", "Artesunate stock-out"),
    )
    for facility in facilities:
        for field, title in commodity_fields:
            value = facility.get(field)
            if isinstance(value, int) and value > 0:
                alerts.append(
                    {
                        "id": f"{facility['uid']}:{field}",
                        "kind": "commodity",
                        "title": title,
                        "facility_uid": facility["uid"],
                        "facility_name": facility["name"],
                        "status": "action_required",
                        "detail": f"{value} reported day{'s' if value != 1 else ''} out of stock",
                    }
                )
    if tested is not None and suspected is not None and tested > suspected:
        alerts.append(
            {
                "id": "district:tested-exceeds-suspected",
                "kind": "data_quality",
                "title": "Testing denominator requires review",
                "facility_uid": None,
                "facility_name": scope_name,
                "status": "review",
                "detail": (
                    f"Reported tests ({tested:,}) exceed suspected-malaria reports "
                    f"({suspected:,}); no testing rate is published."
                ),
            }
        )
    if confirmed is not None and suspected is not None and confirmed > suspected:
        alerts.append(
            {
                "id": "district:confirmed-exceeds-suspected",
                "kind": "data_quality",
                "title": "Malaria counts require reconciliation",
                "facility_uid": None,
                "facility_name": scope_name,
                "status": "review",
                "detail": (
                    f"Reported confirmed malaria ({confirmed:,}) exceeds suspected-malaria "
                    f"reports ({suspected:,}) for the selected source fields."
                ),
            }
        )
    return alerts


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


__all__ = ["build_live_dashboard_runner"]
