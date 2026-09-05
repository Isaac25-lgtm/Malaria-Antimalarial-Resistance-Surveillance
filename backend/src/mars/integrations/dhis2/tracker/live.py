"""Live-session runner for the one-facility controlled Tracker preview."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from mars.core.settings import Settings
from mars.domain.enums import MalariaTestResult
from mars.ingestion.encounters.validation import EncounterValidator
from mars.integrations.dhis2.tracker.client import BoundedTrackerEventClient, TrackerClientConfig
from mars.integrations.dhis2.tracker.mapping import load_approved_tracker_mapping
from mars.integrations.dhis2.tracker.translate import TrackerEncounterTranslator
from mars.integrations.ports import RemoteEvent, RemoteScope, iterate_pages


def build_live_tracker_preview_runner(
    settings: Settings, *, project_root: Path
) -> Callable[[str, str, str, date, date], dict[str, Any]]:
    """Return the source-specific callback injected only by application wiring."""

    def run(
        username: str,
        password: str,
        facility_uid: str,
        period_start: date,
        period_end: date,
    ) -> dict[str, Any]:
        configured_path = settings.dhis2_tracker_mapping_path
        if not configured_path:
            raise RuntimeError(
                "MARS_DHIS2_TRACKER_MAPPING_PATH is not configured; an approved mapping is required"
            )
        mapping_path = Path(configured_path)
        if not mapping_path.is_absolute():
            mapping_path = project_root / mapping_path
        mapping = load_approved_tracker_mapping(mapping_path)
        config = TrackerClientConfig(
            base_url=settings.dhis2_login_base_url,
            username=username,
            password=password,
            timeout_seconds=settings.dhis2_login_timeout_seconds,
            page_size=settings.dhis2_tracker_preview_page_size,
            max_records=settings.dhis2_tracker_preview_max_records,
            max_response_bytes=settings.dhis2_tracker_max_response_bytes,
        )
        scope = RemoteScope(
            organisation_unit_remote_ids=(facility_uid,),
            period_start=period_start,
            period_end=period_end,
            extra={
                "programme_uid": mapping.programme_uid,
                "program_stage_uid": mapping.program_stage_uid,
            },
        )
        translator = TrackerEncounterTranslator(mapping)
        validator = EncounterValidator()
        events: list[RemoteEvent] = []
        with BoundedTrackerEventClient(
            config,
            authorized_org_unit_uids=frozenset({facility_uid}),
        ) as client:
            for page in iterate_pages(
                lambda cursor: client.fetch_events(scope, cursor), max_pages=10
            ):
                events.extend(event for event in page.records if isinstance(event, RemoteEvent))

        coverage = dict.fromkeys(mapping.data_elements, 0)
        loadable = 0
        positive = 0
        people: set[str] = set()
        for line_number, event in enumerate(events, start=1):
            people.add(event.person_remote_id)
            for concept, uid in mapping.data_elements.items():
                if uid is not None and event.data_values.get(uid) not in (None, ""):
                    coverage[concept] += 1
            row = translator.translate(event, line_number=line_number)
            outcome = validator.validate(row)
            if outcome.is_loadable and outcome.encounter is not None:
                loadable += 1
                if any(
                    result is MalariaTestResult.POSITIVE for _, result in outcome.encounter.tests
                ):
                    positive += 1

        return {
            "status": "validated_preview",
            "facility_uid": facility_uid,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "retrieved_event_count": len(events),
            "unique_patient_count": len(people),
            "loadable_event_count": loadable,
            "invalid_event_count": len(events) - loadable,
            "positive_event_count": positive,
            "field_coverage": coverage,
            "mapping_schema_version": mapping.schema_version,
            "patient_data_retrieved": True,
            "patient_rows_returned": False,
            "persisted": False,
        }

    return run


__all__ = ["build_live_tracker_preview_runner"]
