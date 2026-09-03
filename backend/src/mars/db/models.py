"""Single import point for every ORM model.

Alembic autogenerate only sees tables that have been imported. Importing them in
one place means a new model is picked up by adding it here, rather than by
remembering to touch the migration environment.
"""

from __future__ import annotations

from mars.db.base import Base
from mars.domain.aggregate import (
    AggregateObservation,
    AggregateSubmission,
    CommodityStockObservation,
    LaboratoryTestObservation,
    ReconciliationFinding,
)
from mars.domain.audit import AuditEvent
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterDiagnosis,
    OpdEncounterPrescription,
    OpdEncounterReferral,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.episode import EpisodeBuild, EpisodeCandidate, EpisodeMember
from mars.domain.geography import (
    BoundaryVersion,
    GeographyUnit,
    GeographyUnitAlias,
    GeographyUnitGeometry,
    GeographyUnitRevision,
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
from mars.domain.indicator import (
    IndicatorDefinition,
    IndicatorDefinitionVersion,
    IndicatorResult,
)
from mars.domain.ingestion import (
    ImportBatch,
    ImportSourceRow,
    ImportStageExecution,
    ImportValidationIssue,
)
from mars.domain.integration import IntegrationMappingProposal, IntegrationRun
from mars.domain.organisation import Facility, FacilityIdentifier, OrganisationUnit
from mars.domain.recurrence import RecurrenceResult
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
    "AggregateObservation",
    "AggregateSubmission",
    "AuditEvent",
    "Base",
    "BoundaryVersion",
    "CommodityStockObservation",
    "ConfigurationKey",
    "ConfigurationVersion",
    "EpisodeBuild",
    "EpisodeCandidate",
    "EpisodeMember",
    "Facility",
    "FacilityIdentifier",
    "GeographyUnit",
    "GeographyUnitAlias",
    "GeographyUnitGeometry",
    "GeographyUnitRevision",
    "IdentityIdentifier",
    "IdentityRecord",
    "ImportBatch",
    "ImportSourceRow",
    "ImportStageExecution",
    "ImportValidationIssue",
    "IndicatorDefinition",
    "IndicatorDefinitionVersion",
    "IndicatorResult",
    "IntegrationMappingProposal",
    "IntegrationRun",
    "LaboratoryTestObservation",
    "MethodDefinition",
    "MethodVersion",
    "OpdEncounter",
    "OpdEncounterDiagnosis",
    "OpdEncounterPrescription",
    "OpdEncounterReferral",
    "OpdEncounterTest",
    "OrganisationUnit",
    "PatientReference",
    "ReconciliationFinding",
    "RecurrenceResult",
    "ReidentificationEvent",
    "Role",
    "RolePermission",
    "UserAccount",
    "UserFacilityScope",
    "UserGeographyScope",
    "UserRole",
    "UserSensitivityScope",
]
