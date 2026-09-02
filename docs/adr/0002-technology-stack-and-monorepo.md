# ADR 0002: Technology stack and monorepo structure

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 1

## Context

MARS is a data-intensive surveillance platform: heavy analytical work over
patient-level and aggregate health data, real spatial epidemiology, and a
polished operational interface. It must be deployable, maintainable by a team
whose centre of gravity is data science rather than web engineering, and stable
across a multi-year build.

The blueprint (section 014) proposes a stack. Reconnaissance confirmed it is
appropriate and that the required skills are present.

## Decision

**Backend.** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic. Python
because the analytics engines, the geospatial processing and the eventual
statistical validation all live there; splitting the API into another language
would put a network boundary through the middle of the domain.

**Database.** PostgreSQL 16 with PostGIS 3.4. Pinned to 16 rather than 17 or 18:
PostGIS packaging and container images are most mature there, and the migration
cost to a later major version is a dump and restore with no application change,
so nothing is foreclosed.

**Frontend.** React, TypeScript, Vite, TanStack Query, MapLibre GL JS, ECharts.
MapLibre is open source with no tile-provider lock-in, which matters because
analytical data must stay on MARS servers.

**Structure.** A single repository containing `backend/`, `frontend/`,
`contracts/`, `scripts/`, `docs/`, `infra/` and `data/`. The API contract is
generated from the backend and consumed by the frontend, and CI fails on drift.
A split repository would make that check a cross-repository dance and would let
the two sides disagree between merges.

**Package layout.** Inside `backend/src/mars/`: `domain/` holds models and
enumerations, `services/` holds business logic and repositories, `api/` holds
routers only. A route handler never contains a query and never decides its own
authorisation. This is what allows the analytics engines of later phases to be
tested without a web server.

## Consequences

- One language for the API, the workers and the analytics.
- One dependency graph and one CI pipeline.
- PostGIS is required for the geography phase; a database without it fails
  readiness with a clear message rather than silently degrading.
- The repository is larger, and a frontend-only change still runs backend CI.
  Accepted: the contract check is worth more than the saved minutes.

## Revisit when

The analytics workload outgrows a single process in a way horizontal API scaling
cannot address, or a separate analytics service becomes necessary for
operational isolation rather than architectural neatness.
