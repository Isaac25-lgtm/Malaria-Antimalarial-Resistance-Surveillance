"""Scope-safe pseudonymous patient evidence from stored encounters.

Repeat-positive status comes from the shared recurrence engine through the
stored-encounter adapter - the same engine the live snapshot uses - so the two
paths cannot give one patient two different meanings.

Two guarantees shape this module:

* **Coverage comes from import provenance.** Stored evidence is complete for a
  facility only across the register windows of its completed encounter
  imports. The earliest stored encounter is no evidence that nothing is
  missing, so it is never used as a coverage bound. A facility in scope without
  a completed import covering the whole span makes an absence of recurrence
  unreliable, and the finding says ``indeterminate`` rather than ``no``.
* **People are paged after the whole cohort is evaluated.** ``total`` counts
  every match whatever the page size. The cursor is bound to a fingerprint of
  the evaluated evidence, the definition, the period and the filter, so a page
  never silently shifts when any of them changes underneath it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.core.errors import (
    ConflictError,
    FeatureDisabledError,
    NotFoundError,
    ValidationFailedError,
)
from mars.core.settings import Settings
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import ImportBatchStatus, MalariaTestResult
from mars.domain.ingestion import ImportBatch
from mars.domain.longitudinal import (
    EXPLORATORY_PRESET,
    Determination,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    FrozenRecurrenceDefinition,
    RecurrenceEvaluation,
)
from mars.security.principal import AuthenticatedPrincipal
from mars.services.analytics_query import AnalyticsQueryService
from mars.services.stored_encounter_adapter import adapt_stored_encounters

_ALIAS_PURPOSE = b"MARS patient display alias v1\x00"
_CURSOR_VERSION = 1


class PatientSurveillanceService:
    """Read pseudonymous longitudinal evidence without touching the vault."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._scope = AnalyticsQueryService(session)

    def patients_page(
        self,
        principal: AuthenticatedPrincipal,
        *,
        period_from: date | None,
        period_to: date | None,
        limit: int,
        cursor: str | None = None,
        determination: str | None = None,
        definition: FrozenRecurrenceDefinition = EXPLORATORY_PRESET,
    ) -> dict[str, Any]:
        """One page of people with an eligible positive in the period.

        Every candidate's complete in-scope evidence back to the lookback start
        is loaded and evaluated first; only then is the page taken. Encounters at
        facilities outside the caller's scope never enter the calculation.
        """
        key = self._display_key()
        facilities = self._scope.facility_ids(principal)
        summary = definition_summary(definition)
        bounds = self._positive_bounds(facilities, period_from, period_to)
        if bounds is None:
            return {
                "items": [],
                "total": 0,
                "limit": limit,
                "period_start": period_from,
                "period_end": period_to,
                "coverage_status": "partial",
                "coverage_notes": ["No stored positive malaria evidence is in scope."],
                "definition": summary,
            }
        start, end = bounds
        lookback = start - timedelta(days=definition.maximum_window_days)

        candidates = (
            select(OpdEncounter.patient_reference_id)
            .join(OpdEncounterTest)
            .where(
                OpdEncounter.patient_reference_id.is_not(None),
                OpdEncounterTest.result == MalariaTestResult.POSITIVE,
                OpdEncounter.encounter_date >= start,
                OpdEncounter.encounter_date <= end,
            )
        )
        if facilities is not None:
            candidates = candidates.where(OpdEncounter.facility_id.in_(facilities))
        encounters = list(
            self._session.execute(
                self._encounters_in_scope(principal).where(
                    OpdEncounter.patient_reference_id.in_(candidates.distinct()),
                    OpdEncounter.encounter_date >= lookback,
                    OpdEncounter.encounter_date <= end,
                )
            )
            .unique()
            .scalars()
        )
        coverage = stored_coverage(
            self._import_windows(facilities, lookback, end),
            self._requested_facilities(facilities, lookback, end),
            lookback,
            end,
        )
        evaluated = evaluate_stored_patients(
            encounters,
            definition=definition,
            query=FrozenAnalysisQuery(start, end),
            coverage=coverage,
            key=key,
            key_version=self._settings.patient_display_key_version,
        )
        rows = evaluated.rows
        if determination is not None:
            rows = [row for row in rows if row["determination"] == determination]
        fingerprint = page_fingerprint(
            evaluated.evaluation.dataset_hash, definition.checksum(), start, end, determination
        )
        items, next_cursor, previous_cursor = page_rows(
            rows, limit=limit, cursor=cursor, fingerprint=fingerprint
        )
        return {
            "items": items,
            "total": len(rows),
            "limit": limit,
            "next_cursor": next_cursor,
            "previous_cursor": previous_cursor,
            "period_start": start,
            "period_end": end,
            "coverage_status": evaluated.evaluation.coverage_status,
            "coverage_notes": list(evaluated.evaluation.coverage_notes),
            "definition": summary,
        }

    def _positive_bounds(
        self, facilities: set[uuid.UUID] | None, start: date | None, end: date | None
    ) -> tuple[date, date] | None:
        """The requested period, or the span of positives in scope when open."""
        if start is not None and end is not None:
            return start, end
        statement = (
            select(func.min(OpdEncounter.encounter_date), func.max(OpdEncounter.encounter_date))
            .join(OpdEncounterTest)
            .where(OpdEncounterTest.result == MalariaTestResult.POSITIVE)
        )
        if facilities is not None:
            statement = statement.where(OpdEncounter.facility_id.in_(facilities))
        earliest, latest = self._session.execute(statement).one()
        if earliest is None or latest is None:
            return None
        return start or earliest, end or latest

    def _import_windows(
        self, facilities: set[uuid.UUID] | None, start: date, end: date
    ) -> dict[str, list[tuple[date, date]]]:
        """Register windows of completed encounter imports overlapping the span."""
        statement = select(
            ImportBatch.facility_id,
            ImportBatch.register_opened_on,
            ImportBatch.register_closed_on,
        ).where(
            ImportBatch.import_domain == "encounter",
            ImportBatch.import_status == ImportBatchStatus.COMPLETED,
            ImportBatch.facility_id.is_not(None),
            ImportBatch.register_opened_on.is_not(None),
            ImportBatch.register_closed_on.is_not(None),
            ImportBatch.register_opened_on <= end,
            ImportBatch.register_closed_on >= start,
        )
        if facilities is not None:
            statement = statement.where(ImportBatch.facility_id.in_(facilities))
        windows: dict[str, list[tuple[date, date]]] = defaultdict(list)
        for facility_id, opened, closed in self._session.execute(statement):
            windows[str(facility_id)].append((opened, closed))
        return windows

    def _requested_facilities(
        self, facilities: set[uuid.UUID] | None, start: date, end: date
    ) -> set[str]:
        """Every facility whose evidence the analysis depends on.

        For a facility-scoped caller that is the whole scope. For an unrestricted
        caller it is every facility with an encounter or an import in the span.
        """
        if facilities is not None:
            return {str(item) for item in facilities}
        with_encounters = select(OpdEncounter.facility_id).where(
            OpdEncounter.encounter_date >= start, OpdEncounter.encounter_date <= end
        )
        with_imports = select(ImportBatch.facility_id).where(
            ImportBatch.import_domain == "encounter",
            ImportBatch.facility_id.is_not(None),
            ImportBatch.register_opened_on <= end,
            ImportBatch.register_closed_on >= start,
        )
        found: set[uuid.UUID | None] = set()
        found.update(self._session.execute(with_encounters.distinct()).scalars())
        found.update(self._session.execute(with_imports.distinct()).scalars())
        return {str(item) for item in found if item is not None}

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


