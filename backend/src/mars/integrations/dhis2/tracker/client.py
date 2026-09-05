"""Read-only, hard-bounded DHIS2 Tracker event adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.client import Dhis2Error
from mars.integrations.ports import RemoteEvent, RemotePage, RemoteScope

_ALLOWED_HOSTS = frozenset({"eregisters.health.go.ug"})
_EVENT_PATH = "/api/tracker/events"
_EVENT_FIELDS = (
    "event,trackedEntity,program,programStage,orgUnit,status,occurredAt,updatedAt,"
    "dataValues[dataElement,value,updatedAt]"
)


@dataclass(frozen=True, slots=True)
class TrackerClientConfig:
    base_url: str
    username: str | None = None
    password: str | None = None
    token: str | None = None
    timeout_seconds: float = 30.0
    page_size: int = 50
    max_records: int = 200
    max_window_days: int = 14
    max_response_bytes: int = 4 * 1024 * 1024
    allowed_hosts: frozenset[str] = _ALLOWED_HOSTS

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in self.allowed_hosts
        ):
            raise ValueError("Tracker access requires the allowlisted eRegisters HTTPS origin")
        if parsed.username or parsed.password:
            raise ValueError("Tracker credentials must not be embedded in the base URL")
        if not 1 <= self.page_size <= 100:
            raise ValueError("Tracker page_size must be between 1 and 100")
        if not 1 <= self.max_records <= 50_000:
            raise ValueError("Tracker max_records must be between 1 and 50000")
        if not 1 <= self.max_window_days <= 366:
            raise ValueError("Tracker max_window_days must be between 1 and 366")

    def __repr__(self) -> str:
        return (
            f"TrackerClientConfig(base_url={self.base_url!r}, "
            f"auth={'token' if self.token else 'basic' if self.username else 'none'})"
        )

    @property
    def origin_host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()


class BoundedTrackerEventClient:
    """Fetch modern Tracker events without ever issuing an unbounded request."""

    source_system = "dhis2_tracker"

    def __init__(
        self,
        config: TrackerClientConfig,
        *,
        authorized_org_unit_uids: frozenset[str],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._authorized_units = authorized_org_unit_uids
        headers = {"Accept": "application/json", "User-Agent": "MARS-Tracker/1.0.0"}
        if config.token:
            headers["Authorization"] = f"ApiToken {config.token}"
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers=headers,
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [self._guard_request]},
        )

    def __enter__(self) -> BoundedTrackerEventClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _guard_request(self, request: httpx.Request) -> None:
        parsed = urlsplit(str(request.url))
        if request.method != "GET" or parsed.scheme.lower() != "https":
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE, "Tracker access is GET-only"
            )
        if (parsed.hostname or "").lower() != self._config.origin_host:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "Tracker access refused a request outside the configured origin",
            )
        if parsed.path.rstrip("/") != _EVENT_PATH:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "Tracker event access refused a non-event route",
            )

    def fetch_events(self, scope: RemoteScope, cursor: str | None = None) -> RemotePage:
        org_unit, programme, stage = self._validate_scope(scope)
        page = _page_number(cursor)
        max_pages = (
            self._config.max_records + self._config.page_size - 1
        ) // self._config.page_size
        if page > max_pages:
            raise Dhis2Error(
                IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                "Tracker pagination exceeded the approved bounded row limit",
            )
        params = {
            "program": programme,
            "programStage": stage,
            "orgUnit": org_unit,
            "ouMode": "SELECTED",
            "occurredAfter": scope.period_start.isoformat(),  # type: ignore[union-attr]
            "occurredBefore": scope.period_end.isoformat(),  # type: ignore[union-attr]
            "page": str(page),
            "pageSize": str(self._config.page_size),
            "totalPages": "true",
            "order": "updatedAt:asc,event:asc",
            "fields": _EVENT_FIELDS,
        }
        updated_after = scope.extra.get("updated_after")
        if updated_after:
            params["updatedAfter"] = updated_after
        payload = self._request(params)
        raw_events = payload.get("instances", payload.get("events", []))
        if not isinstance(raw_events, list):
            raise Dhis2Error(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 Tracker returned an event collection that is not a list",
            )
        records = tuple(_remote_event(item) for item in raw_events if isinstance(item, dict))
        pager_raw = payload.get("pager")
        pager = pager_raw if isinstance(pager_raw, dict) else {}
        page_count = _positive_int(pager.get("pageCount"))
        total = _positive_int(pager.get("total"))
        if total is not None and total > self._config.max_records:
            raise Dhis2Error(
                IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                "Tracker query exceeds the approved bounded row limit; "
                "narrow the facility or dates",
            )
        has_more = (
            page < page_count if page_count is not None else len(records) == self._config.page_size
        )
        if has_more and page >= max_pages:
            raise Dhis2Error(
                IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                "Tracker query reached the approved bounded row limit before completion",
            )
        return RemotePage(
            records=records,
            next_cursor=str(page + 1) if has_more else None,
            total_declared=total,
            page_description=f"Tracker event page {page}",
        )

    def _validate_scope(self, scope: RemoteScope) -> tuple[str, str, str]:
        if len(scope.organisation_unit_remote_ids) != 1 or scope.include_descendants:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "The controlled Tracker pull requires exactly one facility and no descendants",
            )
        org_unit = scope.organisation_unit_remote_ids[0]
        if org_unit not in self._authorized_units:
            raise Dhis2Error(
                IntegrationErrorCategory.AUTHORISATION,
                "The requested facility is outside the authenticated Tracker-search scope",
            )
        if scope.period_start is None or scope.period_end is None:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "The controlled Tracker pull requires explicit start and end dates",
            )
        days = (scope.period_end - scope.period_start).days + 1
        if days < 1 or days > self._config.max_window_days:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "The Tracker window exceeds the configured bounded date range",
            )
        programme = scope.extra.get("programme_uid", "")
        stage = scope.extra.get("program_stage_uid", "")
        if not programme or not stage:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "An approved programme and program stage are required",
            )
        updated_after = scope.extra.get("updated_after")
        if updated_after:
            try:
                datetime.fromisoformat(updated_after.replace("Z", "+00:00"))
            except ValueError as exc:
                raise Dhis2Error(
                    IntegrationErrorCategory.MAPPING_INCOMPLETE,
                    "updated_after must be an ISO-8601 timestamp",
                ) from exc
        return org_unit, programme, stage

    def _request(self, params: dict[str, str]) -> dict[str, Any]:
        auth = None
        if not self._config.token and self._config.username and self._config.password:
            auth = (self._config.username, self._config.password)
        try:
            with self._client.stream("GET", _EVENT_PATH, params=params, auth=auth) as response:
                if 300 <= response.status_code < 400:
                    raise Dhis2Error(
                        IntegrationErrorCategory.TRANSPORT,
                        "DHIS2 issued a redirect; Tracker access does not follow redirects",
                    )
                _raise_status(response)
                body = _read_capped(response, self._config.max_response_bytes)
        except Dhis2Error:
            raise
        except httpx.TimeoutException as exc:
            raise Dhis2Error(IntegrationErrorCategory.TIMEOUT, "DHIS2 Tracker timed out") from exc
        except httpx.TransportError as exc:
            raise Dhis2Error(
                IntegrationErrorCategory.TRANSPORT,
                "Could not reach the configured DHIS2 Tracker origin",
            ) from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise Dhis2Error(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 Tracker returned a body that is not JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise Dhis2Error(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 Tracker returned JSON that is not an object",
            )
        return payload


def _remote_event(raw: dict[str, Any]) -> RemoteEvent:
    required = ("event", "trackedEntity", "program", "programStage", "orgUnit", "occurredAt")
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
        raise Dhis2Error(
            IntegrationErrorCategory.MALFORMED_RESPONSE,
            "DHIS2 Tracker returned an event without required references or timing",
        )
    try:
        occurred_at = _timestamp(raw["occurredAt"])
        updated_at = _timestamp(raw["updatedAt"]) if raw.get("updatedAt") else None
    except ValueError as exc:
        raise Dhis2Error(
            IntegrationErrorCategory.MALFORMED_RESPONSE,
            "DHIS2 Tracker returned an event with an invalid timestamp",
        ) from exc
    values: dict[str, str | None] = {}
    data_values = raw.get("dataValues") or []
    if not isinstance(data_values, list):
        raise Dhis2Error(
            IntegrationErrorCategory.MALFORMED_RESPONSE,
            "DHIS2 Tracker returned dataValues that are not a list",
        )
    for item in data_values:
        if not isinstance(item, dict) or not isinstance(item.get("dataElement"), str):
            continue
        value = item.get("value")
        values[item["dataElement"]] = str(value) if value is not None else None
    return RemoteEvent(
        remote_id=raw["event"],
        person_remote_id=raw["trackedEntity"],
        programme_remote_id=raw["program"],
        programme_stage_remote_id=raw["programStage"],
        organisation_unit_remote_id=raw["orgUnit"],
        occurred_at=occurred_at,
        updated_at=updated_at,
        status=str(raw["status"]) if raw.get("status") is not None else None,
        data_values=values,
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _page_number(cursor: str | None) -> int:
    if cursor is None:
        return 1
    try:
        page = int(cursor)
    except ValueError as exc:
        raise Dhis2Error(
            IntegrationErrorCategory.MALFORMED_RESPONSE, "Invalid Tracker cursor"
        ) from exc
    if page < 1:
        raise Dhis2Error(IntegrationErrorCategory.MALFORMED_RESPONSE, "Invalid Tracker cursor")
    return page


def _positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _read_capped(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise Dhis2Error(
                IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                "DHIS2 Tracker response exceeded the configured size cap",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    category = {
        401: IntegrationErrorCategory.AUTHENTICATION,
        403: IntegrationErrorCategory.AUTHORISATION,
        404: IntegrationErrorCategory.NOT_FOUND,
        429: IntegrationErrorCategory.RATE_LIMITED,
    }.get(response.status_code)
    if category is None:
        category = (
            IntegrationErrorCategory.REMOTE_SERVER_ERROR
            if response.status_code >= 500
            else IntegrationErrorCategory.TRANSPORT
        )
    raise Dhis2Error(category, f"DHIS2 returned HTTP {response.status_code}")


__all__ = ["BoundedTrackerEventClient", "TrackerClientConfig"]
