"""The inbound contract MARS accepts encounters in.

MARS-owned, and deliberately so. No authoritative external e-register API has
been supplied, and building an adapter against a system whose fields nobody has
seen would mean inventing them. MARS publishes what it will accept; a source
system, or an adapter written for one, produces it.

The full contract, including the value sets and the versioning rules, is
``docs/data-dictionary/ereg-inbound-contract.md``. This module is its executable
half: the types, the parser, and the adapter protocol a future source implements.

**Nothing here writes anything.** Parsing produces objects; deciding what to do
with them is the pipeline's job. That separation is what lets a dry run read a
whole file and report exactly what a load would do, without a transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

#: Contract versions this build understands.
#:
#: An unknown version quarantines the whole batch rather than attempting a
#: best-effort read. Guessing a mapping is how a field silently lands in the
#: wrong column, and the failure then surfaces as clinical nonsense months later
#: rather than as an import error today.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

#: Bumped when the pipeline's behaviour changes in a way that could alter what a
#: given artefact produces. Recorded on every encounter, so a row can be traced
#: to the code that wrote it.
INGEST_METHOD_VERSION = "1.0.0"

#: Fields a producer must not send, and why refusing beats ignoring.
#:
#: OPD 002 column 8 exists on the form; MARS stores next of kin nowhere.
#: Accepting and dropping it would leave the producer believing MARS holds it,
#: which is the worst of both outcomes.
FORBIDDEN_ROW_FIELDS: frozenset[str] = frozenset({"next_of_kin", "next_of_kin_contact"})


class ContractError(ValueError):
    """The artefact is not readable as this contract.

    Distinct from a row being invalid: this means the file itself cannot be
    interpreted, so no row-level outcome is meaningful.
    """


@dataclass(frozen=True, slots=True)
class InboundIdentity:
    """The identity block of one row.

    The only object in the contract carrying direct identifiers. It is consumed
    inside the identity boundary and never reaches ``mars_core``, a log, or a
    quarantine row.
    """

    identifier_type: str | None = None
    identifier_value: str | None = None
    surname: str | None = None
    given_name: str | None = None
    phone_contact: str | None = None

    def __repr__(self) -> str:
        """Never render the values.

        This object exists to be passed to the identity service and discarded.
        A ``repr`` reaches tracebacks, and a traceback reaches logs.
        """
        present = [
            name
            for name, value in (
                ("identifier", self.identifier_value),
                ("surname", self.surname),
                ("given_name", self.given_name),
                ("phone", self.phone_contact),
            )
            if value
        ]
        return f"InboundIdentity(present={present!r})"

    @property
    def is_empty(self) -> bool:
        """Whether the row carried nothing identifying.

        The common case, and an honest one: many register rows have no usable
        identifier, and the encounter still loads without a patient reference.
        """
        return not any((self.identifier_value, self.surname, self.given_name, self.phone_contact))


@dataclass(frozen=True, slots=True)
class InboundEnvelope:
    """The first line of a batch file."""

    schema_version: str
    source_system: str
    facility_code: str
    extracted_at: datetime | None
    declared_row_count: int
    register_opened_on: date | None = None
    register_closed_on: date | None = None


@dataclass(slots=True)
class InboundRow:
    """One encounter as the source described it.

    ``raw`` keeps the original object so the pipeline can store a redacted copy
    without re-serialising, and so an operator can see what the source actually
    sent rather than what MARS made of it.
    """

    source_row_id: str
    line_number: int
    raw: dict[str, Any]
    identity: InboundIdentity = field(default_factory=InboundIdentity)

    @property
    def redacted(self) -> dict[str, Any]:
        """The row with its identity block removed.

        Removed, not masked. A masked identifier is still an identifier, and the
        quarantine table is read by operators, analysts and anyone debugging an
        import - none of whom hold the re-identification permission.
        """
        return {key: value for key, value in self.raw.items() if key != "identity"}


class InboundAdapter(Protocol):
    """What a source system must provide.

    The JSONL reader below is the reference implementation. A CSV adapter, or
    one speaking a real vendor API, produces the same objects and the rest of
    the pipeline is unchanged.

    **No adapter may invent a field.** A source that does not carry a malaria
    result produces rows without one, and the encounter records that no test was
    done rather than guessing that one was.
    """

    source_system: str

    def envelope(self, artefact: Path) -> InboundEnvelope: ...

    def rows(self, artefact: Path) -> Iterator[InboundRow]: ...


class JsonLinesAdapter:
    """The reference adapter: one JSON object per line, envelope first.

    Streaming, so a national extract does not have to fit in memory and line
    40,000 can be rejected without holding the first 39,999 - and so a truncated
    file is detectable rather than merely unparseable.
    """

    source_system = "jsonl"

    def envelope(self, artefact: Path) -> InboundEnvelope:
        with artefact.open(encoding="utf-8") as handle:
            first = handle.readline()
        if not first.strip():
            raise ContractError("the artefact is empty; expected an envelope on line 1")

        try:
            payload = json.loads(first)
        except json.JSONDecodeError as exc:
            raise ContractError(f"line 1 is not valid JSON: {exc.msg}") from exc

        if not isinstance(payload, dict) or payload.get("record_type") != "envelope":
            raise ContractError("line 1 is not an envelope record")

        missing = [
            name
            for name in (
                "schema_version",
                "source_system",
                "facility_code",
                "extracted_at",
                "row_count",
            )
            if payload.get(name) in (None, "")
        ]
        if missing:
            raise ContractError(f"the envelope is missing required fields: {missing}")

        return InboundEnvelope(
            schema_version=str(payload["schema_version"]),
            source_system=str(payload["source_system"]),
            facility_code=str(payload["facility_code"]),
            extracted_at=_parse_timestamp(payload.get("extracted_at")),
            declared_row_count=int(payload["row_count"]),
            register_opened_on=_parse_date(payload.get("register_opened_on")),
            register_closed_on=_parse_date(payload.get("register_closed_on")),
        )

    def rows(self, artefact: Path) -> Iterator[InboundRow]:
        """Every encounter line after the envelope.

        A malformed line raises rather than being skipped. A skipped line is a
        row nobody knows is missing, and the whole point of the counters is that
        a short import cannot pass for a quiet week.
        """
        with artefact.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if number == 1 or not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ContractError(f"line {number} is not valid JSON: {exc.msg}") from exc

                if not isinstance(payload, dict):
                    raise ContractError(f"line {number} is not a JSON object")
                if payload.get("record_type") != "encounter":
                    raise ContractError(
                        f"line {number} has record_type "
                        f"{payload.get('record_type')!r}; expected 'encounter'"
                    )

                present = FORBIDDEN_ROW_FIELDS & set(payload)
                if present:
                    raise ContractError(
                        f"line {number} carries {sorted(present)}, which MARS stores "
                        "nowhere. Refused rather than dropped, so the producer does "
                        "not believe it was kept."
                    )

                source_row_id = payload.get("source_row_id")
                if not source_row_id:
                    raise ContractError(
                        f"line {number} has no source_row_id; it is what makes a "
                        "replay idempotent and cannot be defaulted"
                    )

                yield InboundRow(
                    source_row_id=str(source_row_id),
                    line_number=number,
                    raw=payload,
                    identity=_parse_identity(payload.get("identity")),
                )


def _parse_identity(block: Any) -> InboundIdentity:
    if not isinstance(block, dict):
        return InboundIdentity()
    return InboundIdentity(
        identifier_type=_clean(block.get("identifier_type")),
        identifier_value=_clean(block.get("identifier_value")),
        surname=_clean(block.get("surname")),
        given_name=_clean(block.get("given_name")),
        phone_contact=_clean(block.get("phone_contact")),
    )


def _clean(value: Any) -> str | None:
    """Blank is not a value.

    ``None``, ``""`` and whitespace all mean *not recorded*, and none of them
    becomes a default.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"not a valid RFC 3339 timestamp: {value!r}") from exc


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ContractError(f"not a valid ISO date: {value!r}") from exc


__all__ = [
    "FORBIDDEN_ROW_FIELDS",
    "INGEST_METHOD_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ContractError",
    "InboundAdapter",
    "InboundEnvelope",
    "InboundIdentity",
    "InboundRow",
    "JsonLinesAdapter",
]
