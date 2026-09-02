"""Module boundaries.

Architectural rules that are easy to state and easy to violate accidentally.
Each one is recorded in an ADR; these tests are what stop the rule decaying into
a comment nobody reads.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "mars"


def _imports_of(path: Path) -> set[str]:
    """Every module imported by a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _package_files(package: str) -> list[Path]:
    directory = SRC / package
    if not directory.exists():
        return []
    return sorted(directory.rglob("*.py"))


def _all_source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestAiIsALeafDependency:
    """ADR 0008: disabling the assistant must change nothing else.

    If any module imported ``mars.ai``, removing the assistant would break that
    module, and the claim that core surveillance works without AI would be
    false.
    """

    def test_no_module_imports_the_ai_package(self) -> None:
        offenders: list[str] = []
        for path in _all_source_files():
            if "ai" in path.relative_to(SRC).parts:
                continue
            for module in _imports_of(path):
                if module == "mars.ai" or module.startswith("mars.ai."):
                    offenders.append(str(path.relative_to(SRC)))
        assert not offenders, (
            "modules importing mars.ai - the assistant must remain a leaf so it "
            f"can be disabled without affecting anything else: {offenders}"
        )

    def test_ai_is_disabled_by_default(self) -> None:
        from mars.core.settings import Settings

        settings = Settings(database_url="postgresql+psycopg://mars:pw@db:5432/mars")
        assert not settings.ai_assistant_enabled


class TestDomainDoesNotImportFramework:
    """ADR 0002: domain models stay testable without a web server.

    A FastAPI import inside a model is how a domain slowly becomes untestable
    except through HTTP.
    """

    FORBIDDEN = ("fastapi", "starlette", "uvicorn")

    @pytest.mark.parametrize("package", ["domain", "geo"])
    def test_no_web_framework_import(self, package: str) -> None:
        offenders: list[tuple[str, str]] = []
        for path in _package_files(package):
            for module in _imports_of(path):
                root = module.split(".")[0]
                if root in self.FORBIDDEN:
                    offenders.append((str(path.relative_to(SRC)), module))
        assert not offenders, f"web framework imported inside {package}: {offenders}"


class TestDomainDoesNotImportIntegrations:
    """ADR 0003: no external system shape reaches the core.

    An adapter translates a source into the canonical model. If the domain
    imported an adapter, a change to that source would become a change to the
    domain.
    """

    @pytest.mark.parametrize("package", ["domain", "services", "geo"])
    def test_no_integration_import(self, package: str) -> None:
        offenders: list[tuple[str, str]] = []
        for path in _package_files(package):
            for module in _imports_of(path):
                if module.startswith("mars.integrations"):
                    offenders.append((str(path.relative_to(SRC)), module))
        assert not offenders, f"integration adapter imported inside {package}: {offenders}"


class TestRoutersStayThin:
    """ADR 0002: authorisation and queries live outside the route handlers."""

    def test_routers_do_not_build_queries(self) -> None:
        """A ``select()`` in a router is business logic in the wrong place."""
        offenders: list[str] = []
        for path in _package_files("api"):
            source = path.read_text(encoding="utf-8")
            if "select(" in source:
                offenders.append(str(path.relative_to(SRC)))
        assert not offenders, (
            f"routers constructing queries directly; move them to a service: {offenders}"
        )

    def test_routers_do_not_import_orm_models_directly(self) -> None:
        """Routers speak in response schemas, not in ORM models."""
        allowed = {"mars.domain.enums"}
        offenders: list[tuple[str, str]] = []
        for path in _package_files("api"):
            for module in _imports_of(path):
                if module.startswith("mars.domain.") and module not in allowed:
                    offenders.append((str(path.relative_to(SRC)), module))
        assert not offenders, f"routers importing ORM models: {offenders}"


class TestPlaceholderPackagesAreEmpty:
    """Phases 1-2 implement no analytics, signals or investigations.

    A stub that looks implemented is worse than an absent module, because a
    reader cannot tell the difference without opening it.
    """

    @pytest.mark.parametrize(
        "package",
        ["analytics", "signals", "explainability", "investigations", "ingestion"],
    )
    def test_package_contains_only_its_docstring(self, package: str) -> None:
        files = _package_files(package)
        assert files, f"{package} package is missing entirely"
        non_init = [f for f in files if f.name != "__init__.py"]
        assert not non_init, (
            f"{package} contains implementation, but phases 1-2 define no such "
            f"capability: {[str(f.relative_to(SRC)) for f in non_init]}"
        )

    @pytest.mark.parametrize(
        "package",
        ["analytics", "signals", "explainability", "investigations", "ingestion"],
    )
    def test_docstring_names_the_prompt_that_fills_it(self, package: str) -> None:
        """A reader should know when the module becomes real."""
        source = (SRC / package / "__init__.py").read_text(encoding="utf-8")
        assert "Prompt" in source, f"{package}/__init__.py does not say which phase implements it"
