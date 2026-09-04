# Architecture and data flow

How a row in a paper register becomes a signal on a screen, and what stops it
becoming a claim.

## The two lanes

Everything in MARS belongs to one of two evidence lanes, and nothing crosses
between them.

**Lane A — routine-derived.** HMIS 033b, HMIS 105 and the OPD e-register.
Produces indicators, episodes, recurrence measures, baselines, anomalies,
hotspots, clusters and signals. May say *this pattern is worth investigating*.
May never say *resistance*.

**Lane B — confirmed evidence.** Therapeutic efficacy studies and molecular
markers, from an external reference laboratory under separate governance. Not
implemented in this build, and deliberately not writable from Lane A.

A terminology lint runs in CI over the whole repository and fails on prohibited
phrasing.

## The pipeline

```
  Paper / e-register            HMIS 033b (weekly)      HMIS 105 (monthly)
          │                            │                        │
          ▼                            ▼                        ▼
  ┌───────────────────────────────────────────────────────────────────┐
  │ Ingestion — strict contract, quarantine, lineage, checksum        │
  │ mars_core.import_batch · opd_encounter · aggregate_submission     │
  └───────────────────────────────────────────────────────────────────┘
          │                                        │
          │  pseudonymous reference only           │
          ▼                                        ▼
  ┌────────────────────┐                  ┌──────────────────────┐
  │ mars_identity      │                  │ Reconciliation       │
  │ encrypted vault    │                  │ aggregate vs source  │
  │ separate DB role   │                  └──────────────────────┘
  │ NEVER read by      │                             │
  │ analytics          │                             ▼
  └────────────────────┘        ┌────────────────────────────────────┐
                                │ Governed indicators (Prompt 13)     │
                                │ definition → version → result       │
                                └────────────────────────────────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              ▼                              ▼                              ▼
    ┌──────────────────┐        ┌────────────────────────┐      ┌──────────────────┐
    │ Episodes         │        │ Testing / treatment /  │      │ Geographic       │
    │ Recurrence       │        │ commodity surveillance │      │ aggregation      │
    └──────────────────┘        └────────────────────────┘      └──────────────────┘
              │                              │                              │
              │                              ▼                              ▼
              │                  ┌────────────────────┐          ┌──────────────────┐
              │                  │ Baselines          │          │ Hotspots         │
              │                  │ (area's own past)  │─────────▶│ Clusters         │
              │                  └────────────────────┘          └──────────────────┘
              │                              │                              │
              │                              ▼                              │
              │                  ┌────────────────────┐                     │
              │                  │ Temporal anomalies │                     │
              │                  └────────────────────┘                     │
              │                              │                              │
              └──────────────┬───────────────┴──────────────────────────────┘
                             ▼
                 ┌────────────────────────────┐
                 │ Signals (Prompt 21)         │  typed evidence, both roles
                 │ + Explanations (Prompt 22)  │  deterministic, no model
                 └────────────────────────────┘
                             │
                             ▼
                 ┌────────────────────────────┐
                 │ Investigations (Prompt 26)  │  never mutates the signal
                 └────────────────────────────┘
```

## The six schemas

| Schema | Holds | Rule |
| --- | --- | --- |
| `mars_core` | Canonical surveillance data, investigations | No direct identifiers |
| `mars_identity` | Direct identifiers, linkage tokens | Separate restricted role, encrypted |
| `mars_audit` | Audit events | Append-only, no update or delete path |
| `mars_security` | Users, roles, scopes | — |
| `mars_governance` | Configuration versions, method registry | Values, not just flags |
| `mars_analytics` | Derived results | Rebuildable, immutable |

71 tables at migration head `0023_active_signal_index`.

## Properties the architecture enforces

### Blank is not zero, at every step

Preserved from a blank cell on a paper form all the way to a KPI card. An
undefined denominator produces `unavailable`, never zero; the value column is
null and the status column says which kind of absence it is. Nothing in the
pipeline can turn one into the other, and the constraint
`value_present_iff_available` is on every result table that carries a figure.

### Analytical results are immutable

A recomputation writes a new row with a new input fingerprint beside the old
one. The figure a district acted on last week is still readable after this
week's correction. Every consumer reads the latest `computed_at` per period, so
a superseded figure never votes twice in its own history.

### Nothing computes without governed approval

Windows, thresholds, weights, privacy minimums, priority bands and SLAs are all
configuration. MARS implements the mechanisms and ships no values. Each engine
refuses with a record naming the missing key.

### Identity never reaches analytics

No module in `analytics`, `signals`, `explainability`, `investigations`,
`services` or `ai` imports `mars.identity`. A module-boundary test enforces it.
No analytical table has a column that could hold a name, a phone number or a
national identifier.

### Scope is applied in SQL

Never by filtering results afterwards. A facility's district membership does not
grant the district-wide picture. Out-of-scope reads return 404, because 403
would confirm that something exists there.

## Boundaries, by ADR

| ADR | Rule |
| --- | --- |
| 0002 | Routers hold no queries and never import ORM models |
| 0003 | Domain, services and analytics never import an adapter |
| 0005 | Scientific terminology and the two evidence lanes |
| 0006 | Identity separation and the three authorisation axes |
| 0007 | Governance and method versioning |
| 0008 | `mars.ai` is a leaf — a disabled deployment never imports it |

Each has a test. The rules are only real because something checks them.

## Request path

```
Client → reverse proxy (TLS, rate limit)
       → RequestContextMiddleware   assigns and echoes a request id
       → SecurityHeadersMiddleware  nosniff, DENY, no-referrer, no-store
       → AccessLogMiddleware        structured line, sensitive params redacted
       → route
           → require_permissions(...)   permission + sensitivity, server-side
           → service                    scope applied in SQL, returns dicts
           → response schema            no ORM object reaches the router
```

## Frontend

React 18, Vite 6, TanStack Query, MapLibre GL. Types are generated from the
OpenAPI document, and CI fails on any difference — a backend field rename breaks
the build rather than the running interface.

**The frontend computes no analytical value.** Every figure arrives as a record
carrying its period, scope, source, method version and availability status.
There is no KPI formula in the browser to disagree with the server about.

MapLibre is dynamically imported and manually chunked, so a user who opens the
command centre and drills into a district table never downloads it.
