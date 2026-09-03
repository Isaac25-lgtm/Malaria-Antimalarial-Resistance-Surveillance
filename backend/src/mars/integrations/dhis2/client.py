"""The DHIS2 HTTP adapter.

The only module in MARS that knows DHIS2 exists. It speaks DHIS2's Web API and
returns the port types from :mod:`mars.integrations.ports`; everything upstream
sees MARS's own vocabulary.

What this module is careful about, and why each one is here rather than left to
a caller who will forget:

**Credentials come from settings and go nowhere else.** They are held as
``SecretStr``, attached to a request header, and never written to a log, an
exception message, a run record or a repr. The base URL is stripped of any
userinfo before it is stored or reported - a URL is a place credentials hide.

**Every failure has a category.** An operator's next action after a 401 is
completely different from their next action after a 503, and a single
``IntegrationError`` would leave them re-running a request that can never
succeed.

**A response is size-capped before it is parsed.** A DHIS2 analytics query with
a careless dimension returns hundreds of megabytes; parsing it kills the worker,
and a worker that dies takes every other queued job with it.

**Retries are bounded and only for the failures that can succeed later.** A
timeout is worth retrying. A 403 is not, and retrying it just means being
locked out three times as fast.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from mars.core.logging import get_logger
from mars.core.settings import Settings
from mars.core.urls import strip_url_credentials
from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.ports import (
    RemoteDataElement,
    RemoteDataValue,
    RemoteOrganisationUnit,
    RemotePage,
    RemoteScope,
)

logger = get_logger(__name__)

#: Bumped when a change here could alter what a given remote payload produces.
ADAPTER_VERSION = "1.0.0"

SYSTEM_NAME = "dhis2"


class Dhis2Error(RuntimeError):
    """A DHIS2 exchange failed, with a category that says what to do next.

    The message is composed by MARS. The remote body is deliberately **not**
    included: a DHIS2 error response can quote the request that produced it,
    and that request carries an ``Authorization`` header.
    """

    def __init__(
        self,
        category: IntegrationErrorCategory,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code

    @property
    def is_retryable(self) -> bool:
        return self.category in {
            IntegrationErrorCategory.TIMEOUT,
            IntegrationErrorCategory.TRANSPORT,
            IntegrationErrorCategory.RATE_LIMITED,
            IntegrationErrorCategory.REMOTE_SERVER_ERROR,
        }


@dataclass(frozen=True, slots=True)
class Dhis2Config:
    """Everything the client needs, resolved from settings.

    Built through :meth:`from_settings` so the "is it configured" question is
    answered once, in one place, rather than by each caller checking a
    different subset of the fields.
    """

    base_url: str
    username: str | None
    password: str | None
    token: str | None
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    page_size: int
    max_response_bytes: int
    verify_tls: bool

    def __repr__(self) -> str:
        """Never render the credentials.

        A config object reaches tracebacks, and a traceback reaches logs.
        """
        return (
            f"Dhis2Config(base_url={self.base_url!r}, "
            f"auth={'token' if self.token else 'basic' if self.username else 'none'}, "
            f"verify_tls={self.verify_tls!r})"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Dhis2Config | None:
        """The config, or ``None`` when the deployment has not been given one.

        ``None`` rather than an exception: an unconfigured integration is an
        ordinary state for a MARS deployment that does not exchange with DHIS2,
        and it should be reported as such rather than raised at import time.
        """
        if not settings.dhis2_enabled or not settings.dhis2_base_url:
            return None

        return cls(
            base_url=strip_credentials(settings.dhis2_base_url),
            username=settings.dhis2_username,
            password=(
                settings.dhis2_password.get_secret_value() if settings.dhis2_password else None
            ),
            token=(settings.dhis2_token.get_secret_value() if settings.dhis2_token else None),
            timeout_seconds=settings.dhis2_timeout_seconds,
            max_retries=settings.dhis2_max_retries,
            retry_backoff_seconds=settings.dhis2_retry_backoff_seconds,
            page_size=settings.dhis2_page_size,
            max_response_bytes=settings.dhis2_max_response_bytes,
            verify_tls=settings.dhis2_verify_tls,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.token or (self.username and self.password))


def strip_credentials(url: str) -> str:
    """Remove any userinfo from a URL.

    Delegates to :func:`mars.core.urls.strip_url_credentials`, which lives
    outside the adapter because the status service needs the same rule and may
    not import an adapter (ADR 0003).
    """
    return strip_url_credentials(url) or ""


class Dhis2Client:
    """Talks to one DHIS2 instance."""

    source_system = SYSTEM_NAME

    def __init__(
        self,
        config: Dhis2Config,
        *,
        transport: httpx.BaseTransport | None = None,
        correlation_id: str = "",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._correlation_id = correlation_id
        self._sleep = sleep
        # The transport seam is what lets the contract tests run against a
        # scripted DHIS2 without a live server. Nothing else about the request
        # path changes, so the tests exercise the real retry, pagination and
        # size-cap code rather than a simplified stand-in.
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
            headers=self._headers(),
        )

    def __enter__(self) -> Dhis2Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"MARS/{ADAPTER_VERSION}",
        }
        if self._correlation_id:
            # Correlates this run's requests in the DHIS2 access log as well as
            # in ours, which is what makes a remote operator able to help.
            headers["X-Correlation-Id"] = self._correlation_id
        if self._config.token:
            headers["Authorization"] = f"ApiToken {self._config.token}"
        return headers

    # -- Transport ---------------------------------------------------------
    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One GET, with bounded retries and a size cap.

        Retries only the categories that can succeed later. The backoff is
        linear rather than exponential because DHIS2 deployments are commonly
        behind a proxy with a short-lived queue, and an exponential wait turns a
        two-second blip into a two-minute stall.
        """
        auth: tuple[str, str] | None = None
        if not self._config.token and self._config.username and self._config.password:
            auth = (self._config.username, self._config.password)

        attempts = self._config.max_retries + 1
        last: Dhis2Error | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._attempt(path, params, auth)
            except Dhis2Error as error:
                last = error
                if not error.is_retryable or attempt == attempts:
                    raise
                delay = self._config.retry_backoff_seconds * attempt
                logger.warning(
                    "dhis2_request_retry",
                    path=path,
                    attempt=attempt,
                    category=error.category.value,
                    delay_seconds=delay,
                    correlation_id=self._correlation_id,
                )
                self._sleep(delay)

        raise (
            last
            if last
            else Dhis2Error(  # pragma: no cover - loop always raises or returns
                IntegrationErrorCategory.TRANSPORT, "request failed with no recorded cause"
            )
        )

    def _attempt(
        self, path: str, params: dict[str, Any], auth: tuple[str, str] | None
    ) -> dict[str, Any]:
        try:
            with self._client.stream("GET", path, params=params, auth=auth) as response:
                self._raise_for_status(response)
                body = self._read_capped(response)
        except httpx.TimeoutException as exc:
            raise Dhis2Error(
                IntegrationErrorCategory.TIMEOUT,
                f"DHIS2 did not respond within {self._config.timeout_seconds}s",
            ) from exc
        except httpx.TransportError as exc:
            # Deliberately does not include str(exc): a transport error can
            # carry the full request URL, and a URL is where credentials hide.
            raise Dhis2Error(
                IntegrationErrorCategory.TRANSPORT,
                f"could not reach DHIS2 at {self._config.base_url}",
            ) from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise Dhis2Error(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 returned a body that is not JSON",
            ) from exc

        if not isinstance(payload, dict):
            raise Dhis2Error(
                IntegrationErrorCategory.MALFORMED_RESPONSE,
                "DHIS2 returned JSON that is not an object",
            )
        return payload

    def _read_capped(self, response: httpx.Response) -> bytes:
        """Read the body, refusing anything over the configured cap.

        Streamed and counted rather than read whole then measured: measuring
        afterwards means the oversized body is already in memory, which is the
        thing being prevented.
        """
        limit = self._config.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise Dhis2Error(
                    IntegrationErrorCategory.RESPONSE_TOO_LARGE,
                    f"DHIS2 response exceeded the {limit} byte cap; narrow the "
                    "requested scope or raise MARS_DHIS2_MAX_RESPONSE_BYTES",
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

        # The status and MARS's own sentence. Never the body.
        raise Dhis2Error(category, f"DHIS2 returned HTTP {status}", status_code=status)

    # -- Metadata ports ----------------------------------------------------
    def fetch_organisation_units(self, cursor: str | None = None) -> RemotePage:
        payload = self._request(
            "/api/organisationUnits",
            {
                "fields": "id,name,code,level,openingDate,closedDate,parent[id],geometry",
                "paging": "true",
                "pageSize": self._config.page_size,
                "page": _page_number(cursor),
            },
        )
        units = tuple(_organisation_unit(entry) for entry in payload.get("organisationUnits") or [])
        return _page(units, payload, "organisationUnits")

    def fetch_data_elements(self, cursor: str | None = None) -> RemotePage:
        payload = self._request(
            "/api/dataElements",
            {
                "fields": "id,name,code,valueType,categoryCombo[id]",
                "paging": "true",
                "pageSize": self._config.page_size,
                "page": _page_number(cursor),
            },
        )
        elements = tuple(_data_element(entry) for entry in payload.get("dataElements") or [])
        return _page(elements, payload, "dataElements")

    def fetch_datasets(self, cursor: str | None = None) -> RemotePage:
        payload = self._request(
            "/api/dataSets",
            {
                "fields": "id,name,code,periodType",
                "paging": "true",
                "pageSize": self._config.page_size,
                "page": _page_number(cursor),
            },
        )
        datasets = tuple(_data_element(entry) for entry in payload.get("dataSets") or [])
        return _page(datasets, payload, "dataSets")

    # -- Aggregate data port -----------------------------------------------
    def fetch_data_values(self, scope: RemoteScope, cursor: str | None = None) -> RemotePage:
        """One page of ``dataValueSets``.

        DHIS2 does not page ``dataValueSets`` the way it pages metadata: the
        request is scoped by period and org unit and returns the whole set. The
        cursor is therefore over the *scope*, not the response - each call takes
        the next org-unit group - which keeps the port's contract identical
        while being honest about what the remote API actually does.
        """
        if not scope.organisation_unit_remote_ids:
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "no organisation unit was requested; MARS will not pull a whole "
                "DHIS2 instance implicitly",
            )
        if not (scope.period_start and scope.period_end):
            raise Dhis2Error(
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
                "a period range is required; an unbounded data value request "
                "cannot be resumed or fingerprinted",
            )

        index = _page_number(cursor) - 1
        groups = _chunk(scope.organisation_unit_remote_ids, self._config.page_size)
        if index >= len(groups):
            return RemotePage(records=(), next_cursor=None, page_description="exhausted")

        params: dict[str, Any] = {
            "orgUnit": list(groups[index]),
            "startDate": scope.period_start.isoformat(),
            "endDate": scope.period_end.isoformat(),
            "children": str(scope.include_descendants).lower(),
        }
        if scope.dataset_remote_ids:
            params["dataSet"] = list(scope.dataset_remote_ids)
        if scope.data_element_remote_ids:
            params["dataElement"] = list(scope.data_element_remote_ids)

        payload = self._request("/api/dataValueSets", params)
        values = tuple(_data_value(entry) for entry in payload.get("dataValues") or [])
        has_more = index + 1 < len(groups)
        return RemotePage(
            records=values,
            next_cursor=str(index + 2) if has_more else None,
            total_declared=None,
            page_description=f"org unit group {index + 1} of {len(groups)}",
        )


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
def _organisation_unit(entry: dict[str, Any]) -> RemoteOrganisationUnit:
    parent = entry.get("parent") or {}
    latitude, longitude = _point(entry.get("geometry"))
    return RemoteOrganisationUnit(
        remote_id=str(entry.get("id") or ""),
        name=str(entry.get("name") or ""),
        level=_optional_int(entry.get("level")),
        parent_remote_id=str(parent.get("id")) if parent.get("id") else None,
        code=entry.get("code") or None,
        latitude=latitude,
        longitude=longitude,
    )


