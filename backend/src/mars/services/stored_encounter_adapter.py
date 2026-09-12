"""Stored OPD encounters to the canonical encounter contract.

The same :class:`~mars.domain.longitudinal.CanonicalEncounter` the live Tracker
adapter produces, built from normalised local ``OpdEncounter`` rows. Tests belong
to their encounter row, so that row is their verified clinical parent.
Prescriptions are recorded as prescribed - never as dispensed - and dose,
frequency and duration are passed through as recorded, never computed.

The caller supplies encounters already restricted to the principal's authorised
facilities; this adapter never widens a scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from types import MappingProxyType

from mars.domain.encounter import OpdEncounter, OpdEncounterPrescription
from mars.domain.enums import (
    AgeUnit,
    AttendanceType,
    FeverStatus,
    MalariaTestMethod,
    MalariaTestResult,
    Sex,
)
from mars.domain.longitudinal import (
    UNRESOLVED_OUTCOMES,
    AdaptedEvidence,
    Availability,
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    LabOutcome,
    Medication,
    MedicationEvidence,
    SourceRecord,
)

STORED_NAMESPACE = "mars:opd-encounter"
STORED_MAPPING_VERSION = "opd-002"

#: Display grouping for cohort filters, not a clinical classification.
AGE_GROUPS: tuple[str, ...] = ("under_5", "5_to_14", "15_and_over")

_OUTCOMES: dict[MalariaTestResult, LabOutcome] = {
    MalariaTestResult.POSITIVE: LabOutcome.POSITIVE,
    MalariaTestResult.NEGATIVE: LabOutcome.NEGATIVE,
    MalariaTestResult.NOT_DONE: LabOutcome.NOT_DONE,
    MalariaTestResult.NOT_APPLICABLE: LabOutcome.NOT_DONE,
    MalariaTestResult.UNKNOWN: LabOutcome.UNMAPPED,
}
_METHODS: dict[MalariaTestMethod, str] = {
    MalariaTestMethod.RDT: "rdt",
    MalariaTestMethod.MICROSCOPY: "microscopy",
}


def age_group(value: int | None, unit: AgeUnit | None) -> str | None:
    """A cohort band, or ``None`` when age was not recorded. Never zero years."""
    if value is None or unit is None:
        return None
    years = float(value)
    if unit is AgeUnit.MONTHS:
        years = value / 12
    elif unit is AgeUnit.DAYS:
        years = value / 365.25
    if years < 5:
        return "under_5"
    if years < 15:
        return "5_to_14"
    return "15_and_over"


def _number(value: object) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).normalize(), "f")


def _medication(item: OpdEncounterPrescription, namespace: str) -> Medication:
    dose = _number(item.units_per_dose)
    frequency = _number(item.doses_per_day)
    duration = _number(item.days)
    quantity = _number(item.total_units)

    def seen(value: str | None) -> Availability:
        return Availability.RECORDED if value is not None else Availability.NOT_RECORDED

    return Medication(
        source=SourceRecord(namespace, f"opd-rx:{item.id}", "prescription"),
        name=item.drug_name_raw or item.prescription_raw,
        evidence=MedicationEvidence.PRESCRIBED,
        dose=dose,
        frequency=frequency,
        duration_days=duration,
        quantity=quantity,
        availability=MappingProxyType(
            {
                "name": Availability.RECORDED,
                "dose": seen(dose),
                "frequency": seen(frequency),
                "duration": seen(duration),
                "quantity": seen(quantity),
                "formulation": Availability.NOT_MAPPED,
                "dispensing": Availability.NOT_MAPPED,
            }
        ),
    )


def adapt_stored_encounters(
    encounters: Sequence[OpdEncounter], *, namespace: str = STORED_NAMESPACE
) -> AdaptedEvidence:
    canonical: list[CanonicalEncounter] = []
    unresolved = 0
    for encounter in sorted(encounters, key=lambda e: (e.encounter_date, str(e.id))):
        key = f"opd:{encounter.id}"
        # A stored OPD register records a test on its encounter row, and that
        # row's date is the date the register gives the test. It is assigned
        # here explicitly, as the test's own evidence date, rather than being
        # read back from the encounter by the engine.
        tests = tuple(
            ClinicalTest(
                SourceRecord(namespace, f"opd-test:{test.id}", "test"),
                _METHODS.get(test.method, "unknown"),
                _OUTCOMES[test.result],
                observed_on=encounter.encounter_date,
                precision=DatePrecision.DAY,
            )
            for test in sorted(encounter.tests, key=lambda t: (t.sequence, str(t.id)))
        )
        unresolved += sum(1 for test in tests if test.outcome in UNRESOLVED_OUTCOMES)
        medications = tuple(
            _medication(item, namespace)
            for item in sorted(encounter.prescriptions, key=lambda p: (p.sequence, str(p.id)))
            if not item.is_device
        )
        fever = {FeverStatus.YES: "yes", FeverStatus.NO: "no"}.get(encounter.fever_present)
        revision = encounter.updated_at.isoformat() if encounter.updated_at else None
        canonical.append(
            CanonicalEncounter(
                encounter_key=key,
                namespace=namespace,
                patient_key=(
                    str(encounter.patient_reference_id)
                    if encounter.patient_reference_id is not None
                    else None
                ),
                facility_ref=str(encounter.facility_id),
                encounter_date=encounter.encounter_date,
                date_precision=DatePrecision.DAY,
                parent_ref=str(encounter.id),
                parent_retrieved=True,
                tests=tests,
                diagnoses=tuple(
                    d.diagnosis_raw
                    for d in sorted(encounter.diagnoses, key=lambda d: (d.sequence, str(d.id)))
                ),
                fever=fever,
                attendance=(
                    None
                    if encounter.attendance_type is AttendanceType.UNKNOWN
                    else encounter.attendance_type.value
                ),
                referrals=tuple(f"{r.direction.value} referral" for r in encounter.referrals),
                medications=medications,
                sources=(SourceRecord(namespace, key, "opd_encounter", revision),),
                sex=None if encounter.sex is Sex.UNKNOWN else encounter.sex.value,
                age_group=age_group(encounter.age_value, encounter.age_unit),
                availability=MappingProxyType(
                    {
                        "visit": Availability.RECORDED,
                        "medications": Availability.RECORDED,
                        "fever": (
                            Availability.RECORDED
                            if fever is not None
                            else Availability.NOT_RECORDED
                        ),
                    }
                ),
                mapping_version=encounter.ingest_method_version or STORED_MAPPING_VERSION,
            )
        )
    return AdaptedEvidence(
        encounters=tuple(canonical),
        unlinked_medications=MappingProxyType({}),
        duplicate_groups=(),
        unresolved_results=unresolved,
        namespace=namespace,
        mapping_version=STORED_MAPPING_VERSION,
    )


__all__ = [
    "AGE_GROUPS",
    "STORED_MAPPING_VERSION",
    "STORED_NAMESPACE",
    "adapt_stored_encounters",
    "age_group",
]
