# MARS: repair the current system first, then implement configurable longitudinal surveillance

## Prompt for Claude

You are implementing changes in the existing MARS application. **First diagnose and repair the current system. Verify those repairs before adding the enhancements below.** Do not substitute a new application, a demonstration dashboard, or a cosmetic rewrite. Preserve the current professional appearance and real-data operation.

Repository/workspace:

`F:\MY FILES\DATA SCIENCE\MARS PROJECT\MARS IMPLEMENTATION`

This handoff was prepared from the local source on 11 September 2026. Its code findings are inspection findings, not a claim that the running application was tested today. Recheck the worktree before editing; another model or the owner may have made further changes.

The original enhancement brief is available locally at:

`C:\Users\USER\.codex\attachments\a9affd00-9767-4849-aaae-ad4f7d88671b\pasted-text.txt`

Read that brief completely. The implementation contract below translates its 30 requirements into an execution plan. Its illustrative patient counts, dates, drug examples and thresholds are not source data or clinically validated thresholds.

## 1. Mandatory first phase: repair, stabilize and establish evidence

Do this phase before feature development. Do not merely list defects and move on.

### 1.1 Preserve the working system

1. Read applicable `AGENTS.md` instructions. Inspect `git status`, current HEAD and actual diffs, including differences excluding line endings. Inventory existing uncommitted work; do not reset, overwrite or indiscriminately reformat it.
2. Record the processes bound to 5173, 8000 and 5460, their executable paths and command lines without revealing secrets. Check UI/API health, configured database identity and migration head using the existing supported diagnostics. Do not assume a listening port proves authentication or synchronization works.
3. Preserve `.local-secrets`, `.runtime`, existing PostgreSQL data, user credentials and stable encryption/linkage/display keys. Never initialize replacement keys to make a feature test pass. Do not bypass role boundaries by running the application as the database owner.
4. Keep feature development and automated tests away from `mars_live`. Use a separate test database and separate test service ports. A running Vite development server can hot-reload local file edits; therefore merely avoiding a restart does NOT guarantee the owner's browser remains unchanged. Use an isolated development copy/worktree containing the relevant local fixes, or explicitly arrange a controlled local cutover. Do not silently omit uncommitted fixes when making that copy.
5. No GitHub push, publishing of clinical data, or destructive cleanup is part of this task. Never commit credentials, keys, raw patient exports, database contents or patient screenshots.

Four files currently have substantive local differences after ignoring line endings. Preserve and understand them before touching related code:

- `backend/src/mars/services/live_sync_store.py`: checkpoint resumption boundaries and existing worker-lease fencing.
- `backend/tests/unit/test_durable_live_sync.py`: regression coverage for checkpoint behavior.
- `backend/tests/integration/test_identity_vault.py`: restricted-role connection/SQL fixes.
- `scripts/start-mars-live.ps1`: actionable startup diagnostics for the local Windows/PostgreSQL environment.

The important synchronization invariant is that a completed retrieval ends a resumable attempt sequence. An old failed attempt must not seed an unrelated fresh refresh. A stale worker must never overwrite a newer snapshot. Preserve these invariants through the feature work.

### 1.2 Investigate current failures; distinguish them from historical symptoms

The owner previously encountered PostgreSQL startup failures, DPAPI decryption failures, misleading login errors, failed refreshes, missing session cookies, empty patient/commodity/signal views and inconsistent dashboard status. These are historical observations, not proof that every one still exists.

Reproduce what currently fails, collect a sanitized root cause and fix it at its source:

| Area | Required diagnosis and repair verification |
| --- | --- |
| Startup | Inspect the supported launcher's exact failed stage and PostgreSQL logs. Check port ownership and actual server liveness before handling stale PID files. Never remove a PID file just because startup failed. |
| Keys and database roles | Validate required key availability/decryption before remote patient retrieval. Retain stable keys and restricted app/identity roles. Inspect the actual SQL error behind generic validation failures. Apply narrowly necessary grants through the supported migration/provisioning mechanism, not blanket privileges. |
| Authentication | Trace `/api/v1/auth/login` from client through provider/session/database. Invalid credentials, local configuration errors and upstream failures need distinct safe responses. A backend HTTP 500 must not be presented as proof of a wrong password. |
| Sessions | Verify cookie path, host, secure/same-site behavior, proxy and credentialed requests across overview, investigations, patients and refresh. Test logout and re-login. Do not switch casually between `localhost` and `127.0.0.1`; browser cookies are host-specific. |
| Synchronization | Trace one authorized job through submission, remote retrieval, checkpoints, publication and browser refresh. Verify last-good snapshot survives failure, stale workers are fenced, and a real fresh refresh fetches new data. |
| Status messaging | Separate connected/authenticated, retrieving, complete, partial, stale and failed. Report last successful dataset time separately from last attempt time. Do not display synchronized and failed as contradictory claims about the same attempt. |
| Clinical mappings | Check actual option-set codes/results. Positive HMIS totals do not prove Tracker positives must match, but thousands of tests and zero mapped positives demand mapping and coverage checks before claiming no recurrence. |
| Indicators | Trace each displayed value to source, period, facilities, formula and units. Investigate numerator/denominator mismatches; never clamp a rate above 100% to disguise an incompatible denominator. |
| Maps | Use supplied GeoJSON and existing geography mapping services. Check that a Pader view actually renders its scoped boundaries, not an unfiltered national outline under a district title. |
| Navigation | Verify overview, signals, investigations, map, patients, analytics, commodities, data quality, facilities and reports. A genuine zero is different from unavailable, unconfigured or not evaluated. |

