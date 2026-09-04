# Release checklist

Run in order. Everything here has been run against this build; the results are
recorded in the final report.

## 1. Quality gates

```bash
cd backend
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/ruff.exe check .
.venv/Scripts/mypy.exe
.venv/Scripts/pytest.exe tests -m "not integration" -q
```

```bash
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

## 2. Repository gates

```bash
backend/.venv/Scripts/python.exe scripts/terminology_lint.py
backend/.venv/Scripts/python.exe scripts/geography_audit.py --verify-only
backend/.venv/Scripts/python.exe scripts/export_openapi.py --check
git diff --check
git status --short
```

`geography_audit --verify-only` must report all four JSON sources unchanged.
`export_openapi --check` must report the contract up to date — a drifted
contract means the published API and the generated TypeScript disagree.

## 3. Integration suite

Against a **disposable** PostgreSQL 16 with PostGIS. Never a database anyone
else is using.

```bash
createdb mars_release_check && psql -d mars_release_check -c "CREATE EXTENSION postgis"
MARS_TEST_DATABASE_URL=postgresql+psycopg://mars@localhost:5432/mars_release_check \
  backend/.venv/Scripts/pytest.exe backend/tests -m integration -q
dropdb mars_release_check
```

Set `MARS_GEOGRAPHY_DATA_DIR` to the repository root to run the fourteen tests
that otherwise skip for want of the real boundary sources.

## 4. Migrations

```bash
alembic upgrade head          # from an empty database
alembic check                 # must say: No new upgrade operations detected
alembic downgrade base        # must leave no mars_* schema
alembic upgrade head --sql    > /dev/null
alembic downgrade head:base --sql > /dev/null
```

Then confirm, against the database at head:

* the expected table count,
* that no relation, constraint or index name exceeds 63 characters — PostgreSQL
  truncates silently, and a truncated name is one nobody can read in the error
  message that eventually quotes it.

## 5. Restore drill

```bash
python scripts/backup_restore.py drill
```

Must report the same table count as the source, a non-zero one, and the
expected migration head.

## 6. Release verification

* [ ] Working tree clean; every change committed.
* [ ] `git log` shows one commit per prompt, no rewritten history.
* [ ] No credential, token or password in the diff.
* [ ] Supplied PDFs, the DOCX and all four geography JSON files unchanged.
* [ ] No test weakened or deleted to obtain a green result.
* [ ] The README's implementation-status claim matches what exists.
* [ ] Known limitations documented rather than discovered.

## 7. Deployment verification

After deploying — see [deployment.md](./deployment.md).

* [ ] `/api/v1/health/ready` returns healthy.
* [ ] `/api/v1/health/schema` reports the expected migration head.
* [ ] Security headers present on every response.
* [ ] The application refused to start with a wildcard CORS origin (test it
      deliberately once, in staging).
* [ ] Development authentication and demo mode refused in the protected
      environment.
* [ ] The command centre loads and reports `not_configured` for every measure —
      the correct state before governance activation.

## What a fresh deployment looks like

Not broken. **Unconfigured**, and saying so.

Every KPI reports `not_configured` and names the indicator version it is waiting
for. The provenance bar says no indicator has an approved version. The map
refuses patient-derived detail and names the missing privacy policy. There is no
overdue investigation queue and the API explains that it needs an approved SLA.

This is the intended first impression. A system that showed a country of zeroes
would look finished and be wrong.
