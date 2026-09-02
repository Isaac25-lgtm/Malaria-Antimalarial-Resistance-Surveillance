"""Controlled vocabularies.

Every enumerated value used by more than one module lives here so that the
database, the API schema and the frontend contract cannot drift apart.

Nothing in this file encodes a clinical threshold, a surveillance window or an
epidemiological rule. Those are governed configuration (see
``mars.domain.governance``) and are supplied by the malaria programme, not by
the implementation.
"""

from __future__ import annotations

import enum


class GeographyLevel(str, enum.Enum):
    """Administrative hierarchy levels.

    All seven levels exist in the schema from the outset. Parish and village are
    defined but will remain empty: no parish or village boundary data has been
    supplied, and MARS does not fabricate geography.
    """

    COUNTRY = "country"
    REGION = "region"
    DISTRICT = "district"
    COUNTY = "county"
    SUBCOUNTY = "subcounty"
    PARISH = "parish"
    VILLAGE = "village"

    @property
    def depth(self) -> int:
        return _GEOGRAPHY_DEPTH[self]

    @property
    def parent_level(self) -> GeographyLevel | None:
        return _GEOGRAPHY_PARENT[self]


_GEOGRAPHY_DEPTH: dict[GeographyLevel, int] = {
    GeographyLevel.COUNTRY: 0,
    GeographyLevel.REGION: 1,
    GeographyLevel.DISTRICT: 2,
    GeographyLevel.COUNTY: 3,
    GeographyLevel.SUBCOUNTY: 4,
    GeographyLevel.PARISH: 5,
    GeographyLevel.VILLAGE: 6,
}

_GEOGRAPHY_PARENT: dict[GeographyLevel, GeographyLevel | None] = {
    GeographyLevel.COUNTRY: None,
    GeographyLevel.REGION: GeographyLevel.COUNTRY,
    GeographyLevel.DISTRICT: GeographyLevel.REGION,
    GeographyLevel.COUNTY: GeographyLevel.DISTRICT,
    GeographyLevel.SUBCOUNTY: GeographyLevel.COUNTY,
    GeographyLevel.PARISH: GeographyLevel.SUBCOUNTY,
    GeographyLevel.VILLAGE: GeographyLevel.PARISH,
}


class GeographyUnitKind(str, enum.Enum):
    """The local-government form a unit actually takes.

    The supplied subcounty layer mixes rural subcounties, town councils and
    urban divisions at a single hierarchy level. The level says where a unit
    sits; the kind says what it is. Recorded rather than inferred, and set to
    ``unspecified`` unless the source states it.
    """

    UNSPECIFIED = "unspecified"
    RURAL_SUBCOUNTY = "rural_subcounty"
    TOWN_COUNCIL = "town_council"
    URBAN_DIVISION = "urban_division"
    MUNICIPALITY = "municipality"
    CITY = "city"


class OrganisationUnitType(str, enum.Enum):
    """Health-sector organisational hierarchy.

    Deliberately distinct from ``GeographyLevel``. A Health Sub-District is a
    health-sector management unit; it is not equivalent to a county, and MARS
    must not assume it is. The correspondence, where one exists, is recorded as
    data once the Ministry list is supplied.
    """

    NATIONAL = "national"
    REGIONAL_REFERRAL = "regional_referral"
    DISTRICT_HEALTH_OFFICE = "district_health_office"
    HEALTH_SUB_DISTRICT = "health_sub_district"
    FACILITY = "facility"


class FacilityLevel(str, enum.Enum):
    """Uganda health facility levels.

    Values follow the national facility-level nomenclature. The authoritative
    assignment for any given facility comes from the facility master, which has
    not yet been supplied; ``unknown`` is the honest default.
    """

    UNKNOWN = "unknown"
    HC_II = "hc_ii"
    HC_III = "hc_iii"
    HC_IV = "hc_iv"
    GENERAL_HOSPITAL = "general_hospital"
    REGIONAL_REFERRAL_HOSPITAL = "regional_referral_hospital"
    NATIONAL_REFERRAL_HOSPITAL = "national_referral_hospital"
    SPECIALISED_CLINIC = "specialised_clinic"


