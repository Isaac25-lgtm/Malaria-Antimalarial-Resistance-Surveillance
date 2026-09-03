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


# ---------------------------------------------------------------------------
# Outpatient encounter — HMIS OPD 002 (Print Version July 2024)
#
# Every value here is either printed on the form or is an explicit MARS
# addition for an entry that is absent or unreadable. The additions are named
# ``UNKNOWN`` and documented as such, so nobody can mistake one for a category
# the register offers. See docs/data-dictionary/opd-002.md.
# ---------------------------------------------------------------------------
class AgeUnit(str, enum.Enum):
    """The unit the register recorded an age in.

    OPD 002 column 4 deliberately changes unit with age: complete years above
    one year, months under one year, days under one month. MARS keeps the unit
    the clerk wrote rather than converting, because converting a three-day-old
    to ``0.008 years`` discards precision the form went out of its way to
    capture.
    """

    YEARS = "years"
    MONTHS = "months"
    DAYS = "days"


class Sex(str, enum.Enum):
    """OPD 002 column 5. The form prints M and F only.

    ``UNKNOWN`` is a MARS value for a blank or unreadable cell. It is not a
    third option on the register, and a count of it is a data-quality finding
    rather than a demographic one.
    """

    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class PatientCategory(str, enum.Enum):
    """OPD 002 column 6: N national, R refugee, F foreigner.

    Also says which identifier system column 2 holds, since that column carries
    a national ID, a refugee number or a passport number with no type marker.
    """

    NATIONAL = "national"
    REFUGEE = "refugee"
    FOREIGNER = "foreigner"
    UNKNOWN = "unknown"


class AttendanceType(str, enum.Enum):
    """OPD 002 column 16, the New / Re-attendance tick.

    Re-attendance is a Lane A signal input and nothing more. A patient returns
    for many reasons this register does not record, so no MARS surface may
    describe a re-attendance as treatment failure or as evidence of resistance
    (ADR 0005).

    ``UNKNOWN`` covers both ticks set and neither set: the form gives no rule
    for resolving either, so MARS does not invent one.
    """

    NEW_ATTENDANCE = "new_attendance"
    RE_ATTENDANCE = "re_attendance"
    UNKNOWN = "unknown"


class FeverStatus(str, enum.Enum):
    """OPD 002 column 13, the Fever (Y/N) sub-column."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class MalariaTestMethod(str, enum.Enum):
    """OPD 002 column 13, Tests Done.

    Printed codes: ``B/S`` microscopy, ``RDT`` rapid diagnostic test, ``ND`` not
    done. The grid header additionally shows ``Y/N``, which has no meaning under
    the printed instructions and is treated as a printing artefact.
    """

    MICROSCOPY = "microscopy"
    RDT = "rdt"
    NOT_DONE = "not_done"
    UNKNOWN = "unknown"


class MalariaTestResult(str, enum.Enum):
    """OPD 002 column 13, Results.

    The instructions and the grid header disagree: the instructions say the
    column takes ``ND``, the header prints ``POS/NEG/NA``. Both are accepted and
    kept distinct rather than merged, because "no test was done" and "not
    applicable" are different statements and the form does not say they are the
    same.
    """

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NOT_DONE = "not_done"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ReferralDirection(str, enum.Enum):
    """OPD 002 columns 21 and 22.

    Column 21 is the number on a referral note the patient arrived with; column
    22 is the number on one written for them to leave with. Held as a direction
    rather than two parallel columns so a row can carry both, neither, or - as
    an extract occasionally does - two of one kind.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class DateAssignmentMethod(str, enum.Enum):
    """How an encounter's date was determined.

    The register writes the date once on a blank row, and it applies to every
    row beneath until the next date row. An extract that loses row order loses
    the date, so MARS records how each row got its date rather than presenting
    all dates as equally certain.
    """

    #: Read from a date header row immediately governing this row.
    ROW_HEADER = "row_header"
    #: Carried forward from an earlier date header.
    CARRIED_FORWARD = "carried_forward"
    #: Supplied per row by the source system, needing no carry-forward.
    SOURCE_SUPPLIED = "source_supplied"
    #: No date could be established. The row is retained and flagged.
    UNRESOLVED = "unresolved"


