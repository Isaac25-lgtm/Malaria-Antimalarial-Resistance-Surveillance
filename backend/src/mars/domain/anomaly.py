"""Temporal anomalies and how long they lasted.

Three records, because three different things need to survive.

``anomaly_build`` is the run. With no approved detection rule it is stored as
``not_configured`` and names the missing parameters, so a quiet deployment is
legibly unconfigured rather than apparently calm.

``temporal_anomaly_result`` is one observation judged against one baseline. It
keeps observed, expected, both deviations, the band, the method and the
threshold that was applied - everything a reader needs to disagree with it.
Crucially it also keeps the cases where MARS could **not** judge: no baseline,
too few cases, no case count at all. "Could not judge" stored as "judged
normal" is the failure that makes a surveillance system quietly useless, and a
district reading a quiet map is entitled to know which quiet it is looking at.

``anomaly_persistence`` separates a one-period spike from a sustained one. It
counts consecutive flagged periods - a fact - and calls the run *sustained*
only when a programme has approved how many periods that takes. The count is
arithmetic; the label is a judgement, and they are stored in different columns
for that reason.

Nothing here claims a cause. A flagged period says an observation departed from
its own history by more than an approved amount. It does not say why, and it
never says resistance.
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
    AnomalyBuildStatus,
    AnomalyDetectionMethod,
    AnomalyDirection,
    AnomalyOutcome,
    BaselineSeriesKind,
    GeographyGrain,
    PeriodGrain,
)


class AnomalyBuild(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One detection run over one period and one series kind."""

    __tablename__ = "anomaly_build"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint(
            "build_status <> 'completed' OR (method_version_id IS NOT NULL AND "
            "detection_method IS NOT NULL AND deviation_threshold IS NOT NULL)",
            name="completed_runs_carry_their_rule",
        ),
        # A refusal names what would end it. ``jsonb_typeof`` as well as
        # nullity, because a JSONB column given a Python ``None`` is stored as
        # JSON ``null`` rather than SQL NULL, and a refusal naming nothing
        # would otherwise pass.
        CheckConstraint(
            "build_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name="refusals_name_what_is_missing",
        ),
        CheckConstraint(
            "minimum_case_count IS NULL OR minimum_case_count >= 0",
            name="minimum_case_count_not_negative",
        ),
        CheckConstraint(
            "persistence_periods IS NULL OR persistence_periods >= 1",
            name="persistence_periods_positive",
        ),
        Index("ix_anomaly_build_period", "period_start", "series_kind"),
        Index("ix_anomaly_build_status", "build_status"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One anomaly detection run: the governed rule in force, or - "
                "when none is approved - which parameters are missing."
            ),
        },
    )

    build_status: Mapped[AnomalyBuildStatus] = mapped_column(
        pg_enum(AnomalyBuildStatus, name="anomaly_build_status", schema=ANALYTICS),
        nullable=False,
        default=AnomalyBuildStatus.RUNNING,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    #: The baseline run this detection compared against. Null only when the
    #: run refused, because a detection with no baseline has nothing to say.
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
    detection_method: Mapped[AnomalyDetectionMethod | None] = mapped_column(
        pg_enum(AnomalyDetectionMethod, name="anomaly_detection_method", schema=ANALYTICS),
        nullable=True,
    )
    #: How large a departure has to be. Governed; no default exists anywhere in
    #: MARS, because the number decides how many districts get an alert.
    deviation_threshold: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    #: Fewest cases an observation must carry to be judged at all. A doubling
    #: of two cases is arithmetic, not epidemiology.
    minimum_case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: How many consecutive flagged periods make a run *sustained*. Optional:
    #: without it MARS counts consecutive periods and declines to label them.
    persistence_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)

    missing_configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    observations_examined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flagged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_flagged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list[TemporalAnomalyResult]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )


class TemporalAnomalyResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One observation judged against one baseline.

    Carries everything a reader needs to disagree: what was observed, what was
    expected, how far apart they are in two forms, the band, the method, the
    threshold that was applied and how much history stood behind the
    expectation.
    """

    __tablename__ = "temporal_anomaly_result"
    __table_args__ = (
        UniqueConstraint(
            "anomaly_build_id",
            "series_kind",
            "series_key",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "input_fingerprint",
            name="uq_temporal_anomaly_build_series_scope_input",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint(
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR "
            "(geography_grain <> 'facility' AND facility_id IS NULL)",
            name="facility_id_matches_grain",
        ),
        # A flag is a claim, and a claim needs both halves of its evidence:
        # something to compare against, and an approved rule saying how far is
        # too far.
        CheckConstraint(
            "outcome <> 'flagged' OR (baseline_result_id IS NOT NULL AND "
            "method_version_id IS NOT NULL AND deviation_threshold IS NOT NULL)",
            name="a_flag_carries_its_evidence",
        ),
        CheckConstraint(
            "outcome <> 'flagged' OR absolute_deviation IS NOT NULL",
            name="a_flag_has_a_deviation",
        ),
        # The one that matters most: "not flagged" must mean *evaluated and
        # within range*. If it could also mean "could not tell", a quiet map
        # would carry two opposite meanings in one colour.
        CheckConstraint(
            "outcome <> 'not_flagged' OR (absolute_deviation IS NOT NULL AND "
            "baseline_result_id IS NOT NULL)",
            name="not_flagged_means_evaluated",
        ),
        CheckConstraint(
            "absolute_deviation IS NULL OR expected_value IS NOT NULL",
            name="a_deviation_needs_an_expectation",
        ),
        # No baseline means nothing to compare against, so there is no
        # expectation and no deviation to record.
        CheckConstraint(
            "outcome <> 'not_evaluated_no_baseline' OR (expected_value IS NULL AND "
            "absolute_deviation IS NULL AND baseline_result_id IS NULL)",
            name="no_baseline_means_no_expectation",
        ),
        CheckConstraint(
            "(uncertainty_lower IS NULL) = (uncertainty_upper IS NULL)",
            name="band_has_both_ends",
        ),
        Index("ix_temporal_anomaly_build", "anomaly_build_id"),
        Index("ix_temporal_anomaly_series", "series_kind", "series_key", "period_start"),
        Index("ix_temporal_anomaly_outcome", "outcome", "period_start"),
        Index("ix_temporal_anomaly_facility", "facility_id", "period_start"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One observation judged against one baseline, including the "
                "observations MARS could not judge and why."
            ),
        },
    )

    anomaly_build_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.anomaly_build.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The exact baseline row compared against, not just the run. A later
    #: reader can retrieve the history that produced the expectation.
    baseline_result_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.baseline_result.id", ondelete="RESTRICT"),
        nullable=True,
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=True,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    series_key: Mapped[str] = mapped_column(String(96), nullable=False)

    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    outcome: Mapped[AnomalyOutcome] = mapped_column(
        pg_enum(AnomalyOutcome, name="anomaly_outcome", schema=ANALYTICS), nullable=False
    )
    direction: Mapped[AnomalyDirection | None] = mapped_column(
        pg_enum(AnomalyDirection, name="anomaly_direction", schema=ANALYTICS), nullable=True
    )
    detection_method: Mapped[AnomalyDetectionMethod | None] = mapped_column(
        pg_enum(AnomalyDetectionMethod, name="anomaly_detection_method", schema=ANALYTICS),
        nullable=True,
    )

    #: What actually happened. Present whenever the source reported a value,
    #: including when MARS could not evaluate it - the observation is a fact
    #: regardless of whether it could be judged.
    observed_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    expected_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    #: Both forms, because each misleads alone. An absolute rise of 0.1 is
    #: trivial at 0.8 and dramatic at 0.02; a relative doubling is trivial at
    #: two cases and serious at two hundred.
    absolute_deviation: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    relative_deviation: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    #: Deviation in units of the baseline's own spread, when the baseline had
    #: one. Null for a single-period baseline rather than zero.
    deviation_score: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    uncertainty_lower: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    uncertainty_upper: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    #: The governed threshold this observation was measured against, copied
    #: onto the row. A later change to the rule must not silently rewrite what
    #: a past detection meant.
    deviation_threshold: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    #: The case count behind the observation, and the minimum it was held to.
    case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_case_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How much history stood behind the expectation. A flag against four
    #: periods and a flag against twenty-four are different claims.
    history_periods_used: Mapped[int | None] = mapped_column(Integer, nullable=True)

    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Why MARS could not judge, when it could not. Never a silent skip.
    quality_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    build: Mapped[AnomalyBuild] = relationship(back_populates="results")


class AnomalyPersistence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An unbroken run of flagged periods for one series in one place.

    A one-period spike and a six-month rise need different responses, and
    presenting them identically is how alert fatigue starts.

    ``consecutive_periods`` is arithmetic. ``is_sustained`` is a judgement, and
    stays null until a programme approves how many periods make a run
    sustained. Two columns rather than one, so the difference cannot be lost.
    """

    __tablename__ = "anomaly_persistence"
    __table_args__ = (
        UniqueConstraint(
            "series_kind",
            "series_key",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "first_period_start",
            name="uq_anomaly_persistence_series_scope_start",
        ),
        CheckConstraint("last_period_end >= first_period_start", name="run_period_ordered"),
        CheckConstraint("consecutive_periods >= 1", name="a_run_has_at_least_one_period"),
        # Sustained is a label a programme defines. Without an approved
        # persistence rule MARS counts and declines to label.
        CheckConstraint(
            "is_sustained IS NULL OR (method_version_id IS NOT NULL AND "
            "persistence_periods IS NOT NULL)",
            name="sustained_requires_configuration",
        ),
        CheckConstraint(
            "persistence_periods IS NULL OR persistence_periods >= 1",
            name="persistence_periods_positive",
        ),
        CheckConstraint(
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR "
            "(geography_grain <> 'facility' AND facility_id IS NULL)",
            name="facility_id_matches_grain",
        ),
        Index("ix_anomaly_persistence_series", "series_kind", "series_key"),
        Index("ix_anomaly_persistence_last_seen", "last_period_end"),
        {
            "schema": ANALYTICS,
            "comment": (
                "An unbroken run of flagged periods. The count is arithmetic; "
                "calling it sustained requires an approved rule."
            ),
        },
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    series_key: Mapped[str] = mapped_column(String(96), nullable=False)

    geography_grain: Mapped[GeographyGrain] = mapped_column(
        pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
    )
    geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )
    first_period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    last_period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    consecutive_periods: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Null unless a programme has approved a persistence rule.
    is_sustained: Mapped[bool | None] = mapped_column(nullable=True)
    persistence_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: The flagged results making up the run, oldest first.
    contributing_result_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    first_detected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_detected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["AnomalyBuild", "AnomalyPersistence", "TemporalAnomalyResult"]
