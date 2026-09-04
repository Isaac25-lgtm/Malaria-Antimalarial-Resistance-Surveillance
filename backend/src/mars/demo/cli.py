"""Command-line entry point for the synthetic demo dataset.

    mars-demo-dataset generate --out-dir demo/            # write the artefacts
    mars-demo-dataset register --out-dir demo/            # create the facilities
    mars-demo-dataset purge    --confirm                  # remove every demo record

``generate`` needs a database only to resolve real districts and subcounties.
It invents no administrative unit: if the geography has not been imported, it
says so and stops, because a demo built on made-up districts cannot be navigated
on the real map and teaches the audience to trust a map that is wrong.

``register`` creates the fictional facilities so the artefacts have somewhere to
load. Every one is marked ``is_synthetic`` and **none is given a coordinate**.

``purge`` removes them again. It only ever touches records whose facility code
carries the demo prefix, and it requires ``--confirm``.

Exit codes: 0 success, 2 usage error, 3 the geography or the dataset is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session

from mars.core.logging import configure_logging, get_logger
from mars.core.settings import get_settings
from mars.db.session import session_scope
from mars.demo.generator import (
    FACILITY_CODE_PREFIX,
    SOURCE_SYSTEM,
    DemoDatasetGenerator,
    DemoDistrict,
    GeneratorOptions,
    parse_period,
)
from mars.demo.storylines import STORYLINES, StorylineKey
from mars.domain.aggregate import AggregateSubmission
from mars.domain.encounter import OpdEncounter
from mars.domain.enums import FacilityLevel, FacilityOwnership, GeographyLevel, OrganisationUnitType
from mars.domain.geography import GeographyUnit
from mars.domain.ingestion import ImportBatch
from mars.domain.organisation import Facility, OrganisationUnit
from mars.domain.signal import SurveillanceSignal

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_INPUT = 3

#: One district per storyline, so the demo has a control and four patterns.
#: Which real district plays which part is a **demo fiction** and says nothing
#: about that district; the manifest repeats this in writing.
STORYLINE_ORDER: tuple[StorylineKey, ...] = tuple(storyline.key for storyline in STORYLINES)

#: The generator's own defaults, so the CLI and the library cannot drift
#: apart. ``GeneratorOptions`` uses slots, so its defaults are read from an
#: instance rather than off the class.
DEFAULTS = GeneratorOptions()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mars-demo-dataset",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("generate", "register", "purge"))
    parser.add_argument("--out-dir", type=Path, help="Where the dataset is written or read")
    parser.add_argument(
        "--district",
        action="append",
        default=None,
        metavar="CODE",
        help=(
            "District code to use, repeatable. Storylines are assigned in the "
            "order given. Defaults to the first districts in the imported "
            "hierarchy, alphabetically by code."
        ),
    )
    parser.add_argument("--seed", type=int, default=DEFAULTS.seed)
    parser.add_argument("--start", type=parse_period, default=DEFAULTS.period_start)
    parser.add_argument("--end", type=parse_period, default=DEFAULTS.period_end)
    parser.add_argument(
        "--facilities-per-district", type=int, default=DEFAULTS.facilities_per_district
    )
    parser.add_argument("--daily-attendance", type=int, default=DEFAULTS.daily_attendance)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required by purge. Without it purge reports what it would delete.",
    )
    return parser


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------
def _resolve_districts(session: Session, codes: list[str] | None) -> list[DemoDistrict] | None:
    """Real districts and their subcounties, from the imported hierarchy.

    Returns ``None`` when the geography is not there. Nothing is invented: a
    demo on fabricated administrative units is a map that lies.
    """
    wanted = len(STORYLINE_ORDER)
    query = select(GeographyUnit).where(
        GeographyUnit.level == GeographyLevel.DISTRICT, GeographyUnit.is_active.is_(True)
    )
    if codes:
        query = query.where(GeographyUnit.preferred_code.in_(codes))
    units = session.execute(query.order_by(GeographyUnit.preferred_code)).scalars().all()

    if codes:
        found = {unit.preferred_code for unit in units}
        absent = [code for code in codes if code not in found]
        if absent:
            print(f"ERROR: district code(s) not in the hierarchy: {absent}", file=sys.stderr)
            return None
        units = sorted(units, key=lambda u: codes.index(u.preferred_code or ""))
    else:
        units = list(units[:wanted])

    if len(units) < wanted:
        print(
            f"ERROR: {wanted} districts are needed, one per storyline; the "
            f"hierarchy yielded {len(units)}. Import the geography first "
            "(mars-import-geography), or name districts with --district.",
            file=sys.stderr,
        )
        return None

    districts: list[DemoDistrict] = []
    for index, unit in enumerate(units[:wanted]):
        subcounties = (
            session.execute(
                select(GeographyUnit.raw_name)
                .where(
                    GeographyUnit.level == GeographyLevel.SUBCOUNTY,
                    GeographyUnit.path.like(f"{unit.path}/%"),
                    GeographyUnit.is_active.is_(True),
                )
                .order_by(GeographyUnit.raw_name)
                .limit(6)
            )
            .scalars()
            .all()
        )
        districts.append(
            DemoDistrict(
                code=unit.preferred_code or str(unit.id),
                name=unit.raw_name,
                storyline=STORYLINE_ORDER[index],
                subcounties=tuple(subcounties),
            )
        )
    return districts


def _generate(args: argparse.Namespace) -> int:
    if args.out_dir is None:
        print("ERROR: --out-dir is required", file=sys.stderr)
        return EXIT_USAGE

    with session_scope() as session:
        districts = _resolve_districts(session, args.district)
    if districts is None:
        return EXIT_MISSING_INPUT

    options = GeneratorOptions(
        seed=args.seed,
        period_start=args.start,
        period_end=args.end,
        facilities_per_district=args.facilities_per_district,
        daily_attendance=args.daily_attendance,
    )
    result = DemoDatasetGenerator(districts, options).generate(args.out_dir)

    print(f"wrote {args.out_dir}")
    print(f"  {result.summary()}")
    print()
    for district in districts:
        print(f"  {district.code:8s} {district.name:<24s} {district.storyline.value}")
    print()
    print(f"  manifest:   {result.manifest_path.name}")
    print(f"  facilities: {result.facilities_path.name}")
    print()
    print("  Next: mars-demo-dataset register --out-dir <dir>")
    print("        mars-import-encounters load --file <dir>/batches/<artefact>.jsonl")
    return EXIT_OK


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
def _register(args: argparse.Namespace) -> int:
    if args.out_dir is None:
        print("ERROR: --out-dir is required", file=sys.stderr)
        return EXIT_USAGE

    facilities_path = args.out_dir / "facilities.json"
    if not facilities_path.is_file():
        print(f"ERROR: {facilities_path} not found; run generate first", file=sys.stderr)
        return EXIT_MISSING_INPUT

    entries = json.loads(facilities_path.read_text(encoding="utf-8"))
    created = 0
    updated = 0

    with session_scope() as session:
        organisation = _demo_organisation_unit(session)
        for entry in entries:
            district = session.execute(
                select(GeographyUnit).where(
                    GeographyUnit.level == GeographyLevel.DISTRICT,
                    GeographyUnit.preferred_code == entry["district_code"],
                )
            ).scalar_one_or_none()
            if district is None:
                print(
                    f"ERROR: district {entry['district_code']} is not in the "
                    "hierarchy; the dataset was generated against a different "
                    "geography import",
                    file=sys.stderr,
                )
                return EXIT_MISSING_INPUT

            facility = session.execute(
                select(Facility).where(Facility.code == entry["code"])
            ).scalar_one_or_none()
            if facility is None:
                facility = Facility(code=entry["code"])
                session.add(facility)
                created += 1
            else:
                updated += 1

            facility.raw_name = entry["name"]
            facility.normalised_name = " ".join(str(entry["name"]).lower().split())
            facility.facility_level = FacilityLevel(entry["facility_level"])
            facility.ownership = FacilityOwnership(entry["ownership"])
            facility.organisation_unit_id = organisation.id
            facility.district_geography_unit_id = district.id
            facility.is_active = True
            # Never a coordinate. A plausible point on a fictional facility is
            # the detail that escapes a demo and gets believed.
            facility.latitude = None
            facility.longitude = None
            facility.coordinate_source = None
            facility.coordinate_validated = False
            facility.is_synthetic = True
            facility.source_system = SOURCE_SYSTEM

        session.flush()

    print(f"registered {created} facilities, updated {updated}")
    print("  every one is marked is_synthetic and carries no coordinate")
    return EXIT_OK


def _demo_organisation_unit(session: Session) -> OrganisationUnit:
    code = "DEMO-ORG"
    unit = session.execute(
        select(OrganisationUnit).where(OrganisationUnit.code == code)
    ).scalar_one_or_none()
    if unit is None:
        unit = OrganisationUnit(
            code=code,
            raw_name="MARS Demo Organisation",
            normalised_name="mars demo organisation",
            # The demo root sits at the top of the organisational hierarchy,
            # which is where a synthetic facility's parent has to hang. The
            # column is NOT NULL: omitting it made ``register`` fail against a
            # real database while passing every test that used a stub session.
            unit_type=OrganisationUnitType.NATIONAL,
            depth=0,
            path=code,
            is_active=True,
        )
        session.add(unit)
        session.flush()
    return unit


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------
def _purge(args: argparse.Namespace) -> int:
    """Remove the demo facilities.

    Scoped by the demo code prefix, and by nothing else. A purge that took a
    date range or a district would eventually be pointed at real data.
    """
    with session_scope() as session:
        facilities = session.execute(
            select(Facility.id, Facility.code).where(
                Facility.code.like(f"{FACILITY_CODE_PREFIX}-%")
            )
        ).all()
        facility_ids = [row.id for row in facilities]
        encounters = session.execute(
            select(func.count())
            .select_from(OpdEncounter)
            .where(
                OpdEncounter.facility_id.in_(facility_ids),
                OpdEncounter.source_system == SOURCE_SYSTEM,
            )
        ).scalar_one()

        if not args.confirm:
            print(f"would delete {len(facilities)} demo facilities and {encounters} encounters")
            print("  re-run with --confirm")
            return EXIT_OK

        # Refuse rather than remove anything that does not unambiguously belong
        # to the synthetic dataset. Derived signals and aggregate submissions
        # are durable records with their own audit meaning; a development reset
        # is the correct cleanup once either exists.
        protected = {
            "non-demo encounters": session.execute(
                select(func.count())
                .select_from(OpdEncounter)
                .where(
                    OpdEncounter.facility_id.in_(facility_ids),
                    OpdEncounter.source_system != SOURCE_SYSTEM,
                )
            ).scalar_one(),
            "non-demo import batches": session.execute(
                select(func.count())
                .select_from(ImportBatch)
                .where(
                    ImportBatch.facility_id.in_(facility_ids),
                    ImportBatch.source_system != SOURCE_SYSTEM,
                )
            ).scalar_one(),
            "aggregate submissions": session.execute(
                select(func.count())
                .select_from(AggregateSubmission)
                .where(AggregateSubmission.facility_id.in_(facility_ids))
            ).scalar_one(),
            "surveillance signals": session.execute(
                select(func.count())
                .select_from(SurveillanceSignal)
                .where(SurveillanceSignal.facility_id.in_(facility_ids))
            ).scalar_one(),
        }
        blockers = {name: count for name, count in protected.items() if count}
        if blockers:
            print(
                "REFUSED: demo facilities have durable dependent records: "
                + ", ".join(f"{name}={count}" for name, count in blockers.items()),
                file=sys.stderr,
            )
            print("Recreate the disposable development database instead.", file=sys.stderr)
            return EXIT_USAGE

        removal = session.execute(
            delete(OpdEncounter).where(
                OpdEncounter.facility_id.in_(facility_ids),
                OpdEncounter.source_system == SOURCE_SYSTEM,
            )
        )
        deleted = cast("CursorResult[Any]", removal).rowcount
        session.execute(
            delete(ImportBatch).where(
                ImportBatch.facility_id.in_(facility_ids),
                ImportBatch.source_system == SOURCE_SYSTEM,
            )
        )
        session.execute(delete(Facility).where(Facility.code.like(f"{FACILITY_CODE_PREFIX}-%")))
        print(f"deleted {deleted} demo encounters and {len(facilities)} demo facilities")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())

    if args.command == "generate":
        return _generate(args)
    if args.command == "register":
        return _register(args)
    return _purge(args)


def run() -> None:  # pragma: no cover - console script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