class EncounterQuarantineReason(str, enum.Enum):
    """Why a source row could not become an encounter.

    Quarantined rather than dropped: a row MARS cannot parse is a data-quality
    finding about a facility's records, and silently discarding it would hide
    exactly the gaps surveillance needs to see.
    """

    NO_DATE = "no_date"
    NO_FACILITY = "no_facility"
    CONTRADICTORY_TEST = "contradictory_test"
    UNPARSABLE_AGE = "unparsable_age"
    DUPLICATE_SOURCE_ROW = "duplicate_source_row"
    MALFORMED_ROW = "malformed_row"


# ---------------------------------------------------------------------------
# Identity and linkage — mars_identity (Prompt 8)
# ---------------------------------------------------------------------------
class IdentifierType(str, enum.Enum):
    """Which identifier system a stored value belongs to.

    OPD 002 column 2 carries a national ID, a refugee number or a passport
    number in a single cell with no type marker; column 6 (Patient Category) is
    what says which. The type is recorded because it is a *domain separator* for
    the linkage token: without it a passport ``CM12345`` and a NIN ``CM12345``
    would derive the same token and merge two unrelated clinical histories.

    ``UNSPECIFIED_SCHEME`` is used when column 6 is blank or contradicts the
    value's shape. A value under it links only to other values under it, which
    is the conservative behaviour: an unknown scheme should never merge with a
    known one.
    """

    NATIONAL_ID = "national_id"
    REFUGEE_NUMBER = "refugee_number"
    PASSPORT = "passport"
    PHONE = "phone"
    UNSPECIFIED_SCHEME = "unspecified_scheme"


class LinkageConfidence(str, enum.Enum):
    """How a person was linked to their own earlier records.

    MARS performs no probabilistic linkage. Fuzzy matching on names and dates of
    birth produces false merges, and a false merge in surveillance attaches one
    person's clinical history to another. Every value here is a deterministic
    statement about what was matched.
    """

    #: A normalised identifier of a known type matched exactly.
    DETERMINISTIC_IDENTIFIER = "deterministic_identifier"
    #: Matched, but the identifier's scheme was unknown, so the match holds only
    #: within the unspecified-scheme domain.
    DETERMINISTIC_UNSPECIFIED_SCHEME = "deterministic_unspecified_scheme"
    #: No identifier was usable. The encounter stands alone, which is honest.
    UNLINKED = "unlinked"
    #: A link was made and later withdrawn - a correction, or a consent change.
    WITHDRAWN = "withdrawn"


class ReidentificationOutcome(str, enum.Enum):
    """What happened to a re-identification request.

    ``NOT_FOUND`` and ``DENIED`` are separate values in the audit trail but
    produce the *same* response to the caller. The distinction is for the people
    reviewing access, not for the person asking.
    """

    DISCLOSED = "disclosed"
    DENIED_PERMISSION = "denied_permission"
    DENIED_SENSITIVITY = "denied_sensitivity"
    DENIED_NO_REASON = "denied_no_reason"
    NOT_FOUND = "not_found"


