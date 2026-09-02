"""Identity vault, encrypted at rest, with an append-only disclosure log.

Revision ID: 0006_identity_vault
Revises: 0005_opd_encounter_model
Created: 2026-09-02

Fills ``mars_identity``, present and deliberately empty since migration 0001.

Three things matter more than the table shapes.

**Every direct identifier is stored as ciphertext.** ``bytea`` columns holding
AES-256-GCM envelopes, encrypted in the application before they reach
PostgreSQL. Schema separation keeps identity away from the application; it does
nothing for a backup, a replica or a stolen disk, and a column named ``surname``
holding a name in a nightly dump is the same breach whichever schema it sits in.

**The disclosure log is append-only in the database.** A trigger rejects UPDATE
and DELETE on ``reidentification_event``. An audit trail that the auditee can
edit is not an audit trail, and the identity service is precisely the component
with the most reason to edit it.

**Roles are not created here.** Granting privileges needs no special rights;
creating a role needs ``CREATEROLE``, which an ordinary migration runner should
not have. Role creation is a privileged provisioning step - see
``scripts/provision_identity_roles.sql`` - and this migration applies grants
only to roles that already exist, so it runs identically with or without them.

See ``docs/security/identity-vault.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_identity_vault"
down_revision: str | None = "0005_opd_encounter_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTITY = "mars_identity"

#: The application role: API, workers, analytics. Must never reach identity.
APP_ROLE = "mars_app"

#: The identity service role. Reaches identity and nothing else.
IDENTITY_ROLE = "mars_identity_service"


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    postgresql.ENUM(
        "national_id",
        "refugee_number",
        "passport",
        "phone",
        "unspecified_scheme",
        name="identifier_type",
        schema=IDENTITY,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "deterministic_identifier",
        "deterministic_unspecified_scheme",
        "unlinked",
        "withdrawn",
        name="linkage_confidence",
        schema=IDENTITY,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "disclosed",
        "denied_permission",
        "denied_sensitivity",
        "denied_no_reason",
        "not_found",
        name="reidentification_outcome",
        schema=IDENTITY,
    ).create(bind, checkfirst=True)

    # -- identity_record ---------------------------------------------------
    op.create_table(
        "identity_record",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("patient_reference_id", sa.UUID(), nullable=False),
        # Ciphertext, not text. The associated data binds each value to this
        # table, this column and this patient reference, so a value moved
        # between columns or rows fails to decrypt rather than misattributing a
        # name to the wrong person.
        sa.Column("surname_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("given_name_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("phone_contact_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("date_of_birth_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("encryption_key_version", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_record")),
        sa.UniqueConstraint("patient_reference_id", name="uq_identity_record_reference"),
        schema=IDENTITY,
        comment=(
            "Direct patient identity, encrypted at rest. No clinical data may "
            "be stored here: the vault knows who, mars_core knows what happened."
        ),
    )
    op.create_index(
        "ix_identity_record_reference",
        "identity_record",
        ["patient_reference_id"],
        schema=IDENTITY,
    )

    # -- identity_identifier -----------------------------------------------
    #
    # No foreign key to mars_core.patient_reference, deliberately: a cross-schema
    # key would need the identity role to hold privileges on mars_core, and would
    # let one join carry a name and a diagnosis out together.
    #
    # The linkage token is plaintext because it must be indexed for equality.
    # It is a MAC, not an encryption: it cannot be reversed to the identifier
    # even with the key, and it is derived under a *different* key from the one
    # protecting the ciphertext beside it.
    op.create_table(
        "identity_identifier",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("identity_record_id", sa.UUID(), nullable=False),
        sa.Column(
            "identifier_type",
            postgresql.ENUM(
                "national_id",
                "refugee_number",
                "passport",
                "phone",
                "unspecified_scheme",
                name="identifier_type",
                schema=IDENTITY,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("raw_value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_key_version", sa.String(length=16), nullable=False),
        sa.Column("linkage_token", sa.String(length=64), nullable=False),
        sa.Column("linkage_key_version", sa.String(length=16), nullable=False),
        sa.Column(
            "confidence",
            postgresql.ENUM(
                "deterministic_identifier",
                "deterministic_unspecified_scheme",
                "unlinked",
                "withdrawn",
                name="linkage_confidence",
                schema=IDENTITY,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_identifier")),
        sa.ForeignKeyConstraint(
            ["identity_record_id"],
            [f"{IDENTITY}.identity_record.id"],
            name=op.f("fk_identity_identifier_identity_record_id_identity_record"),
            ondelete="CASCADE",
        ),
        # One national ID per person. A second means a data-quality problem that
        # must surface rather than silently attach to the same patient.
        sa.UniqueConstraint(
            "identity_record_id",
            "identifier_type",
            name="uq_identity_identifier_one_per_type",
        ),
        # The constraint that makes concurrent linkage safe: two workers racing
        # on the same identifier both try to insert, one wins, the loser re-reads
        # and returns the winner's patient rather than creating a second.
        sa.UniqueConstraint(
            "linkage_token",
            "linkage_key_version",
            name="uq_identity_identifier_token_version",
        ),
        sa.CheckConstraint(
            "length(linkage_token) = 64",
            name=op.f("ck_identity_identifier_linkage_token_is_sha256_hex"),
        ),
        sa.CheckConstraint(
            "octet_length(raw_value_encrypted) > 0",
            name=op.f("ck_identity_identifier_raw_value_is_ciphertext"),
        ),
        schema=IDENTITY,
        comment=(
            "Patient identifiers, encrypted, beside the linkage token derived "
            "from them. The token is a MAC under a separate key: it cannot be "
            "reversed to the identifier, and matching never decrypts anything."
        ),
    )
    op.create_index(
        "ix_identity_identifier_record",
        "identity_identifier",
        ["identity_record_id"],
        schema=IDENTITY,
    )
    op.create_index(
        "ix_identity_identifier_token",
        "identity_identifier",
        ["linkage_token"],
        schema=IDENTITY,
    )

    # -- reidentification_event --------------------------------------------
    #
    # In mars_identity rather than mars_audit because the fact that a particular
    # reference was re-identified, and why, is itself sensitive; mars_audit is
    # readable by roles that must not learn it. The general audit trail records
    # that a disclosure happened, without the reason.
    op.create_table(
        "reidentification_event",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("actor_label", sa.String(length=160), nullable=False),
        sa.Column("session_reference", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("patient_reference_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "outcome",
            postgresql.ENUM(
                "disclosed",
                "denied_permission",
                "denied_sensitivity",
                "denied_no_reason",
                "not_found",
                name="reidentification_outcome",
                schema=IDENTITY,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reidentification_event")),
        # A disclosure without a stated purpose is not reviewable afterwards, so
        # the database refuses to record one.
        sa.CheckConstraint(
            "length(btrim(reason)) > 0",
            name=op.f("ck_reidentification_event_reason_is_stated"),
        ),
        schema=IDENTITY,
        comment=(
            "Append-only. Re-identification attempts: who asked, about which "
            "reference, why, and with what outcome. Never the identifier or "
            "name that was disclosed."
        ),
    )
    op.create_index(
        "ix_reidentification_event_actor",
        "reidentification_event",
        ["actor_user_id", "requested_at"],
        schema=IDENTITY,
    )
    op.create_index(
        "ix_reidentification_event_outcome",
        "reidentification_event",
        ["outcome"],
        schema=IDENTITY,
    )
    op.create_index(
        "ix_reidentification_event_reference",
        "reidentification_event",
        ["patient_reference_id"],
        schema=IDENTITY,
    )

    # -- Append-only enforcement -------------------------------------------
    #
    # Revoking UPDATE and DELETE from the identity role would be undone by any
    # future GRANT, and would not bind a superuser or the table owner at all. A
    # trigger binds every writer, which is the point: the component with the
    # most reason to edit this table is the one that writes to it.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {IDENTITY}.reject_disclosure_log_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'reidentification_event is append-only; % is not permitted',
                TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER reidentification_event_append_only
            BEFORE UPDATE OR DELETE ON {IDENTITY}.reidentification_event
            FOR EACH ROW
            EXECUTE FUNCTION {IDENTITY}.reject_disclosure_log_mutation();
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {IDENTITY}.reject_disclosure_log_mutation() IS "
        "'Enforces append-only on the disclosure log. Binds every writer "
        "including the identity service itself.'"
    )

    # -- Privilege separation ----------------------------------------------
    #
    # Applied only to roles that already exist. Creating them needs CREATEROLE,
    # which a migration runner should not hold; provisioning is a separate,
    # privileged step (scripts/provision_identity_roles.sql). Running this
    # migration on a cluster without the roles is not an error - the schema is
    # created and the grants are applied later by provisioning.
    op.execute(f"REVOKE ALL ON SCHEMA {IDENTITY} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {IDENTITY} FROM PUBLIC")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                -- REVOKE rather than merely not granting: PUBLIC or an earlier
                -- deployment may have granted USAGE, and a privilege nobody
                -- remembers granting is exactly how this boundary leaks.
                REVOKE ALL ON SCHEMA {IDENTITY} FROM {APP_ROLE};
                REVOKE ALL ON ALL TABLES IN SCHEMA {IDENTITY} FROM {APP_ROLE};
                -- So a table added by a later migration inherits the denial
                -- rather than being readable because it did not exist today.
                ALTER DEFAULT PRIVILEGES IN SCHEMA {IDENTITY}
                    REVOKE ALL ON TABLES FROM {APP_ROLE};
            END IF;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{IDENTITY_ROLE}') THEN
                GRANT USAGE ON SCHEMA {IDENTITY} TO {IDENTITY_ROLE};
                -- No UPDATE or DELETE on the disclosure log. The trigger binds
                -- every writer; withholding the privilege as well means an
                -- attempt fails before it reaches the trigger at all.
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON {IDENTITY}.identity_record TO {IDENTITY_ROLE};
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON {IDENTITY}.identity_identifier TO {IDENTITY_ROLE};
                GRANT SELECT, INSERT
                    ON {IDENTITY}.reidentification_event TO {IDENTITY_ROLE};
                ALTER DEFAULT PRIVILEGES IN SCHEMA {IDENTITY}
                    GRANT SELECT, INSERT ON TABLES TO {IDENTITY_ROLE};

                -- The separation cuts both ways. A compromised identity service
                -- must not be able to read what the people it can name were
                -- treated for.
                REVOKE ALL ON SCHEMA mars_core FROM {IDENTITY_ROLE};
                REVOKE ALL ON ALL TABLES IN SCHEMA mars_core FROM {IDENTITY_ROLE};
            END IF;
        END
        $$;
        """
    )

    op.execute(
        f"COMMENT ON SCHEMA {IDENTITY} IS "
        "'Direct patient identifiers, encrypted at rest. Reachable only by "
        "mars_identity_service; mars_app has USAGE revoked, so an ordinary "
        "connection cannot name these tables at all.'"
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse only what the upgrade granted, and never broaden access.
    #
    # An earlier draft restored ALL on future tables to mars_app here, on the
    # theory that a downgrade should undo a revoke. That was backwards: the
    # revoke is the safe state, and a downgrade that hands the application role
    # blanket rights over every future identity table would turn a routine
    # rollback into a privilege escalation.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{IDENTITY_ROLE}') THEN
                ALTER DEFAULT PRIVILEGES IN SCHEMA {IDENTITY}
                    REVOKE ALL ON TABLES FROM {IDENTITY_ROLE};
                REVOKE ALL ON ALL TABLES IN SCHEMA {IDENTITY} FROM {IDENTITY_ROLE};
                REVOKE ALL ON SCHEMA {IDENTITY} FROM {IDENTITY_ROLE};
            END IF;
        END
        $$;
        """
    )

    op.execute(
        f"DROP TRIGGER IF EXISTS reidentification_event_append_only "
        f"ON {IDENTITY}.reidentification_event"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {IDENTITY}.reject_disclosure_log_mutation()")

    op.drop_table("reidentification_event", schema=IDENTITY)
    op.drop_table("identity_identifier", schema=IDENTITY)
    op.drop_table("identity_record", schema=IDENTITY)

    postgresql.ENUM(name="reidentification_outcome", schema=IDENTITY).drop(
        bind, checkfirst=True
    )
    postgresql.ENUM(name="linkage_confidence", schema=IDENTITY).drop(bind, checkfirst=True)
    postgresql.ENUM(name="identifier_type", schema=IDENTITY).drop(bind, checkfirst=True)

    # Roles are left alone: this migration did not create them, they may be
    # shared with other databases in the cluster, and dropping a role that owns
    # objects elsewhere would fail or silently remove somebody else's access.
    op.execute(
        f"COMMENT ON SCHEMA {IDENTITY} IS "
        "'Direct patient identifiers. Separate role; restricted; empty.'"
    )
