"""Pydantic response models for the v1 API.

These are the contract. The TypeScript client is generated from the OpenAPI
document they produce, and CI fails if the two drift apart.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mars.domain.enums import InvestigationOutcome


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
    auth_mode: str = "demo"
    live_login_enabled: bool = False
    active_method_versions: list[str] = Field(
        default_factory=list,
        description="Qualified identifiers, e.g. 'IND-TPR@1.2.0'. Empty when unconfigured.",
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
    scope_type: str = "none"
    mapping_status: str = "mapped"
    landing_path: str | None = None


class SourceStatusSummary(MarsModel):
    mode: str
    source: str
    authentication: str
    mapping: str
    last_sync: datetime | None = None


class AuthorisedDistrictSummary(MarsModel):
    org_unit_id: uuid.UUID
    org_unit_name: str
    preferred_code: str


class SessionScopeSummary(MarsModel):
    scope_type: str
    org_unit_id: uuid.UUID | None = None
    org_unit_name: str | None = None
    national_access: bool = False
    authorised_districts: list[AuthorisedDistrictSummary] = Field(default_factory=list)


class SessionUserSummary(MarsModel):
    display_name: str
    username: str


class SessionStatusResponse(MarsModel):
    """Public session probe. Never returns 401 for an anonymous caller."""

    authenticated: bool
    auth_mode: str
    csrf_token: str | None = None
    user: SessionUserSummary | None = None
    scope: SessionScopeSummary | None = None
    permissions: list[str] | None = None
    source_status: SourceStatusSummary | None = None
    profile: CurrentUserResponse | None = None


class LiveLoginRequest(MarsModel):
    """eRegisters username and password, posted only to MARS."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


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