# ---------------------------------------------------------------------------
# Ingestion lifecycle — Prompt 9
# ---------------------------------------------------------------------------
class ImportBatchStatus(str, enum.Enum):
    """Where a batch is in its lifecycle.

    ``PARTIALLY_COMPLETED`` is a first-class success, not a degraded one. Real
    registers contain unreadable rows, and refusing a whole district's month
    because forty rows are malformed would lose far more than it protects.
    """

    #: The artefact was accepted and its checksum recorded. Nothing read yet.
    RECEIVED = "received"
    #: Rows are being checked. Nothing has been written to mars_core.
    VALIDATING = "validating"
    #: No row was loadable. The batch is retained in full for diagnosis.
    QUARANTINED = "quarantined"
    #: Valid rows are being written.
    LOADING = "loading"
    #: Every row loaded.
    COMPLETED = "completed"
    #: Some rows loaded, some quarantined. The normal outcome for real data.
    PARTIALLY_COMPLETED = "partially_completed"
    #: The batch itself was unusable: bad envelope, checksum, schema version or
    #: an unresolvable facility. No row was even considered.
    FAILED = "failed"


class ImportStage(str, enum.Enum):
    """The stages a batch passes through.

    Named because each is timed and counted separately: a run that slows down is
    usually slow in one stage, and an aggregate duration hides which.
    """

    READ = "read"
    VALIDATE = "validate"
    LINK_IDENTITY = "link_identity"
    WRITE_CANONICAL = "write_canonical"


class SourceRowOutcome(str, enum.Enum):
    """What became of one source row."""

    #: Written to mars_core as a new encounter.
    LOADED = "loaded"
    #: Already present and unchanged. Counted separately from ``loaded`` so a
    #: replay reports honestly rather than claiming to have imported anything.
    UNCHANGED = "unchanged"
    #: Already present and updated from a newer revision of the source.
    UPDATED = "updated"
    #: Not loadable. The issues say why.
    QUARANTINED = "quarantined"


class ValidationSeverity(str, enum.Enum):
    """How much an issue matters.

    A warning is recorded and the row still loads. An error quarantines the row.
    A ``FATAL`` issue is about the batch rather than a row - an unknown schema
    version, an unresolvable facility - and stops the run.
    """

    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


# ---------------------------------------------------------------------------
# Aggregate reporting — Prompt 11
# ---------------------------------------------------------------------------
class AggregateForm(str, enum.Enum):
    """Which printed form a submission transcribes.

    The form is part of a submission's identity, not a label. HMIS 033b and
    HMIS 105 both report malaria testing and treatment, over different periods
    and with different disaggregation, and a figure from one is not
    interchangeable with a figure from the other.
    """

    #: Health Unit Weekly Epidemiological Surveillance Report.
    HMIS_033B = "hmis_033b"
    #: Health Unit Outpatient Monthly Report.
    HMIS_105 = "hmis_105"


class AggregatePeriodType(str, enum.Enum):
    """The reporting period a form covers.

    033b is weekly - Monday to Sunday, stated on the form itself. 105 is
    monthly, due on the 7th of the following month. Storing the type alongside
    the dates means a weekly and a monthly figure can never be summed by
    accident.
    """

    WEEK = "week"
    MONTH = "month"


class AgeBand(str, enum.Enum):
    """The age bands HMIS 105 prints.

    Exactly the form's own bands, in the form's own order. MARS does not
    invent a band, and does not re-band a reported figure: an aggregate arrives
    already summarised, and splitting it again would be inventing detail the
    facility never reported.

    ``UNSPECIFIED`` is for forms with no age disaggregation at all - 033b
    reports a single total per field - so the same table serves both without
    pretending 033b carries a band it does not.
    """

    DAYS_0_28 = "days_0_28"
    DAYS_29_TO_YEARS_4 = "days_29_to_years_4"
    YEARS_5_9 = "years_5_9"
    YEARS_10_19 = "years_10_19"
    YEARS_20_PLUS = "years_20_plus"
    UNSPECIFIED = "unspecified"


