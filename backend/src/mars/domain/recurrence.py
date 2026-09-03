"""Recurrence surveillance results: counts of observed patterns, and nothing more.

Every row here answers a question of the form "how many patients returned with a
second positive result, and how long did they take". None answers "did the
treatment work", and none may be presented as if it did.

**Facility of care and residence geography are separate scopes.** A patient may
attend a clinic outside their own district. Merging the two attributes a pattern
to the wrong place, and the two questions they answer are different: a facility
concentration points at a clinic, a residence concentration points at a village.

**Every result carries its denominator and its exclusions.** A repeat-positive
count without the number of linked patients it came from is unreadable, and one
without the number of *unlinked* encounters is misleading - those are patients
MARS could not follow, and their absence always makes recurrence look rarer than
it is.

**Interval bands are governed, not shipped.** MARS records actual return
intervals in days on the episode members; banding them is a programme decision
held in the configuration registry. A result with no approved bands reports the
counts it can and says the bands are unavailable.
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
from mars.db.schemas import ANALYTICS
from mars.domain.enums import (
    IndicatorValueStatus,
    PeriodGrain,
    RecurrenceMeasure,
    RecurrenceScopeKind,
)


class RecurrenceResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One recurrence measure for one scope and period.

    Immutable and fingerprinted, like every other analytical result: a
    recomputation over changed episodes writes a new row rather than editing
    one a district may have acted on.
    """

    __tablename__ = "recurrence_result"
    __table_args__ = (
        UniqueConstraint(
            "episode_build_id",
            "measure",
            "scope_kind",
            "scope_id",
            "period_start",
            "interval_band",
            "input_fingerprint",
            name="uq_recurrence_result_build_measure_scope_period_band",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint("numerator IS NULL OR numerator >= 0", name="numerator_not_negative"),
        CheckConstraint("denominator IS NULL OR denominator >= 0", name="denominator_not_negative"),
        # The same rule the indicator results carry: an undefined denominator
        # yields no value, never a zero. A recurrence proportion of 0.0 and one
        # that could not be computed are opposite statements about a facility.
        CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR "
            "(value IS NULL AND value_status <> 'available')",
            name="value_present_iff_available",
        ),
        # A band belongs to a band count and nothing else. Without this a
        # patient count could carry a band and be double-counted by anything
        # grouping on it.
        CheckConstraint(
            "(measure = 'interval_band_count' AND interval_band IS NOT NULL) OR "
            "(measure <> 'interval_band_count' AND interval_band IS NULL)",
            name="band_only_on_band_counts",
        ),
        Index("ix_recurrence_result_scope", "scope_kind", "scope_id", "period_start"),
        Index("ix_recurrence_result_measure", "measure", "period_start"),
        Index("ix_recurrence_result_build", "episode_build_id"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Counts of observed recurrence patterns. Never a clinical "
                "outcome: routine data cannot establish treatment failure, "
                "recrudescence, reinfection or resistance."
            ),
        },
    )

    #: The episode build these counts came from. Recurrence is a property of a
    #: grouping, so the grouping's rule version is part of the result's meaning.
    episode_build_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.episode_build.id", ondelete="CASCADE", name="fk_recurrence_build"),
        nullable=False,
    )

    measure: Mapped[RecurrenceMeasure] = mapped_column(
        pg_enum(RecurrenceMeasure, name="recurrence_measure", schema=ANALYTICS), nullable=False
    )
    scope_kind: Mapped[RecurrenceScopeKind] = mapped_column(
        pg_enum(RecurrenceScopeKind, name="recurrence_scope_kind", schema=ANALYTICS),
        nullable=False,
    )
    #: The facility or geography unit. Not a foreign key: the scope kind says
    #: which table it belongs to, and a single nullable column with a discriminator
    #: keeps one row shape for every scope.
    scope_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[PeriodGrain] = mapped_column(
        pg_enum(PeriodGrain, name="period_grain", schema="mars_governance"), nullable=False
    )

    #: The governed band this count belongs to, for band counts only. Stored as
    #: the band's label so a result stays readable after the bands change - the
    #: configuration version below says which definition produced it.
    interval_band: Mapped[str | None] = mapped_column(String(64), nullable=True)

    numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
        default=IndicatorValueStatus.AVAILABLE,
    )

    # -- The population this describes -------------------------------------
    #: Linked patients with at least one positive result in scope. The
    #: population recurrence is measured *within*.
    eligible_patients: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Encounters MARS could not link to any patient. Always reported: their
    #: absence makes recurrence look rarer than it is, and a reader who cannot
    #: see the number cannot correct for it.
    excluded_unlinked_encounters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Positive results with no antimalarial recorded. The ordinary explanation
    #: for a repeat positive, and the first thing an investigator should rule
    #: out.
    positives_without_treatment_record: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Episodes whose residence did not resolve, so they contribute to facility
    #: measures but not residence ones.
    residence_unresolved_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- Provenance --------------------------------------------------------
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: The episode rule in force. Denormalised because recurrence read under a
    #: 28-day window is a different quantity from recurrence read under 42.
    episode_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    #: The configuration that supplied the interval bands, when bands were used.
    configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    boundary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: What a reader needs before using this figure. Always includes the
    #: statement that a repeat positive is not evidence of treatment failure.
    interpretation_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Why a figure is unavailable, when it is.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    build: Mapped[Any] = relationship("EpisodeBuild")


__all__ = ["RecurrenceResult"]
