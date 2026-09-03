"""Aggregate reporting: HMIS 033b weekly and HMIS 105 monthly.

The forms are transcribed in ``mars.domain.hmis_elements``; this module is how
what they carry is stored.

Four ideas shape it.

**A blank cell is not a zero.** HMIS 033b instruction 7 requires a health unit
to report every week "whether there are cases or not", so a reported zero is a
statement the facility made and a blank is a statement it did not make. They
are stored differently and are never conflated - a facility reporting zero
malaria deaths and a facility that did not report are different facts about
that facility, and treating a blank as zero is how a reporting gap becomes an
apparent improvement.

**A correction does not overwrite.** A revised weekly report is a real event:
the original was already acted on. A new submission supersedes the old one and
the old one is kept, marked ``superseded``. Overwriting would leave the record
showing a district that never had the figure anyone reacted to.

**A reported figure and a derived figure are both kept.** MARS can compute the
same quantity from the encounters it holds. When the two disagree, neither is
corrected: the difference is the finding. Silently preferring one source is
how a data-quality problem becomes invisible, and preferring the *derived* one
would mean MARS reporting numbers no facility ever submitted.

**Nothing here is re-banded.** An aggregate arrives already summarised. MARS
stores the form's own age bands and never splits or merges them.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import CORE
from mars.domain.enums import (
    AgeBand,
    AggregateForm,
    AggregatePeriodType,
    AggregateSubmissionStatus,
    ReconciliationStatus,
    Sex,
    StockMetric,
)


class AggregateSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One facility's return of one form for one period.

    Identified by ``(facility, form, period_start, period_end, revision)``. The
    revision is part of the key because a corrected report is a new submission,
    not an edit: the figures a district acted on in week 14 must still be
    readable after week 14 is corrected.
    """

    __tablename__ = "aggregate_submission"
    __table_args__ = (
        UniqueConstraint(
            "facility_id",
            "form",
            "period_start",
            "period_end",
            "revision",
            name="uq_aggregate_submission_facility_form_period_revision",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("revision >= 1", name="revision_is_positive"),
        # A week is Monday to Sunday and a month is a calendar month, both
        # stated on the forms. Enforced so a "weekly" submission covering a
        # quarter cannot be stored and later summed with real weeks.
        CheckConstraint(
            "(period_type = 'week' AND period_end - period_start = 6) OR "
            "(period_type = 'month' AND period_end - period_start BETWEEN 27 AND 30)",
            name="period_length_matches_type",
        ),
        CheckConstraint(
            "(form = 'hmis_033b' AND period_type = 'week') OR "
            "(form = 'hmis_105' AND period_type = 'month')",
            name="form_matches_period_type",
        ),
        CheckConstraint(
            "period_type <> 'week' OR EXTRACT(ISODOW FROM period_start) = 1",
            name="week_starts_monday",
        ),
        CheckConstraint(
            "period_type <> 'month' OR ("
            "EXTRACT(DAY FROM period_start) = 1 AND "
            "period_end = (period_start + INTERVAL '1 month' - INTERVAL '1 day')::date)",
            name="month_is_calendar_month",
        ),
        # The same rule ``import_batch`` applies to its artefact checksum. The
        # model has to declare it or autogenerate would offer to drop it, and
        # dropping it would silently remove the protection that stops a
        # producer changing figures under an unchanged revision number.
        CheckConstraint("length(payload_checksum) = 64", name="payload_checksum_sha256"),
        Index("ix_aggregate_submission_facility_period", "facility_id", "period_start"),
        Index("ix_aggregate_submission_form_period", "form", "period_start"),
        Index("ix_aggregate_submission_status", "submission_status"),
        Index(
            "uq_aggregate_submission_one_accepted",
            "facility_id",
            "form",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=text("submission_status = 'accepted'"),
        ),
        {
            "schema": CORE,
            "comment": (
                "One facility's return of one HMIS form for one period. A "
                "correction is a new revision; the superseded one is kept."
            ),
        },
    )

    facility_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.facility.id", ondelete="RESTRICT"),
        nullable=False,
    )

    form: Mapped[AggregateForm] = mapped_column(
        pg_enum(AggregateForm, name="aggregate_form", schema=CORE), nullable=False
    )
    period_type: Mapped[AggregatePeriodType] = mapped_column(
        pg_enum(AggregatePeriodType, name="aggregate_period_type", schema=CORE), nullable=False
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    #: The form's own period label - 033b prints a week number, 105 a month.
    #: Stored as the facility wrote it, so a transcription can be checked
    #: against the paper without recomputing it.
    period_label_raw: Mapped[str | None] = mapped_column(String(32), nullable=True)

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: Named ``submission_status``, not ``status``: each lifecycle in the
    #: schema carries its own named column, so a reader never has to ask which
    #: status a generic one means.
    submission_status: Mapped[AggregateSubmissionStatus] = mapped_column(
        pg_enum(AggregateSubmissionStatus, name="aggregate_submission_status", schema=CORE),
        nullable=False,
        default=AggregateSubmissionStatus.RECEIVED,
    )

    #: The submission this one replaces, when it replaces one. Null for a first
    #: return. Kept as a link rather than a flag so the chain of corrections is
    #: readable in order.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.aggregate_submission.id",
            ondelete="SET NULL",
            name="fk_aggregate_submission_supersedes",
        ),
        nullable=True,
    )

    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The persistent import lifecycle record that loaded it.
    source_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{CORE}.import_batch.id", ondelete="SET NULL", name="fk_aggregate_batch"),
        nullable=True,
    )
    ingest_method_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Canonical SHA-256 of the inbound logical submission. A producer cannot
    #: silently change figures while reusing the same revision number.
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    received_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When the facility completed the form, when it says. 105 is due on the
    #: 7th; a submission completed long after its period is worth seeing.
    reported_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    #: Free-text from the form's comments box. Never parsed into a figure.
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    observations: Mapped[list[AggregateObservation]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    stock_observations: Mapped[list[CommodityStockObservation]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    laboratory_observations: Mapped[list[LaboratoryTestObservation]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    facility: Mapped[Any] = relationship("Facility")


class AggregateObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cell of one form.

    ``value`` is nullable and that is the whole point: null means the cell was
    blank, zero means the facility wrote a zero. 033b requires zero reporting,
    so the distinction carries real information about whether a facility
    reported at all.
    """

    __tablename__ = "aggregate_observation"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_submission_id",
            "element_code",
            "age_band",
            "sex",
            name="uq_aggregate_observation_submission_element_band_sex",
        ),
        # A count of people is never negative and never fractional. A form with
        # a negative case count has been transcribed wrongly.
        CheckConstraint("value IS NULL OR value >= 0", name="value_not_negative"),
        Index("ix_aggregate_observation_element", "element_code"),
        Index("ix_aggregate_observation_submission", "aggregate_submission_id"),
        {
            "schema": CORE,
            "comment": (
                "One cell of one form. A NULL value means the cell was blank; "
                "zero means the facility reported a zero. HMIS 033b requires "
                "zero reporting, so the two are different facts."
            ),
        },
    )

    aggregate_submission_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.aggregate_submission.id",
            ondelete="CASCADE",
            name="fk_aggregate_observation_submission",
        ),
        nullable=False,
    )

    #: The form's code where it prints one, or a MARS-assigned code where it
    #: does not. ``mars.domain.hmis_elements`` says which is which.
    element_code: Mapped[str] = mapped_column(String(48), nullable=False)

    age_band: Mapped[AgeBand] = mapped_column(
        pg_enum(AgeBand, name="age_band", schema=CORE),
        nullable=False,
        default=AgeBand.UNSPECIFIED,
    )
    sex: Mapped[Sex] = mapped_column(
        pg_enum(Sex, name="sex", schema=CORE), nullable=False, default=Sex.UNKNOWN
    )

    value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: What the paper actually said, when it was not a plain number - "-",
    #: "nil", an illegible mark. Kept so a transcription decision can be
    #: audited rather than argued about.
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    submission: Mapped[AggregateSubmission] = relationship(back_populates="observations")


class CommodityStockObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One commodity's stock position for one period.

    HMIS 105 section 6.1 prints four columns per commodity; 033b section 7
    prints a single balance. Both land here, with ``metric`` saying which
    measure a value is - so a weekly balance is never mistaken for a monthly
    consumption figure.

    ``days_out_of_stock`` is the surveillance-relevant one. The form defines
    out of stock as *none left in the health unit store*, and a testing decline
    that coincides with days out of stock has a commodity explanation rather
    than an epidemiological one.
    """

    __tablename__ = "commodity_stock_observation"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_submission_id",
            "commodity_code",
            "metric",
            name="uq_commodity_stock_submission_commodity_metric",
        ),
        CheckConstraint("value IS NULL OR value >= 0", name="value_not_negative"),
        # A month has no more than 31 days out of stock, and a week no more
        # than 7. Checked against the submission's own period length rather
        # than a constant, which is why it lives in the validator too.
        CheckConstraint(
            "metric <> 'days_out_of_stock' OR value IS NULL OR value <= 31",
            name="days_out_of_stock_within_a_month",
        ),
        Index("ix_commodity_stock_commodity", "commodity_code"),
        Index("ix_commodity_stock_submission", "aggregate_submission_id"),
        {"schema": CORE},
    )

    aggregate_submission_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.aggregate_submission.id",
            ondelete="CASCADE",
            name="fk_commodity_stock_submission",
        ),
        nullable=False,
    )

    commodity_code: Mapped[str] = mapped_column(String(48), nullable=False)
    metric: Mapped[StockMetric] = mapped_column(
        pg_enum(StockMetric, name="stock_metric", schema=CORE), nullable=False
    )

    #: Nullable for the same reason as an observation: blank is not zero, and
    #: "no days out of stock" is a very different claim from "not reported".
    value: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The form's printed unit of issue - tablet, vial, test, piece. Stored
    #: because a consumption figure without its unit is not a quantity.
    unit_of_issue: Mapped[str | None] = mapped_column(String(64), nullable=True)

    submission: Mapped[AggregateSubmission] = relationship(back_populates="stock_observations")


class LaboratoryTestObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One laboratory test row: number done and number positive.

    From HMIS 105 section 10.2.1. Kept apart from the OPD diagnosis block on
    purpose: the laboratory counts tests it performed and the OPD block counts
    patients it diagnosed, and where the two disagree, the disagreement is
    itself the finding. Merging them would destroy exactly the comparison the
    form makes possible.
    """

    __tablename__ = "laboratory_test_observation"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_submission_id",
            "test_code",
            name="uq_laboratory_test_submission_test",
        ),
        CheckConstraint("number_done IS NULL OR number_done >= 0", name="done_not_negative"),
        CheckConstraint(
            "number_positive IS NULL OR number_positive >= 0", name="positive_not_negative"
        ),
        # More positives than tests is arithmetically impossible, so it is a
        # transcription error rather than an unusual month.
        CheckConstraint(
            "number_done IS NULL OR number_positive IS NULL OR number_positive <= number_done",
            name="positive_not_above_done",
        ),
        Index("ix_laboratory_test_submission", "aggregate_submission_id"),
        Index("ix_laboratory_test_code", "test_code"),
        {"schema": CORE},
    )

    aggregate_submission_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.aggregate_submission.id",
            ondelete="CASCADE",
            name="fk_laboratory_test_submission",
        ),
        nullable=False,
    )

    test_code: Mapped[str] = mapped_column(String(48), nullable=False)
    number_done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number_positive: Mapped[int | None] = mapped_column(Integer, nullable=True)

    raw_done: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_positive: Mapped[str | None] = mapped_column(Text, nullable=True)

    submission: Mapped[AggregateSubmission] = relationship(back_populates="laboratory_observations")


class ReconciliationFinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reported figure set beside the same figure derived from encounters.

    Both numbers are kept and neither is corrected. A difference between what a
    facility reported and what its own register implies is a data-quality
    finding, and resolving it belongs to the district, not to MARS. Silently
    preferring the aggregate would hide the register's detail; silently
    preferring the derived figure would mean MARS publishing numbers no
    facility ever submitted.

    ``UNCOMPARABLE`` is a real outcome, not a failure: with no e-register data
    for that facility and period there is nothing to compare against, and
    saying so is more useful than reporting a difference of everything.
    """

    __tablename__ = "reconciliation_finding"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_submission_id",
            "element_code",
            "method_version",
            "input_checksum",
            "absolute_tolerance",
            name="uq_reconciliation_submission_element_method_input",
        ),
        CheckConstraint("length(input_checksum) = 64", name="input_checksum_is_sha256"),
        CheckConstraint("absolute_tolerance >= 0", name="tolerance_not_negative"),
        Index("ix_reconciliation_status", "reconciliation_status"),
        Index("ix_reconciliation_submission", "aggregate_submission_id"),
        {
            "schema": CORE,
            "comment": (
                "Reported against derived. Both values are kept and neither is "
                "corrected: the difference is the finding."
            ),
        },
    )

    aggregate_submission_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            f"{CORE}.aggregate_submission.id",
            ondelete="CASCADE",
            name="fk_reconciliation_submission",
        ),
        nullable=False,
    )

    element_code: Mapped[str] = mapped_column(String(48), nullable=False)
    reconciliation_status: Mapped[ReconciliationStatus] = mapped_column(
        pg_enum(ReconciliationStatus, name="reconciliation_status", schema=CORE), nullable=False
    )

    #: What the facility reported, summed over the disaggregation.
    reported_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What MARS computes from the encounters it holds for the same facility
    #: and period.
    derived_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: reported - derived. Stored rather than computed on read so a finding
    #: keeps its meaning after either side is corrected.
    difference: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How many encounters the derived figure was computed from. A derived
    #: figure from four encounters and one from four hundred deserve different
    #: attention, and a bare difference hides which is which.
    derived_denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: The comparison rule that produced this finding. Versioned, so a finding
    #: can be read against the rule in force when it was made.
    method_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Fingerprint of the official submission plus the encounter/test snapshot
    #: used for this comparison. A later import creates new evidence rather
    #: than rewriting the result a district previously reviewed.
    input_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    absolute_tolerance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Why the comparison could not be made, when it could not.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    submission: Mapped[AggregateSubmission] = relationship()


__all__ = [
    "AggregateObservation",
    "AggregateSubmission",
    "CommodityStockObservation",
    "LaboratoryTestObservation",
    "ReconciliationFinding",
]