class AggregateSubmissionStatus(str, enum.Enum):
    """Where a submission is in its life.

    ``SUPERSEDED`` exists because a corrected weekly report is a real event.
    The original was already acted on, so it is kept and marked rather than
    overwritten - otherwise the record would show a district that never had the
    figure anyone reacted to.
    """

    RECEIVED = "received"
    VALIDATED = "validated"
    QUARANTINED = "quarantined"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class StockMetric(str, enum.Enum):
    """The four columns HMIS 105 section 6.1 prints for every commodity.

    ``DAYS_OUT_OF_STOCK`` is the one that matters for surveillance: the form
    defines out of stock as *none left in the health unit store*, and a testing
    decline that coincides with days out of stock has a commodity explanation
    rather than an epidemiological one.
    """

    QUANTITY_CONSUMED = "quantity_consumed"
    DAYS_OUT_OF_STOCK = "days_out_of_stock"
    STOCK_ON_HAND = "stock_on_hand"
    QUANTITY_EXPIRED = "quantity_expired"


class ReconciliationStatus(str, enum.Enum):
    """How a reported aggregate compares with the same figure derived from
    encounters.

    ``UNCOMPARABLE`` is a first-class outcome, not a failure. If MARS holds no
    e-register data for that facility and period, there is nothing to compare
    against, and saying so is more useful than reporting a difference of
    everything.
    """

    MATCHED = "matched"
    WITHIN_TOLERANCE = "within_tolerance"
    DIFFERS = "differs"
    REPORTED_ONLY = "reported_only"
    DERIVED_ONLY = "derived_only"
    UNCOMPARABLE = "uncomparable"


# ---------------------------------------------------------------------------
# External integration — Prompt 12
# ---------------------------------------------------------------------------
class IntegrationResource(str, enum.Enum):
    """What an integration run asked the remote system for.

    Named for the *MARS* concept, not the remote endpoint. DHIS2 calls it
    ``organisationUnits``; another system will call it something else, and the
    run record has to stay readable when a second adapter arrives.
    """

    ORGANISATION_UNIT_METADATA = "organisation_unit_metadata"
    FACILITY_METADATA = "facility_metadata"
    DATA_ELEMENT_METADATA = "data_element_metadata"
    DATASET_METADATA = "dataset_metadata"
    AGGREGATE_DATA_VALUES = "aggregate_data_values"
    ANALYTICS_QUERY = "analytics_query"


class IntegrationRunStatus(str, enum.Enum):
    """Where an exchange got to.

    ``PARTIAL`` is a first-class outcome, not a failure: a paginated pull that
    read eleven of fourteen pages has genuinely fetched eleven pages, and
    resuming from page twelve is cheaper and more honest than discarding them.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class IntegrationErrorCategory(str, enum.Enum):
    """Why an exchange failed, in terms that decide what to do next.

    The distinction that matters operationally is between "retry later"
    (timeout, rate limit, server error) and "someone must change something"
    (authentication, authorisation, configuration). A single ``error`` value
    would leave an operator re-running a request that can never succeed.
    """

    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    AUTHENTICATION = "authentication"
    AUTHORISATION = "authorisation"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    REMOTE_SERVER_ERROR = "remote_server_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"
    MAPPING_INCOMPLETE = "mapping_incomplete"


class MappingProposalStatus(str, enum.Enum):
    """Whether a remote identifier has been reconciled with MARS.

    A proposal is never promoted by an import. ``ACCEPTED`` and ``REJECTED``
    are recorded by a governance action, because deciding that a DHIS2 UID is a
    particular Ugandan district is an administrative judgement, not a parsing
    outcome.
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# Indicators and analytics — Prompt 13
# ---------------------------------------------------------------------------
class EvidenceLane(str, enum.Enum):
    """Which evidence lane a derived figure belongs to.

    The two-lane model is the scientific boundary of the whole product, so it
    is a column on every indicator definition rather than a convention. A
    routine-derived figure may support a signal; it can never become a
    confirmed finding, and the lane is what makes an attempt to promote one
    a schema error rather than an editorial slip.
    """

    #: Derived from routine e-register and HMIS reporting.
    ROUTINE_SURVEILLANCE = "routine_surveillance"
    #: Established externally - therapeutic efficacy studies, molecular
    #: results - under separate governance. No MARS calculation produces this.
    CONFIRMED_EVIDENCE = "confirmed_evidence"


