"""Read-only live Pader dashboard assembly from approved DHIS2 metadata.

This module is application wiring, not a surveillance rule engine. It retrieves
reported HMIS values and the minimum Tracker event envelope needed to count
repeat-positive patients. It never requests tracked-entity attributes and never
returns a DHIS2 tracked-entity UID to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from mars.core.settings import Settings
from mars.integrations.dhis2.client import Dhis2Client, Dhis2Config
from mars.integrations.dhis2.tracker.client import BoundedTrackerEventClient, TrackerClientConfig
from mars.integrations.ports import RemoteDataValue, RemoteEvent, RemoteScope, iterate_pages


def build_live_dashboard_runner(
    settings: Settings, *, project_root: Path
) -> Callable[[str, str, Sequence[Mapping[str, Any]], date, date], dict[str, Any]]:
    mapping_path = project_root / "config" / "dhis2" / "pader-live-v1.json"

    def run(
        username: str,
        password: str,
        facilities: Sequence[Mapping[str, Any]],
        period_start: date,
        period_end: date,
    ) -> dict[str, Any]:
        mapping = _load_mapping(mapping_path)
        mapping["_runtime_facilities"] = [dict(item) for item in facilities]
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
        try:
            with Dhis2Client(config) as client:
                for page in iterate_pages(
                    lambda cursor: client.fetch_data_values(aggregate_scope, cursor),
                    max_pages=10,
                ):
                    aggregate_values.extend(
                        value for value in page.records if isinstance(value, RemoteDataValue)
                    )
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
                for page in iterate_pages(
                    lambda cursor: client.fetch_data_values(trend_scope, cursor),
                    max_pages=10,
                ):
                    trend_values.extend(
                        value for value in page.records if isinstance(value, RemoteDataValue)
                    )
        except Exception as error:
            warnings.append(f"Aggregate HMIS request unavailable ({type(error).__name__})")

        tracker = mapping["tracker"]
        lab_stage = tracker["stages"]["laboratory_tests"]
        tracker_events: list[RemoteEvent] = []
        tracker_failed: list[str] = []
        tracker_config = TrackerClientConfig(
            base_url=settings.dhis2_login_base_url,
            username=username,
            password=password,
            timeout_seconds=max(settings.dhis2_login_timeout_seconds, 30.0),
            page_size=100,
            max_records=10_000,
            max_window_days=62,
            max_response_bytes=8 * 1024 * 1024,
        )
        with BoundedTrackerEventClient(
            tracker_config,
            authorized_org_unit_uids=frozenset(facility_uids),
        ) as client:
            for facility_uid in facility_uids:
                scope = RemoteScope(
                    organisation_unit_remote_ids=(facility_uid,),
                    period_start=period_start,
                    period_end=period_end,
                    extra={
                        "programme_uid": mapping["programme_uid"],
                        "program_stage_uid": lab_stage,
                    },
                )
                try:
                    for page in iterate_pages(
                        lambda cursor, scope=scope: client.fetch_events(scope, cursor),
                        max_pages=100,
                    ):
                        tracker_events.extend(
                            event for event in page.records if isinstance(event, RemoteEvent)
                        )
                except Exception:
                    tracker_failed.append(facility_uid)
        if tracker_failed:
            warnings.append(
                f"Tracker events were unavailable for {len(tracker_failed)} of "
                f"{len(facility_uids)} authorised facilities"
            )

        display_key = settings.patient_display_key
        if display_key is None:
            raise RuntimeError("MARS_PATIENT_DISPLAY_KEY is required for live patient aliases")
        return _assemble(
            mapping,
            aggregate_values,
            tracker_events,
            facility_names,
            period_start,
            period_end,
            display_key.get_secret_value().encode("utf-8"),
            warnings,
            tracker_failed,
            trend_values,
        )

    return run


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "2.0" or raw.get("status") != "approved":
        raise RuntimeError("The Pader live mapping is absent or not approved")
    required = ("programme_uid", "datasets", "aggregate_data_elements", "tracker")
    if any(not raw.get(key) for key in required):
        raise RuntimeError("The Pader live mapping is incomplete")
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

    tracker_elements: Mapping[str, str] = mapping["tracker"]["data_elements"]
    tracker_options: Mapping[str, Any] = mapping["tracker"]["options"]
    test_uid = tracker_elements["laboratory_test_type"]
    result_uid = tracker_elements["laboratory_result"]
    malaria_tests = {_normalise_option(item) for item in tracker_options["test_types_malaria"]}
    positive_results = {
        _normalise_option(item) for item in tracker_options["positive_malaria_results"]
    }
    positives_by_person: dict[str, list[RemoteEvent]] = defaultdict(list)
    tracker_reporting_facilities: set[str] = set()
    latest_tracker_update: datetime | None = None
    malaria_lab_events = 0
    positive_malaria_events = 0
    for event in events:
        tracker_reporting_facilities.add(event.organisation_unit_remote_id)
        if event.updated_at and (
            latest_tracker_update is None or event.updated_at > latest_tracker_update
        ):
            latest_tracker_update = event.updated_at
        if _normalise_option(event.data_values.get(test_uid)) not in malaria_tests:
            continue
        malaria_lab_events += 1
        if _normalise_option(event.data_values.get(result_uid)) in positive_results:
            positive_malaria_events += 1
            positives_by_person[event.person_remote_id].append(event)

    patient_rows: list[dict[str, Any]] = []
    repeat_positive_count = 0
    for person_uid, positive_events in positives_by_person.items():
        positive_events.sort(key=lambda event: (event.occurred_at, event.remote_id))
        if len(positive_events) < 2:
            continue
        repeat_positive_count += 1
        first = positive_events[0]
        latest = positive_events[-1]
        patient_rows.append(
            {
                "mars_patient_id": _patient_alias(display_key, person_uid),
                "first_positive_on": first.occurred_at.date().isoformat(),
                "latest_positive_on": latest.occurred_at.date().isoformat(),
                "positive_encounter_count": len(positive_events),
                "interval_days": (latest.occurred_at.date() - first.occurred_at.date()).days,
                "facility_name": facility_names.get(
                    latest.organisation_unit_remote_id, "Authorised facility"
                ),
                "cross_facility": len(
                    {event.organisation_unit_remote_id for event in positive_events}
                )
                > 1,
            }
        )
    patient_rows.sort(
        key=lambda row: (row["latest_positive_on"], row["positive_encounter_count"]),
        reverse=True,
    )

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

    trend = _trend_points(elements, trend_values or unique_values)
    operational_alerts = _operational_alerts(facilities_payload, suspected, tested, confirmed)

    source_moments = [moment for moment in (latest_source_update, latest_tracker_update) if moment]
    source_updated_at = max(source_moments) if source_moments else None
    synchronized_at = datetime.now(tz=UTC)
    has_aggregate = bool(unique_values)
    has_tracker = bool(events)
    status = (
        "synchronized"
        if has_aggregate and has_tracker
        else "partial"
        if has_aggregate or has_tracker
        else "unavailable"
    )
    return {
        "status": status,
        "scope": "Pader District",
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
        "unique_positive_patient_count": len(positives_by_person),
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
                repeat_positive_count if has_tracker else None,
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
        "repeat_positive_patients": patient_rows[:25],
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
            if confirmed is not None and tested is not None and 0 < confirmed <= tested
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
                "facility_name": "Pader District",
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
                "facility_name": "Pader District",
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