# ---------------------------------------------------------------------------
# Pure helpers: no database, no clock
# ---------------------------------------------------------------------------
def stored_coverage(
    windows: Mapping[str, Sequence[tuple[date, date]]],
    requested: Iterable[str],
    start: date,
    end: date,
) -> EvidenceCoverage:
    """Coverage of stored evidence from the register windows of completed imports.

    A facility is covered only when its windows join, without a gap, from
    ``start`` to ``end``. Every requested facility that is not covered is
    reported missing, which makes an absence of recurrence unreliable.
    """
    wanted = frozenset(requested)
    covered = frozenset(
        facility for facility in wanted if _spans(windows.get(facility, ()), start, end)
    )
    everything = [window for items in windows.values() for window in items]
    fully = bool(wanted) and covered == wanted
    return EvidenceCoverage(
        earliest_date=start if fully else min((w[0] for w in everything), default=None),
        latest_date=end if fully else max((w[1] for w in everything), default=None),
        complete=True,
        stages=frozenset({"stored_encounter"}),
        facility_refs=covered,
        requested_facility_refs=wanted,
        exclusions=() if fully else ("Stored import coverage is incomplete for the span.",),
    )


def _spans(windows: Sequence[tuple[date, date]], start: date, end: date) -> bool:
    reached = start
    for opened, closed in sorted(windows):
        if opened > reached:
            return False
        reached = max(reached, closed + timedelta(days=1))
        if reached > end:
            return True
    return reached > end