Do not declare a source permission problem without inspecting the response and current scope. Do not ask the owner to paste passwords into chat or introduce synthetic data into the live database.

### 1.3 Confirmed code gaps to address

These were found in the source, independently of old screenshots:

1. `integrations/dhis2/live_dashboard.py` fetches the laboratory stage for the live patient calculation. The mapping also names medical-visit and medicine stages, but naming them is not equivalent to retrieving and joining them. The live timeline cannot currently satisfy the requested care/treatment history.
2. Its `_assemble` groups positive laboratory records into visits using a parent reference, or facility plus date, then calls `visits.setdefault`. This helps avoid counting two tests on one visit twice, but does not preserve an explicit, reviewable duplicate-decision model. Recurrence is essentially at least two grouped positive visits, without the requested minimum gap, maximum anchored window and N controls.
3. The live row's `interval_days` is first-to-latest. `services/patient_surveillance.py` computes latest-to-previous. The same-looking label can represent different intervals.
4. `PatientSurveillanceService.patients_of_interest` limits encounter rows to 5,000 before grouping people and returns another limited list. Joining test rows before that limit can further affect coverage. This cannot establish complete patient-level counts or longitudinal history.
5. `analytics/episodes.py` groups using gaps between successive encounters, including context attendances. This is a different episode definition from a positive-to-positive, anchored recurrence window. Do not silently change the meaning of historical method versions.
6. `api/v1/governance.py` exposes reading of existing governance information; do not assume definition creation/approval controls already exist because governance services exist.
7. Existing investigations require `signal_id`. A patient alias cannot simply be supplied to that endpoint as if it were a signal.

During repair, make present behavior honest, consistent and adequately tested. During the enhancement phase, replace the overlapping recurrence implementations with the shared engine specified below. Do not create an interim fourth recurrence algorithm that must later be removed.

### 1.4 Repair gate

Produce `docs/operations/current-system-repair-report.md` containing:

- reproduced failure, actual cause, repaired files, regression test and observed result;
- what worked without changes;
- anything that could not be exercised because of source availability or unavailable local credentials;
- a section-by-section dashboard check;
- the actual local-service status, without invented completion percentages.

The gate is satisfied when reproduced application defects are repaired and verified. Genuine absence of source information is not a reason to fabricate values or permanently block unrelated enhancements. Clearly record such limitations and continue with unaffected work. An unresolved core startup/authentication defect is not a successful repair gate.

## 2. Why MARS currently has two paths

There are two data-processing routes, not two Ministry logins:

| Path | Purpose and current implementation |
| --- | --- |
| Live source/snapshot | `integrations/dhis2/live_dashboard.py` reads authorized eRegisters data; `durable_live_dashboard.py` and `live_sync_store.py` persist encrypted snapshots and resumable jobs; `api/v1/live_data.py` serves them to live dashboard/patient views. It does not require all remote events first to become local `OpdEncounter` rows. |
| Stored encounter/analytics | `domain/encounter.py` holds normalized local encounters. `analytics/episodes.py`, `analytics/recurrence.py` and their workers produce method-versioned analyses. `services/patient_surveillance.py` also creates patient summaries directly from these local encounters. |

Keeping distinct source adapters can be useful. Maintaining different recurrence meanings across these routes is the problem. A further direct count in the stored patient service means there are more calculation implementations than the phrase "two paths" suggests.

Target: **two source adapters, one canonical encounter contract, one duplicate engine, one recurrence engine, versioned definitions and consistent outputs.** Do not force a risky full live-to-local ingestion rewrite as a prerequisite. Do not simply delete the existing stored analytics, historical results, live synchronization or aliases.

Browser routes are a separate matter. `frontend/src/app/App.tsx` maps `/command-centre` and `/district/:unitId` to `CommandCentreView`, and also contains remote-scope and stored-workspace routes. Explain their navigation purposes if asked; route aliases are not themselves proof of two separate applications.

## 3. Implementation contract: semantics before UI

Write these semantics into `docs/methods/configurable-recurrence.md`, typed models and tests before wiring panels. The choices below are implementation defaults for exploratory analysis, not assertions of a clinically validated resistance method.

### 3.1 Canonical encounter and clinical evidence

Create immutable, typed backend DTOs for patient linkage, encounter, tests, diagnoses, observations, medications, referrals, facility transitions, source provenance and retrieval coverage.

An encounter includes:

- internal stable patient linkage reference, source namespace, encounter reference, facility and authorized geography references;
- encounter/test dates, available timestamps, timezone/date precision and parent-link evidence;
- all component tests with methods, mapped outcomes and source references;
- diagnoses, fever/observations, attendance/admission/outpatient status, referrals and outcomes when recorded;
- medication name, formulation, dose and units, quantity, frequency, duration, prescribed date, dispensed status/date/quantity where actually recorded;
- field-level availability and multiple quality flags;
- mapping/schema versions and immutable source revision information.

Do not confuse "not mapped", "not returned", "not recorded", "not authorized" and an explicit negative value. Do not infer dose, administration, adherence, dispensing or treatment success from medicine quantity or drug name. A parsed prescription total is not verified dispensing.

