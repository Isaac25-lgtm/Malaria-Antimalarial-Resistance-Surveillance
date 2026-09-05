"""Live login API: cookies, CSRF, Origin, throttling, isolation from demo."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mars.api.dependencies import (
    get_audit_service,
    get_configuration_service,
    get_db_session,
    get_facility_service,
    get_geography_map_service,
    get_geography_service,
    get_method_registry_service,
    get_organisation_service,
    get_overview_service,
    get_surveillance_summary_service,
)
from mars.core.errors import NotFoundError
from mars.core.settings import Environment, Settings
from mars.domain.enums import IntegrationErrorCategory
from mars.integrations.dhis2.login.errors import LoginAdapterError
from mars.integrations.dhis2.login.models import LoginSnapshot, RemoteOrgUnit, RemoteOrgUnitLevel
from mars.main import create_app
from mars.security.principal import AuthenticatedPrincipal, GeographyScope
from mars.security.source_login import AuthenticationProvider
from mars.services.live_discovery import LiveMetadataDiscoveryService
from mars.services.live_scope import StaticGeographyLookup
from mars.services.overview import OverviewService
from mars.services.surveillance_summary import SurveillanceSummaryService
from tests.api.conftest import (
    FakeAuditService,
    FakeConfigurationService,
    FakeFacilityService,
    FakeGeographyMapService,
    FakeGeographyService,
    FakeMethodRegistryService,
    FakeOrganisationService,
)

SENTINEL = "live-login-sentinel-PASSWORD-must-never-appear"
ORIGIN = "http://127.0.0.1:5173"
PADER_ID = uuid.UUID("00000000-0000-4000-8000-000000000312")
GULU_ID = uuid.UUID("00000000-0000-4000-8000-000000000304")
COUNTRY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
FACILITY_ID = uuid.UUID("00000000-0000-4000-8000-00000000f001")
SIBLING_FACILITY_ID = uuid.UUID("00000000-0000-4000-8000-00000000f002")
PADER_UID = "PaderDist01"
GULU_UID = "GuluDistr01"
ROOT_UID = "UgandanRoot"
FAC_UID = "Facility001"
UNKNOWN_UID = "UnknownUid0"
TRACKER_UID = "PaderFac001"

PADER = GeographyScope(
    geography_unit_id=PADER_ID,
    preferred_code="312",
    level="district",
    name="Pader",
    path="UG/3/312",
)
GULU = GeographyScope(
    geography_unit_id=GULU_ID,
    preferred_code="304",
    level="district",
    name="Gulu",
    path="UG/3/304",
)
COUNTRY = GeographyScope(
    geography_unit_id=COUNTRY_ID,
    preferred_code="UG",
    level="country",
    name="Uganda",
    path="UG",
)


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def execute(self, _statement: object) -> Any:
        class _Empty:
            def scalars(self) -> _Empty:
                return self

            def all(self) -> list[Any]:
                return []

            def first(self) -> None:
                return None

            def scalar_one(self) -> int:
                return 0

            def scalar_one_or_none(self) -> None:
                return None

            def __iter__(self) -> Iterator[Any]:
                return iter(())

        return _Empty()

    def add(self, instance: object) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        return None

    def get(self, *_a: object, **_k: object) -> None:
        return None


class ScopedFakeGeographyService(FakeGeographyService):
    """Applies the live principal's geography before the empty lookup."""

    def get_unit(self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID, *_a: Any, **_k: Any):
        if not principal.covers_geography(unit_id):
            raise NotFoundError("geography unit not found or outside your assigned scope")
        raise NotFoundError("geography unit not found or outside your assigned scope")


class ScopedFakeFacilityService(FakeFacilityService):
    def list_facilities(
        self, principal: AuthenticatedPrincipal, *_a: Any, **kwargs: Any
    ) -> list[Any]:
        district_id = kwargs.get("district_id")
        if (
            district_id is not None
            and not principal.has_national_scope
            and district_id not in principal.scope_unit_ids()
        ):
            return []
        if principal.is_facility_restricted:
            return []
        return []

    def get_facility(
        self, principal: AuthenticatedPrincipal, facility_id: uuid.UUID, *_a: Any, **_k: Any
    ):
        if not principal.covers_facility(facility_id):
            raise NotFoundError("facility not found or outside your assigned scope")
        raise NotFoundError("facility not found or outside your assigned scope")


