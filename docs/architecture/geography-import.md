# Geography import

How the supplied Uganda boundary sources become the canonical hierarchy, and
what the importer guarantees about them.

Read [ADR 0004](../adr/0004-geography-source-handling.md) first: it records what
the audit found in the sources and which file plays which role.

## What the importer builds

Five levels, all derived from the supplied data:

| Level | Count | Code | Name from | Geometry from |
| --- | ---: | --- | --- | --- |
| Country | 1 | `UG` | The importer | `COUNTRY_BOUNDARY.json` |
| Region | 4 | `FScode[0:1]` | **Unresolved** — see below | Dissolved from districts |
| District | 146 | `FScode[0:3]` | `District` attribute | `UGANDA_DISTRICT.json` |
| County | 312 | `FScode[0:4]` | `County` attribute | Dissolved from subcounties |
| Subcounty | 2,190 | `FScode[0:6]` | `Sub_County` attribute | `UGANDA_SUBCOUNTIES.json` |
| Parish | 0 | — | — | **No source supplied** |
| Village | 0 | — | — | **No source supplied** |

Parish and village exist in the schema and stay empty. MARS does not fabricate
geography.

### Region names are unresolved

The sources carry a region *code* (`RCode`, and the leading digit of `FScode`)
and no region *name*. Naming the four regions would require knowledge from
outside the supplied data, which ADR 0003 forbids, so:

- `preferred_code` is the digit, e.g. `3`
- `raw_name` is also the digit
- every import records a `region_name_unresolved` advisory issue per region

Supplying an authoritative region list is a one-line change to the names and
needs no schema change, because the codes are already stable.

## Running it

```bash
# Validate without writing anything
mars-import-geography --data-dir /path/to/sources --dry-run

# Import and publish
mars-import-geography --data-dir /path/to/sources --imported-by "ops:initial-load"

# Hierarchy only, no geometry - fast, for development
mars-import-geography --data-dir /path/to/sources --skip-geometry
```

The data directory is always supplied by the caller. No filesystem path is
hard-coded anywhere in the importer.

The same work runs as a worker job (`mars.workers.geography_import`), which is
where it belongs in a deployment: it reads 226 MB and dissolves geometry in
PostGIS, so it is minutes of work rather than a request.

## Guarantees

### Sources are never modified

Opened read-only. Every import records each source's SHA-256, size and
modification time on the boundary version. `scripts/geography_audit.py
--verify-only` confirms the bytes are unchanged at any time.

Geometry defects are repaired **only in the derived copy** MARS stores. The
original ring is described in `geography_unit_geometry.validity_issues`, and
`repair_method` names exactly what was done.

### Identity is stable across re-imports

Units are matched on `(level, preferred_code)`, so re-importing updates the
existing row and **keeps its UUID**. Facilities and user geography scopes
reference that UUID; replacing rather than updating would break every reference.

A unit absent from a newer source is **deactivated, never deleted** — historical
encounters and signals still resolve through it (blueprint appendix 139).

### Publication is all or nothing

Exactly one boundary version is published at a time, enforced by a partial
unique index rather than by the service that happens to be running.

On a blocking validation failure the previously published version is
**untouched**, and the failed attempt is retained as a
`validation_failed` version carrying its full report — so the failure is
diagnosable later rather than merely logged.

### Re-importing identical bytes is a no-op

The combined checksum of all three sources identifies the source set. An import
whose checksum is already published returns `already_imported` with the existing
version, and creates nothing. `--force` overrides this for a deliberate rebuild.

## Validation

Blocking — publication stops:

| Code | Meaning |
| --- | --- |
| `source_missing` | A required source file is absent |
| `source_code_missing` | A feature has no `FScode` |
| `unparsable_source_code` | `FScode` is not six digits |
| `duplicate_source_code` | The same `FScode` appears twice |
| `region_code_disagreement` | `FScode[0]` disagrees with `RCode` |
| `district_name_disagreement` | One district code carries two names |
| `district_geometry_unmatched` | District geometry matches no derived district |
| `district_geometry_missing` | A derived district has no geometry |
| `parent_missing` | A unit references a parent that does not exist |
| `depth_mismatch` / `path_inconsistent` | The materialised hierarchy disagrees with itself |
| `duplicate_name_under_parent` | Two units share a name under one parent |
| `control_total_mismatch` | Areas no longer sum as the audit established |

Advisory — recorded and carried forward:

| Code | Meaning |
| --- | --- |
| `region_name_unresolved` | No region name exists in the sources |
| `source_name_defect` | Repeated whitespace, parenthetical alias, mixed case |
| `geometry_quarantined` | A feature's geometry could not be prepared |
| `unit_deactivated` | A unit is absent from the new source |

### Control totals

The audit established that district areas sum to the country area exactly, and
subcounty areas sum to their district. The importer re-checks both to a relative
tolerance of `1e-6` and **refuses to publish** if either fails.

Areas here are planar degrees, used only as an import control. Real areas are
measured server-side on the PostGIS `geography` type, in square kilometres — the
supplied `Shape_Area` attribute is wrong on four subcounties and is never used.

## Geometry

Stored as `MultiPolygon` in EPSG:4326. Polygon inputs are promoted at read time,
so every level has one database type.

| Column | Purpose |
| --- | --- |
| `geom` | Validated full-resolution analytical geometry |
| `geom_web` | Topology-preserving simplification for the browser |
| `area_sq_km` | Measured on the geography type, not in degrees |

Both geometry columns carry a GIST index.

### Simplification

Raw subcounty geometry carries 1.67 million vertices and must never reach a
client. Tolerances are per level, finer where the level is viewed closer:

| Level | Tolerance (degrees) |
| --- | ---: |
| Country | 0.0050 |
| Region | 0.0040 |
| District | 0.0020 |
| County | 0.0015 |
| Subcounty | 0.0010 |

### Repair policy

A ring is a digitising artefact when it encloses less than `1e-7` square
degrees. That threshold is judged on **area alone**: the observed artefacts
enclose between `5.8e-13` and `6.3e-9`, while the smallest genuine subcounty
encloses `1.4e-4` — four orders of magnitude clear.

Vertex count is deliberately *not* a disqualifier. A legitimate rectangular
boundary has five points, so disqualifying on vertex count would discard real
geography.

| Repair | When |
| --- | --- |
| `none` | Valid MultiPolygon, nothing done |
| `promoted_to_multipolygon` | A valid Polygon, promoted for storage |
| `dropped_degenerate_rings` | One or more artefact rings removed |
| `dropped_degenerate_rings_and_promoted` | Both |
| `dissolved_from_children` | Region or county geometry, built by `ST_Union` |

A feature with no usable ring left is **quarantined**: the row exists with its
validity state and issues, and both geometry columns stay null. It is never
stored empty and never silently dropped.

## Where the data comes from

```
UGANDA_SUBCOUNTIES.json ──┬──> region   (FScode[0:1], name unresolved)
  the hierarchy spine     ├──> district (FScode[0:3], name from District)
  2,190 features          ├──> county   (FScode[0:4], name from County)
                          └──> subcounty + geometry

UGANDA_DISTRICT.json ─────────> district geometry, joined on normalised name
  146 features                  (safe only here: both sets are exactly 146
                                 and match one to one, per the audit)

COUNTRY_BOUNDARY.json ────────> country geometry + the area control total

UGANDA_DISTRICTS.json ────────> NOT IMPORTED
  the Esri twin                 Retained as the CRS and field-schema witness
```
