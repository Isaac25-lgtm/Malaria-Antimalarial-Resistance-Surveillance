from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from mars.domain.enums import IntegrationErrorCategory
from mars.ingestion.encounters.validation import EncounterValidator
from mars.integrations.dhis2.client import Dhis2Error
from mars.integrations.dhis2.tracker.client import (
    BoundedTrackerEventClient,
    TrackerClientConfig,
)
from mars.integrations.dhis2.tracker.mapping import (
    ApprovedTrackerMapping,
    TrackerMappingError,
    load_approved_tracker_mapping,
)
from mars.integrations.dhis2.tracker.translate import TrackerEncounterTranslator
from mars.integrations.ports import RemoteEvent, RemoteScope

FACILITY = "aBc12345678"
PROGRAMME = "pRogram1234"
STAGE = "sTage123456"
TEI = "tEntity12345"
EVENT = "eVent123456"
RESULT = "rEsult12345"
METHOD = "mEthod12345"
TREATMENT = "tReat123456"


def _config(**overrides: object) -> TrackerClientConfig:
    values: dict[str, object] = {
        "base_url": "https://eregisters.health.go.ug",
        "username": "pilot-user",
        "password": "not-a-real-secret",
        "page_size": 2,
        "max_records": 4,
    }
    values.update(overrides)
    return TrackerClientConfig(**values)  # type: ignore[arg-type]


def _scope(**extra: str) -> RemoteScope:
    return RemoteScope(
        organisation_unit_remote_ids=(FACILITY,),
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 14),
        extra={"programme_uid": PROGRAMME, "program_stage_uid": STAGE, **extra},
    )


def _event_payload(event: str = EVENT) -> dict[str, object]:
    return {
        "event": event,
        "trackedEntity": TEI,
        "program": PROGRAMME,
        "programStage": STAGE,
        "orgUnit": FACILITY,
        "status": "COMPLETED",
        "occurredAt": "2026-08-03T10:20:00.000Z",
        "updatedAt": "2026-08-03T11:20:00.000Z",
        "dataValues": [
            {"dataElement": RESULT, "value": "POS"},
            {"dataElement": METHOD, "value": "RDT"},
        ],
    }


class TestBoundedTrackerEventClient:
    def test_fetches_only_one_facility_and_projects_event(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"instances": [_event_payload()], "pager": {"page": 1, "pageCount": 1}},
            )

        with BoundedTrackerEventClient(
            _config(),
            authorized_org_unit_uids=frozenset({FACILITY}),
            transport=httpx.MockTransport(handler),
        ) as client:
            page = client.fetch_events(_scope())

        assert len(page.records) == 1
        event = page.records[0]
        assert isinstance(event, RemoteEvent)
        assert event.person_remote_id == TEI
        assert event.data_values == {RESULT: "POS", METHOD: "RDT"}
        assert page.is_last
        assert len(captured) == 1
        request = captured[0]
        assert request.method == "GET"
        assert request.url.path == "/api/tracker/events"
        assert request.url.params["orgUnit"] == FACILITY
        assert request.url.params["ouMode"] == "SELECTED"
        assert request.url.params["occurredAfter"] == "2026-08-01"
        assert request.url.params["occurredBefore"] == "2026-08-14"
        assert "attributes" not in request.url.params["fields"]
        assert "relationships" not in request.url.params["fields"]

    @pytest.mark.parametrize(
        ("scope", "category"),
        [
            (
                RemoteScope(
                    organisation_unit_remote_ids=(FACILITY, "bCd12345678"),
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 2),
                    extra={"programme_uid": PROGRAMME, "program_stage_uid": STAGE},
                ),
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
            ),
            (
                RemoteScope(
                    organisation_unit_remote_ids=("bCd12345678",),
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 2),
                    extra={"programme_uid": PROGRAMME, "program_stage_uid": STAGE},
                ),
                IntegrationErrorCategory.AUTHORISATION,
            ),
            (
                RemoteScope(
                    organisation_unit_remote_ids=(FACILITY,),
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 15),
                    extra={"programme_uid": PROGRAMME, "program_stage_uid": STAGE},
                ),
                IntegrationErrorCategory.MAPPING_INCOMPLETE,
            ),
        ],
    )
    def test_refuses_unbounded_or_unauthorised_scope(
        self, scope: RemoteScope, category: IntegrationErrorCategory
    ) -> None:
        with (
            BoundedTrackerEventClient(
                _config(),
                authorized_org_unit_uids=frozenset({FACILITY}),
                transport=httpx.MockTransport(lambda _: httpx.Response(500)),
            ) as client,
            pytest.raises(Dhis2Error) as caught,
        ):
            client.fetch_events(scope)
        assert caught.value.category is category

    def test_refuses_result_larger_than_hard_cap(self) -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"instances": [_event_payload()], "pager": {"total": 5, "pageCount": 3}},
            )
        )
        with (
            BoundedTrackerEventClient(
                _config(),
                authorized_org_unit_uids=frozenset({FACILITY}),
                transport=transport,
            ) as client,
            pytest.raises(Dhis2Error) as caught,
        ):
            client.fetch_events(_scope())
        assert caught.value.category is IntegrationErrorCategory.RESPONSE_TOO_LARGE

    def test_incremental_marker_is_sent_but_not_logged_or_returned(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"instances": []})

        with BoundedTrackerEventClient(
            _config(),
            authorized_org_unit_uids=frozenset({FACILITY}),
            transport=httpx.MockTransport(handler),
        ) as client:
            client.fetch_events(_scope(updated_after="2026-08-14T00:00:00Z"))
        assert requests[0].url.params["updatedAfter"] == "2026-08-14T00:00:00Z"