class ScriptedProvider(AuthenticationProvider):
    method = "dhis2_basic"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: LoginAdapterError | None = None
        self.snapshot = _snapshot(PADER_UID, username="officer", name="Pader")

    def authenticate(self, username: str, password: str) -> LoginSnapshot:
        self.calls.append((username, password))
        if self.error is not None:
            raise self.error
        return self.snapshot


def _snapshot(
    uid: str,
    *,
    username: str = "officer",
    level: int = 3,
    extra_units: tuple[RemoteOrgUnit, ...] = (),
    capture: tuple[RemoteOrgUnit, ...] = (),
    tracker: tuple[RemoteOrgUnit, ...] = (),
    name: str = "Unit",
    data_view_field_present: bool = True,
) -> LoginSnapshot:
    units: tuple[RemoteOrgUnit, ...] = (
        RemoteOrgUnit(
            uid=uid,
            name=name,
            code=None,
            level=level,
            path=f"/UgandanRoot/{uid}",
            parent_uid="UgandanRoot",
        ),
        *extra_units,
    )
    return LoginSnapshot(
        remote_user_id="UserUid0001",
        username=username,
        display_name="Officer",
        authorities=(),
        organisation_units=capture,
        data_view_organisation_units=units if data_view_field_present else (),
        tei_search_organisation_units=tracker,
        organisation_unit_levels=(
            RemoteOrgUnitLevel(1, "Country"),
            RemoteOrgUnitLevel(3, "District"),
            RemoteOrgUnitLevel(4, "Facility"),
        ),
        organisation_unit_groups=(),
        system_name="eRegisters",
        system_version="2.40",
        requested_paths=(
            "/api/system/info",
            "/api/me",
            "/api/me/authorization",
            "/api/organisationUnitLevels",
            "/api/organisationUnitGroups",
            "/api/organisationUnitGroupSets",
        ),
        data_view_field_present=data_view_field_present,
    )


def _live_settings() -> Settings:
    return Settings(
        environment=Environment.LOCAL,
        auth_mode="live",
        database_url="postgresql+psycopg://mars:test@localhost:5432/mars_live",
        dev_auth_enabled=False,
        demo_mode_enabled=False,
        cors_allow_origins=[ORIGIN, "http://localhost:5173"],
        dhis2_login_base_url="https://dhis2.example.org",
        log_format="console",
        login_throttle_max_attempts=5,
        login_throttle_window_seconds=900,
    )


@pytest.fixture
def live_app() -> Iterator[tuple[FastAPI, ScriptedProvider, RecordingSession, FakeAuditService]]:
    settings = _live_settings()
    application = create_app(settings)
    provider = ScriptedProvider()
    db = RecordingSession()
    audit = FakeAuditService()
    application.state.dhis2_login_provider = provider
    application.state.live_geography_lookup = StaticGeographyLookup(
        uids={PADER_UID: PADER, GULU_UID: GULU, ROOT_UID: COUNTRY},
        facilities={FAC_UID: FACILITY_ID},
    )
    application.dependency_overrides[get_db_session] = lambda: db
    application.dependency_overrides[get_audit_service] = lambda: audit
    application.dependency_overrides[get_geography_service] = ScopedFakeGeographyService
    application.dependency_overrides[get_geography_map_service] = FakeGeographyMapService
    application.dependency_overrides[get_organisation_service] = FakeOrganisationService
    application.dependency_overrides[get_facility_service] = ScopedFakeFacilityService
    application.dependency_overrides[get_configuration_service] = FakeConfigurationService
    application.dependency_overrides[get_method_registry_service] = FakeMethodRegistryService
    application.dependency_overrides[get_overview_service] = lambda: OverviewService(db, settings)
    application.dependency_overrides[get_surveillance_summary_service] = (
        lambda: SurveillanceSummaryService(db)
    )
    yield application, provider, db, audit
    application.dependency_overrides.clear()


