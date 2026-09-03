"""The DHIS2 adapter, against a scripted server rather than a live one.

Every test here drives the real client through an ``httpx`` transport, so the
retry, pagination, size-cap and error-categorisation code under test is the code
that would run against a real DHIS2. Nothing contacts a network.

The things worth proving are the ones that only bite in production: that a
credential never escapes into a log, an exception or a repr; that a 403 is not
retried until the account is locked out; that a huge response is refused before
it is in memory; and that an unresolved identifier fails visibly instead of
being matched to something with a similar name.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from mars.core.settings import Settings
from mars.domain.enums import IntegrationErrorCategory, IntegrationResource
from mars.integrations.dhis2.client import (
    Dhis2Client,
    Dhis2Config,
    Dhis2Error,
    strip_credentials,
)
from mars.integrations.dhis2.service import describe_scope, scope_fingerprint
from mars.integrations.ports import RemoteScope

SECRET_TOKEN = "d2pat-NEVER-IN-A-LOG-0000"
SECRET_PASSWORD = "not-a-real-password-9999"


def config(**overrides: Any) -> Dhis2Config:
    defaults: dict[str, Any] = {
        "base_url": "https://dhis2.example.org",
        "username": "mars_reader",
        "password": SECRET_PASSWORD,
        "token": None,
        "timeout_seconds": 5.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.0,
        "page_size": 2,
        "max_response_bytes": 1_000_000,
        "verify_tls": True,
    }
    defaults.update(overrides)
    return Dhis2Config(**defaults)


def scripted(responses: list[httpx.Response]) -> httpx.MockTransport:
    """A transport that returns each response in turn."""
    remaining = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(500)

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def org_unit_page(page: int, page_count: int, units: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "pager": {"page": page, "pageCount": page_count, "total": len(units) * page_count},
            "organisationUnits": units,
        },
    )


class TestCredentialsNeverEscape:
    def test_the_config_repr_does_not_render_a_password_or_token(self) -> None:
        """A config object reaches tracebacks, and a traceback reaches logs."""
        rendered = repr(config(token=SECRET_TOKEN))
        assert SECRET_TOKEN not in rendered
        assert SECRET_PASSWORD not in rendered
        assert "auth=token" in rendered

    def test_userinfo_is_stripped_from_a_base_url(self) -> None:
        """A URL is a place credentials hide, and this one is stored on a run."""
        stripped = strip_credentials("https://admin:district2026@dhis2.example.org/path/")
        assert stripped == "https://dhis2.example.org/path"
        assert "district2026" not in stripped
        assert "admin" not in stripped

    def test_settings_carry_the_secret_but_the_stored_url_does_not(self) -> None:
        settings = Settings(
            database_url="postgresql+psycopg://mars:pw@db:5432/mars",
            dhis2_enabled=True,
            dhis2_base_url="https://admin:district2026@dhis2.example.org",
            dhis2_token=SECRET_TOKEN,
        )
        resolved = Dhis2Config.from_settings(settings)
        assert resolved is not None
        assert "district2026" not in resolved.base_url
        # The token is still usable - it just never appears in the URL.
        assert resolved.token == SECRET_TOKEN

    def test_a_transport_failure_message_does_not_quote_the_request(self) -> None:
        """A transport error can carry the full request URL."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with (
            Dhis2Client(config(max_retries=0), transport=httpx.MockTransport(handler)) as client,
            pytest.raises(Dhis2Error) as raised,
        ):
            client.fetch_organisation_units()

        message = str(raised.value)
        assert SECRET_PASSWORD not in message
        assert raised.value.category is IntegrationErrorCategory.TRANSPORT

    def test_a_remote_error_body_is_not_repeated_back(self) -> None:
        """DHIS2 error responses can echo the request that produced them."""
        body = {"message": f"failed for Authorization: ApiToken {SECRET_TOKEN}"}
        transport = scripted([httpx.Response(500, json=body)])

        with (
            Dhis2Client(config(max_retries=0), transport=transport) as client,
            pytest.raises(Dhis2Error) as raised,
        ):
            client.fetch_organisation_units()

        assert SECRET_TOKEN not in str(raised.value)
        assert "HTTP 500" in str(raised.value)


