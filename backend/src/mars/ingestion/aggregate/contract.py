"""The inbound contract for HMIS 033b and 105 submissions.

MARS-owned and versioned, for the same reason the encounter contract is: no
authoritative external submission API has been supplied, and writing an adapter
against fields nobody has seen would mean inventing them.

One JSON object per line. The envelope first, then one object per submission,
each carrying its own observations, stock rows and laboratory rows. A
submission is a single form, so it arrives whole rather than as loose cells -
a half-transmitted form is then a parse failure rather than a facility that
appears to have reported fewer malaria cases than it did.

**Nothing here writes anything.** Parsing produces objects; deciding what to do
with them belongs to the pipeline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from mars.domain.enums import AggregateForm

#: Contract versions this build understands. An unknown version fails the whole
#: batch: guessing which cell a code means is how a malaria figure lands in a
#: dysentery row.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

#: Bumped when the pipeline's behaviour changes in a way that could alter what
#: a given artefact produces.
INGEST_METHOD_VERSION = "1.0.0"

#: Fields a producer must not send on an aggregate submission, and why refusing
#: beats ignoring.
#:
#: HMIS 033b and 105 are aggregate returns: counts, never people. But the whole
#: inbound object is stored on ``ImportSourceRow.payload_redacted``, whose
#: contract is that identity has been *removed* - and that table is read by
#: operators, analysts and anyone debugging an import, none of whom hold the
#: re-identification permission. A mis-mapped export that attached a line
#: listing would otherwise land patient data in ``mars_core`` with no error.
#:
#: Refused rather than stripped, exactly as the encounter contract refuses next
#: of kin: a producer that believes MARS is holding these values needs to be
#: told it is not.
FORBIDDEN_SUBMISSION_FIELDS: frozenset[str] = frozenset(
    {
        "patient_name",
        "patient_names",
        "surname",
        "given_name",
        "nin",
        "national_id",
        "passport",
        "phone",
        "phone_contact",
        "next_of_kin",
        "identity",
        "patients",
        "line_list",
    }
)

#: Every field this contract defines, at any depth. Used only to decide whether
#: a key is safe to name in an error message: these are MARS's own vocabulary,
#: so echoing one cannot disclose anything a producer sent.
KNOWN_SUBMISSION_FIELDS: frozenset[str] = frozenset(
    {
        # Envelope
        "record_type",
        "schema_version",
        "source_system",
        "extracted_at",
        "submission_count",
        # Submission
        "form",
        "facility_code",
        "period_start",
        "period_end",
        "period_label",
        "reported_on",
        "revision",
        "source_reference",
        "remarks",
        "observations",
        "stock",
        "laboratory",
        # Observation
        "element",
        "value",
        "age_band",
        "sex",
        "raw_value",
        # Stock
        "commodity",
        "metric",
        "unit",
        # Laboratory
        "test",
        "done",
        "positive",
        "raw_done",
        "raw_positive",
    }
)


class AggregateContractError(ValueError):
    """The artefact is not readable as this contract."""


@dataclass(frozen=True, slots=True)
class InboundAggregateEnvelope:
    """The first line of a submission batch."""

    schema_version: str
    source_system: str
    extracted_at: datetime | None
    declared_submission_count: int


@dataclass(slots=True)
class InboundObservation:
    """One cell.

    ``value`` is ``None`` when the cell was blank. That is not the same as a
    reported zero and the two are never merged: HMIS 033b requires a facility
    to report every week whether there are cases or not, so a blank means the
    facility did not answer and a zero means it answered "none".
    """

    element: str
    value: int | None = None
    age_band: str = "unspecified"
    sex: str = "unknown"
    raw_value: str | None = None


@dataclass(slots=True)
class InboundStockRow:
    """One commodity measure."""

    commodity: str
    metric: str
    value: Decimal | None = None
    unit: str | None = None
    raw_value: str | None = None


@dataclass(slots=True)
class InboundLaboratoryRow:
    """One laboratory test row: number done and number positive."""

    test: str
    done: int | None = None
    positive: int | None = None
    raw_done: str | None = None
    raw_positive: str | None = None


@dataclass(slots=True)
class InboundSubmission:
    """One facility's return of one form for one period."""

    form: str
    facility_code: str
    period_start: date
    period_end: date
    line_number: int
    raw: dict[str, Any]
    period_label: str | None = None
    reported_on: date | None = None
    revision: int = 1
    source_reference: str | None = None
    remarks: str | None = None
    observations: list[InboundObservation] = field(default_factory=list)
    stock: list[InboundStockRow] = field(default_factory=list)
    laboratory: list[InboundLaboratoryRow] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, str, int]:
        """What makes this submission the same submission on a re-send."""
        return (
            self.facility_code,
            self.form,
            self.period_start.isoformat(),
            self.period_end.isoformat(),
            self.revision,
        )


