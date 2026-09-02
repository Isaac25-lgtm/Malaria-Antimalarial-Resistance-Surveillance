"""Core domain tables: audit, governance, security, geography and organisation.

Revision ID: 0002_core_domain
Revises: 0001_schema_baseline
Created: 2026-09-01

Creates every table required by phases 1 and 2:

* ``mars_audit``      append-only audit_event, plus the trigger that enforces it
* ``mars_governance`` configuration and method registries
* ``mars_security``   users, roles, permissions, geography and sensitivity scopes
* ``mars_core``       boundary versions, geography hierarchy, organisation units,
  facilities and facility identifiers

``mars_identity`` and ``mars_analytics`` stay empty. Their boundaries exist from
migration 0001; their contents arrive in later phases.

Enum types are created explicitly rather than implicitly by the column
definitions, because two tables share ``lifecycle_status`` and implicit creation
would attempt it twice. The label lists are generated from the same Python enums
the models use, so the type and the model cannot disagree.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_core_domain"
down_revision: str | None = "0001_schema_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Rejects UPDATE and DELETE on the audit trail at the database level, so a code
# path that bypasses the ORM listeners still cannot rewrite history.
AUDIT_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION mars_audit.reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'mars_audit.audit_event is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

AUDIT_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER audit_event_append_only
BEFORE UPDATE OR DELETE ON mars_audit.audit_event
FOR EACH ROW EXECUTE FUNCTION mars_audit.reject_audit_mutation();
"""

