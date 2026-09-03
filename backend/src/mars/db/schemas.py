"""PostgreSQL schema boundaries.

MARS separates data by sensitivity and by ownership at the schema level, not by
convention. The separation exists from the first migration because retrofitting
it means rewriting every foreign key that crosses it.

    mars_core       Canonical surveillance data. Geography, organisation units,
                    facilities, encounters and aggregates. Contains no direct
                    patient identifier.

    mars_identity   Direct patient identifiers and deterministic linkage tokens.
                    Owned by a separate database role; the application role
                    holds no grant on it.

    mars_audit      Append-only audit events. Insert-only from the application's
                    perspective; no update or delete path is exposed.

    mars_security   Users, roles, permissions and the three authorisation scope
                    axes. Separated from mars_core so that operator access to
                    surveillance data does not imply access to the access model.

    mars_governance Configuration versions and the method/model registry. The
                    record of which rules and thresholds were in force when a
                    given analytical result was produced.

    mars_analytics  Derived and materialised analytical output. Placeholder in
                    phases 1-2; populated from Prompt 13 onwards. Kept separate
                    so it can be rebuilt without touching canonical data.
"""

from __future__ import annotations

from typing import Final

CORE: Final = "mars_core"
IDENTITY: Final = "mars_identity"
AUDIT: Final = "mars_audit"
SECURITY: Final = "mars_security"
GOVERNANCE: Final = "mars_governance"
ANALYTICS: Final = "mars_analytics"

#: Every schema MARS creates, in creation order.
ALL_SCHEMAS: Final[tuple[str, ...]] = (
    CORE,
    IDENTITY,
    AUDIT,
    SECURITY,
    GOVERNANCE,
    ANALYTICS,
)

#: Schemas the application role must never be granted read access to by default.
#: Reading these requires a distinct, separately provisioned role.
RESTRICTED_SCHEMAS: Final[frozenset[str]] = frozenset({IDENTITY})

#: Human-readable purpose, surfaced in documentation and admin tooling.
SCHEMA_PURPOSE: Final[dict[str, str]] = {
    CORE: "Canonical surveillance data. No direct patient identifiers.",
    IDENTITY: "Direct patient identifiers and linkage tokens. Separate restricted role.",
    AUDIT: "Append-only audit events. No update or delete path.",
    SECURITY: "Users, roles, permissions, geography and sensitivity scopes.",
    GOVERNANCE: "Configuration versions and the method/model registry.",
    ANALYTICS: "Derived and materialised analytical output. Rebuildable.",
}
