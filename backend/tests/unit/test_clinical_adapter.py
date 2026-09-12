"""Source adapters: live Tracker events and stored encounters, one contract.

Every value here is invented. The parity test is the handoff's case 12: the same
clinical evidence through either adapter must produce identical findings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

# Transient ORM rows configure every mapper, so the whole registry must be loaded.
import mars.db.models  # noqa: F401
from mars.analytics.positive_recurrence import evaluate_recurrence
from mars.analytics.recurrence_views import summarise
from mars.domain.encounter import OpdEncounter, OpdEncounterPrescription, OpdEncounterTest
from mars.domain.enums import (
    AgeUnit,
    AttendanceType,
    FeverStatus,
    MalariaTestMethod,
    MalariaTestResult,
    Sex,
)
from mars.domain.longitudinal import (
    EXPLORATORY_PRESET,
    Availability,
    DuplicateKind,
    EvidenceCoverage,
    FrozenAnalysisQuery,
    LabOutcome,
    MedicationEvidence,
    QualityFlag,
    RecurrenceEvaluation,
)
from mars.integrations.dhis2.tracker.clinical_adapter import (
    LiveClinicalMapping,
    adapt_live_events,
    classify_method,
)
from mars.integrations.ports import RemoteEvent
from mars.services.stored_encounter_adapter import adapt_stored_encounters, age_group

BASE = date(2026, 8, 3)
NS = "dhis2:test:programme"
POSITIVE = "Malaria: Positive - Plasmodium falciparum"

CONFIG = {
    "schema_version": "2.0",
    "programme_uid": "programme",
    "tracker": {
        "parent_event_data_element_uid": "parent",
        "stages": {
            "medical_visit": "visitStage",
            "laboratory_tests": "labStage",
            "medicines_and_supplies": "medStage",
        },
        "data_elements": {
            "laboratory_test_type": "testType",
            "laboratory_result": "result",
            "diagnosis": "diagnosis",
            "signs_and_symptoms": "signs",
            "outcome": "outcome",
            "opd_number": "opdNumber",
            "medicine_type": "medicine",
            "medicine_quantity": "quantity",
        },
        "options": {
            "test_types_malaria": ["Malaria Test RDT", "Malaria Test Microscopy Bs"],
            "positive_malaria_results": [POSITIVE],
            "negative_result": "Negative",
        },
    },
}
MAPPING = LiveClinicalMapping.from_config(CONFIG)


def at(offset: int, hour: int = 0) -> datetime:
    return datetime(2026, 8, 3, hour, tzinfo=UTC) + timedelta(days=offset)


def event(
    remote_id: str,
    person: str,
    stage: str,
    offset: int,
    values: dict[str, str | None],
    *,
    facility: str = "F1",
    updated: datetime | None = None,
) -> RemoteEvent:
    return RemoteEvent(
        remote_id=remote_id,
        person_remote_id=person,
        programme_remote_id="programme",
        programme_stage_remote_id=stage,
        organisation_unit_remote_id=facility,
        occurred_at=at(offset),
        updated_at=updated,
        status="COMPLETED",
        data_values=values,
    )


def lab(
    remote_id: str,
    person: str,
    offset: int,
    *,
    parent: str | None = None,
    test: str = "Malaria Test RDT",
    result: str | None = POSITIVE,
    facility: str = "F1",
    updated: datetime | None = None,
) -> RemoteEvent:
    values: dict[str, str | None] = {"testType": test, "result": result}
    if parent:
        values["parent"] = parent
    return event(remote_id, person, "labStage", offset, values, facility=facility, updated=updated)


def adapt(labs, visits=(), medicines=(), *, retrieved: bool = True):  # type: ignore[no-untyped-def]
    return adapt_live_events(
        labs,
        MAPPING,
        namespace=NS,
        visit_events=visits,
        medicine_events=medicines,
        context_retrieved=retrieved,
    )


def by_key(evidence, key):  # type: ignore[no-untyped-def]
    return next(e for e in evidence.encounters if e.encounter_key == key)


# ---------------------------------------------------------------------------
# Grouping, revisions, identity
# ---------------------------------------------------------------------------
def test_rdt_and_microscopy_sharing_a_parent_become_one_encounter_with_both_tests() -> None:
    evidence = adapt(
        [
            lab("l1", "tA", 0, parent="V1"),
            lab("l2", "tA", 0, parent="V1", test="Malaria Test Microscopy Bs"),
        ],
        [event("V1", "tA", "visitStage", 0, {"diagnosis": "Malaria"})],
    )
    encounter = by_key(evidence, "visit:V1")
    assert [t.method for t in encounter.tests] == ["rdt", "microscopy"]
    assert encounter.parent_retrieved
    assert encounter.diagnoses == ("Malaria",)


def test_repeated_retrieval_of_one_event_counts_once_and_is_recorded() -> None:
    evidence = adapt(
        [
            lab("l1", "tA", 0, result="Negative", updated=at(0, 8)),
            lab("l1", "tA", 0, result=POSITIVE, updated=at(0, 9)),
        ]
    )
    encounter = by_key(evidence, "event:l1")
    assert len(encounter.tests) == 1
    assert encounter.tests[0].outcome is LabOutcome.POSITIVE  # the later revision
    assert [g.kind for g in evidence.duplicate_groups] == [DuplicateKind.SOURCE_REVISION]


def test_records_without_a_parent_stay_distinct_candidates() -> None:
    evidence = adapt([lab("l1", "tA", 0), lab("l2", "tA", 0)])
    assert {e.encounter_key for e in evidence.encounters} == {"event:l1", "event:l2"}


def test_a_parent_claimed_by_two_people_never_merges_them() -> None:
    evidence = adapt([lab("l1", "tA", 0, parent="V1"), lab("l2", "tB", 0, parent="V1")])
    people = {e.patient_key for e in evidence.encounters}
    assert people == {"tA", "tB"}
    stray = by_key(evidence, "event:l2")
    assert QualityFlag.IDENTITY_LINKAGE_UNCERTAIN in stray.quality_flags


# ---------------------------------------------------------------------------
# Audit defect A: a positive is dated by its own laboratory event
# ---------------------------------------------------------------------------
def _evaluate(evidence) -> RecurrenceEvaluation:  # type: ignore[no-untyped-def]
    start, end = BASE, BASE + timedelta(days=30)
    return evaluate_recurrence(
        evidence.encounters,
        EXPLORATORY_PRESET,
        FrozenAnalysisQuery(start, end),
        EvidenceCoverage(start - timedelta(days=40), end, True),
    )


def test_lab_positives_keep_their_own_dates_when_the_parent_visits_differ() -> None:
    """Positives on 3 and 12 August whose parent visits are dated 3 and 8 August."""
    evidence = adapt(
        [lab("l1", "tA", 0, parent="V1"), lab("l2", "tA", 9, parent="V2")],
        [
            event("V1", "tA", "visitStage", 0, {"diagnosis": "Malaria"}),
            event("V2", "tA", "visitStage", 5, {"diagnosis": "Malaria"}),
        ],
    )
    second = by_key(evidence, "visit:V2")
    assert second.encounter_date == date(2026, 8, 8)  # the visit, for the care timeline
    assert second.tests[0].observed_on == date(2026, 8, 12)  # the test, for the interval
    found = _evaluate(evidence).patients[0]
    assert found.determination.value == "qualifies"
    assert found.chain_interval is not None and found.chain_interval.days == 9
    assert QualityFlag.DATE_QUALITY_ISSUE not in found.quality_flags


def test_tests_under_one_parent_keep_every_date_and_a_disagreement_is_flagged() -> None:
    evidence = adapt(
        [
            lab("l1", "tA", 0, parent="V1"),
            lab("l2", "tA", 2, parent="V1", test="Malaria Test Microscopy Bs"),
        ],
        [event("V1", "tA", "visitStage", 0, {})],
    )
    encounter = by_key(evidence, "visit:V1")
    assert [t.observed_on for t in encounter.tests] == [date(2026, 8, 3), date(2026, 8, 5)]
    found = _evaluate(evidence).patients[0]
    assert QualityFlag.DATE_QUALITY_ISSUE in found.quality_flags
    assert found.decisions[0].local_day == date(2026, 8, 3)  # the earliest positive test


def test_source_precision_survives_the_adapter() -> None:
    from dataclasses import replace
    from zoneinfo import ZoneInfo

    from mars.domain.longitudinal import DatePrecision

    late = datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
    evidence = adapt(
        [
            lab("l1", "tA", 0),
            replace(lab("l2", "tB", 0), occurred_at=late, occurred_precision="timestamp"),
            replace(lab("l3", "tC", 0), occurred_at=late, occurred_precision="local_time"),
        ]
    )
    zone = ZoneInfo("Africa/Kampala")
    assert by_key(evidence, "event:l1").tests[0].precision is DatePrecision.DAY
    instant = by_key(evidence, "event:l2").tests[0]
    assert instant.precision is DatePrecision.TIMESTAMP
    assert instant.local_day(zone) == date(2026, 8, 4)  # 22:30 UTC is 01:30 in Kampala
    wall = by_key(evidence, "event:l3").tests[0]
    assert wall.precision is DatePrecision.LOCAL_TIME
    assert wall.local_day(zone) == date(2026, 8, 3)  # as entered, never shifted


def test_a_visit_belonging_to_another_person_is_not_joined() -> None:
    evidence = adapt(
        [lab("l1", "tA", 0, parent="V1")],
        [event("V1", "tB", "visitStage", 0, {"diagnosis": "Pneumonia"})],
    )
    encounter = by_key(evidence, "visit:V1")
    assert not encounter.parent_retrieved
    assert encounter.diagnoses == ()
    assert QualityFlag.IDENTITY_LINKAGE_UNCERTAIN in encounter.quality_flags


def test_non_malaria_laboratory_tests_are_not_malaria_evidence() -> None:
    evidence = adapt([lab("l1", "tA", 0, test="Haemoglobin")])
    assert evidence.encounters == ()


def test_the_opd_register_number_is_never_carried() -> None:
    evidence = adapt(
        [lab("l1", "tA", 0, parent="V1")],
        [event("V1", "tA", "visitStage", 0, {"opdNumber": "OPD-2026-771", "diagnosis": "Malaria"})],
    )
    assert "OPD-2026-771" not in repr(evidence)


# ---------------------------------------------------------------------------
# Results and treatment
# ---------------------------------------------------------------------------
def test_missing_and_uninterpretable_results_are_different_facts() -> None:
    evidence = adapt([lab("l1", "tA", 0, result=None), lab("l2", "tA", 1, result="Invalid")])
    assert by_key(evidence, "event:l1").tests[0].outcome is LabOutcome.NOT_RECORDED
    assert by_key(evidence, "event:l2").tests[0].outcome is LabOutcome.UNMAPPED
    assert evidence.unresolved_results == 2


def test_medicine_attaches_only_through_the_verified_parent() -> None:
    evidence = adapt(
        [lab("l1", "tA", 0, parent="V1")],
        [event("V1", "tA", "visitStage", 0, {})],
        [
            event("m1", "tA", "medStage", 0, {"parent": "V1", "medicine": "AL", "quantity": "24"}),
            event("m2", "tA", "medStage", 1, {"medicine": "Paracetamol"}),
        ],
    )
    encounter = by_key(evidence, "visit:V1")
    assert [m.name for m in encounter.medications] == ["AL"]
    assert [m.name for m in evidence.unlinked_medications["tA"]] == ["Paracetamol"]


def test_recorded_medicine_is_never_upgraded_and_dose_is_never_invented() -> None:
    evidence = adapt(
        [lab("l1", "tA", 0, parent="V1")],
        [event("V1", "tA", "visitStage", 0, {})],
        [event("m1", "tA", "medStage", 0, {"parent": "V1", "medicine": "AL", "quantity": "24"})],
    )
    medication = by_key(evidence, "visit:V1").medications[0]
    assert medication.evidence is MedicationEvidence.RECORDED
    assert medication.dose is None and medication.frequency is None
    assert medication.availability["dose"] is Availability.NOT_MAPPED
    assert medication.quantity == "24"


def test_unretrieved_context_is_not_reported_as_no_treatment() -> None:
    evidence = adapt([lab("l1", "tA", 0, parent="V1")], retrieved=False)
    availability = by_key(evidence, "visit:V1").availability
    assert availability["medications"] is Availability.NOT_RETURNED


def test_method_classification_leaves_option_codes_unknown() -> None:
    assert classify_method("Malaria Test RDT") == "rdt"
    assert classify_method("Malaria Test Microscopy Bs") == "microscopy"
    assert classify_method("cTBHMKQ1DD7") == "unknown"


# ---------------------------------------------------------------------------
# Stored adapter
# ---------------------------------------------------------------------------
def stored(
    patient: uuid.UUID,
    offset: int,
    facility: uuid.UUID,
    results: list[tuple[MalariaTestMethod, MalariaTestResult]],
    drugs: tuple[str, ...] = (),
    *,
    devices: tuple[str, ...] = (),
) -> OpdEncounter:
    encounter = OpdEncounter(
        id=uuid.uuid4(),
        encounter_date=BASE + timedelta(days=offset),
        facility_id=facility,
        patient_reference_id=patient,
        sex=Sex.UNKNOWN,
        fever_present=FeverStatus.UNKNOWN,
        attendance_type=AttendanceType.UNKNOWN,
        source_system="test",
        source_row_reference="row",
    )
    encounter.tests = [
        OpdEncounterTest(id=uuid.uuid4(), sequence=i + 1, method=method, result=result)
        for i, (method, result) in enumerate(results)
    ]
    encounter.prescriptions = [
        OpdEncounterPrescription(
            id=uuid.uuid4(),
            sequence=i + 1,
            prescription_raw=name,
            drug_name_raw=name,
            is_device=name in devices,
        )
        for i, name in enumerate(drugs + devices)
    ]
    encounter.diagnoses = []
    encounter.referrals = []
    return encounter


def test_stored_prescriptions_are_prescribed_and_devices_are_not_medicines() -> None:
    patient, facility = uuid.uuid4(), uuid.uuid4()
    evidence = adapt_stored_encounters(
        [
            stored(
                patient,
                0,
                facility,
                [(MalariaTestMethod.RDT, MalariaTestResult.POSITIVE)],
                ("Artemether/Lumefantrine",),
                devices=("Syringe",),
            )
        ]
    )
    medications = evidence.encounters[0].medications
    assert [m.name for m in medications] == ["Artemether/Lumefantrine"]
    assert medications[0].evidence is MedicationEvidence.PRESCRIBED


def test_unknown_age_is_unknown_not_zero() -> None:
    assert age_group(None, None) is None
    assert age_group(0, AgeUnit.DAYS) == "under_5"
    assert age_group(30, AgeUnit.MONTHS) == "under_5"
    assert age_group(9, AgeUnit.YEARS) == "5_to_14"
    assert age_group(40, AgeUnit.YEARS) == "15_and_over"


# ---------------------------------------------------------------------------
# Case 12: parity
# ---------------------------------------------------------------------------
def _projection(evaluation: RecurrenceEvaluation) -> list[tuple[object, ...]]:
    rows = [
        (
            p.determination,
            tuple(i.days for i in p.adjacent_intervals),
            tuple(i.days for i in p.anchor_intervals),
            p.chain_interval.days if p.chain_interval else None,
            p.chain_length,
            p.observed_positive_count,
            p.eligible_positive_count,
            p.final_date,
            tuple(t.prior_medications for t in p.transitions),
            p.cross_facility,
            p.quality_flags,
        )
        for p in evaluation.patients
    ]
    return sorted(rows, key=repr)


def test_live_and_stored_adapters_give_identical_findings() -> None:
    labs = [
        lab("L1", "tA", 0, parent="V1"),
        lab("L2", "tA", 12, parent="V2", test="Malaria Test Microscopy Bs"),
        lab("L3", "tA", 20, parent="V3", result="Negative"),
        lab("L4", "tB", 5, parent="V4"),
    ]
    visits = [
        event("V1", "tA", "visitStage", 0, {}),
        event("V2", "tA", "visitStage", 12, {}),
        event("V3", "tA", "visitStage", 20, {}),
        event("V4", "tB", "visitStage", 5, {}),
    ]
    medicines = [
        event("M1", "tA", "medStage", 0, {"parent": "V1", "medicine": "Artemether/Lumefantrine"})
    ]

    patient_a, patient_b, facility = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rdt, micro = MalariaTestMethod.RDT, MalariaTestMethod.MICROSCOPY
    pos, neg = MalariaTestResult.POSITIVE, MalariaTestResult.NEGATIVE
    rows = [
        stored(patient_a, 0, facility, [(rdt, pos)], ("Artemether/Lumefantrine",)),
        stored(patient_a, 12, facility, [(micro, pos)]),
        stored(patient_a, 20, facility, [(rdt, neg)]),
        stored(patient_b, 5, facility, [(rdt, pos)]),
    ]

    query = FrozenAnalysisQuery(BASE, BASE + timedelta(days=30))
    coverage = EvidenceCoverage(BASE - timedelta(days=30), BASE + timedelta(days=40), True)
    live = evaluate_recurrence(
        adapt(labs, visits, medicines).encounters, EXPLORATORY_PRESET, query, coverage
    )
    local = evaluate_recurrence(
        adapt_stored_encounters(rows).encounters, EXPLORATORY_PRESET, query, coverage
    )

    assert _projection(live) == _projection(local)
    assert summarise(live) == summarise(local)
    assert summarise(live).qualifying_patients == 1


def test_a_facility_whose_context_failed_reports_not_returned_only_there() -> None:
    evidence = adapt_live_events(
        [
            lab("l1", "tA", 0, parent="V1", facility="F1"),
            lab("l2", "tB", 0, parent="V2", facility="F2"),
        ],
        MAPPING,
        namespace=NS,
        visit_events=[event("V1", "tA", "visitStage", 0, {}, facility="F1")],
        context_retrieved=True,
        context_missing_facilities=frozenset({"F2"}),
    )
    assert by_key(evidence, "visit:V1").availability["medications"] is Availability.RECORDED
    assert by_key(evidence, "visit:V2").availability["medications"] is Availability.NOT_RETURNED
