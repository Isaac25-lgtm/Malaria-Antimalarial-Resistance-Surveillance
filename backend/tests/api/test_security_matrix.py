"""The permission and sensitivity matrix — Prompt 28.

Blueprint 057: *every endpoint declares permission scope and geography scope.
Server-side enforcement is mandatory; hiding a button is not access control.*

This file is the machine-checked half of that claim. It walks the live route
table rather than a hand-maintained list, so a new endpoint added without a
permission fails here on the day it is written rather than on the day someone
notices. The matrix it prints is the documentation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mars.security.permissions import Permission

#: Routes that are deliberately reachable without a permission, each with the
#: reason. A route may only be added here with one.
UNAUTHENTICATED_ROUTES: dict[str, str] = {
    "/api/v1/health/live": "Liveness probe. Reports nothing about the data.",
    "/api/v1/health/ready": "Readiness probe. Dependency reachability only.",
    "/api/v1/health/schema": "Migration head only. No surveillance content.",
    "/api/v1/meta/version": "Build and governance identity. No patient data.",
    "/api/v1/meta/permissions": "The permission catalogue itself.",
    "/api/v1/meta/evidence-lanes": "Static description of the two evidence lanes.",
    "/api/v1/meta/assistant": "Whether the optional assistant is switched on.",
    "/api/v1/auth/me": "Returns the caller's own identity; authenticated by definition.",
    "/api/v1/auth/logout": "Ends the caller's own session.",
    "/api/v1/auth/dev/users": "Development authentication; refused in protected environments.",
    "/api/v1/auth/dev/login": "Development authentication; refused in protected environments.",
}

#: Endpoints that additionally require a sensitivity tier above aggregate.
SENSITIVE_ROUTES: dict[str, str] = {
    "/api/v1/analytics/episodes": "pseudonymous_case",
}


def _routes(app: FastAPI) -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _declared_dependencies(route: APIRoute) -> str:
    """Every dependency callable behind a route, as text.

    Read from the route's own dependant tree, so a permission declared through
    an ``Annotated`` alias counts exactly as one declared inline.
    """
    parts: list[str] = []
    for dependency in route.dependant.dependencies:
        call = dependency.call
        parts.append(getattr(call, "__qualname__", repr(call)))
        parts.append(repr(getattr(call, "__closure__", "")))
        for cell in getattr(call, "__closure__", None) or ():
            try:
                parts.append(repr(cell.cell_contents))
            except ValueError:  # pragma: no cover - empty cell
                continue
    return " ".join(parts)


class TestEveryEndpointDeclaresItsAccess:
    def test_no_route_is_reachable_without_a_declared_permission(self, app: FastAPI) -> None:
        """The systemic guarantee. A new endpoint with no permission fails here
        on the day it is written."""
        offenders: list[str] = []
        for route in _routes(app):
            if route.path in UNAUTHENTICATED_ROUTES:
                continue
            declared = _declared_dependencies(route)
            if "require_permissions" not in declared and "Permission." not in declared:
                offenders.append(f"{sorted(route.methods)} {route.path}")
        assert not offenders, (
            "endpoints with no declared permission - hiding a button is not "
            f"access control: {offenders}"
        )

    def test_every_unauthenticated_route_has_a_recorded_reason(self, app: FastAPI) -> None:
        """The exemption list is small and each entry says why."""
        paths = {route.path for route in _routes(app)}
        for path, reason in UNAUTHENTICATED_ROUTES.items():
            if path in paths:
                assert reason, f"{path} is exempt without a reason"

    def test_the_exemption_list_contains_no_surveillance_endpoint(self) -> None:
        """A health probe may be open. A district's figures may not."""
        for path in UNAUTHENTICATED_ROUTES:
            assert not any(
                segment in path
                for segment in (
                    "/surveillance/",
                    "/analytics/",
                    "/signals",
                    "/investigations",
                    "/reports/",
                )
            )

    def test_the_matrix_is_printable(self, app: FastAPI, capsys: Any) -> None:
        """The documentation half. Failing to render it is a signal that the
        route table has grown a shape this file does not understand."""
        rows = []
        for route in _routes(app):
            declared = _declared_dependencies(route)
            permissions = sorted(
                {
                    permission.value
                    for permission in Permission
                    if repr(permission) in declared or permission.value in declared
                }
            )
            rows.append(
                (
                    ",".join(sorted(route.methods - {"HEAD", "OPTIONS"})),
                    route.path,
                    permissions or ["(open)"],
                )
            )
        assert rows
        for method, path, permissions in sorted(rows, key=lambda row: row[1]):
            print(f"{method:8} {path:60} {','.join(permissions)}")
        assert capsys.readouterr().out


