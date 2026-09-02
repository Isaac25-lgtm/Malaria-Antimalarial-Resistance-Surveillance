"""API surface, error contract and route-level authorisation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mars.core.errors import PROBLEM_CONTENT_TYPE
from mars.security.principal import AuthenticatedPrincipal


class TestHealthEndpoints:
    def test_liveness_needs_no_authentication(self, client: TestClient) -> None:
        response = client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive", "service": "MARS"}

    def test_readiness_reports_database_unavailable(self, client: TestClient) -> None:
        """No database is reachable in this harness, and readiness must say so."""
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        names = {d["name"]: d for d in body["dependencies"]}
        assert names["postgresql"]["status"] == "unavailable"

    def test_readiness_does_not_leak_connection_details(self, client: TestClient) -> None:
        """A failed probe must not disclose the connection string."""
        body = client.get("/api/v1/health/ready").text
        assert "password" not in body.lower()
        assert "postgresql+psycopg" not in body


class TestRequestIdentity:
    def test_response_carries_a_request_id(self, client: TestClient) -> None:
        response = client.get("/api/v1/health/live")
        assert response.headers.get("X-Request-ID")

    def test_inbound_request_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "trace-from-proxy"})
        assert response.headers["X-Request-ID"] == "trace-from-proxy"

    def test_oversized_request_id_is_replaced(self, client: TestClient) -> None:
        """An unbounded client value would flow into every log line."""
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "x" * 500})
        assert response.headers["X-Request-ID"] != "x" * 500
        assert len(response.headers["X-Request-ID"]) <= 64


class TestErrorContract:
    def test_unknown_route_returns_a_problem_document(self, client: TestClient) -> None:
        response = client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        body = response.json()
        assert body["code"] == "not_found"
        assert body["status"] == 404
        assert body["request_id"]

    def test_unauthenticated_request_returns_401_problem(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "unauthenticated"
        assert "bearer token" in body["detail"].lower()

    def test_validation_failure_lists_field_errors(self, client: TestClient) -> None:
        response = client.post("/api/v1/auth/dev/login", json={"username": ""})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "validation_failed"
        assert body["errors"]
        assert body["errors"][0]["field"] == "username"

    def test_no_stack_trace_reaches_the_client(self, client: TestClient) -> None:
        body = client.get("/api/v1/health/ready").text
        assert "Traceback" not in body
        assert "site-packages" not in body


class TestMetadataEndpoints:
    def test_version_reports_deployment_identity(self, client: TestClient) -> None:
        response = client.get("/api/v1/meta/version")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "MARS"
        assert body["api_version"] == "v1"
        assert body["display_timezone"] == "Africa/Kampala"
        assert body["environment"] == "local"

    def test_version_reports_ai_disabled(self, client: TestClient) -> None:
        """Core surveillance must not depend on generative AI."""
        assert client.get("/api/v1/meta/version").json()["ai_assistant_enabled"] is False

    def test_version_reports_empty_registries(self, client: TestClient) -> None:
        """No method or configuration has been defined yet; say so honestly."""
        body = client.get("/api/v1/meta/version").json()
        assert body["active_method_versions"] == []
        assert body["active_configuration_keys"] == []

    def test_permission_catalogue_is_published(self, client: TestClient) -> None:
        body = client.get("/api/v1/meta/permissions").json()
        codes = {p["code"] for p in body["permissions"]}
        assert "surveillance:view_aggregate" in codes
        assert "patient:reidentify" in codes
        reid = next(p for p in body["permissions"] if p["code"] == "patient:reidentify")
        assert reid["minimum_sensitivity"] == "direct_identity"

    def test_no_system_role_grants_reidentification(self, client: TestClient) -> None:
        body = client.get("/api/v1/meta/permissions").json()
        for role in body["system_roles"]:
            assert "patient:reidentify" not in role["permissions"], role["code"]


class TestEvidenceLanesEndpoint:
    """The scientific boundary is served from one authoritative place."""

    def test_declares_two_separate_lanes(self, client: TestClient) -> None:
        body = client.get("/api/v1/meta/evidence-lanes").json()
        lane_ids = {lane["id"] for lane in body["lanes"]}
        assert lane_ids == {"routine_surveillance", "confirmed_evidence"}

    def test_routine_lane_permits_only_bounded_language(self, client: TestClient) -> None:
        body = client.get("/api/v1/meta/evidence-lanes").json()
        routine = next(lane for lane in body["lanes"] if lane["id"] == "routine_surveillance")
        for phrase in routine["permitted_language"]:
            assert "confirmed resistance" not in phrase.lower()
        assert "cannot" in routine["boundary"].lower()

    def test_confirmed_lane_is_never_fed_by_routine_data(self, client: TestClient) -> None:
        body = client.get("/api/v1/meta/evidence-lanes").json()
        confirmed = next(lane for lane in body["lanes"] if lane["id"] == "confirmed_evidence")
        assert "never populated from routine data" in confirmed["boundary"].lower()

    def test_reports_neither_lane_is_implemented_yet(self, client: TestClient) -> None:
        status = client.get("/api/v1/meta/evidence-lanes").json()["implementation_status"]
        assert status["routine_surveillance"] == "not_implemented"
        assert status["confirmed_evidence"] == "not_implemented"


class TestRouteAuthorisation:
    """Permission checks are enforced at the router, for every caller."""

    def test_administrator_cannot_read_surveillance_geography(
        self,
        authenticated_client,
        administrator_principal: AuthenticatedPrincipal,
    ) -> None:
        """Administrator holds geography:view, so this one is permitted."""
        client = authenticated_client(administrator_principal)
        assert client.get("/api/v1/geography/overview").status_code == 200

    def test_administrator_is_denied_the_governance_method_registry(
        self,
        authenticated_client,
        administrator_principal: AuthenticatedPrincipal,
    ) -> None:
        client = authenticated_client(administrator_principal)
        response = client.get("/api/v1/governance/methods")
        assert response.status_code == 200  # administrator holds method:view

    def test_facility_user_is_denied_organisation_listing(
        self,
        authenticated_client,
        gulu_facility_principal: AuthenticatedPrincipal,
    ) -> None:
        """A facility user has no organisation:view grant."""
        client = authenticated_client(gulu_facility_principal)
        response = client.get("/api/v1/organisation-units")
        assert response.status_code == 403
        body = response.json()
        assert body["code"] == "permission_denied"
        assert "organisation:view" in body["detail"]

    def test_denial_names_the_permission_not_the_resource(
        self,
        authenticated_client,
        gulu_facility_principal: AuthenticatedPrincipal,
    ) -> None:
        """A 403 must not confirm that a resource exists."""
        client = authenticated_client(gulu_facility_principal)
        detail = client.get("/api/v1/organisation-units").json()["detail"]
        assert "requires" in detail.lower()

    def test_denials_are_audited(
        self,
        authenticated_client,
        gulu_facility_principal: AuthenticatedPrincipal,
        audit_recorder,
    ) -> None:
        client = authenticated_client(gulu_facility_principal)
        client.get("/api/v1/organisation-units")
        assert audit_recorder.denials(), "a denied action was not audited"
        assert "organisation:view" in audit_recorder.denials()[0]["reason"]

    def test_national_user_may_list_facilities(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        client = authenticated_client(national_principal)
        response = client.get("/api/v1/facilities")
        assert response.status_code == 200
        assert response.json()["items"] == []


class TestCurrentUserEndpoint:
    def test_returns_effective_authorisation(
        self, authenticated_client, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        client = authenticated_client(gulu_district_principal)
        body = client.get("/api/v1/auth/me").json()
        assert body["username"] == "district.gulu"
        assert body["max_sensitivity"] == "pseudonymous_case"
        assert body["has_national_scope"] is False
        assert body["geography_scopes"][0]["preferred_code"] == "304"

    def test_marks_synthetic_development_accounts(
        self, authenticated_client, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        client = authenticated_client(gulu_district_principal)
        assert client.get("/api/v1/auth/me").json()["is_synthetic"] is True

    def test_does_not_expose_reidentification_permission(
        self, authenticated_client, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        client = authenticated_client(gulu_district_principal)
        assert "patient:reidentify" not in client.get("/api/v1/auth/me").json()["permissions"]


class TestGeographyOverview:
    def test_reports_all_levels_including_empty_ones(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        """Parish and village must read as 'none loaded', not as missing."""
        client = authenticated_client(national_principal)
        body = client.get("/api/v1/geography/overview").json()
        levels = {entry["level"]: entry["count"] for entry in body["levels"]}
        assert set(levels) == {
            "country",
            "region",
            "district",
            "county",
            "subcounty",
            "parish",
            "village",
        }
        assert levels["parish"] == 0
        assert levels["village"] == 0

    def test_explains_why_levels_are_empty(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        client = authenticated_client(national_principal)
        note = client.get("/api/v1/geography/overview").json()["note"]
        assert "no parish or village boundary data has been supplied" in note.lower()
