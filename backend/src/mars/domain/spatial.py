"""Geographic aggregation and hotspots.

Two records and a run.

``geographic_aggregation_result`` is one measure for one administrative unit
for one period. It is **recomputed** from the numerators and denominators
underneath, never averaged from the facility values: the mean of five facility
positivity rates is not the district's positivity rate, and the difference
grows with how unequal the facilities are.

Every row carries the basis it was rolled up by. A patient may attend a clinic
outside their own district; rolling up by where care was given points at a
clinic, and rolling up by where people live points at a village. The two are
stored separately and a constraint keeps them from being confused, because
merging them attributes a pattern to the wrong place.

Every row also carries how much of the unit reported. A district figure built
from three of its twenty facilities is not a district figure, and a reader who
cannot see that will treat it as one.

``hotspot_result`` is one area, one period, one metric. Blueprint 037 is
explicit that a hotspot must have a method rather than a red colour, so the row
cites two governed versions - the definition that called it a hotspot, and the
temporal baseline method that produced the expectation - together with the
threshold applied, the minimum case count, the completeness required, and how
many consecutive periods the area has been flagged.

The expectation comes from the **area's own history** in
``geographic_aggregation_result``, summarised under the approved baseline
method. A district measured against the history of its facilities rather than
its own would be compared with a quantity nobody reports.

Persistence is computed by reading the previous period's row rather than kept
as a mutable tally, so nothing already written changes meaning.

``spatial_run`` is the run, and exists so a refusal is a record: with no
approved hotspot definition it is stored as ``not_configured`` naming the
missing parameters.
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
from mars.db.schemas import ANALYTICS, CORE, GOVERNANCE
from mars.domain.enums import (
    BaselineSeriesKind,
    GeographyGrain,
    HotspotOutcome,
    IndicatorValueStatus,
    PeriodGrain,
    SpatialAggregationBasis,
    SpatialRunStatus,
)


class SpatialRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One geographic aggregation or hotspot run, or its refusal."""

    __tablename__ = "spatial_run"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint(
            "run_status <> 'completed' OR run_kind <> 'hotspot' OR "
            "(method_version_id IS NOT NULL AND deviation_threshold IS NOT NULL AND "
            "minimum_case_count IS NOT NULL AND minimum_completeness IS NOT NULL)",
            name="completed_hotspot_runs_carry_their_definition",
        ),
        CheckConstraint(
            "run_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name="refusals_name_what_is_missing",
        ),
        CheckConstraint("run_kind IN ('aggregation', 'hotspot')", name="run_kind_is_known"),
        CheckConstraint(
            "minimum_completeness IS NULL OR "
            "(minimum_completeness >= 0 AND minimum_completeness <= 1)",
            name="minimum_completeness_is_a_proportion",
        ),
        Index("ix_spatial_run_period", "period_start", "run_kind"),
        Index("ix_spatial_run_status", "run_status"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One geographic aggregation or hotspot run. A refusal is a row, not an absence."
            ),
        },
    )

    #: ``aggregation`` or ``hotspot``. A plain string rather than an enum: the
    #: two runs share every other column, and a third kind would be a code
    #: change here rather than a type change in three schemas.
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    run_status: Mapped[SpatialRunStatus] = mapped_column(
        pg_enum(SpatialRunStatus, name="spatial_run_status", schema=ANALYTICS),
        nullable=False,
        default=SpatialRunStatus.RUNNING,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    aggregation_basis: Mapped[SpatialAggregationBasis] = mapped_column(
        pg_enum(SpatialAggregationBasis, name="spatial_aggregation_basis", schema=ANALYTICS),
        nullable=False,
    )
    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    boundary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.boundary_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    baseline_build_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.baseline_build.id", ondelete="RESTRICT"),
        nullable=True,
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: The governed hotspot definition, copied onto the run. Blueprint 037
    #: names each of these as part of a hotspot's definition.
    deviation_threshold: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    minimum_case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    persistence_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)

    missing_configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    units_examined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    aggregations: Mapped[list[GeographicAggregationResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    hotspots: Mapped[list[HotspotResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class GeographicAggregationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One measure for one administrative unit for one period."""

    __tablename__ = "geographic_aggregation_result"
    __table_args__ = (
        UniqueConstraint(
            "spatial_run_id",
            "series_kind",
            "series_key",
            "geography_unit_id",
            "aggregation_basis",
            "input_fingerprint",
            name="uq_geographic_aggregation_run_series_unit_basis_input",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR "
            "(value IS NULL AND value_status <> 'available')",
            name="value_present_iff_available",
        ),
        CheckConstraint("numerator IS NULL OR numerator >= 0", name="numerator_not_negative"),
        CheckConstraint("denominator IS NULL OR denominator >= 0", name="denominator_not_negative"),
        # A value computed from no denominator is not a rate. The rest of MARS
        # keeps this distinction and a roll-up is where it is most tempting to
        # lose it.
        CheckConstraint(
            "value IS NULL OR denominator IS NULL OR denominator > 0",
            name="a_rate_needs_a_denominator",
        ),
        CheckConstraint(
            "contributing_facilities >= 0 AND expected_facilities >= 0",
            name="facility_counts_not_negative",
        ),
        CheckConstraint(
            "contributing_facilities <= expected_facilities",
            name="contributors_within_expected",
        ),
        # An aggregate belongs to an administrative unit, never a facility. A
        # facility-level row here would be the source data, not a roll-up.
        CheckConstraint("geography_grain <> 'facility'", name="not_a_facility_grain"),
        Index("ix_geographic_aggregation_unit", "geography_unit_id", "period_start"),
        Index("ix_geographic_aggregation_series", "series_kind", "series_key", "period_start"),
        Index("ix_geographic_aggregation_run", "spatial_run_id"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One measure rolled up to one administrative unit, recomputed "
                "from its parts and carrying how much of the unit reported."
            ),
        },
    )

    # These three foreign keys carry explicit names. Left to the naming
    # convention they resolve past PostgreSQL's 63-character limit and are
    # truncated to an opaque hash suffix - a name nobody can read in the error
    # message that eventually quotes it.
    spatial_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.spatial_run.id",
            ondelete="CASCADE",
            name="fk_geographic_aggregation_run",
        ),
        nullable=False,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    series_key: Mapped[str] = mapped_column(String(96), nullable=False)

    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.geography_unit.id",
            ondelete="RESTRICT",
            name="fk_geographic_aggregation_geography_unit",
        ),
        nullable=False,
    )
    boundary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.boundary_version.id",
            ondelete="RESTRICT",
            name="fk_geographic_aggregation_boundary_version",
        ),
        nullable=True,
    )
    #: Where care was given, or where people live. Never mixed.
    aggregation_basis: Mapped[SpatialAggregationBasis] = mapped_column(
        pg_enum(SpatialAggregationBasis, name="spatial_aggregation_basis", schema=ANALYTICS),
        nullable=False,
    )

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    #: Recomputed from the parts, never averaged. The mean of five facility
    #: rates is not the district's rate.
    numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
    )

    #: How much of the unit stands behind the figure. A district figure built
    #: from three of twenty facilities is not a district figure.
    contributing_facilities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_facilities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reporting_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    #: Encounters whose residence did not resolve to a unit. They contribute to
    #: nothing, and their absence always makes a residence map look emptier
    #: than the truth.
    unresolved_contributions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    quality_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[SpatialRun] = relationship(back_populates="aggregations")


class HotspotResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One area, one period, one metric - with the method that made it red."""

    __tablename__ = "hotspot_result"
    __table_args__ = (
        UniqueConstraint(
            "spatial_run_id",
            "series_kind",
            "series_key",
            "geography_unit_id",
            "input_fingerprint",
            name="uq_hotspot_result_run_series_unit_input",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        # Blueprint 037: a hotspot must have a method, not just a red colour.
        # Two governed versions, because two decisions were made: which
        # definition called it a hotspot, and which baseline method produced
        # the expectation it was measured against.
        CheckConstraint(
            "outcome <> 'hotspot' OR (method_version_id IS NOT NULL AND "
            "baseline_method_version_id IS NOT NULL AND deviation_threshold IS NOT NULL "
            "AND observed_value IS NOT NULL AND expected_value IS NOT NULL)",
            name="a_hotspot_carries_its_method",
        ),
        # The same rule the temporal engine keeps: "not a hotspot" must mean
        # examined and found unremarkable, never "could not tell".
        CheckConstraint(
            "outcome <> 'not_hotspot' OR (expected_value IS NOT NULL AND "
            "baseline_method_version_id IS NOT NULL)",
            name="not_hotspot_means_examined",
        ),
        CheckConstraint(
            "outcome <> 'not_evaluated_no_baseline' OR expected_value IS NULL",
            name="no_baseline_means_no_expectation",
        ),
        CheckConstraint("consecutive_periods >= 0", name="consecutive_periods_not_negative"),
        # Persistence is a count; calling a run persistent is a judgement.
        CheckConstraint(
            "is_persistent IS NULL OR (method_version_id IS NOT NULL AND "
            "persistence_periods IS NOT NULL)",
            name="persistent_requires_configuration",
        ),
        CheckConstraint(
            "geography_grain <> 'facility'", name="a_hotspot_is_an_area_not_a_facility"
        ),
        Index("ix_hotspot_result_unit", "geography_unit_id", "period_start"),
        Index("ix_hotspot_result_outcome", "outcome", "period_start"),
        Index("ix_hotspot_result_run", "spatial_run_id"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One area evaluated against a governed hotspot definition, "
                "including the areas that could not be evaluated and why."
            ),
        },
    )

    spatial_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.spatial_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    aggregation_result_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.geographic_aggregation_result.id",
            ondelete="RESTRICT",
            name="fk_hotspot_result_aggregation",
        ),
        nullable=True,
    )
    #: The governed hotspot definition that judged this area.
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.method_version.id",
            ondelete="RESTRICT",
            name="fk_hotspot_result_definition_version",
        ),
        nullable=True,
    )
    #: The governed temporal baseline method that produced the expectation. A
    #: separate column because it is a separate decision: one says how large a
    #: departure has to be, the other says what normal was.
    baseline_method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{GOVERNANCE}.method_version.id",
            ondelete="RESTRICT",
            name="fk_hotspot_result_baseline_version",
        ),
        nullable=True,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    series_key: Mapped[str] = mapped_column(String(96), nullable=False)

    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    geography_unit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.geography_unit.id", ondelete="RESTRICT"),
        nullable=False,
    )
    aggregation_basis: Mapped[SpatialAggregationBasis] = mapped_column(
        pg_enum(SpatialAggregationBasis, name="spatial_aggregation_basis", schema=ANALYTICS),
        nullable=False,
    )

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    outcome: Mapped[HotspotOutcome] = mapped_column(
        pg_enum(HotspotOutcome, name="hotspot_outcome", schema=ANALYTICS), nullable=False
    )

    observed_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    expected_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    absolute_deviation: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    relative_deviation: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    deviation_score: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    #: The definition applied, copied onto the row. Blueprint 037 requires a
    #: hotspot to state its metric, threshold, minimum count and completeness.
    deviation_threshold: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reporting_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    minimum_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    contributing_facilities: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_facilities: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: How much of the area's own history stood behind the expectation.
    history_periods_used: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Blueprint 037: first detected, last detected, consecutive periods.
    #: Computed by reading the previous period's row rather than kept as a
    #: mutable tally, so nothing already written changes meaning.
    consecutive_periods: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_detected_period_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    last_detected_period_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    #: Null unless a programme has approved a persistence rule.
    is_persistent: Mapped[bool | None] = mapped_column(nullable=True)
    persistence_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)

    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    quality_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[SpatialRun] = relationship(back_populates="hotspots")


__all__ = ["GeographicAggregationResult", "HotspotResult", "SpatialRun"]
