"""Permission catalogue and the three authorisation axes.

Authorisation in MARS is three independent checks, not one:

1. **Permission** - may this role perform this kind of action at all?
2. **Geography scope** - does the caller's assigned area cover the requested
   area? A district user cannot read another district, at the query level.
3. **Sensitivity scope** - may the caller see this level of patient detail?
   National aggregate access does not imply patient detail, and administrator
   implies neither (blueprint section 009).

All three are enforced server-side. Hiding a control in the interface is not
access control.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class SensitivityLevel(int, enum.Enum):
    """Data sensitivity tiers, ordered from least to most disclosing."""

    AGGREGATE = 10
    """Counts, rates and signals over administrative areas. No individual."""

    PSEUDONYMOUS_CASE = 20
    """Case evidence keyed by a MARS display alias. No direct identifier."""

    DIRECT_IDENTITY = 30
    """Name, national identity number, contact details. Re-identification."""

    def covers(self, required: SensitivityLevel) -> bool:
        """True when this level grants access to ``required``."""
        return self.value >= required.value


class Permission(str, enum.Enum):
    """Every permission MARS recognises.

    Adding a value here without adding it to a role grants nothing: roles are
    explicit, and the default is denial.
    """

    # -- Surveillance -----------------------------------------------------
    SURVEILLANCE_VIEW_AGGREGATE = "surveillance:view_aggregate"
    """View aggregate surveillance metrics, trends, maps and signal summaries."""

    CASE_EVIDENCE_VIEW = "case:view_pseudonymous_evidence"
    """Open pseudonymous case evidence timelines for authorised facilities."""

    PATIENT_REIDENTIFY = "patient:reidentify"
    """Resolve a MARS display alias to a direct identifier. Separately granted,
    always audited, and never implied by any other permission."""

    # -- Outputs ----------------------------------------------------------
    DATA_EXPORT = "data:export"
    """Generate CSV or PDF exports within the caller's scope."""

    REPORT_GENERATE = "report:generate"
    """Produce briefs and investigation packets."""

    # -- Workflow ---------------------------------------------------------
    INVESTIGATION_TRIAGE = "investigation:triage"
    INVESTIGATION_ASSIGN = "investigation:assign"
    INVESTIGATION_UPDATE = "investigation:update"
    INVESTIGATION_CLOSE = "investigation:close"

    # -- Governance -------------------------------------------------------
    CONFIGURATION_VIEW = "configuration:view"
    CONFIGURATION_MANAGE = "configuration:manage"
    METHOD_VIEW = "method:view"
    METHOD_APPROVE = "method:approve"

    # -- Reference data ---------------------------------------------------
    GEOGRAPHY_VIEW = "geography:view"
    GEOGRAPHY_MANAGE = "geography:manage"
    ORGANISATION_VIEW = "organisation:view"
    ORGANISATION_MANAGE = "organisation:manage"
    FACILITY_VIEW = "facility:view"
    FACILITY_MANAGE = "facility:manage"

    # -- Platform ---------------------------------------------------------
    INTEGRATION_MANAGE = "integration:manage"
    USER_ADMINISTER = "user:administer"
    AUDIT_VIEW = "audit:view"
    DATA_QUALITY_VIEW = "data_quality:view"


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    """Descriptive metadata for a permission, surfaced in admin tooling."""

    permission: Permission
    label: str
    description: str
    minimum_sensitivity: SensitivityLevel
    """The sensitivity tier a caller must hold for this permission to be usable.

    A permission and a sensitivity scope are separate grants: holding
    ``CASE_EVIDENCE_VIEW`` without ``PSEUDONYMOUS_CASE`` sensitivity is a
    misconfiguration, and the dependency layer refuses it rather than
    silently upgrading the caller.
    """


