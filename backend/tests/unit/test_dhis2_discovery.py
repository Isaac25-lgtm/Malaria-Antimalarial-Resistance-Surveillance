"""Safety and behaviour of metadata-only DHIS2 discovery.

Every test drives the real client through an ``httpx`` transport. Nothing
contacts a network. The assertions that matter are the ones that only bite in
production: HTTPS, same-origin, GET-only, allowlists, no patient collections,
no credential leakage, and a report that stops before retrieval.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mars.core.settings import Settings
from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.discovery.allowlists import (
    ALLOWED_ROUTES,
    PATIENT_COLLECTION_PATHS,
)
from mars.integrations.dhis2.discovery.classify import candidate_mappings, compact_unit
from mars.integrations.dhis2.discovery.client import DiscoveryClient, DiscoveryError
from mars.integrations.dhis2.discovery.config import DiscoveryConfig, DiscoveryConfigError
from mars.integrations.dhis2.discovery.models import CapabilityStatus
from mars.integrations.dhis2.discovery.render import render_markdown, write_reports
from mars.integrations.dhis2.discovery.service import run_discovery

SECRET_TOKEN = "d2pat-DISCOVERY-NEVER-IN-A-LOG"
SECRET_PASSWORD = "discovery-password-must-not-escape"

CLIENT_SOURCE = Path(inspect.getfile(DiscoveryClient)).read_text(encoding="utf-8")


def config(**overrides: Any) -> DiscoveryConfig:
    defaults: dict[str, Any] = {
        "base_url": "https://dhis2.example.org",
        "username": "mars_reader",
        "password": SECRET_PASSWORD,
        "token": None,
        "timeout_seconds": 5.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.0,
        "page_size": 2,
        "max_pages": 3,
        "max_response_bytes": 1_000_000,
        "verify_tls": True,
        "output_dir": "data/discovery",
    }
    defaults.update(overrides)
    return DiscoveryConfig.from_url(**defaults)


def recording(handler: Any) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def scripted(responses: list[httpx.Response]) -> httpx.MockTransport:
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return remaining.pop(0) if remaining else httpx.Response(500)

    return recording(handler)


def json_response(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class TestConfigurationIsStrict:
    def test_http_is_refused(self) -> None:
        with pytest.raises(DiscoveryConfigError, match="HTTPS-only"):
            DiscoveryConfig.from_url("http://hmis.health.go.ug")

    def test_userinfo_in_the_url_is_refused(self) -> None:
        with pytest.raises(DiscoveryConfigError, match="userinfo"):
            DiscoveryConfig.from_url("https://admin:secret@hmis.health.go.ug")

    def test_an_unknown_host_is_refused(self) -> None:
        with pytest.raises(DiscoveryConfigError, match="allowlist"):
            DiscoveryConfig.from_url("https://evil.example.net")

    def test_disabling_tls_verification_is_refused(self) -> None:
        with pytest.raises(DiscoveryConfigError, match="TLS"):
            DiscoveryConfig.from_url("https://dhis2.example.org", verify_tls=False)

    def test_ministry_hosts_are_accepted(self) -> None:
        for host in ("hmis.health.go.ug", "eregisters.health.go.ug"):
            resolved = DiscoveryConfig.from_url(f"https://{host}")
            assert resolved.origin_host == host

    def test_the_config_repr_does_not_render_secrets(self) -> None:
        rendered = repr(config(token=SECRET_TOKEN))
        assert SECRET_TOKEN not in rendered
        assert SECRET_PASSWORD not in rendered
        assert "auth=token" in rendered

    def test_from_settings_is_unconfigured_without_a_url(self) -> None:
        settings = Settings(database_url="postgresql+psycopg://mars:pw@db:5432/mars")
        assert DiscoveryConfig.from_settings(settings) is None


class TestTheClientHasNoPatientSurface:
    def test_public_methods_do_not_name_patient_collections(self) -> None:
        names = {name for name in dir(DiscoveryClient) if not name.startswith("_")}
        forbidden = {
            "fetch_tracked_entities",
            "fetch_enrollments",
            "fetch_events",
            "fetch_relationships",
            "fetch_analytics",
            "fetch_data_values",
            "post",
            "put",
            "patch",
            "delete",
        }
        assert names.isdisjoint(forbidden)

    @pytest.mark.parametrize(
        "path",
        sorted(PATIENT_COLLECTION_PATHS),
    )
    def test_patient_paths_are_not_issued_as_get_targets(self, path: str) -> None:
        assert f'_get("{path}"' not in CLIENT_SOURCE
        assert f'stream("GET", "{path}"' not in CLIENT_SOURCE

    def test_every_allowlisted_route_is_get_only(self) -> None:
        assert "/api/trackedEntities" not in ALLOWED_ROUTES
        assert "/api/events" not in ALLOWED_ROUTES
        assert "/api/analytics" not in ALLOWED_ROUTES


class TestTransportSafety:
    def test_a_non_get_is_refused_before_the_network(self) -> None:
        transport = scripted([])
        with (
            DiscoveryClient(config(), transport=transport) as client,
            pytest.raises(DiscoveryError, match="GET-only"),
        ):
            client._client.request("POST", "/api/me")
        assert transport.seen == []

    def test_an_unknown_route_is_refused_before_the_network(self) -> None:
        transport = scripted([])
        with (
            DiscoveryClient(config(), transport=transport) as client,
            pytest.raises(DiscoveryError, match="allowlist"),
        ):
            client._get("/api/users", {"fields": "id"})
        assert transport.seen == []

    def test_a_patient_collection_is_refused_before_the_network(self) -> None:
        transport = scripted([])
        with (
            DiscoveryClient(config(), transport=transport) as client,
            pytest.raises(DiscoveryError, match="Patient-collection"),
        ):
            client._get("/api/trackedEntities", {"fields": "id"})
        assert transport.seen == []

    def test_a_disallowed_query_key_is_refused(self) -> None:
        transport = scripted([])
        with (
            DiscoveryClient(config(), transport=transport) as client,
            pytest.raises(DiscoveryError, match="query parameter"),
        ):
            client._get("/api/me", {"fields": "id", "skipMeta": "true"})
        assert transport.seen == []

    def test_a_redirect_is_not_followed(self) -> None:
        transport = recording(
            lambda _request: httpx.Response(
                302,
                headers={"Location": "https://evil.example.net/api/trackedEntities"},
            )
        )
        with (
            DiscoveryClient(config(max_retries=0), transport=transport) as client,
            pytest.raises(DiscoveryError, match="redirect"),
        ):
            client.system_info()
        assert len(transport.seen) == 1
        assert transport.seen[0].url.host == "dhis2.example.org"

    def test_a_403_is_not_retried(self) -> None:
        transport = scripted([httpx.Response(403)] * 4)
        with (
            DiscoveryClient(config(max_retries=3), transport=transport) as client,
            pytest.raises(DiscoveryError) as raised,
        ):
            client.system_info()
        assert raised.value.category is IntegrationErrorCategory.AUTHORISATION
        assert len(transport.seen) == 1

    def test_an_oversized_response_is_refused_while_streaming(self) -> None:
        huge = json.dumps({"version": "x" * 5000})
        transport = scripted([httpx.Response(200, text=huge)])
        with (
            DiscoveryClient(
                config(max_response_bytes=64, max_retries=0), transport=transport
            ) as client,
            pytest.raises(DiscoveryError) as raised,
        ):
            client.system_info()
        assert raised.value.category is IntegrationErrorCategory.RESPONSE_TOO_LARGE

    def test_a_remote_error_body_is_not_repeated_back(self) -> None:
        body = {"message": f"failed for Authorization: ApiToken {SECRET_TOKEN}"}
        transport = scripted([httpx.Response(500, json=body)])
        with (
            DiscoveryClient(
                config(max_retries=0, token=SECRET_TOKEN), transport=transport
            ) as client,
            pytest.raises(DiscoveryError) as raised,
        ):
            client.system_info()
        assert SECRET_TOKEN not in str(raised.value)
        assert "HTTP 500" in str(raised.value)

    def test_response_keys_outside_the_allowlist_are_dropped(self) -> None:
        transport = scripted(
            [
                json_response(
                    {
                        "version": "2.41",
                        "email": "admin@example.org",
                        "password": SECRET_PASSWORD,
                    }
                )
            ]
        )
        with DiscoveryClient(config(), transport=transport) as client:
            info = client.system_info()
        assert info == {"version": "2.41"}
        assert "email" not in info
        assert "password" not in info

    def test_unexpected_nested_keys_are_dropped_even_if_fields_is_ignored(self) -> None:
        transport = scripted(
            [
                json_response(
                    {
                        "programs": [
                            {
                                "id": "P1",
                                "name": "OPD",
                                "patient_name": "must not survive",
                                "trackedEntityType": {
                                    "id": "T1",
                                    "name": "Person",
                                    "telephone": "must not survive",
                                },
                            }
                        ],
                        "pager": {"page": 1, "pageCount": 1},
                    }
                )
            ]
        )
        with DiscoveryClient(config(), transport=transport) as client:
            programmes, truncated = client.collect("/api/programs")
        assert truncated is False
        assert programmes == [
            {
                "id": "P1",
                "name": "OPD",
                "trackedEntityType": {"id": "T1", "name": "Person"},
            }
        ]


class TestPaginationBounds:
    def test_collection_paging_stops_at_max_pages(self) -> None:
        pages = [
            json_response(
                {
                    "organisationUnits": [{"id": f"U{page}", "name": f"Unit {page}"}],
                    "pager": {"page": page, "pageCount": 9, "pageSize": 1, "total": 9},
                }
            )
            for page in range(1, 6)
        ]
        transport = scripted(pages)
        with DiscoveryClient(config(max_pages=2, page_size=1), transport=transport) as client:
            records, truncated = client.collect("/api/organisationUnits")
        assert [item["id"] for item in records] == ["U1", "U2"]
        assert truncated is True
        assert len(transport.seen) == 2

    def test_collection_paging_stops_when_the_pager_says_so(self) -> None:
        transport = scripted(
            [
                json_response(
                    {
                        "dataElements": [{"id": "DE1", "name": "Tested"}],
                        "pager": {"page": 1, "pageCount": 1, "total": 1},
                    }
                )
            ]
        )
        with DiscoveryClient(config(), transport=transport) as client:
            records, truncated = client.collect("/api/dataElements")
        assert truncated is False
        assert records[0]["id"] == "DE1"


def _metadata_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/system/info":
        return json_response({"version": "2.41.1", "systemName": "Uganda HMIS"})
    if path == "/api/me":
        return json_response(
            {
                "id": "user1",
                "username": "mars_pader_reader",
                "email": "should-be-dropped@example.org",
                "organisationUnits": [
                    {
                        "id": "PAD1",
                        "name": "Pader",
                        "code": "PADER",
                        "level": 3,
                        "leaf": False,
                    }
                ],
                "dataViewOrganisationUnits": [{"id": "PAD1", "name": "Pader", "level": 3}],
                "teiSearchOrganisationUnits": [{"id": "PAD1", "name": "Pader", "level": 3}],
            }
        )
    if path == "/api/me/authorization":
        # Current DHIS2 commonly returns the authority set as a bare JSON list.
        return httpx.Response(200, json=["F_METADATA_EXPORT"])
    if path == "/api/me/authorities":
        return httpx.Response(404)
    collections = {
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
    key = collections.get(path)
    if key == "organisationUnits":
        return json_response(
            {
                key: [
                    {
                        "id": "PAD1",
                        "name": "Pader",
                        "code": "PADER",
                        "level": 3,
                        "leaf": False,
                    },
                    {
                        "id": "HF1",
                        "name": "Pader HC IV",
                        "level": 5,
                        "leaf": True,
                        "parent": {"id": "PAD1"},
                    },
                ],
                "pager": {"page": 1, "pageCount": 1},
            }
        )
    if key == "programs":
        return json_response(
            {
                key: [
                    {
                        "id": "PRG1",
                        "name": "HMIS OPD 002 Outpatient",
                        "code": "OPD002",
                        "programType": "WITHOUT_REGISTRATION",
                    }
                ],
                "pager": {"page": 1, "pageCount": 1},
            }
        )
    if key == "dataElements":
        return json_response(
            {
                key: [{"id": "DE1", "name": "Malaria RDT positive", "code": "MAL_RDT"}],
                "pager": {"page": 1, "pageCount": 1},
            }
        )
    if key:
        return json_response({key: [], "pager": {"page": 1, "pageCount": 1}})
    raise AssertionError(f"unexpected path {path}")


class TestDiscoveryRun:
    def test_a_full_run_never_requests_patient_collections(self) -> None:
        transport = recording(_metadata_handler)
        with DiscoveryClient(config(token=SECRET_TOKEN), transport=transport) as client:
            report = run_discovery(client, origin_host="dhis2.example.org")
        paths = [request.url.path for request in transport.seen]
        assert paths
        assert not any(
            path.rstrip("/") in PATIENT_COLLECTION_PATHS or path.startswith("/api/tracker/")
            for path in paths
        )
        assert all(request.method == "GET" for request in transport.seen)
        assert all(request.url.scheme == "https" for request in transport.seen)
        assert all(request.url.host == "dhis2.example.org" for request in transport.seen)
        patient = [
            record
            for record in report.capabilities
            if not record.probed
            and record.name
            in {
                "tracked_entity_instances",
                "enrollments",
                "events",
                "relationships",
                "tracker_tracked_entities",
                "tracker_enrollments",
                "tracker_events",
                "tracker_relationships",
                "tracked_entity_analytics_query",
                "enrollment_analytics_query",
                "event_analytics_query",
                "event_analytics_aggregate",
                "aggregate_data_values",
            }
        ]
        assert {record.name for record in patient} >= {
            "enrollments",
            "events",
            "relationships",
            "tracked_entity_analytics_query",
        }
        assert all(record.probed is False for record in patient)
        assert any(item.kind == "opd_programme" for item in report.candidate_mappings)
        assert any(item.kind == "malaria_variable" for item in report.candidate_mappings)
        assert report.pader_candidates
        assert report.pader_candidates[0].id == "PAD1"
        assert report.api_generation == "modern_tracker_preferred_legacy_deprecated"
        assert report.accessible_facility_count == 1
        assert report.accessible_facilities[0].id == "HF1"
        assert report.facility_scope_counts == {
            "capture": 1,
            "data_view": 1,
            "tracker_search": 1,
        }
        assert "/api/dataValueSets" in report.supported_analytical_apis
        assert report.stop_before_patient_data is True
        dumped = json.dumps(report.sanitized_dict())
        assert SECRET_TOKEN not in dumped
        assert "should-be-dropped@example.org" not in dumped

    def test_resource_catalogue_classifies_analytics_without_requesting_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/resources":
                return json_response(
                    {
                        "resources": [
                            {
                                "name": "Analytics",
                                "relativeApiEndpoint": "/api/analytics",
                            }
                        ]
                    }
                )
            return _metadata_handler(request)

        transport = recording(handler)
        with DiscoveryClient(config(), transport=transport) as client:
            report = run_discovery(client, origin_host="dhis2.example.org")
        record = next(
            item for item in report.capabilities if item.name == "event_analytics_aggregate"
        )
        assert record.status is CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
        assert record.probed is False
        assert "/api/analytics/events/aggregate" in report.supported_analytical_apis
        assert not any(request.url.path.startswith("/api/analytics/") for request in transport.seen)

    def test_version_42_selects_modern_tracker_and_rejects_legacy_generation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/system/info":
                return json_response({"version": "2.42.0"})
            return _metadata_handler(request)

        transport = recording(handler)
        with DiscoveryClient(config(), transport=transport) as client:
            report = run_discovery(client, origin_host="dhis2.example.org")
        assert report.api_generation == "modern_tracker_only"
        modern = next(
            item for item in report.capabilities if item.name == "tracker_tracked_entities"
        )
        legacy = next(
            item for item in report.capabilities if item.name == "tracked_entity_instances"
        )
        assert modern.status is CapabilityStatus.SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED
        assert legacy.status is CapabilityStatus.NOT_SUPPORTED
        assert modern.probed is legacy.probed is False

    def test_forbidden_metadata_is_classified_without_being_called_a_zero(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/dataElements":
                return httpx.Response(403)
            return _metadata_handler(request)

        transport = recording(handler)
        with DiscoveryClient(config(), transport=transport) as client:
            report = run_discovery(client, origin_host="dhis2.example.org")
        record = next(item for item in report.capabilities if item.name == "data_elements")
        assert record.status is CapabilityStatus.SUPPORTED_BUT_FORBIDDEN
        assert report.data_elements == []

    def test_authentication_failure_is_classified(self) -> None:
        transport = scripted([httpx.Response(401)])
        with DiscoveryClient(config(max_retries=0), transport=transport) as client:
            report = run_discovery(client, origin_host="dhis2.example.org")
        info = next(item for item in report.capabilities if item.name == "system_info")
        assert info.status is CapabilityStatus.AUTHENTICATION_FAILED

    def test_markdown_and_json_are_written_atomically(self, tmp_path: Path) -> None:
        transport = recording(_metadata_handler)
        with DiscoveryClient(config(), transport=transport) as client:
            report = run_discovery(client, origin_host="dhis2.example.org")
        json_path, markdown_path = write_reports(report, tmp_path)
        assert json_path.exists()
        assert markdown_path.exists()
        text = markdown_path.read_text(encoding="utf-8")
        assert "not retrieved" in text.lower() or "Patient data" in text
        assert SECRET_PASSWORD not in text
        assert "Mandatory stop" in text
        body = json.loads(json_path.read_text(encoding="utf-8"))
        assert body["stop_before_patient_data"] is True
        rendered = render_markdown(report)
        assert "Pader" in rendered


class TestClassificationIsProposalOnly:
    def test_a_name_match_is_labelled_a_proposal(self) -> None:
        unit = compact_unit({"id": "X", "name": "Pader District", "level": 3})
        assert unit.classification == "pader_candidate"
        proposals = candidate_mappings(
            units=[unit],
            programmes=[{"id": "P", "name": "OPD Register"}],
            data_elements=[{"id": "D", "name": "Malaria cases"}],
            attributes=[],
        )
        assert all(item.status == "proposal" for item in proposals)

    def test_a_blank_name_is_not_invented(self) -> None:
        unit = compact_unit({"id": "Z"})
        assert unit.name is None
        assert unit.classification == "organisation_unit"
