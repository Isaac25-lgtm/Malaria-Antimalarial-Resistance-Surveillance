# Backup, restore and migration recovery

A backup nobody has restored is a hope, not a backup. `scripts/backup_restore.py`
exists so the restore is a routine command rather than an improvisation at the
worst possible moment.

## Taking a backup

```bash
export MARS_DATABASE_URL=postgresql+psycopg://mars@db:5432/mars
python scripts/backup_restore.py backup /var/backups/mars/mars-$(date +%F).dump
```

Custom format, not plain SQL: it restores selectively and in parallel. Plain SQL
restores only in full and only in order, which is the wrong shape for a database
where one schema may need recovering on its own.

The password is never passed as an argument — libpq reads `PGPASSWORD` or
`.pgpass` directly, so it never appears in a process list.

## The restore drill

Run this on a schedule. It is the only thing that turns a backup into a
guarantee.

```bash
python scripts/backup_restore.py drill
```

It backs up the source, creates a fresh disposable database, restores into it,
compares the table count, reports the migration head, and drops the copy.

The comparison asserts the count is both **equal and non-zero** — a drill that
passed because both databases were empty would be worthless.

**Last drill result** (2026-09-04, disposable PostgreSQL 16.4 / PostGIS 3.6.2):

```
drill: source=mars_m0022 target=mars_drill_1788492961
backup written: mars_m0022-1788492961.dump (371156 bytes)
restored into mars_drill_1788492961
drill passed: 71 tables restored, migration head 0023_active_signal_index
drill copy dropped: mars_drill_1788492961
```

## Restoring for real

```bash
python scripts/backup_restore.py restore /var/backups/mars/mars-2026-09-04.dump mars_restore_20260904
```

The target must already exist and must have PostGIS installed.

A target whose name does not begin `mars_drill_`, `mars_restore_`, `mars_test_`
or `mars_tmp_` is **refused** unless `--i-know-this-is-not-a-drill` is passed.
Restoring over the wrong database is the mistake this guard exists to make
difficult.

The recommended shape of a real recovery is therefore: restore into a new
database, verify it, then repoint the application — rather than restoring over
the live one and discovering the archive was truncated.

## What a backup contains

Everything, including `mars_identity`. The identity vault is encrypted at rest
with the keys in `MARS_ENCRYPTION_KEYS`, so **a backup is useless without those
keys and dangerous with them.** Store them separately from the dumps, under
different access control. A backup archive and its key material in the same
bucket is a single point of compromise.

Retention is **not set** by MARS. How long a surveillance backup may be kept is
a legal determination for the programme and its data protection authority, and
MARS will not invent one.

## Migration rollback

Every MARS migration has a written and tested `downgrade`. Migrations are
round-tripped in CI: base → head → base.

```bash
alembic current                      # what is applied
alembic history --verbose | head     # what exists
alembic downgrade -1                 # one step back
alembic downgrade 0022_investigations
```

### Before rolling back a migration that dropped or altered data

**Take a backup first.** A downgrade that removes a column removes the data in
it, and `alembic downgrade` cannot put it back. The reversibility guarantee is
about schema shape, not about content.

The migrations in this build are additive — new tables, new columns, new
indexes — so their downgrades lose only rows written since the upgrade. That
will not be true of every future migration.

### If a migration fails part-way

PostgreSQL runs each migration in a transaction, so a failed migration leaves
the database at its previous revision with nothing half-applied. The usual
cause is a data condition the migration refuses to guess at — migration 0007 is
the worked example: it will not guess whether a bare age of `3` means years,
months or days, and stops with the query that finds the offending rows.

1. Read the error. MARS migrations that fail closed say what to fix.
2. Fix the data.
3. Re-run `alembic upgrade head`.

Do not edit an applied migration to make it pass. Add a new one.

### Offline SQL

For a database an operator will not let a tool connect to:

```bash
alembic upgrade head --sql > upgrade.sql
alembic downgrade head:base --sql > downgrade.sql
```

Both directions render without a connection. Review them, then hand them to
whoever holds the credentials.

## Recovering the identity vault

The vault is a separate schema behind a separate database role. Recovering it
requires:

1. the dump,
2. `MARS_ENCRYPTION_KEYS` including every version that encrypted a row still
   present — retired keys stay in the list to decrypt old rows, and are never
   used to encrypt new ones,
3. the restricted role re-provisioned with `scripts/provision_identity_roles.sql`.

If the keys are lost the vault is unrecoverable. That is the intended property
of encryption at rest, and it is why the keys belong in a secret manager with
its own backup and its own access log.
