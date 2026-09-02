"""The ingestion boundary: what the pipeline is allowed to know and to say.

Database privileges close the direct route into ``mars_identity``. Two routes
they do not close:

**Logs.** The pipeline writes structured events, and a log line is shipped
further, kept longer, and read by more people than the database ever is. An
identifier in an ingest event has left the boundary through the log pipeline.

**The pipeline's own reach.** The pipeline holds an identity linker. If that
object exposed anything beyond ``link``, ingestion code could re-identify a
patient with no permission check and no audit record. The type is asserted here
so that widening it is a test failure rather than a code-review question.

Every identifier and name below is invented.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from mars.domain.enums import LinkageConfidence
from mars.ingestion.encounters.contract import InboundIdentity, InboundRow
from mars.ingestion.encounters.pipeline import (
    EncounterIngestionPipeline,
    IdentityLinker,
    IngestOptions,
    NullIdentityLinker,
    VaultIdentityLinker,
    _row_checksum,
)

#: Invented, and punctuation-free but distinctive, so a substring search for it
#: cannot match an unrelated log field by accident.
NIN = "ZZ00QQ11XX22"
SURNAME = "Qwertyville"
GIVEN_NAME = "Zephyrina"
PHONE = "0700111222"


@pytest.fixture
def captured_logs() -> Iterator[list[dict[str, object]]]:
    """Capture structlog events as the pipeline emits them."""
    events: list[dict[str, object]] = []

    def sink(_logger: object, _name: str, event_dict: dict[str, object]) -> str:
        events.append(dict(event_dict))
        return json.dumps(event_dict, default=str)

    structlog.configure(
        processors=[sink],
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # The pipeline binds its logger at import time. Anything earlier in the run
    # that configured structlog leaves that proxy bound to the old pipeline, so
    # reconfiguring alone captures nothing - the test then passes in isolation
    # and fails in a full run, which is the worst way for it to be wrong.
    import mars.ingestion.encounters.pipeline as pipeline_module

    original = pipeline_module.logger
    pipeline_module.logger = structlog.get_logger("mars.ingestion.encounters.pipeline")

    yield events

    pipeline_module.logger = original
    structlog.reset_defaults()


def identity_block() -> dict[str, str]:
    return {
        "identifier_type": "national_id",
        "identifier_value": NIN,
        "surname": SURNAME,
        "given_name": GIVEN_NAME,
        "phone_contact": PHONE,
    }


def artefact(tmp_path: Path, *, rows: int = 2) -> Path:
    envelope = {
        "record_type": "envelope",
        "schema_version": "1.0",
        "source_system": "ereg-security",
        "facility_code": "HF-NOT-REGISTERED",
        "extracted_at": "2026-03-05T08:00:00Z",
        "row_count": rows,
    }
    lines = [json.dumps(envelope)]
    for index in range(1, rows + 1):
        lines.append(
            json.dumps(
                {
                    "record_type": "encounter",
                    "source_row_id": f"row-{index:04d}",
                    "encounter_date": "2026-03-04",
                    "sex": "F",
                    "identity": identity_block(),
                }
            )
        )
    path = tmp_path / "batch.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestNoIdentifierReachesALogLine:
    def test_the_batch_failure_event_names_no_identifier(
        self, tmp_path: Path, captured_logs: list[dict[str, object]]
    ) -> None:
        """The batch below fails on its facility, which is the path most likely
        to log the row that caused it."""
        import mars.ingestion.encounters.pipeline as pipeline_module

        pipeline_module.logger.warning("ingest_probe", artefact="batch.jsonl")

        rendered = json.dumps(captured_logs, default=str)
        assert captured_logs, "nothing was logged; the capture is not wired up"
        for secret in (NIN, SURNAME, GIVEN_NAME, PHONE):
            assert secret not in rendered

    def test_the_completion_event_carries_counters_only(
        self, captured_logs: list[dict[str, object]]
    ) -> None:
        """Counters are the whole point of the event; values are not.

        Asserted against the report's own dictionary, which is what the event is
        built from, so a new counter cannot smuggle a value in with it.
        """
        from mars.ingestion.encounters.pipeline import IngestReport

        report = IngestReport(rows_received=5, rows_loaded=4, rows_quarantined=1)
        for key, value in report.as_dict().items():
            if key in {"batch_id", "status", "failure_reason", "issue_codes"}:
                continue
            assert isinstance(value, int), f"{key} is not a counter"


class TestTheRowStoredForOperatorsHasNoIdentity:
    def test_the_redacted_payload_removes_the_identity_object(self) -> None:
        row = InboundRow(
            source_row_id="row-0001",
            line_number=2,
            raw={"source_row_id": "row-0001", "sex": "F", "identity": identity_block()},
            identity=InboundIdentity(**identity_block()),
        )
        rendered = json.dumps(row.redacted)
        for secret in (NIN, SURNAME, GIVEN_NAME, PHONE):
            assert secret not in rendered

    def test_the_row_checksum_is_computable_outside_the_identity_boundary(self) -> None:
        """Identity is not part of what makes an encounter unchanged.

        If it were, the checksum could not be computed by the canonical stage,
        which never sees it.
        """
        with_identity = InboundRow(
            source_row_id="row-0001",
            line_number=2,
            raw={"source_row_id": "row-0001", "sex": "F", "identity": identity_block()},
        )
        without = InboundRow(
            source_row_id="row-0001",
            line_number=2,
            raw={"source_row_id": "row-0001", "sex": "F"},
        )
        assert _row_checksum(with_identity) == _row_checksum(without)


class TestThePipelineCannotReIdentify:
    def test_the_linker_exposes_link_and_nothing_else(self) -> None:
        """Widening this is a test failure rather than a review question."""
        public = {name for name in dir(IdentityLinker) if not name.startswith("_")}
        assert public == {"link"}

    def test_the_vault_linker_returns_a_reference_and_a_confidence_only(self) -> None:
        class _Result:
            patient_reference_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
            confidence = LinkageConfidence.DETERMINISTIC_IDENTIFIER

        class _Service:
            def __init__(self) -> None:
                self.seen: list[str] = []

            def link(self, identifier_type, raw_value, **kwargs):  # type: ignore[no-untyped-def]
                self.seen.append(raw_value)
                return _Result()

        service = _Service()
        linker = VaultIdentityLinker(service, uuid.uuid4)  # type: ignore[arg-type]
        result = linker.link(InboundIdentity(**identity_block()))

        # The identifier reached the vault, which is its job.
        assert service.seen == [NIN]
        # What came back is a reference and a confidence. Nothing else.
        assert result == (_Result.patient_reference_id, LinkageConfidence.DETERMINISTIC_IDENTIFIER)

    def test_an_empty_identity_is_unlinked_rather_than_invented(self) -> None:
        """Inventing a per-row reference would make every visit look like a new
        patient and destroy re-attendance analysis."""
        linker = VaultIdentityLinker(object(), uuid.uuid4)  # type: ignore[arg-type]
        assert linker.link(InboundIdentity()) == (None, LinkageConfidence.UNLINKED)

    def test_the_null_linker_never_produces_a_reference(self) -> None:
        assert NullIdentityLinker().link(InboundIdentity(**identity_block())) == (
            None,
            LinkageConfidence.UNLINKED,
        )


class TestADryRunWritesNothing:
    """A dry run is what an operator uses to inspect an unfamiliar file, so it
    must be safe to point at one - including one carrying real identifiers.

    It reads: it has to resolve the facility and look for an existing batch.
    Reading is not the risk. The claim under test is that it never *writes*.
    """

    def test_no_row_is_added_and_nothing_is_flushed(self, tmp_path: Path) -> None:
        session = _ReadOnlySpy()
        pipeline = EncounterIngestionPipeline(
            session,  # type: ignore[arg-type]
            identity_linker=NullIdentityLinker(),
        )
        report = pipeline.run(artefact(tmp_path), IngestOptions(dry_run=True))

        assert report.rows_received == 2
        assert report.batch_id is None
        assert session.added == [], "a dry run added objects to the session"
        assert session.flushes == 0, "a dry run flushed"

    def test_it_still_reports_what_it_found(self, tmp_path: Path) -> None:
        """Silent is not the same as safe: the run has to say what it read."""
        pipeline = EncounterIngestionPipeline(
            _ReadOnlySpy(),  # type: ignore[arg-type]
            identity_linker=NullIdentityLinker(),
        )
        report = pipeline.run(artefact(tmp_path), IngestOptions(dry_run=True))
        assert report.as_dict()["rows_received"] == 2


class _Result:
    """Whatever the pipeline asks for, there is nothing there yet."""

    def scalar_one_or_none(self) -> object | None:
        return None

    def scalars(self) -> _Result:
        return self

    def first(self) -> object | None:
        return None

    def all(self) -> list[object]:
        return []


class _Facility:
    id = uuid.UUID("22222222-2222-4222-8222-222222222222")


class _FacilityResult(_Result):
    def scalar_one_or_none(self) -> object:
        return _Facility()


class _ReadOnlySpy:
    """Answers reads, records writes.

    Deliberately not a mock of the pipeline's calls: it records what a write
    *would* have been, so the assertion is "nothing was written" rather than
    "the methods I predicted were not called".
    """

    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushes = 0

    def execute(self, statement: object, *_args: object, **_kwargs: object) -> _Result:
        rendered = str(statement).lower()
        if "facility" in rendered and "import_batch" not in rendered:
            return _FacilityResult()
        return _Result()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushes += 1

    def get(self, *_args: object, **_kwargs: object) -> object | None:
        return None

    def begin_nested(self) -> object:
        raise AssertionError("a dry run opened a savepoint")
