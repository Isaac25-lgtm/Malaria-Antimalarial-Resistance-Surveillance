"""Module boundaries.

Architectural rules that are easy to state and easy to violate accidentally.
Each one is recorded in an ADR; these tests are what stop the rule decaying into
a comment nobody reads.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

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


class TestTheDhis2AdapterIsALeaf:
    """ADR 0003: no external system's shape reaches the core.

    The rule is only real if something checks it. An adapter import inside the
    domain would make a DHIS2 field-rename a domain change, and the coupling is
    invisible until the day DHIS2 changes.
    """

    #: Everything that must be able to run with the adapter deleted.
    INDEPENDENT: ClassVar[list[str]] = [
        "domain",
        "services",
        "analytics",
        "signals",
        "explainability",
        "investigations",
        "geo",
        "api",
    ]

    @pytest.mark.parametrize("package", INDEPENDENT)
    def test_no_module_imports_the_dhis2_adapter(self, package: str) -> None:
        offenders: list[tuple[str, str]] = []
        for path in _package_files(package):
            for module in _imports_of(path):
                if module.startswith("mars.integrations.dhis2"):
                    offenders.append((str(path.relative_to(SRC)), module))
        assert not offenders, (
            f"{package} imports the DHIS2 adapter; the domain must depend on the "
            f"ports in mars.integrations.ports instead: {offenders}"
        )

    def test_the_ports_module_names_no_external_system(self) -> None:
        """A port that mentions DHIS2 is not a seam, it is the adapter with a
        different filename."""
        source = (SRC / "integrations" / "ports.py").read_text(encoding="utf-8")
        body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
        # The module docstring may say what it is *not* coupled to; the code
        # must not name it at all.
        code = body.split('"""', 2)[-1]
        for system in ("dhis2", "DHIS2", "httpx"):
            assert system not in code, f"the ports module names {system}"

    def test_the_adapter_does_not_import_analytics_or_signals(self) -> None:
        """The dependency runs one way. An adapter reaching into analytics would
        make the exchange layer part of the calculation."""
        offenders: list[tuple[str, str]] = []
        for path in _package_files("integrations"):
            for module in _imports_of(path):
                if module.startswith(("mars.analytics", "mars.signals", "mars.explainability")):
                    offenders.append((str(path.relative_to(SRC)), module))
        assert not offenders, f"the integration layer imports analytics: {offenders}"


#: Packages that no implemented prompt fills yet. A stub that looks implemented
#: is worse than an absent module, because a reader cannot tell the difference
#: without opening it.
#:
#: ``ingestion`` left this list at Prompt 5 (geography importer) and
#: ``analytics`` at Prompt 13 (indicator registry and aggregation). Each
#: entry is removed only by the prompt that genuinely fills it.
UNIMPLEMENTED_PACKAGES = ["signals", "explainability", "investigations"]


class TestPlaceholderPackagesAreEmpty:
    """Capabilities not yet implemented must stay visibly absent."""

    @pytest.mark.parametrize("package", UNIMPLEMENTED_PACKAGES)
    def test_package_contains_only_its_docstring(self, package: str) -> None:
        files = _package_files(package)
        assert files, f"{package} package is missing entirely"
        non_init = [f for f in files if f.name != "__init__.py"]
        assert not non_init, (
            f"{package} contains implementation, but no prompt implemented so far "
            f"defines that capability: {[str(f.relative_to(SRC)) for f in non_init]}"
        )

    @pytest.mark.parametrize("package", UNIMPLEMENTED_PACKAGES)
    def test_docstring_names_the_prompt_that_fills_it(self, package: str) -> None:
        """A reader should know when the module becomes real."""
        source = (SRC / package / "__init__.py").read_text(encoding="utf-8")
        assert "Prompt" in source, f"{package}/__init__.py does not say which phase implements it"


class TestIngestionContainsOnlyWhatHasBeenBuilt:
    """``ingestion`` grows one sub-package per prompt, and no faster.

    Prompt 5 added ``geography``; Prompt 9 added ``encounters``. Aggregate
    ingestion and the DHIS2 adapter arrive with later prompts. Listing the
    permitted set here stops a stub for one of those appearing before the
    prompt that owns it - which is how a half-built module starts being
    imported and then has to be kept.
    """

    #: Update this only when the prompt that owns the sub-package is done.
    IMPLEMENTED: ClassVar[list[str]] = ["aggregate", "encounters", "geography"]

    def test_only_the_implemented_sub_packages_exist(self) -> None:
        directory = SRC / "ingestion"
        subpackages = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_dir() and path.name != "__pycache__"
        )
        assert subpackages == self.IMPLEMENTED, (
            f"ingestion contains {subpackages}; implemented so far: {self.IMPLEMENTED}"
        )

    def test_no_loose_modules_beside_the_sub_package(self) -> None:
        directory = SRC / "ingestion"
        modules = sorted(path.name for path in directory.glob("*.py") if path.name != "__init__.py")
        assert modules == [], f"unexpected modules directly under ingestion: {modules}"

    def test_the_importer_does_not_reach_into_api_or_analytics(self) -> None:
        """Ingestion is a domain service; it must not depend on the web layer."""
        offenders: list[tuple[str, str]] = []
        for path in _package_files("ingestion"):
            for module in _imports_of(path):
                if module.startswith(("mars.api", "mars.analytics", "mars.signals")):
                    offenders.append((str(path.relative_to(SRC)), module))
        assert not offenders, f"ingestion imports a forbidden layer: {offenders}"
