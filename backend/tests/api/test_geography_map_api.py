"""Contract behaviour of the map delivery endpoints.

No database. These assert the things that must hold regardless of what geography
happens to be loaded: who is refused, what a refusal reveals, and what an
unloaded deployment answers. The behaviour that needs real geometry - scope
filtering, payload size, the property allow-list against live rows - is proved
in ``tests/integration/test_geography_map.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from mars.security.permissions import Permission, SystemRole
from mars.security.principal import AuthenticatedPrincipal
from mars.services.geography_map_service import (
    DEFAULT_FEATURE_LIMIT,
    FEATURE_PROPERTIES,
    MAX_FEATURES,
    NATIONAL_LAYER_LEVELS,
)

#: Every map route, with a subject id where one is needed.
UNIT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")

MAP_ROUTES: list[str] = [
    "/api/v1/geography/map/metadata",
    "/api/v1/geography/map/features?level=district",
    "/api/v1/geography/national",
    f"/api/v1/geography/units/{UNIT_ID}/geometry",
    f"/api/v1/geography/units/{UNIT_ID}/bounds",
    f"/api/v1/geography/units/{UNIT_ID}/breadcrumbs",
    "/api/v1/geography/districts/304",
    "/api/v1/geography/subcounties/304101",
]


@pytest.fixture
def permissionless_principal() -> AuthenticatedPrincipal:
    """An authenticated account holding no permissions at all.

    Distinct from ``unscoped_principal``, which *does* hold ``geography:view``
    and simply has no geography assigned. The two must fail differently: no
    permission is a 403 naming the grant, no scope is an empty or not-found
    answer that reveals nothing about what exists.
    """
    from tests.conftest import make_principal

    return make_principal(
        role=SystemRole.DISTRICT_HSD,
        permissions=frozenset(),
        username="no.permissions",
    )


class TestPermissionIsRequired:
    """``geography:view`` gates every map route, without exception."""

    @pytest.mark.parametrize("route", MAP_ROUTES)
    def test_unauthenticated_request_is_refused(self, client: TestClient, route: str) -> None:
        response = client.get(route)
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"

    @pytest.mark.parametrize("route", MAP_ROUTES)
    def test_principal_without_the_permission_is_refused(
        self,
        authenticated_client,
        permissionless_principal: AuthenticatedPrincipal,
        route: str,
    ) -> None:
        response = authenticated_client(permissionless_principal).get(route)
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"

    @pytest.mark.parametrize("route", MAP_ROUTES)
    def test_the_denial_names_the_permission_not_the_resource(
        self,
        authenticated_client,
        permissionless_principal: AuthenticatedPrincipal,
        route: str,
    ) -> None:
        """A 403 must not confirm that the requested unit exists."""
        body = authenticated_client(permissionless_principal).get(route).json()
        detail = (body.get("detail") or "").lower()
        assert "geography:view" in detail
        assert str(UNIT_ID) not in detail
        assert "304" not in detail

    @pytest.mark.parametrize("route", MAP_ROUTES)
    def test_the_denial_is_audited(
        self,
        authenticated_client,
        permissionless_principal: AuthenticatedPrincipal,
        audit_recorder,
        route: str,
    ) -> None:
        authenticated_client(permissionless_principal).get(route)
        assert audit_recorder.denials(), f"no denial recorded for {route}"


class TestScopelessPrincipalIsNotForbidden:
    """Holding the permission with no geography assigned is not a 403.

    The distinction matters. A 403 says "you may not do this"; an empty result
    says "there is nothing here for you". Returning 403 for a scope miss would
    let a caller tell an account that is misconfigured from one that is simply
    looking at an area it does not cover - and, on a per-unit route, would
    confirm the unit exists.
    """

    def test_metadata_is_served_and_reports_nothing_drawable(
        self, authenticated_client, unscoped_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(unscoped_principal).get("/api/v1/geography/map/metadata")
        assert response.status_code == 200
        assert response.json()["is_available"] is False

    def test_features_are_served_and_empty(
        self, authenticated_client, unscoped_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(unscoped_principal).get(
            "/api/v1/geography/map/features?level=district"
        )
        assert response.status_code == 200
        assert response.json()["features"] == []

    def test_a_unit_route_is_not_found_rather_than_forbidden(
        self, authenticated_client, unscoped_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(unscoped_principal).get(
            f"/api/v1/geography/units/{UNIT_ID}/geometry"
        )
        assert response.status_code == 404

    def test_it_holds_the_permission_it_is_not_being_refused_for(
        self, unscoped_principal: AuthenticatedPrincipal
    ) -> None:
        """Guards the premise of this whole class."""
        assert unscoped_principal.has_permission(Permission.GEOGRAPHY_VIEW)
        assert not unscoped_principal.geography_scopes


class TestExistenceHiding:
    """Hidden and absent must be the same answer.

    The fake service raises the same not-found the real one raises for an
    out-of-scope unit. If a route ever distinguished the two - by status, code
    or wording - a caller could enumerate the hierarchy by probing it.
    """

    SUBJECT_ROUTES: ClassVar[list[str]] = [
        f"/api/v1/geography/units/{UNIT_ID}/geometry",
        f"/api/v1/geography/units/{UNIT_ID}/bounds",
        f"/api/v1/geography/units/{UNIT_ID}/breadcrumbs",
        "/api/v1/geography/districts/999",
        "/api/v1/geography/subcounties/999999",
    ]

    @pytest.mark.parametrize("route", SUBJECT_ROUTES)
    def test_an_unresolvable_subject_is_not_found(
        self, authenticated_client, national_principal: AuthenticatedPrincipal, route: str
    ) -> None:
        response = authenticated_client(national_principal).get(route)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    @pytest.mark.parametrize("route", SUBJECT_ROUTES)
    def test_the_message_does_not_echo_the_requested_identifier(
        self, authenticated_client, national_principal: AuthenticatedPrincipal, route: str
    ) -> None:
        """Echoing the id back is harmless alone, but it invites a caller to
        treat a reflected value as confirmation. The message stays generic."""
        detail = authenticated_client(national_principal).get(route).json().get("detail") or ""
        assert str(UNIT_ID) not in detail

    def test_geometry_and_bounds_answer_identically_for_a_hidden_unit(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        client = authenticated_client(national_principal)
        geometry = client.get(f"/api/v1/geography/units/{UNIT_ID}/geometry")
        bounds = client.get(f"/api/v1/geography/units/{UNIT_ID}/bounds")
        assert geometry.status_code == bounds.status_code == 404
        assert geometry.json()["code"] == bounds.json()["code"]


class TestUnloadedDeployment:
    """Before the importer has run, the map says so rather than failing."""

    def test_metadata_reports_unavailable(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        body = authenticated_client(national_principal).get("/api/v1/geography/map/metadata").json()
        assert body["is_available"] is False
        assert body["boundary_version_id"] is None
        assert body["levels"] == []

    def test_metadata_still_states_the_delivery_contract(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        """A client must be able to learn the ceiling before it asks for a layer."""
        body = authenticated_client(national_principal).get("/api/v1/geography/map/metadata").json()
        assert body["geometry_resolution"] == "simplified"
        assert body["max_features"] == MAX_FEATURES

    def test_features_returns_an_empty_collection_not_an_error(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(national_principal).get(
            "/api/v1/geography/map/features?level=district"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "FeatureCollection"
        assert body["features"] == []
        assert body["mars"]["boundary_version_code"] is None

    def test_national_reports_no_root(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        body = authenticated_client(national_principal).get("/api/v1/geography/national").json()
        assert body["root"] is None
        assert body["children"] == []


class TestRequestValidation:
    """The payload budget is enforced at the contract, not only in the service."""

    def test_level_is_required(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(national_principal).get("/api/v1/geography/map/features")
        assert response.status_code == 422

    def test_an_unknown_level_is_rejected(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(national_principal).get(
            "/api/v1/geography/map/features?level=continent"
        )
        assert response.status_code == 422

    def test_a_limit_above_the_ceiling_is_rejected(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        """Rejected at the boundary rather than silently clamped, so a client
        asking for more than it may have is told, not quietly given less."""
        response = authenticated_client(national_principal).get(
            f"/api/v1/geography/map/features?level=district&limit={MAX_FEATURES + 1}"
        )
        assert response.status_code == 422

    def test_a_malformed_parent_id_is_rejected(
        self, authenticated_client, national_principal: AuthenticatedPrincipal
    ) -> None:
        response = authenticated_client(national_principal).get(
            "/api/v1/geography/map/features?level=district&parent_id=not-a-uuid"
        )
        assert response.status_code == 422


class TestDeliveryContract:
    """Constants that are part of the published contract, not implementation."""

    def test_the_default_limit_is_within_the_ceiling(self) -> None:
        assert 0 < DEFAULT_FEATURE_LIMIT <= MAX_FEATURES

    def test_the_ceiling_admits_the_national_district_layer(self) -> None:
        """146 districts is the largest legitimate national request.

        The ceiling must sit above it, or the national map cannot be drawn at
        all; and below 2,190, or a national subcounty request would be served.
        """
        assert MAX_FEATURES > 146
        assert MAX_FEATURES < 2190

    def test_subcounty_is_not_a_national_layer(self) -> None:
        from mars.domain.enums import GeographyLevel

        assert GeographyLevel.SUBCOUNTY not in NATIONAL_LAYER_LEVELS
        assert GeographyLevel.DISTRICT in NATIONAL_LAYER_LEVELS

    def test_the_property_allow_list_carries_no_health_data(self) -> None:
        """The allow-list is administrative reference data only.

        A property named for a case, patient, household, coordinate or test
        result would mean health data had reached a public map payload.
        """
        forbidden = ("patient", "case", "household", "test", "result", "treatment", "resistance")
        for name in FEATURE_PROPERTIES:
            assert not any(word in name.lower() for word in forbidden), name

    def test_the_allow_list_is_closed(self) -> None:
        """Pinned deliberately.

        Widening a public payload should require editing this test, so it is a
        decision someone made rather than a field that appeared.
        """
        assert (
            frozenset(
                {
                    "unit_id",
                    "level",
                    "code",
                    "name",
                    "parent_id",
                    "path",
                    "area_sq_km",
                    "is_active",
                }
            )
            == FEATURE_PROPERTIES
        )


class TestOpenApiContract:
    """The generated TypeScript client is only as honest as this document."""

    def test_every_map_route_is_published(self, app) -> None:
        paths = app.openapi()["paths"]
        for route in [
            "/api/v1/geography/map/metadata",
            "/api/v1/geography/map/features",
            "/api/v1/geography/national",
            "/api/v1/geography/units/{unit_id}/geometry",
            "/api/v1/geography/units/{unit_id}/bounds",
            "/api/v1/geography/units/{unit_id}/breadcrumbs",
            "/api/v1/geography/districts/{code}",
            "/api/v1/geography/subcounties/{code}",
        ]:
            assert route in paths, f"{route} missing from the OpenAPI document"

    def test_feature_properties_are_a_closed_schema(self, app) -> None:
        """Declared as a model, so the allow-list reaches the generated client.

        A free-form object here would let a property appear in a payload without
        appearing in the contract.
        """
        schema = app.openapi()["components"]["schemas"]["MapFeatureProperties"]
        assert set(schema["properties"]) == FEATURE_PROPERTIES

    def test_the_geography_namespace_was_extended_not_duplicated(self, app) -> None:
        """Prompt 6 asked for one geography API, not a competing one.

        Matched on whole path segments. A substring test also catches
        ``mapping-proposals`` under an unrelated namespace, and a guard that
        fires on an innocent route teaches people to widen it until it fires on
        nothing.
        """
        paths = app.openapi()["paths"]
        map_paths = [
            path
            for path in paths
            if {"map", "geometry", "bounds"} & set(path.strip("/").split("/"))
        ]
        assert map_paths, "no map routes found"
        assert all(p.startswith("/api/v1/geography/") for p in map_paths), map_paths


class TestSensitivityAxis:
    """Why the map routes carry no explicit sensitivity dependency.

    MARS has three orthogonal authorisation axes: permission, geography scope
    and sensitivity. The map routes gate the first two explicitly. The third is
    enforced upstream: ``AuthService`` drops any permission whose minimum
    sensitivity exceeds the caller's ceiling, so *holding* ``geography:view``
    already proves the caller covers what it requires.

    That reasoning depends on a fact about the catalogue, so the fact is
    asserted rather than trusted. If ``geography:view`` were ever raised above
    the tier every role that holds it can reach, this fails - and the routes
    would need an explicit ``require_sensitivity``.
    """

    def test_geography_view_sits_at_the_aggregate_tier(self) -> None:
        from mars.security.permissions import PERMISSION_CATALOGUE, SensitivityLevel

        entry = PERMISSION_CATALOGUE[Permission.GEOGRAPHY_VIEW]
        assert entry.minimum_sensitivity is SensitivityLevel.AGGREGATE, (
            "geography:view no longer sits at the lowest tier; the map routes "
            "now need an explicit sensitivity dependency"
        )

    def test_every_role_holding_it_can_reach_that_tier(self) -> None:
        from mars.security.permissions import (
            PERMISSION_CATALOGUE,
            ROLE_DEFAULT_SENSITIVITY,
            ROLE_PERMISSIONS,
        )

        required = PERMISSION_CATALOGUE[Permission.GEOGRAPHY_VIEW].minimum_sensitivity
        for role, permissions in ROLE_PERMISSIONS.items():
            if Permission.GEOGRAPHY_VIEW in permissions:
                ceiling = ROLE_DEFAULT_SENSITIVITY[role]
                assert ceiling.covers(required), (
                    f"{role.value} is granted geography:view but cannot reach {required.name}"
                )

    def test_boundaries_are_not_facility_scoped(self) -> None:
        """The facility axis does not apply to administrative geography.

        A facility user is scoped to a facility *and* to the geography that
        contains it; the boundary of a subcounty is not facility data and is not
        filtered by facility. Recorded here so the omission reads as a decision
        rather than a gap.
        """
        from mars.services import geography_map_service

        source = Path(geography_map_service.__file__).read_text(encoding="utf-8")
        assert "facility" not in source.lower(), (
            "the map service references facilities; if facility scoping is now "
            "relevant to boundaries, that needs its own tests"
        )
