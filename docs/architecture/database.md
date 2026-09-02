# Database architecture

How MARS stores data, why the schemas are separated, and the conventions every
table follows.

## Schema boundaries

Six PostgreSQL schemas, created by migration `0001_schema_baseline`. The
separation is by sensitivity and by ownership, not by convention.

| Schema | Holds | Notes |
| --- | --- | --- |
| `mars_core` | Canonical surveillance data: geography, organisation units, facilities; later encounters and aggregates | Contains no direct patient identifier |
| `mars_identity` | Direct patient identifiers | Separate database role. Application role holds no grant. **Empty until Prompt 8** |
| `mars_audit` | Append-only audit events | No update or delete path exists |
| `mars_security` | Users, roles, permissions, geography and sensitivity scopes | Separated so operator access to data does not imply access to the access model |
| `mars_governance` | Configuration versions and the method registry | The record of which rules produced a result |
| `mars_analytics` | Derived and materialised output | Placeholder; rebuildable without touching canonical data |

`mars_identity` is created empty, deliberately. The boundary must exist before
the data does, because retrofitting it means rewriting every foreign key that
crosses it (ADR 0006).

## Conventions

Blueprint appendix 159, asserted by tests in
`backend/tests/unit/test_migrations.py` rather than left to discipline.

- **UUID primary keys**, always named `id`, defaulting to `gen_random_uuid()`.
  Source identifiers live in their own columns and are never promoted to a key.
- **snake_case** tables and columns.
- **`*_id`** for every foreign key.
- **`created_at` / `updated_at`**, timezone-aware, stored in UTC. A naive
  timestamp is rejected in application code: guessing its zone is how a
  reporting period silently shifts by a day.
- **Native enum types**, storing the member *value* so what is in the database,
  what the API returns and what the TypeScript contract declares are the same
  lowercase string.
- **No overloaded `status` column.** Each lifecycle has its own named column:
  `import_status`, `match_status`, `validity_state`, `outcome`. Only the
  governance registries use `status`, for exactly one lifecycle each.
- **Deterministic constraint names** from a metadata naming convention, so a
  constraint violation is identifiable from the error message alone.

One constraint name is shortened by hand:
`uq_geography_unit_alias_source_and_unit`. The convention would generate 67
characters, above the PostgreSQL 63-character limit. A test now guards the
limit for every identifier.

## Transactions

Route handlers never open a transaction. They receive a session from
`get_db_session`, which commits when the handler returns and rolls back on any
exception, so a partially applied write cannot escape a failed request. Workers
and scripts use `session_scope()`, which behaves identically.

Access-denial audit events are the deliberate exception to the request
transaction: they use a separate short-lived session so the audit record remains
durable when the denied request rolls back. This session cannot commit any
application work from the rejected request.

Every connection sets `statement_timeout` and `TIME ZONE 'UTC'` on connect. The
timeout stops a runaway analytical query holding a connection indefinitely.

## Audit immutability

`mars_audit.audit_event` is append-only, enforced at three levels because one is
not enough:

1. `AuditService` exposes no update or delete method. A test asserts the absence.
2. SQLAlchemy `before_update` and `before_delete` listeners raise, plus a
   session-level `before_flush` guard that catches attempts bypassing them.
3. A database trigger, `audit_event_append_only`, rejects `UPDATE` and `DELETE`
   with a `restrict_violation`. This is the layer raw SQL cannot bypass, and it
   is exercised by the integration suite.

A mistake in an audit record is corrected by appending a correcting event, never
by editing history.

## Migrations

Alembic, schema-aware. `include_object` restricts autogenerate comparison to the
six MARS schemas, so PostGIS tables in `public` are never proposed for deletion.

Both directions are required. `downgrade` is written and tested, not stubbed: a
migration that cannot be reversed cannot be safely applied to a production
surveillance database. A test fails any migration with an empty downgrade.

Enum types are created explicitly, once each, and column definitions reference
them with `create_type=False`. Without this, `lifecycle_status` - used by two
tables - would be created twice and the second `CREATE TYPE` would fail on
apply. A test classifies every enum reference in the migration and asserts the
column definitions disclaim creation.

### Verifying without a database

```bash
cd backend
MARS_DATABASE_URL=postgresql+psycopg://mars:offline@localhost:5432/mars \
  alembic upgrade head --sql        # renders the full schema as SQL
MARS_DATABASE_URL=... alembic downgrade head:base --sql
```

This proves both directions are executable and lets the generated DDL be
reviewed before it touches a server.

### Applying to a database

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic downgrade -1
```

## Current state

20 tables, 36 indexes, 14 enum types across three migrations.

| Migration | Creates |
| --- | --- |
| `0001_schema_baseline` | Six schemas, `pgcrypto` extension, schema comments |
| `0002_core_domain` | All 20 tables, indexes, enum types, audit trigger |
| `0003_phase2_hardening` | PostGIS `MultiPolygon` geometry columns and GIST indexes; database triggers rejecting multi-node geography and organisation hierarchy cycles |
