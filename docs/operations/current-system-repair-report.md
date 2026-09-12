# Current-system repair report

> This remains the phase-1 repair record. For the later recurrence persistence,
> API, UI and verification work, see
> [the 12 September completion handoff](claude-recurrence-completion-handoff.md).

Prepared on 11 September 2026 for phase 1 of
[the repair-first handoff](claude-repair-first-recurrence-handoff.md). It was
checked against `main` at `1ad893c` with the local uncommitted work in place.

**Gate status: partly met.** Every application defect that could be reproduced
has been repaired and verified by automated tests. Startup, key decryption,
database roles, Ministry authentication and a live synchronization could not be
exercised in this session (section 4). The gate's startup and authentication
conditions are therefore **unverified**. They are not claimed as passed. No
startup or authentication defect was reproduced either.

## 1. Worktree inventory

At the start, HEAD was `1ad893c` ("Fix test-role authentication and stale
live-sync worker ownership"). The uncommitted work found there has been
preserved. None of it was reset, reverted or wholesale reformatted.

| File | Local change found | Status |
| --- | --- | --- |
| `backend/src/mars/services/live_sync_store.py` | Checkpoint boundary in `_resumable_checkpoint` | Preserved; extended by R1 |
| `backend/tests/unit/test_durable_live_sync.py` | Parametrised boundary test | Preserved; extended by R1 |
| `backend/tests/integration/test_identity_vault.py` | Restricted-role SQL through `quote_ident`, `quote_literal` and `exec_driver_sql` | Preserved; **unverified**, because the integration suite needs a test database |
| `scripts/start-mars-live.ps1` | Diagnostic for "could not create restricted token" | Preserved; not exercised |
| Login, session and scope code, with its tests (19 files) | **Line endings only.** `git diff --ignore-cr-at-eol` shows no content change against HEAD. | Preserved untouched; the unit and API tests pass (section 6) |

That login, session and scope code comprises:

- `api/middleware.py`;
- `domain/governance.py`, `domain/organisation.py` and `domain/security.py`;
- `integrations/dhis2/login/*`;
- `security/live_session.py`, `security/login_throttle.py` and `security/origin.py`;
- `services/auth_service.py` and `services/live_scope.py`.

## 2. Processes, ports and logs

Observed on 11 September 2026:

| Port | Expected owner | Observed |
| --- | --- | --- |
| 5460 | Project PostgreSQL (`.runtime/postgres`, database `mars_live`) | Not listening |
| 8000 | Live API | Not listening |
| 5173 | Live UI (Vite) | Not listening |
| 5432, 5433 | Not MARS | System services `postgresql-x64-16` and `postgresql-x64-18`. The launcher does not use them, and they were not touched. |

No MARS process was running, so no edit made in this session could have
hot-reloaded into the owner's browser. `MARS_TEST_DATABASE_URL` is not set.

The last API run (`.runtime/live-api-20260909-200103.*`) started normally and
answered `200` until its log ends. The project cluster's
`postgres.stderr.log` shows:

- **7 September, 23:17.** The last routine checkpoint. No shutdown record
  follows it.
- **9 September, 19:58.** "database system was not properly shut down;
  automatic recovery in progress", then "redo done" and "ready to accept
  connections".
- **9 September, 20:34:29.** `FATAL: terminating connection due to unexpected
  postmaster exit`. No shutdown request and no PostgreSQL error precede it.

Both stops ended the postmaster from outside PostgreSQL: the process tree or the
machine stopped, and the database itself did not fail. The last recovery
succeeded, and nothing in the log suggests data loss. This is not a MARS code
defect, and no change was made for it.

To avoid a crash recovery every time, stop the cluster cleanly before shutting
down or sleeping the machine. For example:
`pg_ctl stop -D .runtime\postgres\data -m fast`

The four `stale-postmaster-*.pid` files the launcher set aside were preserved.

## 3. Repairs

### R1. Checkpoint lineage depended on clock resolution (reproduced failure)

- **Reproduced.** `test_durable_live_sync.py` failed intermittently in the full
  suite.
- **Cause.** Checkpoint lineage orders the attempts for one scope and window by
  `created_at`. Two submissions within one clock tick got identical timestamps,
  and the order then fell back to the random attempt UUID.
  - About half of those ties treated an attempt from *before* a completed
    retrieval as the latest, and resumed its checkpoint.
  - That broke the invariant that a completed retrieval ends a resumable
    sequence.
- **Repair.** In `services/live_sync_store.py`, `submit()` now places a new
  attempt's `created_at` strictly after every earlier attempt for the same scope
  and window. This happens under the existing submission lock. The earlier
  boundary change in `_resumable_checkpoint` is kept.
- **Regression test.** `test_checkpoint_lineage_does_not_depend_on_clock_resolution`
  freezes the module clock and runs 20 lineage scenarios. It fails without the
  repair and passes with it.
- **Observed.** All 17 durable-sync tests pass.

### Confirmed code gaps from handoff §1.3

These gaps were found by inspecting the code, not by a failing run. Each is
closed with tests. The acceptance report lists the tests by name.

| Gap | Cause | Repair |
| --- | --- | --- |
| G1. The live calculation read only the laboratory stage, and only inside the reporting period | `run()` never retrieved the visit or medicine stages. It could not see a positive before `period_start`, so a chain crossing a month boundary was invisible. | See G1 below |
| G2. Duplicate handling was implicit | Positives were grouped with `visits.setdefault` by parent reference, or by facility plus date. The decision was not recorded and could not be reviewed. | The pure classifier `analytics/duplicate_detection.py` runs before counting, with a same-day policy. Every record stays visible. |
| G3. `interval_days` meant different things | The live path used first-to-latest; the stored path used latest-to-previous. | Additive named fields `chain_interval_days`, `adjacent_interval_days` and `anchor_interval_days`. `interval_days` keeps one documented meaning on both paths. The UI says "Qualifying interval" and "Positive-to-positive gaps". |
| G4. The stored patient list stopped at 5,000 encounter rows | `.limit(5000)` was applied before patients were grouped. | See G4 below |
| G5. The live list stopped at 25 patients (found during the repair) | `repeat_positive_patients[:25]` | The cap was removed. A test proves every qualifying patient is listed. |

**G1 repair** in `integrations/dhis2/live_dashboard.py`: retrieval plan
`tracker-plan/2`.

- It reads the laboratory, visit and medicine stages over the reporting period
  plus the lookback.
- It uses 62-day windows that overlap by one day.
- A capped window is split, at most six times.
- Checkpoints are keyed by plan version.
- A failed context stage is not checkpointed.
- A lost session stops all further reads.

**G4 repair**: `patients_of_interest` selects the candidate patients in scope,
then loads each candidate's full evidence in scope from the lookback start. It
pages over people with `offset`.

### Changes a user will notice

- Two positives one day apart no longer count as a repeat positive under the
  exploratory preset, which requires a 7-day minimum gap. The single assertion
  in `test_live_dashboard.py` that encoded the old behaviour was rewritten, with
  a comment giving the reason.
- The repeat-positive KPI is `unavailable`, not a number, when retrieval
  coverage is partial or a patient's result is indeterminate.
- The definition in force is shown above the patient list and labelled "not an
  approved programme method".

### A defect introduced during this work and fixed before hand-over

The first wiring of the shared engine made the DHIS2 adapter import
`mars.analytics`. `test_module_boundaries.py` forbids that, and the full suite
reported 1 failed and 1242 passed.

The test was not weakened. Instead:

- the source-neutral contract moved to `mars/domain/longitudinal.py`;
- the evaluation for the live snapshot moved to `services/live_recurrence.py`.

All 28 boundary tests now pass. This is a deviation from the handoff's file map,
and [the method document](../methods/configurable-recurrence.md) records it.

## 4. What could not be exercised

| Area (handoff §1.2) | Why not | What was checked instead |
| --- | --- | --- |
| Startup | The project cluster is down, and the launcher prompts for the `mars_live` role password with `Read-Host`. This session cannot answer a prompt, and passwords are not requested in chat. The owner declined starting a disposable cluster. | The logs (section 2) |
| Keys and database roles | Needs the running `mars_live` and the protected key material in `.local-secrets`, which was not touched | Nothing. The `test_identity_vault.py` change is unverified. |
| Authentication | No Ministry session | `test_live_auth_api.py`, `test_dhis2_login_adapter.py`, `test_live_session.py`; frontend `auth`, `live-login` and `session-expiry` tests |
| Sessions: cookies, logout, re-login | No Ministry session | The same unit and API tests |
| Synchronization end to end | No Ministry session | `test_durable_live_sync.py` (17 tests) and `test_live_dashboard.py` (19 tests, against a fake source) |
| Status messaging | No live snapshot | Frontend `live-jobs` and `live-snapshot` tests |
| Clinical mappings | The stage, element and option codes in `config/dhis2/pader-live-v1.json` were not rechecked against current metadata | The adapter treats an unrecognised result value as `unmapped`, never as negative |
| Indicators | No live data | Nothing |
| Maps | No live render | `geography-map` and `national-map` tests |
| Integration suite (536 tests), and fresh, upgrade and downgrade migrations | `MARS_TEST_DATABASE_URL` is unset, and no disposable database exists | Nothing. The CI failure on `1ad893c` also remains unverified locally. |

## 5. Dashboard section check

No section was rendered against live data in this session. The build compiles
every section, and the evidence below comes from the tests.

| Section | Route | Changed this session | Automated evidence |
| --- | --- | --- | --- |
| Sign-in | `/sign-in` | No | `auth`, `live-login`; the e2e test `live-login.spec` was not run |
| Overview | `/command-centre`, `/district/:unitId`, `/live/dhis2/*` | One column, now "Qualifying interval" | `overview`, `live-snapshot`, `live-jobs` |
| Signals | `/signals`, `/signals/:signalId` | No | `signal-evidence` |
| Action centre and investigations | `/action-centre` | No | No dedicated view test |
| Map and geography | `/geography` | No | `geography-map`, `national-map` |
| Patients | `/patients`, `/patients/:patientReferenceId` | Yes (see list below) | `recurrence-labels` covers the wording only; there is no render test |
| Analytics | `/analytics` | No | No dedicated view test |
| Commodities | `/commodities` | No | No dedicated view test |
| Data quality | `/data-quality` | No | No dedicated view test |
| Facilities | `/facilities`, `/live/dhis2/facility/:dhis2Uid` | No | `live-facility` |
| Reports | `/reports` | No | No dedicated view test |
| Governance, organisation, administration, status, profile | Their own routes | No | `shell`, `breadcrumbs`, `accessibility` (shell and navigation) |

The changes on the Patients section:

- the applied-definition strip;
- named interval columns;
- the "Why this patient is listed" panel;
- scope-driven copy.

No global theme token was changed. The new classes use existing tokens only.

## 6. Verification run in this session

| Gate | Result |
| --- | --- |
| `ruff format --check .` | 280 files already formatted |
| `ruff check .` | Passed |
| `mypy` | No issues in 199 source files |
| `pytest -m "not integration"` | 1243 passed, 536 deselected |
| `scripts/export_openapi.py --check` | Contract up to date |
| Frontend `lint` / `typecheck` | Clean |
| Frontend `test` | 16 files, 131 tests passed |
| Frontend `build` | Succeeded |
| `scripts/terminology_lint.py` | No prohibited resistance claims |
| Integration suite and migration tests | **Not run**: no test database |
| Playwright e2e | **Not run**: no isolated test stack |

## 7. Actual service status

Nothing is serving. `http://127.0.0.1:5173`, `http://127.0.0.1:8000` and the
database on port 5460 are not listening.

To start the stack, run `scripts\start-mars-live.ps1` from an interactive
PowerShell window, which asks for the database password. No migration was added,
so the schema head is still `0027_live_sync`. After a restart the API serves the
repaired and extended code, with the behaviour changes listed in section 3.
