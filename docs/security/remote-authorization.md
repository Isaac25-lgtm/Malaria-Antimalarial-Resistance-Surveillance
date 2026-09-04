# Remote authorization versus local mapping

Live login produces five separate facts. They must not be collapsed into one
"scope" whose meaning changes when a `geography_unit_alias` row is missing.

1. **Authenticated source identity** — DHIS2 user id, username, display name.
2. **Remote authorization** — capture (`organisationUnits`), data-view
   (`dataViewOrganisationUnits`) and Tracker-search
   (`teiSearchOrganisationUnits`) kept distinct, plus the fail-closed fallback
   record.
3. **Effective remote workspace** — classified from data-view units as
   national, district, multi-district, facility, other, or unresolved.
4. **Local MARS mapping** — confirmed `geography_unit_alias` or facility
   identifier. Status is `resolved`, `pending`, `ambiguous` or `rejected`.
5. **Data readiness** — geography mapping, malaria metadata, aggregate sync
   and Tracker sync. Authentication is not synchronization. Tracker sync is
   `not_started` until a later approved collection.

## Dashboard authorization rule

Aggregate surveillance dashboards use **data-view** organisation units.

1. If `dataViewOrganisationUnits` is present and non-empty, use it.
2. If the field is present but empty, do **not** substitute capture or
   Tracker-search units.
3. If the field is absent from `/api/me`, a documented compatibility fallback
   may classify capture `organisationUnits`. The session records
   `fallback_used`, `fallback_source` and `fallback_reason`.
4. `teiSearchOrganisationUnits` is never a dashboard fallback.
5. Multiple districts are never national access.
6. National access requires an assigned data-view unit classified as
   country/national from DHIS2 level metadata.

`LoginSnapshot.all_assigned_units()` is a diagnostic helper only.

## Identifier namespaces

- Local MARS geography and facilities use UUIDs: `/district/{uuid}`,
  `/facility/{uuid}`.
- Unmapped but remotely authorized units use `/live/dhis2/district/{uid}` or
  `/live/dhis2/facility/{uid}` after DHIS2 UID syntax validation
  (`^[A-Za-z][A-Za-z0-9]{10}$`).
- A DHIS2 UID must never be passed to an API route that expects a MARS UUID.

## Workspace versus data

Remote authorization may grant the live workspace shell (profile, mapping
status, readiness). Local surveillance KPIs, signals, commodities, facility
analytics and geographic drilldowns require a confirmed local mapping. The
principal's `geography_scopes` stay empty until that mapping exists.

## Confirming a geography crosswalk

Automatic confirmation is permitted only for an already approved deterministic
lookup:

- exact remote UID on a confirmed `geography_unit_alias` (`source_system=dhis2`);
- or a confirmed source code at the classified administrative level;
- exactly one local candidate;
- no conflicting confirmed alias.

Name matching is never used. An unresolved UID becomes an
`integration_mapping_proposal` with remote evidence (UID, name, code, level,
path, parent) and any exact code-level local candidate left **pending** for
explicit approval.

Do not request tracked entities, enrollments, events, relationships or
patient records to classify geography.