class TestFailuresAreCategorised:
    @pytest.mark.parametrize(
        ("status", "category", "retryable"),
        [
            (401, IntegrationErrorCategory.AUTHENTICATION, False),
            (403, IntegrationErrorCategory.AUTHORISATION, False),
            (404, IntegrationErrorCategory.NOT_FOUND, False),
            (429, IntegrationErrorCategory.RATE_LIMITED, True),
            (503, IntegrationErrorCategory.REMOTE_SERVER_ERROR, True),
        ],
    )
    def test_each_status_maps_to_the_action_it_implies(
        self, status: int, category: IntegrationErrorCategory, retryable: bool
    ) -> None:
        """An operator's next move after a 401 is nothing like their next move
        after a 503. One error type would leave them re-running a request that
        can never succeed."""
        transport = scripted([httpx.Response(status)] * 4)
        with (
            Dhis2Client(config(max_retries=0), transport=transport) as client,
            pytest.raises(Dhis2Error) as raised,
        ):
            client.fetch_organisation_units()

        assert raised.value.category is category
        assert raised.value.is_retryable is retryable

    def test_a_403_is_not_retried(self) -> None:
        """Retrying an authorisation failure just locks the account out faster."""
        transport = scripted([httpx.Response(403)] * 5)
        with (
            Dhis2Client(config(max_retries=3), transport=transport) as client,
            pytest.raises(Dhis2Error),
        ):
            client.fetch_organisation_units()

        assert len(transport.seen) == 1, "an authorisation failure was retried"

    def test_a_503_is_retried_up_to_the_configured_limit(self) -> None:
        transport = scripted([httpx.Response(503)] * 6)
        slept: list[float] = []
        with (
            Dhis2Client(
                config(max_retries=2, retry_backoff_seconds=0.5),
                transport=transport,
                sleep=slept.append,
            ) as client,
            pytest.raises(Dhis2Error),
        ):
            client.fetch_organisation_units()

        assert len(transport.seen) == 3, "expected the original attempt plus two retries"
        assert slept == [0.5, 1.0], "backoff should grow with the attempt"

    def test_a_retry_that_succeeds_returns_the_good_response(self) -> None:
        transport = scripted(
            [httpx.Response(503), org_unit_page(1, 1, [{"id": "A", "name": "Alpha"}])]
        )
        with Dhis2Client(config(max_retries=1), transport=transport, sleep=lambda _s: None) as c:
            page = c.fetch_organisation_units()

        assert len(page.records) == 1
        assert page.records[0].remote_id == "A"

    def test_a_non_json_body_is_a_malformed_response_not_a_crash(self) -> None:
        transport = scripted([httpx.Response(200, text="<html>proxy error</html>")])
        with (
            Dhis2Client(config(max_retries=0), transport=transport) as client,
            pytest.raises(Dhis2Error) as raised,
        ):
            client.fetch_organisation_units()
        assert raised.value.category is IntegrationErrorCategory.MALFORMED_RESPONSE

    def test_an_oversized_response_is_refused(self) -> None:
        """Refused while streaming, so the body never lands in memory - a worker
        that dies on memory takes every other queued job with it."""
        huge = json.dumps({"organisationUnits": [{"id": "X" * 5000, "name": "Y" * 5000}]})
        transport = scripted([httpx.Response(200, text=huge)])

        with (
            Dhis2Client(config(max_response_bytes=1024, max_retries=0), transport=transport) as c,
            pytest.raises(Dhis2Error) as raised,
        ):
            c.fetch_organisation_units()

        assert raised.value.category is IntegrationErrorCategory.RESPONSE_TOO_LARGE
        assert "narrow the requested scope" in str(raised.value)


