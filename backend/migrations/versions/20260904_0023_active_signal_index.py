"""Active signal period index.

Revision ID: 0023_active_signal_index
Revises: 0022_investigations
Created: 2026-09-04

One partial index, added after reviewing the query paths the command centre
actually takes.

The national screen asks the same question on every load: which signals are
active for this period, within this scope. The existing indexes cover signal
type and period, and geography and facility separately, but nothing covered the
status filter that every one of those reads applies.

The index is partial on ``signal_status = 'active'`` because an active signal is
a small and shrinking fraction of the table. Signals are immutable and a
correction supersedes rather than edits, so every superseded revision of every
signal stays for ever; an unconditional index would grow with the history while
only the live rows are ever queried this way.

Every MARS migration must be reversible. ``downgrade`` is written and tested,
not left as a stub: a migration that cannot be reversed cannot be safely applied
to a production surveillance database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

ANALYTICS = "mars_analytics"

revision: str = "0023_active_signal_index"
down_revision: str | None = "0022_investigations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_surveillance_signal_active_period",
        "surveillance_signal",
        ["period_start", "period_end", "geography_unit_id"],
        unique=False,
        schema=ANALYTICS,
        postgresql_where=sa.text("signal_status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_surveillance_signal_active_period",
        table_name="surveillance_signal",
        schema=ANALYTICS,
        postgresql_where=sa.text("signal_status = 'active'"),
    )
