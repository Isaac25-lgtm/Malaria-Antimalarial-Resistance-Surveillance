# Independent audit of the repair-first recurrence handoff

> **Audit-history notice:** this document records the defects as independently
> reproduced before the completion pass. They are not the current status.
> Their code-level resolutions and current verification evidence are mapped in
> [the 12 September completion handoff](claude-recurrence-completion-handoff.md).
> The original findings remain below so the engineering history is auditable.

Audit date: 11 September 2026.

## Verdict

The handoff was partially executed. There is substantial new backend calculation and adapter code, real regression coverage, and limited integration into existing patient views. It is not a completed configurable surveillance dashboard, and the repair-first live verification gate has not been demonstrated.

Claude's report is generally candid about what is missing. Its phrase "half the enhancement phase is done" is not supported by a defined completion measure. Several entries labeled Done also overstate correctness: independent edge-case probes found defects despite the existing tests passing.

No application source was changed in this audit. No services were started/stopped, no migrations were applied, no Ministry records were retrieved, and no Git commits or pushes were made. Automated tests, a frontend build, local connection checks and in-memory probes using invented test fixtures were performed. This audit document is the only new deliverable source file.

## 1. Evidence inspected

- The original enhancement brief and `claude-repair-first-recurrence-handoff.md`.
- Claude's pasted final report, `current-system-repair-report.md`, `recurrence-implementation-acceptance-report.md` and the method documentation.
- Current working-tree differences, including line-ending-insensitive diffs. HEAD remains `1ad893c`; the enhancement work is uncommitted, with new modules untracked.
- Canonical contract, both source adapters, duplicate/recurrence/projection engines, live runner, synchronization store, patient service/routes/schemas and frontend patient/overview integration.
- Existing governed episode/recurrence code, API registration, new-module presence and frontend recurrence directory.
- The current nonintegration backend suite, frontend tests, lint/type/build gates and OpenAPI check.

## 2. Execution by phase

| Handoff phase | Audit result |
| --- | --- |
| Preserve local fixes and design | Existing substantive local fixes remain. No global design-token rewrite was found. The contract's move into `domain/longitudinal.py` is a reasonable module-boundary adaptation. |
| Reproduce and repair current problems first | A specific checkpoint ordering repair exists and has a frozen-clock regression test. However, live startup, keys, role boundaries, Ministry authentication, live synchronization and all-section browser verification remain untested. The full gate is not met. |
| Canonical evidence and shared recurrence engine | Substantially built and wired into live and stored patient summaries, but correctness defects remain; see section 4. |
| Bounded three-stage retrieval and lookback | Implemented against mocked sources. Real metadata/source behavior was not verified. Context-failure completion and cancellation handling still have defects. |
| Encrypted canonical evidence datasets, saved definitions, immutable runs | Not implemented. Existing encrypted dashboard snapshots must not be confused with the new canonical dataset/run model. |
| New recurrence API and governance operations | Not implemented. Existing patient schemas/routes were extended, but there is no requested run/definition/projection endpoint family. |
| Interactive dashboard additions | Limited to labels, interval columns, preset disclosure and a live-patient explanation panel. No editable definition/Apply workflow, treatment timeline, duplicate review, matrix or comparison screen. |
| Aggregate-context signals and patient-origin investigations | Not implemented. Existing aggregate behavior is retained, but the new linkage into signals/review is absent. |
| Integration, migration, browser/visual and controlled live cutover | Not completed. |

### Local services

During this audit no listeners were returned for the expected MARS ports, and TCP probes could not connect to `127.0.0.1:5173`, `:8000` or `:5460`. This supports Claude's report that the local stack was down. It does not establish the cause of the downtime.

The report's stronger explanation that historical database stops were definitely caused externally is not independently confirmed here. An interrupted database and successful crash recovery do not by themselves identify the process, operating-system event or initiating cause. No data-loss conclusion is made by this audit.

## 3. What is genuinely present