class TestPagination:
    def test_pages_are_followed_until_the_pager_says_stop(self) -> None:
        transport = scripted(
            [
                org_unit_page(1, 3, [{"id": "A", "name": "Alpha"}]),
                org_unit_page(2, 3, [{"id": "B", "name": "Beta"}]),
                org_unit_page(3, 3, [{"id": "C", "name": "Gamma"}]),
            ]
        )
        with Dhis2Client(config(), transport=transport) as client:
            cursor: str | None = None
            seen: list[str] = []
            for _ in range(10):
                page = client.fetch_organisation_units(cursor)
                seen.extend(unit.remote_id for unit in page.records)
                if page.is_last:
                    break
                cursor = page.next_cursor

        assert seen == ["A", "B", "C"]

    def test_the_last_page_reports_no_cursor(self) -> None:
        transport = scripted([org_unit_page(2, 2, [{"id": "B", "name": "Beta"}])])
        with Dhis2Client(config(), transport=transport) as client:
            page = client.fetch_organisation_units("2")
        assert page.is_last
        assert page.next_cursor is None

    def test_a_response_with_no_pager_is_treated_as_a_single_page(self) -> None:
        transport = scripted([httpx.Response(200, json={"organisationUnits": []})])
        with Dhis2Client(config(), transport=transport) as client:
            page = client.fetch_organisation_units()
        assert page.is_last


class TestDataValues:
    def test_a_pull_without_an_org_unit_is_refused(self) -> None:
        """MARS will not pull a whole DHIS2 instance implicitly."""
        with (
            Dhis2Client(config(), transport=scripted([])) as client,
            pytest.raises(Dhis2Error) as raised,
        ):
            client.fetch_data_values(
                RemoteScope(period_start=date(2026, 3, 1), period_end=date(2026, 3, 31))
            )
        assert raised.value.category is IntegrationErrorCategory.MAPPING_INCOMPLETE

    def test_a_pull_without_a_period_is_refused(self) -> None:
        """An unbounded request cannot be resumed or fingerprinted."""
        with (
            Dhis2Client(config(), transport=scripted([])) as client,
            pytest.raises(Dhis2Error, match="period range"),
        ):
            client.fetch_data_values(RemoteScope(organisation_unit_remote_ids=("A",)))

    def test_a_blank_value_stays_blank_rather_than_becoming_zero(self) -> None:
        """The single distinction MARS spends the most effort preserving. It
        must not be decided at the seam."""
        transport = scripted(
            [
                httpx.Response(
                    200,
                    json={
                        "dataValues": [
                            {"dataElement": "DE1", "orgUnit": "A", "period": "202603", "value": ""},
                            {
                                "dataElement": "DE2",
                                "orgUnit": "A",
                                "period": "202603",
                                "value": "0",
                            },
                        ]
                    },
                )
            ]
        )
        with Dhis2Client(config(), transport=transport) as client:
            page = client.fetch_data_values(
                RemoteScope(
                    organisation_unit_remote_ids=("A",),
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 3, 31),
                )
            )

        assert page.records[0].value == ""
        assert page.records[1].value == "0"
        assert page.records[0].value != page.records[1].value

    def test_org_units_are_requested_in_groups_of_the_page_size(self) -> None:
        transport = scripted([httpx.Response(200, json={"dataValues": []})] * 3)
        scope = RemoteScope(
            organisation_unit_remote_ids=("A", "B", "C", "D", "E"),
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        )
        with Dhis2Client(config(page_size=2), transport=transport) as client:
            page = client.fetch_data_values(scope)
            assert not page.is_last
            assert "1 of 3" in page.page_description


