"""Duplicate classification, independent of recurrence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from mars.analytics.duplicate_detection import (
    classify_same_day,
    compare,
    group_id,
    latest_revisions,
    select_representative,
    shared_parent_group,
)
from mars.domain.longitudinal import (
    CanonicalEncounter,
    ClinicalTest,
    DatePrecision,
    DuplicateConfidence,
    DuplicateKind,
    LabOutcome,
    SourceRecord,
)

NS = "test:namespace"
DAY = date(2026, 8, 4)
KAMPALA = ZoneInfo("Africa/Kampala")


def enc(
    key: str,
    *,
    facility: str = "f1",
    outcome: LabOutcome = LabOutcome.POSITIVE,
    method: str = "rdt",
    diagnoses: tuple[str, ...] = (),
    at: datetime | None = None,
    patient: str | None = "p1",
) -> CanonicalEncounter:
    record = SourceRecord(NS, key, "lab")
    return CanonicalEncounter(
        encounter_key=key,
        namespace=NS,
        patient_key=patient,
        facility_ref=facility,
        encounter_date=DAY if at is None else None,
        date_precision=DatePrecision.DAY if at is None else DatePrecision.TIMESTAMP,
        occurred_at=at,
        tests=(
            ClinicalTest(
                record,
                method,
                outcome,
                observed_on=DAY if at is None else None,
                observed_at=at,
                precision=DatePrecision.DAY if at is None else DatePrecision.TIMESTAMP,
            ),
        ),
        diagnoses=diagnoses,
        sources=(record,),
    )


def test_the_latest_revision_wins_and_the_repeat_is_recorded() -> None:
    items = [("e1", "2026-08-01"), ("e1", "2026-08-05"), ("e2", "2026-08-02")]
    kept, groups = latest_revisions(
        items, identity=lambda t: t[0], revision=lambda t: (t[1],), member_label=lambda t: t[0]
    )
    assert sorted(kept) == [("e1", "2026-08-05"), ("e2", "2026-08-02")]
    assert len(groups) == 1
    assert groups[0].kind is DuplicateKind.SOURCE_REVISION
    assert groups[0].confidence is DuplicateConfidence.EXACT


def test_identical_revisions_resolve_the_same_way_every_time() -> None:
    items = [("e1", "r", "first"), ("e1", "r", "second")]
    kept, _ = latest_revisions(
        items, identity=lambda t: t[0], revision=lambda t: (t[1],), member_label=lambda t: t[0]
    )
    assert kept == [("e1", "r", "second")]


def test_a_record_seen_once_produces_no_group() -> None:
    _, groups = latest_revisions(
        [("e1", "r")],
        identity=lambda t: t[0],
        revision=lambda t: (t[1],),
        member_label=lambda t: t[0],
    )
    assert groups == []


def test_shared_parent_tests_form_one_verified_group() -> None:
    tests = (
        ClinicalTest(SourceRecord(NS, "rdt", "lab"), "rdt", LabOutcome.POSITIVE),
        ClinicalTest(SourceRecord(NS, "mic", "lab"), "microscopy", LabOutcome.POSITIVE),
    )
    encounter = CanonicalEncounter(
        encounter_key="visit:v1",
        namespace=NS,
        patient_key="p1",
        facility_ref="f1",
        encounter_date=DAY,
        date_precision=DatePrecision.DAY,
        parent_ref="v1",
        tests=tests,
    )
    group = shared_parent_group(encounter)
    assert group is not None
    assert group.confidence is DuplicateConfidence.VERIFIED
    assert set(group.members) == {"rdt", "mic"}


def test_a_single_test_or_no_parent_is_not_a_shared_parent_group() -> None:
    assert shared_parent_group(enc("a")) is None


def test_a_field_missing_on_either_side_is_not_evidence_of_similarity() -> None:
    fields = dict(compare(enc("a"), enc("b", diagnoses=("malaria",))))
    assert fields["diagnoses"] == "missing"
    assert fields["facility"] == "match"


def test_a_near_duplicate_needs_two_matches_and_nothing_differing() -> None:
    groups = classify_same_day([enc("a"), enc("b")], KAMPALA, unresolved=False)
    assert [g.kind for g in groups] == [DuplicateKind.NEAR_DUPLICATE]
    assert groups[0].confidence is DuplicateConfidence.PROBABLE


def test_a_differing_field_downgrades_to_same_day_distinct() -> None:
    groups = classify_same_day(
        [enc("a", diagnoses=("malaria",)), enc("b", diagnoses=("pneumonia",))],
        KAMPALA,
        unresolved=False,
    )
    assert [g.kind for g in groups] == [DuplicateKind.SAME_DAY_DISTINCT]


def test_cross_facility_same_day_records_are_never_called_near_duplicates() -> None:
    groups = classify_same_day(
        [enc("a", facility="f1"), enc("b", facility="f2")], KAMPALA, unresolved=True
    )
    assert groups[0].kind is DuplicateKind.SAME_DAY_DISTINCT
    assert any("not asserted to be erroneous" in reason for reason in groups[0].reasons)
    assert groups[0].unresolved


def test_unlinked_records_are_never_grouped_by_similarity() -> None:
    assert (
        classify_same_day(
            [enc("a", patient=None), enc("b", patient=None)], KAMPALA, unresolved=False
        )
        == []
    )


def test_the_representative_uses_timestamps_only_when_every_member_has_one() -> None:
    early = enc("z", at=datetime(2026, 8, 4, 6, 0, tzinfo=UTC))
    late = enc("a", at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC))
    assert select_representative([late, early]).encounter_key == "z"
    assert select_representative([late, enc("m")]).encounter_key == "a"


def test_group_ids_do_not_depend_on_member_order() -> None:
    kind = DuplicateKind.NEAR_DUPLICATE
    assert group_id(kind, ["b", "a"]) == group_id(kind, ["a", "b"])
    assert group_id(kind, ["a", "b"]) != group_id(DuplicateKind.SAME_DAY_DISTINCT, ["a", "b"])
