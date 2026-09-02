# MARS

**Malaria Antimalarial Resistance Surveillance**

MARS converts routine malaria data — patient-level outpatient encounters, weekly
surveillance, monthly reporting and administrative geography — into explainable
surveillance signals that a named person is accountable for investigating.

> **Scientific boundary.** Signals produced from routine e-register and HMIS data
> indicate patterns requiring investigation. **They do not confirm antimalarial
> resistance.** Routine data cannot distinguish recrudescence from reinfection,
> prove drug exposure or adherence, identify parasite genotype, or confirm
> molecular markers. Externally confirmed findings — therapeutic efficacy studies
> and molecular results — are handled in a separate, separately governed lane.
> See [ADR 0005](docs/adr/0005-scientific-terminology-and-evidence-lanes.md).

---

## What exists today

This build covers **phases 1 and 2** of a fourteen-phase plan.

| Delivered | Detail |
| --- | --- |
| Repository and runtime foundation | Monorepo, Docker Compose, CI, quality gates |
| Database foundation | Six schemas, 20 tables, audit trail, governance registries |
| Authentication and authorisation | OIDC-ready, three authorisation axes, permission matrix |
| Reference-data domain model | Geography, organisation units, facilities |

**Not built yet, and deliberately absent rather than stubbed:** encounter
ingestion, patient episodes, indicators, anomaly detection, signals,
investigations, the national dashboard, and the optional AI assistant. A
navigation item leading to a fabricated dashboard would be worse than one that
is not there.

The application therefore has no surveillance data. Every view says so
specifically — "no boundary version has been imported" is a different fact from
"no facility master has been supplied", and the interface distinguishes them.

---

## Architecture

```
 SOURCES          OPD 002 · HMIS 033b · HMIS 105 · boundary GeoJSON
                                    |
 INGESTION        receive → checksum → validate → quarantine → canonical
                                    |
        +---------------------------+---------------------------+
        |                                                       |
 IDENTITY VAULT                                         CANONICAL STORE
 mars_identity                                          mars_core
 separate DB role                    person_key         no direct identifiers
 empty until Prompt 8               ------------>       geography · facilities
        |                                                       |
                                                        ANALYTICS  (Prompt 13+)
                                                                |
                                                        SIGNAL + EXPLAINABILITY
                                                                |
 API  FastAPI /api/v1 — every endpoint declares a permission and geography scope
                                                                |
 WEB  React + TypeScript + Vite + TanStack Query + MapLibre

 LANE B — confirmed evidence (TES · molecular · CPHL), separately governed.
 Never fed by routine data. The only lane permitted confirmatory language.
```

- [ADR index](docs/adr/) — eight decisions recorded
- [Database architecture](docs/architecture/database.md)
- [Authorisation model](docs/security/authorisation.md)
- [Geography audit](docs/data-dictionary/geography-audit.md) — generated, not written

---

## Prerequisites

| Tool | Version | Required for |
| --- | --- | --- |
| Python | 3.12+ | Backend, scripts |
| Node.js | 20+ (22 recommended) | Frontend |
| Docker Desktop | Any current | Full stack, PostGIS, integration tests |
| Git | 2.40+ | Everything |

Docker is needed for the database. Everything else — lint, types, unit tests,
API tests, the frontend build, the geography audit — runs without it.

---

## Getting started

### With Docker

```bash
cp .env.example .env
docker compose up --build -d
docker compose run --rm api alembic upgrade head
```

| Service | Address |
| --- | --- |
| API docs | http://localhost:8000/docs |
| Web application | http://localhost:5173 |
| PostgreSQL | `localhost:5433` (loopback only) |
| Redis | `localhost:6380` (loopback only) |

Non-default database and cache ports avoid colliding with anything already
running on the host.

### Without Docker

Everything except the database and the integration tests still runs.

```bash
# Backend
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"          # macOS / Linux

.venv/Scripts/ruff.exe format --check .
.venv/Scripts/ruff.exe check .
.venv/Scripts/mypy.exe
.venv/Scripts/pytest.exe -q          # integration tests skip, and say so

# Migrations render as SQL with no database
MARS_DATABASE_URL=postgresql+psycopg://mars:offline@localhost:5432/mars \
  .venv/Scripts/alembic.exe upgrade head --sql

# Frontend
cd ../frontend
npm ci
npm run lint && npm run typecheck && npm run test && npx vite build

# Repository-wide gates
cd ..
python scripts/terminology_lint.py
python scripts/geography_audit.py --verify-only
```

The API starts without a database and reports itself unready, which is the
intended behaviour: a transient outage must not prevent the service from
starting and explaining itself.

```bash
cd backend && .venv/Scripts/python.exe -m uvicorn mars.main:app --reload
curl http://localhost:8000/api/v1/health/ready   # 503, naming what is missing
```

---

## Migrations

