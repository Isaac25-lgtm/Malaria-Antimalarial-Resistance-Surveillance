# DHIS2 exchange

MARS can read organisation-unit metadata and reported aggregate values from a
DHIS2 instance. It is **disabled and unconfigured by default**, and a deployment
that never talks to DHIS2 runs unchanged.

## The boundary

```
mars.integrations.ports          MARS's own vocabulary. No system named.
        ^                        RemoteOrganisationUnit, RemoteDataValue, RemotePage, RemoteScope
        |  implements
mars.integrations.dhis2.client   The only module that knows DHIS2 exists.
        |
mars.integrations.dhis2.service  Run bookkeeping, paging, where data goes.
        |
mars.ingestion.aggregate         The canonical Prompt 11 pipeline. One writer.
```

ADR 0003: domain, services, analytics, signals and the API never import the
adapter. `tests/unit/test_module_boundaries.py::TestTheDhis2AdapterIsALeaf`
enforces it, including that the ports module never names DHIS2 or `httpx` — a
port that mentions the system is the adapter with a different filename.

## Credentials

Supplied through the environment only, held as `SecretStr`, and never written
to a log, an exception, a run record, an API response or a `repr`:

```
MARS_DHIS2_ENABLED=true
MARS_DHIS2_BASE_URL=https://dhis2.example.org
MARS_DHIS2_TOKEN=...            # preferred: scopeable and revocable
MARS_DHIS2_USERNAME=...         # or basic auth
MARS_DHIS2_PASSWORD=...
```

A base URL is stripped of any userinfo before it is stored or reported —
`https://admin:secret@host` is a valid URL and a password in plain sight.

`MARS_DHIS2_VERIFY_TLS` defaults to true and is separately settable, so a
deployment that disables it has to say so in writing where a reviewer can see
it.

## Transport behaviour

| Concern | Behaviour |
| --- | --- |
| Timeouts | `MARS_DHIS2_TIMEOUT_SECONDS`, default 30 |
| Retries | `MARS_DHIS2_MAX_RETRIES`, default 3, linear backoff |
| What is retried | Timeout, transport, 429, 5xx — the failures that can succeed later |
| What is **not** retried | 401, 403, 404 — retrying an authorisation failure locks the account out faster |
| Pagination | `MARS_DHIS2_PAGE_SIZE`, default 500 |
| Response cap | `MARS_DHIS2_MAX_RESPONSE_BYTES`, default 64 MiB, enforced **while streaming** |

Errors carry a category, because an operator's next action after a 401 is
nothing like their next action after a 503. `error_summary` is a sentence MARS
composed; the remote body is never repeated back, since a DHIS2 error can quote
the request that produced it and that request carries an `Authorization` header.

## Identifiers

**A DHIS2 UID is never a MARS key.** UIDs live in the existing crosswalks:

- `geography_unit_alias` — `source_system = 'dhis2'`, `match_status = 'confirmed'`
- `facility_identifier` — `source_system = 'dhis2'`

Only mappings a person has **accepted** count. A `proposed` alias is a question
that has not been answered.

An unresolved UID becomes a row in `integration_mapping_proposal`, upserted with
an occurrence count. **Nothing is matched by name similarity.** Two Ugandan
districts with similar names are exactly the case a fuzzy match gets wrong, and
the failure is invisible: the figures still load, under the wrong district, and
look entirely plausible.

Values whose organisation unit does not resolve are **rejected and counted**,
never attached to a nearby facility.

## Runs

`integration_run` records one exchange with one scope:

- `scope_fingerprint` — SHA-256 over the sorted request. The same scope is the
  same run, which is what makes a scheduled daily pull idempotent.
- `attempt` — a retry after partial failure is a different run; it read
  different pages.
- `cursor`, `pages_fetched` — written as the run progresses, so `--resume`
  continues instead of starting again.
- `payload_checksum` — identical bytes give an identical checksum; changed bytes
  cannot silently keep the previous meaning.

`partial` is a first-class outcome. A pull that read eleven of fourteen pages
genuinely fetched eleven, and resuming from twelve is cheaper and more honest
than discarding them. A run is *resumable* when it is `running` or `partial`
**and** has a cursor — terminal-for-reporting and resumable-for-continuation are
different questions.

## Where aggregate data goes

Through the ordinary Prompt 11 aggregate pipeline. DHIS2 content meets the same
validation, the same revision rules and the same blank-versus-zero handling as a
transcribed paper form. A parallel path would drift, and the first sign would be
two different numbers for one month.

Blank survives the exchange as blank: DHIS2 sends every value as a string
including the empty one, and the adapter keeps it a string so the canonical
validator — which knows that blank is not zero — makes the decision.

## Outbound writes

`MARS_DHIS2_PUSH_ENABLED` is **false** and independent of `MARS_DHIS2_ENABLED`.
Reading another system's data and writing into it are different authorities, and
MARS must not acquire the second by being granted the first. No automatic push
of derived indicators is implemented; a destination dataset would have to be
configured and approved first.

## Commands

```
mars-dhis2 status
mars-dhis2 sync-metadata --dry-run
mars-dhis2 sync-metadata
mars-dhis2 pull-aggregate --org-unit <UID> --from 2026-03-01 --to 2026-03-31
mars-dhis2 proposals
```

Exit codes: `0` complete and fully resolved; `1` unresolved mappings — a
configuration gap, not a system failure; `2` usage; `3` the exchange failed;
`4` DHIS2 is not configured or is disabled.

`pull-aggregate` refuses a request with no organisation unit or no period: MARS
will not pull a whole DHIS2 instance implicitly, and an unbounded request cannot
be resumed or fingerprinted.

## API

`GET /api/v1/integrations/{system}/status`, `/runs`, `/runs/{id}`,
`/mapping-proposals`. All require `integration:manage`. Read-only: starting an
exchange is a CLI or worker action, because a pull can run for minutes and must
survive a client disconnecting.

## Not implemented, and why

- **Event/tracker exchange.** `EventPort` is declared and unimplemented. No
  tracker source has been supplied, so an implementation would be a guess at
  fields nobody has seen. The port exists so later work has a named seam rather
  than a reason to widen the aggregate one.
- **Analytics pull.** `AnalyticsPort` is declared. DHIS2 analytics output is
  that system's *derived* figure computed by its rules; MARS keeps derived
  figures apart from reported ones, and wiring it in needs a decision about
  which lane it belongs to.
- **Production UIDs, datasets, data elements and category-option combos.** None
  are invented. Until a deployment supplies them they are configuration gaps,
  visible as mapping proposals.

## Metadata-only discovery

A separate GET-only utility can inspect system info, current-user scope and
metadata definitions without retrieving patient collections. See
[dhis2-discovery.md](../runbooks/dhis2-discovery.md). After it writes reports,
stop for the [pre-patient approval checklist](../runbooks/pre-patient-approval.md).

## What is still required from the programme

| Input | Needed for |
| --- | --- |
| DHIS2 base URL and credentials | Any exchange at all |
| Accepted organisation-unit UID mappings | Resolving figures to MARS geography and facilities |
| Dataset / data-element UIDs | Scoping an aggregate pull |
| Category-option-combo mapping | Disaggregated values |
| An approved destination dataset | Any outbound write |
