"""Testing, treatment and commodity surveillance.

Revision ID: 0015_surveillance_domains
Revises: 0014_recurrence_surveillance
Created: 2026-09-03

Four tables in mars_analytics:

``testing_surveillance_result``    what a facility did with its tests
``treatment_surveillance_result``  what was prescribed, as recorded
``commodity_stock_fact``           stock conditions the source stated outright
``commodity_operational_alert``    supply-chain alerts, kept apart from signals

Three domains, three evidence shapes, one shared provenance envelope. The
envelope is identical everywhere because the questions are - which rules made
this, from what, as of when, how good were the inputs. The evidence is not
shared: a testing result carries a tested denominator, a treatment result
carries a missing-prescription count, a commodity fact carries days and a unit
of issue. One table of mostly-null columns would lose the distinction between
"this measure has no denominator" and "this measure's denominator was not
recorded", which is the distinction the rest of MARS spends its effort keeping.

``commodity_operational_alert`` is deliberately not a signal table. A stock-out
needs a storekeeper and a district pharmacist; a treatment-response signal needs
an epidemiologist and a laboratory. One table with a kind column would make
converting one into the other a one-line change, and that conversion is the
claim MARS must never make silently. Later signal work may cite an alert as
context; the citation runs one way.

Constraints worth naming:

* ``fact_carries_its_evidence`` - a days-out-of-stock fact must carry days and
  a zero-balance fact must carry a balance, so no fact asserts a condition it
  has no evidence for. The explicit NULL tests are load-bearing: a check
  constraint passes when it evaluates to NULL, so the comparison alone would
  admit the row it exists to refuse.
* ``classified_alerts_need_config`` - only ``stock_out_reported`` may exist
  without a governed rule, because it restates what the facility reported.
  Prolonged, repeated, low and imminent are judgements, and a judgement with no
  rule behind it is an engineer's opinion driving a supply decision.
* ``severity_requires_configuration`` - severity stays ``unclassified`` unless
  governed rules say otherwise. How urgent a stock-out is depends on resupply
  times and buffer stocks MARS does not know.
* ``value_present_iff_available`` and ``facility_id_matches_grain`` on all
  three result tables, from the shared envelope.

The enum types ``geography_grain`` and ``period_grain`` (mars_governance) and
``indicator_value_status`` (mars_analytics) belong to migration 0012 and are
referenced, not created.

Documented in ``docs/data-dictionary/surveillance-domains.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_surveillance_domains"
down_revision: str | None = "0014_recurrence_surveillance"
ANALYTICS = "mars_analytics"
CORE = "mars_core"
GOVERNANCE = "mars_governance"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # Types created by an earlier migration are referenced with
    # create_type=False and never dropped here: geography_grain, indicator_value_status, period_grain.
    postgresql.ENUM(
        "testing_coverage",
        "rdt_share",
        "microscopy_share",
        "test_positivity",
        "negative_cases_treated",
        "untested_cases_treated",
        "testing_volume_change",
        "missing_result_count",
        name="testing_measure",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "confirmed_treated",
        "confirmed_not_treated",
        "treated_without_confirmation",
        "repeat_treatment_episodes",
        "missing_treatment_information",
        name="treatment_measure",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "stock_on_hand_zero",
        "days_out_of_stock_reported",
        "stock_not_reported",
        name="commodity_fact_kind",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "stock_out_reported",
        "prolonged_stock_out",
        "repeated_stock_out",
        "multi_commodity_stock_out",
        "low_stock",
        "imminent_stock_out",
        name="commodity_alert_kind",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "unclassified",
        "informational",
        "attention",
        "urgent",
        name="alert_severity",
        schema=ANALYTICS,
    ).create(bind, checkfirst=True)

    op.create_table(
        "testing_surveillance_result",
        sa.Column(
            "measure",
            postgresql.ENUM(
                "testing_coverage",
                "rdt_share",
                "microscopy_share",
                "test_positivity",
                "negative_cases_treated",
                "untested_cases_treated",
                "testing_volume_change",
                "missing_result_count",
                name="testing_measure",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
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
        sa.Column("missing_results", sa.Integer(), nullable=True),
        sa.Column("untested_encounters", sa.Integer(), nullable=True),
        sa.Column("commodity_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "geography_grain",
            postgresql.ENUM(
                "facility",
                "subcounty",
                "district",
                "national",
                name="geography_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
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
        sa.Column("indicator_version_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contributing_units", sa.Integer(), nullable=True),
        sa.Column("expected_units", sa.Integer(), nullable=True),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_testing_surveillance_result_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_testing_surveillance_result_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator >= 0",
            name=op.f("ck_testing_surveillance_result_denominator_not_negative"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_testing_surveillance_result_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "numerator IS NULL OR numerator >= 0",
            name=op.f("ck_testing_surveillance_result_numerator_not_negative"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_testing_surveillance_result_period_ordered")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_testing_surveillance_result")),
        sa.UniqueConstraint(
            "measure",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_testing_result_measure_scope_period_input",
        ),
        schema=ANALYTICS,
        comment="Testing-practice measures. Describes what a facility did with its tests, never how much malaria there is.",
    )
    op.create_index(
        "ix_testing_result_facility",
        "testing_surveillance_result",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_testing_result_geography",
        "testing_surveillance_result",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_testing_result_measure",
        "testing_surveillance_result",
        ["measure", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_testing_result_period",
        "testing_surveillance_result",
        ["period_start", "period_end"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "treatment_surveillance_result",
        sa.Column(
            "measure",
            postgresql.ENUM(
                "confirmed_treated",
                "confirmed_not_treated",
                "treated_without_confirmation",
                "repeat_treatment_episodes",
                "missing_treatment_information",
                name="treatment_measure",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
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
        sa.Column("missing_treatment_information", sa.Integer(), nullable=True),
        sa.Column("confirmed_without_treatment", sa.Integer(), nullable=True),
        sa.Column("commodity_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "geography_grain",
            postgresql.ENUM(
                "facility",
                "subcounty",
                "district",
                "national",
                name="geography_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
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
        sa.Column("indicator_version_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contributing_units", sa.Integer(), nullable=True),
        sa.Column("expected_units", sa.Integer(), nullable=True),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_treatment_surveillance_result_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_treatment_surveillance_result_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator >= 0",
            name=op.f("ck_treatment_surveillance_result_denominator_not_negative"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_treatment_surveillance_result_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "numerator IS NULL OR numerator >= 0",
            name=op.f("ck_treatment_surveillance_result_numerator_not_negative"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start",
            name=op.f("ck_treatment_surveillance_result_period_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_treatment_surveillance_result")),
        sa.UniqueConstraint(
            "measure",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_treatment_result_measure_scope_period_input",
        ),
        schema=ANALYTICS,
        comment="Treatment-practice measures. Records what was prescribed, never what a patient received or took.",
    )
    op.create_index(
        "ix_treatment_result_facility",
        "treatment_surveillance_result",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_treatment_result_geography",
        "treatment_surveillance_result",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_treatment_result_measure",
        "treatment_surveillance_result",
        ["measure", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_treatment_result_period",
        "treatment_surveillance_result",
        ["period_start", "period_end"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "commodity_operational_alert",
        sa.Column(
            "alert_kind",
            postgresql.ENUM(
                "stock_out_reported",
                "prolonged_stock_out",
                "repeated_stock_out",
                "multi_commodity_stock_out",
                "low_stock",
                "imminent_stock_out",
                name="commodity_alert_kind",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("commodity_code", sa.String(length=48), nullable=False),
        sa.Column("commodity_label", sa.String(length=160), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=False),
        sa.Column("district_geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "severity",
            postgresql.ENUM(
                "unclassified",
                "informational",
                "attention",
                "urgent",
                name="alert_severity",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("supporting_fact_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
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
            "alert_kind = 'stock_out_reported' OR configuration_version_id IS NOT NULL",
            name=op.f("ck_commodity_operational_alert_classified_alerts_need_config"),
        ),
        sa.CheckConstraint(
            "severity = 'unclassified' OR configuration_version_id IS NOT NULL",
            name=op.f("ck_commodity_operational_alert_severity_requires_configuration"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_commodity_operational_alert_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_commodity_operational_alert_period_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            ["mars_core.facility.id"],
            name="fk_commodity_alert_facility",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commodity_operational_alert")),
        sa.UniqueConstraint(
            "alert_kind",
            "commodity_code",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_commodity_alert_kind_code_facility_period_input",
        ),
        schema=ANALYTICS,
        comment="Operational supply-chain alerts. Deliberately not signals: a stock-out says nothing about transmission, treatment response or resistance, and nothing may convert one into a finding about the parasite.",
    )
    op.create_index(
        "ix_commodity_alert_facility",
        "commodity_operational_alert",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_commodity_alert_kind",
        "commodity_operational_alert",
        ["alert_kind", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_table(
        "commodity_stock_fact",
        sa.Column(
            "fact_kind",
            postgresql.ENUM(
                "stock_on_hand_zero",
                "days_out_of_stock_reported",
                "stock_not_reported",
                name="commodity_fact_kind",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("commodity_code", sa.String(length=48), nullable=False),
        sa.Column("commodity_label", sa.String(length=160), nullable=True),
        sa.Column("unit_of_issue", sa.String(length=64), nullable=True),
        sa.Column("stock_on_hand", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("days_out_of_stock", sa.Integer(), nullable=True),
        sa.Column("quantity_consumed", sa.Numeric(precision=12, scale=2), nullable=True),
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
        sa.Column("aggregate_submission_id", sa.UUID(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "geography_grain",
            postgresql.ENUM(
                "facility",
                "subcounty",
                "district",
                "national",
                name="geography_grain",
                schema=GOVERNANCE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
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
        sa.Column("indicator_version_id", sa.UUID(), nullable=True),
        sa.Column("method_version_id", sa.UUID(), nullable=True),
        sa.Column("configuration_version_id", sa.UUID(), nullable=True),
        sa.Column("boundary_version_id", sa.UUID(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contributing_units", sa.Integer(), nullable=True),
        sa.Column("expected_units", sa.Integer(), nullable=True),
        sa.Column("quality_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(fact_kind <> 'days_out_of_stock_reported' OR (days_out_of_stock IS NOT "
            "NULL AND days_out_of_stock > 0)) AND (fact_kind <> 'stock_on_hand_zero' "
            "OR (stock_on_hand IS NOT NULL AND stock_on_hand = 0))",
            name=op.f("ck_commodity_stock_fact_fact_carries_its_evidence"),
        ),
        sa.CheckConstraint(
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR (geography_grain <> 'facility' AND facility_id IS NULL)",
            name=op.f("ck_commodity_stock_fact_facility_id_matches_grain"),
        ),
        sa.CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR (value IS NULL AND value_status <> 'available')",
            name=op.f("ck_commodity_stock_fact_value_present_iff_available"),
        ),
        sa.CheckConstraint(
            "days_out_of_stock IS NULL OR days_out_of_stock >= 0",
            name=op.f("ck_commodity_stock_fact_days_out_of_stock_not_negative"),
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name=op.f("ck_commodity_stock_fact_fingerprint_is_sha256"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_commodity_stock_fact_period_ordered")
        ),
        sa.CheckConstraint(
            "stock_on_hand IS NULL OR stock_on_hand >= 0",
            name=op.f("ck_commodity_stock_fact_stock_on_hand_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_submission_id"],
            ["mars_core.aggregate_submission.id"],
            name="fk_commodity_fact_submission",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commodity_stock_fact")),
        sa.UniqueConstraint(
            "fact_kind",
            "commodity_code",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_commodity_fact_kind_code_scope_period_input",
        ),
        schema=ANALYTICS,
        comment="Commodity conditions the source stated outright. No statistical judgement: prolonged, repeated, low and imminent require governed thresholds and are not here.",
    )
    op.create_index(
        "ix_commodity_fact_commodity",
        "commodity_stock_fact",
        ["commodity_code", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_commodity_fact_facility",
        "commodity_stock_fact",
        ["facility_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_commodity_fact_geography",
        "commodity_stock_fact",
        ["geography_unit_id", "period_start"],
        unique=False,
        schema=ANALYTICS,
    )
    op.create_index(
        "ix_commodity_fact_period",
        "commodity_stock_fact",
        ["period_start", "period_end"],
        unique=False,
        schema=ANALYTICS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_commodity_fact_period", table_name="commodity_stock_fact", schema=ANALYTICS)
    op.drop_index(
        "ix_commodity_fact_geography", table_name="commodity_stock_fact", schema=ANALYTICS
    )
    op.drop_index("ix_commodity_fact_facility", table_name="commodity_stock_fact", schema=ANALYTICS)
    op.drop_index(
        "ix_commodity_fact_commodity", table_name="commodity_stock_fact", schema=ANALYTICS
    )
    op.drop_table("commodity_stock_fact", schema=ANALYTICS)
    op.drop_index(
        "ix_commodity_alert_kind", table_name="commodity_operational_alert", schema=ANALYTICS
    )
    op.drop_index(
        "ix_commodity_alert_facility", table_name="commodity_operational_alert", schema=ANALYTICS
    )
    op.drop_table("commodity_operational_alert", schema=ANALYTICS)
    op.drop_index(
        "ix_treatment_result_period", table_name="treatment_surveillance_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_treatment_result_measure", table_name="treatment_surveillance_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_treatment_result_geography",
        table_name="treatment_surveillance_result",
        schema=ANALYTICS,
    )
    op.drop_index(
        "ix_treatment_result_facility", table_name="treatment_surveillance_result", schema=ANALYTICS
    )
    op.drop_table("treatment_surveillance_result", schema=ANALYTICS)
    op.drop_index(
        "ix_testing_result_period", table_name="testing_surveillance_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_testing_result_measure", table_name="testing_surveillance_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_testing_result_geography", table_name="testing_surveillance_result", schema=ANALYTICS
    )
    op.drop_index(
        "ix_testing_result_facility", table_name="testing_surveillance_result", schema=ANALYTICS
    )
    op.drop_table("testing_surveillance_result", schema=ANALYTICS)

    # One call per type: the migration guard counts creates against
    # drops in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="alert_severity", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="commodity_alert_kind", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="commodity_fact_kind", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="treatment_measure", schema=ANALYTICS).drop(bind, checkfirst=True)
    postgresql.ENUM(name="testing_measure", schema=ANALYTICS).drop(bind, checkfirst=True)