Group tests sharing a verified clinical parent into one encounter while retaining every test. RDT plus microscopy on the same encounter is not two positive encounters. Deduplicate repeated retrieval of the exact same source event/revision without discarding provenance. Distinct records without a reliable parent must remain inspectable candidates, not be silently merged beyond an explicitly recorded policy decision.

Attach treatment by verified encounter/parent linkage. Same patient and nearby date alone is not sufficient to claim that medicine treated a particular positive encounter. Display unlinked medicines separately as contextual evidence.

Cross-facility linkage requires the same stable source patient reference within a verified source namespace, plus current source and MARS permissions. Do not merge by name, approximate demographics or an assumed equivalence between a local UUID and a DHIS2 ID. Preserve existing alias algorithms and key versions; add explicit adapter identity metadata instead of changing aliases invisibly.

### 3.2 Independent duplicate classification

Implement a pure duplicate classifier before recurrence evaluation. Distinguish:

1. the same source event returned again or updated;
2. multiple tests belonging to one verified encounter;
3. distinct encounters/records for the same person on the same calendar day;
4. near-duplicate records supported by deterministic, versioned field comparisons.

Use the configured reporting timezone, initially `Africa/Kampala`, when converting aware timestamps to calendar days. Preserve date-only values without inventing times. Missing/ambiguous dates are quality issues, not fabricated midnight observations.

Store duplicate-group IDs, member references, reasons, compared fields and confidence category. Missing fields must not count as positive evidence of similarity. Similarity rules operate within already established patient linkage; they never become fuzzy identity linkage.

Same-day policies:

- `exclude`: same-day candidates contribute at most one representative positive encounter per patient/day to the recurrence calculation; all records remain visible.
- `review_separately`: the same counting protection, plus a dedicated review queue and explicit unresolved-quality markers.
- `include`: distinct encounters may count separately. Exact duplicate source events and multiple tests of one verified encounter never count twice.

Document deterministic representative selection, without changing the source record. Inclusion of day-zero pairs also requires `minimum_gap_days = 0`; an include policy does not override a positive minimum gap. Cross-facility same-day records remain visible and must not be asserted to be erroneous merely because the date matches.

### 3.3 Shared positive-to-positive recurrence engine

Create a pure, source-independent API along these lines:

```python
def evaluate_recurrence(
    encounters: Sequence[CanonicalEncounter],
    definition: FrozenRecurrenceDefinition,
    query: FrozenAnalysisQuery,
    coverage: EvidenceCoverage,
) -> RecurrenceEvaluation:
    ...
```

Separate normalization, duplicate classification, qualifying-sequence evaluation, aggregation and explanation construction. No database, network, browser or current-clock dependencies inside the clinical sequence evaluator.

Definition fields include minimum gap, maximum anchored follow-up window, minimum positive encounter count, same-day policy, duplicate rule version, permitted confirmed-positive test mappings, treatment eligibility, linkage eligibility, quality eligibility, interval bands and engine semantic version. User-selectable result filters cannot turn negative/unmapped results into confirmed positives.

Use explicit initial sequence semantics:

- Only confirmed positive encounters contribute to a qualifying sequence. Nonpositive visits remain context and cannot bridge a recurrence window.
- Minimum gap is inclusive between consecutive selected positive encounters.
- Maximum follow-up is inclusive from the first selected positive to the last selected positive, not a rolling window that grows indefinitely with each attendance.
- Require at least N distinct qualifying encounters, with N >= 2.
- Evaluate all possible positive anchors. A patient can qualify on a later chain even if an earlier positive is too distant.
- Produce a deterministic qualifying witness and preserve the full encounter history, including positives skipped by the selected definition. Use an efficient sorted-window/dynamic-programming approach, not exhaustive subset enumeration.
- For reporting attribution, a qualifying chain's final positive falls inside the selected reporting period. Retrieve lookback to `period_start - maximum_window_days`. Do not label an incompletely observed lookback as a reliable absence of recurrence.
- Count each person once in the overall qualifying-patient total. Show observed positive count, policy-eligible count and qualifying-chain count separately.

Return explicitly named adjacent positive intervals, anchor-to-positive intervals and the qualifying-chain interval. Remove the ambiguity of the old bare `interval_days` through additive named fields and a documented compatibility adapter.

For positives on days 0, 9 and 23, expose 9, 14 and 23 with their actual endpoint encounters. A negative visit between them does not alter those differences. For an exploratory definition min=7, max=28, N=2, day 3 alone does not qualify against day 0; day 12 does; day 35 does not qualify against day 0. It can still qualify against another eligible anchor if the sequence meets the definition.

Validate min >= 0, max >= min, N >= 2 and valid date ranges. Establish documented resource ceilings as settings, not unexplained UI restrictions. Support values beyond 10 days. Interval bands must be ordered, nonoverlapping and validated, with a clear policy for open-ended bands and any gaps.

## 4. Retrieve sufficient real evidence, without breaking live synchronization

The current `config/dhis2/pader-live-v1.json` is schema version 2. It contains:

- programme `ueBhWkWll5v`;
- medical-visit stage `K2nxbE9ubSs`;
- laboratory stage `zKGWob5AZKP`;
- medicines stage `DA0Yt3V16AN`;
- parent-event data element `Wx7x4sMAa62`;
- diagnosis `GdWHQHW4cyT`, signs `ZznYqd15BxB`, malaria-treated `UzzpUXan7VU`;
- patient type `lFKnsWk3TxJ`, outcome `WE19mnVVSpq`;
- referral-out `L6JzFSII5lg`, referral reason `fwzt9uypedL`;
- lab type `uiTFOkMMwV3`, lab result `yzmgiNZEUCz`;
- medicine type `ZDN18IlfOGh`, medicine quantity `gJuWvi6vOdu`.

