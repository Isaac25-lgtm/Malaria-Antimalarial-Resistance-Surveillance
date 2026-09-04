# Permission and sensitivity matrix

Blueprint 057: *every endpoint declares permission scope and geography scope.
Server-side enforcement is mandatory; hiding a button is not access control.*

This table is generated from the live route table. The machine-checked half
lives in `backend/tests/api/test_security_matrix.py`, which walks the same
routes and fails when an endpoint appears without a permission — so a new
endpoint written without one fails on the day it is written, not on the day
someone notices.

## Sensitivity tiers

| Tier | Means |
| --- | --- |
| `aggregate` | Counts, rates and signals over administrative areas. No individual. |
| `pseudonymous_case` | Event sequences keyed by a MARS patient number. No direct identifier. |
| `identified` | The identity vault. No analytical endpoint carries this tier. |

## The matrix

| Method | Path | Permissions | Sensitivity |
| --- | --- | --- | --- |
| `GET` | `/api/v1/analytics/commodity-alerts` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/analytics/episodes` | case:view_pseudonymous_evidence | **pseudonymous_case** |
| `GET` | `/api/v1/analytics/results/{kind}` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/analytics/spatial/cells` | surveillance:view_aggregate | aggregate |
| `POST` | `/api/v1/auth/logout` | (open) | — |
| `GET` | `/api/v1/auth/me` | (open) | — |
| `GET` | `/api/v1/facilities` | facility:view | aggregate |
| `GET` | `/api/v1/facilities/{facility_id}` | facility:view | aggregate |
| `GET` | `/api/v1/geography/*` | geography:view | aggregate |
| `GET` | `/api/v1/governance/configuration-keys` | configuration:view | aggregate |
| `GET` | `/api/v1/governance/methods` | method:view | aggregate |
| `GET` | `/api/v1/health/live` | (open) | — |
| `GET` | `/api/v1/health/ready` | (open) | — |
| `GET` | `/api/v1/health/schema` | (open) | — |
| `GET` | `/api/v1/indicators/definitions` | method:view | aggregate |
| `GET` | `/api/v1/indicators/definitions/{code}` | method:view | aggregate |
| `GET` | `/api/v1/indicators/summary` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/integrations/{system}/*` | integration:manage | aggregate |
| `POST` | `/api/v1/investigations` | investigation:triage | aggregate |
| `GET` | `/api/v1/investigations/queues` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/investigations/queues/{name}` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/investigations/{id}` | surveillance:view_aggregate | aggregate |
| `POST` | `/api/v1/investigations/{id}/triage` | investigation:triage | aggregate |
| `POST` | `/api/v1/investigations/{id}/assign` | investigation:assign | aggregate |
| `POST` | `/api/v1/investigations/{id}/start` | investigation:update | aggregate |
| `POST` | `/api/v1/investigations/{id}/notes` | investigation:update | aggregate |
| `POST` | `/api/v1/investigations/{id}/evidence-requests` | investigation:update | aggregate |
| `POST` | `/api/v1/investigations/{id}/evidence-requests/{rid}/result` | investigation:update | aggregate |
| `POST` | `/api/v1/investigations/{id}/close` | investigation:close | aggregate |
| `POST` | `/api/v1/investigations/{id}/escalate` | investigation:close | aggregate |
| `GET` | `/api/v1/meta/assistant` | (open) | — |
| `GET` | `/api/v1/meta/evidence-lanes` | (open) | — |
| `GET` | `/api/v1/meta/permissions` | (open) | — |
| `GET` | `/api/v1/meta/version` | (open) | — |
| `GET` | `/api/v1/organisation-units` | organisation:view | aggregate |
| `GET` | `/api/v1/organisation-units/{unit_id}` | organisation:view | aggregate |
| `GET` | `/api/v1/reports/{product}` | report:generate **+** surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/reports/{product}/export.csv` | data:export **+** surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/signals` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/signals/{id}` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/signals/{id}/explanation` | surveillance:view_aggregate | aggregate |
| `GET` | `/api/v1/surveillance/*` | surveillance:view_aggregate | aggregate |
| `POST` | `/api/v1/ai/ask` † | surveillance:view_aggregate | aggregate |

† Registered only when `ai_assistant_enabled` is true. See
[ask-mars.md](../architecture/ask-mars.md).

## The open endpoints, and why

Only eleven routes are reachable without a permission, and each has a recorded
reason in the test file. A test asserts that none of them contains
`/surveillance/`, `/analytics/`, `/signals`, `/investigations` or `/reports/`:
a health probe may be open, a district's figures may not.

| Route | Why |
| --- | --- |
| `/health/live`, `/health/ready` | Probes. Report reachability, not data. |
| `/health/schema` | Migration head only. |
| `/meta/version`, `/meta/permissions`, `/meta/evidence-lanes` | Build and vocabulary. |
| `/meta/assistant` | Whether the optional assistant is on. Reads only the flag. |
| `/auth/me`, `/auth/logout` | The caller's own identity and session. |
| `/auth/dev/*` | Development authentication; refused in protected environments. |

A test asserts that none of these responses contains `password`, `secret`, a
connection string or a host.

## Write permissions are not shared

Triage, assignment, updating and closure are separate permissions. One blanket
`investigation:write` would let whoever can add a note also close the case, and
a closure is the record a programme decision rests on.

Report generation and export are likewise separate: reading a figure on screen
and carrying it out of the system in a file are different acts with different
risks.

## Scope enforcement

Applied in SQL by the services, never by filtering results afterwards.

* **Facility accounts** read their own facilities. A facility's district
  membership does not grant the district-wide surveillance picture — established
  in commit `64e3e21` and enforced across the indicator summary, analytical
  results, hotspots, clusters, episodes, signals, investigations and map cells.
* **District accounts** read their scope subtree, resolved by materialised path
  prefix.
* **Unscoped accounts** read nothing. An empty scope is the misconfiguration
  most likely to exist in a real deployment.

**Out-of-scope reads return 404, not 403.** Telling a caller that a signal
exists but is not theirs would itself disclose that something was flagged
there. Where an identifier is named explicitly in a request, it is *rejected*
rather than filtered away, so a caller learns their request was refused instead
of quietly receiving less than they asked for.

## Response headers

Applied to every response by `SecurityHeadersMiddleware`.

| Header | Value | Why |
| --- | --- | --- |
| `X-Content-Type-Options` | `nosniff` | A JSON response rendered as HTML is how a stored value becomes a script. |
| `X-Frame-Options` | `DENY` | Clickjacking a triage button attacks a workflow that changes records. |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` | Same, and this API serves no markup. |
| `Referrer-Policy` | `no-referrer` | A signal ID in a Referer leaks which district was open. |
| `Permissions-Policy` | camera, mic, geolocation off | This API needs none of them. |
| `Cache-Control` | `no-store` | Responses are per-principal; a shared cache would leak one officer's answer to the next. |
| `Strict-Transport-Security` | 1 year, subdomains | **Protected environments only** — sending it from a local HTTP deployment would pin a developer's browser to HTTPS for localhost. |

## CORS

A wildcard origin is **refused at startup**, not silently narrowed. MARS sends
credentials with cross-origin requests, and `allow_origins=["*"]` with
credentials would let any site read a signed-in user's surveillance data. A
misconfiguration that would ship silently is worse than one that fails loudly.

## Export safety

CSV cells beginning `=`, `+`, `-`, `@`, tab or carriage return are prefixed so
they cannot execute. A surveillance export is exactly the file someone opens
without thinking. See [Prompt 25's report service](../data-dictionary/../architecture/../data-dictionary/investigations.md).

## Audit

| Act | Action recorded |
| --- | --- |
| Report generated | `report_generated` with product, period, scope, row count — **not** the figures |
| Investigation opened / transitioned | `signal_triaged`, `investigation_updated` with from/to |
| Ask MARS query | `ai_request_submitted` with topic, provider, model, response hash, record IDs — **not** the question text |
| Scope denial | `record_denial` on a separate durable session, so a rolled-back request still leaves the denial |

## Known gaps

These are not implemented and are listed rather than claimed.

* **Rate limiting** is not implemented in the application. It belongs at the
  reverse proxy, and the deployment documentation says so; putting a token
  bucket in the app would give a false sense of protection against a
  distributed source.
* **MFA** is an identity-provider concern. MARS consumes OIDC and does not
  implement authentication factors.
* **Data retention periods** are not set. How long a surveillance record is
  kept is a legal determination, and MARS will not invent one.
* **Backup access control** is a deployment concern; the procedure is
  documented but no code enforces it.