class TestApprovedTrackerMapping:
    def test_requires_explicit_approval_and_all_concepts(self, tmp_path: Path) -> None:
        path = tmp_path / "mapping.json"
        path.write_text(json.dumps({"schema_version": "1.0", "status": "draft"}))
        with pytest.raises(TrackerMappingError, match="unapproved"):
            load_approved_tracker_mapping(path)

    def test_translates_without_direct_identity_in_redacted_payload(self) -> None:
        mapping = ApprovedTrackerMapping(
            schema_version="1.0",
            programme_uid=PROGRAMME,
            program_stage_uid=STAGE,
            data_elements={
                "fever_present": None,
                "malaria_test_method": METHOD,
                "malaria_test_result": RESULT,
                "diagnosis": None,
                "treatment": TREATMENT,
                "referral": None,
                "attendance_type": None,
                "age_value": None,
                "age_unit": None,
                "sex": None,
                "patient_category": None,
            },
            option_maps={
                "malaria_test_method": {"RDT": "rdt"},
                "malaria_test_result": {"POS": "positive"},
            },
            approved_by="Ministry reviewer",
            approved_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
        event = RemoteEvent(
            remote_id=EVENT,
            person_remote_id=TEI,
            programme_remote_id=PROGRAMME,
            programme_stage_remote_id=STAGE,
            organisation_unit_remote_id=FACILITY,
            occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
            updated_at=None,
            status="COMPLETED",
            data_values={METHOD: "RDT", RESULT: "POS", TREATMENT: "AL 6 doses"},
        )

        row = TrackerEncounterTranslator(mapping).translate(event, line_number=1)
        assert row.identity.identifier_value == f"dhis2:tracked-entity:{TEI}"
        assert "trackedEntity" not in row.redacted
        assert TEI not in json.dumps(row.redacted)
        validation = EncounterValidator().validate(row)
        assert validation.is_loadable
        assert validation.encounter is not None
        assert validation.encounter.tests[0][1].value == "positive"
        assert validation.encounter.prescriptions[0]["prescription_raw"] == "AL 6 doses"

    def test_unmapped_option_is_refused_without_echoing_patient_value(self) -> None:
        mapping = ApprovedTrackerMapping(
            schema_version="1.0",
            programme_uid=PROGRAMME,
            program_stage_uid=STAGE,
            data_elements=dict.fromkeys(
                (
                    "fever_present",
                    "malaria_test_method",
                    "malaria_test_result",
                    "diagnosis",
                    "treatment",
                    "referral",
                    "attendance_type",
                    "age_value",
                    "age_unit",
                    "sex",
                    "patient_category",
                ),
                None,
            )
            | {"malaria_test_result": RESULT},
            option_maps={"malaria_test_result": {"POS": "positive"}},
            approved_by="reviewer",
            approved_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
        event = RemoteEvent(
            remote_id=EVENT,
            person_remote_id=TEI,
            programme_remote_id=PROGRAMME,
            programme_stage_remote_id=STAGE,
            organisation_unit_remote_id=FACILITY,
            occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
            updated_at=None,
            status=None,
            data_values={RESULT: "unexpected-sensitive-value"},
        )
        with pytest.raises(TrackerMappingError) as caught:
            TrackerEncounterTranslator(mapping).translate(event, line_number=1)
        assert "unexpected-sensitive-value" not in str(caught.value)


def test_tracker_config_never_repr_credentials() -> None:
    config = _config(password="very-secret")
    assert "very-secret" not in repr(config)
