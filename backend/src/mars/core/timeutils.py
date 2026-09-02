"""Time handling.

Blueprint appendix 138: timestamps are stored in UTC with source timezone
metadata and displayed in Uganda local time by default. Reporting period
assignment follows the source form's convention, never the browser timezone.

This module owns the UTC/EAT boundary. Nothing else in the backend should
construct a naive datetime.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

# Uganda observes East Africa Time year round, with no daylight saving.
EAT = timezone(timedelta(hours=3), name="EAT")
DISPLAY_TIMEZONE_NAME = "Africa/Kampala"


def utc_now() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as UTC, rejecting naive datetimes.

    A naive datetime is an error rather than an assumption: guessing its zone is
    how reporting periods silently shift by a day.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime rejected; supply a timezone-aware value")
    return value.astimezone(UTC)


def to_display(value: datetime) -> datetime:
    """Convert a stored UTC instant to Uganda local time for presentation."""
    return ensure_utc(value).astimezone(EAT)


def iso_utc(value: datetime) -> str:
    """Serialise as an ISO-8601 UTC string."""
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def epi_week_of(day: date) -> tuple[int, int]:
    """Return the ISO year and ISO week number for ``day``.

    This is the ISO-8601 week, provided as a neutral default. The authoritative
    epidemiological week convention for HMIS 033b is a source-form property and
    is recorded per submission during aggregate ingestion (Prompt 11), not
    assumed here.
    """
    iso = day.isocalendar()
    return iso.year, iso.week