Verify these against current metadata before relying on them. Do not invent additional UIDs. The separate `tracker/mapping.py` approved ingestion mapping uses a different schema; do not feed the live schema-2 document into it blindly.

Retrieve and join laboratory, visit and medicine stages over the same exact authorized facilities and required date extent. Discover only necessary additional metadata for formulation/dose/frequency/duration/dispensing/age/sex. Where no verified source field exists, show field-level unavailability and document it; basic positive recurrence should still work when dose is unavailable unless a particular analysis expressly requires it.

Retain existing HTTPS host allowlisting, read-only source calls, scope checks, request limits, pagination and cancellation/session checks. The current client bounds a Tracker request to 62 days. Longer analytical coverage requires planned, bounded chunks, not removing the guard. Deduplicate across chunk boundaries. If a record cap is reached, split safely where possible or mark coverage partial; never silently treat a capped response as complete.

Checkpoint progress by source, facility, stage, date chunk, cursor and mapping version. An old laboratory-only checkpoint must not falsely satisfy a new three-stage plan. Retain fenced leases and short database transactions; do not hold a transaction open around remote requests.

Persist a versioned, encrypted canonical evidence dataset server-side. Keep patient evidence out of the general aggregate snapshot DTO. Dataset coverage must describe facilities, stages, date extent, mapping version, complete/partial status and exclusions. Reuse sufficient retained evidence for parameter changes without refetching the Ministry on every Apply.

Separate cache identity for evidence retrieval from the definition/run fingerprint. Both must respect source, effective current authorization and key version. A changed definition creates a new analysis result over the same eligible dataset, not an unnecessary new remote retrieval. An expanded window or changed rights must trigger a coverage check.

## 5. Persistence, programme governance and exploratory analysis

Use a small additive domain model, with these responsibilities:

- `ClinicalEvidenceDataset`: encrypted canonical payload/reference, source/mapping/schema fingerprints, coverage, key version and creation metadata; no cleartext raw patient payload in a JSON column.
- `RecurrenceDefinition` and immutable `RecurrenceDefinitionVersion`: named reusable exploratory definitions with owner, parameters, version and checksum. These are not an alternative approval system.
- `RecurrenceAnalysisRun`: immutable definition snapshot plus exact dates, selected scope, filters, creator, source dataset IDs/hashes, coverage, engine version and result reference. Job lifecycle metadata can change; the submitted analytical manifest cannot.
- `PatientRecurrenceFinding`: stable scoped reference to a patient's finding within a run, with encrypted detail/explanation and evidence references needed for review.

For official programme mode, reuse `MethodDefinition`/`MethodVersion` and the existing governance transition service. A dedicated recurrence method can use the existing episode-rule method kind with a new method code and explicit engine version. Do not overwrite historical episode definitions or reinterpret their old results.

Promoting exploratory parameters means creating an official method draft and performing the real authorized lifecycle, with audit and validation evidence. It does not mean toggling an `approved=true` flag. Add the missing authorized management endpoints and UI. Respect `CONFIGURATION_MANAGE`, `METHOD_APPROVE` and the current role matrix.

Provide a usable exploratory starting preset, clearly labeled, using the brief's illustrative 7/28/N=2/exclude settings. It is not a fabricated national approval. Programme mode selects an actually active method; absent one, expose the precise status and allow authorized exploratory work rather than making the whole dashboard unusable.

Freeze scope/dates/filters in each saved run even though reusable definition versions can be used over new periods. Use atomic version allocation, database uniqueness and optimistic concurrency. Reopening a run must recheck current permissions; a saved analysis is not a permanent grant to the creator's former scope.

Add new Alembic migrations after the actual current head. Do not edit applied migration `0027_live_sync` or reset `mars_live`. Grant new tables only the required restricted roles. Test both fresh installation and upgrade from the current schema, including the identity role's continued inability to read unrelated analytical tables.

## 6. API, filtered views and analysis outputs

Add a coherent `/api/v1/recurrence` resource family, using existing API conventions, request validation, CSRF, audit and error envelopes:

| Endpoint family | Responsibility |
| --- | --- |
| `definitions`, `definitions/{id}/versions` | List/create immutable exploratory definitions and versions; explicit ownership/scope rules. |
| Official method draft/transition endpoints | Wrap existing governance services and their permissions; do not duplicate lifecycle rules. |
| `runs` POST | Validate parameters and evidence coverage; return a run/job receipt, using 202 for asynchronous work and idempotency for duplicate Apply submissions. |
| `runs/{id}` GET | Status, applied manifest, dataset freshness/coverage and summary measures. |
| `runs/{id}/patients` GET | Server-side filtered, stable cursor pagination with complete totals independent of page size. |
| `runs/{id}/patients/{alias}` GET | Full permitted timeline, duplicate evidence and inclusion explanation. |
| `runs/{id}/duplicates`, `facilities`, `intervals`, `trends`, `treatments`, `geography` | Consistent projections from the same frozen run. |
| `comparisons` POST | A/B results with shared evidence provenance, overlap, differences and coverage checks. |
| Patient-finding review/investigation endpoint | Open a real scoped case review with idempotency and an immutable finding reference. |

