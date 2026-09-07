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