- `backend/src/mars/domain/longitudinal.py`: typed canonical evidence, definitions, queries, coverage and findings.
- `backend/src/mars/analytics/duplicate_detection.py`: separate same-day/near-duplicate classification and representative selection.
- `backend/src/mars/analytics/positive_recurrence.py`: positive-only sequence evaluation with inclusive minimum gaps, anchored maximum windows, N thresholds, later anchors and explanations.
- `backend/src/mars/analytics/recurrence_views.py`: pure summary, interval, facility, trend, treatment and comparison helpers. These are library functions, not working dashboard panels.
- `backend/src/mars/integrations/dhis2/tracker/clinical_adapter.py` and `backend/src/mars/services/stored_encounter_adapter.py`: source adapters.
- `backend/src/mars/services/live_recurrence.py`: shared-engine integration and live patient-summary serialization.
- `backend/src/mars/integrations/dhis2/live_dashboard.py`: laboratory/visit/medicine retrieval, lookback and capped-window splitting.
- `backend/src/mars/services/live_sync_store.py:158`: strictly increasing attempt timestamps under the submission lock, preserving prior checkpoint/fencing work.
- `backend/src/mars/services/patient_surveillance.py`: encounter truncation removed from the query, shared evaluation before offset pagination.
- Additive interval/definition/coverage response fields and corresponding generated types.
- `frontend/src/features/recurrence/recurrenceLabels.ts`: the only file currently in the new frontend recurrence directory.
- Existing patient view changes and modest scoped CSS additions.

## 4. Independent defects and incomplete repairs

These observations are independent of Claude's own success claims. The probes used invented test evidence, without accessing live patients or any database.

### A. High: a positive interval can use the visit date instead of the positive-test date

Location: `backend/src/mars/integrations/dhis2/tracker/clinical_adapter.py:321`.

When the clinical parent is retrieved, `_build()` assigns the parent's visit date to the canonical encounter. Individual `ClinicalTest` objects do not preserve their own event dates. The resulting recurrence calculation can therefore use attendance dates rather than the requested positive-to-positive dates.

Reproduction:

- Laboratory positives: 3 August and 12 August, nine days apart.
- Their parent visits: 3 August and 8 August.
- Adapter output: 3 August and 8 August.
- Evaluation with the 7-day minimum: `does_not_qualify`, with a five-day interval.

Expected: preserve the actual laboratory dates and calculate nine days, or explicitly classify unresolved temporal linkage without substituting visit dates. A date-quality flag alone does not correct the wrong interval. The adapter also assigns day precision to every event, so the engine's timestamp tests do not prove preservation of source timestamp semantics through this adapter.

### B. High at the shared-engine boundary: source namespaces are not part of patient grouping

Locations: `backend/src/mars/analytics/positive_recurrence.py:81` and `backend/src/mars/analytics/duplicate_detection.py`.

The contract says a patient key is unique within its namespace, but recurrence groups by `patient_key` alone. Same-day grouping also omits namespace.

Reproduction: two encounters with the same patient key, different source namespaces and dates nine days apart yielded one qualifying patient.

Expected: composite source namespace plus patient reference, or explicit rejection of mixed-namespace input. This is a demonstrated library defect, not evidence that actual Ministry patients have already been merged. Current adapters usually supply one source at a time; the risk becomes especially important when unifying datasets or adding comparisons.

### C. High: the claim that lost sessions stop all further reads is too broad

Locations: `backend/src/mars/integrations/dhis2/live_dashboard.py:132`, `:141`, `:172`, `:179`.

Tracker handling propagates session-loss errors, but the aggregate and historical HMIS blocks catch broad exceptions. Their checkpoint checks occur after obtaining a remote page. A session-loss exception can be converted into a warning, after which the next HMIS request is still attempted.

Mock reproduction: when the first post-read checkpoint check raised `session_required`, two aggregate reads occurred rather than stopping after the first.

Expected: check before each remote request/page and propagate cancellation/session/lease loss through every source branch. Never treat authorization cancellation as an ordinary source outage.

### D. Medium: configured duplicate-quality exclusions do not exclude newly classified duplicates

Locations: `backend/src/mars/analytics/positive_recurrence.py:154` and `:172`.

Eligibility checks the encounter's existing quality flags before the new duplicate classification is attached at patient level.

Reproduction: positives on days 0, 0 and 9 with `quality_exclusions={POSSIBLE_DUPLICATE}` still returned `qualifies`, and the resulting finding carried `POSSIBLE_DUPLICATE`.

Expected: duplicate decisions must feed the eligibility step according to documented exclusion semantics. Currently the new quality configuration cannot reliably enforce that policy. The future UI/API must not expose this as working until corrected.

### E. Medium: failed medicine retrieval can still mark the retrieval complete