class FacilityOwnership(str, enum.Enum):
    """Facility ownership category."""

    UNKNOWN = "unknown"
    GOVERNMENT = "government"
    PRIVATE_NOT_FOR_PROFIT = "private_not_for_profit"
    PRIVATE_FOR_PROFIT = "private_for_profit"
    COMMUNITY = "community"


class AliasMatchStatus(str, enum.Enum):
    """Review state of a source-code to MARS-unit mapping.

    Blueprint section 024 and appendix 120: unresolved or ambiguous source text
    stays unresolved. A mapping is never silently promoted to ``confirmed``.
    """

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"


class BoundaryImportStatus(str, enum.Enum):
    """Lifecycle of a boundary version import."""

    REGISTERED = "registered"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    IMPORTED = "imported"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class GeometryValidityState(str, enum.Enum):
    """Outcome of geometry validation.

    Raw supplied geometry is never edited. Defects are recorded here and are
    repaired only in the derived geometry produced by the importer.
    """

    NOT_ASSESSED = "not_assessed"
    VALID = "valid"
    INVALID_REPAIRED = "invalid_repaired"
    INVALID_UNREPAIRED = "invalid_unrepaired"


class LifecycleStatus(str, enum.Enum):
    """Change-control lifecycle for governed configuration and methods.

    Blueprint sections 077 and 078: draft, review, approval, an effective date,
    and the ability to retire and roll back. Exactly one version of a given key
    may be ``active`` at a time.
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


class MethodKind(str, enum.Enum):
    """What kind of governed method a registry entry describes."""

    INDICATOR_DEFINITION = "indicator_definition"
    EPISODE_RULE = "episode_rule"
    TEMPORAL_BASELINE = "temporal_baseline"
    SPATIAL_METHOD = "spatial_method"
    SIGNAL_RULE = "signal_rule"
    SIGNAL_SCORE = "signal_score"
    DATA_QUALITY_RULE = "data_quality_rule"
    STATISTICAL_MODEL = "statistical_model"
    MACHINE_LEARNING_MODEL = "machine_learning_model"


class AuditAction(str, enum.Enum):
    """Auditable actions.

    Blueprint section 066 enumerates the events that must be reconstructable.
    Actions relating to phases not yet implemented are declared here so that the
    vocabulary is stable when those phases land.
    """

    # Authentication and session
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"

    # Authorisation
    ACCESS_DENIED = "access_denied"
    ROLE_ASSIGNED = "role_assigned"
    ROLE_REVOKED = "role_revoked"
    PERMISSION_CHANGED = "permission_changed"
    GEOGRAPHY_SCOPE_CHANGED = "geography_scope_changed"
    SENSITIVITY_SCOPE_CHANGED = "sensitivity_scope_changed"

    # Governance
    CONFIGURATION_CHANGED = "configuration_changed"
    CONFIGURATION_ACTIVATED = "configuration_activated"
    METHOD_REGISTERED = "method_registered"
    METHOD_PROMOTED = "method_promoted"
    METHOD_ROLLED_BACK = "method_rolled_back"

    # Reference data
    GEOGRAPHY_IMPORTED = "geography_imported"
    ORGANISATION_UNIT_CHANGED = "organisation_unit_changed"
    FACILITY_CHANGED = "facility_changed"

    # Reserved for later phases; declared so the vocabulary does not churn.
    DATA_IMPORTED = "data_imported"
    SIGNAL_CREATED = "signal_created"
    SIGNAL_TRIAGED = "signal_triaged"
    INVESTIGATION_UPDATED = "investigation_updated"
    CASE_EVIDENCE_ACCESSED = "case_evidence_accessed"
    REIDENTIFICATION_PERFORMED = "reidentification_performed"
    EXPORT_GENERATED = "export_generated"
    REPORT_GENERATED = "report_generated"
    AI_REQUEST_SUBMITTED = "ai_request_submitted"


class AuditOutcome(str, enum.Enum):
    """Whether the audited action succeeded."""

    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"
