"""GET-only DHIS2 client for metadata discovery.

This client is intentionally *not* the exchange adapter. It has no methods that
retrieve tracked entities, enrollments, events, relationships, analytics or
aggregate data values. Those collections are classified as
``not_probed_to_protect_patient_data`` without a request being issued.

Every request is:

* GET;
* HTTPS;
* same-origin to the configured host;
* on an allowlisted route;
* with allowlisted query keys;
* projected to a compact ``fields=`` list;
* size-capped while streaming;
* never followed across a redirect.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from mars.core.logging import get_logger
from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.discovery.allowlists import (
    ALLOWED_ROUTES,
    CATEGORY_COMBO_FIELDS,
    CURRENT_USER_FIELDS,
    DATA_ELEMENT_FIELDS,
    DATASET_FIELDS,
    OPTION_SET_FIELDS,
    ORGANISATION_UNIT_FIELDS,
    PAGER_KEYS,
    PATIENT_COLLECTION_PATHS,
    PROGRAM_STAGE_FIELDS,
    PROGRAMME_FIELDS,
    RESOURCES_FIELDS,
    RESPONSE_KEYS,
    SYSTEM_INFO_FIELDS,
    TRACKED_ENTITY_ATTRIBUTE_FIELDS,
    TRACKED_ENTITY_TYPE_FIELDS,
)
from mars.integrations.dhis2.discovery.config import DiscoveryConfig

logger = get_logger(__name__)

DISCOVERY_CLIENT_VERSION = "1.0.0"

_COLLECTION_FIELDS: dict[str, str] = {
    "/api/resources": RESOURCES_FIELDS,
    "/api/organisationUnits": ORGANISATION_UNIT_FIELDS,
    "/api/programs": PROGRAMME_FIELDS,
    "/api/programStages": PROGRAM_STAGE_FIELDS,
    "/api/trackedEntityTypes": TRACKED_ENTITY_TYPE_FIELDS,
    "/api/trackedEntityAttributes": TRACKED_ENTITY_ATTRIBUTE_FIELDS,
    "/api/dataElements": DATA_ELEMENT_FIELDS,
    "/api/optionSets": OPTION_SET_FIELDS,
    "/api/dataSets": DATASET_FIELDS,
    "/api/categoryCombos": CATEGORY_COMBO_FIELDS,
}

_COLLECTION_KEYS: dict[str, str] = {
    "/api/resources": "resources",
    "/api/organisationUnits": "organisationUnits",
    "/api/programs": "programs",
    "/api/programStages": "programStages",
    "/api/trackedEntityTypes": "trackedEntityTypes",
    "/api/trackedEntityAttributes": "trackedEntityAttributes",
    "/api/dataElements": "dataElements",
    "/api/optionSets": "optionSets",
    "/api/dataSets": "dataSets",
    "/api/categoryCombos": "categoryCombos",
}

# Defence in depth: a DHIS2 instance or intermediary that ignores ``fields=``
# must still be unable to place unexpected profile or record-shaped properties
# into the sanitized report.
_SAFE_METADATA_KEYS = frozenset(
    {
        "id",
        "name",
        "code",
        "username",
        "authorities",
        "organisationUnits",
        "dataViewOrganisationUnits",
        "teiSearchOrganisationUnits",
        "level",
        "path",
        "leaf",
        "openingDate",
        "closedDate",
        "parent",
        "geometry",
        "type",
        "coordinates",
        "organisationUnitGroups",
        "programType",
        "trackedEntityType",
        "trackedEntityTypeAttributes",
        "trackedEntityAttribute",
        "programStages",
        "program",
        "programStageDataElements",
        "dataElement",
        "valueType",
        "unique",
        "confidential",
        "domainType",
        "categoryCombo",
        "optionSet",
        "options",
        "periodType",
        "dataSetElements",
        "categories",
        "resources",
        "plural",
        "singular",
        "relativeApiEndpoint",
    }
)


class DiscoveryError(RuntimeError):
    """A discovery request failed, with a category and no remote body."""

    def __init__(
        self,
        category: IntegrationErrorCategory,
        message: str,
        *,
        status_code: int | None = None,
        capability: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.capability = capability

    @property
    def is_retryable(self) -> bool:
        return self.category in {
            IntegrationErrorCategory.TIMEOUT,
            IntegrationErrorCategory.TRANSPORT,
            IntegrationErrorCategory.RATE_LIMITED,
            IntegrationErrorCategory.REMOTE_SERVER_ERROR,
        }


class DiscoveryClient:
    """Talks to one DHIS2 instance for metadata only."""

    def __init__(
        self,
        config: DiscoveryConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
            follow_redirects=False,
            headers=self._headers(),
            event_hooks={"request": [self._guard_request]},
        )

    def __enter__(self) -> DiscoveryClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"MARS-DHIS2-Discovery/{DISCOVERY_CLIENT_VERSION}",
        }
        if self._config.token:
            headers["Authorization"] = f"ApiToken {self._config.token}"
        return headers

    def _guard_request(self, request: httpx.Request) -> None:
        """Refuse anything that is not a same-origin allowlisted GET.

        Runs as an httpx request hook so a future caller cannot bypass the
        allowlist by using the underlying client.
        """
        if request.method != "GET":
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery is GET-only; writes and other methods are refused.",
            )
        parsed = urlsplit(str(request.url))
        if parsed.scheme.lower() != "https":
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery is HTTPS-only.",
            )
        host = (parsed.hostname or "").lower()
        if host != self._config.origin_host or host not in self._config.allowed_hosts:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a request that left the configured origin.",
            )
        path = parsed.path.rstrip("/") or parsed.path
        if path.endswith("/") and path != "/":
            path = path.rstrip("/")
        if path in PATIENT_COLLECTION_PATHS or path.startswith("/api/tracker/"):
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "Patient-collection routes are not requested by metadata discovery.",
            )
        allowed_keys = ALLOWED_ROUTES.get(path)
        if allowed_keys is None:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a route that is not on the metadata allowlist.",
            )
        extra = {key for key in request.url.params if key not in allowed_keys}
        if extra:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a query parameter that is not allowlisted.",
            )

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        if path in PATIENT_COLLECTION_PATHS:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "Patient-collection routes are not requested by metadata discovery.",
            )
        allowed_keys = ALLOWED_ROUTES.get(path)
        if allowed_keys is None:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a route that is not on the metadata allowlist.",
            )
        extra = set(params) - allowed_keys
        if extra:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a query parameter that is not allowlisted.",
            )
        query = dict(params)

        auth: tuple[str, str] | None = None
        if not self._config.token and self._config.username and self._config.password:
            auth = (self._config.username, self._config.password)

        attempts = self._config.max_retries + 1
        last: DiscoveryError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._attempt(path, query, auth)
            except DiscoveryError as error:
                last = error
                if not error.is_retryable or attempt == attempts:
                    raise
                delay = self._config.retry_backoff_seconds * attempt
                logger.warning(
                    "dhis2_discovery_retry",
                    path=path,
                    attempt=attempt,
                    category=error.category.value,
                    delay_seconds=delay,
                )
                self._sleep(delay)
        raise last or DiscoveryError(
            IntegrationErrorCategory.TRANSPORT,
            "discovery request failed with no recorded cause",
        )

    def _attempt(
        self,
        path: str,
        params: dict[str, str],
        auth: tuple[str, str] | None,
    ) -> dict[str, Any]:
        try:
            with self._client.stream("GET", path, params=params, auth=auth) as response:
                if 300 <= response.status_code < 400:
                    raise DiscoveryError(
                        IntegrationErrorCategory.TRANSPORT,
                        "DHIS2 issued a redirect; discovery does not follow redirects.",
                        status_code=response.status_code,
                    )
                self._raise_for_status(response)
                body = self._read_capped(response)
        except DiscoveryError:
            raise
        except httpx.TimeoutException as exc:
            raise DiscoveryError(
                IntegrationErrorCategory.TIMEOUT,
                f"DHIS2 did not respond within {self._config.timeout_seconds}s",
            ) from exc
        except httpx.TransportError as exc:
            raise DiscoveryError(
                IntegrationErrorCategory.TRANSPORT,
                "could not reach the configured DHIS2 origin",
            ) from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise DiscoveryError(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 returned a body that is not JSON",
            ) from exc
        if path in {"/api/me/authorization", "/api/me/authorities"} and isinstance(payload, list):
            payload = {"authorities": [item for item in payload if isinstance(item, str)]}
        if not isinstance(payload, dict):
            raise DiscoveryError(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 returned JSON that is not an object",
            )
        return _project_payload(path, payload)

    def _read_capped(self, response: httpx.Response) -> bytes:
        limit = self._config.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise DiscoveryError(
                    IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                    f"DHIS2 response exceeded the {limit} byte discovery cap",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _raise_for_status(self, response: httpx.Response) -> None:
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
        raise DiscoveryError(category, f"DHIS2 returned HTTP {status}", status_code=status)

    def system_info(self) -> dict[str, Any]:
        return self._get("/api/system/info", {"fields": SYSTEM_INFO_FIELDS})

    def current_user(self) -> dict[str, Any]:
        return self._get("/api/me", {"fields": CURRENT_USER_FIELDS})

    def current_user_authorization(self) -> dict[str, Any]:
        return self._get("/api/me/authorization", {})

    def current_user_authorities_legacy(self) -> dict[str, Any]:
        """Probe the legacy spelling without treating its absence as failure."""
        return self._get("/api/me/authorities", {})

    def iter_collection(self, path: str) -> Iterator[dict[str, Any]]:
        """Page an allowlisted collection, stopping at ``max_pages``."""
        collection_key = _COLLECTION_KEYS.get(path)
        fields = _COLLECTION_FIELDS.get(path)
        if collection_key is None or fields is None:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a route that is not on the metadata allowlist.",
            )
        for page_number in range(1, self._config.max_pages + 1):
            payload = self._get(
                path,
                {
                    "fields": fields,
                    "paging": "true",
                    "pageSize": str(self._config.page_size),
                    "page": str(page_number),
                },
            )
            items = payload.get(collection_key) or []
            if not isinstance(items, list):
                raise DiscoveryError(
                    IntegrationErrorCategory.MALFORMED_RESPONSE,
                    "DHIS2 returned a collection that is not a list",
                )
            for item in items:
                if isinstance(item, dict):
                    yield item
            pager_raw = payload.get("pager")
            pager: dict[str, Any] = pager_raw if isinstance(pager_raw, dict) else {}
            page_count = int(pager.get("pageCount") or page_number)
            if page_number >= page_count:
                return
        logger.warning("dhis2_discovery_truncated", path=path, max_pages=self._config.max_pages)

    def collect(self, path: str) -> tuple[list[dict[str, Any]], bool]:
        """Return all pages and whether the page cap truncated the result."""
        records: list[dict[str, Any]] = []
        pages_seen = 0
        collection_key = _COLLECTION_KEYS.get(path)
        fields = _COLLECTION_FIELDS.get(path)
        if collection_key is None or fields is None:
            raise DiscoveryError(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "DHIS2 discovery refused a route that is not on the metadata allowlist.",
            )
        truncated = False
        for page_number in range(1, self._config.max_pages + 1):
            payload = self._get(
                path,
                {
                    "fields": fields,
                    "paging": "true",
                    "pageSize": str(self._config.page_size),
                    "page": str(page_number),
                },
            )
            pages_seen = page_number
            items = payload.get(collection_key) or []
            if isinstance(items, list):
                records.extend(item for item in items if isinstance(item, dict))
            pager_raw = payload.get("pager")
            pager: dict[str, Any] = pager_raw if isinstance(pager_raw, dict) else {}
            page_count = int(pager.get("pageCount") or page_number)
            if page_number >= page_count:
                truncated = False
                break
        else:
            truncated = True
        if truncated:
            logger.warning(
                "dhis2_discovery_truncated",
                path=path,
                pages=pages_seen,
                max_pages=self._config.max_pages,
            )
        return records, truncated


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
            projected[key] = _sanitize_metadata_value(value)
    return projected


def _sanitize_metadata_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_metadata_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_metadata_value(item)
            for key, item in value.items()
            if key in _SAFE_METADATA_KEYS
        }
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return None


__all__ = ["DISCOVERY_CLIENT_VERSION", "DiscoveryClient", "DiscoveryError"]
