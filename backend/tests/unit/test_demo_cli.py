"""The demo CLI's refusals.

Almost everything here is about what the CLI *will not* do: invent a district
when the geography is absent, register facilities from a dataset that was never
generated, or delete anything without being told to twice. A demo tool is run
casually, often against whatever database happens to be configured, so its
refusals are the part worth testing.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from mars.demo import cli
from mars.demo.generator import GeneratorOptions
from mars.demo.storylines import STORYLINES


def _executable_source(function: object) -> str:
    """A function's code with its docstring removed.

    So a test asserting that the implementation never mentions "district"
    is not defeated by a docstring explaining why it must not.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    if ast.get_docstring(node) is not None:
        node.body = node.body[1:]
    return ast.unparse(node)


@pytest.fixture(autouse=True)
def no_logging_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(cli, "get_settings", lambda: object())


class _Scope:
    def __init__(self, session: object) -> None:
        self._session = session

    def __enter__(self) -> object:
        return self._session

    def __exit__(self, *_exc: object) -> bool:
        return False


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[object]:
        return []

    def first(self) -> object | None:
        return None

    def scalar_one_or_none(self) -> object | None:
        return None


class _EmptySession:
    """A database with no geography in it."""

    def execute(self, *_args: object, **_kwargs: object) -> _EmptyResult:
        return _EmptyResult()


class TestUsage:
    def test_generate_needs_an_output_directory(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["generate"]) == cli.EXIT_USAGE
        assert "--out-dir is required" in capsys.readouterr().err

    def test_register_needs_an_output_directory(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["register"]) == cli.EXIT_USAGE
        assert "--out-dir is required" in capsys.readouterr().err

    def test_the_cli_defaults_come_from_the_generator(self) -> None:
        """A second copy of the defaults would drift and then lie in --help."""
        args = cli.build_parser().parse_args(["generate", "--out-dir", "x"])
        defaults = GeneratorOptions()
        assert args.seed == defaults.seed
        assert args.start == defaults.period_start
        assert args.end == defaults.period_end


class TestItRefusesToInventGeography:
    def test_generate_stops_when_no_districts_are_imported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A demo built on made-up districts is a map that lies."""
        monkeypatch.setattr(cli, "session_scope", lambda: _Scope(_EmptySession()))

        code = cli.main(["generate", "--out-dir", str(tmp_path / "demo")])

        assert code == cli.EXIT_MISSING_INPUT
        message = capsys.readouterr().err
        assert "mars-import-geography" in message
        assert not (tmp_path / "demo").exists()

    def test_a_district_code_that_is_not_in_the_hierarchy_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "session_scope", lambda: _Scope(_EmptySession()))

        code = cli.main(["generate", "--out-dir", str(tmp_path / "demo"), "--district", "NOPE-01"])

        assert code == cli.EXIT_MISSING_INPUT
        assert "NOPE-01" in capsys.readouterr().err

    def test_one_district_is_needed_per_storyline(self) -> None:
        """Fewer districts than storylines means a storyline silently missing."""
        assert len(cli.STORYLINE_ORDER) == len(STORYLINES)


class TestRegisterRefusesAnAbsentDataset:
    def test_it_stops_when_facilities_json_is_not_there(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["register", "--out-dir", str(tmp_path)])
        assert code == cli.EXIT_MISSING_INPUT
        assert "run generate first" in capsys.readouterr().err

    def test_it_reads_the_generated_register_rather_than_regenerating_it(
        self, tmp_path: Path
    ) -> None:
        """Registering from a fresh generation would create facilities that do
        not match the artefacts already written."""
        (tmp_path / "facilities.json").write_text(json.dumps([]), encoding="utf-8")
        body = _executable_source(cli._register)
        assert "facilities.json" in body
        assert "DemoDatasetGenerator" not in body


class TestPurgeIsDeliberate:
    def test_without_confirm_it_only_reports(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        deleted: list[object] = []

        class _CountingResult(_EmptyResult):
            def scalar_one(self) -> int:
                return 0

        class _Session(_EmptySession):
            def execute(self, statement: object, *_a: object, **_k: object) -> _CountingResult:
                rendered = str(statement).upper()
                if rendered.startswith("DELETE"):
                    deleted.append(statement)
                return _CountingResult()

        monkeypatch.setattr(cli, "session_scope", lambda: _Scope(_Session()))

        assert cli.main(["purge"]) == cli.EXIT_OK
        assert deleted == [], "purge deleted without --confirm"
        assert "--confirm" in capsys.readouterr().out

    def test_it_is_scoped_by_the_demo_prefix_and_nothing_else(self) -> None:
        """A purge that took a date range or a district would eventually be
        pointed at real data.

        Asserted against the parsed body with the docstring removed, not
        against the raw text: this docstring itself contains the words the
        assertion forbids, and a test that matched them would be checking its
        own prose.
        """
        body = _executable_source(cli._purge)
        assert "FACILITY_CODE_PREFIX" in body
        assert "SOURCE_SYSTEM" in body
        assert "encounter_date" not in body
        assert "district" not in body
        assert "between" not in body.lower()
