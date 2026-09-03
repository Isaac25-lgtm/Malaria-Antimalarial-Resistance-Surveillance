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
