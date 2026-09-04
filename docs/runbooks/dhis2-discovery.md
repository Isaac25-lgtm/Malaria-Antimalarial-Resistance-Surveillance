"""Metadata-only DHIS2 discovery.

This utility infers what an authorized DHIS2 instance can offer **without
retrieving patient collections**. Candidate mappings in its reports are
proposals, not accepted crosswalks.

It never queries tracked entities, enrollments, events, relationships or
patient-level analytics. After it finishes, stop and wait for explicit approval
before any patient synchronization.

## What it contacts

HTTPS origins on the Ministry hostname allowlist only:

- `https://hmis.health.go.ug`
- `https://eregisters.health.go.ug`

Credentials stay in the process environment. Do not put a token, password or
PAT in a file, a command argument, a URL, a log or a report.

Preferred credential: a **GET-restricted personal access token** on a dedicated
read-only service account, scoped to the minimum organisation units and
programmes required for the Pader pilot.

## PowerShell — configure, then discover

From the repository root, in a session that already has the backend virtual
environment on `PATH` or by calling the venv Python directly:

```powershell
# Values belong in the session environment, never in the command line.
$env:MARS_DHIS2_DISCOVERY_BASE_URL = "https://hmis.health.go.ug"
$env:MARS_DHIS2_DISCOVERY_TOKEN = $env:MARS_DHIS2_DISCOVERY_TOKEN
$env:MARS_DHIS2_DISCOVERY_OUTPUT_DIR = "data/discovery"
$env:MARS_DHIS2_DISCOVERY_VERIFY_TLS = "true"

# Confirm configuration without contacting DHIS2.
& ".\backend\.venv\Scripts\python.exe" -m mars.integrations.dhis2.discovery --dry-run-config

# Metadata discovery. Writes gitignored JSON and Markdown, then stops.
& ".\backend\.venv\Scripts\python.exe" -m mars.integrations.dhis2.discovery
```

If the token is held in Windows Credential Manager or a secret prompt, assign
it to `$env:MARS_DHIS2_DISCOVERY_TOKEN` interactively so it never appears in
history as a literal:

```powershell
$env:MARS_DHIS2_DISCOVERY_BASE_URL = "https://eregisters.health.go.ug"
$env:MARS_DHIS2_DISCOVERY_OUTPUT_DIR = "data/discovery"
$env:MARS_DHIS2_DISCOVERY_VERIFY_TLS = "true"

$secure = Read-Host "GET-restricted DHIS2 PAT" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $env:MARS_DHIS2_DISCOVERY_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  & ".\backend\.venv\Scripts\python.exe" -m mars.integrations.dhis2.discovery --dry-run-config
  & ".\backend\.venv\Scripts\python.exe" -m mars.integrations.dhis2.discovery
}
finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  Remove-Item Env:MARS_DHIS2_DISCOVERY_TOKEN -ErrorAction SilentlyContinue
}
```

Equivalent entry point once the backend package is installed:

```powershell
mars-dhis2-discover
```

Reports land in `data/discovery/`, which is gitignored. They name the origin
host, the service account's organisation-unit scope, programmes, metadata
definitions and a capability matrix. They do not contain credentials.

## What "supported_but_forbidden" means

A 403 on a metadata route means the account cannot see that metadata. It is not
permission to retry with a broader account, and it is not evidence that the
collection is empty.

`not_probed_to_protect_patient_data` means the route was classified from the
deny list and **no HTTP request was issued**.

## Mandatory stop

After the reports are written:

1. Do not retrieve a tracked entity.
2. Do not retrieve an enrollment.
3. Do not retrieve an event.
4. Do not retrieve a relationship.
5. Do not run patient-level analytics.
6. Do not begin patient synchronization.

A later patient sync, if approved, must use separate high-water marks for
tracked entities, enrollments, events and relationships, with `updatedAfter`,
a fixed UTC upper bound, overlap, deterministic pagination, idempotency and
tombstone handling.
