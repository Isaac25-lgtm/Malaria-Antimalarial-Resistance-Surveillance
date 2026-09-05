"""JSON report schema for DHIS2 metadata discovery.

Candidate mappings in this document are proposals. They are not accepted
crosswalks and they are not instructions to retrieve patient records.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityStatus(StrEnum):
    SUPPORTED_AND_AUTHORIZED = "supported_and_authorized"
    SUPPORTED_BUT_FORBIDDEN = "supported_but_forbidden"
    SUPPORTED_BY_VERSION_AUTHORIZATION_NOT_PROBED = "supported_by_version_authorization_not_probed"
    NOT_SUPPORTED = "not_supported"
    AUTHENTICATION_FAILED = "authentication_failed"
    INDETERMINATE = "indeterminate"
    NOT_PROBED_TO_PROTECT_PATIENT_DATA = "not_probed_to_protect_patient_data"


class CapabilityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    route: str
    status: CapabilityStatus
    http_status: int | None = None
    detail: str
    probed: bool


class OrganisationUnitRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None
    code: str | None = None
    level: int | None = None
    path: str | None = None
    leaf: bool | None = None
    parent_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    ancestor_names: list[str] = Field(default_factory=list)
    group_names: list[str] = Field(default_factory=list)
    classification: Literal[
        "pader_candidate",
        "candidate_facility",
        "organisation_unit",
    ] = "organisation_unit"


class CandidateMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["opd_programme", "malaria_variable", "pader_organisation_unit"]
    remote_id: str
    name: str | None = None
    code: str | None = None
    reason: str
    status: Literal["proposal"] = "proposal"


class DiscoveryReport(BaseModel):
    """Sanitized metadata-discovery report. No credentials, no patient rows."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    client_version: str
    origin_host: str
    interpretation_limit: str
    stop_before_patient_data: Literal[True] = True
    api_generation: str = "indeterminate"
    system: dict[str, Any] = Field(default_factory=dict)
    current_user: dict[str, Any] = Field(default_factory=dict)
    authorities: list[str] = Field(default_factory=list)
    capture_organisation_units: list[OrganisationUnitRecord] = Field(default_factory=list)
    data_view_organisation_units: list[OrganisationUnitRecord] = Field(default_factory=list)
    tracker_search_organisation_units: list[OrganisationUnitRecord] = Field(default_factory=list)
    pader_candidates: list[OrganisationUnitRecord] = Field(default_factory=list)
    accessible_facilities: list[OrganisationUnitRecord] = Field(default_factory=list)
    accessible_facility_count: int | None = None
    facility_scope_counts: dict[str, int | None] = Field(default_factory=dict)
    programmes: list[dict[str, Any]] = Field(default_factory=list)
    program_stages: list[dict[str, Any]] = Field(default_factory=list)
    tracked_entity_types: list[dict[str, Any]] = Field(default_factory=list)
    tracked_entity_attributes: list[dict[str, Any]] = Field(default_factory=list)
    data_elements: list[dict[str, Any]] = Field(default_factory=list)
    option_sets: list[dict[str, Any]] = Field(default_factory=list)
    data_sets: list[dict[str, Any]] = Field(default_factory=list)
    category_combos: list[dict[str, Any]] = Field(default_factory=list)
    candidate_mappings: list[CandidateMapping] = Field(default_factory=list)
    capabilities: list[CapabilityRecord] = Field(default_factory=list)
    supported_analytical_apis: list[str] = Field(default_factory=list)
    access_limitations: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    truncated_collections: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def sanitized_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
