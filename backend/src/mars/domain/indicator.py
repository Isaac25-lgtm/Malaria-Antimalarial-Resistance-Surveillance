"""The indicator registry: the single authority for what a metric means.

Every number MARS publishes traces to a row here. That is the point: a
positivity rate quoted in a dashboard, a report and an investigation packet must
be the same quantity computed the same way, and the only way to guarantee it is
for all three to read one definition rather than three implementations.

Four ideas shape the module.

**A definition is versioned and approved, or it is not in force.** Changing what
"confirmed malaria" counts is a governance act, not a deployment. A definition
carries a lifecycle, and a version that has not been approved cannot produce a
published figure - which is why the registry can ship complete while every
programme-specific parameter is still absent.

**A definition says what it does not cover.** Blank handling, exclusions, the
denominator's own definition, and the evidence lane are columns, not prose in a
wiki. An indicator whose blank rule is unstated is an indicator two people will
compute differently.

**No threshold lives here.** A definition says how to compute a figure. What
counts as *too high* is a programme decision, held in the configuration registry
and absent until approved. Putting a threshold on a definition would make every
consumer of the definition inherit an unapproved judgement.

**A result is immutable and carries its provenance.** Definition version, input
fingerprint, source cutoff, boundary version and completeness travel with the
number, so a figure read six months later can still be explained - and so a
recomputation under a changed definition creates a new row rather than editing
the one somebody already acted on.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import ANALYTICS, GOVERNANCE
from mars.domain.enums import (
    AgeBand,
    EvidenceLane,
    GeographyGrain,
    IndicatorSourceDomain,
    IndicatorUnit,
    IndicatorValueStatus,
    LifecycleStatus,
    PeriodGrain,
    Sex,
)


class IndicatorDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What a metric is, in terms that do not change between versions.

    The stable half. A code, what the metric is for, and the axes it lives on -
    period grain, geography grain, source domain, evidence lane. These are
    identity: an indicator that changed its period grain would not be a new
    version of the same indicator, it would be a different indicator wearing
    the same name.
    """

    __tablename__ = "indicator_definition"
    __table_args__ = (
        UniqueConstraint("code", name="uq_indicator_definition_code"),
        Index("ix_indicator_definition_domain", "source_domain"),
        Index("ix_indicator_definition_lane", "evidence_lane"),
        {
            "schema": GOVERNANCE,
            "comment": (
                "What a metric is. Carries no threshold: what counts as too "
                "high is a programme decision held in the configuration "
                "registry, not a property of the definition."
            ),
        },
    )

    #: Stable, human-readable, and referenced by every result. Never reused for
    #: a different quantity - a code is a promise about meaning.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)

    #: What the metric is for. Written for a district officer, not an engineer.
    purpose: Mapped[str] = mapped_column(Text, nullable=False)

    #: How to read it, including what it does *not* mean. This is where the
    #: difference between "positivity rose" and "transmission rose" is stated.
    interpretation: Mapped[str] = mapped_column(Text, nullable=False)

    unit: Mapped[IndicatorUnit] = mapped_column(
        pg_enum(IndicatorUnit, name="indicator_unit", schema=GOVERNANCE), nullable=False
    )
    source_domain: Mapped[IndicatorSourceDomain] = mapped_column(
        pg_enum(IndicatorSourceDomain, name="indicator_source_domain", schema=GOVERNANCE),
        nullable=False,
    )
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )
    #: The finest level this indicator can be computed at. Rollups go upward
    #: from here; nothing is ever disaggregated downward, because an aggregate
    #: cannot be split into detail the source never reported.
    base_geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    evidence_lane: Mapped[EvidenceLane] = mapped_column(
        pg_enum(EvidenceLane, name="evidence_lane", schema=GOVERNANCE),
        nullable=False,
        default=EvidenceLane.ROUTINE_SURVEILLANCE,
    )

    #: Where the definition comes from - a form and its printed field, a
    #: national guideline, a published method. An indicator with no citation is
    #: an opinion.
    definition_source: Mapped[str] = mapped_column(Text, nullable=False)

    #: The governed method this indicator is an instance of, when one exists.
    #: Optional because a plain count needs no method beyond its own definition.
    method_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.method_definition.id",
            ondelete="SET NULL",
            name="fk_indicator_method_definition",
        ),
        nullable=True,
    )

    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)

    versions: Mapped[list[IndicatorDefinitionVersion]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="IndicatorDefinitionVersion.version_number",
    )

    @property
    def active_version(self) -> IndicatorDefinitionVersion | None:
        """The version in force, if any.

        ``None`` is an ordinary answer, not an error: an indicator whose
        definition has not been approved yet is registered and inert, which is
        exactly how a definition awaiting programme sign-off should behave.
        """
        return next((v for v in self.versions if v.status is LifecycleStatus.ACTIVE), None)


class IndicatorDefinitionVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One version of how a metric is computed.

    The changeable half, and everything in it is a decision someone made:
    which source elements form the numerator, what the denominator is, how a
    blank is treated, what is excluded and why.
    """

    __tablename__ = "indicator_definition_version"
    __table_args__ = (
        UniqueConstraint(
            "indicator_definition_id",
            "version_number",
            name="uq_indicator_version_definition_number",
        ),
        CheckConstraint("version_number >= 1", name="version_number_is_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        # The same rule the method registry applies. An active definition with
        # nobody's name on it is an ungoverned definition.
        CheckConstraint(
            "status NOT IN ('approved', 'active') OR approved_by IS NOT NULL",
            name="approved_requires_approver",
        ),
        CheckConstraint("length(specification_checksum) = 64", name="checksum_is_sha256"),
        Index("ix_indicator_version_status", "indicator_definition_id", "status"),
        {"schema": GOVERNANCE},
    )

    indicator_definition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.indicator_definition.id",
            ondelete="CASCADE",
            name="fk_indicator_version_definition",
        ),
        nullable=False,
    )

    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    semantic_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")

    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, name="lifecycle_status", schema=GOVERNANCE),
        nullable=False,
        default=LifecycleStatus.DRAFT,
    )

    #: Which source elements the numerator counts, and how. Structured rather
    #: than free text so the aggregation engine reads the same thing a reviewer
    #: reads - a definition nobody can execute is a definition nobody can check.
    numerator_specification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: Null for a count. A proportion without a denominator specification is
    #: not a proportion, and the constraint below refuses one.
    denominator_specification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: How a missing input is treated. Stated per version because it is a real
    #: methodological choice: excluding blank facilities and treating them as
    #: zero give different national totals, and both are defensible for
    #: different questions.
    blank_handling: Mapped[str] = mapped_column(Text, nullable=False)

    #: What is deliberately left out, and why. The exclusions are where an
    #: indicator's honesty lives.
    exclusion_rules: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Dimensions this version permits - age band, sex, test method. Only where
    #: the source actually reports them: MARS never disaggregates a figure the
    #: facility supplied as a total.
    permitted_dimensions: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: SHA-256 over the executable specification. What makes "the same
    #: definition" the same definition across a rename or a reformat.
    specification_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The governed method version this definition is bound to, when the
    #: computation is a method rather than a sum.
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.method_version.id",
            ondelete="SET NULL",
            name="fk_indicator_version_method",
        ),
        nullable=True,
    )

    effective_from: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    reason_for_change: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Notes a reviewer needs: known limitations, comparability caveats.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    definition: Mapped[IndicatorDefinition] = relationship(back_populates="versions")


class IndicatorResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One materialised figure, with everything needed to explain it later.

    Immutable. A recomputation under different inputs or a different definition
    writes a new row; nothing is edited in place, because a district acted on
    the figure that was there and a record showing only the latest one cannot
    explain what anyone did.

    The uniqueness key is the whole provenance - definition version, grain,
    period, dimensions **and** input fingerprint - so re-running identical
    inputs is idempotent while changed inputs produce a new, separately
    readable result.
    """

    __tablename__ = "indicator_result"
    __table_args__ = (
        UniqueConstraint(
            "indicator_version_id",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "age_band",
            "sex",
            "input_fingerprint",
            name="uq_indicator_result_version_grain_period_dims_input",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint("numerator IS NULL OR numerator >= 0", name="numerator_not_negative"),
        CheckConstraint("denominator IS NULL OR denominator >= 0", name="denominator_not_negative"),
        # An undefined denominator must never become a zero value. If there is
        # no value, the status says which kind of nothing it is.
        CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR "
            "(value IS NULL AND value_status <> 'available')",
            name="value_present_iff_available",
        ),
        # A facility-grain result names a facility; a higher grain does not.
        # Without this a national row could carry a facility id and be counted
        # twice by anything joining on it.
        CheckConstraint(
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR "
            "(geography_grain <> 'facility' AND facility_id IS NULL)",
            name="facility_id_matches_grain",
        ),
        Index("ix_indicator_result_version_period", "indicator_version_id", "period_start"),
        Index("ix_indicator_result_geography", "geography_unit_id", "period_start"),
        Index("ix_indicator_result_facility", "facility_id", "period_start"),
        Index("ix_indicator_result_status", "value_status"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Materialised indicator values. Immutable: a recomputation "
                "writes a new row keyed by its input fingerprint. An undefined "
                "denominator yields no value, never zero."
            ),
        },
    )

    indicator_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.indicator_definition_version.id",
            ondelete="RESTRICT",
            name="fk_indicator_result_version",
        ),
        nullable=False,
    )

    #: Denormalised from the definition so a result stays readable when joined
    #: alone, and so a query can filter by code without a three-table join.
    indicator_code: Mapped[str] = mapped_column(String(64), nullable=False)

    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    #: Null only at national grain. Not a foreign key into a specific boundary
    #: version's units, because ``boundary_version_id`` below already says
    #: which hierarchy the id belongs to.
    geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    #: Dimensions. ``UNSPECIFIED``/``UNKNOWN`` mean "not disaggregated", which
    #: is different from "disaggregated and the value was unknown" - the latter
    #: is carried by the source's own value.
    age_band: Mapped[AgeBand] = mapped_column(
        pg_enum(AgeBand, name="age_band", schema=GOVERNANCE),
        nullable=False,
        default=AgeBand.UNSPECIFIED,
    )
    sex: Mapped[Sex] = mapped_column(
        pg_enum(Sex, name="sex", schema=GOVERNANCE), nullable=False, default=Sex.UNKNOWN
    )

    numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: A proportion is stored as a fraction, never pre-multiplied. Multiplying
    #: at presentation is reversible; a bare 43.7 is not.
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
        default=IndicatorValueStatus.AVAILABLE,
    )

    # -- Provenance --------------------------------------------------------
    #: SHA-256 over the exact source rows this figure was computed from. Two
    #: runs over unchanged data agree; a correction upstream produces a new
    #: fingerprint and therefore a new result rather than a rewritten one.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The latest source revision included. A figure computed before a late
    #: submission arrived is not wrong - it is as-of a moment, and this is that
    #: moment.
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: Which hierarchy the geography id belongs to. A district id means nothing
    #: without it: boundaries change, and comparing across versions silently is
    #: how a split district appears to double.
    boundary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    #: The configuration in force when this was computed, when the calculation
    #: consulted any. Null when it consulted none, which is honest rather than
    #: implying a governed parameter was used.
    configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What produced it, so a figure can be read against the code that made it.
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # -- Completeness ------------------------------------------------------
    #: How many reporting units contributed, out of how many were expected. A
    #: district total from four of forty facilities and one from forty of forty
    #: are different facts, and a bare total hides which.
    contributing_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Inputs that were blank rather than zero. Carried alongside the value
    #: because it is the difference between "none happened" and "nobody said".
    missing_inputs: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Anything a reader needs to interpret this number: exclusions applied,
    #: which source revisions were used, why a period was not comparable.
    quality_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    version: Mapped[IndicatorDefinitionVersion] = relationship()


__all__ = [
    "IndicatorDefinition",
    "IndicatorDefinitionVersion",
    "IndicatorResult",
]