PERMISSION_CATALOGUE: dict[Permission, PermissionSpec] = {
    Permission.SURVEILLANCE_VIEW_AGGREGATE: PermissionSpec(
        Permission.SURVEILLANCE_VIEW_AGGREGATE,
        "View aggregate surveillance",
        "National, district and facility aggregate metrics, trends and signals.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.CASE_EVIDENCE_VIEW: PermissionSpec(
        Permission.CASE_EVIDENCE_VIEW,
        "View pseudonymous case evidence",
        "Case evidence timelines identified by MARS display alias only.",
        SensitivityLevel.PSEUDONYMOUS_CASE,
    ),
    Permission.PATIENT_REIDENTIFY: PermissionSpec(
        Permission.PATIENT_REIDENTIFY,
        "Re-identify a patient",
        "Resolve a display alias to a direct identifier. Always audited.",
        SensitivityLevel.DIRECT_IDENTITY,
    ),
    Permission.DATA_EXPORT: PermissionSpec(
        Permission.DATA_EXPORT,
        "Export data",
        "Generate exports within the caller's geography and sensitivity scope.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.REPORT_GENERATE: PermissionSpec(
        Permission.REPORT_GENERATE,
        "Generate reports",
        "Produce national, district and facility reporting products.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.INVESTIGATION_TRIAGE: PermissionSpec(
        Permission.INVESTIGATION_TRIAGE,
        "Triage signals",
        "Review a new signal and decide whether it warrants investigation.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.INVESTIGATION_ASSIGN: PermissionSpec(
        Permission.INVESTIGATION_ASSIGN,
        "Assign investigations",
        "Allocate an investigation to a responsible officer.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.INVESTIGATION_UPDATE: PermissionSpec(
        Permission.INVESTIGATION_UPDATE,
        "Update investigations",
        "Add notes, complete checklist items and record progress.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.INVESTIGATION_CLOSE: PermissionSpec(
        Permission.INVESTIGATION_CLOSE,
        "Close investigations",
        "Record an outcome and close an investigation.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.CONFIGURATION_VIEW: PermissionSpec(
        Permission.CONFIGURATION_VIEW,
        "View configuration",
        "Read governed configuration keys and their active versions.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.CONFIGURATION_MANAGE: PermissionSpec(
        Permission.CONFIGURATION_MANAGE,
        "Manage configuration",
        "Draft, review and activate configuration versions.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.METHOD_VIEW: PermissionSpec(
        Permission.METHOD_VIEW,
        "View methods",
        "Read the method registry and validation references.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.METHOD_APPROVE: PermissionSpec(
        Permission.METHOD_APPROVE,
        "Approve methods",
        "Promote or roll back an analytical method version.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.GEOGRAPHY_VIEW: PermissionSpec(
        Permission.GEOGRAPHY_VIEW,
        "View geography",
        "Read the administrative hierarchy and boundary metadata.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.GEOGRAPHY_MANAGE: PermissionSpec(
        Permission.GEOGRAPHY_MANAGE,
        "Manage geography",
        "Import boundary versions and curate source-code aliases.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.ORGANISATION_VIEW: PermissionSpec(
        Permission.ORGANISATION_VIEW,
        "View organisation units",
        "Read the health-sector organisational hierarchy.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.ORGANISATION_MANAGE: PermissionSpec(
        Permission.ORGANISATION_MANAGE,
        "Manage organisation units",
        "Create and amend organisation units.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.FACILITY_VIEW: PermissionSpec(
        Permission.FACILITY_VIEW,
        "View facilities",
        "Read facility metadata within the caller's geography scope.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.FACILITY_MANAGE: PermissionSpec(
        Permission.FACILITY_MANAGE,
        "Manage facilities",
        "Create and amend facility records and identifiers.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.INTEGRATION_MANAGE: PermissionSpec(
        Permission.INTEGRATION_MANAGE,
        "Manage integrations",
        "Configure and trigger external source synchronisation.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.USER_ADMINISTER: PermissionSpec(
        Permission.USER_ADMINISTER,
        "Administer users",
        "Manage users, role assignments and scopes. Grants no data access.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.AUDIT_VIEW: PermissionSpec(
        Permission.AUDIT_VIEW,
        "View audit trail",
        "Read audit events. Restricted; itself audited.",
        SensitivityLevel.AGGREGATE,
    ),
    Permission.DATA_QUALITY_VIEW: PermissionSpec(
        Permission.DATA_QUALITY_VIEW,
        "View data quality",
        "Completeness, freshness, reconciliation and linkage quality.",
        SensitivityLevel.AGGREGATE,
    ),
}


class SystemRole(str, enum.Enum):
    """The roles MARS ships with.

    Additional roles may be created by an administrator; these are the baseline
    the permission-matrix tests assert against.
    """

    NATIONAL_PROGRAMME = "national_programme"
    DISTRICT_HSD = "district_hsd"
    FACILITY = "facility"
    ANALYST = "analyst"
    ADMINISTRATOR = "administrator"


