"""Single import point for every ORM model.

Alembic autogenerate only sees tables that have been imported. Importing them in
one place means a new model is picked up by adding it here, rather than by
remembering to touch the migration environment.
"""

from __future__ import annotations

from mars.db.base import Base
from mars.domain.audit import AuditEvent
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterDiagnosis,
    OpdEncounterPrescription,
    OpdEncounterReferral,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.geography import (
    BoundaryVersion,
    GeographyUnit,
    GeographyUnitAlias,
    GeographyUnitGeometry,
)
from mars.domain.governance import (
    ConfigurationKey,
    ConfigurationVersion,
    MethodDefinition,
    MethodVersion,
)
from mars.domain.identity import (
    IdentityIdentifier,
    IdentityRecord,
    ReidentificationEvent,
)
from mars.domain.organisation import Facility, FacilityIdentifier, OrganisationUnit
from mars.domain.security import (
    Role,
    RolePermission,
    UserAccount,
    UserFacilityScope,
    UserGeographyScope,
    UserRole,
    UserSensitivityScope,
)

__all__ = [
    "AuditEvent",
    "Base",
    "BoundaryVersion",
    "ConfigurationKey",
    "ConfigurationVersion",
    "Facility",
    "FacilityIdentifier",
    "GeographyUnit",
    "GeographyUnitAlias",
    "GeographyUnitGeometry",
    "IdentityIdentifier",
    "IdentityRecord",
    "MethodDefinition",
    "MethodVersion",
    "OpdEncounter",
    "OpdEncounterDiagnosis",
    "OpdEncounterPrescription",
    "OpdEncounterReferral",
    "OpdEncounterTest",
    "OrganisationUnit",
    "PatientReference",
    "ReidentificationEvent",
    "Role",
    "RolePermission",
    "UserAccount",
    "UserFacilityScope",
    "UserGeographyScope",
    "UserRole",
    "UserSensitivityScope",
]
