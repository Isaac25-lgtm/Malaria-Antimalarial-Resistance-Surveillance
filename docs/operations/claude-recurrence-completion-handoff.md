# MARS recurrence handoff after Codex completion pass

Prepared 12 September 2026 from the current uncommitted workspace. This is an
implementation handoff, not evidence that GitHub, the live Ministry source, or
the production database has been changed.

## Copy/paste prompt for Claude

Work in the existing repository at:

`F:\MY FILES\DATA SCIENCE\MARS PROJECT\MARS IMPLEMENTATION`

Continue from the current dirty worktree. Do not reset, clean, checkout over,
reformat broadly, rotate local keys, change `mars_live`, or recreate the feature.
The local changes belong to the owner. First read this document, then inspect
`git status`, `git diff --ignore-space-at-eol`, the migration head, and the files
listed below. Verify every claim from code and tests.

The repair and configurable recurrence implementation is now wired end to end
through the application code. Your immediate role is independent review and,
only if requested by the owner, a careful commit/push of the existing changes.
Do not substitute a plan for inspection. Do not claim live validation or
PostgreSQL integration unless you actually run them against an isolated test
database or an authorised session.

### Preservation and security rules

- Preserve every substantive local edit, including durable live-sync fencing,
  checkpoint lineage, DPAPI launcher diagnostics, identity-vault boundaries,
  the GeoJSON map path, and the new recurrence implementation.
- Never put credentials, DPAPI material, patient identifiers, Tracker entity
  identifiers, database dumps, or live screenshots with patient data in Git.
- Never use synthetic values in live mode.
- Do not run destructive Git commands or migrations against `mars_live`.
- Keep the old episode engine for historical method versions. New positive-to-
  positive methods use explicit version-aware dispatch.
- Facility points require verified coordinates. Otherwise use the existing
  GeoJSON district/subcounty polygons and verified crosswalks; invent no pins.
- Preserve the MARS navy/teal design system and accessibility conventions.

## What Codex completed in this pass

### 1. Live evidence reaches the reproducible run system

- `backend/src/mars/services/live_recurrence.py` now requires and carries the
  exact `EvidenceCoverage` used for evaluation.
- `backend/src/mars/integrations/dhis2/live_dashboard.py` accepts an evidence
  sink and publishes canonical evidence only after the live session is checked
  again.
- `backend/src/mars/main.py` creates the evidence cipher and recurrence run
  executor, retains authorised canonical live evidence through
  `ClinicalEvidenceStore`, supplies the stored-source executor outside live
  mode, and closes executors during application shutdown.
- Regression tests prove evidence is retained outside the public aggregate
  snapshot and is never retained after session revalidation fails.

### 2. Persistent definitions, datasets and frozen runs

- Migration `backend/migrations/versions/20260911_0028_recurrence_analysis.py`
  adds encrypted evidence datasets, immutable definition versions, asynchronous
  runs, encrypted patient findings, duplicate payloads, and patient-origin
  investigations, with mutation guards and restricted runtime grants.
- `backend/src/mars/domain/recurrence_analysis.py` defines those records.
- `backend/src/mars/services/clinical_evidence_store.py` seals canonical
  evidence with AES-GCM/AAD and a key version.
- Dataset uniqueness is scope-safe: source kind, scope key, mapping version,
  content hash and key version are all part of identity. Identical bytes from
  different authorisation scopes cannot collapse into one dataset.
- `backend/src/mars/services/recurrence_analysis_service.py` implements saved
  definitions, immutable versions, ceilings, frozen manifests, idempotent
  submission, fenced asynchronous execution, current-scope checks, projections,
  paginated findings, duplicate review, comparison and audit events.

### 3. Typed and permissioned recurrence API

- `backend/src/mars/api/v1/recurrence.py` is registered by
  `backend/src/mars/api/v1/router.py` and wired in
  `backend/src/mars/api/dependencies.py`.
- It exposes programme status, definitions/versions, governance promotion and
  transitions, `POST /runs` with HTTP 202, run status, projections, cursor-
  paginated patients, patient timelines, duplicates, A/B comparison, and
  patient-origin investigation creation.
- Every read rechecks the caller's current geography/sensitivity. Aggregate
  permission does not expose patient arrays. Patient paths use the constrained
  `MARS-PT2-*` alias shape.
- `contracts/openapi.json` was regenerated at 92 paths/126 schemas and
  `frontend/src/api/schema.d.ts` was regenerated from it.

### 4. Investigation integration

- `backend/src/mars/domain/investigation.py` and migration 0028 enforce exactly
  one investigation source: either a surveillance signal or a recurrence
  patient finding.
- `backend/src/mars/investigations/service.py` creates patient-finding reviews
  idempotently, keeps their priority unclassified for manual triage, and does
  not write signal-method feedback for a patient-origin review.
