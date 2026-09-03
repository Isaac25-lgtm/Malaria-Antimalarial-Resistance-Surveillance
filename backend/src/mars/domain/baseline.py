"""Historical baselines: what a series usually looks like.

A baseline is the reference an anomaly is measured against, and it is the point
where surveillance most easily goes wrong. Comparing March against February in
Uganda flags the transmission season rather than an event. Comparing a facility
against three months of history when it opened two months ago produces a
confident expectation from nothing. Both mistakes look like working software.

So two records, not one.

``baseline_build`` is the run: which governed method was in force, how much
history it asked for, what it required of that history, and - when no method is
approved - which configuration is missing. A ``not_configured`` build is a
normal, expected row for a fresh deployment, and it names what is absent rather
than leaving an operator to guess why no baselines appeared.

``baseline_result`` is one expected level for one series in one place. It
carries the history it was computed from, the periods it had to exclude and
why, and a ``sufficiency`` that says plainly when there was not enough. A row
with insufficient history has **no** expected value: an expectation drawn from
two periods is worse than none, because it looks like an answer.

The envelope is shared with the surveillance results (Prompt 16), so ``value``
here means the same thing structurally as it does there - a figure that exists
only when its status says it does. What it *measures* is the expected level.
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
    BaselineBuildStatus,
    BaselineMethod,
    BaselineSeriesKind,
    BaselineSufficiency,
    DispersionMeasure,
    IndicatorValueStatus,
    PeriodGrain,
)

# Package-internal reuse: the envelope and its rules are defined once, in the
# module that introduced them, so the two cannot drift apart.
from mars.domain.surveillance import AnalyticalResultEnvelope, _envelope_constraints


class BaselineBuild(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One baseline run over one target period and one series kind."""

    __tablename__ = "baseline_build"
    __table_args__ = (
        CheckConstraint("target_period_end >= target_period_start", name="target_period_ordered"),
        CheckConstraint(
            "history_start IS NULL OR history_end IS NULL OR history_end >= history_start",
            name="history_period_ordered",
        ),
        # A completed build applied a governed method. Without this a run could
        # report expected values while recording no method that produced them.
        CheckConstraint(
            "build_status <> 'completed' OR (method_version_id IS NOT NULL AND "
            "baseline_method IS NOT NULL AND history_periods IS NOT NULL)",
            name="completed_builds_carry_their_method",
        ),
        # A refusal has to say what would end it. An operator seeing no
        # baselines needs the parameter name, not a shrug.
        #
        # The ``jsonb_typeof`` half is not redundant. A JSONB column given
        # Python ``None`` is stored as JSON ``null``, which is not SQL NULL, so
        # ``IS NOT NULL`` alone would accept a refusal that names nothing.
        CheckConstraint(
            "build_status <> 'not_configured' OR (missing_configuration IS NOT NULL AND "
            "jsonb_typeof(missing_configuration) = 'object')",
            name="refusals_name_what_is_missing",
        ),
        CheckConstraint(
            "history_periods IS NULL OR history_periods >= 1", name="history_periods_positive"
        ),
        CheckConstraint(
            "minimum_history_periods IS NULL OR minimum_history_periods >= 1",
            name="minimum_history_positive",
        ),
        CheckConstraint(
            "minimum_completeness IS NULL OR "
            "(minimum_completeness >= 0 AND minimum_completeness <= 1)",
            name="minimum_completeness_is_a_proportion",
        ),
        Index("ix_baseline_build_target", "target_period_start", "series_kind"),
        Index("ix_baseline_build_status", "build_status"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One baseline run: the governed method in force, the history it "
                "asked for, and - when no method is approved - what is missing."
            ),
        },
    )

    #: ``build_status``, not ``status``: every lifecycle in the schema carries
    #: its own named column, so a reader never has to ask which status a
    #: generic one means.
    build_status: Mapped[BaselineBuildStatus] = mapped_column(
        pg_enum(BaselineBuildStatus, name="baseline_build_status", schema=ANALYTICS),
        nullable=False,
        default=BaselineBuildStatus.RUNNING,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )

    target_period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    target_period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
    )

    #: Null until a method is approved. The build still exists, so the refusal
    #: is a record rather than an absence.
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{GOVERNANCE}.method_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    baseline_method: Mapped[BaselineMethod | None] = mapped_column(
        pg_enum(BaselineMethod, name="baseline_method", schema=ANALYTICS), nullable=True
    )

    #: How many comparable periods the method asked for, and how few it would
    #: still accept. Both are governed; neither has a default here.
    history_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_history_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_completeness: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    #: Optional. Without it a baseline has a centre and no band, which is
    #: honest: how wide an uncertainty band should be is a statistical choice
    #: a programme makes, not one an engine makes for it.
    uncertainty_multiplier: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)

    history_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    history_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    #: The parameter names an operator must have approved before this build can
    #: produce anything. Present exactly when the build refused.
    missing_configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    series_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_history: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_completeness: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list[BaselineResult]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )


class BaselineResult(AnalyticalResultEnvelope, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One expected level, and the history that produced it.

    ``value`` is the expected level. It exists only when ``sufficiency`` is
    ``sufficient``; every other sufficiency leaves it null with a status saying
    which kind of "not enough" applies. Reporting an expectation computed from
    two periods would give a district a number it could act on and should not.
    """

    __tablename__ = "baseline_result"
    __table_args__ = (
        UniqueConstraint(
            "baseline_build_id",
            "series_kind",
            "series_key",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "input_fingerprint",
            name="uq_baseline_result_build_series_scope_input",
        ),
        *_envelope_constraints("baseline_result"),
        # The rule that stops a thin history producing a usable-looking number.
        CheckConstraint(
            "(sufficiency = 'sufficient' AND value_status = 'available') OR "
            "(sufficiency <> 'sufficient' AND value_status <> 'available')",
            name="sufficiency_matches_value_status",
        ),
        # A band is two ended or it is absent. Half a band would be read as a
        # one-sided limit, which is a different claim.
        CheckConstraint(
            "(uncertainty_lower IS NULL) = (uncertainty_upper IS NULL)",
            name="band_has_both_ends",
        ),
        CheckConstraint(
            "uncertainty_lower IS NULL OR value IS NOT NULL", name="band_requires_an_expectation"
        ),
        CheckConstraint(
            "uncertainty_lower IS NULL OR uncertainty_upper >= uncertainty_lower",
            name="band_is_ordered",
        ),
        CheckConstraint("history_periods_used >= 0", name="history_used_not_negative"),
        CheckConstraint(
            "history_periods_used <= history_periods_available", name="used_within_available"
        ),
        # A single period has a centre but no spread, and calling that spread
        # zero would make the series look perfectly stable.
        CheckConstraint(
            "(dispersion_measure = 'none') = (dispersion_value IS NULL)",
            name="dispersion_measure_matches_value",
        ),
        Index("ix_baseline_result_build", "baseline_build_id"),
        Index("ix_baseline_result_series", "series_kind", "series_key", "period_start"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One expected level for one series in one place, with the "
                "history behind it and what that history was missing."
            ),
        },
    )

    baseline_build_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.baseline_build.id", ondelete="CASCADE"),
        nullable=False,
    )

    series_kind: Mapped[BaselineSeriesKind] = mapped_column(
        pg_enum(BaselineSeriesKind, name="baseline_series_kind", schema=ANALYTICS), nullable=False
    )
    #: The indicator code or measure name the baseline is for.
    series_key: Mapped[str] = mapped_column(String(96), nullable=False)

    baseline_method: Mapped[BaselineMethod] = mapped_column(
        pg_enum(BaselineMethod, name="baseline_method", schema=ANALYTICS), nullable=False
    )

    #: The expected level. Null unless the history was sufficient.
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
    )

    sufficiency: Mapped[BaselineSufficiency] = mapped_column(
        pg_enum(BaselineSufficiency, name="baseline_sufficiency", schema=ANALYTICS), nullable=False
    )

    dispersion_measure: Mapped[DispersionMeasure] = mapped_column(
        pg_enum(DispersionMeasure, name="dispersion_measure", schema=ANALYTICS),
        nullable=False,
        default=DispersionMeasure.NONE,
    )
    dispersion_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    uncertainty_lower: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    uncertainty_upper: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)

    #: How many comparable periods the method looked for, how many carried a
    #: usable value, and how many it needed. All three, because "8 of 12, needed
    #: 6" and "8 of 8, needed 6" describe different confidence.
    history_periods_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    history_periods_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    history_periods_required: Mapped[int | None] = mapped_column(Integer, nullable=True)

    history_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    history_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    #: The periods that contributed, with their values. Kept so a later
    #: explainability object can show the history rather than assert it.
    contributing_periods: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: The periods that did not contribute, each with a reason. A baseline that
    #: silently drops half its history is a baseline nobody can audit.
    excluded_periods: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    build: Mapped[BaselineBuild] = relationship(back_populates="results")


__all__ = ["BaselineBuild", "BaselineResult"]