class PeriodGrain(str, enum.Enum):
    """The reporting period an indicator is defined over.

    An indicator has exactly one grain. A weekly figure and a monthly figure
    are different quantities, and letting one definition serve both is how a
    week's cases get compared with a month's.
    """

    DAY = "day"
    EPIDEMIOLOGICAL_WEEK = "epidemiological_week"
    MONTH = "month"


class GeographyGrain(str, enum.Enum):
    """The level an indicator is computed at.

    ``FACILITY`` is not a geography level - it is the reporting unit - but it
    is the grain most source data arrives at, so it belongs in the same axis.
    Rollups go facility -> subcounty -> district -> national.
    """

    FACILITY = "facility"
    SUBCOUNTY = "subcounty"
    DISTRICT = "district"
    NATIONAL = "national"


class IndicatorUnit(str, enum.Enum):
    """What an indicator's value is.

    ``PROPORTION`` is stored as a fraction between 0 and 1, never as a
    pre-multiplied percentage: multiplying at the presentation layer is
    reversible, and a value stored as 43.7 with no unit is not.
    """

    COUNT = "count"
    PROPORTION = "proportion"
    RATE_PER_PERIOD = "rate_per_period"
    DAYS = "days"


class IndicatorSourceDomain(str, enum.Enum):
    """Which source an indicator is computed from.

    Recorded because the same clinical quantity computed from an e-register and
    from a paper return is two different measurements, and a summary that mixed
    them would double-count. An indicator names one domain.
    """

    ENCOUNTER = "encounter"
    AGGREGATE_WEEKLY = "aggregate_weekly"
    AGGREGATE_MONTHLY = "aggregate_monthly"
    COMMODITY = "commodity"
    LABORATORY = "laboratory"
    REPORTING_METADATA = "reporting_metadata"


class IndicatorValueStatus(str, enum.Enum):
    """Whether a materialised value is a number, and if not, why not.

    ``UNAVAILABLE`` exists because an undefined denominator must never become
    zero. A positivity of "0.0" and a positivity of "we could not compute this"
    look identical in a chart and are opposite statements about a facility.
    """

    #: A computed value the definition's rules permit.
    AVAILABLE = "available"
    #: The denominator was zero, null, or not reported. No value exists.
    UNAVAILABLE_NO_DENOMINATOR = "unavailable_no_denominator"
    #: Inputs were present but the definition's completeness rules excluded
    #: the period. The exclusion is the finding.
    UNAVAILABLE_INSUFFICIENT_DATA = "unavailable_insufficient_data"
    #: Suppressed by a governed privacy rule. Distinct from missing: something
    #: is there, and the rule is why it is not shown.
    SUPPRESSED = "suppressed"


# ---------------------------------------------------------------------------
# Episodes and recurrence — Prompt 14
# ---------------------------------------------------------------------------
class EpisodeStatus(str, enum.Enum):
    """What an episode candidate is, in terms routine data can support.

    Every value is deliberately provisional. Routine data cannot establish that
    two positive results are one illness or two, so MARS records what it can
    see - visits, intervals, treatment records - and calls the grouping a
    *candidate*. Naming it anything firmer would be a clinical claim the data
    cannot carry.
    """

    #: One or more encounters grouped by the active rule. The ordinary case.
    CANDIDATE = "candidate"
    #: The rule's window extends past the data MARS holds, so the episode may
    #: continue beyond what is recorded. Reported rather than closed silently.
    OPEN_AT_PERIOD_END = "open_at_period_end"
    #: Grouped, but with evidence a reviewer needs to see before using it -
    #: a missing treatment record, an unresolved facility.
    QUALIFIED = "qualified"