An analysis request is not authorization. Resolve and intersect requested geography/facilities with current MARS and source rights before querying. Apply patient-detail sensitivity and `CASE_EVIDENCE_VIEW`; an aggregate-only user must not receive patient arrays hidden by CSS. Re-identification stays on the separate, audited permission boundary. Scoped source event IDs may be shown in an authorized duplicate-detail view; raw tracked-entity IDs, credentials and direct identity must not leak into logs or aggregate APIs.

Use the existing credentialed API client, not uncredentialed ad-hoc fetches. TanStack Query keys must include authenticated-session/scope identity and run/definition/dates/filters. Clear sensitive caches on logout or rights changes. Ignore late responses from an older Apply rather than replacing newer results.

Filter by date period, district/subcounty/facility, test type, regimen, age group, sex, quality and investigation status. Document whether a filter applies to a qualifying endpoint, whole patient cohort or surrounding context. Do not remove context encounters before reconstructing the clinical sequence. Unknown age/sex/treatment must remain explicit categories; unknown age is not zero years.

Outputs must include qualifying patients, interval distribution, facility counts and proportions, weekly trends, maps, treatment-before-positive patterns, and patients with 2/3/4+ positives. Define denominators and counting units explicitly:

- A repeat-positive proportion is not a resistance or treatment-failure rate.
- A suitable initial denominator is distinct linked patients with an eligible positive in the reporting period, using the same filters and coverage. Show numerator and denominator.
- Facility recurrence attribution uses qualifying final-positive facility, with clear handling of multiple qualifying episodes. National/district unique-patient totals are not the sum of facility patient counts.
- Weekly patient counts and distinct reporting-period counts need not sum; state the attribution rule.
- Offer an observed-adjacent-interval distribution before recurrence thresholds, clearly distinguished from qualifying-chain intervals after thresholds. Both use the same selected source cohort; avoid making a seven-day filter falsely suggest no shorter intervals exist in the evidence.
- Treatment matrix cells must say whether they count patients or linked positive-to-positive transitions. Preserve unknown/unlinked treatment categories and distinguish prescribed from dispensed.

A/B comparison must use the same immutable source dataset covering both definitions, or explicitly refuse an unsupported comparison and explain how to obtain the required common coverage. Return A-only/B-only/intersection/union, facility and treatment distributions and geographic changes. Overlap/detail endpoints still require patient access.

## 7. Integrate aggregate surveillance and investigations

Keep HMIS burden, testing, positivity, treatment reporting, commodities, completeness and existing temporal/geographic analyses functioning. Do not overwrite official aggregate metrics when a user changes an exploratory patient definition.

Join recurrence findings to aggregate context by verified facility/geography and period, with each source's completeness and provenance. HMIS and Tracker are different reporting processes; do not force their totals to agree by altering records or sharing incompatible denominators.

Represent quality as multiple flags, including `VALID`, `POSSIBLE_DUPLICATE`, `INCOMPLETE_TEST_EVIDENCE`, `INCOMPLETE_TREATMENT_EVIDENCE`, `IDENTITY_LINKAGE_UNCERTAIN` and `DATE_QUALITY_ISSUE`. `VALID` cannot coexist with contradictory unresolved flags. Missing treatment does not erase a confirmed positive; it may make a treatment-response calculation ineligible.

Distinguish patient case-for-review, facility cluster and corroborated potential treatment-response signal. A configured, versioned higher-level method must establish any escalation/priority thresholds. Do not manufacture a high-priority cluster from one patient, arbitrary counts or a missing configuration. Explain contributing evidence, exclusions and uncertainty.

Existing `Investigation.signal_id` is non-null and unique. Extend deliberately:

- retain the existing signal-origin path;
- add `patient_finding_id` as an alternative source, with a database check enforcing exactly one source and uniqueness/idempotency for a finding;
- update service queries, API union schemas, serializers, audit and UI so they no longer assume every investigation has a signal;
- preserve existing investigation state machine, ownership, scope, optimistic version checks and history;
- require explicit manual triage for an exploratory finding, with no invented epidemiological priority. If existing priority fields require a value, use the existing supported review semantics or add a narrowly documented review state rather than labeling it high risk.

The user must be able to navigate overview -> facility -> qualifying list -> exact patient timeline -> review/investigation while retaining run, definition, scope and period context.

## 8. Preserve and extend the current visual design

Use the current source design system, not the older generated reference image as a replacement theme:

- `--surface-sidebar: #0b1f33`, `--accent: #0b6e63`, light neutral page/cards;
- existing typography, 8px spacing rhythm, thin borders, modest radii and minimal shadows;
- existing `Measure`, `States`, `QueryRegion`, `Surveillance` and map components;
- semantic amber/red only where justified; grey means unavailable, not low risk.

Keep the sidebar and overview composition familiar. Add a compact "Repeat-positive definition" control panel with programme/exploratory mode, minimum gap, maximum window, N, same-day policy and Apply. Put secondary filters behind an accessible Advanced filters disclosure. Show a thin applied-definition summary with unsaved-change state, source freshness and coverage.

Use integrated Patients, Duplicates, Treatment and Compare tabs/sections, not a second application. Keep all qualifying patients accessible through proper pagination. Patient detail should show a chronological care timeline, linked medications, interval labels, facility transitions and an inclusion/exclusion explanation. Avoid making the user scroll past large blocks of defensive placeholder prose to find the evidence.