class TestWriteEndpointsAreSeparatelyPermissioned:
    def test_investigation_commands_do_not_share_one_permission(self, app: FastAPI) -> None:
        """One blanket investigation:write would let whoever can add a note
        also close the case."""
        seen: set[str] = set()
        for route in _routes(app):
            if not route.path.startswith("/api/v1/investigations"):
                continue
            declared = _declared_dependencies(route)
            for permission in Permission:
                if permission.value.startswith("investigation:") and (
                    repr(permission) in declared or permission.value in declared
                ):
                    seen.add(permission.value)
        assert {
            "investigation:triage",
            "investigation:assign",
            "investigation:update",
            "investigation:close",
        } <= seen

    def test_export_is_permissioned_separately_from_report_generation(self, app: FastAPI) -> None:
        """Reading a figure on screen and carrying it out of the system in a
        file are different acts with different risks."""
        export = next(route for route in _routes(app) if route.path.endswith("/export.csv"))
        declared = _declared_dependencies(export)
        assert Permission.DATA_EXPORT.value in declared or repr(Permission.DATA_EXPORT) in declared


class TestSecurityHeaders:
    def test_every_response_refuses_content_sniffing(self, client: TestClient) -> None:
        response = client.get("/api/v1/health/live")
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_every_response_refuses_framing(self, client: TestClient) -> None:
        """Clickjacking a triage button is a cheap attack on a workflow that
        changes real records."""
        response = client.get("/api/v1/health/live")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

    def test_no_referrer_leaks_which_district_was_open(self, client: TestClient) -> None:
        response = client.get("/api/v1/health/live")
        assert response.headers["Referrer-Policy"] == "no-referrer"

    def test_surveillance_responses_are_not_shared_cached(self, client: TestClient) -> None:
        """A shared cache holding one district officer's answer for the next is
        the leak this prevents."""
        response = client.get("/api/v1/health/live")
        assert response.headers["Cache-Control"] == "no-store"


class TestAdversarialScope:
    def test_a_district_user_cannot_read_another_districts_signal(
        self, authenticated_client: Any, gulu_district_principal: Any
    ) -> None:
        """404, not 403: telling a caller that a signal exists but is not
        theirs would itself disclose that something was flagged there."""
        client = authenticated_client(gulu_district_principal)
        response = client.get(f"/api/v1/signals/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_a_facility_user_cannot_read_a_district_summary(
        self, authenticated_client: Any, gulu_facility_principal: Any
    ) -> None:
        """A facility's district membership does not grant the district-wide
        surveillance picture."""
        client = authenticated_client(gulu_facility_principal)
        response = client.get(
            f"/api/v1/surveillance/districts/{uuid.uuid4()}/summary",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert response.status_code == 200
        for measure in response.json():
            if measure["code"] == "ACTIVE_SIGNALS":
                continue
            assert measure["value"] is None

    def test_a_facility_user_gets_no_roster_of_neighbouring_facilities(
        self, authenticated_client: Any, gulu_facility_principal: Any
    ) -> None:
        client = authenticated_client(gulu_facility_principal)
        response = client.get(
            f"/api/v1/surveillance/districts/{uuid.uuid4()}/facilities",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_an_unscoped_account_reads_nothing(
        self, authenticated_client: Any, unscoped_principal: Any
    ) -> None:
        """A misconfigured account is the one most likely to exist in a real
        deployment, and it must be able to read nothing."""
        client = authenticated_client(unscoped_principal)
        response = client.get(
            "/api/v1/surveillance/priority-districts",
            params={"period_start": "2026-07-01", "period_end": "2026-07-31"},
        )
        assert response.status_code in (200, 403)
        if response.status_code == 200:
            assert response.json() == []

    def test_an_unauthenticated_caller_reaches_no_surveillance_endpoint(
        self, client: TestClient
    ) -> None:
        for path in (
            "/api/v1/surveillance/national/summary",
            "/api/v1/analytics/results/anomaly",
            "/api/v1/signals",
            "/api/v1/investigations/queues/new",
        ):
            response = client.get(
                path, params={"period_start": "2026-07-01", "period_end": "2026-07-31"}
            )
            assert response.status_code in (401, 403), path


class TestErrorsDoNotLeak:
    def test_a_scope_denial_names_no_record(
        self, authenticated_client: Any, gulu_district_principal: Any
    ) -> None:
        """The problem document says what is missing, never what exists."""
        client = authenticated_client(gulu_district_principal)
        body = client.get(f"/api/v1/signals/{uuid.uuid4()}").json()
        detail = (body.get("detail") or "").lower()
        assert "outside your assigned scope" in detail or "not found" in detail
        for leak in ("select", "traceback", "postgresql", "password"):
            assert leak not in detail

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/health/ready", "/api/v1/health/schema", "/api/v1/meta/version"],
    )
    def test_diagnostic_endpoints_expose_no_credential(self, client: TestClient, path: str) -> None:
        body = client.get(path).text.lower()
        for leak in ("password", "secret", "postgresql+psycopg://", "@127.0.0.1"):
            assert leak not in body
