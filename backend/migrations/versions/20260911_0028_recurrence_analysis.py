"""Retained evidence, definitions, reproducible runs and patient-origin investigations.

Revision ID: 0028_recurrence_analysis
Revises: 0027_live_sync
Created: 2026-09-11

Additive. Five tables in ``mars_analytics`` hold encrypted canonical evidence
datasets, reusable exploratory recurrence definitions and their immutable
versions, analysis runs with a frozen manifest, and pseudonymous per-patient
findings. Triggers make the immutability real rather than a convention:

* a dataset cannot be updated;
* a definition version and a finding cannot be updated (a version cannot be
  deleted either);
* a run's manifest cannot change, its dataset can be set once, and a completed
  or failed run cannot change at all; runs cannot be deleted.

``mars_core.investigation`` gains ``patient_finding_id`` as an alternative
source to ``signal_id``, with a check that exactly one is set and uniqueness per
finding. Existing signal-origin investigations are untouched.

The runtime role receives only the privileges each table needs. The identity
role receives none. The downgrade refuses to run while patient-origin
investigations exist, rather than silently losing their source.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_recurrence_analysis"
down_revision: str | None = "0027_live_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "mars_analytics"
APP_ROLE = "mars_app"
IDENTITY_ROLE = "mars_identity_service"

#: table -> privileges granted to the runtime role; everything else is revoked.
PRIVILEGES: dict[str, tuple[str, ...]] = {
    "clinical_evidence_dataset": ("SELECT", "INSERT"),
    "recurrence_definition": ("SELECT", "INSERT", "UPDATE"),
    "recurrence_definition_version": ("SELECT", "INSERT"),
    "recurrence_analysis_run": ("SELECT", "INSERT", "UPDATE"),
    "patient_recurrence_finding": ("SELECT", "INSERT"),
}


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


REJECT_MUTATION = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.reject_recurrence_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '{SCHEMA}.% is immutable; % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

GUARD_RUN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.guard_recurrence_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a recurrence analysis run is a record and cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.run_status IN ('completed', 'failed') THEN
        RAISE EXCEPTION 'a settled recurrence analysis run is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
       OR NEW.definition_version_id IS DISTINCT FROM OLD.definition_version_id
       OR NEW.method_version_id IS DISTINCT FROM OLD.method_version_id
       OR NEW.definition_name IS DISTINCT FROM OLD.definition_name
       OR NEW.definition_parameters IS DISTINCT FROM OLD.definition_parameters
       OR NEW.definition_checksum IS DISTINCT FROM OLD.definition_checksum
       OR NEW.engine_version IS DISTINCT FROM OLD.engine_version
       OR NEW.period_start IS DISTINCT FROM OLD.period_start
       OR NEW.period_end IS DISTINCT FROM OLD.period_end
       OR NEW.timezone IS DISTINCT FROM OLD.timezone
       OR NEW.scope_key IS DISTINCT FROM OLD.scope_key
       OR NEW.facility_refs IS DISTINCT FROM OLD.facility_refs
       OR NEW.filters IS DISTINCT FROM OLD.filters
       OR NEW.manifest_checksum IS DISTINCT FROM OLD.manifest_checksum
       OR NEW.created_by_subject IS DISTINCT FROM OLD.created_by_subject
       OR NEW.created_by_username IS DISTINCT FROM OLD.created_by_username
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.key_version IS DISTINCT FROM OLD.key_version
       OR (OLD.dataset_id IS NOT NULL AND NEW.dataset_id IS DISTINCT FROM OLD.dataset_id)
       OR (OLD.dataset_hash IS NOT NULL AND NEW.dataset_hash IS DISTINCT FROM OLD.dataset_hash)
    THEN
        RAISE EXCEPTION 'the manifest of a recurrence analysis run is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""

TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("clinical_evidence_dataset_immutable", "clinical_evidence_dataset", "UPDATE"),
    (
        "recurrence_definition_version_immutable",
        "recurrence_definition_version",
        "UPDATE OR DELETE",
    ),
    ("patient_recurrence_finding_immutable", "patient_recurrence_finding", "UPDATE"),
)


#: Audit vocabulary added for recurrence analysis. A PostgreSQL enum value cannot
#: be removed, so the downgrade leaves them; unused values are harmless.
AUDIT_ACTIONS = ("recurrence_definition_saved", "recurrence_run_submitted")


def upgrade() -> None:
    for action in AUDIT_ACTIONS:
        op.execute(f"ALTER TYPE mars_audit.audit_action ADD VALUE IF NOT EXISTS '{action}'")
    op.create_table(
        "clinical_evidence_dataset",
        _id(),
        *_timestamps(),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("namespace", sa.String(160), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("facility_refs", postgresql.JSONB(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=True),
        sa.Column("coverage_end", sa.Date(), nullable=True),
        sa.Column("coverage_complete", sa.Boolean(), nullable=False),
        sa.Column("coverage_notes", postgresql.JSONB(), nullable=False),
        sa.Column("stages", postgresql.JSONB(), nullable=False),
        sa.Column("mapping_version", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("key_version", sa.String(64), nullable=False),
        sa.Column("encounter_count", sa.Integer(), nullable=False),
        sa.Column("patient_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_clinical_evidence_dataset"),
        sa.UniqueConstraint(
            "source_kind",
            "scope_key",
            "mapping_version",
            "dataset_hash",
            "key_version",
            name="uq_clinical_evidence_dataset_identity",
        ),
        sa.CheckConstraint(
            "length(dataset_hash) = 64", name="ck_clinical_evidence_dataset_dataset_hash_is_sha256"
        ),
        sa.CheckConstraint(
            "source_kind IN ('stored_encounter', 'live_tracker')",
            name="ck_clinical_evidence_dataset_source_kind_known",
        ),
        sa.CheckConstraint(
            "coverage_start IS NULL OR coverage_end IS NULL OR coverage_end >= coverage_start",
            name="ck_clinical_evidence_dataset_coverage_ordered",
        ),
        sa.CheckConstraint(
            "encounter_count >= 0 AND patient_count >= 0",
            name="ck_clinical_evidence_dataset_counts_not_negative",
        ),
        schema=SCHEMA,
        comment=(
            "Immutable canonical evidence, encrypted at rest and identified by a "
            "content and coverage fingerprint. UPDATE is refused by trigger."
        ),
    )
    op.create_index(
        "ix_clinical_evidence_dataset_scope",
        "clinical_evidence_dataset",
        ["scope_key", "coverage_end"],
        schema=SCHEMA,
    )

    op.create_table(
        "recurrence_definition",
        _id(),
        *_timestamps(),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("owner_subject", sa.String(160), nullable=False),
        sa.Column("owner_username", sa.String(160), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id", name="pk_recurrence_definition"),
        sa.UniqueConstraint("owner_subject", "name", name="uq_recurrence_definition_owner_name"),
        sa.CheckConstraint("mode IN ('exploratory')", name="ck_recurrence_definition_mode_known"),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name="ck_recurrence_definition_name_not_blank"
        ),
        schema=SCHEMA,
        comment="Reusable exploratory recurrence definitions.",
    )

    op.create_table(
        "recurrence_definition_version",
        _id(),
        *_timestamps(),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_recurrence_definition_version"),
        sa.ForeignKeyConstraint(
            ["definition_id"],
            [f"{SCHEMA}.recurrence_definition.id"],
            ondelete="RESTRICT",
            name="fk_recurrence_definition_version_definition",
        ),
        sa.UniqueConstraint(
            "definition_id", "version_number", name="uq_recurrence_definition_version_number"
        ),
        sa.UniqueConstraint(
            "definition_id", "checksum", name="uq_recurrence_definition_version_checksum"
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_recurrence_definition_version_version_number_positive"
        ),
        sa.CheckConstraint(
            "length(checksum) = 64", name="ck_recurrence_definition_version_checksum_is_sha256"
        ),
        schema=SCHEMA,
        comment="Immutable recurrence definition versions. UPDATE/DELETE refused by trigger.",
    )

    op.create_table(
        "recurrence_analysis_run",
        _id(),
        *_timestamps(),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("definition_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("method_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("definition_name", sa.String(160), nullable=False),
        sa.Column("definition_parameters", postgresql.JSONB(), nullable=False),
        sa.Column("definition_checksum", sa.String(64), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("facility_refs", postgresql.JSONB(), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_checksum", sa.String(64), nullable=False),
        sa.Column("created_by_subject", sa.String(160), nullable=False),
        sa.Column("created_by_username", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_hash", sa.String(64), nullable=True),
        sa.Column("run_status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("progress_completed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("projections", postgresql.JSONB(), nullable=True),
        sa.Column("duplicate_payload", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_recurrence_analysis_run"),
        sa.ForeignKeyConstraint(
            ["definition_version_id"],
            [f"{SCHEMA}.recurrence_definition_version.id"],
            ondelete="RESTRICT",
            name="fk_recurrence_run_definition_version",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            ondelete="RESTRICT",
            name="fk_recurrence_run_method_version",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            [f"{SCHEMA}.clinical_evidence_dataset.id"],
            ondelete="RESTRICT",
            name="fk_recurrence_run_dataset",
        ),
        sa.UniqueConstraint(
            "created_by_subject",
            "idempotency_key",
            name="uq_recurrence_analysis_run_idempotency",
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name="ck_recurrence_analysis_run_period_ordered"
        ),
        sa.CheckConstraint(
            "mode IN ('exploratory', 'programme')", name="ck_recurrence_analysis_run_mode_known"
        ),
        sa.CheckConstraint(
            "run_status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_recurrence_analysis_run_status_known",
        ),
        sa.CheckConstraint(
            "source_kind IN ('stored_encounter', 'live_tracker')",
            name="ck_recurrence_analysis_run_source_kind_known",
        ),
        sa.CheckConstraint(
            "run_status <> 'completed' OR (dataset_id IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_recurrence_analysis_run_completed_run_has_dataset",
        ),
        sa.CheckConstraint(
            "mode <> 'programme' OR method_version_id IS NOT NULL",
            name="ck_recurrence_analysis_run_programme_run_names_its_method",
        ),
        sa.CheckConstraint(
            "length(manifest_checksum) = 64",
            name="ck_recurrence_analysis_run_manifest_is_sha256",
        ),
        schema=SCHEMA,
        comment=(
            "Recurrence analysis runs. The manifest is immutable by trigger; a "
            "completed or failed run is frozen entirely."
        ),
    )
    op.create_index(
        "ix_recurrence_analysis_run_scope",
        "recurrence_analysis_run",
        ["scope_key", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_recurrence_analysis_run_creator",
        "recurrence_analysis_run",
        ["created_by_subject", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "patient_recurrence_finding",
        _id(),
        *_timestamps(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_alias", sa.String(64), nullable=False),
        sa.Column("alias_scheme", sa.String(32), nullable=False),
        sa.Column("determination", sa.String(24), nullable=False),
        sa.Column("chain_length", sa.Integer(), nullable=False),
        sa.Column("eligible_positive_count", sa.Integer(), nullable=False),
        sa.Column("observed_positive_count", sa.Integer(), nullable=False),
        sa.Column("chain_interval_days", sa.Integer(), nullable=True),
        sa.Column("first_positive_on", sa.Date(), nullable=True),
        sa.Column("latest_positive_on", sa.Date(), nullable=True),
        sa.Column("final_date", sa.Date(), nullable=True),
        sa.Column("final_facility_ref", sa.String(64), nullable=True),
        sa.Column("in_period_facility_refs", postgresql.JSONB(), nullable=False),
        sa.Column("facility_refs", postgresql.JSONB(), nullable=False),
        sa.Column("quality_flags", postgresql.JSONB(), nullable=False),
        sa.Column("treatment_names", postgresql.JSONB(), nullable=False),
        sa.Column("test_methods", postgresql.JSONB(), nullable=False),
        sa.Column("cross_facility", sa.Boolean(), nullable=False),
        sa.Column("sex", sa.String(16), nullable=False),
        sa.Column("age_group", sa.String(16), nullable=False),
        sa.Column("detail", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_patient_recurrence_finding"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{SCHEMA}.recurrence_analysis_run.id"],
            ondelete="CASCADE",
            name="fk_patient_recurrence_finding_run",
        ),
        sa.UniqueConstraint("run_id", "patient_alias", name="uq_patient_recurrence_finding_alias"),
        sa.CheckConstraint(
            "determination IN ('qualifies', 'does_not_qualify', 'indeterminate', 'not_in_period')",
            name="ck_patient_recurrence_finding_determination_known",
        ),
        schema=SCHEMA,
        comment=(
            "Pseudonymous per-patient findings. Detail is encrypted; UPDATE is refused by trigger."
        ),
    )
    op.create_index(
        "ix_patient_recurrence_finding_run",
        "patient_recurrence_finding",
        ["run_id", "determination"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_patient_recurrence_finding_order",
        "patient_recurrence_finding",
        ["run_id", "latest_positive_on"],
        schema=SCHEMA,
    )

    op.execute(REJECT_MUTATION)
    op.execute(GUARD_RUN)
    for trigger, table, events in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE {events} ON {SCHEMA}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_recurrence_mutation()"
        )
    op.execute(
        f"CREATE TRIGGER recurrence_analysis_run_manifest_immutable "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.recurrence_analysis_run "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_recurrence_run()"
    )

    # Patient-origin investigations: an alternative, exclusive source.
    op.alter_column("investigation", "signal_id", nullable=True, schema="mars_core")
    op.add_column(
        "investigation",
        sa.Column("patient_finding_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="mars_core",
    )
    op.create_foreign_key(
        "fk_investigation_patient_finding",
        "investigation",
        "patient_recurrence_finding",
        ["patient_finding_id"],
        ["id"],
        source_schema="mars_core",
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_investigation_patient_finding",
        "investigation",
        ["patient_finding_id"],
        schema="mars_core",
    )
    op.create_check_constraint(
        "ck_investigation_exactly_one_source",
        "investigation",
        "(signal_id IS NULL) <> (patient_finding_id IS NULL)",
        schema="mars_core",
    )

    _grant()


def _grant() -> None:
    statements: list[str] = []
    for table, privileges in PRIVILEGES.items():
        qualified = f"{SCHEMA}.{table}"
        statements.append(f"REVOKE ALL ON {qualified} FROM {APP_ROLE}")
        statements.append(f"GRANT {', '.join(privileges)} ON {qualified} TO {APP_ROLE}")
    body = "\n".join(f"EXECUTE {statement!r};" for statement in statements)
    identity = "\n".join(
        f"EXECUTE {f'REVOKE ALL ON {SCHEMA}.{table} FROM {IDENTITY_ROLE}'!r};"
        for table in PRIVILEGES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                {body}
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{IDENTITY_ROLE}') THEN
                {identity}
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM mars_core.investigation WHERE patient_finding_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'Downgrade refused: patient-origin investigations exist and would lose '
                    'their source. Close and archive them under a supported procedure first.'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END
        $$;
        """
    )
    op.drop_constraint(
        "ck_investigation_exactly_one_source", "investigation", schema="mars_core", type_="check"
    )
    op.drop_constraint(
        "uq_investigation_patient_finding", "investigation", schema="mars_core", type_="unique"
    )
    op.drop_constraint(
        "fk_investigation_patient_finding",
        "investigation",
        schema="mars_core",
        type_="foreignkey",
    )
    op.drop_column("investigation", "patient_finding_id", schema="mars_core")
    op.alter_column("investigation", "signal_id", nullable=False, schema="mars_core")

    op.execute(
        f"DROP TRIGGER IF EXISTS recurrence_analysis_run_manifest_immutable "
        f"ON {SCHEMA}.recurrence_analysis_run"
    )
    for trigger, table, _events in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {SCHEMA}.{table}")
    op.drop_table("patient_recurrence_finding", schema=SCHEMA)
    op.drop_table("recurrence_analysis_run", schema=SCHEMA)
    op.drop_table("recurrence_definition_version", schema=SCHEMA)
    op.drop_table("recurrence_definition", schema=SCHEMA)
    op.drop_table("clinical_evidence_dataset", schema=SCHEMA)
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_recurrence_run()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_recurrence_mutation()")
