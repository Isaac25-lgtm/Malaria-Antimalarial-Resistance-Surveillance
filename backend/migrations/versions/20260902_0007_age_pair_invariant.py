"""An age is a value and a unit, or it is nothing.

Revision ID: 0007_age_pair_invariant
Revises: 0006_identity_vault
Created: 2026-09-02

Migration 0005 made ``age_value`` and ``age_unit`` independently nullable, so a
row could carry a number with no unit. HMIS OPD 002 writes the two together -
"3" and "MTH", or "14" and nothing because years are the default *on paper* -
and a stored value with no unit is not an age at all. Anything reading it would
have to assume a unit, and a three-day-old assumed to be three years old is the
kind of error that survives into an age-banded rate and is never noticed.

Committed migration 0005 is not edited; this is a forward correction.

The upgrade **inspects before it constrains**. If any row violates the invariant
the migration stops and names the count, rather than coercing a unit onto data
nobody recorded one for. Choosing which unit those rows meant is a clinical
question about a specific facility's records, not something a migration may
decide.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_age_pair_invariant"
down_revision: str | None = "0006_identity_vault"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE = "mars_core"
TABLE = "opd_encounter"
#: Bare name. NAMING_CONVENTION prefixes it with "ck_<table>_", and both
#: create_check_constraint and drop_constraint apply that convention - so
#: passing the resolved name to either doubles the prefix.
CONSTRAINT = "age_value_and_unit_together"
RESOLVED = f"ck_{TABLE}_{CONSTRAINT}"

#: The invariant, in one place so the check and the constraint cannot diverge.
_PAIRED = (
    "(age_value IS NULL AND age_unit IS NULL) OR (age_value IS NOT NULL AND age_unit IS NOT NULL)"
)


def upgrade() -> None:
    bind = op.get_bind()

    offending = bind.execute(
        sa.text(f"SELECT count(*) FROM {CORE}.{TABLE} WHERE NOT ({_PAIRED})")  # noqa: S608
    ).scalar_one()

    if offending:
        raise RuntimeError(
            f"{offending} row(s) in {CORE}.{TABLE} carry an age value without a "
            "unit, or a unit without a value. This migration will not guess "
            "which unit was meant: an age recorded as bare '3' could be three "
            "years, three months or three days, and the difference changes "
            "every age-banded figure derived from it.\n"
            "\n"
            "To remediate, find them with:\n"
            f"    SELECT id, source_system, source_row_reference, age_value, age_unit\n"
            f"      FROM {CORE}.{TABLE} WHERE NOT ({_PAIRED});\n"
            "\n"
            "then either set both columns from the source register, or set both "
            "to NULL to record that the age is unknown - which is honest, and "
            "which the constraint permits."
        )

    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        _PAIRED,
        schema=CORE,
    )
    op.execute(
        f"COMMENT ON CONSTRAINT {RESOLVED} ON {CORE}.{TABLE} IS "
        "'An age is a value and a unit together, or neither. A number without "
        "a unit is not an age, and would be read as years by anything that "
        "assumed a default.'"
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, schema=CORE, type_="check")