Locations: `backend/src/mars/integrations/dhis2/live_dashboard.py:291`, `backend/src/mars/services/live_recurrence.py:59`, `backend/src/mars/services/live_sync_store.py:272`.

Mock reproduction of a failed medicine stage produced:

```text
status = partial
retrieval_complete = True
repeat_positive_coverage = complete
```

Laboratory-only recurrence coverage can legitimately be complete while treatment coverage is partial. However, the overall `retrieval_complete` marker excludes context failures, and the durable store uses that marker to clear checkpoints. This loses successful resumable work even though part of the requested three-stage retrieval failed.

Expected: separate laboratory analytical coverage from visit/medicine and overall retrieval completion; preserve appropriate resumable progress until the requested retrieval plan is settled.

### F. Medium: comparison checks coverage, not identity of the underlying evidence

Location: `backend/src/mars/analytics/recurrence_views.py:350`.

`compare()` checks equality of coverage metadata and reporting query. It has no immutable dataset ID/hash to check.

Reproduction: two completely different patient datasets with the same coverage descriptor were accepted, producing A-only/B-only results.

Expected: require common retained evidence identity/version, not merely matching dates/facilities. Claude's claim that the comparison refuses different evidence is broader than the implemented check. The helper is not yet exposed through a UI/API.

### G. Medium: partial coverage can still receive a definite patient-level negative determination

Location: `backend/src/mars/analytics/positive_recurrence.py:337`.

The evaluation marks overall coverage partial when evidence ends before the reporting period, but `_absence_unreliable()` does not check that end boundary.

Reproduction: one positive, sufficient historical start, but evidence ending on the first reporting day returned overall `partial` and patient-level `does_not_qualify`.

Expected: an incomplete observation window should not support a reliable absence finding. In addition, the stored adapter service constructs `EvidenceCoverage(..., end, True)` from a broad earliest-encounter date, which does not prove complete ingestion for every scoped facility through the end date. Stored coverage needs retrieval/import provenance, not assumptions from a minimum date.

### H. Medium: stored patient pagination and date filtering are still incomplete in the UI

Location: `frontend/src/features/patients/PatientSurveillanceView.tsx:41`.

The backend now accepts `offset`, but the stored view requests only `limit: 100`; it does not pass offset or the displayed reporting period, and its query key contains neither. Therefore removing the backend 5,000-row cap does not make every patient accessible through the current stored UI. The period control can change visually without changing that request.

The live list can page over the returned array, but the complete patient arrays are still embedded in snapshots. The planned server-side paginated run API and independently computed totals remain absent. Loading all evidence on every stored list request also needs resource/performance verification.

### I. Architectural completion gap: the two paths are only partly unified

Live and stored patient summaries now call the shared evaluator. This is genuine progress.

However, `analytics/episodes.py:372`, `analytics/recurrence.py:287` and their workers still use the existing governed episode/recurrence path; there is no new-method dispatch into the shared engine. Preserving historical semantics was correct, but it does not complete the requested version-aware unification. Do not describe all MARS recurrence outputs as already governed by one calculation.

## 5. Requirement-by-requirement extent

Here, **implemented core** means the core behavior is substantially present; it does not mean a complete user-facing feature or live validation. **Partial** means important pieces exist but defects or missing integration prevent accepting the whole requirement.