#: Default permission grants per system role.
#:
#: Two deliberate omissions, both from blueprint section 009:
#:
#: * No role is granted ``PATIENT_REIDENTIFY``. Re-identification is a separately
#:   permissioned clinical action, granted to an individual, never to a role.
#: * ``ANALYST`` and ``ADMINISTRATOR`` receive no patient-evidence permission.
#:   Managing definitions or managing users does not confer access to patient
#:   data.
ROLE_PERMISSIONS: dict[SystemRole, frozenset[Permission]] = {
    SystemRole.NATIONAL_PROGRAMME: frozenset(
        {
            Permission.SURVEILLANCE_VIEW_AGGREGATE,
            Permission.GEOGRAPHY_VIEW,
            Permission.ORGANISATION_VIEW,
            Permission.FACILITY_VIEW,
            Permission.DATA_QUALITY_VIEW,
            Permission.INVESTIGATION_TRIAGE,
            Permission.INVESTIGATION_ASSIGN,
            Permission.INVESTIGATION_UPDATE,
            Permission.INVESTIGATION_CLOSE,
            Permission.REPORT_GENERATE,
            Permission.DATA_EXPORT,
            Permission.CONFIGURATION_VIEW,
            Permission.METHOD_VIEW,
        }
    ),
    SystemRole.DISTRICT_HSD: frozenset(
        {
            Permission.SURVEILLANCE_VIEW_AGGREGATE,
            Permission.GEOGRAPHY_VIEW,
            Permission.ORGANISATION_VIEW,
            Permission.FACILITY_VIEW,
            Permission.DATA_QUALITY_VIEW,
            Permission.CASE_EVIDENCE_VIEW,
            Permission.INVESTIGATION_TRIAGE,
            Permission.INVESTIGATION_ASSIGN,
            Permission.INVESTIGATION_UPDATE,
            Permission.INVESTIGATION_CLOSE,
            Permission.REPORT_GENERATE,
            Permission.METHOD_VIEW,
        }
    ),
    SystemRole.FACILITY: frozenset(
        {
            Permission.SURVEILLANCE_VIEW_AGGREGATE,
            Permission.GEOGRAPHY_VIEW,
            Permission.FACILITY_VIEW,
            Permission.DATA_QUALITY_VIEW,
            Permission.CASE_EVIDENCE_VIEW,
            Permission.INVESTIGATION_UPDATE,
        }
    ),
    SystemRole.ANALYST: frozenset(
        {
            Permission.SURVEILLANCE_VIEW_AGGREGATE,
            Permission.GEOGRAPHY_VIEW,
            Permission.ORGANISATION_VIEW,
            Permission.FACILITY_VIEW,
            Permission.DATA_QUALITY_VIEW,
            Permission.CONFIGURATION_VIEW,
            Permission.CONFIGURATION_MANAGE,
            Permission.METHOD_VIEW,
            Permission.METHOD_APPROVE,
            Permission.REPORT_GENERATE,
            Permission.DATA_EXPORT,
        }
    ),
    SystemRole.ADMINISTRATOR: frozenset(
        {
            Permission.USER_ADMINISTER,
            Permission.AUDIT_VIEW,
            Permission.GEOGRAPHY_VIEW,
            Permission.GEOGRAPHY_MANAGE,
            Permission.ORGANISATION_VIEW,
            Permission.ORGANISATION_MANAGE,
            Permission.FACILITY_VIEW,
            Permission.FACILITY_MANAGE,
            Permission.INTEGRATION_MANAGE,
            Permission.CONFIGURATION_VIEW,
            Permission.METHOD_VIEW,
        }
    ),
}


#: Default sensitivity ceiling per system role.
#:
#: A role's ceiling caps what an individual assignment can grant. No role
#: defaults to ``DIRECT_IDENTITY``: that tier is assigned per user, with a
#: recorded reason, and is audited on every use.
ROLE_DEFAULT_SENSITIVITY: dict[SystemRole, SensitivityLevel] = {
    SystemRole.NATIONAL_PROGRAMME: SensitivityLevel.AGGREGATE,
    SystemRole.DISTRICT_HSD: SensitivityLevel.PSEUDONYMOUS_CASE,
    SystemRole.FACILITY: SensitivityLevel.PSEUDONYMOUS_CASE,
    SystemRole.ANALYST: SensitivityLevel.AGGREGATE,
    SystemRole.ADMINISTRATOR: SensitivityLevel.AGGREGATE,
}
