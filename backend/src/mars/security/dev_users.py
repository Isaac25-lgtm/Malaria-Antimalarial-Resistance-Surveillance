"""Synthetic development users.

Every account defined here is fictional and is flagged ``is_synthetic`` so it
can never be mistaken for a real operator in the audit trail. They exist so that
the scoping rules can be exercised end to end without a live identity provider.

The geography codes referenced are the codes the Prompt 5 importer will derive
from ``FScode``. Until that import runs, the seeder attaches whichever of these
units exist and leaves the rest unscoped - a user with no scope can read nothing,
which is the correct behaviour for an unprovisioned account.
"""

from __future__ import annotations

from dataclasses import dataclass

from mars.security.permissions import SensitivityLevel, SystemRole


@dataclass(frozen=True, slots=True)
class DevelopmentUserSpec:
    """A synthetic account and the scope it should be granted."""

    username: str
    display_name: str
    role: SystemRole
    scope_description: str
    #: Geography unit ``preferred_code`` this user is scoped to. ``None`` means
    #: national scope, granted by attaching the country unit.
    geography_code: str | None
    max_sensitivity: SensitivityLevel
    sensitivity_reason: str | None = None
    #: When true the seeder also attaches a facility scope, once a synthetic
    #: facility exists. Facility users see only their own facility.
    facility_scoped: bool = False


#: The synthetic accounts seeded into a development database.
#:
#: Chosen to make the scoping rules falsifiable: two district users in different
#: districts, and a facility user inside one of them, so a cross-boundary test
#: has something real to fail against.
DEVELOPMENT_USERS: tuple[DevelopmentUserSpec, ...] = (
    DevelopmentUserSpec(
        username="national.programme",
        display_name="National Programme Officer (synthetic)",
        role=SystemRole.NATIONAL_PROGRAMME,
        scope_description="Uganda - all districts. Aggregate surveillance only.",
        geography_code=None,
        max_sensitivity=SensitivityLevel.AGGREGATE,
    ),
    DevelopmentUserSpec(
        username="district.gulu",
        display_name="Gulu District Health Officer (synthetic)",
        role=SystemRole.DISTRICT_HSD,
        scope_description="Gulu district only. Aggregate plus pseudonymous case evidence.",
        geography_code="304",
        max_sensitivity=SensitivityLevel.PSEUDONYMOUS_CASE,
    ),
    DevelopmentUserSpec(
        username="district.pader",
        display_name="Pader District Health Officer (synthetic)",
        role=SystemRole.DISTRICT_HSD,
        scope_description="Pader district only. Used to prove cross-district denial.",
        geography_code="312",
        max_sensitivity=SensitivityLevel.PSEUDONYMOUS_CASE,
    ),
    DevelopmentUserSpec(
        username="facility.gulu",
        display_name="Facility Records Officer, Gulu (synthetic)",
        role=SystemRole.FACILITY,
        scope_description="A single facility in Gulu. Sees no sibling facility.",
        geography_code="304",
        max_sensitivity=SensitivityLevel.PSEUDONYMOUS_CASE,
        facility_scoped=True,
    ),
    DevelopmentUserSpec(
        username="analyst",
        display_name="Surveillance Analyst (synthetic)",
        role=SystemRole.ANALYST,
        scope_description=(
            "Uganda - all districts. Manages definitions and methods. No patient-level access."
        ),
        geography_code=None,
        max_sensitivity=SensitivityLevel.AGGREGATE,
    ),
    DevelopmentUserSpec(
        username="administrator",
        display_name="System Administrator (synthetic)",
        role=SystemRole.ADMINISTRATOR,
        scope_description=(
            "Manages users, geography and integrations. No surveillance or patient-level access."
        ),
        geography_code=None,
        max_sensitivity=SensitivityLevel.AGGREGATE,
    ),
)