class TestGeometryIsOfferedNeverInvented:
    def test_a_point_is_read(self) -> None:
        transport = scripted(
            [
                org_unit_page(
                    1,
                    1,
                    [
                        {
                            "id": "A",
                            "name": "Alpha",
                            "geometry": {"type": "Point", "coordinates": [32.5, 0.3]},
                        }
                    ],
                )
            ]
        )
        with Dhis2Client(config(), transport=transport) as client:
            unit = client.fetch_organisation_units().records[0]
        assert (unit.latitude, unit.longitude) == (0.3, 32.5)

    def test_a_polygon_does_not_become_a_centroid(self) -> None:
        """Reducing a polygon to a point would manufacture a facility location
        nobody surveyed - the opposite of MARS's rule that an unvalidated
        coordinate is stored as absent."""
        transport = scripted(
            [
                org_unit_page(
                    1,
                    1,
                    [
                        {
                            "id": "A",
                            "name": "Alpha",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[32.0, 0.0], [33.0, 0.0], [33.0, 1.0]]],
                            },
                        }
                    ],
                )
            ]
        )
        with Dhis2Client(config(), transport=transport) as client:
            unit = client.fetch_organisation_units().records[0]
        assert unit.latitude is None
        assert unit.longitude is None


class TestScopeIdentity:
    def test_the_same_scope_in_a_different_order_is_the_same_run(self) -> None:
        """Otherwise a scheduled pull creates a new run whenever the caller
        happens to build its list differently."""
        left = RemoteScope(
            organisation_unit_remote_ids=("A", "B", "C"),
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        )
        right = RemoteScope(
            organisation_unit_remote_ids=("C", "A", "B"),
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        )
        resource = IntegrationResource.AGGREGATE_DATA_VALUES
        assert scope_fingerprint(resource, left) == scope_fingerprint(resource, right)

    def test_a_different_period_is_a_different_run(self) -> None:
        resource = IntegrationResource.AGGREGATE_DATA_VALUES
        march = RemoteScope(
            organisation_unit_remote_ids=("A",),
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        )
        april = RemoteScope(
            organisation_unit_remote_ids=("A",),
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
        )
        assert scope_fingerprint(resource, march) != scope_fingerprint(resource, april)

    def test_the_same_scope_under_a_different_resource_is_a_different_run(self) -> None:
        scope = RemoteScope(organisation_unit_remote_ids=("A",))
        assert scope_fingerprint(
            IntegrationResource.AGGREGATE_DATA_VALUES, scope
        ) != scope_fingerprint(IntegrationResource.ANALYTICS_QUERY, scope)

    def test_the_description_carries_no_credential(self) -> None:
        described = describe_scope(
            RemoteScope(
                organisation_unit_remote_ids=("A", "B"),
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
            )
        )
        assert "2 org unit(s)" in described
        assert "@" not in described


class TestConfigurationIsOptional:
    def test_a_disabled_deployment_has_no_config_rather_than_an_error(self) -> None:
        """A MARS deployment that never talks to DHIS2 is an ordinary case."""
        settings = Settings(
            database_url="postgresql+psycopg://mars:pw@db:5432/mars",
            dhis2_enabled=False,
            dhis2_base_url="https://dhis2.example.org",
        )
        assert Dhis2Config.from_settings(settings) is None

    def test_enabled_without_a_url_is_also_unconfigured(self) -> None:
        settings = Settings(
            database_url="postgresql+psycopg://mars:pw@db:5432/mars",
            dhis2_enabled=True,
        )
        assert Dhis2Config.from_settings(settings) is None

    def test_tls_verification_is_on_by_default(self) -> None:
        settings = Settings(database_url="postgresql+psycopg://mars:pw@db:5432/mars")
        assert settings.dhis2_verify_tls is True

    def test_outbound_push_is_off_by_default(self) -> None:
        """Reading another system's data and writing into it are different
        authorities. MARS must not acquire the second by being granted the
        first."""
        settings = Settings(database_url="postgresql+psycopg://mars:pw@db:5432/mars")
        assert settings.dhis2_push_enabled is False
        assert settings.dhis2_push_dataset_uid is None
