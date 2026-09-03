"""What MARS needs from an external source system, in MARS's own words.

These are the seams. Nothing here mentions DHIS2, HTTP, JSON or pagination: an
adapter translates a remote contract into these types, and the rest of MARS
depends on the types rather than on the system that happened to supply them.

ADR 0003: the domain never imports an adapter. That rule only means something
if there is somewhere else for the domain to look, which is here.

**No port returns a remote identifier as an identity.** Every remote id arrives
alongside its type, so the crosswalk decides what it means. A port that returned
a DHIS2 UID where MARS expected a facility id would make the UID load-bearing by
accident, and the next system's ids would not fit.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RemoteOrganisationUnit:
    """An organisation unit as the source describes it.

    ``level`` is the source's own numbering, kept verbatim. Mapping it onto
    MARS's geography levels is the crosswalk's job, because the two hierarchies
    do not have to agree and pretending they do is how a subcounty becomes a
    district.
    """

    remote_id: str
    name: str
    level: int | None = None
    parent_remote_id: str | None = None
    code: str | None = None
    opening_date: date | None = None
    closed_date: date | None = None
    #: Coordinates the source carries. MARS stores a facility coordinate only
    #: when it is validated, so this is offered, never trusted.
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class RemoteDataElement:
    """A data element or indicator the source can report."""

    remote_id: str
    name: str
    code: str | None = None
    value_type: str | None = None
    category_combo_remote_id: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteDataValue:
    """One reported value, in the source's own coordinates.

    Deliberately close to the wire: element, org unit, period, disaggregation
    and a **string** value. Parsing the string into a count belongs to the
    canonical validator, which already knows that blank is not zero and that a
    non-numeric cell is preserved rather than discarded.
    """

    data_element_remote_id: str
    organisation_unit_remote_id: str
    period: str
    value: str | None
    category_option_combo_remote_id: str | None = None
    attribute_option_combo_remote_id: str | None = None
    stored_by: str | None = None
    last_updated: str | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class RemotePage:
    """One page of results, and how to ask for the next.

    ``next_cursor`` is opaque. The adapter that produced it is the only thing
    that interprets it, so a source paging by token and a source paging by page
    number look the same to everything upstream.
    """

    records: tuple[Any, ...]
    next_cursor: str | None = None
    total_declared: int | None = None
    page_description: str = ""

    @property
    def is_last(self) -> bool:
        return self.next_cursor is None


@dataclass(frozen=True, slots=True)
class RemoteScope:
    """What is being asked for.

    Held as data so it can be fingerprinted: the same scope requested twice is
    the same run, and that is what makes a re-pull idempotent rather than a
    second import of the same month.
    """

    organisation_unit_remote_ids: tuple[str, ...] = ()
    dataset_remote_ids: tuple[str, ...] = ()
    data_element_remote_ids: tuple[str, ...] = ()
    period_start: date | None = None
    period_end: date | None = None
    include_descendants: bool = False
    extra: dict[str, str] = field(default_factory=dict)


class MetadataPort(Protocol):
    """Organisation units, facilities and data elements."""

    def fetch_organisation_units(self, cursor: str | None = None) -> RemotePage: ...

    def fetch_data_elements(self, cursor: str | None = None) -> RemotePage: ...

    def fetch_datasets(self, cursor: str | None = None) -> RemotePage: ...


class AggregateDataPort(Protocol):
    """Reported aggregate values for a scope."""

    def fetch_data_values(self, scope: RemoteScope, cursor: str | None = None) -> RemotePage: ...


class AnalyticsPort(Protocol):
    """Pre-aggregated analytics, where the remote system offers them.

    Separate from :class:`AggregateDataPort` deliberately. Analytics output is
    the remote system's *derived* figure, computed by its rules, and MARS keeps
    derived figures apart from reported ones. Treating the two as one endpoint
    would erase that distinction at the seam, where it is hardest to recover.
    """

    def fetch_analytics(self, scope: RemoteScope, cursor: str | None = None) -> RemotePage: ...


class EventPort(Protocol):
    """Individual-level events, when a source can supply them.

    Declared and **not implemented**. No tracker or event source has been
    supplied, so an implementation would be a guess at fields nobody has seen.
    The port exists so that later work has a named seam to fill rather than a
    reason to widen the aggregate one; anything calling it today gets an
    explicit refusal, not an empty list that looks like "no events".
    """

    def fetch_events(self, scope: RemoteScope, cursor: str | None = None) -> RemotePage: ...


def iterate_pages(
    fetch: Any, *, first_cursor: str | None = None, max_pages: int = 10_000
) -> Iterator[RemotePage]:
    """Walk a paginated port until it says there is no more.

    ``max_pages`` is a stop, not a limit anyone should reach: a remote system
    that keeps returning a next cursor forever would otherwise pull until the
    process dies, and a run that never finishes is indistinguishable from one
    that hangs.
    """
    cursor = first_cursor
    for _ in range(max_pages):
        page = fetch(cursor)
        yield page
        if page.is_last:
            return
        cursor = page.next_cursor
    raise RuntimeError(f"pagination did not terminate within {max_pages} pages")


__all__ = [
    "AggregateDataPort",
    "AnalyticsPort",
    "EventPort",
    "MetadataPort",
    "RemoteDataElement",
    "RemoteDataValue",
    "RemoteOrganisationUnit",
    "RemotePage",
    "RemoteScope",
    "iterate_pages",
]
