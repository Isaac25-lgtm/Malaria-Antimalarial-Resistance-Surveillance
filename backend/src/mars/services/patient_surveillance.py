"""Scope-safe pseudonymous patient evidence for the live pilot."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mars.core.errors import FeatureDisabledError, NotFoundError
from mars.core.settings import Settings
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import MalariaTestResult
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService

_ALIAS_PURPOSE = b"MARS patient display alias v1\x00"


class PatientSurveillanceService:
    """Read pseudonymous longitudinal evidence without touching the vault."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._scope = AnalyticsQueryService(session)

    def patients_of_interest(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_from: date | None,
        period_to: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        key = self._display_key()
        statement = (
            self._encounters_in_scope(principal)
            .join(OpdEncounterTest)
            .where(
                OpdEncounter.patient_reference_id.is_not(None),
                OpdEncounterTest.result == MalariaTestResult.POSITIVE,
            )
        )
        if period_from is not None:
            statement = statement.where(OpdEncounter.encounter_date >= period_from)
        if period_to is not None:
            statement = statement.where(OpdEncounter.encounter_date <= period_to)
        encounters = (
            self._session.execute(
                statement.order_by(OpdEncounter.encounter_date.desc()).limit(5000)
            )
            .unique()
            .scalars()
        )
        grouped: dict[uuid.UUID, list[OpdEncounter]] = defaultdict(list)
        for encounter in encounters:
            if encounter.patient_reference_id is not None:
                grouped[encounter.patient_reference_id].append(encounter)

        rows: list[dict[str, Any]] = []
        for patient_id, positives in grouped.items():
            ordered = sorted(positives, key=lambda item: item.encounter_date)
            latest = ordered[-1]
            first = ordered[0]
            rows.append(
                {
                    "patient_reference_id": patient_id,
                    "mars_patient_id": patient_alias(
                        patient_id,
                        key=key,
                        key_version=self._settings.patient_display_key_version,
                    ),
                    "sex": latest.sex.value,
                    "age_value": latest.age_value,
                    "age_unit": latest.age_unit.value if latest.age_unit else None,
                    "first_positive_on": first.encounter_date,
                    "latest_positive_on": latest.encounter_date,
                    "positive_encounter_count": len(ordered),
                    "interval_days": (
                        (latest.encounter_date - ordered[-2].encounter_date).days
                        if len(ordered) > 1
                        else None
                    ),
                    "facility_id": latest.facility_id,
                    "facility_name": latest.facility.raw_name,
                    "classification": (
                        "repeat_positive_input" if len(ordered) > 1 else "positive_encounter"
                    ),
                }
            )
        rows.sort(
            key=lambda item: (item["latest_positive_on"], item["mars_patient_id"]), reverse=True
        )
        return rows[:limit]

    def timeline(
        self, principal: AuthenticatedPrincipal, *, patient_reference_id: uuid.UUID
    ) -> dict[str, Any]:
        key = self._display_key()
        statement = self._encounters_in_scope(principal).where(
            OpdEncounter.patient_reference_id == patient_reference_id
        )
        encounters = list(
            self._session.execute(statement.order_by(OpdEncounter.encounter_date.desc()))
            .unique()
            .scalars()
        )
        if not encounters:
            raise NotFoundError("No patient evidence is available within your authorised scope")
        return {
            "patient_reference_id": patient_reference_id,
            "mars_patient_id": patient_alias(
                patient_reference_id,
                key=key,
                key_version=self._settings.patient_display_key_version,
            ),
            "identity_available": False,
            "identity_detail": (
                "Direct identity is available only through a separately permissioned, "
                "reason-required and audited source lookup."
            ),
            "encounters": [self._encounter_row(encounter) for encounter in encounters],
        }

    def _encounters_in_scope(self, principal: AuthenticatedPrincipal) -> Any:
        statement = select(OpdEncounter).options(
            selectinload(OpdEncounter.facility),
            selectinload(OpdEncounter.tests),
            selectinload(OpdEncounter.diagnoses),
            selectinload(OpdEncounter.prescriptions),
            selectinload(OpdEncounter.referrals),
        )
        facilities = self._scope.facility_ids(principal)
        if facilities is not None:
            statement = statement.where(OpdEncounter.facility_id.in_(facilities))
        return statement

    def _display_key(self) -> bytes:
        configured = self._settings.patient_display_key
        if configured is None or not configured.get_secret_value():
            raise FeatureDisabledError(
                "Patient evidence requires MARS_PATIENT_DISPLAY_KEY; no fallback alias key is used."
            )
        return configured.get_secret_value().encode("utf-8")

    @staticmethod
    def _encounter_row(encounter: OpdEncounter) -> dict[str, Any]:
        return {
            "encounter_id": encounter.id,
            "encounter_date": encounter.encounter_date,
            "facility_id": encounter.facility_id,
            "facility_name": encounter.facility.raw_name,
            "sex": encounter.sex.value,
            "age_value": encounter.age_value,
            "age_unit": encounter.age_unit.value if encounter.age_unit else None,
            "fever_present": encounter.fever_present.value,
            "attendance_type": encounter.attendance_type.value,
            "tests": [
                {"method": test.method.value, "result": test.result.value}
                for test in encounter.tests
            ],
            "diagnoses": [item.diagnosis_raw for item in encounter.diagnoses],
            "treatments": [item.prescription_raw for item in encounter.prescriptions],
            "source_system": encounter.source_system,
        }


def patient_alias(patient_reference_id: uuid.UUID, *, key: bytes, key_version: str) -> str:
    """A stable 100-bit HMAC alias; never a truncation of a source identifier."""
    digest = hmac.new(key, _ALIAS_PURPOSE + patient_reference_id.bytes, hashlib.sha256).digest()
    token = base64.b32encode(digest[:13]).decode("ascii").rstrip("=")
    return f"MARS-PT-{key_version.upper()}-{token}"


__all__ = ["PatientSurveillanceService", "patient_alias"]