```bash
docker compose run --rm api alembic upgrade head       # apply
docker compose run --rm api alembic downgrade -1       # roll back one
docker compose run --rm api alembic revision --autogenerate -m "add signal table"
```

Every migration must be reversible. `downgrade` is written and tested, not
stubbed — a migration that cannot be reversed cannot safely be applied to a
production surveillance database, and a test fails any that is empty.

---

## Tests

```bash
cd backend && .venv/Scripts/pytest.exe -q                  # all; integration skips
cd backend && .venv/Scripts/pytest.exe -m integration -q   # needs a database
cd frontend && npm run test
```

Integration tests require `MARS_TEST_DATABASE_URL`. Without it they **skip and
report the skip** — an absent database never produces a false pass.

```bash
export MARS_TEST_DATABASE_URL=postgresql+psycopg://mars:mars_local_development@localhost:5433/mars
```

---

## The geography files

Four supplied boundary files totalling 226 MB, at the repository root. They are
**excluded from Git by size**, with SHA-256 checksums tracked in
`data/manifests/geography.sha256.json`, so provenance survives a clone even
though the payload does not.

```bash
python scripts/geography_audit.py --verify-only   # confirm bytes are unchanged
python scripts/geography_audit.py                 # full audit report
```

They are never modified. `scripts/geography_audit.py` opens them read-only and
regenerates every figure in
[the audit document](docs/data-dictionary/geography-audit.md) from the files
themselves — no number in that document is a hard-coded constant.

See [ADR 0004](docs/adr/0004-geography-source-handling.md) for what the audit
found and which file plays which role.

---

## Quality gates

| Gate | Command | Enforces |
| --- | --- | --- |
| Terminology | `python scripts/terminology_lint.py` | No claim that routine data confirm resistance |
| Backend format | `ruff format --check .` | Consistent formatting |
| Backend lint | `ruff check .` | Style and common defects |
| Backend types | `mypy` | Strict typing, no untyped defs |
| Backend tests | `pytest` | Unit, API, security, integration |
| Frontend lint | `npm run lint` | Style, hooks, accessibility |
| Frontend types | `npm run typecheck` | Strict TypeScript |
| Frontend tests | `npm run test` | Component and behaviour |
| Frontend build | `npx vite build` | Production build succeeds |
| Contract drift | `python scripts/export_openapi.py --check` | API and client agree |
| Geography | `python scripts/geography_audit.py --verify-only` | Sources unchanged |

All run in CI. The terminology lint runs **first**: a change claiming routine
data confirm resistance should fail before anything else is spent on it.

### After changing the API

```bash
python scripts/export_openapi.py
cd frontend && npm run generate:api
```

CI fails if these are stale. The TypeScript client is generated from the
backend, so a field rename breaks the build rather than the running interface.

---

## Repository layout

```
backend/          FastAPI application, domain model, services, migrations
  src/mars/
    api/          Routers and dependencies. No queries, no authorisation logic
    core/         Settings, logging, errors, time, request context
    db/           Engine, session, base, schema constants
    domain/       ORM models and enumerations
    services/     Business logic and repositories
    security/     Permissions, principal, auth providers
    geo/          FScode parsing, name normalisation
    analytics/ signals/ explainability/ investigations/   Empty; later phases
frontend/         React application
  src/
    api/          Generated types and typed client
    app/          Shell, router, error boundary
    auth/         Context, provider, route guards
    design-system/ Tokens and the four data states
    features/     One directory per workspace
contracts/        openapi.json — the frontend contract
data/manifests/   Geography checksums
docs/             ADRs, architecture, security, data dictionary
infra/            Dockerfiles
scripts/          Geography audit, terminology lint, contract export
```

---

## Current limitations

**Environment**

- Docker was not available on the development machine when this was built, so
  the Compose stack, the PostGIS integration tests and the applied migrations
  have **not been executed**. Migrations were verified by rendering both
  directions as SQL offline. See the completion report for exactly what ran.
- PostGIS is required from the geography phase. Readiness reports its absence as
  `not_installed` rather than failing, since phases 1–2 do not need it.

**Data**

- No facility master, facility coordinates or Health Sub-District list has been
  supplied. The schema supports all three; none is populated, and none is
  invented.
- No parish or village boundaries exist. Those levels are in the schema and stay
  empty.
- No population denominators. Until they arrive, spatial output can only be
  counts and proportions — never incidence.
- The three HMIS reference documents (OPD 002, 033b, 105) are **not in the
  repository**. They are required before the canonical encounter schema can be
  built, and are the highest-priority missing input.

**Governance**

- No surveillance window, threshold, minimum count or signal weight has been
  supplied by the malaria programme. The registries ship empty, and
  `/api/v1/meta/version` reports them as empty rather than defaulting.

---

## Licence and data handling

Proprietary. Not for public distribution.

Contains no real patient data. Every account, facility and record in this build
is synthetic and marked as such. Real health data must not be loaded into a
development or staging environment.
