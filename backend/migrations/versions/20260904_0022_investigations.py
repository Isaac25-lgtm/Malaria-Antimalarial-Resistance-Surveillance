"""Investigation workflow.

Revision ID: 0022_investigations
Revises: 0021_explainability
Created: 2026-09-04

Four tables in mars_core:

``investigation``                    one programme response to one signal
``investigation_event``              the append-only timeline
``investigation_evidence_request``   a request for evidence MARS cannot produce
``investigation_feedback``           labelled outcomes for later method review

This is where MARS stops being an analysis and becomes operational software. A
signal nobody acts on is a signal that was never worth generating, so these
tables close the loop from detection to programme decision - and the schema is
shaped by what makes that decision defensible afterwards.

``investigation_event`` is append-only. Nothing updates or deletes it. An
investigation whose history can be rewritten cannot support the decision it led
to, and the decision is the entire point of the record.

Nothing here writes to ``surveillance_signal``. The foreign key points one way
and is RESTRICT: an investigation outlives interest in the signal that started
it, and deleting the analysis would orphan the decision. Concluding an
investigation does not edit the signal's evidence, score or status - the
analysis said what it said on the day it ran, and a later human judgement sits
beside it rather than correcting it.

Constraints worth naming:

* ``closure_records_its_outcome`` and ``escalation_records_its_reason`` - a
  terminal state carries the reason it reached it. An investigation closed
  without an outcome is a decision nobody can review.
* ``assigned_work_has_an_owner`` - assignment is what turns a queue entry into
  somebody's work, so the two cannot come apart.
* ``a_note_carries_text`` - an empty note is a row someone will later rely on
  and find nothing in.
* ``a_received_result_has_a_reference`` - an evidence request marked received
  must say where the result lives. MARS stores the pointer, never the clinical
  content: that separation keeps the confirmed-evidence lane distinct from
  routine surveillance.
* ``uq_investigation_signal`` - one live investigation per signal. A second
  would split the timeline and leave two people each believing the other had
  it.

``record_version`` carries optimistic concurrency. Two reviewers who both open
an investigation and both press close must not silently overwrite one another;
losing one of two contradictory conclusions is worse than making someone press
the button again. ``idempotency_key`` stops a retried open producing a second
investigation.

``investigation_feedback`` is the learning loop, and deliberately an inert one.
It records what a reviewer concluded against the method version in force when
the signal was generated, so a later governed method review has labelled
evidence to work from. It moves no threshold and changes no rule. Automatic
tuning from field outcomes is exactly the quiet drift that makes a surveillance
system unauditable.

An outcome of ``validated_signal`` means the pattern held up and warrants
programme action. It does not mean resistance was confirmed, and there is no
column here that could hold such a claim: confirmation reaches MARS from an
external reference laboratory through the separately governed evidence lane.

The enum type ``signal_priority`` (mars_analytics) belongs to migration 0020
and is referenced, not created.

Documented in ``docs/data-dictionary/investigations.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_investigations"
down_revision: str | None = "0021_explainability"
ANALYTICS = "mars_analytics"
CORE = "mars_core"
GOVERNANCE = "mars_governance"
SECURITY = "mars_security"

branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # -- Enum types -------------------------------------------------------
    # Types created by an earlier migration are referenced with
    # create_type=False and never dropped here: signal_priority.
    postgresql.ENUM(
        "new",
        "triaged",
        "assigned",
        "under_investigation",
        "closed",
        "escalated",
        name="investigation_status",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "validated_signal",
        "explained",
        "data_issue",
        "insufficient_evidence",
        name="investigation_outcome",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "opened",
        "triaged",
        "assigned",
        "reassigned",
        "note_added",
        "evidence_requested",
        "external_result_recorded",
        "outcome_recorded",
        "closed",
        "escalated",
        name="investigation_event_kind",
        schema=CORE,
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "awaiting",
        "received",
        "cancelled",
        name="evidence_request_status",
        schema=CORE,
    ).create(bind, checkfirst=True)

    op.create_table(
        "investigation",
        sa.Column("signal_id", sa.UUID(), nullable=False),
        sa.Column(
            "investigation_status",
            postgresql.ENUM(
                "new",
                "triaged",
                "assigned",
                "under_investigation",
                "closed",
                "escalated",
                name="investigation_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "priority",
            postgresql.ENUM(
                "unclassified",
                "informational",
                "attention",
                "high",
                "urgent",
                name="signal_priority",
                schema=ANALYTICS,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("geography_unit_id", sa.UUID(), nullable=True),
        sa.Column("facility_id", sa.UUID(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("assigned_to_user_id", sa.UUID(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            postgresql.ENUM(
                "validated_signal",
                "explained",
                "data_issue",
                "insufficient_evidence",
                name="investigation_outcome",
                schema=CORE,
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
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
            "investigation_status <> 'closed' OR (outcome IS NOT NULL AND closed_at IS NOT NULL)",
            name=op.f("ck_investigation_closure_records_its_outcome"),
        ),
        sa.CheckConstraint(
            "investigation_status <> 'escalated' OR (escalation_reason IS NOT NULL AND closed_at IS NOT NULL)",
            name=op.f("ck_investigation_escalation_records_its_reason"),
        ),
        sa.CheckConstraint(
            "investigation_status NOT IN ('assigned', 'under_investigation') OR assigned_to_user_id IS NOT NULL",
            name=op.f("ck_investigation_assigned_work_has_an_owner"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_investigation_period_ordered")
        ),
        sa.CheckConstraint(
            "record_version >= 1", name=op.f("ck_investigation_record_version_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_investigation_assigned_to_user_id_user_account"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["mars_analytics.surveillance_signal.id"],
            name=op.f("fk_investigation_signal_id_surveillance_signal"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation")),
        sa.UniqueConstraint("idempotency_key", name="uq_investigation_idempotency_key"),
        sa.UniqueConstraint("signal_id", name="uq_investigation_signal"),
        schema=CORE,
        comment="One programme response to one signal. The signal itself is never modified by anything recorded here.",
    )
    op.create_index(
        "ix_investigation_facility", "investigation", ["facility_id"], unique=False, schema=CORE
    )
    op.create_index(
        "ix_investigation_geography",
        "investigation",
        ["geography_unit_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_investigation_owner",
        "investigation",
        ["assigned_to_user_id", "investigation_status"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_investigation_status",
        "investigation",
        ["investigation_status"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "investigation_event",
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "event_kind",
            postgresql.ENUM(
                "opened",
                "triaged",
                "assigned",
                "reassigned",
                "note_added",
                "evidence_requested",
                "external_result_recorded",
                "outcome_recorded",
                "closed",
                "escalated",
                name="investigation_event_kind",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("actor_label", sa.String(length=160), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "event_kind <> 'note_added' OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name=op.f("ck_investigation_event_a_note_carries_text"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_investigation_event_sequence_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["mars_security.user_account.id"],
            name=op.f("fk_investigation_event_actor_user_id_user_account"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["mars_core.investigation.id"],
            name=op.f("fk_investigation_event_investigation_id_investigation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_event")),
        sa.UniqueConstraint("investigation_id", "sequence", name="uq_investigation_event_sequence"),
        schema=CORE,
        comment="Append-only investigation timeline. No update or delete path.",
    )
    op.create_index(
        "ix_investigation_event_investigation",
        "investigation_event",
        ["investigation_id", "sequence"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "investigation_evidence_request",
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column(
            "request_status",
            postgresql.ENUM(
                "awaiting",
                "received",
                "cancelled",
                name="evidence_request_status",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("result_reference", sa.String(length=256), nullable=True),
        sa.Column("result_recorded_at", sa.DateTime(timezone=True), nullable=True),
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
            "request_status <> 'received' OR (result_reference IS NOT NULL AND result_recorded_at IS NOT NULL)",
            name=op.f("ck_investigation_evidence_request_result_has_a_reference"),
        ),
        sa.CheckConstraint(
            "length(trim(description)) > 0",
            name=op.f("ck_investigation_evidence_request_request_states_its_ask"),
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["mars_core.investigation.id"],
            name="fk_evidence_request_investigation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["mars_security.user_account.id"],
            name="fk_evidence_request_requested_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_evidence_request")),
        schema=CORE,
        comment="A request for externally supplied evidence. Holds a reference to the external record, never its clinical content.",
    )
    op.create_index(
        "ix_evidence_request_investigation",
        "investigation_evidence_request",
        ["investigation_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_evidence_request_status",
        "investigation_evidence_request",
        ["request_status"],
        unique=False,
        schema=CORE,
    )
    op.create_table(
        "investigation_feedback",
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("signal_id", sa.UUID(), nullable=False),
        sa.Column("method_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "outcome",
            postgresql.ENUM(
                "validated_signal",
                "explained",
                "data_issue",
                "insufficient_evidence",
                name="investigation_outcome",
                schema=CORE,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("signal_input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
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
            ["investigation_id"],
            ["mars_core.investigation.id"],
            name="fk_investigation_feedback_investigation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["method_version_id"],
            ["mars_governance.method_version.id"],
            name="fk_investigation_feedback_method_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["mars_analytics.surveillance_signal.id"],
            name=op.f("fk_investigation_feedback_signal_id_surveillance_signal"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_feedback")),
        sa.UniqueConstraint("investigation_id", name="uq_investigation_feedback_investigation"),
        schema=CORE,
        comment="Labelled outcomes for later method review. Informs a governed review; changes nothing on its own.",
    )
    op.create_index(
        "ix_investigation_feedback_method",
        "investigation_feedback",
        ["method_version_id"],
        unique=False,
        schema=CORE,
    )
    op.create_index(
        "ix_investigation_feedback_outcome",
        "investigation_feedback",
        ["outcome"],
        unique=False,
        schema=CORE,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(
        "ix_investigation_feedback_outcome", table_name="investigation_feedback", schema=CORE
    )
    op.drop_index(
        "ix_investigation_feedback_method", table_name="investigation_feedback", schema=CORE
    )
    op.drop_table("investigation_feedback", schema=CORE)
    op.drop_index(
        "ix_evidence_request_status", table_name="investigation_evidence_request", schema=CORE
    )
    op.drop_index(
        "ix_evidence_request_investigation",
        table_name="investigation_evidence_request",
        schema=CORE,
    )
    op.drop_table("investigation_evidence_request", schema=CORE)
    op.drop_index(
        "ix_investigation_event_investigation", table_name="investigation_event", schema=CORE
    )
    op.drop_table("investigation_event", schema=CORE)
    op.drop_index("ix_investigation_status", table_name="investigation", schema=CORE)
    op.drop_index("ix_investigation_owner", table_name="investigation", schema=CORE)
    op.drop_index("ix_investigation_geography", table_name="investigation", schema=CORE)
    op.drop_index("ix_investigation_facility", table_name="investigation", schema=CORE)
    op.drop_table("investigation", schema=CORE)

    # One call per type: the migration guard counts creates against
    # drops in the source, and a loop would read as a single drop.
    postgresql.ENUM(name="evidence_request_status", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="investigation_event_kind", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="investigation_outcome", schema=CORE).drop(bind, checkfirst=True)
    postgresql.ENUM(name="investigation_status", schema=CORE).drop(bind, checkfirst=True)
