"""Geography importer.

Builds the five-level administrative hierarchy from the supplied sources, loads
validated geometry into PostGIS, and publishes the result as a boundary version.

Design commitments, all from ADR 0004:

* **Sources are immutable.** They are opened read-only and their checksums are
  recorded. Defects are repaired only in derived geometry.
* **Identity is stable.** Units are keyed by UUID and matched on
  ``(level, preferred_code)`` across imports, so a facility or a user's
  geography scope keeps pointing at the same unit when the boundary is
  re-imported. A unit absent from a newer source is deactivated, never deleted.
* **Source codes are aliases.** ``FScode`` is recorded under source system
  ``ubos_fscode``, never used as a key.
* **Publication is all or nothing.** A failed validation leaves the previously
  published version untouched and retains the failed attempt with its report.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.core.timeutils import utc_now
from mars.domain.enums import (
    AliasMatchStatus,
    BoundaryImportStatus,
    GeographyLevel,
    GeographyUnitKind,
    GeometryValidityState,
)
from mars.domain.geography import (
    BoundaryVersion,
    GeographyUnit,
    GeographyUnitAlias,
    GeographyUnitGeometry,
    GeographyUnitRevision,
)
from mars.geo import fscode as fs
from mars.geo.naming import extract_alias_names, infer_unit_kind, name_defects, normalise_name
from mars.ingestion.geography import geometry as geom
from mars.ingestion.geography.reader import (
    BoundarySourceReader,
    SourceFile,
    SourceProfile,
    SourceRole,
)
from mars.ingestion.geography.result import ImportOutcome, ImportResult

logger = get_logger(__name__)

#: Bumped whenever the importer's behaviour changes in a way that would produce
#: different output from identical source bytes. Recorded on every boundary
#: version so a stored hierarchy can be traced to the code that built it.
IMPORTER_VERSION = "1.0.0"

#: Source system recorded on aliases derived from the six-digit source code.
FSCODE_SOURCE_SYSTEM = fs.SOURCE_SYSTEM

#: Source system for aliases recorded from a supplied name.
NAME_SOURCE_SYSTEM = "ubos_boundary_name"

#: Display name for the country unit. The supplied country file carries no name
#: attribute; this is the one name the importer supplies, and it is the subject
#: of the entire dataset rather than an inference about it.
COUNTRY_NAME = "Uganda"

#: The area control total must agree to this relative tolerance. The audited
#: sources agree exactly; anything beyond floating-point noise means the sources
#: are no longer the coherent set ADR 0004 describes.
AREA_TOLERANCE = 1e-6


@dataclass(slots=True)
class ImportOptions:
    """How one import run should behave."""

    #: Validate and report without writing anything.
    dry_run: bool = False

    #: Load geometry. Off makes a hierarchy-only run fast, for development.
    load_geometry: bool = True

    #: Derive region and county geometry by dissolving their children in
    #: PostGIS. Off leaves those levels without geometry, which is a legitimate
    #: intermediate state - the units still exist and still scope correctly.
    derive_geometry: bool = True

    #: Build the simplified browser geometry. Raw subcounty geometry must never
    #: reach a client, so this is on unless geometry is skipped entirely.
    simplify_geometry: bool = True

    #: Re-import even when these exact source bytes are already published.
    force: bool = False

    #: Recorded on the boundary version. A service label, never a personal name.
    imported_by: str = "geography-importer"

    #: Free-text note stored with the version.
    note: str | None = None


@dataclass(slots=True)
class _PendingUnit:
    """A unit assembled in memory before it is written."""

    level: GeographyLevel
    preferred_code: str
    raw_name: str
    normalised_name: str
    parent_code: str | None
    unit_kind: GeographyUnitKind
    depth: int
    path: str
    source_record_id: str | None = None
    #: (source_system, source_code, source_name, match_method)
    aliases: list[tuple[str, str, str | None, str]] = field(default_factory=list)
    geometry: dict[str, Any] | None = None
    assessment: geom.GeometryAssessment | None = None
    name_defects: list[str] = field(default_factory=list)


class GeographyImporter:
    """Imports the supplied boundary sources into the canonical model."""

    def __init__(self, session: Session, sources: dict[SourceRole, Path]) -> None:
        self._session = session
        self._sources = sources

    # -- Public entry point -------------------------------------------------
    def run(self, options: ImportOptions | None = None) -> ImportResult:
        """Execute one import.

        The caller owns the transaction. On a blocking failure this raises
        nothing - it returns a result whose outcome says what happened - but it
        does leave the session dirty, so the caller must roll back. The CLI and
        worker entry points both do.
        """
        options = options or ImportOptions()
        started = time.perf_counter()

        result = ImportResult(
            outcome=ImportOutcome.FAILED,
            importer_version=IMPORTER_VERSION,
            started_at=utc_now().isoformat().replace("+00:00", "Z"),
        )

        profiles = self._profile_sources(result)
        if result.blocking_issues:
            return self._finish(result, started, ImportOutcome.VALIDATION_FAILED)

        combined = self._combined_checksum(profiles)
        result.combined_checksum = combined
        result.sources = [profile.as_dict() for profile in profiles.values()]

        existing = self._published_version_for(combined)
        if existing is not None and not options.force:
            result.boundary_version_id = existing.id
            result.boundary_version_code = existing.code
            result.notes.append(
                "These exact source bytes are already published as "
                f"{existing.code}. No second version was created."
            )
            return self._finish(result, started, ImportOutcome.ALREADY_IMPORTED)

        pending = self._build_hierarchy(profiles, result, options)
        self._validate_hierarchy(pending, result)
        self._validate_control_totals(pending, result)

        if result.blocking_issues:
            outcome = ImportOutcome.VALIDATION_FAILED
            if not options.dry_run:
                self._record_failed_attempt(combined, profiles, result, options)
            return self._finish(result, started, outcome)

        if options.dry_run:
            result.notes.append("Dry run: validated only, nothing written.")
            return self._finish(result, started, ImportOutcome.VALIDATED_ONLY)

        version = self._create_version(combined, profiles, options)
        result.boundary_version_id = version.id
        result.boundary_version_code = version.code

        self._persist(pending, version, result, options)

        if options.load_geometry and options.derive_geometry:
            self._derive_parent_geometry(version, result, options)

        # The outcome is set before publishing, because _publish writes the
        # result document onto the version. Setting it afterwards stored a
        # summary that reported the pre-publication state.
        result.outcome = ImportOutcome.PUBLISHED
        self._publish(version, result)
        return self._finish(result, started, ImportOutcome.PUBLISHED)

    # -- Sources ------------------------------------------------------------
    def _profile_sources(self, result: ImportResult) -> dict[SourceRole, SourceProfile]:
        profiles: dict[SourceRole, SourceProfile] = {}
        for role, path in self._sources.items():
            if not path.exists():
                result.add_issue(
                    "source_missing",
                    f"{path.name} was not found",
                    blocking=True,
                    context={"role": role.value, "path": str(path)},
                )
                continue
            reader = BoundarySourceReader(SourceFile(path=path, role=role))
            profiles[role] = reader.profile()
        return profiles

    @staticmethod
    def _combined_checksum(profiles: dict[SourceRole, SourceProfile]) -> str:
        """One checksum identifying the whole source set.

        Ordered by role so the value does not depend on dictionary ordering. Any
        source changing produces a different combined checksum, which is what
        makes re-import detection exact rather than approximate.
        """
        digest = hashlib.sha256()
        for role in sorted(profiles, key=lambda item: item.value):
            digest.update(role.value.encode())
            digest.update(profiles[role].sha256.encode())
        return digest.hexdigest()

    def _published_version_for(self, combined: str) -> BoundaryVersion | None:
        return self._session.execute(
            select(BoundaryVersion).where(
                BoundaryVersion.source_checksum == combined,
                BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED,
            )
        ).scalar_one_or_none()

    # -- Hierarchy ----------------------------------------------------------
    def _build_hierarchy(
        self,
        profiles: dict[SourceRole, SourceProfile],
        result: ImportResult,
        options: ImportOptions,
    ) -> dict[tuple[GeographyLevel, str], _PendingUnit]:
        """Assemble every unit from the sources.

        The subcounty source is the hierarchy spine: it is the only file
        carrying FScode, County, District and RCode together, so region,
        district and county units are all derived from it. District *geometry*
        comes from the district source, joined on name - safe only because both
        sets are exactly 146 and match one to one.
        """
        pending: dict[tuple[GeographyLevel, str], _PendingUnit] = {}

        # -- Country --------------------------------------------------------
        country = _PendingUnit(
            level=GeographyLevel.COUNTRY,
            preferred_code=fs.COUNTRY_CODE,
            raw_name=COUNTRY_NAME,
            normalised_name=normalise_name(COUNTRY_NAME),
            parent_code=None,
            unit_kind=GeographyUnitKind.UNSPECIFIED,
            depth=0,
            path=fs.COUNTRY_CODE,
        )
        pending[(GeographyLevel.COUNTRY, fs.COUNTRY_CODE)] = country

        if SourceRole.COUNTRY_BOUNDARY in profiles and options.load_geometry:
            self._attach_country_geometry(country, result)

        # -- Region, district, county, subcounty from the spine -------------
        self._read_spine(pending, result, options)

        # -- District geometry from its own source --------------------------
        if SourceRole.DISTRICT_GEOMETRY in profiles and options.load_geometry:
            self._attach_district_geometry(pending, result)

        return pending

    def _attach_country_geometry(self, country: _PendingUnit, result: ImportResult) -> None:
        reader = BoundarySourceReader(
            SourceFile(
                path=self._sources[SourceRole.COUNTRY_BOUNDARY],
                role=SourceRole.COUNTRY_BOUNDARY,
            )
        )
        for feature in reader.read():
            assessment = geom.assess(feature.geometry, label="country boundary")
            country.geometry = assessment.prepared
            country.assessment = assessment
            if not assessment.is_usable:
                result.add_issue(
                    "country_geometry_unusable",
                    "The country boundary geometry could not be prepared",
                    blocking=True,
                    context={"issues": assessment.issue_codes()},
                )
            break

    def _read_spine(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        result: ImportResult,
        options: ImportOptions,
    ) -> None:
        """Read the subcounty source and derive four levels from it."""
        reader = BoundarySourceReader(
            SourceFile(
                path=self._sources[SourceRole.SUBCOUNTY_HIERARCHY],
                role=SourceRole.SUBCOUNTY_HIERARCHY,
            )
        )

        seen_codes: set[str] = set()

        for feature in reader.read():
            props = feature.properties
            raw_code = props.get("FScode")
            if raw_code is None:
                result.add_issue(
                    "source_code_missing",
                    f"feature {feature.index} has no FScode",
                    blocking=True,
                    context={"feature_index": feature.index},
                )
                continue

            try:
                parts = fs.parse_fscode(raw_code)
            except fs.InvalidFsCodeError as error:
                result.add_issue(
                    "unparsable_source_code",
                    f"feature {feature.index}: {error}",
                    blocking=True,
                    context={"feature_index": feature.index, "value": str(raw_code)},
                )
                continue

            if parts.subcounty in seen_codes:
                result.add_issue(
                    "duplicate_source_code",
                    f"FScode {parts.subcounty} appears more than once",
                    blocking=True,
                    context={"code": parts.subcounty},
                )
                continue
            seen_codes.add(parts.subcounty)

            declared_region = str(props.get("RCode", "")).strip()
            if declared_region and parts.region != declared_region:
                result.add_issue(
                    "region_code_disagreement",
                    f"FScode {parts.subcounty} starts with {parts.region} "
                    f"but RCode is {declared_region}",
                    blocking=True,
                    context={"code": parts.subcounty},
                )
                continue

            self._ensure_region(pending, parts, result)
            self._ensure_district(pending, parts, props, result)
            self._ensure_county(pending, parts, props, result)
            self._add_subcounty(pending, parts, props, feature, result, options)

    def _ensure_region(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        parts: fs.FsCodeParts,
        result: ImportResult,
    ) -> None:
        key = (GeographyLevel.REGION, parts.region)
        if key in pending:
            return

        # The sources carry a region *code* and nothing else. Naming the regions
        # would require outside knowledge, which ADR 0003 forbids, so the code
        # is the name and the gap is reported.
        unit = _PendingUnit(
            level=GeographyLevel.REGION,
            preferred_code=parts.region,
            raw_name=parts.region,
            normalised_name=parts.region,
            parent_code=fs.COUNTRY_CODE,
            unit_kind=GeographyUnitKind.UNSPECIFIED,
            depth=1,
            path=f"{fs.COUNTRY_CODE}/{parts.region}",
        )
        unit.aliases.append((FSCODE_SOURCE_SYSTEM, parts.region, None, "source_code_derivation"))
        pending[key] = unit

        result.add_issue(
            "region_name_unresolved",
            f"Region {parts.region} has no name in the supplied sources; "
            "the code is used as the display name until an authoritative "
            "region list is provided",
            blocking=False,
            context={"code": parts.region},
        )

    def _ensure_district(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        parts: fs.FsCodeParts,
        props: dict[str, Any],
        result: ImportResult,
    ) -> None:
        key = (GeographyLevel.DISTRICT, parts.district)
        raw_name = str(props.get("District", "")).strip()

        if key in pending:
            existing = pending[key]
            if existing.raw_name != raw_name and raw_name:
                result.add_issue(
                    "district_name_disagreement",
                    f"District code {parts.district} appears as "
                    f"{existing.raw_name!r} and {raw_name!r}",
                    blocking=True,
                    context={"code": parts.district},
                )
            return

        if not raw_name:
            result.add_issue(
                "district_name_missing",
                f"District code {parts.district} has no name",
                blocking=True,
                context={"code": parts.district},
            )
            return

        unit = _PendingUnit(
            level=GeographyLevel.DISTRICT,
            preferred_code=parts.district,
            raw_name=raw_name,
            normalised_name=normalise_name(raw_name),
            parent_code=parts.region,
            unit_kind=infer_unit_kind(raw_name),
            depth=2,
            path=f"{fs.COUNTRY_CODE}/{parts.region}/{parts.district}",
        )
        unit.name_defects = name_defects(raw_name)
        unit.aliases.append(
            (FSCODE_SOURCE_SYSTEM, parts.district, raw_name, "source_code_derivation")
        )
        unit.aliases.append((NAME_SOURCE_SYSTEM, normalise_name(raw_name), raw_name, "exact_name"))
        pending[key] = unit

    def _ensure_county(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        parts: fs.FsCodeParts,
        props: dict[str, Any],
        result: ImportResult,
    ) -> None:
        key = (GeographyLevel.COUNTY, parts.county)
        raw_name = str(props.get("County", "")).strip()

        if key in pending:
            return

        if not raw_name:
            result.add_issue(
                "county_name_missing",
                f"County code {parts.county} has no name",
                blocking=True,
                context={"code": parts.county},
            )
            return

        unit = _PendingUnit(
            level=GeographyLevel.COUNTY,
            preferred_code=parts.county,
            raw_name=raw_name,
            normalised_name=normalise_name(raw_name),
            parent_code=parts.district,
            unit_kind=infer_unit_kind(raw_name),
            depth=3,
            path=f"{fs.COUNTRY_CODE}/{parts.region}/{parts.district}/{parts.county}",
        )
        unit.name_defects = name_defects(raw_name)
        unit.aliases.append(
            (FSCODE_SOURCE_SYSTEM, parts.county, raw_name, "source_code_derivation")
        )
        pending[key] = unit

    def _add_subcounty(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        parts: fs.FsCodeParts,
        props: dict[str, Any],
        feature: Any,
        result: ImportResult,
        options: ImportOptions,
    ) -> None:
        raw_name = str(props.get("Sub_County", "")).strip()
        if not raw_name:
            result.add_issue(
                "subcounty_name_missing",
                f"Subcounty {parts.subcounty} has no name",
                blocking=True,
                context={"code": parts.subcounty},
            )
            return

        unit = _PendingUnit(
            level=GeographyLevel.SUBCOUNTY,
            preferred_code=parts.subcounty,
            raw_name=raw_name,
            normalised_name=normalise_name(raw_name),
            parent_code=parts.county,
            unit_kind=infer_unit_kind(raw_name),
            depth=4,
            path=(
                f"{fs.COUNTRY_CODE}/{parts.region}/{parts.district}"
                f"/{parts.county}/{parts.subcounty}"
            ),
            source_record_id=str(props.get("FID")) if props.get("FID") is not None else None,
        )
        unit.name_defects = name_defects(raw_name)
        unit.aliases.append(
            (FSCODE_SOURCE_SYSTEM, parts.subcounty, raw_name, "source_code_derivation")
        )

        # A parenthetical alternative name is a second way the same place is
        # written. Recorded so a residence field naming the alternative can
        # still resolve, rather than being lost to normalisation.
        for alternative in extract_alias_names(raw_name):
            unit.aliases.append((NAME_SOURCE_SYSTEM, alternative, raw_name, "parenthetical_alias"))

        if options.load_geometry:
            assessment = geom.assess(
                feature.geometry, label=f"subcounty {parts.subcounty} ({raw_name})"
            )
            unit.geometry = assessment.prepared
            unit.assessment = assessment

        pending[(GeographyLevel.SUBCOUNTY, parts.subcounty)] = unit

    def _attach_district_geometry(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        result: ImportResult,
    ) -> None:
        """Join district geometry onto the districts derived from the spine.

        Joined on normalised name. That is safe here and only here: both sets
        are exactly 146 and, per the audit, match one to one. Any name that does
        not match is a blocking issue rather than a silent omission.
        """
        by_name = {
            unit.normalised_name: unit
            for unit in pending.values()
            if unit.level is GeographyLevel.DISTRICT
        }

        reader = BoundarySourceReader(
            SourceFile(
                path=self._sources[SourceRole.DISTRICT_GEOMETRY],
                role=SourceRole.DISTRICT_GEOMETRY,
            )
        )

        matched = 0
        for feature in reader.read():
            raw_name = str(feature.properties.get("District", "")).strip()
            normalised = normalise_name(raw_name) if raw_name else ""
            unit = by_name.get(normalised)

            if unit is None:
                result.add_issue(
                    "district_geometry_unmatched",
                    f"District geometry for {raw_name!r} matches no district "
                    "derived from the hierarchy source",
                    blocking=True,
                    context={"name": raw_name},
                )
                continue

            assessment = geom.assess(feature.geometry, label=f"district {raw_name}")
            unit.geometry = assessment.prepared
            unit.assessment = assessment
            matched += 1

        missing = len(by_name) - matched
        if missing > 0:
            result.add_issue(
                "district_geometry_missing",
                f"{missing} district(s) derived from the hierarchy source have no geometry",
                blocking=True,
                context={"missing": missing},
            )

    # -- Validation ---------------------------------------------------------
    def _validate_hierarchy(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        result: ImportResult,
    ) -> None:
        """Check the assembled hierarchy before anything is written."""
        roots = [unit for unit in pending.values() if unit.level is GeographyLevel.COUNTRY]
        if len(roots) != 1:
            result.add_issue(
                "root_count_invalid",
                f"expected exactly one country root, found {len(roots)}",
                blocking=True,
            )

        by_code: dict[tuple[GeographyLevel, str], _PendingUnit] = pending
        names_under_parent: dict[tuple[str | None, GeographyLevel, str], int] = defaultdict(int)

        for unit in pending.values():
            # Parent must exist at the level above.
            if unit.parent_code is not None:
                parent_level = unit.level.parent_level
                if parent_level is None or (parent_level, unit.parent_code) not in by_code:
                    result.add_issue(
                        "parent_missing",
                        f"{unit.level.value} {unit.preferred_code} references "
                        f"parent {unit.parent_code} which does not exist",
                        blocking=True,
                        context={"code": unit.preferred_code},
                    )

            if unit.depth != unit.level.depth:
                result.add_issue(
                    "depth_mismatch",
                    f"{unit.level.value} {unit.preferred_code} has depth "
                    f"{unit.depth}, expected {unit.level.depth}",
                    blocking=True,
                    context={"code": unit.preferred_code},
                )

            expected_suffix = f"/{unit.preferred_code}" if unit.parent_code else unit.preferred_code
            if not unit.path.endswith(expected_suffix):
                result.add_issue(
                    "path_inconsistent",
                    f"{unit.level.value} {unit.preferred_code} has path {unit.path}",
                    blocking=True,
                    context={"code": unit.preferred_code},
                )

            names_under_parent[(unit.parent_code, unit.level, unit.normalised_name)] += 1

            if unit.name_defects:
                result.add_issue(
                    "source_name_defect",
                    f"{unit.level.value} {unit.preferred_code} name {unit.raw_name!r}: "
                    f"{', '.join(unit.name_defects)}",
                    blocking=False,
                    context={"code": unit.preferred_code, "defects": unit.name_defects},
                )

        for (parent, level, name), count in names_under_parent.items():
            if count > 1:
                result.add_issue(
                    "duplicate_name_under_parent",
                    f"{count} {level.value} units named {name!r} share parent {parent}",
                    blocking=True,
                    context={"parent": parent, "name": name},
                )

        for level in GeographyLevel:
            units = [unit for unit in pending.values() if unit.level is level]
            if not units:
                continue
            counts = result.level(level.value)
            for unit in units:
                if unit.assessment is not None:
                    if unit.assessment.is_usable:
                        counts.with_geometry += 1
                        if unit.assessment.validity_state is GeometryValidityState.INVALID_REPAIRED:
                            counts.repaired_geometry += 1
                    else:
                        counts.quarantined_geometry += 1
                        result.add_issue(
                            "geometry_quarantined",
                            f"{level.value} {unit.preferred_code} geometry could not be "
                            f"prepared: {', '.join(unit.assessment.issue_codes())}",
                            blocking=False,
                            context={"code": unit.preferred_code},
                        )

    def _validate_control_totals(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        result: ImportResult,
    ) -> None:
        """Check the audited area identities still hold.

        The audit established that district areas sum to the country area and
        subcounty areas sum to their district, exactly. If that stops being
        true, the sources are no longer the coherent set ADR 0004 describes and
        publishing them would silently corrupt every later spatial figure.
        """
        totals: dict[str, float] = {}
        for level in (GeographyLevel.COUNTRY, GeographyLevel.DISTRICT, GeographyLevel.SUBCOUNTY):
            totals[level.value] = sum(
                unit.assessment.planar_area_deg2
                for unit in pending.values()
                if unit.level is level and unit.assessment is not None
            )

        country = totals.get(GeographyLevel.COUNTRY.value, 0.0)
        result.control_totals = {
            "country_area_deg2": round(country, 6),
            "district_sum_deg2": round(totals.get(GeographyLevel.DISTRICT.value, 0.0), 6),
            "subcounty_sum_deg2": round(totals.get(GeographyLevel.SUBCOUNTY.value, 0.0), 6),
            "tolerance": AREA_TOLERANCE,
            "note": (
                "Planar degrees, used only as an import control total. Real areas "
                "are measured server-side on the geography type. District polygons "
                "include open water, so area is never a population proxy."
            ),
        }

        if country <= 0:
            result.add_issue(
                "control_total_unavailable",
                "the country area could not be computed; area checks were skipped",
                blocking=False,
            )
            return

        for level in (GeographyLevel.DISTRICT, GeographyLevel.SUBCOUNTY):
            total = totals.get(level.value, 0.0)
            if total <= 0:
                continue
            ratio = total / country
            result.control_totals[f"{level.value}_sum_over_country"] = round(ratio, 8)
            if abs(ratio - 1.0) > AREA_TOLERANCE:
                result.add_issue(
                    "control_total_mismatch",
                    f"{level.value} areas sum to {ratio:.8f} of the country area; "
                    f"the audited sources agree to within {AREA_TOLERANCE}",
                    blocking=True,
                    context={"level": level.value, "ratio": ratio},
                )

    # -- Persistence --------------------------------------------------------
    def _unique_version_code(self, preferred: str) -> str:
        """Return a boundary version code that is not already taken.

        The code is a timestamp plus a checksum prefix, which reads well and is
        unique in every ordinary case. A forced re-import of identical bytes
        within the same second produces the same value, though, so a numeric
        suffix disambiguates rather than letting the insert fail.
        """
        existing = set(
            self._session.execute(
                select(BoundaryVersion.code).where(BoundaryVersion.code.like(f"{preferred}%"))
            ).scalars()
        )
        if preferred not in existing:
            return preferred
        for attempt in range(2, 1000):
            candidate = f"{preferred}-{attempt}"
            if candidate not in existing:
                return candidate
        raise RuntimeError(f"could not find a free boundary version code for {preferred}")

    def _create_version(
        self,
        combined: str,
        profiles: dict[SourceRole, SourceProfile],
        options: ImportOptions,
    ) -> BoundaryVersion:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        code = self._unique_version_code(f"UG-ADMIN-{stamp}-{combined[:8]}")
        version = BoundaryVersion(
            code=code,
            label=f"Uganda administrative boundaries imported {stamp}Z",
            source_name="Supplied Uganda boundary set",
            source_file_name=", ".join(sorted(profile.filename for profile in profiles.values())),
            source_checksum=combined,
            source_format="geojson+esri",
            source_crs=next(
                (p.declared_crs for p in profiles.values() if p.declared_crs),
                None,
            ),
            storage_crs="EPSG:4326",
            import_status=BoundaryImportStatus.VALIDATING,
            imported_at=utc_now(),
            imported_by=options.imported_by,
            lineage={
                "importer_version": IMPORTER_VERSION,
                "options": {
                    "load_geometry": options.load_geometry,
                    "derive_geometry": options.derive_geometry,
                    "simplify_geometry": options.simplify_geometry,
                    "force": options.force,
                },
                "sources": [profile.as_dict() for profile in profiles.values()],
            },
            notes=options.note,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def _record_failed_attempt(
        self,
        combined: str,
        profiles: dict[SourceRole, SourceProfile],
        result: ImportResult,
        options: ImportOptions,
    ) -> None:
        """Keep the failed attempt, without disturbing what is published.

        Blueprint section 083: a failed refresh must not replace good data. The
        attempt is retained as a non-published version carrying its report, so
        the failure is diagnosable later.
        """
        version = self._create_version(combined, profiles, options)
        version.import_status = BoundaryImportStatus.VALIDATION_FAILED
        result.boundary_version_id = version.id
        result.boundary_version_code = version.code
        # Set before serialising, so the stored document reports the outcome it
        # actually had rather than the value the result was constructed with.
        result.outcome = ImportOutcome.VALIDATION_FAILED
        version.validation_summary = result.as_dict()
        self._session.flush()

    def _persist(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        version: BoundaryVersion,
        result: ImportResult,
        options: ImportOptions,
    ) -> None:
        """Write units, revisions, aliases and geometry.

        Units are matched on ``(level, preferred_code)`` so a re-import keeps
        the existing UUID. Facilities, user geography scopes and encounters
        reference that UUID, so replacing rather than reusing it would break
        every one of them.

        Two things are written for each unit, and the distinction is the point:

        * a **revision** describing what *this* boundary version says - name,
          kind, parent, depth, path, presence. Immutable once the version is
          published, so a later recut adds history rather than erasing it.
        * the **cached columns** on the stable row, updated to match. Fast to
          read, and explicitly not a historical answer.

        Before revisions existed, only the second happened, and a second import
        left the earlier ``BoundaryVersion`` describing boundaries that no
        longer existed anywhere.
        """
        existing = {
            (unit.level, unit.preferred_code): unit
            for unit in self._session.execute(select(GeographyUnit)).scalars()
        }

        # Parents before children, so parent_id is always available.
        ordered = sorted(pending.values(), key=lambda unit: (unit.depth, unit.preferred_code))
        code_to_id: dict[tuple[GeographyLevel, str], uuid.UUID] = {}
        code_to_revision: dict[tuple[GeographyLevel, str], uuid.UUID] = {}

        for item in ordered:
            counts = result.level(item.level.value)
            parent_id: uuid.UUID | None = None
            if item.parent_code is not None:
                parent_level = item.level.parent_level
                assert parent_level is not None
                parent_id = code_to_id.get((parent_level, item.parent_code))

            key = (item.level, item.preferred_code)
            unit = existing.get(key)

            if unit is None:
                unit = GeographyUnit(
                    level=item.level,
                    preferred_code=item.preferred_code,
                    raw_name=item.raw_name,
                    normalised_name=item.normalised_name,
                    unit_kind=item.unit_kind,
                    parent_id=parent_id,
                    depth=item.depth,
                    path=item.path,
                    boundary_version_id=version.id,
                    is_active=True,
                    source_system=FSCODE_SOURCE_SYSTEM,
                    source_record_id=item.source_record_id,
                    source_version=version.code,
                )
                self._session.add(unit)
                counts.created += 1
            else:
                unit.raw_name = item.raw_name
                unit.normalised_name = item.normalised_name
                unit.unit_kind = item.unit_kind
                unit.parent_id = parent_id
                unit.depth = item.depth
                unit.path = item.path
                unit.boundary_version_id = version.id
                unit.is_active = True
                unit.source_version = version.code
                counts.updated += 1

            self._session.flush()
            code_to_id[key] = unit.id

            revision = self._write_revision(unit, item, version, parent_id, code_to_revision)
            code_to_revision[key] = revision.id

            self._write_aliases(unit, item, result)

            if options.load_geometry and item.assessment is not None:
                self._write_geometry(unit, item, version, result, options)

        self._deactivate_absent(pending, existing, version, result)

    def _write_revision(
        self,
        unit: GeographyUnit,
        item: _PendingUnit,
        version: BoundaryVersion,
        parent_id: uuid.UUID | None,
        code_to_revision: dict[tuple[GeographyLevel, str], uuid.UUID],
    ) -> GeographyUnitRevision:
        """Record what this boundary version says about this unit.

        The parent link points at the *parent's revision under the same
        version*, not at the stable parent unit: a recut may re-parent a
        subcounty, and a link to the stable unit would lose which parent it had
        at the time.

        Re-running the same import updates the revision in place, which the
        database permits only while the version is unpublished - publication is
        what makes it immutable.
        """
        parent_revision_id: uuid.UUID | None = None
        if item.parent_code is not None:
            parent_level = item.level.parent_level
            assert parent_level is not None
            parent_revision_id = code_to_revision.get((parent_level, item.parent_code))

        revision = self._session.execute(
            select(GeographyUnitRevision).where(
                GeographyUnitRevision.geography_unit_id == unit.id,
                GeographyUnitRevision.boundary_version_id == version.id,
            )
        ).scalar_one_or_none()

        if revision is None:
            revision = GeographyUnitRevision(
                geography_unit_id=unit.id, boundary_version_id=version.id
            )
            self._session.add(revision)

        revision.level = item.level
        revision.unit_kind = item.unit_kind
        revision.preferred_code = item.preferred_code
        revision.raw_name = item.raw_name
        revision.normalised_name = item.normalised_name
        revision.parent_revision_id = parent_revision_id
        revision.depth = item.depth
        revision.path = item.path
        revision.is_present = True
        self._session.flush()
        return revision

    def _write_aliases(self, unit: GeographyUnit, item: _PendingUnit, result: ImportResult) -> None:
        """Record every source code that identifies this unit.

        A mapping derived from the structured source code is ``confirmed``: it
        is arithmetic on a value the source supplies, not a guess. A mapping
        derived from a name stays ``proposed`` until reviewed, because names
        repeat across the supplied data.
        """
        existing = {
            (alias.source_system, alias.source_code)
            for alias in self._session.execute(
                select(GeographyUnitAlias).where(GeographyUnitAlias.geography_unit_id == unit.id)
            ).scalars()
        }

        for source_system, source_code, source_name, method in item.aliases:
            if (source_system, source_code) in existing:
                continue
            status = (
                AliasMatchStatus.CONFIRMED
                if method == "source_code_derivation"
                else AliasMatchStatus.PROPOSED
            )
            self._session.add(
                GeographyUnitAlias(
                    geography_unit_id=unit.id,
                    source_system=source_system,
                    source_code=source_code,
                    source_name=source_name,
                    source_level=item.level.value,
                    match_status=status,
                    match_method=method,
                )
            )
            result.aliases_written += 1

    def _write_geometry(
        self,
        unit: GeographyUnit,
        item: _PendingUnit,
        version: BoundaryVersion,
        result: ImportResult,
        options: ImportOptions,
    ) -> None:
        assessment = item.assessment
        assert assessment is not None

        record = self._session.execute(
            select(GeographyUnitGeometry).where(
                GeographyUnitGeometry.geography_unit_id == unit.id,
                GeographyUnitGeometry.boundary_version_id == version.id,
            )
        ).scalar_one_or_none()

        if record is None:
            record = GeographyUnitGeometry(
                geography_unit_id=unit.id, boundary_version_id=version.id
            )
            self._session.add(record)

        record.validity_state = assessment.validity_state
        record.validity_issues = {"issues": assessment.issues} if assessment.issues else None
        record.repair_method = assessment.repair_method
        record.ring_count = assessment.ring_count
        record.vertex_count = assessment.vertex_count
        record.part_count = assessment.part_count
        if assessment.bbox:
            record.bbox_min_lon, record.bbox_min_lat, record.bbox_max_lon, record.bbox_max_lat = (
                assessment.bbox
            )

        if not assessment.is_usable:
            result.geometry_quarantined += 1
            self._session.flush()
            return

        self._session.flush()
        self._load_geometry_sql(record.id, assessment.prepared, unit.level, options)
        result.geometry_written += 1

    def _load_geometry_sql(
        self,
        geometry_id: uuid.UUID,
        prepared: dict[str, Any] | None,
        level: GeographyLevel,
        options: ImportOptions,
    ) -> None:
        """Write geometry through PostGIS.

        ``ST_Multi`` guarantees the column's MultiPolygon type even after a
        repair reduced the feature to a single part. ``ST_MakeValid`` fixes
        self-intersections that survive ring-level repair; the pre-repair state
        is already recorded on the row.

        Area is measured on the geography type, which gives square metres on the
        spheroid rather than meaningless square degrees.
        """
        import json as _json

        payload = _json.dumps(prepared)
        tolerance = geom.tolerance_for(level)

        statement = text(
            """
            UPDATE mars_core.geography_unit_geometry
               SET geom = ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:payload), 4326))),
                   geom_web = CASE WHEN :simplify THEN
                       ST_Multi(ST_MakeValid(ST_SimplifyPreserveTopology(
                           ST_SetSRID(ST_GeomFromGeoJSON(:payload), 4326), :tolerance)))
                   ELSE NULL END,
                   simplification_tolerance_deg = CASE WHEN :simplify THEN :tolerance ELSE NULL END,
                   area_sq_km = ST_Area(
                       ST_SetSRID(ST_GeomFromGeoJSON(:payload), 4326)::geography
                   ) / 1000000.0,
                   perimeter_km = ST_Perimeter(
                       ST_SetSRID(ST_GeomFromGeoJSON(:payload), 4326)::geography
                   ) / 1000.0
             WHERE id = :geometry_id
            """
        )
        self._session.execute(
            statement,
            {
                "payload": payload,
                "tolerance": tolerance,
                "simplify": options.simplify_geometry,
                "geometry_id": geometry_id,
            },
        )

    def _deactivate_absent(
        self,
        pending: dict[tuple[GeographyLevel, str], _PendingUnit],
        existing: dict[tuple[GeographyLevel, str], GeographyUnit],
        version: BoundaryVersion,
        result: ImportResult,
    ) -> None:
        """Mark units the new source no longer contains as inactive.

        Never deleted. A district that is split or renamed still has historical
        encounters and signals attached to it, and blueprint appendix 139
        requires historical analytics to keep resolving.
        """
        absent = set(existing) - set(pending)
        for key in absent:
            unit = existing[key]
            if not unit.is_active:
                continue
            unit.is_active = False
            unit.effective_to = version.effective_from or utc_now().date()
            result.level(unit.level.value).deactivated += 1
            result.add_issue(
                "unit_deactivated",
                f"{unit.level.value} {unit.preferred_code} ({unit.raw_name}) is absent "
                "from the new source and was deactivated, not deleted",
                blocking=False,
                context={"code": unit.preferred_code},
            )

    def _derive_parent_geometry(
        self,
        version: BoundaryVersion,
        result: ImportResult,
        options: ImportOptions,
    ) -> None:
        """Build region and county geometry by dissolving their children.

        ADR 0004: no separate region or county boundary file is needed. Counties
        dissolve from their subcounties and regions from their districts, which
        is far cheaper than dissolving 2,190 subcounties into four regions.
        """
        for level, child_level in (
            (GeographyLevel.COUNTY, GeographyLevel.SUBCOUNTY),
            (GeographyLevel.REGION, GeographyLevel.DISTRICT),
        ):
            tolerance = geom.tolerance_for(level)
            statement = text(
                """
                INSERT INTO mars_core.geography_unit_geometry
                    (id, geography_unit_id, boundary_version_id,
                     validity_state, repair_method,
                     geom, geom_web, simplification_tolerance_deg,
                     area_sq_km, perimeter_km,
                     part_count, ring_count, vertex_count,
                     bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat,
                     created_at, updated_at)
                SELECT gen_random_uuid(), parent.id, :version_id,
                       'valid', 'dissolved_from_children',
                       dissolved.geom,
                       CASE WHEN :simplify THEN
                           ST_Multi(ST_MakeValid(ST_SimplifyPreserveTopology(
                               dissolved.geom, :tolerance)))
                       ELSE NULL END,
                       CASE WHEN :simplify THEN :tolerance ELSE NULL END,
                       ST_Area(dissolved.geom::geography) / 1000000.0,
                       ST_Perimeter(dissolved.geom::geography) / 1000.0,
                       ST_NumGeometries(dissolved.geom),
                       ST_NRings(dissolved.geom),
                       ST_NPoints(dissolved.geom),
                       -- Measured here rather than left to the reader. A map
                       -- fits its viewport from these four numbers, and a
                       -- dissolved region with no bounding box would be the one
                       -- level that could not be zoomed to.
                       ST_XMin(dissolved.geom), ST_YMin(dissolved.geom),
                       ST_XMax(dissolved.geom), ST_YMax(dissolved.geom),
                       now(), now()
                  FROM mars_core.geography_unit AS parent
                  JOIN LATERAL (
                       SELECT ST_Multi(ST_MakeValid(ST_Union(child_geom.geom))) AS geom
                         FROM mars_core.geography_unit AS child
                         JOIN mars_core.geography_unit_geometry AS child_geom
                           ON child_geom.geography_unit_id = child.id
                        WHERE child.parent_id = parent.id
                          AND child.level = :child_level
                          AND child_geom.geom IS NOT NULL
                  ) AS dissolved ON dissolved.geom IS NOT NULL
                 WHERE parent.level = :level
                   AND parent.boundary_version_id = :version_id
                ON CONFLICT (geography_unit_id, boundary_version_id) DO UPDATE
                    SET geom = EXCLUDED.geom,
                        geom_web = EXCLUDED.geom_web,
                        simplification_tolerance_deg = EXCLUDED.simplification_tolerance_deg,
                        area_sq_km = EXCLUDED.area_sq_km,
                        perimeter_km = EXCLUDED.perimeter_km,
                        part_count = EXCLUDED.part_count,
                        ring_count = EXCLUDED.ring_count,
                        vertex_count = EXCLUDED.vertex_count,
                        bbox_min_lon = EXCLUDED.bbox_min_lon,
                        bbox_min_lat = EXCLUDED.bbox_min_lat,
                        bbox_max_lon = EXCLUDED.bbox_max_lon,
                        bbox_max_lat = EXCLUDED.bbox_max_lat,
                        validity_state = EXCLUDED.validity_state,
                        repair_method = EXCLUDED.repair_method,
                        updated_at = now()
                """
            )
            cursor = self._session.execute(
                statement,
                {
                    "level": level.value,
                    "child_level": child_level.value,
                    "version_id": version.id,
                    "tolerance": tolerance,
                    "simplify": options.simplify_geometry,
                },
            )
            written = cursor.rowcount if hasattr(cursor, "rowcount") else 0
            counts = result.level(level.value)
            counts.with_geometry = written or 0
            result.geometry_written += written or 0
            logger.info(
                "geography_dissolve_complete",
                level=level.value,
                child_level=child_level.value,
                units=written,
            )

    def _publish(self, version: BoundaryVersion, result: ImportResult) -> None:
        """Make this version the published one.

        Exactly one boundary version is published at a time, so any previously
        published version is superseded in the same transaction. Publication is
        the last step: nothing is visible as authoritative until validation and
        loading have both succeeded.
        """
        self._session.execute(
            update(BoundaryVersion)
            .where(
                BoundaryVersion.import_status == BoundaryImportStatus.PUBLISHED,
                BoundaryVersion.id != version.id,
            )
            .values(import_status=BoundaryImportStatus.SUPERSEDED, effective_to=utc_now().date())
        )
        version.import_status = BoundaryImportStatus.PUBLISHED
        version.effective_from = utc_now().date()
        version.validation_summary = result.as_dict()
        self._session.flush()

    @staticmethod
    def _finish(result: ImportResult, started: float, outcome: ImportOutcome) -> ImportResult:
        result.outcome = outcome
        result.duration_seconds = time.perf_counter() - started
        result.finished_at = utc_now().isoformat().replace("+00:00", "Z")
        return result
