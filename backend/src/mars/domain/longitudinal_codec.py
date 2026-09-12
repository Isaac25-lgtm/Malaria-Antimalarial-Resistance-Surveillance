"""Lossless JSON encoding of canonical evidence, for encrypted retention.

:func:`~mars.domain.longitudinal.canonical_value` renders evidence for
fingerprinting; this module renders it so that it can be read back. A retained
dataset decodes into exactly the encounters that were fingerprinted, so a
definition re-applied to it evaluates the same evidence and a comparison of two
runs over it compares like with like.

Pure: no I/O, no clock, no key. Encryption is the evidence store's job.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

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
)

CODEC_VERSION = "canonical-evidence-codec/1"


def _day(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _moment(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _read_day(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def _read_moment(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _availability(values: Mapping[str, Availability]) -> dict[str, str]:
    return {key: value.value for key, value in sorted(values.items())}


def _read_availability(values: Any) -> Mapping[str, Availability]:
    return MappingProxyType({key: Availability(item) for key, item in (values or {}).items()})


def encode_source(source: SourceRecord) -> list[str | None]:
    return [source.namespace, source.record_id, source.stage, source.revision]


def decode_source(value: Sequence[Any]) -> SourceRecord:
    namespace, record_id, stage, revision = value
    return SourceRecord(str(namespace), str(record_id), str(stage), revision)


def encode_test(test: ClinicalTest) -> dict[str, Any]:
    return {
        "source": encode_source(test.source),
        "method": test.method,
        "outcome": test.outcome.value,
        "observed_on": _day(test.observed_on),
        "observed_at": _moment(test.observed_at),
        "precision": test.precision.value,
    }


def decode_test(value: Mapping[str, Any]) -> ClinicalTest:
    return ClinicalTest(
        decode_source(value["source"]),
        str(value["method"]),
        LabOutcome(value["outcome"]),
        observed_on=_read_day(value.get("observed_on")),
        observed_at=_read_moment(value.get("observed_at")),
        precision=DatePrecision(value.get("precision", DatePrecision.UNKNOWN.value)),
    )


_MEDICATION_TEXT = (
    "formulation",
    "dose",
    "dose_unit",
    "quantity",
    "frequency",
    "duration_days",
    "dispensed_quantity",
)


def encode_medication(medication: Medication) -> dict[str, Any]:
    encoded: dict[str, Any] = {
        "source": encode_source(medication.source),
        "name": medication.name,
        "evidence": medication.evidence.value,
        "prescribed_on": _day(medication.prescribed_on),
        "dispensed_on": _day(medication.dispensed_on),
        "availability": _availability(medication.availability),
    }
    for name in _MEDICATION_TEXT:
        encoded[name] = getattr(medication, name)
    return encoded


def decode_medication(value: Mapping[str, Any]) -> Medication:
    return Medication(
        source=decode_source(value["source"]),
        name=value.get("name"),
        evidence=MedicationEvidence(value["evidence"]),
        prescribed_on=_read_day(value.get("prescribed_on")),
        dispensed_on=_read_day(value.get("dispensed_on")),
        availability=_read_availability(value.get("availability")),
        **{name: value.get(name) for name in _MEDICATION_TEXT},
    )


def encode_encounter(encounter: CanonicalEncounter) -> dict[str, Any]:
    return {
        "encounter_key": encounter.encounter_key,
        "namespace": encounter.namespace,
        "patient_key": encounter.patient_key,
        "facility_ref": encounter.facility_ref,
        "encounter_date": _day(encounter.encounter_date),
        "date_precision": encounter.date_precision.value,
        "occurred_at": _moment(encounter.occurred_at),
        "parent_ref": encounter.parent_ref,
        "parent_retrieved": encounter.parent_retrieved,
        "tests": [encode_test(test) for test in encounter.tests],
        "diagnoses": list(encounter.diagnoses),
        "fever": encounter.fever,
        "attendance": encounter.attendance,
        "referrals": list(encounter.referrals),
        "outcome": encounter.outcome,
        "observations": list(encounter.observations),
        "attributes": [list(pair) for pair in encounter.attributes],
        "medications": [encode_medication(item) for item in encounter.medications],
        "sources": [encode_source(source) for source in encounter.sources],
        "sex": encounter.sex,
        "age_group": encounter.age_group,
        "availability": _availability(encounter.availability),
        "quality_flags": sorted(flag.value for flag in encounter.quality_flags),
        "mapping_version": encounter.mapping_version,
    }


def decode_encounter(value: Mapping[str, Any]) -> CanonicalEncounter:
    return CanonicalEncounter(
        encounter_key=str(value["encounter_key"]),
        namespace=str(value["namespace"]),
        patient_key=value.get("patient_key"),
        facility_ref=str(value["facility_ref"]),
        encounter_date=_read_day(value.get("encounter_date")),
        date_precision=DatePrecision(value["date_precision"]),
        occurred_at=_read_moment(value.get("occurred_at")),
        parent_ref=value.get("parent_ref"),
        parent_retrieved=bool(value.get("parent_retrieved", False)),
        tests=tuple(decode_test(item) for item in value.get("tests", ())),
        diagnoses=tuple(value.get("diagnoses", ())),
        fever=value.get("fever"),
        attendance=value.get("attendance"),
        referrals=tuple(value.get("referrals", ())),
        outcome=value.get("outcome"),
        observations=tuple(value.get("observations", ())),
        attributes=tuple((str(a), str(b)) for a, b in value.get("attributes", ())),
        medications=tuple(decode_medication(item) for item in value.get("medications", ())),
        sources=tuple(decode_source(item) for item in value.get("sources", ())),
        sex=value.get("sex"),
        age_group=value.get("age_group"),
        availability=_read_availability(value.get("availability")),
        quality_flags=frozenset(QualityFlag(flag) for flag in value.get("quality_flags", ())),
        mapping_version=str(value.get("mapping_version", "")),
    )


def encode_coverage(coverage: EvidenceCoverage) -> dict[str, Any]:
    return {
        "earliest_date": _day(coverage.earliest_date),
        "latest_date": _day(coverage.latest_date),
        "complete": coverage.complete,
        "stages": sorted(coverage.stages),
        "facility_refs": sorted(coverage.facility_refs),
        "exclusions": list(coverage.exclusions),
        "requested_facility_refs": sorted(coverage.requested_facility_refs),
    }


def decode_coverage(value: Mapping[str, Any]) -> EvidenceCoverage:
    return EvidenceCoverage(
        earliest_date=_read_day(value.get("earliest_date")),
        latest_date=_read_day(value.get("latest_date")),
        complete=bool(value["complete"]),
        stages=frozenset(value.get("stages", ())),
        facility_refs=frozenset(value.get("facility_refs", ())),
        exclusions=tuple(value.get("exclusions", ())),
        requested_facility_refs=frozenset(value.get("requested_facility_refs", ())),
    )


def encode_dataset(
    encounters: Sequence[CanonicalEncounter],
    coverage: EvidenceCoverage,
    unlinked_medications: Mapping[str, Sequence[Medication]] | None = None,
) -> dict[str, Any]:
    return {
        "codec": CODEC_VERSION,
        "coverage": encode_coverage(coverage),
        "encounters": [
            encode_encounter(encounter)
            for encounter in sorted(encounters, key=lambda e: (e.namespace, e.encounter_key))
        ],
        "unlinked_medications": {
            person: [encode_medication(item) for item in items]
            for person, items in sorted((unlinked_medications or {}).items())
        },
    }


def decode_dataset(
    value: Mapping[str, Any],
) -> tuple[tuple[CanonicalEncounter, ...], EvidenceCoverage, Mapping[str, tuple[Medication, ...]]]:
    if value.get("codec") != CODEC_VERSION:
        raise ValueError(f"Unsupported evidence codec {value.get('codec')!r}")
    encounters = tuple(decode_encounter(item) for item in value.get("encounters", ()))
    coverage = decode_coverage(value["coverage"])
    unlinked = MappingProxyType(
        {
            str(person): tuple(decode_medication(item) for item in items)
            for person, items in (value.get("unlinked_medications") or {}).items()
        }
    )
    return encounters, coverage, unlinked


__all__ = [
    "CODEC_VERSION",
    "decode_coverage",
    "decode_dataset",
    "decode_encounter",
    "decode_medication",
    "decode_source",
    "decode_test",
    "encode_coverage",
    "encode_dataset",
    "encode_encounter",
    "encode_medication",
    "encode_source",
    "encode_test",
]
