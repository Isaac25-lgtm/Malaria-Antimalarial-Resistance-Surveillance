# Live synchronization and snapshot recovery

The live pilot accepts retrieval jobs at `POST /api/v1/live/dashboard/jobs` and
returns HTTP 202. The browser polls `/api/v1/live/dashboard/jobs/latest` for the
selected period and reads `/api/v1/live/dashboard` for published figures.
Overview, operational pages, facilities, and patient surveillance share this
snapshot and reporting period. The synchronous endpoint remains available for
older clients, but new clients should use jobs.

The command-centre and Map Explorer render the imported district/subcounty
GeoJSON. A live area total is attached only through verified DHIS2 hierarchy
metadata; the UI never estimates missing facility coordinates or assigns a
facility to the nearest polygon.

## Storage and publication

Migration `0027_live_sync` adds `mars_analytics.live_sync_job`. Each job contains
an encrypted checkpoint and, once validated, an encrypted snapshot. AES-GCM
uses a key derived with a dedicated context from `MARS_IDENTITY_ENCRYPTION_KEY`.
Associated data binds ciphertext to its scope, job and payload purpose. Raw
cookies, login passwords, tokens and direct patient attributes are not stored.
Checkpoint event references are encrypted and are never returned in job status.

Access requires a current authenticated session, case-evidence permission,
the pseudonymous sensitivity tier and newly discovered facility scope. Storage
keys include the source, subject, permissions, facility metadata, mapping digest
and key versions. A changed scope cannot read the old scope's snapshot. After a
restart, sign in and discover metadata again to access matching saved snapshots.

A failed refresh does not delete existing figures. A partial refresh cannot
replace a complete snapshot for the same reporting period. If no complete
snapshot exists, the latest partial snapshot is displayed with coverage warnings.
Reporting coverage counts facilities supplying data; retrieval coverage counts
successful facility requests, including successful requests with no events.
Neither is automatically an official reporting-completeness indicator.

## Execution and recovery

The password pilot runs at most two retrieval jobs concurrently in background
API threads and admits at most four running-or-queued jobs per API process.
Excess submissions receive a controlled busy response and remain resumable.
Browser navigation and disconnection do not own those jobs.
PostgreSQL advisory locks deduplicate submissions for the same owner/scope/period.
Leases fence abandoned workers so they cannot publish after another worker resumes
their job. Progress polling reads metadata without decrypting patient checkpoints.

Completed aggregate, historical and individual-facility reads are checkpointed.
Incomplete pages within a failed facility are discarded; a retry restarts that
facility and keeps already completed facilities. Transient Tracker transport,
timeout, rate-limit and server errors receive at most two retries. Authentication
and validation errors are not retried by that mechanism.

The lease expires after five minutes without progress. The job then reports
`interrupted`. Once a user signs in and rediscovers the same scope, Overview can
resume the saved work. A process restart still ends live login sessions because
upstream credentials are memory-only. This is not an unattended multi-process
worker deployment; that requires a dedicated credential and shared session design.

## Local rollout

Run the existing launcher as the Windows user owning the protected keys:

```powershell
& ".\scripts\start-mars-live.ps1" -Restart
```

The launcher checks the project-local PostgreSQL cluster on port `5460` before
requesting database credentials. If the cluster is stopped, it restarts the
existing `.runtime/postgres/data` cluster with its matching PostgreSQL binaries.
A stale `postmaster.pid` is archived only after verifying that its recorded
process no longer exists. The launcher never auto-starts a remote database.

If the launcher reports that its local DPAPI key set cannot be decrypted, the
current Windows identity differs from the identity that protected those files
(or that Windows DPAPI profile is no longer usable). Recover once with:

```powershell
& ".\scripts\start-mars-live.ps1" -Restart -InitializeNewLocalKeys
```

