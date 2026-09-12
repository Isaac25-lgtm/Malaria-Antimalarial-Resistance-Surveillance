"""How precisely the Tracker client records an event date (audit defect A).

The client still normalises every moment to an aware UTC value, as before, but
it now records whether DHIS2 sent a date, a wall-clock time without an offset or
an instant. Every value here is invented.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mars.integrations.dhis2.tracker.client import _remote_event


def raw(occurred: str) -> dict[str, object]:
    return {
        "event": "event-1",
        "trackedEntity": "person-1",
        "program": "programme",
        "programStage": "stage",
        "orgUnit": "facility",
        "occurredAt": occurred,
        "status": "COMPLETED",
        "dataValues": [],
    }


@pytest.mark.parametrize(
    ("occurred", "precision"),
    [
        ("2026-08-03", "date"),
        ("2026-08-03T00:00:00.000", "date"),
        ("2026-08-03T10:15:00.000", "local_time"),
        ("2026-08-03T21:30:00.000Z", "timestamp"),
        ("2026-08-03T21:30:00+03:00", "timestamp"),
    ],
)
def test_the_recorded_precision_is_kept(occurred: str, precision: str) -> None:
    event = _remote_event(raw(occurred))
    assert event.occurred_precision == precision
    assert event.occurred_at.tzinfo is not None  # the existing normalisation is unchanged


def test_a_naive_wall_clock_value_keeps_the_date_as_entered() -> None:
    event = _remote_event(raw("2026-08-03T23:30:00.000"))
    assert event.occurred_at == datetime(2026, 8, 3, 23, 30, tzinfo=UTC)
    assert event.occurred_precision == "local_time"
    assert event.occurred_at.date().isoformat() == "2026-08-03"
