"""Worker job for the geography import.

The import reads 226 MB of source and dissolves geometry in PostGIS. That is
minutes of work, not milliseconds, so it belongs on the worker rather than in a
request (blueprint section 061).

The job is idempotent by source checksum: submitting the same sources twice
returns the existing published version rather than creating a competing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mars.core.logging import get_logger
from mars.db.session import session_scope
from mars.ingestion.geography.importer import GeographyImporter, ImportOptions
from mars.ingestion.geography.reader import SourceRole
from mars.ingestion.geography.result import ImportOutcome, ImportResult

logger = get_logger(__name__)

JOB_NAME = "geography.import"


@dataclass(slots=True)
class GeographyImportJob:
    """Parameters for one import run."""

    data_dir: Path
    dry_run: bool = False
    load_geometry: bool = True
    derive_geometry: bool = True
    simplify_geometry: bool = True
    force: bool = False
    imported_by: str = "worker:geography.import"
    note: str | None = None

    def idempotency_key(self) -> str:
        """Key a queue can use to collapse duplicate submissions.

        Deliberately coarse: two concurrent submissions for the same directory
        are the same work. The importer's checksum check is the authoritative
        guard; this only avoids running it twice at once.
        """
        return f"{JOB_NAME}:{self.data_dir.resolve()}"

    def to_options(self) -> ImportOptions:
        return ImportOptions(
            dry_run=self.dry_run,
            load_geometry=self.load_geometry,
            derive_geometry=self.derive_geometry,
            simplify_geometry=self.simplify_geometry,
            force=self.force,
            imported_by=self.imported_by,
            note=self.note,
        )


def resolve_sources(data_dir: Path) -> dict[SourceRole, Path]:
    """Locate each source file. Imported here to keep the CLI the single owner."""
    from mars.ingestion.geography.cli import resolve_sources as _resolve

    return _resolve(data_dir)


def run_job(job: GeographyImportJob) -> ImportResult:
    """Execute the import in its own transaction.

    A blocking validation failure keeps its retained attempt - that record is
    the diagnostic - while never publishing a partial hierarchy. An unexpected
    error rolls the whole transaction back.
    """
    logger.info(
        "geography_import_started",
        job=JOB_NAME,
        data_dir=str(job.data_dir),
        dry_run=job.dry_run,
        force=job.force,
    )

    sources = resolve_sources(job.data_dir)
    missing = [path.name for path in sources.values() if not path.exists()]
    if missing:
        logger.error("geography_import_sources_missing", missing=missing)
        raise FileNotFoundError(f"boundary source(s) not found: {missing}")

    with session_scope() as session:
        importer = GeographyImporter(session, sources)
        result = importer.run(job.to_options())

    logger.info(
        "geography_import_finished",
        job=JOB_NAME,
        outcome=result.outcome.value,
        boundary_version=result.boundary_version_code,
        duration_seconds=round(result.duration_seconds, 2),
        geometry_written=result.geometry_written,
        geometry_quarantined=result.geometry_quarantined,
        blocking_issues=len(result.blocking_issues),
    )

    if result.outcome is ImportOutcome.FAILED:
        raise RuntimeError("geography import failed; see the retained boundary version")

    return result


def describe() -> dict[str, Any]:
    """Job metadata, for the worker's registry."""
    return {
        "name": JOB_NAME,
        "description": "Import the supplied Uganda boundary sources into PostGIS.",
        "idempotent": True,
        "idempotency": "source checksum; identical bytes return the published version",
        "typical_duration": "minutes - reads 226 MB and dissolves geometry",
    }