- `backend/src/mars/api/v1/schemas.py` and
  `frontend/src/features/investigations/ActionCentreView.tsx` understand both
  origins. A live remote facility UID falls back only to the caller's single
  confirmed local facility scope when opening a readable investigation.

### 5. Persistence, API and governance lifecycle

- Migration `backend/migrations/versions/20260911_0028_recurrence_analysis.py`
  adds encrypted retained datasets, immutable definition versions, fenced runs,
  encrypted patient findings and patient-origin investigation constraints.
- `backend/src/mars/services/clinical_evidence_store.py` uses AES-GCM with
  purpose-specific AAD and key versioning. Dataset uniqueness now includes
  source kind, authorization scope, mapping version, content hash and key
  version, preventing equal content in different scopes from aliasing one row.
- `backend/src/mars/services/recurrence_analysis_service.py` validates resource
  ceilings, freezes manifests, evaluates retained evidence, pages results,
  rechecks current scope/sensitivity and supports governed promotion.
- `backend/src/mars/api/v1/recurrence.py` exposes programme status, definitions
  and versions, promotion/transitions, asynchronous 202 runs, run status,
  projections, patient list/detail, duplicates, comparison and patient-origin
  investigation creation.
- `backend/src/mars/api/dependencies.py`, `api/v1/router.py` and `main.py` wire
  the service/executor lifecycle. Shutdown closes both executor and engine.
- `contracts/openapi.json` and `frontend/src/api/schema.d.ts` were regenerated
  from the application rather than hand-edited.

### 6. Professional integrated recurrence workspace

The new `frontend/src/features/recurrence/` module is integrated into Patient
Surveillance and contains:

- `RecurrenceDefinitionPanel.tsx`: exploratory/programme mode, saved definition
  selection and saving, minimum gap, maximum window, positive-count threshold,
  same-day policy, treatment requirement, facility/test/treatment/sex/age/data-
  quality cohort filters, dirty/applied state and Apply.
- `useRecurrenceAnalysis.ts`: submit, run polling and reset behavior.
- `RecurrenceResults.tsx`: frozen provenance, numerator, denominator,
  proportion, coverage, full server-side patient filtering, cursor pagination,
  timeline navigation, duplicates and comparison.
- `RecurrenceCharts.tsx`: observed and qualifying interval distributions,
  facility count/rate, weekly trend, 2/3/4+ frequency and geography.
- `TreatmentResponseMatrix.tsx`: recorded prior treatment by interval band,
  explicitly counted as transitions rather than resistance.
- `PatientCareTimeline.tsx`: the actual API `timeline.entries` contract, positive
  test results, diagnoses, observations, fever, attendance, detailed encounter-
  linked medicines, referrals, outcomes, facility changes, algorithm decisions,
  intervals, explanation and investigation action.
- `DuplicateReviewTable.tsx`: expandable duplicate groups with reasons,
  confidence, dates/times, facilities, test results, source evidence references,
  retained representative and cursor pagination.
- `DefinitionComparison.tsx`: patient overlap plus facility, treatment and
  geographic differences over one retained dataset.
- `recurrence.css`: responsive layout using existing MARS tokens.

`frontend/src/app/App.tsx` includes the patient timeline route and
`frontend/src/features/patients/PatientSurveillanceView.tsx` embeds the
workspace, so this is not a separate application.

### 7. Defects A-I from the independent audit

| Defect | Resolution in the current worktree |
| --- | --- |
| A - test date provenance | `ClinicalTest` retains source time/day and precision; recurrence uses the qualifying positive-test day while the visit date remains timeline context. |
| B - namespace safety | Internal identity is composite `PatientKey(namespace, reference)` and the versioned display alias includes that identity. No name/demographic linkage was added. |
| C - cancellation/session loss | Active checks surround source requests; terminal session/cancellation/lease errors escape broad availability handling and prevent later reads/publication. |
| D - duplicate exclusion ordering | Duplicate decisions are applied before quality eligibility and all members remain reviewable. Same-day policy and quality exclusion remain distinct. |
| E - coverage/checkpoint semantics | Laboratory, clinical context, aggregate and overall-plan completion are separate; incomplete context cannot clear the plan checkpoint. |
| F - comparison identity | Evaluations and runs carry immutable dataset ID/hash; comparison refuses different evidence even if coverage labels match. Dataset identity is also authorization-scope safe. |
| G - incomplete negative | Missing forward or lookback coverage makes a potentially concealed absence indeterminate. Stored coverage comes from import provenance rather than an assumed minimum date. |
| H - paging/filtering | New run endpoints page after full evaluation with stable cursors and frozen totals; the recurrence UI includes server-side pages and period/run-aware query keys. |
| I - version dispatch | `backend/src/mars/services/recurrence_dispatch.py` and `workers/recurrence_compute.py` select old episode semantics or the new positive-to-positive engine explicitly by method version. |

## Verification recorded by Codex

- Full backend non-integration suite: **1,314 passed, 536 deselected**. The only
  warning was pytest being unable to write its optional cache; tests used a
  unique ignored `.runtime` temporary directory.
