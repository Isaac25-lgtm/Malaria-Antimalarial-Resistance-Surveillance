"""Pydantic response models for the v1 API.

These are the contract. The TypeScript client is generated from the OpenAPI
document they produce, and CI fails if the two drift apart.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MarsModel(BaseModel):
    """Base for every response model."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page[T](MarsModel):
    """A single page of results.

    Cursor pagination replaces this for the large signal and case listings in
    later phases; offset pagination is adequate for bounded reference data.
    """

    items: list[T]
    total: int | None = Field(default=None, description="Omitted when counting would be costly.")
    limit: int
    offset: int


# -- Health and metadata --------------------------------------------------
class LivenessResponse(MarsModel):
    status: str = Field(description="Always 'alive' when the process is serving.")
    service: str


class DependencyStatus(MarsModel):
    name: str
    status: str = Field(description="ok | unavailable | not_installed")
    detail: str | None = None
    version: str | None = None


class ReadinessResponse(MarsModel):
    status: str = Field(description="ready | degraded | unavailable")
    checked_at: datetime
    dependencies: list[DependencyStatus]


class VersionResponse(MarsModel):
    """Build and governance identity of this deployment.

    Exposes which method versions are active, so a number on a screen can be
    traced to the rules that produced it. Configuration *content* is not
    exposed here - only which version is in force.
    """

    name: str
    release_version: str
    git_sha: str
    build_timestamp: str | None
    environment: str
    api_version: str
    display_timezone: str
    ai_assistant_enabled: bool
    demo_mode_enabled: bool
    development_auth_active: bool
    active_method_versions: list[str] = Field(
        default_factory=list,
        description="Qualified identifiers, e.g. 'IND-TPR@1.2.0'. Empty until Prompt 13.",
    )
    active_configuration_keys: list[str] = Field(
        default_factory=list,
        description="Keys with an active version. Values are not exposed here.",
    )


# -- Authentication -------------------------------------------------------
class GeographyScopeSummary(MarsModel):
    geography_unit_id: uuid.UUID
    preferred_code: str
    level: str
    name: str


class CurrentUserResponse(MarsModel):
    """The caller's own non-sensitive profile and effective authorisation."""

    user_id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    organisation_label: str | None
    roles: list[str]
    permissions: list[str]
    max_sensitivity: str
    geography_scopes: list[GeographyScopeSummary]
    facility_scope_ids: list[uuid.UUID]
    has_national_scope: bool
    auth_method: str
    is_synthetic: bool = Field(
        description="True for development accounts. The interface must mark these visibly."
    )


class DevelopmentLoginRequest(MarsModel):
    """Request a synthetic development token. Non-production only."""

    username: str = Field(min_length=1, max_length=128)


class DevelopmentLoginResponse(MarsModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    warning: str = Field(
        default="Synthetic development session. Not valid in staging or production.",
    )


class DevelopmentUserSummary(MarsModel):
    """A selectable synthetic user, for the development sign-in screen."""

    username: str
    display_name: str
    role: str
    scope_description: str


# -- Geography ------------------------------------------------------------
class GeographyUnitSummary(MarsModel):
    id: uuid.UUID
    level: str
    unit_kind: str
    preferred_code: str
    name: str = Field(description="The raw supplied name. Displayed as-is.")
    normalised_name: str = Field(description="Lookup form. Not for display.")
    parent_id: uuid.UUID | None
    depth: int
    path: str | None
    is_active: bool
    effective_from: date | None
    effective_to: date | None


class GeographyUnitDetail(GeographyUnitSummary):
    boundary_version_id: uuid.UUID | None
    ancestors: list[GeographyUnitSummary] = Field(default_factory=list)
    child_count: int = 0
    has_geometry: bool = Field(
        default=False,
        description="False throughout phases 1-2; geometry is imported in Prompt 5.",
    )


class GeographyLevelCount(MarsModel):
    level: str
    count: int


class GeographyOverviewResponse(MarsModel):
    """Hierarchy metadata, including levels that are deliberately empty."""

    levels: list[GeographyLevelCount]
    boundary_versions: list[BoundaryVersionSummary]
    note: str


class BoundaryVersionSummary(MarsModel):
    id: uuid.UUID
    code: str
    label: str
    source_name: str
    source_file_name: str | None
    source_checksum: str | None
    source_format: str | None
    source_crs: str | None
    storage_crs: str
    import_status: str
    effective_from: date | None
    effective_to: date | None
    validation_summary: dict[str, Any] | None


class GeographyAliasSummary(MarsModel):
    id: uuid.UUID
    geography_unit_id: uuid.UUID
    source_system: str
    source_code: str
    source_name: str | None
    match_status: str
    match_method: str | None


# -- Organisation and facility -------------------------------------------
class OrganisationUnitSummary(MarsModel):
    id: uuid.UUID
    code: str
    name: str
    unit_type: str
    parent_id: uuid.UUID | None
    depth: int
    primary_geography_unit_id: uuid.UUID | None
    is_active: bool


class OrganisationUnitDetail(OrganisationUnitSummary):
    ancestors: list[OrganisationUnitSummary] = Field(default_factory=list)
    child_count: int = 0


class FacilitySummary(MarsModel):
    id: uuid.UUID
    code: str
    name: str
    facility_level: str
    ownership: str
    district_geography_unit_id: uuid.UUID | None
    subcounty_geography_unit_id: uuid.UUID | None
    organisation_unit_id: uuid.UUID | None
    is_active: bool
    is_synthetic: bool
    has_coordinates: bool = Field(
        description="True only when a validated coordinate exists. "
        "MARS never places a facility approximately."
    )


class FacilityDetail(FacilitySummary):
    opened_on: date | None
    closed_on: date | None
    coordinate_source: str | None
    coordinate_validated: bool
    identifiers: list[FacilityIdentifierSummary] = Field(default_factory=list)


class FacilityIdentifierSummary(MarsModel):
    source_system: str
    external_id: str
    external_name: str | None
    is_primary: bool


# -- Governance -----------------------------------------------------------
class ConfigurationKeySummary(MarsModel):
    id: uuid.UUID
    key: str
    label: str
    description: str
    category: str
    requires_programme_approval: bool
    active_version_number: int | None
    active_version_checksum: str | None
    active_effective_from: date | None


class MethodVersionSummary(MarsModel):
    id: uuid.UUID
    semantic_version: str
    status: str
    summary: str
    effective_from: date | None
    validation_reference: str | None
    artifact_checksum: str | None


class MethodDefinitionSummary(MarsModel):
    id: uuid.UUID
    code: str
    label: str
    kind: str
    purpose: str
    versions: list[MethodVersionSummary] = Field(default_factory=list)


# Forward references resolved after all models are declared.
GeographyOverviewResponse.model_rebuild()
FacilityDetail.model_rebuild()