@pytest.fixture
def live_client(live_app: tuple[FastAPI, ScriptedProvider, RecordingSession, FakeAuditService]):
    application, _provider, _db, _audit = live_app
    with TestClient(application, raise_server_exceptions=False, base_url=ORIGIN) as client:
        yield client


def _login(client: TestClient, **overrides: Any) -> Any:
    body = {"username": "officer", "password": SENTINEL, **overrides}
    return client.post(
        "/api/v1/auth/login",
        json=body,
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )


class TestLiveLoginApi:
    def test_session_is_anonymous_without_a_cookie(self, live_client: TestClient) -> None:
        response = live_client.get("/api/v1/auth/session")
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is False
        assert body["auth_mode"] == "live"

    def test_successful_login_sets_httponly_cookie(self, live_client: TestClient, live_app) -> None:
        response = _login(live_client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["authenticated"] is True
        assert SENTINEL not in json.dumps(body)
        assert "access_token" not in body
        cookies = response.headers.get_list("set-cookie")
        joined = " ".join(cookies).lower()
        assert "mars_session=" in joined
        assert "httponly" in joined
        assert "samesite=lax" in joined
        assert "path=/" in joined
        session = live_client.get("/api/v1/auth/session")
        assert session.json()["authenticated"] is True
        assert session.json()["scope"]["scope_type"] == "district"
        assert session.json()["profile"]["landing_path"] == f"/district/{PADER_ID}"
        assert session.json()["workspace"]["authorization_status"] == "resolved"
        assert session.json()["mapping"]["status"] == "resolved"
        holder = live_app[0].state.live_credential_holder
        raw = live_client.cookies.get("mars_session")
        assert raw and holder.has(raw)
        dumped = json.dumps([str(item) for item in live_app[2].added])
        assert SENTINEL not in dumped

    def test_metadata_discovery_uses_session_credential_and_returns_no_patient_data(
        self, live_client: TestClient, live_app
    ) -> None:
        login = _login(live_client)
        assert login.status_code == 200
        observed: list[tuple[str, str]] = []

        def runner(username: str, password: str) -> dict[str, Any]:
            observed.append((username, password))
            return {
                "stop_before_patient_data": True,
                "generated_at": "2026-09-04T12:00:00Z",
                "system": {"version": "2.40"},
                "api_generation": "modern_tracker_preferred_legacy_deprecated",
                "programmes": [{"id": "program-1"}],
                "program_stages": [{"id": "stage-1"}],
                "data_elements": [{"id": "element-1"}],
                "accessible_facilities": [{"id": "facility-1"}],
                "tracker_search_organisation_units": [{"id": TRACKER_UID}],
                "candidate_mappings": [{"remote_id": "element-1"}],
                "report_files": {"json": "safe.json", "markdown": "safe.md"},
            }

        application = live_app[0]
        application.state.live_metadata_discovery = LiveMetadataDiscoveryService(
            application.state.live_credential_holder,
            runner,
        )
        response = live_client.post(
            "/api/v1/auth/live/metadata-discovery",
            headers={"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["patient_data_retrieved"] is False
        assert body["dhis2_version"] == "2.40"
        assert body["programme_count"] == 1
        assert observed == [("officer", SENTINEL)]
        assert SENTINEL not in response.text
        latest = live_client.get("/api/v1/auth/live/metadata-discovery")
        assert latest.status_code == 200
        assert latest.json() == body

    def test_invalid_credentials_are_generic(self, live_client: TestClient, live_app) -> None:
        live_app[1].error = LoginAdapterError(
            IntegrationErrorCategory.AUTHENTICATION, "DHIS2 returned HTTP 401"
        )
        response = _login(live_client)
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid username or password"
        assert "DHIS2" not in json.dumps(response.json())
        assert SENTINEL not in json.dumps(response.json())

    def test_upstream_unavailable_is_sanitised(self, live_client: TestClient, live_app) -> None:
        live_app[1].error = LoginAdapterError(
            IntegrationErrorCategory.TIMEOUT, "stack trace would be bad"
        )
        response = _login(live_client)
        assert response.status_code == 503
        assert response.json()["detail"] == "Unable to connect to eRegisters"
        assert "stack trace" not in json.dumps(response.json())

    def test_origin_is_required(self, live_client: TestClient) -> None:
        response = live_client.post(
            "/api/v1/auth/login",
            json={"username": "officer", "password": SENTINEL},
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "origin_rejected"

    def test_wrong_content_type_is_rejected(self, live_client: TestClient) -> None:
        response = live_client.post(
            "/api/v1/auth/login",
            content=b"username=officer",
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 422

    def test_throttling_after_repeated_failures(self, live_client: TestClient, live_app) -> None:
        live_app[1].error = LoginAdapterError(IntegrationErrorCategory.AUTHENTICATION, "no")
        for _ in range(5):
            assert _login(live_client).status_code == 401
        blocked = _login(live_client)
        assert blocked.status_code == 429

    def test_csrf_required_for_logout(self, live_client: TestClient) -> None:
        assert _login(live_client).status_code == 200
        rejected = live_client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "csrf_rejected"
        csrf = live_client.get("/api/v1/auth/session").json()["csrf_token"]
        ok = live_client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert ok.status_code == 204
        assert live_client.get("/api/v1/auth/session").json()["authenticated"] is False

    def test_demo_routes_are_absent(self, live_client: TestClient) -> None:
        users = live_client.get("/api/v1/auth/dev/users")
        login = live_client.post(
            "/api/v1/auth/dev/login",
            json={"username": "district.pader"},
            headers={"Origin": ORIGIN},
        )
        assert users.status_code == 404
        assert login.status_code in {403, 404}

    def test_bearer_tokens_are_not_accepted(self, live_client: TestClient) -> None:
        response = live_client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-session"}
        )
        assert response.status_code == 401

    def test_remote_district_without_mapping_opens_live_workspace(
        self, live_client: TestClient, live_app
    ) -> None:
        live_app[1].snapshot = _snapshot(UNKNOWN_UID, username="district.pader", name="Pader")
        response = _login(live_client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["authenticated"] is True
        assert body["workspace"]["authorization_status"] == "resolved"
        assert body["workspace"]["scope_type"] == "district"
        assert body["workspace"]["name"] == "Pader"
        assert body["workspace"]["external_uid"] == UNKNOWN_UID
        assert body["mapping"]["status"] == "pending"
        assert body["mapping"]["geography_unit_id"] is None
        assert body["profile"]["landing_path"] == f"/live/dhis2/district/{UNKNOWN_UID}"
        assert body["profile"]["landing_path"] != "/no-authorised-scope"
        assert body["scope"]["national_access"] is False
        assert "surveillance:view_aggregate" not in (body["permissions"] or [])
        overview = live_client.get(
            "/api/v1/surveillance/overview",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert overview.status_code == 403
        session = live_client.get("/api/v1/auth/session")
        assert session.status_code == 200
        assert session.json()["authenticated"] is True


class TestLiveScopeEnforcement:
    def test_pader_user_cannot_open_another_district(
        self, live_client: TestClient, live_app
    ) -> None:
        assert _login(live_client).status_code == 200
        hidden = live_client.get(f"/api/v1/geography/units/{GULU_ID}")
        assert hidden.status_code == 404
        facilities = live_client.get(
            f"/api/v1/surveillance/districts/{GULU_ID}/facilities",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert facilities.status_code == 403
        assert facilities.json()["code"] == "geography_scope_denied"

    def test_district_overview_is_not_national(self, live_client: TestClient) -> None:
        assert _login(live_client).status_code == 200
        overview = live_client.get(
            "/api/v1/surveillance/overview",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["has_national_scope"] is False
        assert body["title"] == "Pader Overview"
        assert body["requested_scope"] != "national"

    def test_gulu_scope_does_not_open_pader(self, live_client: TestClient, live_app) -> None:
        live_app[1].snapshot = _snapshot(GULU_UID, name="Gulu")
        assert _login(live_client).status_code == 200
        session = live_client.get("/api/v1/auth/session").json()
        assert session["scope"]["org_unit_name"] == "Gulu"
        assert session["profile"]["landing_path"] == f"/district/{GULU_ID}"
        hidden = live_client.get(f"/api/v1/geography/units/{PADER_ID}")
        assert hidden.status_code == 404

    def test_multiple_districts_are_not_national(self, live_client: TestClient, live_app) -> None:
        live_app[1].snapshot = _snapshot(
            PADER_UID,
            name="Pader",
            extra_units=(
                RemoteOrgUnit(
                    uid=GULU_UID,
                    name="Gulu",
                    code=None,
                    level=3,
                    path=f"/UgandanRoot/{GULU_UID}",
                    parent_uid="UgandanRoot",
                ),
            ),
        )
        body = _login(live_client).json()
        assert body["scope"]["scope_type"] == "multi_district"
        assert body["scope"]["national_access"] is False
        assert body["profile"]["landing_path"] == "/authorised-scope"
        names = {item["org_unit_name"] for item in body["scope"]["authorised_districts"]}
        assert names == {"Pader", "Gulu"}

    def test_national_user_may_drill_into_an_authorised_district(
        self, live_client: TestClient, live_app
    ) -> None:
        live_app[1].snapshot = _snapshot(ROOT_UID, level=1, name="Uganda")
        assert _login(live_client).status_code == 200
        session = live_client.get("/api/v1/auth/session").json()
        assert session["scope"]["national_access"] is True
        assert session["profile"]["landing_path"] == "/command-centre"
        roster = live_client.get(
            f"/api/v1/surveillance/districts/{PADER_ID}/facilities",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert roster.status_code == 200
        overview = live_client.get(
            "/api/v1/surveillance/overview",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert overview.json()["title"] == "National Overview"

    def test_facility_user_cannot_open_a_sibling(self, live_client: TestClient, live_app) -> None:
        live_app[1].snapshot = _snapshot(FAC_UID, level=4, name="HC III")
        body = _login(live_client).json()
        assert body["scope"]["scope_type"] == "facility"
        assert body["profile"]["landing_path"] == f"/facility/{FACILITY_ID}"
        sibling = live_client.get(f"/api/v1/facilities/{SIBLING_FACILITY_ID}")
        assert sibling.status_code == 404

    def test_csrf_and_origin_required_for_authenticated_posts(
        self, live_client: TestClient
    ) -> None:
        assert _login(live_client).status_code == 200
        csrf = live_client.get("/api/v1/auth/session").json()["csrf_token"]
        missing_origin = live_client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert missing_origin.status_code == 403
        assert missing_origin.json()["code"] == "origin_rejected"
        wrong_csrf = live_client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "x" * len(csrf)},
        )
        assert wrong_csrf.status_code == 403

    def test_local_session_cookie_is_not_marked_secure(self, live_client: TestClient) -> None:
        for cookie in _login(live_client).headers.get_list("set-cookie"):
            attributes = {part.strip().lower().split("=", 1)[0] for part in cookie.split(";")}
            assert "secure" not in attributes

    def test_login_never_falls_back_to_a_demo_token(
        self, live_client: TestClient, live_app
    ) -> None:
        live_app[1].error = LoginAdapterError(IntegrationErrorCategory.AUTHENTICATION, "no")
        response = _login(live_client)
        assert response.status_code == 401
        assert "access_token" not in response.json()
        assert live_client.cookies.get("mars_session") in {None, ""}

    def test_credentials_are_absent_from_logs_and_audit(
        self, live_client: TestClient, live_app, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG"):
            assert _login(live_client).status_code == 200
        assert SENTINEL not in caplog.text
        dumped_audit = json.dumps(live_app[3].events, default=str)
        assert SENTINEL not in dumped_audit
        dumped_db = json.dumps([str(item) for item in live_app[2].added])
        assert SENTINEL not in dumped_db

    def test_empty_data_view_does_not_inherit_capture(
        self, live_client: TestClient, live_app
    ) -> None:
        live_app[1].snapshot = LoginSnapshot(
            remote_user_id="UserUid0001",
            username="officer",
            display_name="Officer",
            authorities=(),
            organisation_units=(
                RemoteOrgUnit(
                    uid=PADER_UID,
                    name="Pader",
                    code=None,
                    level=3,
                    path=f"/UgandanRoot/{PADER_UID}",
                    parent_uid="UgandanRoot",
                ),
            ),
            data_view_organisation_units=(),
            tei_search_organisation_units=(),
            organisation_unit_levels=(
                RemoteOrgUnitLevel(1, "Country"),
                RemoteOrgUnitLevel(3, "District"),
                RemoteOrgUnitLevel(4, "Facility"),
            ),
            organisation_unit_groups=(),
            system_name="eRegisters",
            system_version="2.40",
            requested_paths=("/api/me",),
            data_view_field_present=True,
        )
        body = _login(live_client).json()
        assert body["workspace"]["authorization_status"] == "unresolved"
        assert body["profile"]["landing_path"] == "/no-authorised-scope"
        assert body["workspace"]["fallback_used"] is False

    def test_tracker_scope_does_not_become_dashboard_scope(
        self, live_client: TestClient, live_app
    ) -> None:
        live_app[1].snapshot = _snapshot(
            PADER_UID,
            name="Pader",
            tracker=(
                RemoteOrgUnit(
                    uid=TRACKER_UID,
                    name="Pader HC III",
                    code=None,
                    level=4,
                    path=f"/UgandanRoot/{PADER_UID}/{TRACKER_UID}",
                    parent_uid=PADER_UID,
                ),
            ),
        )
        body = _login(live_client).json()
        assert body["workspace"]["scope_type"] == "district"
        assert body["workspace"]["external_uid"] == PADER_UID
        assert body["workspace"]["data_view_count"] == 1
        assert body["workspace"]["tracker_search_count"] == 1
        assert body["profile"]["landing_path"] == f"/district/{PADER_ID}"

    def test_dhis2_uid_is_rejected_by_local_uuid_routes(self, live_client: TestClient) -> None:
        assert _login(live_client).status_code == 200
        response = live_client.get(f"/api/v1/geography/units/{PADER_UID}")
        assert response.status_code == 422

    def test_no_usable_remote_authorization_is_no_authorised_scope(
        self, live_client: TestClient, live_app
    ) -> None:
        live_app[1].snapshot = LoginSnapshot(
            remote_user_id="UserUid0001",
            username="officer",
            display_name="Officer",
            authorities=(),
            organisation_units=(),
            data_view_organisation_units=(),
            tei_search_organisation_units=(),
            organisation_unit_levels=(RemoteOrgUnitLevel(3, "District"),),
            organisation_unit_groups=(),
            system_name="eRegisters",
            system_version="2.40",
            requested_paths=("/api/me",),
            data_view_field_present=True,
        )
        body = _login(live_client).json()
        assert body["workspace"]["scope_type"] == "unresolved"
        assert body["profile"]["landing_path"] == "/no-authorised-scope"


class TestLiveDoesNotFallBackToDemo:
    def test_demo_app_refuses_live_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MARS_AUTH_MODE", "demo")
        monkeypatch.setenv("MARS_DEV_AUTH_ENABLED", "true")
        settings = Settings(
            environment=Environment.LOCAL,
            auth_mode="demo",
            database_url="postgresql+psycopg://mars:test@localhost:5432/mars_local",
            dev_auth_enabled=True,
            demo_mode_enabled=True,
            cors_allow_origins=["http://localhost:5173"],
            log_format="console",
        )
        application = create_app(settings)
        with TestClient(application, raise_server_exceptions=False) as demo_client:
            response = demo_client.post(
                "/api/v1/auth/login",
                json={"username": "officer", "password": SENTINEL},
                headers={"Origin": "http://localhost:5173", "Content-Type": "application/json"},
            )
        assert response.status_code == 503
        assert response.json()["code"] == "feature_disabled"
        assert SENTINEL not in json.dumps(response.json())