- Backend targeted recurrence/API/persistence suite: **94 passed**.
- Backend Ruff format: **294 files already formatted**.
- Backend Ruff lint: **passed**.
- Backend mypy: **passed, 207 source files**.
- Frontend ESLint and TypeScript: **passed**.
- Frontend Vitest: **137 passed in 19 files**.
- Frontend production build: **passed**; Vite retains the existing large
  MapLibre chunk advisory.
- OpenAPI export and generated client types: **passed**.
- Repository terminology scan: **passed**. Its nested-tree fallback test also
  passes after correcting repository-root detection in
  `scripts/terminology_lint.py`.
- Migration 0028 upgrade and downgrade SQL render offline. This is syntax/
  topology evidence, not a substitute for an isolated PostgreSQL migration run.
- Two timing-sensitive frontend assertions were given explicit test-only wait
  budgets. This changes no production behavior and keeps the complete parallel
  test run deterministic on a busy Windows host.

## Resolution of the second-eye audit

The subsequent read-only Claude audit returned **ACKNOWLEDGED WITH MINOR
FINDINGS** and reproduced the original verification results. Codex checked each
finding against the current worktree and made these narrow corrections:

- `PatientCareTimeline.tsx` now accepts the canonical fever string emitted by
  the backend (`yes`, `no`, or another retained source value), while retaining
  boolean compatibility. Missing values alone display `Not recorded`.
- `patient-care-timeline.test.tsx` now proves that a recorded fever and
  attendance value render together.
- Current method documentation now names `positive-recurrence/1.1.0`,
  `duplicate-rules/1.1.0`, and `tracker-plan/3`. Older acceptance and repair
  reports retain their historical version references under explicit historical
  checkpoint banners.
- `test_recurrence_dispatch.py` now covers historical episode dispatch, current
  positive-engine dispatch, positive-family recognition, and exact deployed-
  version matching.
- `test_terminology_lint.py` now covers a requested scan root nested inside a
  Git repository, which is the precise case fixed by repository-root detection.

After these corrections, the full backend total increased by five tests to
**1,314 passed**. The frontend remains **137 passed in 19 files**, with lint,
typecheck and production build passing. The audit's whole-cohort decryption
observation remains a performance question requiring representative national-
scale measurements; it is not an evidenced correctness defect and no speculative
cache was introduced.

## Work still requiring a real external environment

Do not relabel these as code failures or silently claim they passed:

1. PostgreSQL/PostGIS migration, role-boundary and integration tests need a
   disposable database through `MARS_TEST_DATABASE_URL`; none was available in
   the Codex environment. Never point these tests at `mars_live`.
2. The controlled browser/live-source workflow needs an authorised Ministry
   login and the owner's stable local keys. Codex did not enter credentials,
   rotate keys or fetch patient data.
3. Live source field mappings must be reverified against current Ministry
   metadata. Unmapped dose/formulation/dispensing fields must remain visibly
   unavailable.
4. A higher-level governed signal method that turns multiple quality-eligible
   patient findings into a facility cluster and then combines recurrence with
   burden, positivity, testing, commodities, completeness, time and geography
   is still separate programme-method work. The current implementation
   correctly delivers patient findings and recurrence projections; it does not
   manufacture a corroborated treatment-response signal.
5. Playwright recurrence coverage and visual checks at the named viewport sizes
   still need an isolated browser stack. Component tests and production build
   pass, but that is not browser acceptance.

## Commands for independent verification

Run from repository root. Use an isolated database only when one is supplied.

```powershell
& .\backend\.venv\Scripts\python.exe -m ruff format --check backend/src backend/tests
& .\backend\.venv\Scripts\python.exe -m ruff check backend/src backend/tests
& .\backend\.venv\Scripts\python.exe -m ruff format --check scripts\terminology_lint.py
& .\backend\.venv\Scripts\python.exe -m ruff check scripts\terminology_lint.py
& .\backend\.venv\Scripts\python.exe -m mypy --config-file backend/pyproject.toml backend/src
$temp = Join-Path (Resolve-Path .\.runtime).Path ("pytest-review-" + [guid]::NewGuid().ToString("N"))
& .\backend\.venv\Scripts\python.exe -m pytest backend/tests -m "not integration" -q --basetemp $temp
& .\backend\.venv\Scripts\python.exe scripts\terminology_lint.py
& .\backend\.venv\Scripts\python.exe scripts\export_openapi.py --check
Push-Location frontend
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run test
npm.cmd run build
Pop-Location
```

If the owner asks you to commit and push, first inspect all untracked files and
diffs, ensure no secrets/runtime artefacts are staged, show the exact proposed
file set and commit message, then commit the coherent implementation and push to
the already configured remote branch. Do not rewrite history. Report the commit
SHA and GitHub CI URL/status; do not claim success before the remote accepts it.