class EpisodeEncounterRole(str, enum.Enum):
    """Why an encounter is in an episode.

    Kept explicit so an explanation can say "this visit started it, this one
    was a repeat positive" rather than presenting an undifferentiated list.
    """

    INDEX = "index"
    FOLLOW_UP = "follow_up"
    REPEAT_POSITIVE = "repeat_positive"


class EpisodeBuildStatus(str, enum.Enum):
    """Where an episode build run got to."""

    RUNNING = "running"
    COMPLETED = "completed"
    #: The active rule version is absent. Not a failure of the run - a
    #: statement that the programme has not approved an episode window, which
    #: is a governance fact rather than a bug.
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Recurrence surveillance — Prompt 15
# ---------------------------------------------------------------------------
class RecurrenceMeasure(str, enum.Enum):
    """What a recurrence result counts.

    Every value is a count or a proportion of *observed patterns*. None is a
    clinical outcome: routine data cannot establish that a repeat positive is
    treatment failure, recrudescence, reinfection or resistance, and no measure
    here is permitted to imply otherwise.
    """

    #: Patients with two or more positive results within the analysis window.
    REPEAT_POSITIVE_PATIENTS = "repeat_positive_patients"
    #: Episodes containing two or more positive results.
    REPEAT_POSITIVE_EPISODES = "repeat_positive_episodes"
    #: Patients with more than one distinct episode.
    PATIENTS_WITH_MULTIPLE_EPISODES = "patients_with_multiple_episodes"
    #: Repeat-positive patients over linked patients with any positive.
    REPEAT_POSITIVE_PROPORTION = "repeat_positive_proportion"
    #: Return intervals falling in a governed band.
    INTERVAL_BAND_COUNT = "interval_band_count"


class RecurrenceScopeKind(str, enum.Enum):
    """Which population a recurrence figure describes.

    Facility of care and residence geography are kept apart on purpose. A
    patient may attend a facility outside their own district, and merging the
    two attributes a pattern to the wrong place - which is the difference
    between investigating a clinic and investigating a village.
    """

    FACILITY = "facility"
    RESIDENCE_DISTRICT = "residence_district"
    RESIDENCE_SUBCOUNTY = "residence_subcounty"


# ---------------------------------------------------------------------------
# Testing, treatment and commodity surveillance — Prompt 16
# ---------------------------------------------------------------------------
class TestingMeasure(str, enum.Enum):
    """What a testing-surveillance result counts.

    Testing practice, not disease. Every value describes what a facility did
    with its tests; none describes how much malaria there is. Conflating the
    two is how a testing collapse gets read as an improvement.
    """

    TESTING_COVERAGE = "testing_coverage"
    RDT_SHARE = "rdt_share"
    MICROSCOPY_SHARE = "microscopy_share"
    TEST_POSITIVITY = "test_positivity"
    NEGATIVE_CASES_TREATED = "negative_cases_treated"
    UNTESTED_CASES_TREATED = "untested_cases_treated"
    TESTING_VOLUME_CHANGE = "testing_volume_change"
    MISSING_RESULT_COUNT = "missing_result_count"


class TreatmentMeasure(str, enum.Enum):
    """What a treatment-surveillance result counts.

    Prescribing practice as the register records it. None of these establishes
    that a patient received, took, or completed a drug: routine data cannot.
    """

    CONFIRMED_TREATED = "confirmed_treated"
    CONFIRMED_NOT_TREATED = "confirmed_not_treated"
    TREATED_WITHOUT_CONFIRMATION = "treated_without_confirmation"
    REPEAT_TREATMENT_EPISODES = "repeat_treatment_episodes"
    MISSING_TREATMENT_INFORMATION = "missing_treatment_information"


