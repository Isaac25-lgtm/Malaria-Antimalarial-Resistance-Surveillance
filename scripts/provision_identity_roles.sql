-- MARS identity role provisioning.
--
-- Run once per cluster, by a role holding CREATEROLE, BEFORE the application
-- connects. Deliberately separate from the Alembic migrations: granting
-- privileges needs no special rights, but creating a role needs CREATEROLE, and
-- an ordinary migration runner should not hold it. A migration that assumed
-- CREATEROLE would either fail in a locked-down deployment or force operators to
-- run migrations as a superuser, which is worse than the problem it solves.
--
-- Usage:
--     psql -v ON_ERROR_STOP=1 -f scripts/provision_identity_roles.sql -d <database>
--
-- Then grant login and credentials separately, from your secret store:
--
--     ALTER ROLE mars_app_login LOGIN PASSWORD :'app_password';
--     GRANT mars_app TO mars_app_login;
--     ALTER ROLE mars_identity_login LOGIN PASSWORD :'identity_password';
--     GRANT mars_identity_service TO mars_identity_login;
--
-- No password appears in this file, in any migration, or in the repository.
-- The two group roles below are created NOLOGIN precisely so that a credential
-- must be attached deliberately, by whoever owns the secret store, rather than
-- inherited from a script somebody committed.

\set ON_ERROR_STOP on

BEGIN;

-- Idempotent: safe to re-run against a cluster that already has the roles, and
-- safe to run on a cluster shared with other databases.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mars_app') THEN
        CREATE ROLE mars_app NOLOGIN;
        COMMENT ON ROLE mars_app IS
            'MARS application, workers and analytics. Has USAGE revoked on '
            'mars_identity: it cannot name an identity table at all.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mars_identity_service') THEN
        CREATE ROLE mars_identity_service NOLOGIN;
        COMMENT ON ROLE mars_identity_service IS
            'MARS identity service. Reaches mars_identity and nothing else; '
            'has no privileges on the clinical schemas.';
    END IF;
END
$$;

COMMIT;

-- After this, run `alembic upgrade head`. Migration 0006 detects the roles and
-- applies the grants and revokes; it also runs cleanly when they are absent, so
-- the order is a recommendation rather than a trap.
--
-- To verify the boundary afterwards:
--
--     SELECT has_schema_privilege('mars_app', 'mars_identity', 'USAGE');
--         -> f
--     SELECT has_table_privilege('mars_identity_service',
--                                'mars_identity.identity_record', 'SELECT');
--         -> t
--     SELECT has_table_privilege('mars_identity_service',
--                                'mars_core.opd_encounter', 'SELECT');
--         -> f
--     SELECT has_table_privilege('mars_identity_service',
--                                'mars_identity.reidentification_event', 'UPDATE');
--         -> f