| # | Requirement | Independent status |
| --- | --- | --- |
| 1 | Count positive encounters, not attendance alone | Implemented core; date provenance still needs A above |
| 2 | Same-day records retained and selectable policy | Partial: engine policies exist; no controls/review screen |
| 3 | Editable minimum gap | Partial: typed parameter only |
| 4 | Editable maximum window | Partial: typed parameter only |
| 5 | Dashboard definition controls and Apply | Not implemented |
| 6 | Editable N positives | Partial: engine parameter only |
| 7 | Independent duplicate classifier | Implemented core; eligibility integration defect D remains |
| 8 | Full qualifying-patient drilldown | Partial: live test timeline/explanation, not full care detail |
| 9 | Detailed treatment history | Partial: some canonical fields, not displayed; source fields unverified |
| 10 | Avoid unsupported resistance claims | Implemented core wording/terminology boundary |
| 11 | Whole care pathway | Partial: some canonical context; no complete care timeline |
| 12 | Safe cross-facility patient journeys | Partial: single-source cases tested; namespace defect B |
| 13 | Multiple analytical result views | Partial: library functions, largely not exposed |
| 14 | Configurable interval distribution | Partial: library only |
| 15 | Inspect duplicate records | Partial: internal groups; no review endpoint/table |
| 16 | Programme and exploratory modes | Not implemented as selectable governed modes; preset disclosure only |
| 17 | Versioned saved definitions | Not implemented |
| 18 | A/B comparison mode | Partial: helper exists, lacks dataset identity and user workflow |
| 19 | Aggregate flags plus recurrence context | Partial: aggregate assembly retained, new cross-reference missing |
| 20 | Quality-driven higher-level eligibility | Partial: flags exist, defect D and no signal integration |
| 21 | Case vs cluster vs corroborated signal | Not implemented |
| 22 | Explainability panels | Partial: live patient explanation only, no complete signal/treatment explanation |
| 23 | Overview-to-case-to-investigation navigation | Partial: patient links exist; complete chain absent |
| 24 | Full analytical filters | Not implemented; limited existing period/repeat-only UI is not the requested filter set |
| 25 | Exact positive-to-positive intervals | Partial: pure-engine tests pass, live adapter violates date contract A |
| 26 | Preserve complete positive sequences | Implemented core canonical representation; exposure still limited |
| 27 | Treatment linked to encounters | Partial: adapter linkage exists, not displayed or live-verified |
| 28 | Treatment-response matrix | Partial: library only |
| 29 | All thresholds configurable | Partial: fields exist; fixed deployed preset, missing controls/settings enforcement |
| 30 | Dynamic unified surveillance query workflow | Partial: foundational engine, not end-to-end capability |

Claude's own table totals **6 Done, 19 Partial, 5 Not done**. This audit downgrades requirements 12 and 25 based on reproduced defects: **4 implemented-core, 21 partial, 5 not implemented**. These are requirement counts, not percentages of engineering effort. They do not support a precise overall "50% complete" estimate, and they certainly do not establish that half of the requested dashboard workflow is usable.

## 6. Independent verification results

| Check | Observed result |
| --- | --- |
| Backend Ruff lint | Passed |
| Backend Ruff format | 280 files already formatted |
| Backend mypy | Passed, 199 source files |
| Backend nonintegration suite | 1,242 passed in a workspace-temp run; one repository-terminology test was contaminated by that run's generated test fixture. After moving the fixture directory into ignored audit storage and rerunning terminology tests with a fresh temp directory outside the repository, all 7 terminology tests passed. Thus all 1,243 selected tests passed across the full run and affected-test rerun, not in one uninterrupted clean run. |
| Initial backend test attempt | 1,129 passed and 114 setup errors caused by denied access to the default Windows pytest temp folder. These were environmental setup errors, not 114 demonstrated application defects. |
| Frontend tests | 131 passed across 16 files |
| Frontend lint | Passed |
| Frontend TypeScript/build | Passed through `npm run build`; Vite warned about a large map bundle |
| OpenAPI contract | Current |
| Repository terminology lint | Passed after isolating generated fixtures |
| Independent edge-case probes | Reproduced A-G above; existing tests do not catch them |
| Integration tests | 536 deselected, not executed |
| Live role/migration/Ministry/browser/visual validation | Not executed; no running MARS stack or authenticated live workflow |

The frontend build generated normal build artifacts; pytest generated temporary fixtures. The workspace-local fixture directory was moved into ignored `.runtime` audit storage rather than deleted. No patient data or keys were involved.

## 7. Recommended next execution order

1. Correct date provenance, namespace handling, cancellation, duplicate exclusions, coverage/completion and comparison identity. Add the independent reproductions above as regression tests.
2. Establish a safe test database/isolated browser stack and complete the outstanding repair gate, including scoped login, real synchronization and section-level status checks. Do not bypass the owner's environment decisions or rotate keys to do this.
3. Add encrypted canonical evidence persistence, immutable definitions/runs and permissioned recurrence APIs, preserving previous encrypted snapshot/fencing behavior.
4. Implement editable definition controls, actual filters/pagination, full care/treatment timeline, duplicate review, charts, treatment matrix and comparison.
5. Add version-aware governed analytics dispatch, aggregate context and patient-origin investigations.
6. Run integration/migration/security/browser/visual regressions, then a controlled local cutover with actual live verification.

Do not restart the application and describe the feature as finished merely because the current tests pass. The tested foundation is valuable, but the required user workflow and several correctness guarantees are still unfinished.
