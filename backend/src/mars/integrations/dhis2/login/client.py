"""GET-only DHIS2 client used at MARS live login.

Transport rules match discovery: HTTPS, verified TLS, host allowlist, GET
only, no cross-origin redirects, capped bodies, projected fields. The route
allowlist is the login subset, not the discovery catalogue.

Patient-collection paths are refused before a socket is opened.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from mars.core.logging import get_logger
from mars.core.urls import strip_url_credentials
from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.login.allowlists import (
    ALLOWED_HOSTS,
    ALLOWED_ROUTES,
    LOGIN_USER_FIELDS,
    ORGANISATION_UNIT_GROUP_FIELDS,
    ORGANISATION_UNIT_GROUP_SET_FIELDS,
    ORGANISATION_UNIT_LEVEL_FIELDS,
    PAGER_KEYS,
    PATIENT_COLLECTION_PATHS,
    RESPONSE_KEYS,
    SAFE_METADATA_KEYS,
    SYSTEM_INFO_FIELDS,
)
from mars.integrations.dhis2.login.errors import LoginAdapterError
from mars.integrations.dhis2.login.models import (
    LoginSnapshot,
    RemoteOrgUnit,
    RemoteOrgUnitGroup,
    RemoteOrgUnitLevel,
)

logger = get_logger(__name__)

LOGIN_CLIENT_VERSION = "1.0.0"


class LoginClient:
    """Talks to one DHIS2 instance for login metadata only."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
        max_response_bytes: int = 2 * 1024 * 1024,
        verify_tls: bool = True,
        page_size: int = 200,
        max_pages: int = 10,
        allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not verify_tls:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login refuses to disable TLS verification",
            )
        stripped = strip_url_credentials(base_url) or ""
        parts = urlsplit(base_url)
        if parts.username or parts.password:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "the login base URL must not contain userinfo",
            )
        if parts.scheme.lower() != "https":
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login is HTTPS-only",
            )
        host = (parts.hostname or "").lower()
        if host not in allowed_hosts:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "the login hostname is not on the Ministry allowlist",
            )
        self._base_url = stripped
        self._origin_host = host
        self._allowed_hosts = allowed_hosts
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_response_bytes = max_response_bytes
        self._page_size = page_size
        self._max_pages = max_pages
        self._username = username
        self._password = password
        self._sleep = sleep or (lambda _seconds: None)
        self._requested_paths: list[str] = []
        self._client = httpx.Client(
            base_url=stripped,
            timeout=timeout_seconds,
            verify=verify_tls,
            transport=transport,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": f"MARS-DHIS2-Login/{LOGIN_CLIENT_VERSION}",
            },
            event_hooks={"request": [self._guard_request]},
        )

    def __enter__(self) -> LoginClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._username = ""
        self._password = ""

    @property
    def requested_paths(self) -> tuple[str, ...]:
        return tuple(self._requested_paths)

    def _guard_request(self, request: httpx.Request) -> None:
        if request.method != "GET":
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login is GET-only",
            )
        parsed = urlsplit(str(request.url))
        if parsed.scheme.lower() != "https":
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login is HTTPS-only",
            )
        host = (parsed.hostname or "").lower()
        if host != self._origin_host or host not in self._allowed_hosts:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login refused a request that left the configured origin",
            )
        path = _normalise_path(parsed.path)
        if path in PATIENT_COLLECTION_PATHS or path.startswith("/api/tracker"):
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "patient-collection routes are not requested at login",
                requested_path=path,
            )
        allowed_keys = ALLOWED_ROUTES.get(path)
        if allowed_keys is None:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login refused a route that is not on the login allowlist",
                requested_path=path,
            )
        extra = {key for key in request.url.params if key not in allowed_keys}
        if extra:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login refused a query parameter that is not allowlisted",
                requested_path=path,
            )

    def fetch_login_metadata(self) -> LoginSnapshot:
        """Identity, authorities and organisation-unit metadata. Nothing else."""
        system_info = self._get("/api/system/info", {"fields": SYSTEM_INFO_FIELDS})
        me = self._get("/api/me", {"fields": LOGIN_USER_FIELDS})
        authorization = self._get("/api/me/authorization", {})
        levels = self._collect("/api/organisationUnitLevels", "organisationUnitLevels")
        groups = self._collect("/api/organisationUnitGroups", "organisationUnitGroups")
        group_sets = self._collect("/api/organisationUnitGroupSets", "organisationUnitGroupSets")
        remote_id = str(me.get("id") or "").strip()
        username = str(me.get("username") or "").strip()
        if not remote_id or not username:
            raise LoginAdapterError(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 current-user metadata was missing identity fields",
            )
        authorities = _string_tuple(authorization.get("authorities"))
        if not authorities:
            authorities = _string_tuple(me.get("authorities"))
        display_name = str(me.get("displayName") or username).strip() or username
        snapshot = LoginSnapshot(
            remote_user_id=remote_id,
            username=username,
            display_name=display_name,
            authorities=authorities,
            organisation_units=_org_units(me.get("organisationUnits")),
            data_view_organisation_units=_org_units(me.get("dataViewOrganisationUnits")),
            tei_search_organisation_units=_org_units(me.get("teiSearchOrganisationUnits")),
            organisation_unit_levels=_levels(levels),
            organisation_unit_groups=_groups(groups, group_sets),
            system_name=_optional_str(system_info.get("systemName")),
            system_version=_optional_str(system_info.get("version")),
            requested_paths=self.requested_paths,
        )
        return snapshot

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        if path in PATIENT_COLLECTION_PATHS or path.startswith("/api/tracker"):
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "patient-collection routes are not requested at login",
                requested_path=path,
            )
        allowed_keys = ALLOWED_ROUTES.get(path)
        if allowed_keys is None:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login refused a route that is not on the login allowlist",
                requested_path=path,
            )
        extra = set(params) - allowed_keys
        if extra:
            raise LoginAdapterError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "live DHIS2 login refused a query parameter that is not allowlisted",
                requested_path=path,
            )
        attempts = self._max_retries + 1
        last: LoginAdapterError | None = None
        for attempt in range(1, attempts + 1):
            try:
                payload = self._attempt(path, dict(params))
                self._requested_paths.append(path)
                return payload
            except LoginAdapterError as error:
                last = error
                if error.is_invalid_credentials or attempt == attempts:
                    raise
                if error.category not in {
                    IntegrationErrorCategory.TIMEOUT,
                    IntegrationErrorCategory.TRANSPORT,
                    IntegrationErrorCategory.REMOTE_SERVER_ERROR,
                    IntegrationErrorCategory.RATE_LIMITED,
                }:
                    raise
                self._sleep(self._retry_backoff_seconds * attempt)
        raise last or LoginAdapterError(
            IntegrationErrorCategory.TRANSPORT,
            "login metadata request failed with no recorded cause",
            requested_path=path,
        )

    def _attempt(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        auth = (self._username, self._password)
        try:
            with self._client.stream("GET", path, params=params, auth=auth) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "")
                    if _redirect_leaves_origin(location, self._origin_host):
                        raise LoginAdapterError(
                            IntegrationErrorCategory.TRANSPORT,
                            "DHIS2 issued a redirect to a different origin",
                            status_code=response.status_code,
                            requested_path=path,
                        )
                    raise LoginAdapterError(
                        IntegrationErrorCategory.TRANSPORT,
                        "DHIS2 issued a redirect; live login does not follow redirects",
                        status_code=response.status_code,
                        requested_path=path,
                    )
                self._raise_for_status(response, path)
                body = self._read_capped(response)
        except LoginAdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise LoginAdapterError(
                IntegrationErrorCategory.TIMEOUT,
                "DHIS2 did not respond in time",
                requested_path=path,
            ) from exc
        except httpx.TransportError as exc:
            raise LoginAdapterError(
                IntegrationErrorCategory.TRANSPORT,
                "could not reach the configured DHIS2 origin",
                requested_path=path,
            ) from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise LoginAdapterError(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 returned a body that is not JSON",
                requested_path=path,
            ) from exc
        if path in {"/api/me/authorization", "/api/me/authorities"} and isinstance(payload, list):
            payload = {"authorities": [item for item in payload if isinstance(item, str)]}
        if not isinstance(payload, dict):
            raise LoginAdapterError(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 returned JSON that is not an object",
                requested_path=path,
            )
        return _project_payload(path, payload)

    def _read_capped(self, response: httpx.Response) -> bytes:
        limit = self._max_response_bytes
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise LoginAdapterError(
                    IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                    "DHIS2 response exceeded the login size cap",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        status = response.status_code
        if status < 400:
            return
        category = {
            401: IntegrationErrorCategory.AUTHENTICATION,
            403: IntegrationErrorCategory.AUTHORISATION,
            404: IntegrationErrorCategory.NOT_FOUND,
            429: IntegrationErrorCategory.RATE_LIMITED,
        }.get(status)
        if category is None:
            category = (
                IntegrationErrorCategory.REMOTE_SERVER_ERROR
                if status >= 500
                else IntegrationErrorCategory.TRANSPORT
            )
        raise LoginAdapterError(
            category,
            f"DHIS2 returned HTTP {status}",
            status_code=status,
            requested_path=path,
        )

    def _collect(self, path: str, collection_key: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        fields = {
            "/api/organisationUnitLevels": ORGANISATION_UNIT_LEVEL_FIELDS,
            "/api/organisationUnitGroups": ORGANISATION_UNIT_GROUP_FIELDS,
            "/api/organisationUnitGroupSets": ORGANISATION_UNIT_GROUP_SET_FIELDS,
        }[path]
        for page_number in range(1, self._max_pages + 1):
            payload = self._get(
                path,
                {
                    "fields": fields,
                    "paging": "true",
                    "pageSize": str(self._page_size),
                    "page": str(page_number),
                },
            )
            items = payload.get(collection_key) or []
            if isinstance(items, list):
                records.extend(item for item in items if isinstance(item, dict))
            pager_raw = payload.get("pager")
            pager: dict[str, Any] = pager_raw if isinstance(pager_raw, dict) else {}
            page_count = int(pager.get("pageCount") or page_number)
            if page_number >= page_count:
                return records
        logger.warning("dhis2_login_collection_truncated", path=path)
        return records


def _normalise_path(path: str) -> str:
    if path.endswith("/") and path != "/":
        return path.rstrip("/")
    return path or "/"


def _redirect_leaves_origin(location: str, origin_host: str) -> bool:
    if not location:
        return False
    parsed = urlsplit(location)
    if not parsed.hostname:
        return False
    return parsed.hostname.lower() != origin_host


def _project_payload(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = RESPONSE_KEYS.get(path)
    if allowed is None:
        return {}
    projected: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "pager" and isinstance(value, dict):
            projected[key] = {k: v for k, v in value.items() if k in PAGER_KEYS}
        else:
            projected[key] = _sanitize(value)
    return projected


def _sanitize(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items() if key in SAFE_METADATA_KEYS}
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return None


def _org_units(raw: Any) -> tuple[RemoteOrgUnit, ...]:
    if not isinstance(raw, list):
        return ()
    units: list[RemoteOrgUnit] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("id") or "").strip()
        if not uid:
            continue
        level_raw = item.get("level")
        level = int(level_raw) if isinstance(level_raw, int) else None
        groups = item.get("organisationUnitGroups")
        group_ids: list[str] = []
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict) and group.get("id"):
                    group_ids.append(str(group["id"]))
        units.append(
            RemoteOrgUnit(
                uid=uid,
                name=_optional_str(item.get("name")),
                code=_optional_str(item.get("code")),
                level=level,
                path=_optional_str(item.get("path")),
                group_ids=tuple(group_ids),
            )
        )
    return tuple(units)


def _levels(raw: list[dict[str, Any]]) -> tuple[RemoteOrgUnitLevel, ...]:
    levels: list[RemoteOrgUnitLevel] = []
    for item in raw:
        number_raw = item.get("level")
        name = _optional_str(item.get("name"))
        if not isinstance(number_raw, int) or not name:
            continue
        levels.append(
            RemoteOrgUnitLevel(
                number=number_raw,
                name=name,
                uid=_optional_str(item.get("id")),
            )
        )
    return tuple(levels)


def _groups(
    groups: list[dict[str, Any]], group_sets: list[dict[str, Any]]
) -> tuple[RemoteOrgUnitGroup, ...]:
    seen: dict[str, RemoteOrgUnitGroup] = {}
    for item in (*groups, *_flatten_group_sets(group_sets)):
        uid = str(item.get("id") or "").strip()
        if not uid or uid in seen:
            continue
        members_raw = item.get("organisationUnitGroups")
        member_ids: tuple[str, ...] = ()
        if isinstance(members_raw, list):
            member_ids = tuple(
                str(member["id"])
                for member in members_raw
                if isinstance(member, dict) and member.get("id")
            )
        seen[uid] = RemoteOrgUnitGroup(
            uid=uid,
            name=_optional_str(item.get("name")),
            code=_optional_str(item.get("code")),
            member_ids=member_ids,
        )
    return tuple(seen.values())


def _flatten_group_sets(group_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for item in group_sets:
        nested = item.get("organisationUnitGroups")
        if isinstance(nested, list):
            flattened.extend(group for group in nested if isinstance(group, dict))
    return flattened


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, str) and item)


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["LOGIN_CLIENT_VERSION", "LoginClient"]
