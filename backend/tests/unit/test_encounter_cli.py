"""The ingestion CLI's contract with whatever schedules it.

A scheduler branches on the exit code, so the codes are part of the interface
and are asserted here rather than left to be discovered in production. The one
that matters most is 4: a deployment whose identity component is missing must
**refuse to load**, because loading would silently record every encounter as a
new person and the damage is invisible until somebody asks how many patients
attended more than once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mars.identity.provisioning import IdentityNotConfiguredError
from mars.ingestion.encounters import cli
from mars.ingestion.encounters.pipeline import IngestReport, NullIdentityLinker


@pytest.fixture(autouse=True)
def no_logging_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI configures logging from settings; the tests do not need it."""
    monkeypatch.setattr(cli, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(cli, "get_settings", lambda: object())


class TestUsageErrorsExitTwo:
    def test_a_missing_file_argument(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["load"]) == cli.EXIT_USAGE
        assert "--file is required" in capsys.readouterr().err

    def test_a_file_that_does_not_exist(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nowhere.jsonl"
        assert cli.main(["load", "--file", str(missing)]) == cli.EXIT_USAGE
        assert "is not a file" in capsys.readouterr().err

    def test_a_batch_id_that_is_not_a_uuid(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["status", "--batch", "not-a-uuid"]) == cli.EXIT_USAGE
        assert "is not a batch id" in capsys.readouterr().err

    def test_status_without_a_batch(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["status"]) == cli.EXIT_USAGE
        assert "--batch is required" in capsys.readouterr().err


class TestAMissingIdentityComponentRefusesToLoad:
    def test_it_exits_four_rather_than_loading_everything_unlinked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        artefact = tmp_path / "batch.jsonl"
        artefact.write_text("{}\n", encoding="utf-8")

        def unconfigured() -> None:
            raise IdentityNotConfiguredError("no identity database url")

        monkeypatch.setattr(cli, "get_identity_session_factory", lambda: unconfigured)

        code = cli.main(["load", "--file", str(artefact)])
        assert code == cli.EXIT_IDENTITY_UNAVAILABLE
        message = capsys.readouterr().err
        assert "--no-identity" in message
        assert "new person" in message

    def test_no_identity_is_an_explicit_deliberate_choice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing the flag is how an operator says they meant it."""
        monkeypatch.setattr(
            cli,
            "get_identity_session_factory",
            lambda: (_ for _ in ()).throw(AssertionError("identity must not be reached")),
        )
        linker, session = cli._build_linker(no_identity=True)
        assert isinstance(linker, NullIdentityLinker)
        assert session is None


class TestTheOutcomeCodesDistinguishWhoHasWork:
    @pytest.mark.parametrize(
        ("report", "expected"),
        [
            (IngestReport(rows_loaded=5), cli.EXIT_OK),
            (IngestReport(rows_loaded=4, rows_quarantined=1), cli.EXIT_QUARANTINED),
        ],
    )
    def test_quarantined_rows_are_reported_separately_from_success(
        self,
        report: IngestReport,
        expected: int,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        artefact = tmp_path / "batch.jsonl"
        artefact.write_text("{}\n", encoding="utf-8")

        monkeypatch.setattr(cli, "_build_linker", lambda _flag: (NullIdentityLinker(), None))
        monkeypatch.setattr(cli, "session_scope", _fake_scope)
        monkeypatch.setattr(cli, "EncounterIngestionPipeline", _pipeline_returning(report))

        assert cli.main(["load", "--file", str(artefact)]) == expected

    def test_a_failed_batch_is_its_own_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 is the producer's problem, 4 is the operator's. A scheduler that
        cannot tell them apart pages the wrong person."""
        from mars.domain.enums import ImportBatchStatus

        artefact = tmp_path / "batch.jsonl"
        artefact.write_text("{}\n", encoding="utf-8")

        report = IngestReport(
            status=ImportBatchStatus.FAILED, failure_reason="unsupported schema_version '9.9'"
        )
        monkeypatch.setattr(cli, "_build_linker", lambda _flag: (NullIdentityLinker(), None))
        monkeypatch.setattr(cli, "session_scope", _fake_scope)
        monkeypatch.setattr(cli, "EncounterIngestionPipeline", _pipeline_returning(report))

        assert cli.main(["load", "--file", str(artefact)]) == cli.EXIT_BATCH_FAILED


class TestTheReportIsWrittenWhereAsked:
    def test_the_json_document_carries_every_counter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        artefact = tmp_path / "batch.jsonl"
        artefact.write_text("{}\n", encoding="utf-8")
        destination = tmp_path / "reports" / "run.json"

        report = IngestReport(rows_received=9, rows_loaded=7, rows_quarantined=2)
        monkeypatch.setattr(cli, "_build_linker", lambda _flag: (NullIdentityLinker(), None))
        monkeypatch.setattr(cli, "session_scope", _fake_scope)
        monkeypatch.setattr(cli, "EncounterIngestionPipeline", _pipeline_returning(report))

        cli.main(["load", "--file", str(artefact), "--json", str(destination)])

        written = json.loads(destination.read_text(encoding="utf-8"))
        assert written["rows_received"] == 9
        assert written["rows_loaded"] == 7
        assert written["rows_quarantined"] == 2


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class _FakeScope:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> bool:
        return False


def _fake_scope() -> _FakeScope:
    return _FakeScope()


def _pipeline_returning(report: IngestReport):  # type: ignore[no-untyped-def]
    class _Pipeline:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self, _artefact: Path, _options: object) -> IngestReport:
            return report

    return _Pipeline
