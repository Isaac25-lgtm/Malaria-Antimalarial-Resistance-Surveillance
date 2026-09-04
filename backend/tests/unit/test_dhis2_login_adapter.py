"""Login-time DHIS2 client: metadata only, no patient collections, no leaks."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.discovery.allowlists import PATIENT_COLLECTION_PATHS
from mars.integrations.dhis2.login.client import LoginClient
from mars.integrations.dhis2.login.errors import LoginAdapterError

SECRET_PASSWORD = "login-sentinel-password-MUST-NOT-ESCAPE"
SECRET_USERNAME = "pilot.reader"


def _client(transport: httpx.BaseTransport) -> LoginClient:
    return LoginClient(
        base_url="https://dhis2.example.org",
        username=SECRET_USERNAME,
        password=SECRET_PASSWORD,
        timeout_seconds=2.0,
        max_retries=0,
        retry_backoff_seconds=0.0,
        transport=transport,
        sleep=lambda _s: None,
    )


def _payloads() -> dict[str, Any]:
    ou = {
        "id": "DisPaderUid1",
        "name": "Pader District",
        "code": "312",
        "level": 3,
        "path": "/RootUid/RegUid/DisPaderUid1",
    }
    return {
        "/api/system/info": {"version": "2.40", "systemName": "eRegisters"},
        "/api/me": {
            "id": "UserUid0001",
            "username": "district.officer",
            "displayName": "District Officer",
            "authorities": ["F_METADATA_IMPORT"],
            "organisationUnits": [ou],
            "dataViewOrganisationUnits": [ou],
            "teiSearchOrganisationUnits": [],
        },
        "/api/me/authorization": {"authorities": ["F_METADATA_IMPORT"]},
        "/api/organisationUnitLevels": {
            "organisationUnitLevels": [
                {"id": "lvl1", "name": "Country", "level": 1},
                {"id": "lvl2", "name": "Region", "level": 2},
                {"id": "lvl3", "name": "District", "level": 3},
                {"id": "lvl4", "name": "Facility", "level": 4},
            ],
            "pager": {"page": 1, "pageCount": 1, "pageSize": 200, "total": 4},
        },
        "/api/organisationUnitGroups": {
            "organisationUnitGroups": [{"id": "g1", "name": "Districts", "code": "DIST"}],
            "pager": {"page": 1, "pageCount": 1, "pageSize": 200, "total": 1},
        },
        "/api/organisationUnitGroupSets": {
            "organisationUnitGroupSets": [],
            "pager": {"page": 1, "pageCount": 1, "pageSize": 200, "total": 0},
        },
    }


def _router(responses: dict[str, Any] | None = None, *, status: dict[str, int] | None = None):
    seen: list[httpx.Request] = []
    payloads = responses or _payloads()
    codes = status or {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = urlsplit(str(request.url)).path.rstrip("/")
        if path in PATIENT_COLLECTION_PATHS or path.startswith("/api/tracker"):
            return httpx.Response(500, json={"message": "patient path must not be requested"})
        if path in codes:
            return httpx.Response(codes[path], json={"message": "denied"})
        if path in payloads:
            body = payloads[path]
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": "missing stub"})

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


class TestLoginMetadataRoundTrip:
    def test_successful_authentication_requests_only_login_metadata(self) -> None:
        transport = _router()
        with _client(transport) as client:
            snapshot = client.fetch_login_metadata()
        paths = [urlsplit(str(item.url)).path.rstrip("/") for item in transport.seen]
        assert snapshot.username == "district.officer"
        assert snapshot.remote_user_id == "UserUid0001"
        assert snapshot.organisation_units[0].uid == "DisPaderUid1"
        assert "/api/system/info" in paths
        assert "/api/me" in paths
        assert "/api/me/authorization" in paths
        assert "/api/organisationUnitLevels" in paths
        assert "/api/organisationUnitGroups" in paths
        assert "/api/organisationUnitGroupSets" in paths
        for path in paths:
            assert path not in PATIENT_COLLECTION_PATHS
            assert not path.startswith("/api/tracker")
            assert path not in {
                "/api/trackedEntityInstances",
                "/api/enrollments",
                "/api/events",
                "/api/relationships",
                "/api/dataValueSets",
                "/api/analytics",
            }

    def test_invalid_credentials(self) -> None:
        transport = _router(status={"/api/system/info": 401})
        with _client(transport) as client, pytest.raises(LoginAdapterError) as raised:
            client.fetch_login_metadata()
        assert raised.value.is_invalid_credentials

    def test_timeout(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow")

        with (
            _client(httpx.MockTransport(handler)) as client,
            pytest.raises(LoginAdapterError) as raised,
        ):
            client.fetch_login_metadata()
        assert raised.value.category is IntegrationErrorCategory.TIMEOUT

    def test_transport_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        with (
            _client(httpx.MockTransport(handler)) as client,
            pytest.raises(LoginAdapterError) as raised,
        ):
            client.fetch_login_metadata()
        assert raised.value.is_unavailable

    def test_redirect_to_a_different_origin_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.example/phish"})

        with (
            _client(httpx.MockTransport(handler)) as client,
            pytest.raises(LoginAdapterError) as raised,
        ):
            client.fetch_login_metadata()
        assert raised.value.category is IntegrationErrorCategory.TRANSPORT
        assert "different origin" in str(raised.value)

    def test_malformed_response(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        with (
            _client(httpx.MockTransport(handler)) as client,
            pytest.raises(LoginAdapterError) as raised,
        ):
            client.fetch_login_metadata()
        assert raised.value.category is IntegrationErrorCategory.MALFORMED_RESPONSE

    def test_http_scheme_is_refused_before_a_request(self) -> None:
        with pytest.raises(LoginAdapterError):
            LoginClient(
                base_url="http://dhis2.example.org",
                username=SECRET_USERNAME,
                password=SECRET_PASSWORD,
            )

    def test_tls_verification_cannot_be_disabled(self) -> None:
        with pytest.raises(LoginAdapterError):
            LoginClient(
                base_url="https://dhis2.example.org",
                username=SECRET_USERNAME,
                password=SECRET_PASSWORD,
                verify_tls=False,
            )

    def test_patient_paths_are_refused_without_http(self) -> None:
        transport = _router()
        with _client(transport) as client:
            with pytest.raises(LoginAdapterError):
                client._get("/api/tracker/trackedEntities", {})  # type: ignore[attr-defined]
            with pytest.raises(LoginAdapterError):
                client._get("/api/events", {})  # type: ignore[attr-defined]
        paths = [urlsplit(str(item.url)).path for item in transport.seen]
        assert paths == []

    def test_credentials_are_not_in_repr_or_serialised_snapshot(self) -> None:
        transport = _router()
        with _client(transport) as client:
            snapshot = client.fetch_login_metadata()
            dumped = json.dumps(
                {
                    "username": snapshot.username,
                    "remote_user_id": snapshot.remote_user_id,
                    "requested_paths": snapshot.requested_paths,
                }
            )
        assert SECRET_PASSWORD not in dumped
        assert SECRET_PASSWORD not in repr(snapshot)
        assert not hasattr(snapshot, "password")