class CommodityFactKind(str, enum.Enum):
    """A commodity condition the source states outright.

    Each of these is read directly off a reported field. None involves a
    statistical judgement, which is why they can exist before any configuration
    is approved - "the facility reported zero stock on hand" is a fact, not an
    inference.

    What is *not* here: prolonged, repeated, low and imminent. Those require
    governed thresholds and appear only once a programme approves them.
    """

    #: The facility reported a stock balance of exactly zero.
    STOCK_ON_HAND_ZERO = "stock_on_hand_zero"
    #: The facility reported one or more days with none in the store.
    DAYS_OUT_OF_STOCK_REPORTED = "days_out_of_stock_reported"
    #: Every commodity cell for the period was blank. Not a stock-out - a
    #: reporting gap, and the difference matters most when supply has failed.
    STOCK_NOT_REPORTED = "stock_not_reported"


class CommodityAlertKind(str, enum.Enum):
    """A direct operational commodity alert.

    Operational, not epidemiological. These say a supply chain needs
    attention; they say nothing about malaria transmission, treatment response
    or resistance, and Prompt 21 may reference one as context without ever
    converting it into a treatment-response signal.

    Only ``STOCK_OUT_REPORTED`` can be raised without governed configuration,
    because it restates a fact the facility itself reported. Everything else
    requires an approved rule and stays absent until one exists.
    """

    STOCK_OUT_REPORTED = "stock_out_reported"
    PROLONGED_STOCK_OUT = "prolonged_stock_out"
    REPEATED_STOCK_OUT = "repeated_stock_out"
    MULTI_COMMODITY_STOCK_OUT = "multi_commodity_stock_out"
    LOW_STOCK = "low_stock"
    IMMINENT_STOCK_OUT = "imminent_stock_out"


class AlertSeverity(str, enum.Enum):
    """How urgent an operational alert is.

    ``UNCLASSIFIED`` is the default and the only value MARS assigns on its own.
    Mapping a condition to a severity is a programme decision: what counts as
    critical depends on resupply times, buffer stocks and district capacity,
    and an invented severity would drive a real prioritisation queue.
    """

    UNCLASSIFIED = "unclassified"
    INFORMATIONAL = "informational"
    ATTENTION = "attention"
    URGENT = "urgent"


# ---------------------------------------------------------------------------
# Historical baselines — Prompt 17
# ---------------------------------------------------------------------------
class BaselineSeriesKind(str, enum.Enum):
    """Which analytical series a baseline is built over.

    Kept explicit rather than inferred from the series key, because the same
    word can name an indicator and a measure, and a baseline built from the
    wrong table would compare a facility against a history that is not its own.
    """

    INDICATOR = "indicator"
    TESTING_MEASURE = "testing_measure"
    TREATMENT_MEASURE = "treatment_measure"


class BaselineMethod(str, enum.Enum):
    """How an expected value is derived from history.

    Implemented here; **chosen** by governance. Which method suits a series
    depends on its seasonality and its noise, and picking one on a programme's
    behalf would decide what counts as normal.
    """

    #: Median of the most recent comparable periods. Robust to one bad period.
    HISTORICAL_MEDIAN = "historical_median"
    #: Mean of the most recent comparable periods.
    HISTORICAL_MEAN = "historical_mean"
    #: Median of the same period-of-year across previous years. The seasonal
    #: form: malaria transmission in Uganda is seasonal, and comparing March
    #: against the preceding February flags the season rather than an event.
    SEASONAL_PERIOD_OF_YEAR_MEDIAN = "seasonal_period_of_year_median"


class DispersionMeasure(str, enum.Enum):
    """How the spread of a baseline's history was summarised.

    Paired with the method rather than chosen freely: a median summarised by a
    standard deviation would report a robust centre with a non-robust spread.
    """

    MEDIAN_ABSOLUTE_DEVIATION = "median_absolute_deviation"
    STANDARD_DEVIATION = "standard_deviation"
    #: A single historical period has a centre but no spread.
    NONE = "none"


