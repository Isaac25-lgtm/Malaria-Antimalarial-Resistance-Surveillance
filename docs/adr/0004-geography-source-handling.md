# ADR 0004: Geography source handling

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 1, informing 4-5

## Context

Four boundary files were supplied, totalling 226 MB. They were audited in full
before any code was written. `scripts/geography_audit.py` regenerates every
figure below from the files themselves; none of them is a hard-coded constant.

| File | Format | Features | Vertices |
| --- | --- | ---: | ---: |
| `COUNTRY_BOUNDARY.json` | GeoJSON | 1 | 28,934 |
| `UGANDA_DISTRICT.json` | GeoJSON | 146 | 489,872 |
| `UGANDA_DISTRICTS.json` | Esri FeatureSet | 146 | 489,872 |
| `UGANDA_SUBCOUNTIES.json` | GeoJSON | 2,190 | 1,671,234 |

Findings that drive this decision:

- The two district files are **the same dataset in two serialisation formats** -
  not duplicates, and not alternatives to choose between. Identical feature
  order, identical attributes, identical per-feature vertex counts. Ring winding
  is reversed (RFC 7946 counter-clockwise against the Esri clockwise
  convention). Only the Esri file declares a CRS.
- The subcounty file carries `FScode`, a six-digit hierarchical code: digit one
  is the region and agrees with `RCode` on all 2,190 rows; the first three
  digits map one-to-one to the 146 districts; the first four give 312
  county-level units; all six are unique.
- Topology is consistent. District areas sum to the country boundary area
  exactly, and subcounty areas sum to their district for all 146 districts.
- Defects exist and must not be edited away: 22 degenerate rings, a duplicated
  `OBJECTID`, four subcounties whose `Shape_Area` attribute is wrong, names with
  repeated whitespace and parenthetical aliases, and 44 subcounty names reused
  across districts (`CENTRAL DIVISION` appears twelve times).

## Decision

**Roles.** `UGANDA_SUBCOUNTIES.json` is the hierarchy spine, being the only file
carrying `FScode`, `County`, `District` and `RCode` together.
`UGANDA_DISTRICT.json` supplies district geometry, its winding already correct
for PostGIS. `COUNTRY_BOUNDARY.json` is the national outline and the import
control total. `UGANDA_DISTRICTS.json` is retained unmodified as the CRS witness
and format record, and is **not** imported.

**Region and county** are derived by dissolving on the code prefix. No further
boundary file is needed for them.

**Parish and village** exist in the schema and stay empty. No such data has been
supplied, and MARS does not fabricate geography.

**`FScode` is an alias, not a key.** It is recorded in `geography_unit_alias`
under source system `ubos_fscode`. `preferred_code` is derived from it for now
and can be replaced by an authoritative code later without touching a single
internal identifier.

**Sources are never modified.** The files are excluded from Git by size, with
their SHA-256 checksums tracked in `data/manifests/geography.sha256.json`.
Defects are recorded against each unit and repaired only in derived geometry.

**Uniqueness follows the data.** `(level, preferred_code)` is unique, and so is
`(parent, level, normalised_name)` - but never name alone, because the supplied
data proves names repeat.

**Vintage is versioned.** The layer is the 146-unit configuration including
Kalaki (2021) and all ten cities. `boundary_version` carries effective dates, so
a future re-cut cannot silently rewrite historical analysis.

## How the Prompt 5 importer will use this

For each of the 2,190 subcounty features, `FScode` yields four codes:

| Slice | Level | Example | Parent code |
| --- | --- | --- | --- |
| `[0:1]` | Region | `3` | `UG` |
| `[0:3]` | District | `314` | `3` |
| `[0:4]` | County | `3141` | `314` |
| `[0:6]` | Subcounty | `314101` | `3141` |

The importer creates the country unit with `preferred_code` `UG`, then the
distinct region, district, county and subcounty units in that order, setting
`parent_id`, `depth` and the materialised `path`. It writes a
`geography_unit_alias` row per unit with source system `ubos_fscode` and match
method `source_code_derivation`, records the district-name join against
`UGANDA_DISTRICT.json` as its own alias, and validates the area control total
before publishing the boundary version. `mars.geo.fscode` already implements the
parsing and `mars.geo.naming` the name normalisation, both unit-tested against
values taken from the supplied file.

## Consequences

- A five-level hierarchy with stable codes, from the supplied data, with no
  invented identifiers.
- The alias table must be populated at import.
- Raw geometry is never map-ready: simplification into `geom_web` is required
  before anything reaches a browser.

## Revisit when

An authoritative national coding scheme is supplied, or parish and village
boundaries become available.
