"""The synthetic demo dataset generator.

Produces **inbound-contract artefacts**, not database rows. The demo therefore
loads through exactly the pipeline real data loads through, is validated by the
same validator, and is quarantined by the same rules. A generator that wrote
straight into ``mars_core`` would prove nothing about the system and would let
the demo drift into a shape real data can never take.

Everything is deterministic. The same seed produces byte-identical artefacts,
because this dataset is also the golden fixture later detector work is measured
against, and a fixture that changes under you is not a fixture. No wall-clock
time and no unseeded randomness appear anywhere below.

Everything is fictional. Facilities carry ``is_synthetic``; patient identifiers
carry a ``SYN`` prefix that no real scheme issues; **no facility has a
coordinate**, because a plausible coordinate on a fictional facility is exactly
the kind of detail that escapes a demo and gets believed.

What the generator does *not* invent: districts and subcounties. Those come
from the imported Uganda geography, by code, and the caller supplies them. A
generator that made up administrative units would produce a demo that cannot be
navigated and a map that cannot be trusted.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from mars.demo.storylines import STORYLINES, Storyline, StorylineKey

#: The contract version the artefacts declare. Bumped only when the contract is.
SCHEMA_VERSION = "1.0"

#: Named so that a row in a log, a quarantine table or a screenshot is instantly
#: recognisable as demo data rather than a leak.
SOURCE_SYSTEM = "mars-demo-ereg"

#: No real identifier scheme issues values with this prefix. Deliberate: a
#: synthetic identifier that looks real is the one that ends up in an email.
IDENTIFIER_PREFIX = "SYN"

#: Facility codes carry it too, for the same reason.
FACILITY_CODE_PREFIX = "DEMO-HF"

_ANTIMALARIALS = (
    ("Artemether/Lumefantrine", "1x2x3"),
    ("Artesunate/Amodiaquine", "1x1x3"),
    ("Dihydroartemisinin/Piperaquine", "1x1x3"),
)
_OTHER_DRUGS = (
    ("Paracetamol", "2x3x3"),
    ("Amoxicillin", "1x3x5"),
    ("Oral Rehydration Salts", "1x3x2"),
)
_MALARIA_DIAGNOSES = ("Malaria, uncomplicated", "Malaria, severe")
_OTHER_DIAGNOSES = (
    "Acute respiratory infection",
    "Diarrhoea, acute",
    "Urinary tract infection",
    "Skin infection",
)
_COMPLAINTS = ("fever", "fever, headache", "fever and chills", "headache, body pains")


@dataclass(frozen=True, slots=True)
class DemoDistrict:
    """A real district, named by the caller, carrying a demo storyline.

    Real because the demo has to be navigable on the real map. Named by the
    caller because the generator has no business choosing which district gets a
    stock-out.
    """

    code: str
    name: str
    storyline: StorylineKey
    #: Subcounty names inside this district, used for the residence fields. The
    #: caller resolves them from the imported hierarchy.
    subcounties: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DemoFacility:
    """A fictional facility. No coordinate, ever."""

    code: str
    name: str
    district_code: str
    level: str
    ownership: str
    storyline: StorylineKey
    #: Index within its district, so a storyline can single one out.
    position: int

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "district_code": self.district_code,
            "facility_level": self.level,
            "ownership": self.ownership,
            "storyline": self.storyline.value,
            "is_synthetic": True,
            "latitude": None,
            "longitude": None,
        }


@dataclass(frozen=True, slots=True)
class GeneratorOptions:
    """The shape of the dataset. Every field affects the output deterministically."""

    seed: int = 20260301
    period_start: date = date(2025, 10, 1)
    period_end: date = date(2026, 3, 31)
    facilities_per_district: int = 3
    #: Mean encounters per facility per day before storyline effects.
    daily_attendance: int = 6
    #: Share of rows carrying a usable identifier. Real registers are far from
    #: complete, and a demo where every row links would hide how linkage
    #: actually behaves.
    identified_share: float = 0.55
    #: Share of rows deliberately made invalid, so the quarantine path is
    #: visible in the demo rather than only in the tests.
    invalid_share: float = 0.02


@dataclass
class GeneratedDataset:
    """What a run produced."""

    manifest_path: Path
    facilities_path: Path
    artefacts: list[Path] = field(default_factory=list)
    encounter_count: int = 0
    identified_count: int = 0
    invalid_count: int = 0
    repeat_positive_patients: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.artefacts)} artefacts, {self.encounter_count} encounters, "
            f"{self.identified_count} identified, {self.invalid_count} deliberately "
            f"invalid, {self.repeat_positive_patients} repeat-positive demo patients"
        )


class DemoDatasetGenerator:
    """Builds the demo dataset for a set of districts."""

    def __init__(
        self, districts: list[DemoDistrict], options: GeneratorOptions | None = None
    ) -> None:
        if not districts:
            raise ValueError("at least one district is required")
        missing = {s.storyline for s in districts} - {s.key for s in STORYLINES}
        if missing:
            raise ValueError(f"unknown storylines: {sorted(m.value for m in missing)}")

        self._districts = districts
        self._options = options or GeneratorOptions()
        if self._options.period_end < self._options.period_start:
            raise ValueError("period_end is before period_start")

        self._random = random.Random(self._options.seed)
        self._patient_counter = 0

    # -- Entry point -------------------------------------------------------
    def generate(self, out_dir: Path) -> GeneratedDataset:
        out_dir.mkdir(parents=True, exist_ok=True)
        batches_dir = out_dir / "batches"
        batches_dir.mkdir(exist_ok=True)

        facilities = self._build_facilities()
        result = GeneratedDataset(
            manifest_path=out_dir / "manifest.json",
            facilities_path=out_dir / "facilities.json",
        )

        planted: dict[str, object] = {}
        for district in self._districts:
            district_facilities = [f for f in facilities if f.district_code == district.code]
            rows_by_month = self._district_rows(district, district_facilities, result)
            for (facility_code, month), rows in sorted(rows_by_month.items()):
                path = batches_dir / f"{facility_code}_{month}.jsonl"
                self._write_artefact(path, facility_code, month, rows)
                result.artefacts.append(path)
            planted[district.code] = {
                "storyline": district.storyline.value,
                "facilities": [f.code for f in district_facilities],
            }

        result.facilities_path.write_text(
            json.dumps([f.as_dict() for f in facilities], indent=2) + "\n", encoding="utf-8"
        )
        result.manifest_path.write_text(
            json.dumps(self._manifest(facilities, planted, result), indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    # -- Facilities --------------------------------------------------------
    def _build_facilities(self) -> list[DemoFacility]:
        levels = ("hc_ii", "hc_iii", "hc_iv", "general_hospital")
        facilities: list[DemoFacility] = []
        for district_index, district in enumerate(self._districts):
            for position in range(self._options.facilities_per_district):
                number = district_index * 100 + position + 1
                facilities.append(
                    DemoFacility(
                        code=f"{FACILITY_CODE_PREFIX}-{number:04d}",
                        name=f"Demo {district.name.title()} Health Centre {position + 1}",
                        district_code=district.code,
                        level=levels[position % len(levels)],
                        ownership="government" if position % 3 else "private_not_for_profit",
                        storyline=district.storyline,
                        position=position,
                    )
                )
        return facilities

    # -- Rows --------------------------------------------------------------
    def _district_rows(
        self,
        district: DemoDistrict,
        facilities: list[DemoFacility],
        result: GeneratedDataset,
    ) -> dict[tuple[str, str], list[dict[str, object]]]:
        rows: dict[tuple[str, str], list[dict[str, object]]] = {}

        for facility in facilities:
            for day in self._days():
                if not self._facility_reports(district, facility, day):
                    continue
                count = self._attendance(district, facility, day)
                for index in range(count):
                    row = self._encounter(district, facility, day, index, result)
                    key = (facility.code, f"{day.year:04d}-{day.month:02d}")
                    rows.setdefault(key, []).append(row)

        # The planted patterns are added after the baseline, so the baseline
        # itself carries none of them - which is what makes the control district
        # a control.
        self._plant(district, facilities, rows, result)
        return rows

    def _days(self) -> Iterator[date]:
        day = self._options.period_start
        while day <= self._options.period_end:
            yield day
            day += timedelta(days=1)

    def _facility_reports(self, district: DemoDistrict, facility: DemoFacility, day: date) -> bool:
        """Whether this facility reported on this day.

        The completeness storyline works by *silence*, not by zeros: a facility
        that is not reporting sends nothing at all. Sending zeros would be a
        different and much rarer real-world situation, and conflating the two is
        exactly the mistake the storyline exists to expose.
        """
        if day.weekday() == 6:  # Sunday: outpatient attendance is minimal
            return False
        silent = (
            district.storyline is StorylineKey.COMPLETENESS_ARTEFACT
            and facility.position >= 1
            and day < self._midpoint()
        )
        return not silent

    def _midpoint(self) -> date:
        span = (self._options.period_end - self._options.period_start).days
        return self._options.period_start + timedelta(days=span // 2)

    def _attendance(self, district: DemoDistrict, facility: DemoFacility, day: date) -> int:
        base = self._options.daily_attendance + facility.position
        if district.storyline is StorylineKey.SEASONAL_CONTROL:
            # A gradual rise in the shape of a season. Deliberately smooth: the
            # control exists to check that "high" alone does not alert.
            elapsed = (day - self._options.period_start).days
            span = max((self._options.period_end - self._options.period_start).days, 1)
            base = int(base * (1.0 + 0.6 * elapsed / span))
        return max(1, base + self._random.randint(-1, 2))

    def _encounter(
        self,
        district: DemoDistrict,
        facility: DemoFacility,
        day: date,
        index: int,
        result: GeneratedDataset,
        *,
        force_positive: bool = False,
        patient: str | None = None,
    ) -> dict[str, object]:
        rng = self._random
        source_row_id = f"{facility.code}-{day.isoformat()}-{index:03d}"

        tested = force_positive or rng.random() < self._testing_rate(district, day)
        if force_positive:
            method, test_result = "rdt", "positive"
        elif tested:
            method = "rdt" if rng.random() < 0.8 else "microscopy"
            test_result = "positive" if rng.random() < 0.32 else "negative"
        else:
            # Not tested is recorded as not tested. It is never a negative: "no
            # test was done" and "a test found nothing" are different facts.
            method, test_result = "not_done", "not_done"

        malaria = test_result == "positive"
        row: dict[str, object] = {
            "record_type": "encounter",
            "source_row_id": source_row_id,
            "serial_number": f"{index + 1:03d}",
            "encounter_date": day.isoformat(),
            "date_source": "source_supplied",
            "sex": rng.choice(["M", "F"]),
            "patient_category": "N" if rng.random() < 0.94 else rng.choice(["R", "F"]),
            "attendance_type": "re_attendance" if rng.random() < 0.18 else "new_attendance",
            "fever_present": "yes" if malaria or rng.random() < 0.6 else "no",
            "presenting_complaint": rng.choice(_COMPLAINTS),
            "age": self._age(rng),
            "residence": self._residence(district, rng),
            "tests": [{"method": method, "result": test_result}],
            "diagnoses": [
                rng.choice(_MALARIA_DIAGNOSES) if malaria else rng.choice(_OTHER_DIAGNOSES)
            ],
            "prescriptions": self._prescriptions(rng, malaria=malaria, tested=tested),
        }

        identifier = patient or self._maybe_identifier(rng)
        if identifier is not None:
            row["identity"] = self._identity(identifier, rng)
            result.identified_count += 1

        if not force_positive and rng.random() < self._options.invalid_share:
            self._make_invalid(row, rng)
            result.invalid_count += 1

        result.encounter_count += 1
        return row

    def _testing_rate(self, district: DemoDistrict, day: date) -> float:
        """The share of attendances that get a malaria test.

        The stock-out storyline moves this and nothing else. Attendance, fever
        reporting and the positivity of the tests still performed are all held
        constant, so the only honest reading of the resulting case decline is a
        testing one.
        """
        if district.storyline is not StorylineKey.TESTING_ANOMALY_STOCKOUT:
            return 0.78
        start = self._midpoint()
        if start <= day < start + timedelta(days=42):
            return 0.18
        return 0.78

    def _age(self, rng: random.Random) -> dict[str, object]:
        draw = rng.random()
        if draw < 0.06:
            return {"value": rng.randint(1, 11), "unit": "months"}
        if draw < 0.08:
            return {"value": rng.randint(1, 30), "unit": "days"}
        if draw < 0.12:
            # Not recorded. Left absent rather than defaulted, which is what a
            # register looks like.
            return {}
        return {"value": rng.randint(1, 84), "unit": "years"}

    def _residence(self, district: DemoDistrict, rng: random.Random) -> dict[str, object]:
        residence: dict[str, object] = {"district": district.name}
        if district.subcounties:
            if district.storyline is StorylineKey.SPATIAL_CLUSTER:
                # The baseline spreads across the whole district; the cluster is
                # planted separately, so the concentration is genuinely planted
                # rather than an artefact of a skewed baseline.
                residence["subcounty"] = rng.choice(district.subcounties)
            else:
                residence["subcounty"] = rng.choice(district.subcounties)
        return residence

    def _prescriptions(
        self, rng: random.Random, *, malaria: bool, tested: bool
    ) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        if malaria or (not tested and rng.random() < 0.12):
            drug, pattern = rng.choice(_ANTIMALARIALS)
            units, doses, days = (int(part) for part in pattern.split("x"))
            entries.append(
                {
                    "text": f"{drug} {pattern}",
                    "drug_name": drug,
                    "units_per_dose": units,
                    "doses_per_day": doses,
                    "days": days,
                }
            )
        if rng.random() < 0.5:
            drug, pattern = rng.choice(_OTHER_DRUGS)
            units, doses, days = (int(part) for part in pattern.split("x"))
            entries.append(
                {
                    "text": f"{drug} {pattern}",
                    "drug_name": drug,
                    "units_per_dose": units,
                    "doses_per_day": doses,
                    "days": days,
                }
            )
        return entries

    def _maybe_identifier(self, rng: random.Random) -> str | None:
        if rng.random() >= self._options.identified_share:
            return None
        self._patient_counter += 1
        return f"{IDENTIFIER_PREFIX}{self._patient_counter:08d}"

    def _identity(self, identifier: str, rng: random.Random) -> dict[str, object]:
        """The identity block. Fictional, and consumed inside the vault.

        Names are drawn from a deliberately small, obviously placeholder set:
        a demo does not need convincing names, and convincing names are what
        make a synthetic record survive being pasted into a real system.
        """
        return {
            "identifier_type": "national_id" if rng.random() < 0.9 else "unspecified_scheme",
            "identifier_value": identifier,
            "surname": f"Demo{identifier[-4:]}",
            "given_name": f"Patient{identifier[-2:]}",
        }

    def _make_invalid(self, row: dict[str, object], rng: random.Random) -> None:
        """Break one field, in one of the ways a real register breaks.

        Present in the demo on purpose: an operator should see the quarantine
        screen with something in it, and a demo where every row is clean teaches
        the wrong expectation of real data.
        """
        choice = rng.randint(0, 3)
        if choice == 0:
            row["sex"] = "X"  # a code outside the value set
        elif choice == 1:
            row["age"] = {"value": 3}  # a number with no unit
        elif choice == 2:
            row["tests"] = [{"method": "not_done", "result": "positive"}]
        else:
            row["encounter_date"] = ""  # a row that cannot be placed in time

    # -- Planted patterns --------------------------------------------------
    def _plant(
        self,
        district: DemoDistrict,
        facilities: list[DemoFacility],
        rows: dict[tuple[str, str], list[dict[str, object]]],
        result: GeneratedDataset,
    ) -> None:
        if district.storyline is StorylineKey.REPEAT_POSITIVE_CLUSTER:
            self._plant_repeat_positives(district, facilities[0], rows, result, patients=14)
        elif district.storyline is StorylineKey.SPATIAL_CLUSTER:
            # Concentrated in the first two subcounties, and spread across the
            # district's facilities so the pattern is spatial rather than a
            # single facility's.
            for facility in facilities:
                self._plant_repeat_positives(
                    district,
                    facility,
                    rows,
                    result,
                    patients=6,
                    subcounties=district.subcounties[:2],
                )

    def _plant_repeat_positives(
        self,
        district: DemoDistrict,
        facility: DemoFacility,
        rows: dict[tuple[str, str], list[dict[str, object]]],
        result: GeneratedDataset,
        *,
        patients: int,
        subcounties: tuple[str, ...] = (),
    ) -> None:
        """Patients who test positive again within the recurrence window.

        Every planted visit records a full antimalarial course. Without that the
        pattern would have an ordinary explanation - the patient was never
        treated - and the storyline would prove nothing.
        """
        rng = self._random
        span = (self._options.period_end - self._options.period_start).days
        window = min(42, max(span - 1, 1))

        for _ in range(patients):
            self._patient_counter += 1
            identifier = f"{IDENTIFIER_PREFIX}{self._patient_counter:08d}"
            first = self._options.period_start + timedelta(
                days=rng.randint(0, max(span - window - 1, 0))
            )
            visits = [first]
            for step in range(rng.randint(1, 2)):
                visits.append(first + timedelta(days=rng.randint(14, window) * (step + 1)))

            for visit_index, day in enumerate(visits):
                if day > self._options.period_end:
                    continue
                row = self._encounter(
                    district,
                    facility,
                    day,
                    900 + visit_index,
                    result,
                    force_positive=True,
                    patient=identifier,
                )
                row["source_row_id"] = f"{facility.code}-{day.isoformat()}-rp{visit_index:02d}"
                row["attendance_type"] = "new_attendance" if visit_index == 0 else "re_attendance"
                if subcounties:
                    row["residence"] = {
                        "district": district.name,
                        "subcounty": subcounties[visit_index % len(subcounties)],
                    }
                key = (facility.code, f"{day.year:04d}-{day.month:02d}")
                rows.setdefault(key, []).append(row)

            result.repeat_positive_patients += 1

    # -- Output ------------------------------------------------------------
    def _write_artefact(
        self, path: Path, facility_code: str, month: str, rows: list[dict[str, object]]
    ) -> None:
        rows = sorted(rows, key=lambda r: (str(r["encounter_date"]), str(r["source_row_id"])))
        envelope = {
            "record_type": "envelope",
            "schema_version": SCHEMA_VERSION,
            "source_system": SOURCE_SYSTEM,
            "facility_code": facility_code,
            # A fixed timestamp, not "now": the artefacts must be byte-identical
            # for a given seed, and a generation time would change every run.
            "extracted_at": f"{month}-01T00:00:00+00:00",
            "row_count": len(rows),
            "register_opened_on": f"{month}-01",
            "register_closed_on": _month_end(month).isoformat(),
        }
        lines = [json.dumps(envelope, sort_keys=True)]
        lines.extend(json.dumps(row, sort_keys=True) for row in rows)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _manifest(
        self,
        facilities: list[DemoFacility],
        planted: dict[str, object],
        result: GeneratedDataset,
    ) -> dict[str, object]:
        return {
            "dataset": "MARS synthetic demo dataset",
            "synthetic": True,
            "warning": (
                "Every facility, patient and identifier in this dataset is "
                "fictional. Districts and subcounties are real administrative "
                "units so the demo is navigable on the real map; the assignment "
                "of a storyline to a district is a demo fiction and describes "
                "nothing about that district."
            ),
            "lane": (
                "Everything here is Lane A: routine-derived. A storyline may "
                "produce a surveillance signal. None of it is, or can become, a "
                "confirmed antimalarial resistance finding - that is Lane B, "
                "established externally under separate governance."
            ),
            "schema_version": SCHEMA_VERSION,
            "source_system": SOURCE_SYSTEM,
            "seed": self._options.seed,
            "period": {
                "start": self._options.period_start.isoformat(),
                "end": self._options.period_end.isoformat(),
            },
            "counts": {
                "districts": len(self._districts),
                "facilities": len(facilities),
                "artefacts": len(result.artefacts),
                "encounters": result.encounter_count,
                "with_identifier": result.identified_count,
                "deliberately_invalid": result.invalid_count,
                "repeat_positive_patients": result.repeat_positive_patients,
            },
            "districts": planted,
            "storylines": [storyline.as_dict() for storyline in STORYLINES],
        }


def _month_end(month: str) -> date:
    year, month_number = (int(part) for part in month.split("-"))
    if month_number == 12:
        return date(year, 12, 31)
    return date(year, month_number + 1, 1) - timedelta(days=1)


def storyline_for(key: str) -> Storyline:
    """Look a storyline up by its manifest key."""
    return {s.key.value: s for s in STORYLINES}[key]


def parse_period(value: str) -> date:
    """Parse a CLI date, refusing anything ambiguous."""
    return datetime.strptime(value, "%Y-%m-%d").date()


__all__ = [
    "FACILITY_CODE_PREFIX",
    "IDENTIFIER_PREFIX",
    "SCHEMA_VERSION",
    "SOURCE_SYSTEM",
    "DemoDatasetGenerator",
    "DemoDistrict",
    "DemoFacility",
    "GeneratedDataset",
    "GeneratorOptions",
    "parse_period",
    "storyline_for",
]
