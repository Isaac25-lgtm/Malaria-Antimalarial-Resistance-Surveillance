"""Structured results from a geography import.

Every import produces one of these whether it succeeded, failed validation or
was a no-op. It is written to ``boundary_version.validation_summary`` so the
outcome is queryable long after the run, and returned to the caller so a CLI or
worker can report without re-reading the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ImportOutcome(str, Enum):
    """What an import run actually did."""

    PUBLISHED = "published"
    """Validation passed and the hierarchy was published."""

    VALIDATED_ONLY = "validated_only"
    """A dry run. Nothing was written."""

    ALREADY_IMPORTED = "already_imported"
    """These exact source bytes are already published. No second version was
    created, because identical bytes must not produce competing versions."""

    VALIDATION_FAILED = "validation_failed"
    """Validation failed. The previous published version is untouched and the
    failed attempt is retained with its report."""

    FAILED = "failed"
    """An unexpected error. Nothing was published."""


@dataclass(slots=True)
class LevelCounts:
    """Units produced at one hierarchy level."""

    level: str
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    with_geometry: int = 0
    quarantined_geometry: int = 0
    repaired_geometry: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "created": self.created,
            "updated": self.updated,
            "deactivated": self.deactivated,
            "total": self.total,
            "with_geometry": self.with_geometry,
            "quarantined_geometry": self.quarantined_geometry,
            "repaired_geometry": self.repaired_geometry,
        }


@dataclass(slots=True)
class ValidationIssue:
    """One problem found during validation.

    ``blocking`` separates a defect that must stop publication from one that is
    recorded and carried forward. A degenerate ring is repairable; a duplicate
    canonical code is not.
    """

    code: str
    detail: str
    blocking: bool = False
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "blocking": self.blocking,
            "context": self.context,
        }


@dataclass(slots=True)
class ImportResult:
    """The full outcome of one import run."""

    outcome: ImportOutcome
    boundary_version_id: uuid.UUID | None = None
    boundary_version_code: str | None = None
    combined_checksum: str | None = None
    importer_version: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0

    sources: list[dict[str, Any]] = field(default_factory=list)
    levels: dict[str, LevelCounts] = field(default_factory=dict)
    issues: list[ValidationIssue] = field(default_factory=list)
    control_totals: dict[str, Any] = field(default_factory=dict)
    aliases_written: int = 0
    geometry_written: int = 0
    geometry_quarantined: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.blocking]

    @property
    def succeeded(self) -> bool:
        return self.outcome in (
            ImportOutcome.PUBLISHED,
            ImportOutcome.ALREADY_IMPORTED,
            ImportOutcome.VALIDATED_ONLY,
        )

    def add_issue(
        self,
        code: str,
        detail: str,
        *,
        blocking: bool = False,
        context: dict[str, Any] | None = None,
    ) -> ValidationIssue:
        """Record one validation finding.

        ``context`` is an explicit mapping rather than keyword arguments, so a
        caller can put a key named ``code`` in it - which is common, since most
        findings are about a specific geography code - without colliding with
        this method's own parameter.
        """
        issue = ValidationIssue(code=code, detail=detail, blocking=blocking, context=context or {})
        self.issues.append(issue)
        return issue

    def level(self, name: str) -> LevelCounts:
        if name not in self.levels:
            self.levels[name] = LevelCounts(level=name)
        return self.levels[name]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "boundary_version_id": str(self.boundary_version_id)
            if self.boundary_version_id
            else None,
            "boundary_version_code": self.boundary_version_code,
            "combined_checksum": self.combined_checksum,
            "importer_version": self.importer_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "sources": list(self.sources),
            "levels": {name: counts.as_dict() for name, counts in self.levels.items()},
            "issues": [issue.as_dict() for issue in self.issues],
            "blocking_issue_count": len(self.blocking_issues),
            "control_totals": dict(self.control_totals),
            "aliases_written": self.aliases_written,
            "geometry_written": self.geometry_written,
            "geometry_quarantined": self.geometry_quarantined,
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        units = sum(counts.total for counts in self.levels.values())
        return (
            f"{self.outcome.value}: {units} units across {len(self.levels)} levels, "
            f"{self.geometry_written} with geometry, "
            f"{self.geometry_quarantined} quarantined, "
            f"{len(self.blocking_issues)} blocking issues"
        )