class BaselineSufficiency(str, enum.Enum):
    """Whether the history behind a baseline was enough to use it.

    Recorded on every row, including the rows with no expected value. A
    facility that opened last month has no baseline, and saying so is more
    useful than an expected value computed from two periods.
    """

    SUFFICIENT = "sufficient"
    #: History exists but fewer comparable periods than the approved minimum.
    INSUFFICIENT_HISTORY = "insufficient_history"
    #: Enough periods, too few of them carrying a usable value.
    INSUFFICIENT_COMPLETENESS = "insufficient_completeness"
    #: No comparable period at all - a new facility, or a new series.
    NO_HISTORY = "no_history"


class BaselineBuildStatus(str, enum.Enum):
    """Where a baseline build run got to."""

    RUNNING = "running"
    COMPLETED = "completed"
    #: No approved temporal baseline method. Not a failure of the run - a
    #: statement that the programme has not decided what normal means.
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Temporal anomaly and persistence — Prompt 18
# ---------------------------------------------------------------------------
class AnomalyDetectionMethod(str, enum.Enum):
    """How a deviation from a baseline is judged large.

    Implemented here; **chosen** by governance. Each answers a different
    question and each is wrong in a different way, which is why MARS does not
    pick one on a programme's behalf.
    """

    #: Deviation in robust units of the baseline's own spread. Needs a
    #: dispersion, so it cannot be applied to a single-period baseline.
    ROBUST_Z_SCORE = "robust_z_score"
    #: Deviation as a proportion of the expected level. Works without a
    #: dispersion, and treats a rise from 2 to 4 as it treats 200 to 400.
    RELATIVE_DEVIATION = "relative_deviation"
    #: Observed outside the baseline's approved uncertainty band.
    EXCEEDS_UNCERTAINTY_BAND = "exceeds_uncertainty_band"


class AnomalyDirection(str, enum.Enum):
    """Which way an observation departed from what was expected.

    Recorded because the two directions mean opposite things. A rise in
    positivity may be transmission; a fall may be a testing collapse, and
    reporting only the magnitude loses the distinction.
    """

    INCREASE = "increase"
    DECREASE = "decrease"
    UNCHANGED = "unchanged"


class AnomalyOutcome(str, enum.Enum):
    """What the engine could conclude about one observation.

    The ``not_evaluated`` values exist so that "MARS could not judge this" is
    never stored as "MARS judged this normal". A district reading a quiet map
    is entitled to know which quiet is an absence of signal and which is an
    absence of evidence.
    """

    FLAGGED = "flagged"
    NOT_FLAGGED = "not_flagged"
    #: The source reported no usable value for the period. There is nothing to
    #: judge, which is not a statement that the period was normal.
    NOT_EVALUATED_NO_OBSERVATION = "not_evaluated_no_observation"
    #: No baseline with sufficient history. Nothing to compare against.
    NOT_EVALUATED_NO_BASELINE = "not_evaluated_no_baseline"
    #: Fewer cases than the approved minimum. A doubling of two cases is
    #: arithmetic, not epidemiology.
    NOT_EVALUATED_BELOW_MINIMUM_COUNT = "not_evaluated_below_minimum_count"
    #: The measure carries no case count, so the minimum cannot be checked.
    #: Different from being below it.
    NOT_EVALUATED_COUNT_UNKNOWN = "not_evaluated_count_unknown"
    #: The approved method cannot be applied to this baseline - a robust
    #: z-score against a baseline with no spread, a relative deviation from an
    #: expected zero, a band test with no band. Recorded rather than silently
    #: falling back to another method, which would apply a rule nobody
    #: approved.
    NOT_EVALUATED_METHOD_INAPPLICABLE = "not_evaluated_method_inapplicable"


class AnomalyBuildStatus(str, enum.Enum):
    """Where an anomaly detection run got to."""

    RUNNING = "running"
    COMPLETED = "completed"
    #: No approved detection rule. Not a failure - a statement that the
    #: programme has not decided how large a departure has to be.
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"
