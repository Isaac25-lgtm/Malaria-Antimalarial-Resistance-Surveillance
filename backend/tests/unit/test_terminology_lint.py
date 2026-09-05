"""The terminology lint must scan repository content, and only that.

The gate previously walked the filesystem with a hand-maintained
``SKIP_DIRECTORIES`` list. That list drifted from ``.gitignore``: seven local
DHIS2 discovery reports under ``data/discovery/`` failed the build with 56
findings, every one of them a genuine DHIS2 option name for *rifampicin*
resistance. Those are tuberculosis metadata in a file ``.gitignore`` excludes
precisely because it records a run rather than a repository fact.

A gate that fails on text no clone will ever contain trains people to bypass
it, which is the one outcome a terminology rule cannot survive. These tests pin
both halves: ignored artefacts are skipped, and everything a clone would
receive is still scanned.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT_PATH = REPO_ROOT / "scripts" / "terminology_lint.py"

#: A phrase the lint exists to reject, asserted as a claim so the "discussing
#: rather than asserting" exemption does not apply to it.
PROHIBITED_LINE = "Rifampicin resistance detected in this district.\n"


def load_lint() -> ModuleType:
    """Import the script by path; ``scripts/`` is not an installed package.

    Registered in ``sys.modules`` before execution because ``@dataclass``
    resolves its annotations through the module entry, and a module absent from
    the table raises rather than resolving.
    """
    name = "mars_terminology_lint"
    spec = importlib.util.spec_from_file_location(name, LINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lint() -> ModuleType:
    return load_lint()


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A throwaway Git repository that ignores ``ignored/``."""
    git("init", "-q", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    return tmp_path


class TestIgnoredArtefactsAreNotScanned:
    def test_an_ignored_file_is_skipped(self, lint: ModuleType, repository: Path) -> None:
        """The exact regression: a gitignored report must not fail the gate."""
        (repository / "ignored" / "discovery.md").write_text(PROHIBITED_LINE, encoding="utf-8")

        assert lint.scan(repository) == []

    def test_the_same_text_outside_the_ignored_path_still_fails(
        self, lint: ModuleType, repository: Path
    ) -> None:
        """Skipping ignored files must not blunt the rule itself."""
        (repository / "docs.md").write_text(PROHIBITED_LINE, encoding="utf-8")

        findings = lint.scan(repository)

        assert [f.path.name for f in findings] == ["docs.md"]


class TestRepositoryContentIsStillScanned:
    def test_a_tracked_file_is_scanned(self, lint: ModuleType, repository: Path) -> None:
        path = repository / "tracked.md"
        path.write_text(PROHIBITED_LINE, encoding="utf-8")
        git("add", "tracked.md", cwd=repository)

        assert [f.path.name for f in lint.scan(repository)] == ["tracked.md"]

    def test_an_untracked_but_unignored_file_is_scanned(
        self, lint: ModuleType, repository: Path
    ) -> None:
        """A file added but not yet committed is about to become content."""
        (repository / "new.md").write_text(PROHIBITED_LINE, encoding="utf-8")

        assert [f.path.name for f in lint.scan(repository)] == ["new.md"]

    def test_a_clean_repository_reports_nothing(
        self, lint: ModuleType, repository: Path
    ) -> None:
        (repository / "fine.md").write_text(
            "A repeat-positive pattern worth investigating.\n", encoding="utf-8"
        )

        assert lint.scan(repository) == []


class TestFallbackWithoutGit:
    def test_a_tree_without_git_is_walked(self, lint: ModuleType, tmp_path: Path) -> None:
        """A source tree extracted from an archive has no ``.git``.

        Scanning everything is the safe default there: it can only be too
        strict, never too permissive.
        """
        (tmp_path / "notes.md").write_text(PROHIBITED_LINE, encoding="utf-8")

        assert [f.path.name for f in lint.scan(tmp_path)] == ["notes.md"]


class TestTheRealRepository:
    def test_the_repository_itself_is_clean(self, lint: ModuleType) -> None:
        """The gate as CI runs it."""
        findings = lint.scan(REPO_ROOT)

        assert findings == [], "\n".join(
            f"{f.path.relative_to(REPO_ROOT).as_posix()}:{f.line_number}" for f in findings
        )