The launcher archives all existing key blobs before creating a consistent new
set. Local-pilot keys use Windows machine-scoped DPAPI and file ACLs restricted
to the creating Windows account, Local System and local administrators. This
avoids PowerShell-host identity incompatibilities while keeping the encrypted
files unavailable to other ordinary local users. Recovery intentionally starts
a new encrypted snapshot scope and changes MARS patient aliases; it does not
modify the archived keys or fall back to synthetic data. Subsequent starts must
return to the normal `-Restart` command.

It applies migration 0027 before replacing listeners. Preserve the existing
DPAPI keys; generating replacements makes old encrypted snapshots unreadable.
After restarting, sign in, allow metadata discovery, then synchronize. API
health alone verifies service readiness, not successful Ministry retrieval.

## Verification

The regression suite exercises snapshot recovery, scope and period isolation,
authenticated encryption, duplicate submission, stale-lease recovery, publication
fencing, logout during retrieval, snapshot preservation, and browser navigation.
Unit tests use controlled fixtures; no test needs Ministry patient data.

## Tracker retrieval plan `tracker-plan/3`

Each authorised facility is read for three Tracker stages: laboratory, medical
visit and medicines. The visit and medicine stages are only read when the
approved mapping names them and the parent-event element. The date extent is
the reporting period plus the recurrence lookback: `period_start` minus the
snapshot definition's `maximum_window_days`. With the exploratory preset
(28 days) a calendar month needs at most 59 days, which is one bounded request
window.

- **Bounded windows.** Requests never exceed the client's 62-day window. Longer
  extents are split into windows that overlap by one day. The duplicated day's
  events are collapsed by source-revision classification, so an
  inclusive-or-exclusive source boundary cannot drop a day.
- **Capped windows are split, not truncated.** A `RESPONSE_TOO_LARGE` window is
  halved and retried, up to six times. A single day that still exceeds the cap
  fails the facility, which makes coverage partial.
- **Versioned checkpoints.** A facility's three stages are checkpointed together
  under a key built from:
  - the plan version;
  - the mapping version;
  - the stage identifiers;
  - the date extent.

  A checkpoint written under the former laboratory-only plan (`tracker:<facility>`)
  is never read. An old plan cannot satisfy a new one.
- **Context failure is not "no treatment".** When the laboratory stage succeeds
  but a visit or medicine stage fails:
  - the facility is not checkpointed, so a retry reads it again;
  - the snapshot carries a warning and is marked `partial`;
  - treatment evidence at that facility is reported as `not_returned`.
- **A lost session stops reading.** Cancellation and session loss end every
  further Tracker request immediately. They are never recorded as a facility
  failure while the loop moves on to the next facility.
- **Attempt order is strict.** Checkpoint lineage depends on attempt order, so a
  new attempt's `created_at` is placed strictly after every earlier attempt for
  the same scope and window, under the submission lock. Two submissions within
  one clock tick previously fell through to a random UUID order. That
  occasionally recovered a checkpoint from before a completed retrieval; the
  repair report records the reproduction and the fix.

### Recurrence fields in the snapshot

The snapshot evaluates repeat positives with the shared engine and reports these
fields:

| Field | Meaning |
| --- | --- |
| `repeat_positive_definition` | The definition used, marked `exploratory` while no approved programme method is in force |
| `repeat_positive_coverage` | `complete` only when retrieval succeeded for every facility and reached the lookback start |
| `repeat_positive_coverage_notes` | Why coverage is partial, in plain words |
| `repeat_positive_denominator` | Distinct linked patients with an eligible positive in the period |
| `repeat_positive_indeterminate` | Patients whose absence of a chain is not a reliable no |
| `possible_duplicate_positive_groups` | Same-day groups that involve a positive; the records stay visible |
| `tracker_lookback_start` | Where the retrieved evidence begins |

The repeat-positive KPI is published only when coverage is complete and no
denominator patient is indeterminate. Otherwise it is `unavailable`, never zero.
`repeat_positive_patients` lists every qualifying patient. The former 25-row cap
made the overview's count understate any larger total.
