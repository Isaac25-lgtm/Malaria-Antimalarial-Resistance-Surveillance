"""Geography import.

Builds the administrative hierarchy from the supplied boundary sources and
loads validated geometry into PostGIS.

The sources are immutable: they are opened read-only, their checksums are
recorded on every boundary version, and geometry defects are repaired only in
the derived copy MARS stores. See ADR 0004 for what the audit found and which
source plays which role.
"""

from mars.ingestion.geography.importer import (
    IMPORTER_VERSION,
    GeographyImporter,
    ImportOptions,
)
from mars.ingestion.geography.reader import SourceRole
from mars.ingestion.geography.result import ImportOutcome, ImportResult

__all__ = [
    "IMPORTER_VERSION",
    "GeographyImporter",
    "ImportOptions",
    "ImportOutcome",
    "ImportResult",
    "SourceRole",
]