Preserve the overview's aggregate cards and maps. A shared recurrence panel can be reused there and in patient analytics. New charts should reuse existing rendering conventions; native SVG is sufficient if it meets accessibility and scale needs. Do not introduce a new UI framework merely for charts.

Map exclusively through supplied GeoJSON/current geography services and validated facility-to-administrative crosswalks. If facilities lack coordinates, aggregate to mapped subcounty/district polygons. Do not invent facility pins or treat polygon centroids as facility locations. Keep unmapped contributions and completeness visible. Use actual boundary versions from the geography manifest.

Check responsive layout at 1366x768, 1920x1080 and a narrow screen; keyboard navigation, labels, focus, legends, empty/partial/error states and table overflow. Save visual comparisons only with nonclinical test fixtures in an isolated environment. No real patient screenshots in the repository.

## 9. File-by-file implementation map

Paths below are relative to the repository root. Existing paths were located during inspection. New names are proposed implementation locations, not claims that those files already exist. Read neighboring conventions before creating them. Modify a conditional file only when the implementation requires it; document any deviation from this map.

### 9.1 Repair and existing backend integration

| Existing file(s) | Intended work |
| --- | --- |
| `scripts/start-mars-live.ps1` | Diagnose startup only; narrowly repair reproduced launcher failures. Preserve key handling, process ownership checks and restricted roles. No feature-driven rewrite. |
| `backend/src/mars/services/live_sync_store.py` | Preserve/further test fencing and checkpoint lineage; support versioned retrieval plan/dataset references without exposing raw evidence. |
| `backend/src/mars/services/durable_live_dashboard.py` | Orchestrate bounded staged retrieval and dataset publication, progress, current-scope checks and safe retries. |
| `backend/src/mars/services/live_dashboard.py` | Maintain snapshot/export compatibility and safe errors; delegate recurrence semantics rather than duplicating them. |
| `backend/src/mars/integrations/dhis2/live_dashboard.py` | Retrieve all required mapped stages; replace direct repeat-count logic with canonical adapter/shared engine; preserve aggregate assembly. |
| `backend/src/mars/integrations/dhis2/tracker/client.py` | Extend requested fields/planned retrieval only as verified; retain allowlist, paging, date and scope limits. |
| `backend/src/mars/integrations/ports.py` | Add only necessary provenance/date precision to the source-neutral envelope. Do not add direct identity attributes to general event payloads. |
| `backend/src/mars/integrations/dhis2/tracker/mapping.py`, `tracker/translate.py` in that same directory | Adapt verified mappings/translation where reused; keep ingestion and live mapping schemas explicit. |
| `config/dhis2/pader-live-v1.json` | Versioned, metadata-verified clinical field availability. Do not change existing mappings speculatively. |
| `backend/src/mars/services/patient_surveillance.py` | Remove inconsistent independent calculations from the new path; query complete evidence before counting, paginate people instead of truncating encounters. Preserve legacy response compatibility. |
| `backend/src/mars/analytics/episodes.py`, `backend/src/mars/analytics/recurrence.py` | Version-aware delegation for the new recurrence method; retain historical episode semantics/results. |
| `backend/src/mars/workers/episode_build.py`, `backend/src/mars/workers/recurrence_compute.py` | Dispatch by explicit method/engine version; do not reinterpret old builds. |
| `backend/src/mars/domain/encounter.py` | Conditional additive clinical provenance/medication fields if needed for stored-source parity; no guessed backfill. |
| `backend/src/mars/services/governance_service.py`, `backend/src/mars/api/v1/governance.py` | Reuse lifecycle, add authorized draft/version/transition operations and concurrency safeguards where required. |
| `backend/src/mars/domain/investigation.py`, `backend/src/mars/investigations/service.py`, `backend/src/mars/api/v1/investigations.py` | Add patient-finding review source safely, preserving signal-origin behavior and workflow invariants. |
| `backend/src/mars/signals/engine.py`, `backend/src/mars/services/signal_query.py`, `backend/src/mars/workers/signal_generate.py` | Integrate governed recurrence/quality/context evidence and explainability without invented severity rules. |
| `backend/src/mars/api/v1/schemas.py`, `backend/src/mars/api/v1/live_data.py`, `backend/src/mars/api/v1/patients.py` | Explicit interval fields, summary/detail separation, compatibility routes and scope-safe response schemas. |
| `backend/src/mars/api/v1/router.py`, `backend/src/mars/api/dependencies.py`, `backend/src/mars/main.py`, `backend/src/mars/db/models.py` | Register new routes/services/models and startup validation; keep live/demo boundaries intact. |
| `backend/src/mars/security/permissions.py` | Prefer existing permissions. Add a new narrowly scoped permission only if necessary, with explicit role mapping and tests; never grant identity by implication. |

Authentication/session files should be edited only if the repair phase reproduces a defect in them. Trace `services/auth_service.py`, `security/live_session.py`, `api/middleware.py`, `security/origin.py` and `integrations/dhis2/login/provider.py` rather than preemptively replacing them.

### 9.2 New backend modules

Under `backend/src/mars/` create:

