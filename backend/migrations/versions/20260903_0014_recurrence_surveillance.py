"""Recurrence surveillance results.

Revision ID: 0014_recurrence_surveillance
Revises: 0013_episode_engine
Created: 2026-09-03

One table, ``mars_analytics.recurrence_result``: counts of observed recurrence
patterns for a facility or a residence geography.

Nothing in it is a clinical outcome. Routine data cannot establish treatment
failure, recrudescence, reinfection or resistance, and every row carries an
``interpretation_context`` saying so - carried on the row rather than added by
a presentation layer, because a figure that reaches a report without it is a
figure someone will over-read.

Facility of care and residence geography are separate ``scope_kind`` values and
are never merged. A patient may attend a clinic outside their own district;
merging the two attributes a pattern to the wrong place, and the questions they
answer are different.

Constraints worth naming:

* ``value_present_iff_available`` - the same rule the indicator results carry.
  A recurrence proportion with no eligible population is unavailable, never
  zero: reporting 0.0 would put a real-looking "no recurrence here" into every
  district summary.
* ``band_only_on_band_counts`` - an interval band belongs to a band count and
  nothing else, so a patient count cannot carry one and be double-counted by
  anything grouping on it.

``excluded_unlinked_encounters`` is on every row. Those are patients MARS could
not follow, and their absence always makes recurrence look rarer than it is.

Interval bands are governed configuration, not shipped values. A result
computed with no approved bands reports its counts and marks the band
breakdown unavailable.

The enum types ``period_grain`` (mars_governance) and
``indicator_value_status`` (mars_analytics) already exist from migration 0012
and are referenced, not created.

Documented in ``docs/data-dictionary/recurrence.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_recurrence_surveillance"
down_revision: str | None = "0013_episode_engine"
ANALYTICS = "mars_analytics"
GOVERNANCE = "mars_governance"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # 'period_grain' and 'indicator_value_status' belong to migration 0012.
    # They are referenced with create_type=False and never dropped here.
    postgresql.ENUM(
        "repeat_positive_patients",
        "repeat_positive_episodes",
        "patients_with_multiple_episodes",
        "repeat_positive_proportion",
        "interval_band_count",
        name="recurrence_measure",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "facility",
        "residence_district",
        "residence_subcounty",
        name="recurrence_scope_kind",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "recurrence_result",
        sa.Column("episode_build_id", sa.UUID(), nullable=False),
        sa.Column(
            "measure",
            postgresql.ENUM(
                "repeat_positive_patients",
                "repeat_positive_episodes",
                "patients_with_multiple_episodes",
                "repeat_positive_proportion",
                "interval_band_count",
                name="recurrence_measure",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "scope_kind",
            postgresql.ENUM(
                "facility",
                "residence_district",
                "residence_subcounty",
                name="recurrence_scope_kind",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "period_grain",
            postgresql.ENUM(
                "day",
                "epidemiological_week",
                "month",
                name="period_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("interval_band", sa.String(length=64), nullable=True),
        sa.Column("numerator", sa.Integer(), nullable=True),
        sa.Column("denominator", sa.Integer(), nullable=True),
        sa.Column("value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "value_status",
            postgresql.ENUM(
                "available",
                "unavailable_no_denominator",
                "unavailable_insufficient_data",
                "suppressed",
                name="indicator_value_status",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("eligible_patients", sa.Integer(), nullable=True),
        sa.Column("excluded_unlinked_encounters", sa.Integer(), nullable=True),
        sa.Column("positives_without_treatment_record", sa.Integer(), nullable=True),
        sa.Column("residence_unresolved_episodes", sa.Integer(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("episode_rule_version_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interpretation_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(measure = 'interval_band_count' AND interval_band IS NOT NULL) OR (measure <> 'interval_band_count' AND interval_band IS NULL)",
            name=op.f("ck_recurrence_result_band_only_on_band_counts"),
        ),
        sa.CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_recurrence_result_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator >= 0",
            name=op.f("ck_recurrence_result_denominator_not_negative"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_recurrence_result_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "numerator IS NULL OR numerator >= 0",
            name=op.f("ck_recurrence_result_numerator_not_negative"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_recurrence_result_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["episode_build_id"],
            ["mars_analytics.episode_build.id"],
            name="fk_recurrence_build",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurrence_result")),
        sa.UniqueConstraint(
            "episode_build_id",
            "measure",
            "scope_kind",
            "scope_id",
            "period_start",
            "interval_band",
            "input_fingerprint",
            name="uq_recurrence_result_build_measure_scope_period_band",
        ),
        schema=ANALYTICS,
        comment="Counts of observed recurrence patterns. Never a clinical outcome: routine data cannot establish treatment failure, recrudescence, reinfection or resistance.",
    )
    op.create_index(
        "ix_recurrence_result_build",
        "recurrence_result",
        ["episode_build_id"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_recurrence_result_measure",
        "recurrence_result",
        ["measure", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_recurrence_result_scope",
        "recurrence_result",
        ["scope_kind", "scope_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_recurrence_result_scope", table_name="recurrence_result", schema=ANALYTICS)
    op.drop_index("ix_recurrence_result_measure", table_name="recurrence_result", schema=ANALYTICS)
    op.drop_index("ix_recurrence_result_build", table_name="recurrence_result", schema=ANALYTICS)
    op.drop_table("recurrence_result", schema=ANALYTICS)

    # One call per type: the migration guard counts creates against drops.
    postgresql.ENUM(name="recurrence_scope_kind", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="recurrence_measure", schema=ANALYTICS).drop(bind, checkfirst=True)
