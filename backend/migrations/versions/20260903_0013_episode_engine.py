"""Malaria episode candidates.

Revision ID: 0013_episode_engine
Revises: 0012_indicator_registry
Created: 2026-09-03

Three tables in mars_analytics:

``episode_build``      one run of the episode engine and what it read
``episode_candidate``  one patient's encounters that may be a single illness
``episode_member``     one encounter's place in that timeline

Every column name here is careful about what routine data can support. An
episode is a *candidate*: routine data cannot distinguish recrudescence from
reinfection, and a prescription line records what was prescribed rather than
what a patient received or took. Nothing in these tables is a clinical
conclusion, and the API and explanation layers are forbidden from presenting
one on their behalf.

``episode_build.rule_version_id`` is nullable so a run made with no approved
episode rule can be recorded as ``not_configured``. That is a governance fact
worth storing: MARS supplies no episode window, because whether two positive
results are one illness or two depends on the drug, the setting and the
programme's guidance, and no defensible universal answer exists.

``episode_member.days_since_previous`` stores actual days, never a band.
Interval bands are governed configuration; an interval recorded as a band
cannot be re-banded when the programme changes them.

Constraints worth naming:

* ``span_matches_dates`` - a span is a consequence of its dates, and a stored
  contradiction would let a query disagree with the timeline it comes from.
* ``positives_within_encounters`` - more positive results than encounters is
  arithmetically impossible.
* the build's uniqueness key includes ``input_fingerprint``, so re-running over
  unchanged evidence is idempotent while a corrected encounter produces a new
  build rather than silently altering episodes a clinician has read.

Grouping is by ``patient_reference_id`` only. No direct identifier is read and
the identity vault is never queried.

Documented in ``docs/data-dictionary/episodes.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_episode_engine"
down_revision: str | None = "0012_indicator_registry"
ANALYTICS = "mars_analytics"
CORE = "mars_core"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    postgresql.ENUM(
        "running",
        "completed",
        "not_configured",
        "failed",
        name="episode_build_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "candidate",
        "open_at_period_end",
        "qualified",
        name="episode_status",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "index",
        "follow_up",
        "repeat_positive",
        name="episode_encounter_role",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "episode_build",
        sa.Column("rule_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "build_status",
            postgresql.ENUM(
                "running",
                "completed",
                "not_configured",
                "failed",
                name="episode_build_status",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("encounters_considered", sa.Integer(), nullable=False),
        sa.Column("encounters_unlinked", sa.Integer(), nullable=False),
        sa.Column("episodes_created", sa.Integer(), nullable=False),
        sa.Column("patients_considered", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "encounters_considered >= 0", name=op.f("ck_episode_build_considered_not_negative")
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64", name=op.f("ck_episode_build_fingerprint_is_sha256")
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_episode_build_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["mars_governance.method_version.id"],
            name="fk_episode_build_rule",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episode_build")),
        sa.UniqueConstraint(
            "rule_version_id",
            "input_fingerprint",
            "period_start",
            "period_end",
            name="uq_episode_build_rule_input_period",
        ),
        schema=ANALYTICS,
        comment="One episode-engine run. An episode's meaning depends on the rule version that built it, so the run is part of the record.",
    )
    op.create_index(
        "ix_episode_build_period",
        "episode_build",
        ["period_start", "period_end"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_episode_build_status", "episode_build", ["build_status"], unique=False, schema=ANALYTICS
    )
    op.create_table(
        "episode_candidate",
        sa.Column("episode_build_id", sa.UUID(), nullable=False),
        sa.Column("patient_reference_id", sa.UUID(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column(
            "episode_status",
            postgresql.ENUM(
                "candidate",
                "open_at_period_end",
                "qualified",
                name="episode_status",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("first_encounter_date", sa.Date(), nullable=False),
        sa.Column("last_encounter_date", sa.Date(), nullable=False),
        sa.Column("span_days", sa.Integer(), nullable=False),
        sa.Column("encounter_count", sa.Integer(), nullable=False),
        sa.Column("positive_encounter_count", sa.Integer(), nullable=False),
        sa.Column("tested_encounter_count", sa.Integer(), nullable=False),
        sa.Column("treated_encounter_count", sa.Integer(), nullable=False),
        sa.Column("index_facility_id", sa.UUID(), nullable=True),
        sa.Column("residence_district_id", sa.UUID(), nullable=True),
        sa.Column("residence_subcounty_id", sa.UUID(), nullable=True),
        sa.Column("uncertainty", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "encounter_count >= 1", name=op.f("ck_episode_candidate_encounter_count_is_positive")
        ),
        sa.CheckConstraint(
            "episode_number >= 1", name=op.f("ck_episode_candidate_episode_number_is_positive")
        ),
        sa.CheckConstraint(
            "last_encounter_date >= first_encounter_date",
            name=op.f("ck_episode_candidate_dates_ordered"),
        ),
        sa.CheckConstraint(
            "positive_encounter_count >= 0 AND positive_encounter_count <= encounter_count",
            name=op.f("ck_episode_candidate_positives_within_encounters"),
        ),
        sa.CheckConstraint(
            "span_days = (last_encounter_date - first_encounter_date)",
            name=op.f("ck_episode_candidate_span_matches_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["episode_build_id"],
            ["mars_analytics.episode_build.id"],
            name="fk_episode_build",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["index_facility_id"],
            ["mars_core.facility.id"],
            name="fk_episode_facility",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["patient_reference_id"],
            ["mars_core.patient_reference.id"],
            name="fk_episode_patient",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episode_candidate")),
        sa.UniqueConstraint(
            "episode_build_id",
            "patient_reference_id",
            "episode_number",
            name="uq_episode_candidate_build_patient_number",
        ),
        schema=ANALYTICS,
        comment="A grouping of one pseudonymous patient's encounters that may be one illness. A candidate, never a clinical conclusion: routine data cannot distinguish recrudescence from reinfection or establish drug exposure.",
    )
    op.create_index(
        "ix_episode_candidate_build",
        "episode_candidate",
        ["episode_build_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_episode_candidate_dates",
        "episode_candidate",
        ["first_encounter_date", "last_encounter_date"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_episode_candidate_facility",
        "episode_candidate",
        ["index_facility_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_episode_candidate_patient",
        "episode_candidate",
        ["patient_reference_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "episode_member",
        sa.Column("episode_candidate_id", sa.UUID(), nullable=False),
        sa.Column("opd_encounter_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "member_role",
            postgresql.ENUM(
                "index",
                "follow_up",
                "repeat_positive",
                name="episode_encounter_role",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("encounter_date", sa.Date(), nullable=False),
        sa.Column("days_since_previous", sa.Integer(), nullable=True),
        sa.Column("test_method", sa.String(length=32), nullable=True),
        sa.Column("test_result", sa.String(length=32), nullable=True),
        sa.Column("attendance_type", sa.String(length=32), nullable=True),
        sa.Column("antimalarial_recorded", sa.Boolean(), nullable=False),
        sa.Column("facility_id", sa.UUID(), nullable=True),
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
            "days_since_previous IS NULL OR days_since_previous >= 0",
            name=op.f("ck_episode_member_interval_not_negative"),
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_episode_member_sequence_is_positive")),
        sa.ForeignKeyConstraint(
            ["episode_candidate_id"],
            ["mars_analytics.episode_candidate.id"],
            name="fk_member_episode",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opd_encounter_id"],
            ["mars_core.opd_encounter.id"],
            name="fk_member_encounter",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episode_member")),
        sa.UniqueConstraint(
            "episode_candidate_id", "opd_encounter_id", name="uq_episode_member_episode_encounter"
        ),
        sa.UniqueConstraint(
            "episode_candidate_id", "sequence", name="uq_episode_member_episode_sequence"
        ),
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_episode_member_encounter",
        "episode_member",
        ["opd_encounter_id"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_episode_member_encounter", table_name="episode_member", schema=ANALYTICS)
    op.drop_table("episode_member", schema=ANALYTICS)
    op.drop_index("ix_episode_candidate_patient", table_name="episode_candidate", schema=ANALYTICS)
    op.drop_index("ix_episode_candidate_facility", table_name="episode_candidate", schema=ANALYTICS)
    op.drop_index("ix_episode_candidate_dates", table_name="episode_candidate", schema=ANALYTICS)
    op.drop_index("ix_episode_candidate_build", table_name="episode_candidate", schema=ANALYTICS)
    op.drop_table("episode_candidate", schema=ANALYTICS)
    op.drop_index("ix_episode_build_status", table_name="episode_build", schema=ANALYTICS)
    op.drop_index("ix_episode_build_period", table_name="episode_build", schema=ANALYTICS)
    op.drop_table("episode_build", schema=ANALYTICS)

    # One call per type: the migration guard counts creates against drops.
    postgresql.ENUM(name="episode_encounter_role", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="episode_status", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="episode_build_status", schema=ANALYTICS).drop(bind, checkfirst=True)
