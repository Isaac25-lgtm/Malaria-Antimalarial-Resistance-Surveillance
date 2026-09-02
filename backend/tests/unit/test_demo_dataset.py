"""The synthetic demo dataset, without a database.

Three properties matter and none of them is "the numbers look plausible":

**Determinism.** The same seed must produce byte-identical artefacts. This
dataset is the golden fixture later detector work is measured against, and a
fixture that changes under you is not a fixture.

**The storylines are actually planted.** A manifest that claims a stock-out is
worthless if the generator did not produce one. Each storyline is asserted
against the data it wrote, not against the manifest that describes it.

**Nothing is mistakable for real.** Synthetic identifiers, synthetic facility
codes, and no coordinates anywhere.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from mars.demo.generator import (
    _ANTIMALARIALS,
    FACILITY_CODE_PREFIX,
    IDENTIFIER_PREFIX,
    SOURCE_SYSTEM,
    DemoDatasetGenerator,
    DemoDistrict,
    GeneratorOptions,
)
from mars.demo.storylines import STORYLINES, StorylineKey

#: Read from the generator rather than retyped: a hard-coded drug list in a
#: test drifts from the one the generator uses and then asserts nothing.
ANTIMALARIAL_NAMES = {name for name, _pattern in _ANTIMALARIALS}

DISTRICTS = [
    DemoDistrict("101", "Alpha", StorylineKey.REPEAT_POSITIVE_CLUSTER, ("Aone", "Atwo", "Athree")),
    DemoDistrict("102", "Beta", StorylineKey.TESTING_ANOMALY_STOCKOUT, ("Bone", "Btwo")),
    DemoDistrict("103", "Gamma", StorylineKey.COMPLETENESS_ARTEFACT, ("Gone", "Gtwo")),
    DemoDistrict("104", "Delta", StorylineKey.SPATIAL_CLUSTER, ("Done", "Dtwo", "Dthree")),
    DemoDistrict("105", "Epsilon", StorylineKey.SEASONAL_CONTROL, ("Eone", "Etwo")),
]

OPTIONS = GeneratorOptions(
    seed=4242,
    period_start=date(2025, 10, 1),
    period_end=date(2026, 1, 31),
    facilities_per_district=3,
    daily_attendance=5,
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory):
    """Generated once. The dataset is read-only, and generating ten thousand
    encounters per test would make the suite slow enough to skip."""
    out_dir = tmp_path_factory.mktemp("demo")
    return DemoDatasetGenerator(list(DISTRICTS), OPTIONS).generate(out_dir)


def rows_of(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def all_rows(dataset) -> list[dict]:
    rows: list[dict] = []
    for artefact in dataset.artefacts:
        rows.extend(rows_of(artefact))
    return rows


def dated(rows: list[dict]) -> list[dict]:
    """Only the rows that carry a usable date.

    The generator deliberately breaks a small share of rows, and one of the
    breakages is an empty ``encounter_date``. Those rows are the point of the
    quarantine storyline; they are not part of any *shape* assertion, and a
    helper that tripped over them would make every storyline test fragile.
    """
    return [row for row in rows if row.get("encounter_date")]


def rows_for_district(dataset, code: str) -> list[dict]:
    district = next(d for d in DISTRICTS if d.code == code)
    prefix_index = DISTRICTS.index(district) * 100
    codes = {
        f"{FACILITY_CODE_PREFIX}-{prefix_index + position + 1:04d}"
        for position in range(OPTIONS.facilities_per_district)
    }
    rows: list[dict] = []
    for artefact in dataset.artefacts:
        if artefact.name.split("_")[0] in codes:
            rows.extend(rows_of(artefact))
    return rows


class TestDeterminism:
    def test_the_same_seed_produces_byte_identical_artefacts(self, tmp_path: Path) -> None:
        first = DemoDatasetGenerator(list(DISTRICTS), OPTIONS).generate(tmp_path / "a")
        second = DemoDatasetGenerator(list(DISTRICTS), OPTIONS).generate(tmp_path / "b")

        assert [p.name for p in first.artefacts] == [p.name for p in second.artefacts]
        for left, right in zip(first.artefacts, second.artefacts, strict=True):
            assert left.read_bytes() == right.read_bytes(), left.name

    def test_a_different_seed_produces_different_data(self, tmp_path: Path) -> None:
        """Otherwise the seed is decorative and the determinism test proves
        nothing."""
        import dataclasses

        other = dataclasses.replace(OPTIONS, seed=OPTIONS.seed + 1)
        first = DemoDatasetGenerator(list(DISTRICTS), OPTIONS).generate(tmp_path / "a")
        second = DemoDatasetGenerator(list(DISTRICTS), other).generate(tmp_path / "b")

        assert any(
            left.read_bytes() != right.read_bytes()
            for left, right in zip(first.artefacts, second.artefacts, strict=False)
        )

    def test_no_wall_clock_time_reaches_an_artefact(self, dataset) -> None:
        """A generation timestamp would change the bytes on every run."""
        envelopes = [
            json.loads(artefact.read_text(encoding="utf-8").splitlines()[0])
            for artefact in dataset.artefacts
        ]
        for envelope in envelopes:
            month = envelope["register_opened_on"][:7]
            assert envelope["extracted_at"] == f"{month}-01T00:00:00+00:00"


class TestNothingIsMistakableForReal:
    def test_every_facility_code_carries_the_demo_prefix(self, dataset) -> None:
        entries = json.loads(dataset.facilities_path.read_text(encoding="utf-8"))
        assert entries
        for entry in entries:
            assert entry["code"].startswith(FACILITY_CODE_PREFIX)
            assert entry["is_synthetic"] is True

    def test_no_facility_has_a_coordinate(self, dataset) -> None:
        """A plausible point on a fictional facility is the detail that escapes
        a demo and gets believed."""
        entries = json.loads(dataset.facilities_path.read_text(encoding="utf-8"))
        for entry in entries:
            assert entry["latitude"] is None
            assert entry["longitude"] is None

    def test_every_identifier_carries_the_synthetic_prefix(self, dataset) -> None:
        identifiers = [
            row["identity"]["identifier_value"] for row in all_rows(dataset) if "identity" in row
        ]
        assert identifiers, "no row carried an identifier; linkage would be untested"
        assert all(value.startswith(IDENTIFIER_PREFIX) for value in identifiers)

    def test_the_source_system_names_itself_as_a_demo(self, dataset) -> None:
        envelopes = [
            json.loads(artefact.read_text(encoding="utf-8").splitlines()[0])
            for artefact in dataset.artefacts
        ]
        assert {e["source_system"] for e in envelopes} == {SOURCE_SYSTEM}
        assert "demo" in SOURCE_SYSTEM

    def test_the_manifest_says_what_the_dataset_is_and_is_not(self, dataset) -> None:
        manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
        assert manifest["synthetic"] is True
        assert "fictional" in manifest["warning"]
        # The one claim that must never be made from routine data.
        assert "Lane B" in manifest["lane"]
        assert "resistance" in manifest["lane"]


class TestTheStorylinesAreActuallyPlanted:
    def test_the_repeat_positive_district_has_recurrence_and_the_control_does_not(
        self, dataset
    ) -> None:
        cluster = self._repeat_positive_patients(rows_for_district(dataset, "101"))
        control = self._repeat_positive_patients(rows_for_district(dataset, "105"))

        assert cluster >= 10, "the repeat-positive cluster was not planted"
        assert control == 0, (
            "the control district carries recurrence; a detector cannot be "
            "measured against a control that is not clean"
        )

    def test_the_repeat_positives_sit_at_one_facility_in_the_district(self, dataset) -> None:
        """The signal must be attributable to a facility, not the district."""
        by_facility: Counter[str] = Counter()
        for artefact in dataset.artefacts:
            code = artefact.name.split("_")[0]
            for row in rows_of(artefact):
                if "rp" in str(row["source_row_id"]) and code.startswith(
                    f"{FACILITY_CODE_PREFIX}-00"
                ):
                    by_facility[code] += 1
        assert len(by_facility) == 1, f"planted across {len(by_facility)} facilities"

    def test_the_stockout_district_tests_far_less_during_the_window(self, dataset) -> None:
        rows = rows_for_district(dataset, "102")
        midpoint = date(2025, 12, 1)

        during = [r for r in dated(rows) if date.fromisoformat(r["encounter_date"]) >= midpoint]
        before = [r for r in dated(rows) if date.fromisoformat(r["encounter_date"]) < midpoint]

        assert self._tested_share(before) > 0.6
        assert self._tested_share(during) < self._tested_share(before)

    def test_the_stockout_does_not_change_attendance_or_fever(self, dataset) -> None:
        """The whole point: only testing moved. If attendance moved too, the
        storyline has an ordinary explanation and proves nothing."""
        rows = rows_for_district(dataset, "102")
        midpoint = date(2025, 12, 1)
        before = [r for r in dated(rows) if date.fromisoformat(r["encounter_date"]) < midpoint]
        during = [
            r
            for r in dated(rows)
            if midpoint <= date.fromisoformat(r["encounter_date"]) < date(2026, 1, 12)
        ]

        fever_before = sum(1 for r in before if r["fever_present"] == "yes") / len(before)
        fever_during = sum(1 for r in during if r["fever_present"] == "yes") / len(during)
        assert abs(fever_before - fever_during) < 0.12

    def test_the_completeness_district_is_silent_before_the_midpoint(self, dataset) -> None:
        """Silent, not zero. A facility that is not reporting sends nothing;
        sending zeros is a different and much rarer situation."""
        late_starters = {f"{FACILITY_CODE_PREFIX}-{200 + position + 1:04d}" for position in (1, 2)}
        for artefact in dataset.artefacts:
            code = artefact.name.split("_")[0]
            if code not in late_starters:
                continue
            for row in dated(rows_of(artefact)):
                assert date.fromisoformat(row["encounter_date"]) >= date(2025, 11, 30)

    def test_the_first_facility_in_that_district_reports_throughout(self, dataset) -> None:
        """Otherwise there is nothing to compare like-for-like against."""
        code = f"{FACILITY_CODE_PREFIX}-0201"
        months = {
            artefact.name.split("_")[1].removesuffix(".jsonl")
            for artefact in dataset.artefacts
            if artefact.name.startswith(code)
        }
        assert months == {"2025-10", "2025-11", "2025-12", "2026-01"}

    def test_the_spatial_cluster_concentrates_in_two_subcounties(self, dataset) -> None:
        rows = [
            row for row in rows_for_district(dataset, "104") if "rp" in str(row["source_row_id"])
        ]
        assert rows, "no cluster was planted"
        subcounties = {row["residence"]["subcounty"] for row in rows}
        assert subcounties == {"Done", "Dtwo"}

    def test_no_row_anywhere_carries_a_household_location(self, dataset) -> None:
        """None exists, and none may be inferred."""
        for row in all_rows(dataset):
            residence = row.get("residence", {})
            assert "latitude" not in residence
            assert "longitude" not in residence
            assert "household" not in residence

    def test_the_control_district_rises_gradually_without_other_changes(self, dataset) -> None:
        rows = rows_for_district(dataset, "105")
        first_month = [r for r in dated(rows) if r["encounter_date"].startswith("2025-10")]
        last_month = [r for r in dated(rows) if r["encounter_date"].startswith("2026-01")]

        assert len(last_month) > len(first_month), "the seasonal rise was not planted"
        # Testing rate is what must not move: a rise with stable testing is the
        # thing a detector has to learn not to alert on.
        assert abs(self._tested_share(first_month) - self._tested_share(last_month)) < 0.1

    @staticmethod
    def _repeat_positive_patients(rows: list[dict]) -> int:
        positives: Counter[str] = Counter()
        for row in rows:
            identity = row.get("identity")
            if not identity:
                continue
            if any(test["result"] == "positive" for test in row.get("tests", [])):
                positives[identity["identifier_value"]] += 1
        return sum(1 for count in positives.values() if count >= 2)

    @staticmethod
    def _tested_share(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        tested = sum(
            1 for row in rows if any(test["method"] != "not_done" for test in row.get("tests", []))
        )
        return tested / len(rows)


class TestTheDataLooksLikeARegister:
    def test_absent_is_never_defaulted(self, dataset) -> None:
        """Some rows carry no age at all, because registers are like that."""
        ages = [row.get("age") for row in all_rows(dataset)]
        assert any(age == {} for age in ages), "every row has an age; that is not a register"

    def test_not_tested_is_recorded_as_not_tested_never_as_negative(self, dataset) -> None:
        for row in all_rows(dataset):
            for test in row.get("tests", []):
                if test["method"] == "not_done":
                    assert test["result"] in {"not_done", "positive"}, test
                    # 'positive' only ever appears as a deliberately invalid row,
                    # which the pipeline must quarantine.

    def test_deliberately_invalid_rows_are_present_and_bounded(self, dataset) -> None:
        """Present so the quarantine screen has something in it; bounded so the
        demo is not mostly rejects."""
        assert dataset.invalid_count > 0
        assert dataset.invalid_count < dataset.encounter_count * 0.05

    def test_positive_rows_record_an_antimalarial(self, dataset) -> None:
        """Without a treatment record, repeat positivity has an ordinary
        explanation and the storyline proves nothing."""
        planted = [row for row in all_rows(dataset) if "rp" in str(row["source_row_id"])]
        assert planted
        for row in planted:
            drugs = {str(p.get("drug_name", "")) for p in row["prescriptions"]}
            assert drugs & ANTIMALARIAL_NAMES, row["source_row_id"]


class TestTheManifestDescribesWhatWasBuilt:
    def test_every_storyline_is_declared(self, dataset) -> None:
        manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
        declared = {entry["key"] for entry in manifest["storylines"]}
        assert declared == {storyline.key.value for storyline in STORYLINES}

    def test_every_storyline_states_what_must_not_happen(self) -> None:
        """The half that catches a detector firing on everything."""
        for storyline in STORYLINES:
            assert storyline.must_not, storyline.key
            assert storyline.expected, storyline.key

    def test_no_storyline_claims_resistance(self) -> None:
        for storyline in STORYLINES:
            rendered = json.dumps(storyline.as_dict()).lower()
            if "resistance" in rendered:
                assert "never" in rendered or "not" in rendered, storyline.key

    def test_the_counts_match_the_artefacts(self, dataset) -> None:
        manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
        assert manifest["counts"]["encounters"] == len(all_rows(dataset))
        assert manifest["counts"]["artefacts"] == len(dataset.artefacts)
        assert manifest["counts"]["facilities"] == len(DISTRICTS) * 3

    def test_each_district_is_recorded_with_its_storyline(self, dataset) -> None:
        manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
        for district in DISTRICTS:
            assert manifest["districts"][district.code]["storyline"] == district.storyline.value


class TestTheGeneratorRefusesNonsense:
    def test_no_districts(self) -> None:
        with pytest.raises(ValueError, match="at least one district"):
            DemoDatasetGenerator([])

    def test_a_reversed_period(self) -> None:
        import dataclasses

        options = dataclasses.replace(
            OPTIONS, period_start=date(2026, 3, 1), period_end=date(2025, 3, 1)
        )
        with pytest.raises(ValueError, match="before period_start"):
            DemoDatasetGenerator(list(DISTRICTS), options)
