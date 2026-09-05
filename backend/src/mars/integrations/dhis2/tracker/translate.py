"""Translate approved DHIS2 Tracker events to MARS's inbound contract."""

from __future__ import annotations

from mars.ingestion.encounters.contract import InboundIdentity, InboundRow
from mars.integrations.dhis2.tracker.mapping import ApprovedTrackerMapping, TrackerMappingError
from mars.integrations.ports import RemoteEvent


class TrackerEncounterTranslator:
    """Maps only concepts named by an approved metadata mapping."""

    def __init__(self, mapping: ApprovedTrackerMapping) -> None:
        self._mapping = mapping

    def translate(self, event: RemoteEvent, *, line_number: int) -> InboundRow:
        if event.programme_remote_id != self._mapping.programme_uid:
            raise TrackerMappingError("Event programme does not match the approved mapping")
        if event.programme_stage_remote_id != self._mapping.program_stage_uid:
            raise TrackerMappingError("Event program stage does not match the approved mapping")

        raw: dict[str, object] = {
            "source_row_id": event.remote_id,
            "encounter_date": event.occurred_at.date().isoformat(),
            "date_source": "source_supplied",
        }
        simple = (
            "fever_present",
            "attendance_type",
            "sex",
            "patient_category",
        )
        for concept in simple:
            value = self._mapped_value(event, concept)
            if value is not None:
                raw[concept] = value

        age_value = self._mapped_value(event, "age_value")
        age_unit = self._mapped_value(event, "age_unit")
        if age_value is not None or age_unit is not None:
            raw["age"] = {"value": age_value, "unit": age_unit}

        method = self._mapped_value(event, "malaria_test_method")
        result = self._mapped_value(event, "malaria_test_result")
        if method is not None or result is not None:
            raw["tests"] = [{"method": method, "result": result}]

        diagnosis = self._mapped_value(event, "diagnosis")
        if diagnosis is not None:
            raw["diagnoses"] = [diagnosis]
        treatment = self._mapped_value(event, "treatment")
        if treatment is not None:
            raw["prescriptions"] = [treatment]
        referral = self._mapped_value(event, "referral")
        if referral is not None:
            raw["referrals"] = [{"direction": "out", "number": referral}]

        return InboundRow(
            source_row_id=event.remote_id,
            line_number=line_number,
            raw=raw,
            identity=InboundIdentity(
                identifier_type="unspecified_scheme",
                identifier_value=f"dhis2:tracked-entity:{event.person_remote_id}",
            ),
        )

    def _mapped_value(self, event: RemoteEvent, concept: str) -> str | None:
        uid = self._mapping.data_element_for(concept)
        if uid is None:
            return None
        return self._mapping.canonical_value(concept, event.data_values.get(uid))


__all__ = ["TrackerEncounterTranslator"]
