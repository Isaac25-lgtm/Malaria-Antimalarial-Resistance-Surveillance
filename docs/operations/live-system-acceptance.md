# Pader live pilot acceptance map

This document distinguishes code readiness from a successful Ministry retrieval.
The live application never substitutes demonstration records. A page may show a
saved real snapshot, a partial-coverage warning, a configuration prerequisite, or
an unavailable state; it must not fill a gap with synthetic data.

## Implemented live path

| Area | Live source and behavior |
| --- | --- |
| Authentication | One eRegisters sign-in creates an opaque MARS cookie. The password remains in process memory and is never returned to the browser after login. |
| Scope | Dashboard scope comes from DHIS2 data-view organisation units. Patient-evidence permission comes separately from Tracker-search scope. |
| Metadata | Discovery retrieves metadata and authorised facilities only. It does not retrieve tracked entities, enrollments or events. |
| Synchronization | A bounded background job reads only the discovered facility scope. The browser can navigate away while it runs and polls durable progress. |
| Recovery | Encrypted checkpoints resume completed aggregate, historical and facility reads. A failed attempt cannot delete the last complete snapshot. |
| Overview | KPI cards, facility table, source issues, commodity observations, trends and patient panels read one selected-period live snapshot. Legacy analytical endpoints are disabled in live mode. |
| Map | District and subcounty polygons come from the imported GeoJSON boundary version. Facility values join to a subcounty only through exact, normalized DHIS2 ancestor metadata. No coordinate or location is invented. |
| Map Explorer | Uses the same selected reporting period and live snapshot. A polygon with a verified reported total is visually distinct from a polygon with no linked total. |
| Facilities | The list and facility drill-down read the live snapshot by DHIS2 facility UID. Live mode does not query legacy local facility summaries. |
| Patient surveillance | Displays only stable MARS aliases and mapped malaria evidence from the live snapshot. Direct identifiers are excluded. |
| Commodities | Shows directly reported stock conditions separately from epidemiological signals. |
| Data quality | Shows retrieved/requested coverage, source row counts, mapped lab events and invalid source values. Coverage is not mislabeled as official reporting completeness. |
| Signals | Does not relabel commodity or data-quality observations as malaria signals. It remains explicitly unconfigured until an approved method version exists. |
| Investigations | Uses the authenticated MARS session cookie and real MARS workflow tables. Source observations awaiting triage are shown separately from opened investigation records. |
| Reports | Exports the selected saved live snapshot through an authorised, audited server endpoint. No browser-built or synthetic report is offered. |

## Runtime acceptance

Run the launcher as the Windows user that owns the DPAPI-protected keys:

```powershell
Set-Location -LiteralPath "F:\MY FILES\DATA SCIENCE\MARS PROJECT\MARS IMPLEMENTATION"
& ".\scripts\start-mars-live.ps1" -Restart
```

Use `-InitializeNewLocalKeys` only for the explicit one-time DPAPI recovery
documented in `live-synchronization.md`. Never use it as part of routine startup.

Do not use `-InitializeNewLocalKeys` again unless deliberately rotating keys and
accepting that old patient aliases and encrypted snapshots will no longer be
reproducible. The launcher must report both restricted database logins and both
privilege-boundary checks as `OK`, apply migration `0027_live_sync`, and then
publish the UI and API listener PIDs.

After login, acceptance requires all of the following for one selected period:

1. Metadata discovery reports the expected DHIS2 version and authorised facility count.
2. The job reaches `completed` or an honestly described `partial` state.
3. The snapshot says `synthetic_data_used: false`.
4. Facility and retrieval coverage match the sanitized job evidence.
5. Refreshing the browser preserves the published snapshot.
6. Overview, Map Explorer, Facilities, Patient Surveillance, Analytics,
   Commodities, Data Quality and Reports show the same period.
7. Signing out removes access to the session-scoped snapshot and patient evidence.

## Deliberate governance boundary

Live retrieval does not itself approve a clinical signal threshold, seasonal
baseline, recurrence interpretation, or investigation SLA. Those panels must
remain unconfigured until the Ministry/programme supplies and approves method
versions. This is a governance input, not missing transport code, and MARS must
not invent it merely to make a dashboard look populated.

## Automated evidence

The non-integration backend suite covers authentication, geography and
sensitivity isolation, durable job fencing, encrypted persistence, resumption,
logout interruption, mapping validation and snapshot preservation. The frontend
suite covers live polling, cache isolation, period selection, GeoJSON joins,
facility drill-down, accessibility and production compilation. PostgreSQL-specific
tests additionally cover migration grants, encrypted persistence, advisory-lock
deduplication, rollback visibility and stale-worker fencing when
`MARS_TEST_DATABASE_URL` is supplied.