def _point(geometry: Any) -> tuple[float | None, float | None]:
    """A point coordinate, or nothing.

    Only ``Point`` is read. A DHIS2 org unit can carry a polygon, and reducing
    a polygon to a centroid would manufacture a facility location that nobody
    surveyed - the opposite of MARS's rule that an unvalidated coordinate is
    stored as absent.
    """
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None, None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) != 2:
        return None, None
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None, None
    return latitude, longitude


def _data_element(entry: dict[str, Any]) -> RemoteDataElement:
    combo = entry.get("categoryCombo") or {}
    return RemoteDataElement(
        remote_id=str(entry.get("id") or ""),
        name=str(entry.get("name") or ""),
        code=entry.get("code") or None,
        value_type=entry.get("valueType") or entry.get("periodType") or None,
        category_combo_remote_id=str(combo.get("id")) if combo.get("id") else None,
    )


def _data_value(entry: dict[str, Any]) -> RemoteDataValue:
    """One reported value, kept as a string.

    DHIS2 sends every value as a string, including the empty one. Parsing here
    would decide - silently, at the seam - whether ``""`` is a blank or a zero,
    which is the single distinction MARS spends the most effort preserving. It
    stays a string until the canonical validator, which knows the rule.
    """
    return RemoteDataValue(
        data_element_remote_id=str(entry.get("dataElement") or ""),
        organisation_unit_remote_id=str(entry.get("orgUnit") or ""),
        period=str(entry.get("period") or ""),
        value=entry.get("value") if entry.get("value") is not None else None,
        category_option_combo_remote_id=entry.get("categoryOptionCombo") or None,
        attribute_option_combo_remote_id=entry.get("attributeOptionCombo") or None,
        stored_by=entry.get("storedBy") or None,
        last_updated=entry.get("lastUpdated") or None,
        comment=entry.get("comment") or None,
    )


def _page(records: tuple[Any, ...], payload: dict[str, Any], key: str) -> RemotePage:
    pager = (payload.get("pager") or {}) if isinstance(payload.get("pager"), dict) else {}
    page = _optional_int(pager.get("page")) or 1
    page_count = _optional_int(pager.get("pageCount"))
    total = _optional_int(pager.get("total"))
    has_more = page_count is not None and page < page_count
    return RemotePage(
        records=records,
        next_cursor=str(page + 1) if has_more else None,
        total_declared=total,
        page_description=f"{key} page {page}" + (f" of {page_count}" if page_count else ""),
    )


def _page_number(cursor: str | None) -> int:
    if not cursor:
        return 1
    try:
        value = int(cursor)
    except ValueError:
        return 1
    return max(value, 1)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chunk(values: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)] or [()]


__all__ = [
    "ADAPTER_VERSION",
    "SYSTEM_NAME",
    "Dhis2Client",
    "Dhis2Config",
    "Dhis2Error",
    "strip_credentials",
]
