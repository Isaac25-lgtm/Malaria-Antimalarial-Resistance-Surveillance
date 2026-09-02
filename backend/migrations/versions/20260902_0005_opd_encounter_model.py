"""Outpatient encounter model, grounded in HMIS OPD 002.

Revision ID: 0005_opd_encounter_model
Revises: 0004_geography_import_support
Created: 2026-09-02

Four tables and eight enum types, every one of them traceable to a printed field
on HMIS OPD 002 (Print Version July 2024). The field-by-field mapping, with the
printed labels and the ambiguities that survive, is
``docs/data-dictionary/opd-002.md``.

Two things this migration deliberately does *not* create:

**No direct identifier.** The register's columns 2, 3 and 8 carry a national ID,
the patient's name and phone, and a next of kin's name and phone. None of them
has a column here. ``mars_core`` gets ``patient_reference``, which holds no
identifying value at all; the vault that resolves it belongs to ``mars_identity``
(ADR 0006). Next of kin is not stored in either schema.

**No outcome column.** The register has none - the only disposition it records
is whether a referral note was written. Inventing an outcome field would invite
a surface that claims one.

Every MARS migration must be reversible. ``downgrade`` is written and tested.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_opd_encounter_model"
down_revision: str | None = "0004_geography_import_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE = "mars_core"


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    #
    # Values are the codes printed on the form, plus an explicit ``unknown``
    # for a blank or unreadable cell. ``unknown`` is a MARS value, not a
    # category the register offers, and is documented as such so a count of it
    # reads as a data-quality finding rather than a demographic one.
    postgresql.ENUM("years", "months", "days", name="age_unit", schema=CORE).create(
        bind, checkfirst=True
    )
    postgresql.ENUM("male", "female", "unknown", name="sex", schema=CORE).create(
        bind, checkfirst=True
    )
    postgresql.ENUM(
        "national",
        "refugee",
        "foreigner",
        "unknown",
        name="patient_category",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "new_attendance",
        "re_attendance",
        "unknown",
        name="attendance_type",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM("yes", "no", "unknown", name="fever_status", schema=CORE).create(
        bind, checkfirst=True
    )
    postgresql.ENUM(
        "microscopy",
        "rdt",
        "not_done",
        "unknown",
        name="malaria_test_method",
        schema=CORE,
    ).create(bind, checkfirst=True)
    # ``not_done`` and ``not_applicable`` are kept distinct because the form's
    # instructions and its grid header disagree about which is printed, and
    # "no test was done" is not the same statement as "not applicable".
    postgresql.ENUM(
        "positive",
        "negative",
        "not_done",
        "not_applicable",
        "unknown",
        name="malaria_test_result",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "row_header",
        "carried_forward",
        "source_supplied",
        "unresolved",
        name="date_assignment_method",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM("inbound", "outbound", name="referral_direction", schema=CORE).create(
        bind, checkfirst=True
    )

    # -- patient_reference -------------------------------------------------
    op.create_table(
        "patient_reference",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("linkage_token_id", sa.UUID(), nullable=True),
        sa.Column("first_seen_on", sa.Date(), nullable=True),
        sa.Column("last_seen_on", sa.Date(), nullable=True),
        sa.Column("encounter_count", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_patient_reference")),
        schema=CORE,
    )
    op.create_index(
        "ix_patient_reference_first_seen",
        "patient_reference",
        ["first_seen_on"],
        schema=CORE,
    )
    op.create_index(
        op.f("ix_patient_reference_linkage_token_id"),
        "patient_reference",
        ["linkage_token_id"],
        schema=CORE,
    )
    op.execute(
        f"COMMENT ON TABLE {CORE}.patient_reference IS "
        "'A person as mars_core is allowed to know them: no name, no national "
        "ID, no phone, no date of birth. The vault that resolves this to a real "
        "person lives in mars_identity.'"
    )

    # -- opd_encounter -----------------------------------------------------
    op.create_table(
        "opd_encounter",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("facility_id", sa.UUID(), nullable=False),
        sa.Column("encounter_date", sa.Date(), nullable=False),
        sa.Column(
            "date_assignment_method",
            postgresql.ENUM(
                "row_header",
                "carried_forward",
                "source_supplied",
                "unresolved",
                name="date_assignment_method",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("serial_number", sa.String(length=16), nullable=True),
        sa.Column("patient_reference_id", sa.UUID(), nullable=True),
        sa.Column("age_value", sa.Integer(), nullable=True),
        sa.Column(
            "age_unit",
            postgresql.ENUM(
                "years", "months", "days", name="age_unit", schema=CORE, create_type=False
            ),
            nullable=True,
        ),
        sa.Column("age_days_approx", sa.Integer(), nullable=True),
        sa.Column(
            "sex",
            postgresql.ENUM(
                "male", "female", "unknown", name="sex", schema=CORE, create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "patient_category",
            postgresql.ENUM(
                "national",
                "refugee",
                "foreigner",
                "unknown",
                name="patient_category",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("residence_district_id", sa.UUID(), nullable=True),
        sa.Column("residence_subcounty_id", sa.UUID(), nullable=True),
        sa.Column("residence_parish_raw", sa.String(length=160), nullable=True),
        sa.Column("residence_village_raw", sa.String(length=160), nullable=True),
        sa.Column("residence_unresolved_raw", sa.String(length=320), nullable=True),
        sa.Column(
            "attendance_type",
            postgresql.ENUM(
                "new_attendance",
                "re_attendance",
                "unknown",
                name="attendance_type",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("presenting_complaint_raw", sa.Text(), nullable=True),
        sa.Column("notifiable_marked", sa.Boolean(), nullable=False),
        sa.Column(
            "fever_present",
            postgresql.ENUM(
                "yes", "no", "unknown", name="fever_status", schema=CORE, create_type=False
            ),
            nullable=False,
        ),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_batch_id", sa.UUID(), nullable=True),
        sa.Column("source_row_reference", sa.String(length=128), nullable=False),
        sa.Column("source_register_page", sa.String(length=32), nullable=True),
        sa.Column("ingest_method_version", sa.String(length=32), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opd_encounter")),
        sa.ForeignKeyConstraint(
            ["facility_id"],
            [f"{CORE}.facility.id"],
            name=op.f("fk_opd_encounter_facility_id_facility"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_reference_id"],
            [f"{CORE}.patient_reference.id"],
            name=op.f("fk_opd_encounter_patient_reference_id_patient_reference"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["residence_district_id"],
            [f"{CORE}.geography_unit.id"],
            name=op.f("fk_opd_encounter_residence_district_id_geography_unit"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["residence_subcounty_id"],
            [f"{CORE}.geography_unit.id"],
            name=op.f("fk_opd_encounter_residence_subcounty_id_geography_unit"),
            ondelete="SET NULL",
        ),
        # One source row becomes one encounter. Re-running a batch updates
        # rather than duplicates, so two extracts of the same register book
        # cannot double-count a facility's attendance.
        sa.UniqueConstraint(
            "source_system", "source_row_reference", name="uq_opd_encounter_source_row"
        ),
        # The form's own unit rules: an age written in months belongs under a
        # year, in days under a month. A value outside these bounds is a
        # transcription error, not an unusual patient.
        sa.CheckConstraint(
            "age_value IS NULL OR age_value >= 0", name=op.f("ck_opd_encounter_age_not_negative")
        ),
        sa.CheckConstraint(
            "age_unit <> 'months' OR age_value IS NULL OR age_value <= 11",
            name=op.f("ck_opd_encounter_age_months_under_a_year"),
        ),
        sa.CheckConstraint(
            "age_unit <> 'days' OR age_value IS NULL OR age_value <= 30",
            name=op.f("ck_opd_encounter_age_days_under_a_month"),
        ),
        sa.CheckConstraint(
            "age_unit <> 'years' OR age_value IS NULL OR age_value <= 130",
            name=op.f("ck_opd_encounter_age_years_plausible"),
        ),
        schema=CORE,
    )

    op.create_index(
        "ix_opd_encounter_facility_date",
        "opd_encounter",
        ["facility_id", "encounter_date"],
        schema=CORE,
    )
    op.create_index("ix_opd_encounter_date", "opd_encounter", ["encounter_date"], schema=CORE)
    op.create_index(
        "ix_opd_encounter_patient", "opd_encounter", ["patient_reference_id"], schema=CORE
    )
    op.create_index(
        "ix_opd_encounter_residence_district",
        "opd_encounter",
        ["residence_district_id"],
        schema=CORE,
    )
    op.execute(
        f"COMMENT ON TABLE {CORE}.opd_encounter IS "
        "'One outpatient visit, from one row of HMIS OPD 002 (July 2024). "
        "Records what was observed and done; the register has no outcome "
        "column and nothing here evidences treatment failure or resistance.'"
    )

    # -- opd_encounter_diagnosis -------------------------------------------
    op.create_table(
        "opd_encounter_diagnosis",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("opd_encounter_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("diagnosis_raw", sa.String(length=300), nullable=False),
        sa.Column("diagnosis_normalised", sa.String(length=300), nullable=False),
        sa.Column("hmis_105_item_id", sa.UUID(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opd_encounter_diagnosis")),
        sa.ForeignKeyConstraint(
            ["opd_encounter_id"],
            [f"{CORE}.opd_encounter.id"],
            name=op.f("fk_opd_encounter_diagnosis_opd_encounter_id_opd_encounter"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "opd_encounter_id", "sequence", name="uq_opd_diagnosis_encounter_sequence"
        ),
        schema=CORE,
    )
    op.create_index(
        "ix_opd_diagnosis_normalised",
        "opd_encounter_diagnosis",
        ["diagnosis_normalised"],
        schema=CORE,
    )
    op.create_index(
        "ix_opd_diagnosis_encounter",
        "opd_encounter_diagnosis",
        ["opd_encounter_id"],
        schema=CORE,
    )

    # -- opd_encounter_prescription ----------------------------------------
    op.create_table(
        "opd_encounter_prescription",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("opd_encounter_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("prescription_raw", sa.String(length=300), nullable=False),
        sa.Column("drug_name_raw", sa.String(length=200), nullable=True),
        sa.Column("drug_name_normalised", sa.String(length=200), nullable=True),
        sa.Column("units_per_dose", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("doses_per_day", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("days", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("total_units", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("is_device", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opd_encounter_prescription")),
        sa.ForeignKeyConstraint(
            ["opd_encounter_id"],
            [f"{CORE}.opd_encounter.id"],
            name=op.f("fk_opd_encounter_prescription_opd_encounter_id_opd_encounter"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "opd_encounter_id", "sequence", name="uq_opd_prescription_encounter_sequence"
        ),
        sa.CheckConstraint(
            "units_per_dose IS NULL OR units_per_dose > 0",
            name=op.f("ck_opd_encounter_prescription_units_positive"),
        ),
        sa.CheckConstraint(
            "doses_per_day IS NULL OR doses_per_day > 0",
            name=op.f("ck_opd_encounter_prescription_doses_positive"),
        ),
        sa.CheckConstraint(
            "days IS NULL OR days > 0", name=op.f("ck_opd_encounter_prescription_days_positive")
        ),
        schema=CORE,
    )
    op.create_index(
        "ix_opd_prescription_encounter",
        "opd_encounter_prescription",
        ["opd_encounter_id"],
        schema=CORE,
    )
    op.create_index(
        "ix_opd_prescription_drug",
        "opd_encounter_prescription",
        ["drug_name_normalised"],
        schema=CORE,
    )

    # -- opd_encounter_test ------------------------------------------------
    op.create_table(
        "opd_encounter_test",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("opd_encounter_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column(
            "method",
            postgresql.ENUM(
                "microscopy",
                "rdt",
                "not_done",
                "unknown",
                name="malaria_test_method",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "result",
            postgresql.ENUM(
                "positive",
                "negative",
                "not_done",
                "not_applicable",
                "unknown",
                name="malaria_test_result",
                schema=CORE,
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opd_encounter_test")),
        sa.ForeignKeyConstraint(
            ["opd_encounter_id"],
            [f"{CORE}.opd_encounter.id"],
            name=op.f("fk_opd_encounter_test_opd_encounter_id_opd_encounter"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("opd_encounter_id", "sequence", name="uq_opd_test_encounter_sequence"),
        # The paper register permits writing a result with no test performed.
        # MARS refuses to store the contradiction: a phantom result would enter
        # every positivity rate downstream.
        sa.CheckConstraint(
            "method <> 'not_done' OR result IN ('not_done', 'not_applicable', 'unknown')",
            name=op.f("ck_opd_encounter_test_no_result_without_a_test"),
        ),
        schema=CORE,
        comment=(
            "A diagnostic test recorded on HMIS OPD 002 column 13. A result is "
            "evidence of infection at a moment, never of whether treatment worked."
        ),
    )
    op.create_index(
        "ix_opd_test_encounter", "opd_encounter_test", ["opd_encounter_id"], schema=CORE
    )
    op.create_index("ix_opd_test_result", "opd_encounter_test", ["result"], schema=CORE)

    # -- opd_encounter_referral --------------------------------------------
    op.create_table(
        "opd_encounter_referral",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("opd_encounter_id", sa.UUID(), nullable=False),
        sa.Column(
            "direction",
            postgresql.ENUM(
                "inbound",
                "outbound",
                name="referral_direction",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("referral_number", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opd_encounter_referral")),
        sa.ForeignKeyConstraint(
            ["opd_encounter_id"],
            [f"{CORE}.opd_encounter.id"],
            name=op.f("fk_opd_encounter_referral_opd_encounter_id_opd_encounter"),
            ondelete="CASCADE",
        ),
        schema=CORE,
        comment=(
            "Referral numbers from HMIS OPD 002 columns 21 and 22. "
            "Facility-issued and not nationally unique; never a linkage key."
        ),
    )
    op.create_index(
        "ix_opd_referral_encounter",
        "opd_encounter_referral",
        ["opd_encounter_id"],
        schema=CORE,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("opd_encounter_referral", schema=CORE)
    op.drop_table("opd_encounter_test", schema=CORE)
    op.drop_table("opd_encounter_prescription", schema=CORE)
    op.drop_table("opd_encounter_diagnosis", schema=CORE)
    op.drop_table("opd_encounter", schema=CORE)
    op.drop_table("patient_reference", schema=CORE)

    # Written out one call per type rather than looped. A loop drops them all
    # correctly at runtime, but the migration guard in tests/unit/test_migrations
    # counts creates against drops in the source, and a loop reads as one drop -
    # which would let a genuinely leaked type slip past unnoticed.
    postgresql.ENUM(name="referral_direction", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="date_assignment_method", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="malaria_test_result", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="malaria_test_method", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="fever_status", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="attendance_type", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="patient_category", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="sex", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="age_unit", schema=CORE).drop(bind, checkfirst=True)