class AggregateAdapter(Protocol):
    """What a source of aggregate submissions must provide.

    **No adapter may invent a cell.** A form that left a cell blank produces an
    observation with a null value, never a zero - the difference between a
    facility that reported none and a facility that did not report is the whole
    of reporting completeness.
    """

    source_system: str

    def envelope(self, artefact: Path) -> InboundAggregateEnvelope: ...

    def submissions(self, artefact: Path) -> Iterator[InboundSubmission]: ...


class JsonLinesAggregateAdapter:
    """The reference adapter: envelope first, one submission per line."""

    source_system = "jsonl"

    def envelope(self, artefact: Path) -> InboundAggregateEnvelope:
        with artefact.open(encoding="utf-8") as handle:
            first = handle.readline()
        if not first.strip():
            raise AggregateContractError("the artefact is empty; expected an envelope on line 1")

        try:
            payload = json.loads(first, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            raise AggregateContractError(f"line 1 is not valid JSON: {detail}") from exc

        if not isinstance(payload, dict) or payload.get("record_type") != "envelope":
            raise AggregateContractError("line 1 is not an envelope record")

        missing = [
            name
            for name in ("schema_version", "source_system", "extracted_at", "submission_count")
            if payload.get(name) in (None, "")
        ]
        if missing:
            raise AggregateContractError(f"the envelope is missing required fields: {missing}")

        declared = _required_integer(payload["submission_count"], "submission_count", 1)
        if declared < 0:
            raise AggregateContractError("line 1: submission_count cannot be negative")

        schema_version = _required_bounded_text(payload["schema_version"], "schema_version", 16, 1)
        source_system = _required_bounded_text(payload["source_system"], "source_system", 64, 1)
        return InboundAggregateEnvelope(
            schema_version=schema_version,
            source_system=source_system,
            extracted_at=_timestamp(payload.get("extracted_at")),
            declared_submission_count=declared,
        )

    def submissions(self, artefact: Path) -> Iterator[InboundSubmission]:
        with artefact.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if number == 1 or not line.strip():
                    continue
                try:
                    payload = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                    raise AggregateContractError(
                        f"line {number} is not valid JSON: {detail}"
                    ) from exc

                if not isinstance(payload, dict):
                    raise AggregateContractError(f"line {number} is not a JSON object")
                if payload.get("record_type") != "submission":
                    raise AggregateContractError(
                        f"line {number} has record_type {payload.get('record_type')!r}; "
                        "expected 'submission'"
                    )

                yield _submission(payload, number)


def _submission(payload: dict[str, Any], number: int) -> InboundSubmission:
    for name in ("form", "facility_code", "period_start", "period_end"):
        if not payload.get(name):
            raise AggregateContractError(f"line {number} has no {name}")

    present = _forbidden_field_paths(payload)
    if present:
        raise AggregateContractError(
            f"line {number} carries identity-shaped field(s) at {present}. "
            "An aggregate return is "
            "counts, never people, and the whole submission is stored where "
            "identity must already be absent. Refused rather than stripped, so "
            "the producer does not believe MARS kept it."
        )

    return InboundSubmission(
        form=str(payload["form"]),
        facility_code=str(payload["facility_code"]),
        period_start=_date(payload["period_start"], number),
        period_end=_date(payload["period_end"], number),
        line_number=number,
        raw=payload,
        period_label=_text(payload.get("period_label")),
        reported_on=_optional_date(payload.get("reported_on"), number),
        revision=_required_integer(payload.get("revision", 1), "revision", number),
        source_reference=_text(payload.get("source_reference")),
        remarks=_text(payload.get("remarks")),
        observations=[
            _observation(entry, number, index)
            for index, entry in enumerate(payload.get("observations") or [])
        ],
        stock=[
            _stock(entry, number, index) for index, entry in enumerate(payload.get("stock") or [])
        ],
        laboratory=[
            _laboratory(entry, number, index)
            for index, entry in enumerate(payload.get("laboratory") or [])
        ],
    )


def _normalise_key(key: str) -> str:
    """Fold a JSON key to its comparison form.

    Case and the common separators, so a producer cannot bypass the guard
    accidentally with a spelling such as ``Patient-Name`` or ``NATIONAL ID``.
    """
    return re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")


def _path_segment(key: str) -> str:
    """A key, but only if it is one this contract recognises.

    **A key can itself be an identity value.** ``{"patients": {"Nakato Sarah":
    {...}}}`` is a plausible export shape, and the path this function builds
    ends up in the error message, which the pipeline stores on
    ``import_batch.failure_reason`` - a persisted column operators read. Echoing
    an arbitrary key would move a name out of the payload and into the failure
    reason, which is the same leak wearing a different hat.

    So the path is built from a whitelist: a key is named only when it is a
    field this contract defines or one it explicitly forbids, both of which are
    MARS's own vocabulary. Anything else is reported by shape alone. An
    operator still learns exactly where in the structure the field sits.
    """
    normalised = _normalise_key(key)
    if normalised in KNOWN_SUBMISSION_FIELDS or normalised in FORBIDDEN_SUBMISSION_FIELDS:
        return normalised
    return "<unrecognised-key>"


def _forbidden_field_paths(value: Any, path: str = "$") -> list[str]:
    """Find identity-shaped keys anywhere in the JSON object.

    The original payload is persisted recursively, so checking only its top
    level is not a privacy boundary: a line-list field attached to an
    observation or a metadata object would otherwise pass through unchanged.

    Reports structural paths only. See ``_path_segment`` for why the path can
    never carry a value.
    """
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            nested_path = f"{path}.{_path_segment(key_text)}"
            if _normalise_key(key_text) in FORBIDDEN_SUBMISSION_FIELDS:
                found.append(nested_path)
            found.extend(_forbidden_field_paths(nested, nested_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_forbidden_field_paths(nested, f"{path}[{index}]"))
    return found


def _observation(entry: Any, number: int, index: int) -> InboundObservation:
    if not isinstance(entry, dict) or not entry.get("element"):
        raise AggregateContractError(f"line {number} observation {index} has no element code")
    value, raw_value = _count_cell(entry.get("value"), entry.get("raw_value"))
    return InboundObservation(
        element=str(entry["element"]).strip(),
        value=value,
        age_band=str(entry.get("age_band") or "unspecified").strip().lower(),
        sex=str(entry.get("sex") or "unknown").strip().lower(),
        raw_value=raw_value,
    )


def _stock(entry: Any, number: int, index: int) -> InboundStockRow:
    if not isinstance(entry, dict) or not entry.get("commodity") or not entry.get("metric"):
        raise AggregateContractError(
            f"line {number} stock row {index} needs both a commodity and a metric"
        )
    value, raw_value = _number_cell(entry.get("value"), entry.get("raw_value"))
    return InboundStockRow(
        commodity=str(entry["commodity"]).strip(),
        metric=str(entry["metric"]).strip().lower(),
        value=value,
        unit=_text(entry.get("unit")),
        raw_value=raw_value,
    )


def _laboratory(entry: Any, number: int, index: int) -> InboundLaboratoryRow:
    if not isinstance(entry, dict) or not entry.get("test"):
        raise AggregateContractError(f"line {number} laboratory row {index} has no test code")
    done, raw_done = _count_cell(entry.get("done"), entry.get("raw_done"))
    positive, raw_positive = _count_cell(entry.get("positive"), entry.get("raw_positive"))
    return InboundLaboratoryRow(
        test=str(entry["test"]).strip(),
        done=done,
        positive=positive,
        raw_done=raw_done,
        raw_positive=raw_positive,
    )


def _count(value: Any) -> int | None:
    """A whole number, or ``None`` for a blank cell.

    An unparsable value is left as ``None`` here and reported by the validator
    with the field that carried it. Raising in the reader would fail the whole
    submission for one bad cell, and one bad cell is not a reason to lose a
    facility's month.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        return None
    return int(text)


def _number(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _count_cell(value: Any, supplied_raw: Any) -> tuple[int | None, str | None]:
    parsed = _count(value)
    raw = _text(supplied_raw)
    if parsed is None and value is not None and value != "" and raw is None:
        raw = _text(value)
    return parsed, raw


def _number_cell(value: Any, supplied_raw: Any) -> tuple[Decimal | None, str | None]:
    parsed = _number(value)
    raw = _text(supplied_raw)
    if parsed is None and value is not None and value != "" and raw is None:
        raw = _text(value)
    return parsed, raw


def _required_integer(value: Any, field: str, line: int) -> int:
    parsed = _count(value)
    if parsed is None:
        raise AggregateContractError(f"line {line}: {field} must be a whole number")
    return parsed


def _required_bounded_text(value: Any, field: str, maximum: int, line: int) -> str:
    parsed = _text(value)
    if parsed is None:
        raise AggregateContractError(f"line {line}: {field} is required")
    if len(parsed) > maximum:
        raise AggregateContractError(
            f"line {line}: {field} exceeds the {maximum}-character contract limit"
        )
    return parsed


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any, number: int) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise AggregateContractError(f"line {number}: {value!r} is not an ISO date") from exc


def _optional_date(value: Any, number: int) -> date | None:
    if not value:
        return None
    return _date(value, number)


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregateContractError(f"not a valid RFC 3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise AggregateContractError(f"timestamp has no UTC offset: {value!r}")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant {value!r}")


def parse_form(value: str) -> AggregateForm | None:
    """The form, or ``None`` if MARS does not know it.

    Returned rather than raised: an unknown form is a batch-level finding the
    pipeline records, not an exception that loses the rest of the artefact.
    """
    try:
        return AggregateForm(value.strip().lower())
    except ValueError:
        return None


__all__ = [
    "FORBIDDEN_SUBMISSION_FIELDS",
    "INGEST_METHOD_VERSION",
    "KNOWN_SUBMISSION_FIELDS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "AggregateAdapter",
    "AggregateContractError",
    "InboundAggregateEnvelope",
    "InboundLaboratoryRow",
    "InboundObservation",
    "InboundStockRow",
    "InboundSubmission",
    "JsonLinesAggregateAdapter",
    "parse_form",
]
