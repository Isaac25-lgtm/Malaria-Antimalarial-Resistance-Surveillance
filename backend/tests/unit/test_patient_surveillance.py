from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

# Transient ORM rows configure every mapper, so the whole registry must be loaded.
import mars.db.models  # noqa: F401
from mars.core.errors import ConflictError, ValidationFailedError
from mars.domain.encounter import OpdEncounter, OpdEncounterTest
from mars.domain.enums import (
    AttendanceType,
    FeverStatus,
    MalariaTestMethod,
    MalariaTestResult,
    Sex,
)
from mars.domain.longitudinal import (
    EXPLORATORY_PRESET,
    EvidenceCoverage,
    FrozenAnalysisQuery,
)
from mars.services.patient_surveillance import (
    page_fingerprint,
    page_rows,
    patient_alias,
    stored_coverage,
    summarise_stored_patients,
)


def test_patient_alias_is_stable_hmac_and_not_the_reference() -> None:
    patient = uuid.UUID("6f8721e1-f171-443c-8050-dd90d623c66b")
    first = patient_alias(patient, key=b"test-key-one", key_version="v3")
    second = patient_alias(patient, key=b"test-key-one", key_version="v3")
    assert first == second
    assert first.startswith("MARS-PT-V3-")
    assert str(patient) not in first
    assert len(first.rsplit("-", 1)[-1]) >= 20


def test_patient_alias_changes_when_display_key_rotates() -> None:
    patient = uuid.UUID("6f8721e1-f171-443c-8050-dd90d623c66b")
    before = patient_alias(patient, key=b"test-key-one", key_version="v1")
    after = patient_alias(patient, key=b"test-key-two", key_version="v2")
    assert before != after


AUGUST = FrozenAnalysisQuery(date(2026, 8, 1), date(2026, 8, 31))
SEPTEMBER = FrozenAnalysisQuery(date(2026, 9, 1), date(2026, 9, 30))
COVERAGE = EvidenceCoverage(date(2026, 6, 1), date(2026, 9, 30), True)
FACILITY = uuid.uuid4()


def _positive(patient: uuid.UUID, day: date) -> OpdEncounter:
    encounter = OpdEncounter(
        id=uuid.uuid4(),
        encounter_date=day,
        facility_id=FACILITY,
        patient_reference_id=patient,
        sex=Sex.UNKNOWN,
        fever_present=FeverStatus.UNKNOWN,
        attendance_type=AttendanceType.UNKNOWN,
        source_system="test",
        source_row_reference="row",
    )
    encounter.tests = [
        OpdEncounterTest(
            id=uuid.uuid4(),
            sequence=1,
            method=MalariaTestMethod.RDT,
            result=MalariaTestResult.POSITIVE,
        )
    ]
    encounter.prescriptions = []
    encounter.diagnoses = []
    encounter.referrals = []
    return encounter


def _summarise(
    encounters: list[OpdEncounter],
    *,
    query: FrozenAnalysisQuery = AUGUST,
    coverage: EvidenceCoverage = COVERAGE,
) -> list[dict[str, object]]:
    return summarise_stored_patients(
        encounters,
        definition=EXPLORATORY_PRESET,
        query=query,
        coverage=coverage,
        key=b"test-key",
        key_version="v1",
    )


def test_more_than_five_thousand_encounters_keep_every_patient() -> None:
    """The former implementation truncated encounters at 5,000 before grouping."""
    encounters: list[OpdEncounter] = []
    for index in range(3000):
        patient = uuid.uuid4()
        second = 10 if index % 2 == 0 else 3
        encounters += [
            _positive(patient, date(2026, 8, 1)),
            _positive(patient, date(2026, 8, 1) + timedelta(days=second)),
        ]
    rows = _summarise(encounters)
    assert len(rows) == 3000
    assert sum(row["classification"] == "repeat_positive_input" for row in rows) == 1500


def test_page_size_never_changes_who_is_counted() -> None:
    encounters = [
        _positive(uuid.uuid4(), date(2026, 8, 1) + timedelta(days=i % 31)) for i in range(40)
    ]
    rows = _summarise(encounters)
    small = [row["mars_patient_id"] for start in range(0, 40, 7) for row in rows[start : start + 7]]
    large = [row["mars_patient_id"] for row in rows[:100]]
    assert small == large
    assert len(set(large)) == 40


def test_a_positive_before_the_period_counts_through_lookback() -> None:
    patient = uuid.uuid4()
    rows = _summarise([_positive(patient, date(2026, 7, 20)), _positive(patient, date(2026, 8, 5))])
    assert rows[0]["classification"] == "repeat_positive_input"
    assert rows[0]["chain_interval_days"] == 16


def test_stored_rows_carry_named_intervals() -> None:
    patient = uuid.uuid4()
    rows = _summarise(
        [_positive(patient, date(2026, 8, 1) + timedelta(days=d)) for d in (0, 9, 23)]
    )
    row = rows[0]
    assert row["adjacent_interval_days"] == [9, 14]
    assert row["anchor_interval_days"] == [9, 23]
    assert row["chain_interval_days"] == 23
    assert row["interval_days"] == 23
    assert str(patient) not in str(row["mars_patient_id"])


# ---------------------------------------------------------------------------
# Audit defect G: stored coverage comes from import provenance
# ---------------------------------------------------------------------------
LOOKBACK, END = date(2026, 7, 4), date(2026, 8, 31)


