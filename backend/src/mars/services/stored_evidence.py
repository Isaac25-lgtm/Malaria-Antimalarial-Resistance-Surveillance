"""Stored OPD evidence for recurrence analysis.

Loads the canonical evidence a stored-source analysis run evaluates, with the
coverage that evidence actually has and the facility context its projections
need. Three rules:

* **Scope first.** Encounters at facilities outside the frozen scope never load.
* **Whole histories.** Every patient with a positive in the reporting period is
  loaded with all of their in-scope encounters back to the lookback start, so a
  chain that began before the period is visible and context is never removed.
* **Coverage from provenance.** A facility is covered only across the register
  windows of its completed encounter imports (see
  :func:`~mars.services.patient_surveillance.stored_coverage`).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import ImportBatchStatus, MalariaTestResult
from mars.domain.ingestion import ImportBatch
from mars.domain.longitudinal import CanonicalEncounter, EvidenceCoverage
from mars.domain.organisation import Facility
from mars.services.patient_surveillance import stored_coverage
from mars.services.stored_encounter_adapter import adapt_stored_encounters


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    encounters: tuple[CanonicalEncounter, ...]
    coverage: EvidenceCoverage
    facility_names: dict[str, str]
    #: Facility -> mapped subcounty (or district) geography unit, where a
    #: verified crosswalk exists. Unmapped facilities are simply absent.
    facility_geography: dict[str, str]


def load_stored_evidence(
    session: Session,
    *,
    facilities: set[uuid.UUID] | None,
    period_start: date,
    period_end: date,
    lookback_start: date,
) -> StoredEvidence:
    candidates = (
        select(OpdEncounter.patient_reference_id)
        .join(OpdEncounterTest)
        .where(
            OpdEncounter.patient_reference_id.is_not(None),
            OpdEncounterTest.result == MalariaTestResult.POSITIVE,
            OpdEncounter.encounter_date >= period_start,
            OpdEncounter.encounter_date <= period_end,
        )
    )
    statement = select(OpdEncounter).options(
        selectinload(OpdEncounter.tests),
        selectinload(OpdEncounter.diagnoses),
        selectinload(OpdEncounter.prescriptions),
        selectinload(OpdEncounter.referrals),
    )
    if facilities is not None:
        candidates = candidates.where(OpdEncounter.facility_id.in_(facilities))
        statement = statement.where(OpdEncounter.facility_id.in_(facilities))
    rows = list(
        session.execute(
            statement.where(
                OpdEncounter.patient_reference_id.in_(candidates.distinct()),
                OpdEncounter.encounter_date >= lookback_start,
                OpdEncounter.encounter_date <= period_end,
            )
        )
        .unique()
        .scalars()
    )
    requested = requested_facilities(session, facilities, lookback_start, period_end)
    coverage = stored_coverage(
        import_windows(session, facilities, lookback_start, period_end),
        requested,
        lookback_start,
        period_end,
    )
    involved = {row.facility_id for row in rows} | {uuid.UUID(item) for item in requested}
    names, geography = facility_context(session, involved)
    return StoredEvidence(adapt_stored_encounters(rows).encounters, coverage, names, geography)


def import_windows(
    session: Session, facilities: set[uuid.UUID] | None, start: date, end: date
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
    for facility_id, opened, closed in session.execute(statement):
        windows[str(facility_id)].append((opened, closed))
    return windows


def requested_facilities(
    session: Session, facilities: set[uuid.UUID] | None, start: date, end: date
) -> set[str]:
    """Every facility whose evidence an analysis over this scope depends on."""
    if facilities is not None:
        return {str(item) for item in facilities}
    found: set[uuid.UUID | None] = set()
    found.update(
        session.execute(
            select(OpdEncounter.facility_id)
            .where(OpdEncounter.encounter_date >= start, OpdEncounter.encounter_date <= end)
            .distinct()
        ).scalars()
    )
    found.update(
        session.execute(
            select(ImportBatch.facility_id)
            .where(
                ImportBatch.import_domain == "encounter",
                ImportBatch.facility_id.is_not(None),
                ImportBatch.register_opened_on <= end,
                ImportBatch.register_closed_on >= start,
            )
            .distinct()
        ).scalars()
    )
    return {str(item) for item in found if item is not None}


def facility_context(
    session: Session, facility_ids: Iterable[uuid.UUID]
) -> tuple[dict[str, str], dict[str, str]]:
    """Facility names, and each facility's mapped subcounty or district."""
    wanted = set(facility_ids)
    if not wanted:
        return {}, {}
    names: dict[str, str] = {}
    geography: dict[str, str] = {}
    for facility_id, name, subcounty, district in session.execute(
        select(
            Facility.id,
            Facility.raw_name,
            Facility.subcounty_geography_unit_id,
            Facility.district_geography_unit_id,
        ).where(Facility.id.in_(wanted))
    ):
        names[str(facility_id)] = name
        area = subcounty or district
        if area is not None:
            geography[str(facility_id)] = str(area)
    return names, geography


__all__ = [
    "StoredEvidence",
    "facility_context",
    "import_windows",
    "load_stored_evidence",
    "requested_facilities",
]