| New path | Code to write |
| --- | --- |
| `analytics/longitudinal.py` | Typed immutable canonical evidence, frozen query/definition and result contracts. |
| `analytics/duplicate_detection.py` | Deterministic duplicate classification, decision provenance and same-day policy application. |
| `analytics/positive_recurrence.py` | Pure positive-anchor sequence algorithm, explicit intervals and witness explanations. |
| `analytics/recurrence_views.py` | Counts, denominator-safe facility/weekly summaries, bands, matrix and geography projections from one evaluation. |
| `integrations/dhis2/tracker/clinical_adapter.py` | Source events -> canonical care pathway with verified parent joins and mapping availability. |
| `services/stored_encounter_adapter.py` | Scoped normalized local encounters -> the identical canonical contract. |
| `domain/recurrence_analysis.py` | Dataset, exploratory definition/version, analysis run and patient-finding persistence models. |
| `services/clinical_evidence_store.py` | Encryption, versioned dataset publication/access, coverage and retention; reuse established cryptographic conventions. |
| `services/recurrence_analysis_service.py` | Authorization, coverage planning, immutable run submission/evaluation, paging and comparison orchestration. |
| `api/v1/recurrence.py` | Validated, permissioned endpoint family described above. |

Add migration file(s) under `backend/migrations/versions/` using the next real revision and correct dependency. Include constraints/indexes/grants/model registration and fresh/upgrade/reversibility tests. Do not allocate a guessed revision number before inspecting head.

### 9.3 Existing frontend and contracts

| Existing file(s) | Intended work |
| --- | --- |
| `frontend/src/features/patients/PatientSurveillanceView.tsx`, `patient-surveillance.css` in the same directory | Integrate real run-backed patients, full care timeline, duplicate review, filters and explanation using current styling. Remove hard-coded Pader copy where user scope is dynamic. |
| `frontend/src/features/command-centre/CommandCentreView.tsx`, `command-centre.css` | Add shared compact definition/summary entry points; retain aggregate panels, maps and current composition. |
| `frontend/src/features/operations/OperationalViews.tsx` | Integrate analysis, DQ, commodity context and reports without contradictory counts or faux empty states. |
| `frontend/src/features/operations/useLiveDashboard.ts`, `useReportingPeriod.ts` | Keep shared retrieval/period behavior; distinguish retrieval from analysis execution and prevent stale response/cache leakage. |
| `frontend/src/features/workspaces/DistrictWorkspaceView.tsx`, `FacilityWorkspaceView.tsx` in that directory | Preserve navigation context and consistent recurrence drilldown. |
| `frontend/src/features/investigations/ActionCentreView.tsx`, `frontend/src/features/signals/SignalEvidenceView.tsx` | Display correct source type and link to frozen patient/aggregate evidence. |
| `frontend/src/features/map/SvgBoundaryMap.tsx`, `BoundaryMap.tsx`, `GeographyCanvas.tsx`, `geography.ts` in that directory | Reuse verified GeoJSON layers and scoped aggregates, with missing-coordinate/mapping handling. |
| `frontend/src/app/App.tsx` | Integrate analysis/run navigation within existing authenticated routes; retain compatible patient links. |
| `frontend/src/api/client.ts` | Typed authenticated run/definition/detail calls, payload validation, pagination and safe error mapping. |
| `contracts/openapi.json`, `frontend/src/api/schema.d.ts` | Regenerate from the actual backend, never hand-edit generated types. |

Read and reuse `frontend/src/design-system/tokens.css`, `base.css`, `Measure.tsx`, `States.tsx`, `QueryRegion.tsx`, `Surveillance.tsx` and associated CSS. Do not change global theme tokens just to style the new module.

Create under `frontend/src/features/recurrence/`:

- `RecurrenceDefinitionPanel.tsx`: draft/applied controls, mode and save/version interaction.
- `useRecurrenceAnalysis.ts`: validated submission, polling, immutable run context and safe cache keys.
- `RecurrenceResults.tsx`: shared KPI/analytics arrangement and page-level states.
- `PatientCareTimeline.tsx`: encounter-level care and treatment sequence with interval evidence.
- `DuplicateReviewTable.tsx`: paginated source-record review with reason badges.
- `RecurrenceCharts.tsx`: accessible interval, weekly and frequency views.
- `TreatmentResponseMatrix.tsx`: linked-treatment analysis with units and missingness.
- `DefinitionComparison.tsx`: run-based A/B comparison.
- `recurrence.css`: scoped styles consuming existing tokens.

Keep components reasonably small; adapt this split to actual shared code rather than creating empty wrappers solely to match filenames.

## 10. Required tests and objective acceptance criteria

New test files should include:

- `backend/tests/unit/test_positive_recurrence.py`
- `backend/tests/unit/test_duplicate_detection.py`
- `backend/tests/unit/test_clinical_adapter.py`
- `backend/tests/unit/test_recurrence_views.py`
- `backend/tests/api/test_recurrence_api.py`
- `backend/tests/integration/test_recurrence_analysis.py`
- `frontend/tests/recurrence-definition.test.tsx`
- `frontend/tests/patient-care-timeline.test.tsx`
- `frontend/tests/recurrence-comparison.test.tsx`
- `frontend/tests/e2e/recurrence.spec.ts`

Extend existing live-dashboard, patient-surveillance, durable-sync, identity-vault, episode/recurrence, investigation, permission and geography tests as applicable. Test fixtures must stay in test environments, never live data or fallbacks.

Mandatory cases:

1. Two attendances but only one positive do not qualify. Negative context cannot extend the anchored window.
2. Repeated retrieval of one source event counts once; an updated revision is handled deterministically.
3. RDT plus microscopy on one parent encounter counts once and both tests remain visible.
4. Same-day distinct encounters behave differently under exclude/review/include; include still obeys min gap. Records are retained in all modes.
5. UTC/local-day boundaries and date-only source records behave predictably.
6. Days 0/9/23 expose 9/14/23. Exact lower and upper bounds qualify. N=2/3/4 and later-anchor qualification work.
7. Day 0/20/40 with max=28, N=3 does not qualify as a three-positive chain merely because each adjacent gap is short enough.
8. Cross-month lookback works; inadequate lookback reports partial/unknown rather than zero.
9. Missing stable identity prevents cross-person/facility linkage; names never repair it. Restricted facilities never contribute hidden encounters.
10. Unmapped tests, ambiguous dates and incomplete retrieval do not become confirmed negatives or reliable zero recurrence.
11. Medicines attach to their real encounter; unlinked medication remains context. Missing dose is displayed honestly; prescribed does not become dispensed.
12. Same canonical evidence through live and stored adapters yields identical new-engine findings, intervals and totals. Historical method results remain unchanged.
13. More than 5,000 encounters and more than one patient page retain complete qualifying totals and histories. Page size never changes the KPI.
14. A/B overlap is deterministic over the same dataset; differing coverage is rejected or explicitly resolved before comparison.
15. Definitions/runs are immutable and concurrent version/save requests cannot collide. An analyst cannot self-approve without the actual governance permission.
16. Patient, duplicate, comparison, export and investigation endpoints enforce current scope/sensitivity; copied run IDs and changed facility parameters cannot bypass it.
17. Logout, revoked rights, expired credentials and stale jobs cannot publish or disclose stale-scope evidence. A late old Apply cannot overwrite newer results.
18. Failed refresh retains the last good snapshot and reports the correct attempt status. Completed prior retrieval does not seed a fresh refresh with obsolete checkpoints.
19. Patient-origin and signal-origin investigations both work, with idempotency, exactly-one-source constraints, state transitions and audits.
20. Aggregate dashboards remain functional and distinguish genuine zero from partial/unavailable. Map sums, facility counts and unique-patient totals use documented attribution.
21. No UI/API text asserts drug resistance from repeated positivity or recorded regimen. All explanations identify actual evidence and method version.
22. Test the complete owner workflow: sign in -> scoped overview -> apply definition -> facility -> patients -> timeline -> review -> comparison -> return to unchanged aggregate context.

### Verification commands

Use the existing environment and CI conventions. Commands below assume repository root. Inspect tests and environment first; never aim an integration suite or migration downgrade at `mars_live`.

```powershell
Push-Location backend
& .\.venv\Scripts\python.exe -m ruff format --check .
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m mypy
& .\.venv\Scripts\python.exe -m pytest -m "not integration" -q
Pop-Location

& .\backend\.venv\Scripts\python.exe scripts/export_openapi.py
Push-Location frontend
npm.cmd run generate:api
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run test
npm.cmd run build
Pop-Location

& .\backend\.venv\Scripts\python.exe scripts/export_openapi.py --check
& .\backend\.venv\Scripts\python.exe scripts/terminology_lint.py
```

Run each gate with exit-code checking; do not continue blindly after a failed native command. On an explicitly verified disposable PostgreSQL/PostGIS test database, run the integration suite and fresh/upgrade/downgrade migration tests from `.github/workflows/ci.yml`. Use test URLs from a protected environment, not committed credentials. The existing Playwright configuration defaults to port 5173 and development authentication: override it to a separate test stack before running E2E, never point those tests at the live Ministry session.

No test run occurred as part of preparing this handoff. Report newly executed results, not old totals copied from prior model reports.

## 11. Execution order and final delivery

1. Finish the repair phase and its report; preserve local fixes and stabilize existing behavior.
2. Freeze semantic contract and write failing engine/parity tests.
3. Build canonical adapters, independent duplicate classifier and shared recurrence evaluator.
4. Add staged bounded retrieval, encrypted evidence persistence, coverage and migrations; verify resumability/security before expanding live requests.
5. Add immutable definitions/runs, governance operations and API projections; verify complete counts and current-scope enforcement.
6. Connect current dashboard/patient views to the shared results, retaining compatibility for historical routes/data.
7. Add care timeline, duplicate view, charts, matrix, comparison and filters using the current design system.
8. Integrate aggregate context, governed higher-level signals and patient-origin investigations.
9. Run full regression and isolated browser/visual tests. Then perform a controlled local cutover through the supported launcher if needed, without reinitializing keys or touching unrelated processes.
10. Verify actual UI/API/database health and an authorized live workflow. Report any source-dependent field that remains unavailable. Do not claim localhost is running solely because the launcher printed "Starting".

Update `docs/data-dictionary/recurrence.md`, `docs/data-dictionary/episodes.md`, `docs/data-dictionary/signals-and-explanations.md`, `docs/operations/live-synchronization.md` and `docs/security/live-sessions.md` where behavior changes. Add an implementation/acceptance report mapping each of the original 30 requirements to code and tests.

Final response must separate:

- existing problems repaired, with cause and proof;
- new features implemented and their locations;
- test/build/migration/browser results;
- verified live-source availability versus unverified or missing fields;
- current local URL/service status;
- remaining work, if any, without claiming perfection or completion while known required work remains.

Do not stop after building filters or adding placeholders. The finished feature must change the actual authorized patient results, preserve complete evidence, explain inclusion, support the requested analysis and review workflow, and leave the existing aggregate MARS dashboard working and visually coherent.