AUDIT_TABLE_COMMENT = (
    "COMMENT ON TABLE mars_audit.audit_event IS "
    "'Append-only. UPDATE and DELETE are rejected by trigger.'"
)


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    postgresql.ENUM(
        "login_succeeded",
        "login_failed",
        "logout",
        "access_denied",
        "role_assigned",
        "role_revoked",
        "permission_changed",
        "geography_scope_changed",
        "sensitivity_scope_changed",
        "configuration_changed",
        "configuration_activated",
        "method_registered",
        "method_promoted",
        "method_rolled_back",
        "geography_imported",
        "organisation_unit_changed",
        "facility_changed",
        "data_imported",
        "signal_created",
        "signal_triaged",
        "investigation_updated",
        "case_evidence_accessed",
        "reidentification_performed",
        "export_generated",
        "report_generated",
        "ai_request_submitted",
        name="audit_action",
        schema="mars_audit",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "succeeded",
        "denied",
        "failed",
        name="audit_outcome",
        schema="mars_audit",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "proposed",
        "confirmed",
        "ambiguous",
        "rejected",
        name="alias_match_status",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "registered",
        "validating",
        "validation_failed",
        "imported",
        "published",
        "superseded",
        name="boundary_import_status",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "unknown",
        "hc_ii",
        "hc_iii",
        "hc_iv",
        "general_hospital",
        "regional_referral_hospital",
        "national_referral_hospital",
        "specialised_clinic",
        name="facility_level",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "unknown",
        "government",
        "private_not_for_profit",
        "private_for_profit",
        "community",
        name="facility_ownership",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "country",
        "region",
        "district",
        "county",
        "subcounty",
        "parish",
        "village",
        name="geography_level",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "unspecified",
        "rural_subcounty",
        "town_council",
        "urban_division",
        "municipality",
        "city",
        name="geography_unit_kind",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "not_assessed",
        "valid",
        "invalid_repaired",
        "invalid_unrepaired",
        name="geometry_validity_state",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "national",
        "regional_referral",
        "district_health_office",
        "health_sub_district",
        "facility",
        name="organisation_unit_type",
        schema="mars_core",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "draft",
        "in_review",
        "approved",
        "active",
        "retired",
        "rejected",
        name="lifecycle_status",
        schema="mars_governance",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "indicator_definition",
        "episode_rule",
        "temporal_baseline",
        "spatial_method",
        "signal_rule",
        "signal_score",
        "data_quality_rule",
        "statistical_model",
        "machine_learning_model",
        name="method_kind",
        schema="mars_governance",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "surveillance:view_aggregate",
        "case:view_pseudonymous_evidence",
        "patient:reidentify",
        "data:export",
        "report:generate",
        "investigation:triage",
        "investigation:assign",
        "investigation:update",
        "investigation:close",
        "configuration:view",
        "configuration:manage",
        "method:view",
        "method:approve",
        "geography:view",
        "geography:manage",
        "organisation:view",
        "organisation:manage",
        "facility:view",
        "facility:manage",
        "integration:manage",
        "user:administer",
        "audit:view",
        "data_quality:view",
        name="permission_code",
        schema="mars_security",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "aggregate",
        "pseudonymous_case",
        "direct_identity",
        name="sensitivity_level",
        schema="mars_security",
    ).create(bind, checkfirst=True)

    # -- Tables and indexes -----------------------------------------------
    op.create_table(
        "audit_event",
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("actor_label", sa.String(length=128), nullable=True),
        sa.Column(
            "action",
            postgresql.ENUM(
                "login_succeeded",
                "login_failed",
                "logout",
                "access_denied",
                "role_assigned",
                "role_revoked",
                "permission_changed",
                "geography_scope_changed",
                "sensitivity_scope_changed",
                "configuration_changed",
                "configuration_activated",
                "method_registered",
                "method_promoted",
                "method_rolled_back",
                "geography_imported",
                "organisation_unit_changed",
                "facility_changed",
                "data_imported",
                "signal_created",
                "signal_triaged",
                "investigation_updated",
                "case_evidence_accessed",
                "reidentification_performed",
                "export_generated",
                "report_generated",
                "ai_request_submitted",
                name="audit_action",
                schema="mars_audit",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            postgresql.ENUM(
                "succeeded",
                "denied",
                "failed",
                name="audit_outcome",
                schema="mars_audit",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("object_type", sa.String(length=64), nullable=True),
        sa.Column("object_id", sa.String(length=128), nullable=True),
        sa.Column("geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("geography_code", sa.String(length=32), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("before_ref", sa.String(length=256), nullable=True),
        sa.Column("after_ref", sa.String(length=256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "actor_user_id IS NOT NULL OR actor_kind <> 'user'",
            name=op.f("ck_audit_event_actor_user_required_for_user_events"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
        schema="mars_audit",
    )
    op.create_index(
        "ix_audit_event_action_occurred",
        "audit_event",
        ["action", "occurred_at"],
        unique=False,
        schema="mars_audit",
    )
    op.create_index(
        "ix_audit_event_actor_occurred",
        "audit_event",
        ["actor_user_id", "occurred_at"],
        unique=False,
        schema="mars_audit",
    )
    op.create_index(
        "ix_audit_event_object",
        "audit_event",
        ["object_type", "object_id"],
        unique=False,
        schema="mars_audit",
    )
    op.create_index(
        "ix_audit_event_occurred_at",
        "audit_event",
        ["occurred_at"],
        unique=False,
        schema="mars_audit",
    )
    op.create_index(
        "ix_audit_event_request_id",
        "audit_event",
        ["request_id"],
        unique=False,
        schema="mars_audit",
    )
    op.create_table(
        "boundary_version",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_file_name", sa.String(length=255), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=True),
        sa.Column("source_format", sa.String(length=32), nullable=True),
        sa.Column("source_retrieved_on", sa.Date(), nullable=True),
        sa.Column("source_crs", sa.String(length=32), nullable=True),
        sa.Column("storage_crs", sa.String(length=32), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "import_status",
            postgresql.ENUM(
                "registered",
                "validating",
                "validation_failed",
                "imported",
                "published",
                "superseded",
                name="boundary_import_status",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_by", sa.String(length=128), nullable=True),
        sa.Column("validation_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_boundary_version_effective_range_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_boundary_version")),
        sa.UniqueConstraint("code", name="uq_boundary_version_code"),
        schema="mars_core",
    )
    op.create_index(
        "ix_boundary_version_status",
        "boundary_version",
        ["import_status"],
        unique=False,
        schema="mars_core",
    )
    op.create_table(
        "configuration_key",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("value_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requires_programme_approval", sa.Boolean(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_configuration_key")),
        sa.UniqueConstraint("key", name="uq_configuration_key_key"),
        schema="mars_governance",
    )
    op.create_table(
        "method_definition",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "indicator_definition",
                "episode_rule",
                "temporal_baseline",
                "spatial_method",
                "signal_rule",
                "signal_score",
                "data_quality_rule",
                "statistical_model",
                "machine_learning_model",
                name="method_kind",
                schema="mars_governance",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_method_definition")),
        sa.UniqueConstraint("code", name="uq_method_definition_code"),
        schema="mars_governance",
    )
    op.create_table(
        "role",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_system_role", sa.Boolean(), nullable=False),
        sa.Column(
            "max_sensitivity",
            postgresql.ENUM(
                "aggregate",
                "pseudonymous_case",
                "direct_identity",
                name="sensitivity_level",
                schema="mars_security",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role")),
        sa.UniqueConstraint("code", name="uq_role_code"),
        schema="mars_security",
    )
    op.create_table(
        "user_account",
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("organisation_label", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_account")),
        sa.UniqueConstraint("subject", name="uq_user_account_subject"),
        sa.UniqueConstraint("username", name="uq_user_account_username"),
        schema="mars_security",
    )
    op.create_index(
        "ix_user_account_is_active",
        "user_account",
        ["is_active"],
        unique=False,
        schema="mars_security",
    )
    op.create_table(
        "geography_unit",
        sa.Column(
            "level",
            postgresql.ENUM(
                "country",
                "region",
                "district",
                "county",
                "subcounty",
                "parish",
                "village",
                name="geography_level",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "unit_kind",
            postgresql.ENUM(
                "unspecified",
                "rural_subcounty",
                "town_council",
                "urban_division",
                "municipality",
                "city",
                name="geography_unit_kind",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("preferred_code", sa.String(length=32), nullable=False),
        sa.Column("raw_name", sa.String(length=255), nullable=False),
        sa.Column("normalised_name", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.Column("source_system", sa.String(length=64), nullable=True),
        sa.Column("source_record_id", sa.String(length=128), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "(level = 'country' AND parent_id IS NULL) OR (level <> 'country' AND parent_id IS NOT NULL)",
            name=op.f("ck_geography_unit_only_country_is_rootless"),
        ),
        sa.CheckConstraint(
            "depth >= 0 AND depth <= 6", name=op.f("ck_geography_unit_depth_within_hierarchy")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_geography_unit_effective_range_ordered"),
        ),
        sa.CheckConstraint("id <> parent_id", name=op.f("ck_geography_unit_no_self_parent")),
        sa.ForeignKeyConstraint(
            ["boundary_version_id"],
            ["mars_core.boundary_version.id"],
            name=op.f("fk_geography_unit_boundary_version_id_boundary_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_geography_unit_parent_id_geography_unit"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geography_unit")),
        sa.UniqueConstraint(
            "level", "preferred_code", name="uq_geography_unit_level_preferred_code"
        ),
        sa.UniqueConstraint(
            "parent_id",
            "level",
            "normalised_name",
            name="uq_geography_unit_parent_id_level_normalised_name",
        ),
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_boundary_version_id",
        "geography_unit",
        ["boundary_version_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_effective",
        "geography_unit",
        ["effective_from", "effective_to"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_is_active",
        "geography_unit",
        ["is_active"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_level", "geography_unit", ["level"], unique=False, schema="mars_core"
    )
    op.create_index(
        "ix_geography_unit_normalised_name",
        "geography_unit",
        ["normalised_name"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_parent_id",
        "geography_unit",
        ["parent_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_table(
        "configuration_version",
        sa.Column("configuration_key_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "in_review",
                "approved",
                "active",
                "retired",
                "rejected",
                name="lifecycle_status",
                schema="mars_governance",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("value_checksum", sa.String(length=64), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("reason_for_change", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provenance", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "status <> 'active' OR effective_from IS NOT NULL",
            name=op.f("ck_configuration_version_active_requires_effective_from"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'active') OR approved_by IS NOT NULL",
            name=op.f("ck_configuration_version_approved_requires_approver"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_configuration_version_effective_range_ordered"),
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_configuration_version_version_number_positive")
        ),
        sa.ForeignKeyConstraint(
            ["configuration_key_id"],
            ["mars_governance.configuration_key.id"],
            name=op.f("fk_configuration_version_configuration_key_id_configuration_key"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_configuration_version")),
        sa.UniqueConstraint(
            "configuration_key_id",
            "version_number",
            name="uq_configuration_version_configuration_key_id_version_number",
        ),
        schema="mars_governance",
    )
    op.create_index(
        "ix_configuration_version_key_status",
        "configuration_version",
        ["configuration_key_id", "status"],
        unique=False,
        schema="mars_governance",
    )
    op.create_table(
        "method_version",
        sa.Column("method_definition_id", sa.UUID(), nullable=False),
        sa.Column("semantic_version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "in_review",
                "approved",
                "active",
                "retired",
                "rejected",
                name="lifecycle_status",
                schema="mars_governance",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation_reference", sa.String(length=512), nullable=True),
        sa.Column("validation_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("artifact_checksum", sa.String(length=64), nullable=True),
        sa.Column("artifact_reference", sa.String(length=512), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_from_id", sa.UUID(), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "status NOT IN ('approved', 'active') OR approved_by IS NOT NULL",
            name=op.f("ck_method_version_approved_requires_approver"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_method_version_effective_range_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["method_definition_id"],
            ["mars_governance.method_definition.id"],
            name=op.f("fk_method_version_method_definition_id_method_definition"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rolled_back_from_id"],
            ["mars_governance.method_version.id"],
            name=op.f("fk_method_version_rolled_back_from_id_method_version"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_method_version")),
        sa.UniqueConstraint(
            "method_definition_id",
            "semantic_version",
            name="uq_method_version_method_definition_id_semantic_version",
        ),
        schema="mars_governance",
    )
    op.create_index(
        "ix_method_version_definition_status",
        "method_version",
        ["method_definition_id", "status"],
        unique=False,
        schema="mars_governance",
    )
    op.create_table(
        "role_permission",
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column(
            "permission",
            postgresql.ENUM(
                "surveillance:view_aggregate",
                "case:view_pseudonymous_evidence",
                "patient:reidentify",
                "data:export",
                "report:generate",
                "investigation:triage",
                "investigation:assign",
                "investigation:update",
                "investigation:close",
                "configuration:view",
                "configuration:manage",
                "method:view",
                "method:approve",
                "geography:view",
                "geography:manage",
                "organisation:view",
                "organisation:manage",
                "facility:view",
                "facility:manage",
                "integration:manage",
                "user:administer",
                "audit:view",
                "data_quality:view",
                name="permission_code",
                schema="mars_security",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["mars_security.role.id"],
            name=op.f("fk_role_permission_role_id_role"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_permission")),
        sa.UniqueConstraint("role_id", "permission", name="uq_role_permission_role_id_permission"),
        schema="mars_security",
    )
    op.create_table(
        "user_facility_scope",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("facility_id", sa.UUID(), nullable=False),
        sa.Column("granted_by", sa.String(length=128), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_user_facility_scope_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_facility_scope")),
        sa.UniqueConstraint(
            "user_id", "facility_id", name="uq_user_facility_scope_user_id_facility_id"
        ),
        schema="mars_security",
    )
    op.create_index(
        "ix_user_facility_scope_user_id",
        "user_facility_scope",
        ["user_id"],
        unique=False,
        schema="mars_security",
    )
    op.create_table(
        "user_geography_scope",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("geography_unit_id", sa.UUID(), nullable=False),
        sa.Column("granted_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_user_geography_scope_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_geography_scope")),
        sa.UniqueConstraint(
            "user_id", "geography_unit_id", name="uq_user_geography_scope_user_id_geography_unit_id"
        ),
        schema="mars_security",
    )
    op.create_index(
        "ix_user_geography_scope_user_id",
        "user_geography_scope",
        ["user_id"],
        unique=False,
        schema="mars_security",
    )
    op.create_table(
        "user_role",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("granted_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name=op.f("ck_user_role_validity_range_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["mars_security.role.id"],
            name=op.f("fk_user_role_role_id_role"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_user_role_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_role")),
        sa.UniqueConstraint("user_id", "role_id", name="uq_user_role_user_id_role_id"),
        schema="mars_security",
    )
    op.create_table(
        "user_sensitivity_scope",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "max_sensitivity",
            postgresql.ENUM(
                "aggregate",
                "pseudonymous_case",
                "direct_identity",
                name="sensitivity_level",
                schema="mars_security",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("granted_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("review_due_on", sa.Date(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "max_sensitivity <> 'direct_identity' OR reason IS NOT NULL",
            name=op.f("ck_user_sensitivity_scope_direct_identity_requires_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_user_sensitivity_scope_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sensitivity_scope")),
        sa.UniqueConstraint("user_id", name="uq_user_sensitivity_scope_user_id"),
        schema="mars_security",
    )
    op.create_table(
        "user_session",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("session_reference", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_user_session_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        schema="mars_security",
    )
    op.create_index(
        "ix_user_session_user_id_started_at",
        "user_session",
        ["user_id", "started_at"],
        unique=False,
        schema="mars_security",
    )
    op.create_table(
        "geography_unit_alias",
        sa.Column("geography_unit_id", sa.UUID(), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_code", sa.String(length=128), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_level", sa.String(length=32), nullable=True),
        sa.Column(
            "match_status",
            postgresql.ENUM(
                "proposed",
                "confirmed",
                "ambiguous",
                "rejected",
                name="alias_match_status",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("match_method", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_geography_unit_alias_effective_range_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_geography_unit_alias_geography_unit_id_geography_unit"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geography_unit_alias")),
        sa.UniqueConstraint(
            "source_system",
            "source_code",
            "geography_unit_id",
            name="uq_geography_unit_alias_source_and_unit",
        ),
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_alias_geography_unit_id",
        "geography_unit_alias",
        ["geography_unit_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_alias_match_status",
        "geography_unit_alias",
        ["match_status"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_alias_source",
        "geography_unit_alias",
        ["source_system", "source_code"],
        unique=False,
        schema="mars_core",
    )
    op.create_table(
        "geography_unit_geometry",
        sa.Column("geography_unit_id", sa.UUID(), nullable=False),
        sa.Column(
            "validity_state",
            postgresql.ENUM(
                "not_assessed",
                "valid",
                "invalid_repaired",
                "invalid_unrepaired",
                name="geometry_validity_state",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("validity_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("repair_method", sa.String(length=64), nullable=True),
        sa.Column("ring_count", sa.Integer(), nullable=True),
        sa.Column("vertex_count", sa.Integer(), nullable=True),
        sa.Column("part_count", sa.Integer(), nullable=True),
        sa.Column("area_sq_km", sa.Float(), nullable=True),
        sa.Column("perimeter_km", sa.Float(), nullable=True),
        sa.Column("bbox_min_lon", sa.Float(), nullable=True),
        sa.Column("bbox_min_lat", sa.Float(), nullable=True),
        sa.Column("bbox_max_lon", sa.Float(), nullable=True),
        sa.Column("bbox_max_lat", sa.Float(), nullable=True),
        sa.Column("simplification_tolerance_deg", sa.Float(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_geography_unit_geometry_geography_unit_id_geography_unit"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geography_unit_geometry")),
        sa.UniqueConstraint(
            "geography_unit_id", name="uq_geography_unit_geometry_geography_unit_id"
        ),
        schema="mars_core",
    )
    op.create_index(
        "ix_geography_unit_geometry_validity",
        "geography_unit_geometry",
        ["validity_state"],
        unique=False,
        schema="mars_core",
    )
    op.create_table(
        "organisation_unit",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("raw_name", sa.String(length=255), nullable=False),
        sa.Column("normalised_name", sa.String(length=255), nullable=False),
        sa.Column(
            "unit_type",
            postgresql.ENUM(
                "national",
                "regional_referral",
                "district_health_office",
                "health_sub_district",
                "facility",
                name="organisation_unit_type",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("primary_geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.Column("source_system", sa.String(length=64), nullable=True),
        sa.Column("source_record_id", sa.String(length=128), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "depth >= 0 AND depth <= 8", name=op.f("ck_organisation_unit_depth_within_hierarchy")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_organisation_unit_effective_range_ordered"),
        ),
        sa.CheckConstraint("id <> parent_id", name=op.f("ck_organisation_unit_no_self_parent")),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["mars_core.organisation_unit.id"],
            name=op.f("fk_organisation_unit_parent_id_organisation_unit"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["primary_geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_organisation_unit_primary_geography_unit_id_geography_unit"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisation_unit")),
        sa.UniqueConstraint("code", name="uq_organisation_unit_code"),
        schema="mars_core",
    )
    op.create_index(
        "ix_organisation_unit_effective",
        "organisation_unit",
        ["effective_from", "effective_to"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_organisation_unit_geography",
        "organisation_unit",
        ["primary_geography_unit_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_organisation_unit_parent_id",
        "organisation_unit",
        ["parent_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_organisation_unit_unit_type",
        "organisation_unit",
        ["unit_type"],
        unique=False,
        schema="mars_core",
    )
    op.create_table(
        "facility",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("raw_name", sa.String(length=255), nullable=False),
        sa.Column("normalised_name", sa.String(length=255), nullable=False),
        sa.Column(
            "facility_level",
            postgresql.ENUM(
                "unknown",
                "hc_ii",
                "hc_iii",
                "hc_iv",
                "general_hospital",
                "regional_referral_hospital",
                "national_referral_hospital",
                "specialised_clinic",
                name="facility_level",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "ownership",
            postgresql.ENUM(
                "unknown",
                "government",
                "private_not_for_profit",
                "private_for_profit",
                "community",
                name="facility_ownership",
                schema="mars_core",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("organisation_unit_id", sa.UUID(), nullable=True),
        sa.Column("district_geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("subcounty_geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("coordinate_source", sa.String(length=64), nullable=True),
        sa.Column("coordinate_validated", sa.Boolean(), nullable=False),
        sa.Column("opened_on", sa.Date(), nullable=True),
        sa.Column("closed_on", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.Column("source_system", sa.String(length=64), nullable=True),
        sa.Column("source_record_id", sa.String(length=128), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR (latitude IS NOT NULL AND longitude IS NOT NULL)",
            name=op.f("ck_facility_coordinates_paired"),
        ),
        sa.CheckConstraint(
            "closed_on IS NULL OR opened_on IS NULL OR closed_on >= opened_on",
            name=op.f("ck_facility_operating_range_ordered"),
        ),
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name=op.f("ck_facility_latitude_in_range"),
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name=op.f("ck_facility_longitude_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["district_geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_facility_district_geography_unit_id_geography_unit"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_unit_id"],
            ["mars_core.organisation_unit.id"],
            name=op.f("fk_facility_organisation_unit_id_organisation_unit"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subcounty_geography_unit_id"],
            ["mars_core.geography_unit.id"],
            name=op.f("fk_facility_subcounty_geography_unit_id_geography_unit"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_facility")),
        sa.UniqueConstraint("code", name="uq_facility_code"),
        schema="mars_core",
    )
    op.create_index(
        "ix_facility_district_geography_unit_id",
        "facility",
        ["district_geography_unit_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_facility_is_active", "facility", ["is_active"], unique=False, schema="mars_core"
    )
    op.create_index(
        "ix_facility_level", "facility", ["facility_level"], unique=False, schema="mars_core"
    )
    op.create_index(
        "ix_facility_normalised_name",
        "facility",
        ["normalised_name"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_facility_organisation_unit_id",
        "facility",
        ["organisation_unit_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_facility_subcounty_geography_unit_id",
        "facility",
        ["subcounty_geography_unit_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_table(
        "facility_identifier",
        sa.Column("facility_id", sa.UUID(), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("external_name", sa.String(length=255), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_facility_identifier_effective_range_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["mars_core.facility.id"],
            name=op.f("fk_facility_identifier_facility_id_facility"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_facility_identifier")),
        sa.UniqueConstraint(
            "source_system", "external_id", name="uq_facility_identifier_source_system_external_id"
        ),
        schema="mars_core",
    )
    op.create_index(
        "ix_facility_identifier_facility_id",
        "facility_identifier",
        ["facility_id"],
        unique=False,
        schema="mars_core",
    )
    op.create_index(
        "ix_facility_identifier_source",
        "facility_identifier",
        ["source_system", "external_id"],
        unique=False,
        schema="mars_core",
    )

    # -- Audit immutability -----------------------------------------------
    op.execute(AUDIT_IMMUTABILITY_FUNCTION)
    op.execute(AUDIT_IMMUTABILITY_TRIGGER)
    op.execute(AUDIT_TABLE_COMMENT)


def downgrade() -> None:
    bind = op.get_bind()

    op.execute("DROP TRIGGER IF EXISTS audit_event_append_only ON mars_audit.audit_event")
    op.execute("DROP FUNCTION IF EXISTS mars_audit.reject_audit_mutation()")

    op.drop_index(
        "ix_facility_identifier_source", table_name="facility_identifier", schema="mars_core"
    )
    op.drop_index(
        "ix_facility_identifier_facility_id", table_name="facility_identifier", schema="mars_core"
    )
    op.drop_table("facility_identifier", schema="mars_core", create_type=False)
    op.drop_index(
        "ix_facility_subcounty_geography_unit_id", table_name="facility", schema="mars_core"
    )
    op.drop_index(
        "ix_facility_organisation_unit_id",
        table_name="facility",
        schema="mars_core",
        create_type=False,
    )
    op.drop_index("ix_facility_normalised_name", table_name="facility", schema="mars_core")
    op.drop_index("ix_facility_level", table_name="facility", schema="mars_core")
    op.drop_index("ix_facility_is_active", table_name="facility", schema="mars_core")
    op.drop_index(
        "ix_facility_district_geography_unit_id", table_name="facility", schema="mars_core"
    )
    op.drop_table("facility", schema="mars_core")
    op.drop_index(
        "ix_organisation_unit_unit_type", table_name="organisation_unit", schema="mars_core"
    )
    op.drop_index(
        "ix_organisation_unit_parent_id", table_name="organisation_unit", schema="mars_core"
    )
    op.drop_index(
        "ix_organisation_unit_geography", table_name="organisation_unit", schema="mars_core"
    )
    op.drop_index(
        "ix_organisation_unit_effective", table_name="organisation_unit", schema="mars_core"
    )
    op.drop_table("organisation_unit", schema="mars_core")
    op.drop_index(
        "ix_geography_unit_geometry_validity",
        table_name="geography_unit_geometry",
        schema="mars_core",
    )
    op.drop_table("geography_unit_geometry", schema="mars_core")
    op.drop_index(
        "ix_geography_unit_alias_source", table_name="geography_unit_alias", schema="mars_core"
    )
    op.drop_index(
        "ix_geography_unit_alias_match_status",
        table_name="geography_unit_alias",
        schema="mars_core",
    )
    op.drop_index(
        "ix_geography_unit_alias_geography_unit_id",
        table_name="geography_unit_alias",
        schema="mars_core",
    )
    op.drop_table("geography_unit_alias", schema="mars_core")
    op.drop_index(
        "ix_user_session_user_id_started_at", table_name="user_session", schema="mars_security"
    )
    op.drop_table("user_session", schema="mars_security")
    op.drop_table("user_sensitivity_scope", schema="mars_security")
    op.drop_table("user_role", schema="mars_security")
    op.drop_index(
        "ix_user_geography_scope_user_id", table_name="user_geography_scope", schema="mars_security"
    )
    op.drop_table("user_geography_scope", schema="mars_security")
    op.drop_index(
        "ix_user_facility_scope_user_id", table_name="user_facility_scope", schema="mars_security"
    )
    op.drop_table("user_facility_scope", schema="mars_security")
    op.drop_table("role_permission", schema="mars_security")
    op.drop_index(
        "ix_method_version_definition_status", table_name="method_version", schema="mars_governance"
    )
    op.drop_table("method_version", schema="mars_governance")
    op.drop_index(
        "ix_configuration_version_key_status",
        table_name="configuration_version",
        schema="mars_governance",
    )
    op.drop_table("configuration_version", schema="mars_governance")
    op.drop_index("ix_geography_unit_parent_id", table_name="geography_unit", schema="mars_core")
    op.drop_index(
        "ix_geography_unit_normalised_name", table_name="geography_unit", schema="mars_core"
    )
    op.drop_index("ix_geography_unit_level", table_name="geography_unit", schema="mars_core")
    op.drop_index("ix_geography_unit_is_active", table_name="geography_unit", schema="mars_core")
    op.drop_index("ix_geography_unit_effective", table_name="geography_unit", schema="mars_core")
    op.drop_index(
        "ix_geography_unit_boundary_version_id", table_name="geography_unit", schema="mars_core"
    )
    op.drop_table("geography_unit", schema="mars_core")
    op.drop_index("ix_user_account_is_active", table_name="user_account", schema="mars_security")
    op.drop_table("user_account", schema="mars_security")
    op.drop_table("role", schema="mars_security")
    op.drop_table("method_definition", schema="mars_governance")
    op.drop_table("configuration_key", schema="mars_governance")
    op.drop_index("ix_boundary_version_status", table_name="boundary_version", schema="mars_core")
    op.drop_table("boundary_version", schema="mars_core")
    op.drop_index("ix_audit_event_request_id", table_name="audit_event", schema="mars_audit")
    op.drop_index("ix_audit_event_occurred_at", table_name="audit_event", schema="mars_audit")
    op.drop_index("ix_audit_event_object", table_name="audit_event", schema="mars_audit")
    op.drop_index("ix_audit_event_actor_occurred", table_name="audit_event", schema="mars_audit")
    op.drop_index("ix_audit_event_action_occurred", table_name="audit_event", schema="mars_audit")
    op.drop_table("audit_event", schema="mars_audit")

    # -- Enum types -------------------------------------------------------
    postgresql.ENUM(name="sensitivity_level", schema="mars_security").drop(bind, checkfirst=True)
    postgresql.ENUM(name="permission_code", schema="mars_security").drop(bind, checkfirst=True)
    postgresql.ENUM(name="method_kind", schema="mars_governance").drop(bind, checkfirst=True)
    postgresql.ENUM(name="lifecycle_status", schema="mars_governance").drop(bind, checkfirst=True)
    postgresql.ENUM(name="organisation_unit_type", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="geometry_validity_state", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="geography_unit_kind", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="geography_level", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="facility_ownership", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="facility_level", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="boundary_import_status", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="alias_match_status", schema="mars_core").drop(bind, checkfirst=True)
    postgresql.ENUM(name="audit_outcome", schema="mars_audit").drop(bind, checkfirst=True)
    postgresql.ENUM(name="audit_action", schema="mars_audit").drop(bind, checkfirst=True)