# -- Map delivery ---------------------------------------------------------
#
# The map contract is separate from the hierarchy contract above. A client that
# only lists districts should not have to understand geometry, and a client that
# draws them should not have to guess which boundary version it is drawing.
class BoundingBoxModel(MarsModel):
    """A geographic extent in EPSG:4326 degrees, west/south/east/north."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


class MapLevelAvailability(MarsModel):
    """Whether one hierarchy level can be drawn, and at what tolerance."""

    level: str
    unit_count: int
    geometry_count: int
    simplification_tolerance_deg: float | None
    is_drawable: bool = Field(
        description="False when the level exists in the schema but no geometry is loaded."
    )
    supports_national_layer: bool = Field(
        description="Whether the whole level may be requested without a parent filter."
    )


class MapMetadataResponse(MarsModel):
    """What the caller may draw, and from which boundary version.

    ``is_available`` false means no boundary version is published or none of
    its levels carry geometry. The client shows an explicit "no boundaries
    loaded" state rather than an empty canvas, which would read as a rendering
    failure.
    """

    is_available: bool
    boundary_version_id: uuid.UUID | None
    boundary_version_code: str | None
    boundary_version_label: str | None
    source_name: str | None
    source_checksum: str | None = Field(
        description="SHA-256 of the source set, so a client can prove which bytes it is drawing."
    )
    imported_at: datetime | None
    initial_bounds: BoundingBoxModel | None = Field(
        description="Extent of the highest unit in the caller's scope, for the first viewport."
    )
    initial_unit_id: uuid.UUID | None
    initial_unit_name: str | None
    initial_unit_level: str | None
    levels: list[MapLevelAvailability]
    geometry_resolution: str = Field(
        description="Always 'simplified'. Full-resolution geometry is never served to a browser."
    )
    max_features: int
    generated_at: datetime


class MapFeatureProperties(MarsModel):
    """The complete set of properties a map feature carries.

    Declared as a closed model rather than a free-form object so the allow-list
    is part of the published contract and appears in the generated client.
    """

    unit_id: uuid.UUID
    level: str
    code: str
    name: str
    parent_id: uuid.UUID | None
    path: str
    area_sq_km: float | None
    is_active: bool
    in_scope: bool | None = Field(
        default=None,
        description=(
            "Present on the public context layer: whether the caller may open "
            "this unit. Absent on the scoped features layer, where every "
            "returned row is already in scope."
        ),
    )


class MapFeature(MarsModel):
    """One administrative area as GeoJSON."""

    type: str = Field(default="Feature")
    id: str
    geometry: dict[str, Any] = Field(description="GeoJSON MultiPolygon, simplified for display.")
    properties: MapFeatureProperties


class MapCollectionMeta(MarsModel):
    """MARS metadata carried inside the FeatureCollection as a foreign member."""

    boundary_version_id: uuid.UUID | None
    boundary_version_code: str | None
    level: str | None
    parent_id: uuid.UUID | None
    within_id: uuid.UUID | None
    geometry_resolution: str
    feature_count: int
    matched_count: int
    truncated: bool


class MapFeatureCollection(MarsModel):
    """A GeoJSON FeatureCollection with the boundary version attached.

    The version travels inside the document, not only in a header, so a saved
    or forwarded response still says which boundaries produced it.
    """

    type: str = Field(default="FeatureCollection")
    features: list[MapFeature]
    bbox: list[float] | None = Field(
        default=None, description="Extent of the whole collection: west, south, east, north."
    )
    mars: MapCollectionMeta


class GeographyBreadcrumb(MarsModel):
    """One step in the ancestor chain, from country down to the unit."""

    unit_id: uuid.UUID
    level: str
    code: str
    name: str
    is_current: bool


class GeographyBreadcrumbsResponse(MarsModel):
    breadcrumbs: list[GeographyBreadcrumb]


class NationalGeographyResponse(MarsModel):
    """The caller's root geography and the level below it.

    "National" is the top of the caller's scope, not necessarily Uganda: a
    district user's national view is their district. The map opens correctly for
    a delegated account without a special case in the client.
    """

    root: GeographyUnitSummary | None
    bounds: BoundingBoxModel | None
    child_level: str | None
    children: list[GeographyUnitSummary]
    boundary_version_id: uuid.UUID | None
    boundary_version_code: str | None


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


class IntegrationStatusSummary(MarsModel):
    """Whether an exchange is configured, and what it has done.

    Deliberately says *whether* credentials are present, never what they are.
    A status endpoint is exactly where a token gets pasted into a support
    ticket.
    """

    system: str
    enabled: bool
    configured: bool
    credentials_present: bool
    tls_verification: bool
    outbound_push_enabled: bool
    adapter_version: str | None
    base_url: str | None
    total_runs: int
    last_run_at: datetime | None
    last_run_status: str | None
    unresolved_mappings: int


class IntegrationRunSummary(MarsModel):
    id: uuid.UUID
    system: str
    resource: str
    run_status: str
    attempt: int
    scope_description: str | None
    started_at: datetime
    finished_at: datetime | None
    pages_fetched: int
    records_received: int
    records_accepted: int
    records_rejected: int
    mappings_unresolved: int
    error_category: str | None


class MappingProposalSummary(MarsModel):
    """A remote identifier with no MARS mapping.

    A configuration gap someone has to close. MARS does not guess a mapping by
    name similarity, so this list is the only way the gap becomes visible.
    """

    id: uuid.UUID
    system: str
    remote_type: str
    remote_id: str
    remote_name: str | None
    proposal_status: str
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime


class IndicatorVersionSummary(MarsModel):
    """One version of an indicator definition.

    The specification is exposed so a reader can check what a figure means.
    No threshold appears because none exists here: what counts as too high is
    a programme decision held in the configuration registry.
    """

    id: uuid.UUID
    version_number: int
    semantic_version: str
    status: str
    blank_handling: str
    specification_checksum: str
    numerator_specification: dict[str, Any]
    denominator_specification: dict[str, Any] | None
    permitted_dimensions: dict[str, Any] | None
    exclusion_rules: dict[str, Any] | None
    effective_from: date | None
    effective_to: date | None
    approved_by: str | None
    notes: str | None


class IndicatorDefinitionSummary(MarsModel):
    """What a metric is, and which version is in force.

    ``active_version`` is null when the programme has not approved one. That is
    an ordinary state, not an error: the indicator is registered and inert.
    """

    id: uuid.UUID
    code: str
    label: str
    purpose: str
    interpretation: str
    unit: str
    source_domain: str
    period_grain: str
    base_geography_grain: str
    evidence_lane: str
    definition_source: str
    active_version: IndicatorVersionSummary | None
    version_count: int


class IndicatorResultSummary(MarsModel):
    """One materialised figure, with the context needed to read it.

    ``value`` is null whenever ``value_status`` is not ``available``. A missing
    value is never rendered as zero: an undefined denominator and a genuine
    zero are opposite statements about a facility.
    """

    id: uuid.UUID
    indicator_code: str
    geography_grain: str
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    period_start: date
    period_end: date
    period_grain: str
    age_band: str
    sex: str
    numerator: int | None
    denominator: int | None
    value: float | None
    value_status: str
    contributing_units: int | None
    expected_units: int | None
    missing_inputs: int | None
    quality_context: dict[str, Any] | None
    source_cutoff: datetime
    boundary_version_id: uuid.UUID | None
    engine_version: str
    computed_at: datetime


# -- Analytical surveillance and signals ---------------------------------
class AnalyticalRecordSummary(MarsModel):
    id: uuid.UUID
    record_type: str
    code: str
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    period_start: date
    period_end: date
    numerator: int | None
    denominator: int | None
    value: float | None
    value_status: str
    details: dict[str, Any]


class SignalEvidenceSummary(MarsModel):
    kind: str
    role: str
    source_table: str
    source_record_id: uuid.UUID
    contribution: float | None
    summary: str
    facts: dict[str, Any]
    quality_context: dict[str, Any] | None


class SignalSummary(MarsModel):
    id: uuid.UUID
    signal_type: str
    status: str
    priority: str
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    period_start: date
    period_end: date
    title: str
    statement: str
    score: float | None
    evidence_count: int
    counter_evidence_count: int
    data_quality: dict[str, Any]
    uncertainty: list[str]
    recommended_action_codes: list[str]
    method_version_id: uuid.UUID
    rule_code: str
    input_fingerprint: str
    group_key: str
    source_cutoff: datetime
    generated_at: datetime
    supersedes_id: uuid.UUID | None
    superseded_by_id: uuid.UUID | None
    evidence: list[SignalEvidenceSummary] | None = None


class SignalExplanationSummary(MarsModel):
    id: uuid.UUID
    signal_id: uuid.UUID
    method_version_id: uuid.UUID
    why_flagged: str
    evidence: list[dict[str, Any]]
    counter_evidence: list[dict[str, Any]]
    data_quality: dict[str, Any]
    method_steps: list[dict[str, Any]]
    uncertainty: list[str]
    missing_information: list[str]
    recommended_actions: list[dict[str, str]]
    interpretation_limit: str
    signal_input_fingerprint: str
    input_fingerprint: str
    generator_version: str
    generated_at: datetime


# ---------------------------------------------------------------------------
# National command centre — Prompt 23
# ---------------------------------------------------------------------------
class PeriodWindow(MarsModel):
    """The reporting window a figure belongs to.

    Always returned beside a value. A number without its period is a number
    nobody can check.
    """

    start: date
    end: date


class MeasureComparison(MarsModel):
    """The same measure over the preceding window of equal length."""

    period: PeriodWindow
    value: str | None
    direction: Literal["up", "down", "unchanged"] | None
    status: str
    status_detail: str | None


class SurveillanceMeasure(MarsModel):
    """One governed figure, or an explicit statement that there is none.

    ``status`` distinguishes a real value from the several ways a value can be
    absent, so a screen never has to render an absence as a zero.
    """

    code: str
    label: str
    value: str | None
    unit: str | None
    numerator: int | None
    denominator: int | None
    period: PeriodWindow
    geography_grain: str
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    source: str
    method_version_id: uuid.UUID | None
    source_freshness: datetime | None
    comparison: MeasureComparison | None
    status: str
    status_detail: str | None
    missing_configuration: list[str]


class PriorityDistrict(MarsModel):
    """A district with active signals, and what that ordering means."""

    geography_unit_id: uuid.UUID
    preferred_code: str | None
    name: str
    active_signals: int
    commodity_alerts: int
    period: PeriodWindow
    ordering: str
    ordering_detail: str


class SurveillanceProvenance(MarsModel):
    """What the screen was built from, and whether it is configured at all."""

    period: PeriodWindow
    indicators_registered: int
    indicators_approved: int
    analytics_refreshed_at: datetime | None
    signals_generated_at: datetime | None
    interpretation_boundary: str
    analytically_configured: bool
    configuration_detail: str | None


class FacilityContribution(MarsModel):
    """One facility's share of a district figure, or its absence.

    A facility that reported nothing is listed with a null value and a status
    saying so. Dropping it would hide the commonest reason a district total
    falls: a large facility stopped reporting.
    """

    facility_id: uuid.UUID
    code: str | None
    name: str
    period: PeriodWindow
    indicator_code: str
    value: int | None
    source_freshness: datetime | None
    status: str
    status_detail: str | None


class DashboardSectionBase(MarsModel):
    """Provenance shared by every overview panel.

    A section that cannot speak is still present, with an availability other
    than ``available`` and a refusal reason. Blank is not zero.
    """

    availability: str
    requested_scope: str
    reporting_period: PeriodWindow
    source: str
    source_period: PeriodWindow | None
    freshness: datetime | None
    last_successful_synchronization: datetime | None
    method_version_id: uuid.UUID | None = None
    refusal_reason: str | None = None


class CountBucket(MarsModel):
    code: str
    label: str
    count: int | None
    status: str
    detail: str | None = None


class MeasureSection(DashboardSectionBase):
    items: list[SurveillanceMeasure]


class BucketSection(DashboardSectionBase):
    items: list[CountBucket]


class DistrictSection(DashboardSectionBase):
    items: list[PriorityDistrict]


class SignalListSection(DashboardSectionBase):
    items: list[SignalSummary]


class CommoditySection(DashboardSectionBase):
    items: list[AnalyticalRecordSummary]


class ChartSection(DashboardSectionBase):
    items: list[dict[str, Any]] = Field(default_factory=list)


class OverviewSnapshot(MarsModel):
    """One coherent dashboard payload. The browser does not compute figures."""

    title: str
    subtitle: str
    interpretation_boundary: str
    data_mode: str
    data_mode_detail: str
    demo_mode_enabled: bool
    requested_scope: str
    has_national_scope: bool
    reporting_period: PeriodWindow
    provenance: SurveillanceProvenance
    last_successful_synchronization: datetime | None
    kpis: MeasureSection
    signals_by_priority: BucketSection
    investigations_by_status: BucketSection
    districts_requiring_review: DistrictSection
    commodity_alerts: CommoditySection
    needs_attention: BucketSection
    recent_signals: SignalListSection
    confirmed_malaria_trend: ChartSection
    testing_positivity: ChartSection


class ReportRow(MarsModel):
    """One measure as it appears in a report.

    ``value`` stays null when the measure had none. A report that wrote zero
    here would put a figure into a briefing that MARS never computed, and a
    spreadsheet cell has nowhere to carry the caveat.
    """

    code: str
    label: str
    value: str | None
    unit: str | None
    numerator: int | None
    denominator: int | None
    status: str
    status_detail: str | None
    period_start: date
    period_end: date
    source: str
    method_version_id: uuid.UUID | None


class GeneratedReport(MarsModel):
    """A governed report, carrying its own provenance and interpretation limit."""

    product: str
    generated_at: datetime
    period_start: date
    period_end: date
    geography_unit_id: uuid.UUID | None
    rows: list[ReportRow]
    interpretation_limit: str
    provenance: SurveillanceProvenance


# ---------------------------------------------------------------------------
# Investigation workflow — Prompt 26
# ---------------------------------------------------------------------------
class InvestigationEventSummary(MarsModel):
    """One entry in the append-only timeline."""

    sequence: int
    event_kind: str
    actor_label: str | None
    occurred_at: datetime
    note: str | None
    payload: dict[str, Any] | None


class EvidenceRequestSummary(MarsModel):
    """A request for externally supplied evidence.

    ``result_reference`` is a pointer into the system that holds the result
    under its own governance. MARS never stores the clinical content.
    """

    id: uuid.UUID
    request_status: str
    description: str
    requested_at: datetime
    result_reference: str | None
    result_recorded_at: datetime | None


class InvestigationQueueEntry(MarsModel):
    """One row in an action-centre queue."""

    id: uuid.UUID
    signal_id: uuid.UUID
    investigation_status: str
    priority: str
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    period_start: date
    period_end: date
    assigned_to_user_id: uuid.UUID | None
    opened_at: datetime
    record_version: int


class InvestigationDetail(InvestigationQueueEntry):
    """One investigation with its full history."""

    triaged_at: datetime | None
    assigned_at: datetime | None
    closed_at: datetime | None
    outcome: str | None
    outcome_note: str | None
    escalation_reason: str | None
    events: list[InvestigationEventSummary]
    evidence_requests: list[EvidenceRequestSummary]


class OpenInvestigationRequest(MarsModel):
    """Open an investigation against a signal.

    ``idempotency_key`` makes a retry safe: a repeated open returns the
    existing investigation rather than splitting the timeline in two.
    """

    signal_id: uuid.UUID
    idempotency_key: str | None = Field(default=None, max_length=128)


class TransitionInvestigationRequest(MarsModel):
    """Move an investigation along.

    ``expected_version`` is the optimistic-concurrency token. Two reviewers who
    both loaded the investigation and both press close must not silently
    overwrite one another.
    """

    expected_version: int = Field(ge=1)
    assigned_to_user_id: uuid.UUID | None = None
    outcome: InvestigationOutcome | None = None
    note: str | None = None
    escalation_reason: str | None = None


class RequestEvidenceRequest(MarsModel):
    """Ask for evidence MARS cannot produce itself, or add a note."""

    description: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=1)


class RecordExternalResultRequest(MarsModel):
    """Record that an external result came back, by reference only."""

    result_reference: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Ask MARS — Prompt 27
# ---------------------------------------------------------------------------
class AskMarsAvailability(MarsModel):
    """Whether the optional assistant can answer here.

    ``available`` is false on a shipped deployment: MARS registers no model
    provider, because choosing one is a procurement and information-governance
    decision. The reason is returned so a client can say so honestly.
    """

    available: bool
    reason: str | None
    detail: str | None
    supported_topics: list[str]


class AskMarsCitation(MarsModel):
    """One MARS record an answer was grounded in."""

    kind: str
    record_id: str
    period_start: date | None
    period_end: date | None
    detail: str | None


class AskMarsRequest(MarsModel):
    """A bounded question.

    ``topic`` restricts what may be asked. A bounded assistant that says what
    it can answer is more useful than an open one that answers badly.
    """

    topic: Literal[
        "district_priority",
        "commodity_alerts",
        "explain_signal",
        "compare_recurrence",
        "investigation_brief",
    ]
    question: str = Field(min_length=1, max_length=2000)
    period_start: date
    period_end: date
    signal_id: uuid.UUID | None = None


class AskMarsAnswer(MarsModel):
    """A grounded, cited answer - or an explicit absence of one."""

    available: bool
    topic: str
    text: str
    citations: list[AskMarsCitation]
    missing_information: list[str]
    interpretation_limit: str
    provider: str | None
    model: str | None
    response_hash: str | None
    unavailable_reason: str | None


# Forward references resolved after all models are declared.
GeographyOverviewResponse.model_rebuild()
FacilityDetail.model_rebuild()
