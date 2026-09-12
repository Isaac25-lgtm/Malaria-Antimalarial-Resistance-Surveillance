"""Retained evidence decodes into exactly the evidence that was fingerprinted.

All evidence here is invented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest

from mars.domain.longitudinal import (
    Availability,
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    EvidenceCoverage,
    LabOutcome,
    Medication,
    MedicationEvidence,
    QualityFlag,
    SourceRecord,
    evidence_fingerprint,
)
from mars.domain.longitudinal_codec import (
    decode_dataset,
    decode_encounter,
    encode_dataset,
    encode_encounter,
)

NS = "test:namespace"


def encounter(key: str = "visit:V1") -> CanonicalEncounter:
    record = SourceRecord(NS, "lab-1", "laboratory", "2026-08-03T08:00:00+00:00")
    return CanonicalEncounter(
        encounter_key=key,
        namespace=NS,
        patient_key="person-1",
        facility_ref="F1",
        encounter_date=date(2026, 8, 3),
        date_precision=DatePrecision.DAY,
        parent_ref="V1",
        parent_retrieved=True,
        tests=(
            ClinicalTest(
                record,
                "rdt",
                LabOutcome.POSITIVE,
                observed_on=date(2026, 8, 4),
                observed_at=datetime(2026, 8, 4, 6, 30, tzinfo=UTC),
                precision=DatePrecision.TIMESTAMP,
            ),
        ),
        diagnoses=("Malaria",),
        observations=("fever",),
        attributes=(("patient_type", "new"),),
        referrals=("referred out",),
        medications=(
            Medication(
                SourceRecord(NS, "med-1", "medicine"),
                "Artemether/Lumefantrine",
                MedicationEvidence.RECORDED,
                quantity="24",
                availability=MappingProxyType(
                    {"quantity": Availability.RECORDED, "dose": Availability.NOT_MAPPED}
                ),
            ),
        ),
        sources=(record,),
        sex="female",
        age_group="5_to_14",
        availability=MappingProxyType({"visit": Availability.RECORDED}),
        quality_flags=frozenset({QualityFlag.DATE_QUALITY_ISSUE}),
        mapping_version="2.0",
    )


COVERAGE = EvidenceCoverage(
    date(2026, 7, 1),
    date(2026, 8, 31),
    True,
    stages=frozenset({"laboratory", "medicine"}),
    facility_refs=frozenset({"F1"}),
    requested_facility_refs=frozenset({"F1", "F2"}),
    exclusions=("F2 not retrieved",),
)


def test_an_encounter_round_trips_exactly() -> None:
    original = encounter()
    assert decode_encounter(encode_encounter(original)) == original


def test_a_dataset_round_trips_and_keeps_its_fingerprint() -> None:
    encounters = (encounter("visit:V2"), encounter("visit:V1"))
    unlinked = {
        "person-1": (
            Medication(SourceRecord(NS, "m2", "medicine"), "AL", MedicationEvidence.RECORDED),
        )
    }
    decoded, coverage, medicines = decode_dataset(encode_dataset(encounters, COVERAGE, unlinked))
    assert coverage == COVERAGE
    assert sorted(decoded, key=lambda e: e.encounter_key) == sorted(
        encounters, key=lambda e: e.encounter_key
    )
    assert medicines["person-1"][0].name == "AL"
    assert evidence_fingerprint(decoded, coverage) == evidence_fingerprint(encounters, COVERAGE)


def test_an_unknown_codec_is_refused() -> None:
    payload = encode_dataset((encounter(),), COVERAGE)
    payload["codec"] = "something-else/9"
    with pytest.raises(ValueError, match="Unsupported evidence codec"):
        decode_dataset(payload)
