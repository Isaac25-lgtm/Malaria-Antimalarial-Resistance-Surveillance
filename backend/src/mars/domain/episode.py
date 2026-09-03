"""Malaria episode candidates: what routine data can and cannot say.

An episode is a grouping of one patient's encounters that *may* be one illness.
The word "candidate" is load-bearing throughout this module, and so is the
uncertainty it names.

**Routine data cannot distinguish recrudescence from reinfection.** A second
positive result forty days after the first may be the same infection that
survived treatment, or a new one from a new bite. Nothing in an e-register
separates them: that needs parasite genotyping. So MARS records the visits, the
interval and whether treatment was recorded, and calls the pattern what it is -
a repeat positive worth investigating.

**Routine data cannot prove adherence or drug exposure.** A prescription line
says a drug was prescribed. It does not say it was dispensed, taken, taken
correctly, or of adequate quality. An episode that records "treated" is
recording a register entry, not a pharmacological fact.

**Linkage is pseudonymous and stays that way.** Episodes are built from
``patient_reference_id`` and nothing else. No name, identifier or contact
detail is read, and the identity vault is never queried - which is why this
module imports nothing from ``mars.identity``.

**Unlinked encounters are counted, never invented.** An encounter with no
usable identifier cannot join an episode. Its absence is a limit on what MARS
can see, reported as a number, and never patched by guessing that two similar
encounters are one person.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import ANALYTICS, CORE
from mars.domain.enums import (
    EpisodeBuildStatus,
    EpisodeEncounterRole,
    EpisodeStatus,
)


class EpisodeBuild(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One run of the episode engine, and what it was working from.

    A build is recorded because an episode's meaning depends entirely on the
    rule that made it. Reading an episode six months later without knowing
    which window was in force would be reading a number with no units.
    """

    __tablename__ = "episode_build"
    __table_args__ = (
        UniqueConstraint(
            "rule_version_id",
            "input_fingerprint",
            "period_start",
            "period_end",
            name="uq_episode_build_rule_input_period",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint("encounters_considered >= 0", name="considered_not_negative"),
        Index("ix_episode_build_status", "build_status"),
        Index("ix_episode_build_period", "period_start", "period_end"),
        {
            "schema": ANALYTICS,
            "comment": (
                "One episode-engine run. An episode's meaning depends on the "
                "rule version that built it, so the run is part of the record."
            ),
        },
    )

    #: The governed rule this build applied. Null only for a build recorded as
    #: ``not_configured`` - which is the honest way to record that a run was
    #: asked for before the programme approved a window.
    rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "mars_governance.method_version.id",
            ondelete="RESTRICT",
            name="fk_episode_build_rule",
        ),
        nullable=True,
    )

    build_status: Mapped[EpisodeBuildStatus] = mapped_column(
        pg_enum(EpisodeBuildStatus, name="episode_build_status", schema=ANALYTICS),
        nullable=False,
        default=EpisodeBuildStatus.RUNNING,
    )

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    #: SHA-256 over the encounters read. Re-running over unchanged evidence is
    #: idempotent; a corrected encounter produces a new build rather than
    #: silently changing episodes a clinician has already looked at.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The latest source moment included. An episode built before a late
    #: encounter arrived is not wrong - it is as-of a moment.
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    encounters_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Encounters that could not join any episode because nothing linked them.
    #: A first-class number: it is the size of what MARS cannot see, and a
    #: recurrence rate computed without it would be quietly overstated.
    encounters_unlinked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    episodes_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patients_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Why a build produced nothing, when it produced nothing. The commonest
    #: value is that no episode rule has been approved.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    episodes: Mapped[list[EpisodeCandidate]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )


class EpisodeCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One patient's encounters that may be a single illness.

    Immutable. A rebuild under a different rule or over corrected encounters
    creates a new episode in a new build; nothing is edited, because a
    clinician may already have read this one.

    **Nothing here is a clinical conclusion.** ``repeat_positive_count`` says
    how many positive results this grouping contains. It does not say the
    treatment failed, that the parasite recrudesced, or that anything is
    resistant - and the API and explanation layers are forbidden from saying so
    on its behalf.
    """

    __tablename__ = "episode_candidate"
    __table_args__ = (
        UniqueConstraint(
            "episode_build_id",
            "patient_reference_id",
            "episode_number",
            name="uq_episode_candidate_build_patient_number",
        ),
        CheckConstraint("episode_number >= 1", name="episode_number_is_positive"),
        CheckConstraint("last_encounter_date >= first_encounter_date", name="dates_ordered"),
        CheckConstraint("encounter_count >= 1", name="encounter_count_is_positive"),
        CheckConstraint(
            "positive_encounter_count >= 0 AND positive_encounter_count <= encounter_count",
            name="positives_within_encounters",
        ),
        # A span is a consequence of the dates, and storing a contradictory one
        # would let a query disagree with the timeline it is derived from.
        CheckConstraint(
            "span_days = (last_encounter_date - first_encounter_date)",
            name="span_matches_dates",
        ),
        Index("ix_episode_candidate_patient", "patient_reference_id"),
        Index("ix_episode_candidate_build", "episode_build_id"),
        Index("ix_episode_candidate_facility", "index_facility_id"),
        Index("ix_episode_candidate_dates", "first_encounter_date", "last_encounter_date"),
        {
            "schema": ANALYTICS,
            "comment": (
                "A grouping of one pseudonymous patient's encounters that may "
                "be one illness. A candidate, never a clinical conclusion: "
                "routine data cannot distinguish recrudescence from "
                "reinfection or establish drug exposure."
            ),
        },
    )

    episode_build_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{ANALYTICS}.episode_build.id", ondelete="CASCADE", name="fk_episode_build"),
        nullable=False,
    )

    #: The pseudonymous reference. **Never** a name, identifier or contact
    #: detail - this column is the whole of what the episode engine knows about
    #: who a patient is.
    patient_reference_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.patient_reference.id", ondelete="CASCADE", name="fk_episode_patient"),
        nullable=False,
    )

    #: This patient's episodes in order within the build. Lets an explanation
    #: say "second episode" without recomputing the ordering.
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    episode_status: Mapped[EpisodeStatus] = mapped_column(
        pg_enum(EpisodeStatus, name="episode_status", schema=ANALYTICS),
        nullable=False,
        default=EpisodeStatus.CANDIDATE,
    )

    first_encounter_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    last_encounter_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    #: Days between first and last encounter. Zero for a single-visit episode.
    span_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    encounter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    positive_encounter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tested_encounter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Encounters in this episode with an antimalarial recorded. A register
    #: entry, not evidence the patient took anything.
    treated_encounter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Where the episode started. Facility of care, deliberately separate from
    #: residence: a patient may attend a facility outside their own district,
    #: and merging the two attributes a case to the wrong place.
    index_facility_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.facility.id", ondelete="SET NULL", name="fk_episode_facility"),
        nullable=True,
    )
    #: Residence as recorded, when it resolved. Null is an ordinary answer.
    residence_district_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    residence_subcounty_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    #: What a reviewer needs before using this episode: a missing treatment
    #: record, an unresolved residence, an interval measured across a facility
    #: change. Uncertainty is carried with the episode, not left to be
    #: rediscovered.
    uncertainty: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    build: Mapped[EpisodeBuild] = relationship(back_populates="episodes")
    members: Mapped[list[EpisodeMember]] = relationship(
        back_populates="episode",
        cascade="all, delete-orphan",
        order_by="EpisodeMember.sequence",
    )

    @property
    def has_repeat_positive(self) -> bool:
        """Whether this grouping contains more than one positive result.

        A reason to look. Not a finding, and not evidence of treatment failure.
        """
        return self.positive_encounter_count >= 2


class EpisodeMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One encounter's membership of one episode, in order.

    The order is stored rather than derived so an explanation can present a
    timeline without re-running the rule, and so the timeline a clinician saw
    stays the timeline that is recorded.
    """

    __tablename__ = "episode_member"
    __table_args__ = (
        UniqueConstraint(
            "episode_candidate_id",
            "opd_encounter_id",
            name="uq_episode_member_episode_encounter",
        ),
        UniqueConstraint(
            "episode_candidate_id", "sequence", name="uq_episode_member_episode_sequence"
        ),
        CheckConstraint("sequence >= 1", name="sequence_is_positive"),
        CheckConstraint(
            "days_since_previous IS NULL OR days_since_previous >= 0",
            name="interval_not_negative",
        ),
        Index("ix_episode_member_encounter", "opd_encounter_id"),
        {"schema": ANALYTICS},
    )

    episode_candidate_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{ANALYTICS}.episode_candidate.id", ondelete="CASCADE", name="fk_member_episode"
        ),
        nullable=False,
    )
    opd_encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.opd_encounter.id", ondelete="CASCADE", name="fk_member_encounter"),
        nullable=False,
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    member_role: Mapped[EpisodeEncounterRole] = mapped_column(
        pg_enum(EpisodeEncounterRole, name="episode_encounter_role", schema=ANALYTICS),
        nullable=False,
        default=EpisodeEncounterRole.INDEX,
    )

    encounter_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    #: Actual days since the previous encounter in this episode. Null for the
    #: index. Stored in days rather than banded: bands are governed
    #: configuration and are absent until approved, and an interval recorded in
    #: a band cannot later be re-banded.
    days_since_previous: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Denormalised evidence, so a timeline reads without joining four tables
    #: and so it still reads if an encounter is later corrected - the episode
    #: records what was true when it was built.
    test_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    test_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attendance_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    antimalarial_recorded: Mapped[bool] = mapped_column(nullable=False, default=False)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    episode: Mapped[EpisodeCandidate] = relationship(back_populates="members")


__all__ = ["EpisodeBuild", "EpisodeCandidate", "EpisodeMember"]
