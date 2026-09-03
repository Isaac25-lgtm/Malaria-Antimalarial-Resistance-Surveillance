"""Single import point for every ORM model.

Alembic autogenerate only sees tables that have been imported. Importing them in
one place means a new model is picked up by adding it here, rather than by
remembering to touch the migration environment.
"""

from __future__ import annotations

from mars.db.base import Base
from mars.domain.adjacency import GeographyAdjacency
from mars.domain.aggregate import (
    AggregateObservation,
    AggregateSubmission,
    CommodityStockObservation,
    LaboratoryTestObservation,
    ReconciliationFinding,
)
from mars.domain.anomaly import AnomalyBuild, AnomalyPersistence, TemporalAnomalyResult
from mars.domain.audit import AuditEvent
from mars.domain.baseline import BaselineBuild, BaselineResult
from mars.domain.clustering import SpatialClusterResult, SpatialClusterRun
from mars.domain.encounter import (
    OpdEncounter,
    OpdEncounterDiagnosis,
    OpdEncounterPrescription,
    OpdEncounterReferral,
    OpdEncounterTest,
    PatientReference,
)
from mars.domain.episode import EpisodeBuild, EpisodeCandidate, EpisodeMember
from mars.domain.explanation import SignalExplanation
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
from mars.domain.investigation import (
    Investigation,
    InvestigationEvent,
    InvestigationEvidenceRequest,
    InvestigationFeedback,
)
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
from mars.domain.signal import SignalEvidence, SignalGenerationRun, SurveillanceSignal
from mars.domain.spatial import GeographicAggregationResult, HotspotResult, SpatialRun
from mars.domain.surveillance import (
    CommodityOperationalAlert,
    CommodityStockFact,
    TestingSurveillanceResult,
    TreatmentSurveillanceResult,
)

__all__ = [
    "AggregateObservation",
    "AggregateSubmission",
    "AnomalyBuild",
    "AnomalyPersistence",
    "AuditEvent",
    "Base",
    "BaselineBuild",
    "BaselineResult",
    "BoundaryVersion",
    "CommodityOperationalAlert",
    "CommodityStockFact",
    "CommodityStockObservation",
    "ConfigurationKey",
    "ConfigurationVersion",
    "EpisodeBuild",
    "EpisodeCandidate",
    "EpisodeMember",
    "Facility",
    "FacilityIdentifier",
    "GeographicAggregationResult",
    "GeographyAdjacency",
    "GeographyUnit",
    "GeographyUnitAlias",
    "GeographyUnitGeometry",
    "GeographyUnitRevision",
    "HotspotResult",
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
    "Investigation",
    "InvestigationEvent",
    "InvestigationEvidenceRequest",
    "InvestigationFeedback",
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
    "SignalEvidence",
    "SignalExplanation",
    "SignalGenerationRun",
    "SpatialClusterResult",
    "SpatialClusterRun",
    "SpatialRun",
    "SurveillanceSignal",
    "TemporalAnomalyResult",
    "TestingSurveillanceResult",
    "TreatmentSurveillanceResult",
    "UserAccount",
    "UserFacilityScope",
    "UserGeographyScope",
    "UserRole",
    "UserSensitivityScope",
]