def page_fingerprint(
    dataset_hash: str,
    definition_checksum: str,
    start: date,
    end: date,
    determination: str | None,
) -> str:
    material = "|".join(
        (dataset_hash, definition_checksum, start.isoformat(), end.isoformat(), determination or "")
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def page_rows(
    rows: Sequence[dict[str, Any]], *, limit: int, cursor: str | None, fingerprint: str
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """One page of already complete rows, with cursors bound to ``fingerprint``."""
    offset = _decode_cursor(cursor, fingerprint) if cursor else 0
    items = list(rows[offset : offset + limit])
    next_cursor = (
        _encode_cursor(offset + limit, fingerprint) if offset + limit < len(rows) else None
    )
    previous_cursor = _encode_cursor(max(0, offset - limit), fingerprint) if offset > 0 else None
    return items, next_cursor, previous_cursor


def _encode_cursor(offset: int, fingerprint: str) -> str:
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "o": offset, "f": fingerprint}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, fingerprint: str) -> int:
    try:
        raw = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
        payload = json.loads(raw)
        version, offset, bound = payload["v"], int(payload["o"]), str(payload["f"])
    except (ValueError, KeyError, TypeError) as error:
        raise ValidationFailedError(
            "The page cursor is not valid; start again from the first page."
        ) from error
    if version != _CURSOR_VERSION or offset < 0:
        raise ValidationFailedError(
            "The page cursor is not valid; start again from the first page."
        )
    if not hmac.compare_digest(bound, fingerprint):
        raise ConflictError(
            "The patient evidence, definition, period or filter changed since this page "
            "was requested; start again from the first page."
        )
    return offset


def definition_summary(definition: FrozenRecurrenceDefinition) -> dict[str, Any]:
    return {
        "name": definition.name or "Unnamed recurrence definition",
        "minimum_gap_days": definition.minimum_gap_days,
        "maximum_window_days": definition.maximum_window_days,
        "minimum_positive_encounters": definition.minimum_positive_encounters,
        "same_day_policy": definition.same_day_policy.value,
        "engine_version": definition.engine_version,
        "exploratory": definition is EXPLORATORY_PRESET or definition.version is None,
    }


@dataclass(frozen=True, slots=True)
class StoredPatientEvaluation:
    rows: list[dict[str, Any]]
    evaluation: RecurrenceEvaluation


def evaluate_stored_patients(
    encounters: Sequence[OpdEncounter],
    *,
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    coverage: EvidenceCoverage,
    key: bytes,
    key_version: str,
) -> StoredPatientEvaluation:
    """Every patient with an eligible positive in the period, newest first.

    Pure: no database, no clock. The caller pages the returned rows, so a page
    size can never change who is counted.
    """
    by_key = {f"opd:{encounter.id}": encounter for encounter in encounters}
    evidence = adapt_stored_encounters(encounters)
    evaluation = evaluate_recurrence(evidence.encounters, definition, query, coverage)
    rows: list[dict[str, Any]] = []
    for finding in evaluation.patients:
        if not finding.in_denominator:
            continue
        counted = [d for d in finding.decisions if d.counted]
        days = [d.local_day for d in counted if d.local_day is not None]
        latest = by_key[finding.final_encounter or counted[-1].encounter_key]
        patient = uuid.UUID(finding.patient.reference)
        rows.append(
            {
                "patient_reference_id": patient,
                "mars_patient_id": patient_alias(patient, key=key, key_version=key_version),
                "sex": latest.sex.value,
                "age_value": latest.age_value,
                "age_unit": latest.age_unit.value if latest.age_unit else None,
                "first_positive_on": days[0],
                "latest_positive_on": days[-1],
                "positive_encounter_count": finding.eligible_positive_count,
                "interval_days": finding.legacy_interval_days,
                "chain_interval_days": (
                    finding.chain_interval.days if finding.chain_interval else None
                ),
                "adjacent_interval_days": [i.days for i in finding.adjacent_intervals],
                "anchor_interval_days": [i.days for i in finding.anchor_intervals],
                "determination": finding.determination.value,
                "facility_id": latest.facility_id,
                "facility_name": (
                    latest.facility.raw_name
                    if latest.facility is not None
                    else "Authorised facility"
                ),
                "classification": (
                    "repeat_positive_input"
                    if finding.determination is Determination.QUALIFIES
                    else "positive_encounter"
                ),
            }
        )
    rows.sort(key=lambda item: (item["latest_positive_on"], item["mars_patient_id"]), reverse=True)
    return StoredPatientEvaluation(rows, evaluation)


def summarise_stored_patients(
    encounters: Sequence[OpdEncounter],
    *,
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    coverage: EvidenceCoverage,
    key: bytes,
    key_version: str,
) -> list[dict[str, Any]]:
    """The rows of :func:`evaluate_stored_patients`."""
    return evaluate_stored_patients(
        encounters,
        definition=definition,
        query=query,
        coverage=coverage,
        key=key,
        key_version=key_version,
    ).rows


def patient_alias(patient_reference_id: uuid.UUID, *, key: bytes, key_version: str) -> str:
    """A stable 100-bit HMAC alias; never a truncation of a source identifier."""
    digest = hmac.new(key, _ALIAS_PURPOSE + patient_reference_id.bytes, hashlib.sha256).digest()
    token = base64.b32encode(digest[:13]).decode("ascii").rstrip("=")
    return f"MARS-PT-{key_version.upper()}-{token}"


__all__ = [
    "PatientSurveillanceService",
    "StoredPatientEvaluation",
    "definition_summary",
    "evaluate_stored_patients",
    "page_fingerprint",
    "page_rows",
    "patient_alias",
    "stored_coverage",
    "summarise_stored_patients",
]
