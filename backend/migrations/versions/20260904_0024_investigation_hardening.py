"""Make investigation history immutable and name the start event correctly.

Revision ID: 0024_investigation_hardening
Revises: 0023_active_signal_index
Created: 2026-09-04

The ORM described ``investigation_event`` as append-only, but revision 0022
did not enforce that promise in PostgreSQL. This forward migration adds the
same database-level protection used by the audit log. It also corrects the
event emitted when work starts: ``outcome_recorded`` was being written before
an outcome existed, so the reversible enum rename makes the timeline truthful.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_investigation_hardening"
down_revision: str | None = "0023_active_signal_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REJECT_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION mars_core.reject_investigation_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'mars_core.investigation_event is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

REJECT_MUTATION_TRIGGER = """
CREATE TRIGGER investigation_event_append_only
BEFORE UPDATE OR DELETE ON mars_core.investigation_event
FOR EACH ROW EXECUTE FUNCTION mars_core.reject_investigation_event_mutation();
"""


def upgrade() -> None:
    op.execute(
        "ALTER TYPE mars_core.investigation_event_kind RENAME VALUE 'outcome_recorded' TO 'started'"
    )
    op.execute(REJECT_MUTATION_FUNCTION)
    op.execute(REJECT_MUTATION_TRIGGER)
    op.execute(
        "COMMENT ON TABLE mars_core.investigation_event IS "
        "'Append-only investigation timeline. UPDATE and DELETE are rejected by trigger.'"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS investigation_event_append_only ON mars_core.investigation_event"
    )
    op.execute("DROP FUNCTION IF EXISTS mars_core.reject_investigation_event_mutation()")
    op.execute(
        "ALTER TYPE mars_core.investigation_event_kind RENAME VALUE 'started' TO 'outcome_recorded'"
    )
    op.execute(
        "COMMENT ON TABLE mars_core.investigation_event IS "
        "'Append-only investigation timeline. No update or delete path.'"
    )