def test_stored_coverage_comes_from_completed_import_windows() -> None:
    windows = {
        "f1": [(date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 8, 1), date(2026, 9, 3))],
        "f2": [(date(2026, 7, 10), date(2026, 8, 31))],
    }
    coverage = stored_coverage(windows, {"f1", "f2", "f3"}, LOOKBACK, END)
    assert coverage.facility_refs == frozenset({"f1"})
    assert coverage.missing_facilities == frozenset({"f2", "f3"})


def test_a_gap_between_import_windows_is_not_coverage() -> None:
    windows = {"f1": [(date(2026, 7, 1), date(2026, 7, 20)), (date(2026, 7, 22), END)]}
    assert stored_coverage(windows, {"f1"}, LOOKBACK, END).missing_facilities == {"f1"}


def test_absence_without_import_provenance_is_indeterminate_and_with_it_is_a_no() -> None:
    """The earliest stored encounter no longer stands in for coverage."""
    patient = uuid.uuid4()
    evidence = [_positive(patient, date(2026, 8, 5))]
    missing = stored_coverage({}, {str(FACILITY)}, LOOKBACK, END)
    assert _summarise(evidence, coverage=missing)[0]["determination"] == "indeterminate"

    imported = {str(FACILITY): [(date(2026, 6, 1), END)]}
    covered = stored_coverage(imported, {str(FACILITY)}, LOOKBACK, END)
    assert covered.missing_facilities == frozenset()
    assert _summarise(evidence, coverage=covered)[0]["determination"] == "does_not_qualify"


def test_a_second_facility_without_imports_keeps_absence_indeterminate() -> None:
    patient = uuid.uuid4()
    imported = {str(FACILITY): [(date(2026, 6, 1), END)]}
    coverage = stored_coverage(imported, {str(FACILITY), "unimported-facility"}, LOOKBACK, END)
    rows = _summarise([_positive(patient, date(2026, 8, 5))], coverage=coverage)
    assert rows[0]["determination"] == "indeterminate"


# ---------------------------------------------------------------------------
# Audit defect H: complete totals, stable cursors, period-aware lists
# ---------------------------------------------------------------------------
def _walk(rows: list[dict[str, object]], limit: int, fingerprint: str) -> list[object]:
    seen: list[object] = []
    cursor: str | None = None
    while True:
        items, cursor, _ = page_rows(rows, limit=limit, cursor=cursor, fingerprint=fingerprint)
        seen += [item["mars_patient_id"] for item in items]
        if cursor is None:
            return seen


def test_more_than_a_hundred_patients_page_completely_whatever_the_page_size() -> None:
    encounters: list[OpdEncounter] = []
    for index in range(2600):
        patient = uuid.uuid4()
        encounters += [
            _positive(patient, date(2026, 8, 1) + timedelta(days=index % 20)),
            _positive(patient, date(2026, 8, 10) + timedelta(days=index % 20)),
        ]
    assert len(encounters) > 5000
    rows = _summarise(encounters)
    assert len(rows) == 2600
    by_hundred = _walk(rows, 100, "fp")
    by_seven = _walk(rows, 7, "fp")
    assert by_hundred == by_seven == [row["mars_patient_id"] for row in rows]
    assert len(set(by_hundred)) == 2600


def test_next_and_previous_cursors_return_to_the_same_page() -> None:
    rows = [{"mars_patient_id": f"MARS-PT-{index:04d}"} for index in range(237)]
    first, forward, backward = page_rows(rows, limit=25, cursor=None, fingerprint="fp")
    assert backward is None and forward is not None
    second, _, back = page_rows(rows, limit=25, cursor=forward, fingerprint="fp")
    assert second[0]["mars_patient_id"] == "MARS-PT-0025"
    assert back is not None
    assert page_rows(rows, limit=25, cursor=back, fingerprint="fp")[0] == first


def test_a_cursor_over_changed_evidence_or_a_forged_cursor_is_refused() -> None:
    rows = [{"mars_patient_id": f"MARS-PT-{index:04d}"} for index in range(30)]
    _, cursor, _ = page_rows(rows, limit=10, cursor=None, fingerprint="before")
    assert cursor is not None
    with pytest.raises(ConflictError):
        page_rows(rows, limit=10, cursor=cursor, fingerprint="after")
    with pytest.raises(ValidationFailedError):
        page_rows(rows, limit=10, cursor="not-a-cursor", fingerprint="before")


def test_the_page_fingerprint_changes_with_period_filter_and_definition() -> None:
    base = page_fingerprint("hash", "definition", AUGUST.period_start, AUGUST.period_end, None)
    assert base != page_fingerprint(
        "hash", "definition", SEPTEMBER.period_start, SEPTEMBER.period_end, None
    )
    assert base != page_fingerprint(
        "hash", "definition", AUGUST.period_start, AUGUST.period_end, "qualifies"
    )
    assert base != page_fingerprint("hash", "other", AUGUST.period_start, AUGUST.period_end, None)


def test_changing_the_period_changes_who_is_listed() -> None:
    august_only, september_only = uuid.uuid4(), uuid.uuid4()
    encounters = [
        _positive(august_only, date(2026, 8, 5)),
        _positive(september_only, date(2026, 9, 20)),
    ]
    in_august = {row["patient_reference_id"] for row in _summarise(encounters)}
    in_september = {row["patient_reference_id"] for row in _summarise(encounters, query=SEPTEMBER)}
    assert in_august == {august_only}
    assert in_september == {september_only}
