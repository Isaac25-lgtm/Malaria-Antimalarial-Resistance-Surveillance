"""Testing, treatment and commodity surveillance results.

Three domains, three tables, one shared envelope.

The envelope - identity, method and configuration versions, source lineage and
cutoff, boundary version, input fingerprint, generation time, completeness - is
identical everywhere because those questions are identical everywhere: *which
rules made this, from what, as of when, and how good were the inputs.*

The **evidence** is not shared, and deliberately so. A testing result carries a
tested denominator; a treatment result carries a confirmed-case denominator and
a count of missing prescriptions; a commodity fact carries days and a unit of
issue. Forcing all three into one table would mean a row of mostly-null columns
where a reader cannot tell "this measure has no denominator" from "this
measure's denominator was not recorded" - which is exactly the distinction the
rest of MARS spends its effort preserving.

**Commodity alerts are structurally separate from epidemiological signals.** A
stock-out is an operational fact about a supply chain. It says nothing about
transmission, treatment response or resistance. Later signal work may cite one
as context; nothing may convert it into a treatment-response finding, and
keeping it in its own table with its own vocabulary is what makes that
conversion a visible act rather than a quiet one.
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
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from mars.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from mars.db.schemas import ANALYTICS, GOVERNANCE
from mars.domain.enums import (
    AlertSeverity,
    CommodityAlertKind,
    CommodityFactKind,
    GeographyGrain,
    IndicatorValueStatus,
    PeriodGrain,
    TestingMeasure,
    TreatmentMeasure,
)


class AnalyticalResultEnvelope:
    """Provenance every derived result carries, whatever it measures.

    A mixin rather than a base table. The three result types have genuinely
    different evidence and belong in different tables; what they share is the
    question "where did this come from", and sharing the *answer's shape*
    without sharing storage is what keeps both properties.
    """

    # -- Where it applies --------------------------------------------------
    @declared_attr
    def geography_grain(cls) -> Mapped[GeographyGrain]:  # noqa: N805
        return mapped_column(
            pg_enum(GeographyGrain, name="geography_grain", schema=GOVERNANCE), nullable=False
        )

    @declared_attr
    def geography_unit_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(PGUUID(as_uuid=True), nullable=True)

    @declared_attr
    def facility_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(PGUUID(as_uuid=True), nullable=True)

    @declared_attr
    def period_start(cls) -> Mapped[datetime.date]:  # noqa: N805
        return mapped_column(Date, nullable=False)

    @declared_attr
    def period_end(cls) -> Mapped[datetime.date]:  # noqa: N805
        return mapped_column(Date, nullable=False)

    @declared_attr
    def period_grain(cls) -> Mapped[PeriodGrain]:  # noqa: N805
        return mapped_column(
            pg_enum(PeriodGrain, name="period_grain", schema=GOVERNANCE), nullable=False
        )

    # -- Which rules made it ----------------------------------------------
    @declared_attr
    def indicator_version_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        """The indicator definition this figure implements, when it implements
        one. Null for a measure computed directly rather than through the
        registry."""
        return mapped_column(PGUUID(as_uuid=True), nullable=True)

    @declared_attr
    def method_version_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(PGUUID(as_uuid=True), nullable=True)

    @declared_attr
    def configuration_version_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        """The governed configuration in force. Null means the calculation
        consulted none - honest, rather than implying a parameter was used."""
        return mapped_column(PGUUID(as_uuid=True), nullable=True)

    @declared_attr
    def boundary_version_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(PGUUID(as_uuid=True), nullable=True)

    # -- What it was computed from ----------------------------------------
    @declared_attr
    def input_fingerprint(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(64), nullable=False)

    @declared_attr
    def source_cutoff(cls) -> Mapped[datetime.datetime]:  # noqa: N805
        return mapped_column(DateTime(timezone=True), nullable=False)

    @declared_attr
    def engine_version(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(32), nullable=False)

    @declared_attr
    def computed_at(cls) -> Mapped[datetime.datetime]:  # noqa: N805
        return mapped_column(DateTime(timezone=True), nullable=False)

    # -- How good the inputs were -----------------------------------------
    @declared_attr
    def contributing_units(cls) -> Mapped[int | None]:  # noqa: N805
        return mapped_column(Integer, nullable=True)

    @declared_attr
    def expected_units(cls) -> Mapped[int | None]:  # noqa: N805
        return mapped_column(Integer, nullable=True)

    @declared_attr
    def quality_context(cls) -> Mapped[dict[str, Any] | None]:  # noqa: N805
        """Confounders and caveats a reader needs. Never optional in practice:
        a testing figure without its commodity context invites the wrong
        conclusion."""
        return mapped_column(JSONB, nullable=True)


def _envelope_constraints(prefix: str) -> tuple[Any, ...]:
    """Constraints every result table shares.

    Written once so the three tables cannot drift apart on the rules that
    matter - particularly the one saying a value exists only when its status
    says it does.
    """
    return (
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        CheckConstraint(
            "(value IS NOT NULL AND value_status = 'available') OR "
            "(value IS NULL AND value_status <> 'available')",
            name="value_present_iff_available",
        ),
        CheckConstraint(
            "(geography_grain = 'facility' AND facility_id IS NOT NULL) OR "
            "(geography_grain <> 'facility' AND facility_id IS NULL)",
            name="facility_id_matches_grain",
        ),
        Index(f"ix_{prefix}_period", "period_start", "period_end"),
        Index(f"ix_{prefix}_facility", "facility_id", "period_start"),
        Index(f"ix_{prefix}_geography", "geography_unit_id", "period_start"),
    )


class TestingSurveillanceResult(
    AnalyticalResultEnvelope, UUIDPrimaryKeyMixin, TimestampMixin, Base
):
    """One testing-practice measure.

    Testing practice, not disease. A fall in confirmed cases during a fall in
    testing is a testing finding; reading it as an improvement is the single
    commonest way malaria surveillance misleads itself.
    """

    __tablename__ = "testing_surveillance_result"
    __table_args__ = (
        UniqueConstraint(
            "measure",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_testing_result_measure_scope_period_input",
        ),
        CheckConstraint("numerator IS NULL OR numerator >= 0", name="numerator_not_negative"),
        CheckConstraint("denominator IS NULL OR denominator >= 0", name="denominator_not_negative"),
        *_envelope_constraints("testing_result"),
        Index("ix_testing_result_measure", "measure", "period_start"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Testing-practice measures. Describes what a facility did with "
                "its tests, never how much malaria there is."
            ),
        },
    )

    measure: Mapped[TestingMeasure] = mapped_column(
        pg_enum(TestingMeasure, name="testing_measure", schema=ANALYTICS), nullable=False
    )

    numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The tested population for a rate. Never attendance: a denominator
    #: inflated by untested attendances understates positivity everywhere, and
    #: worst where testing has broken down.
    denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
        default=IndicatorValueStatus.AVAILABLE,
    )

    #: Encounters with a test recorded but no readable result. Distinct from
    #: "not tested": something was attempted and its outcome is missing.
    missing_results: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Encounters recording no test at all.
    untested_encounters: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Whether a commodity fact overlapped this period. Carried on the row so a
    #: testing decline is never read without its supply context.
    commodity_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class TreatmentSurveillanceResult(
    AnalyticalResultEnvelope, UUIDPrimaryKeyMixin, TimestampMixin, Base
):
    """One treatment-practice measure.

    Prescribing as the register records it. Nothing here establishes that a
    patient received, took or completed a drug, and no consumer may present it
    as if it did.
    """

    __tablename__ = "treatment_surveillance_result"
    __table_args__ = (
        UniqueConstraint(
            "measure",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_treatment_result_measure_scope_period_input",
        ),
        CheckConstraint("numerator IS NULL OR numerator >= 0", name="numerator_not_negative"),
        CheckConstraint("denominator IS NULL OR denominator >= 0", name="denominator_not_negative"),
        *_envelope_constraints("treatment_result"),
        Index("ix_treatment_result_measure", "measure", "period_start"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Treatment-practice measures. Records what was prescribed, "
                "never what a patient received or took."
            ),
        },
    )

    measure: Mapped[TreatmentMeasure] = mapped_column(
        pg_enum(TreatmentMeasure, name="treatment_measure", schema=ANALYTICS), nullable=False
    )

    numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
        default=IndicatorValueStatus.AVAILABLE,
    )

    #: Encounters where the prescription field was blank. Reported separately
    #: from "not treated": a facility that records nothing and a facility that
    #: treated nobody are different facts about that facility.
    missing_treatment_information: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Positive results with no antimalarial recorded. The plainest treatment
    #: gap, and the ordinary explanation for a later repeat positive.
    confirmed_without_treatment: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Whether an antimalarial stock-out overlapped this period. A fall in
    #: recorded treatment during an AL stock-out is a supply finding.
    commodity_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommodityStockFact(AnalyticalResultEnvelope, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A commodity condition the source stated outright.

    Every row restates something a facility reported: a balance of zero, a
    count of days with none in the store, or the absence of any report at all.
    No statistical judgement is involved, which is why these exist before any
    configuration is approved.

    What is deliberately absent: prolonged, repeated, low, imminent. Each needs
    a governed threshold, and each would otherwise be an engineer's guess
    driving a real supply decision.
    """

    __tablename__ = "commodity_stock_fact"
    __table_args__ = (
        UniqueConstraint(
            "fact_kind",
            "commodity_code",
            "geography_grain",
            "geography_unit_id",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_commodity_fact_kind_code_scope_period_input",
        ),
        CheckConstraint(
            "days_out_of_stock IS NULL OR days_out_of_stock >= 0",
            name="days_out_of_stock_not_negative",
        ),
        CheckConstraint(
            "stock_on_hand IS NULL OR stock_on_hand >= 0", name="stock_on_hand_not_negative"
        ),
        # A days-out-of-stock fact must actually carry days, and a zero-balance
        # fact must carry a balance. Without this a fact could assert a
        # condition it has no evidence for.
        #
        # The IS NOT NULL halves are not redundant. A check constraint passes
        # when it evaluates to NULL, so ``days_out_of_stock > 0`` against a
        # null column would admit exactly the row this is meant to refuse: a
        # stock-out asserted with nothing behind it.
        CheckConstraint(
            "(fact_kind <> 'days_out_of_stock_reported' OR "
            "(days_out_of_stock IS NOT NULL AND days_out_of_stock > 0)) AND "
            "(fact_kind <> 'stock_on_hand_zero' OR "
            "(stock_on_hand IS NOT NULL AND stock_on_hand = 0))",
            name="fact_carries_its_evidence",
        ),
        *_envelope_constraints("commodity_fact"),
        Index("ix_commodity_fact_commodity", "commodity_code", "period_start"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Commodity conditions the source stated outright. No "
                "statistical judgement: prolonged, repeated, low and imminent "
                "require governed thresholds and are not here."
            ),
        },
    )

    fact_kind: Mapped[CommodityFactKind] = mapped_column(
        pg_enum(CommodityFactKind, name="commodity_fact_kind", schema=ANALYTICS), nullable=False
    )
    commodity_code: Mapped[str] = mapped_column(String(48), nullable=False)
    commodity_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: The form's printed unit. A quantity without its unit is not a quantity.
    unit_of_issue: Mapped[str | None] = mapped_column(String(64), nullable=True)

    stock_on_hand: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    days_out_of_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity_consumed: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    #: The envelope's value/status pair, so the shared constraint applies. For
    #: a fact the value is the reported quantity the fact is about.
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_status: Mapped[IndicatorValueStatus] = mapped_column(
        pg_enum(IndicatorValueStatus, name="indicator_value_status", schema=ANALYTICS),
        nullable=False,
        default=IndicatorValueStatus.AVAILABLE,
    )

    #: Which submission reported it, so a fact traces to a form.
    aggregate_submission_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "mars_core.aggregate_submission.id",
            ondelete="SET NULL",
            name="fk_commodity_fact_submission",
        ),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommodityOperationalAlert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A supply-chain alert, kept apart from every epidemiological signal.

    **This table exists to be separate.** A stock-out needs a storekeeper and a
    district pharmacist; a treatment-response signal needs an epidemiologist
    and a laboratory. Putting them in one table with a ``kind`` column would
    make converting one into the other a one-line change, and that conversion
    is exactly the claim MARS must never make silently.

    Later signal work may **cite** an alert as supporting context. It may not
    rescore or relabel it, and the citation direction - signal references
    alert, never the reverse - is what keeps the asymmetry visible.

    ``STOCK_OUT_REPORTED`` is the only kind raisable without configuration,
    because it restates a fact the facility reported. Every other kind requires
    an approved rule and stays absent until one exists.
    """

    __tablename__ = "commodity_operational_alert"
    __table_args__ = (
        UniqueConstraint(
            "alert_kind",
            "commodity_code",
            "facility_id",
            "period_start",
            "input_fingerprint",
            name="uq_commodity_alert_kind_code_facility_period_input",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("length(input_fingerprint) = 64", name="fingerprint_is_sha256"),
        # Only the reported-fact alert may exist without a governed rule.
        # Everything else is a judgement, and a judgement with no rule behind it
        # is an engineer's opinion driving a supply decision.
        CheckConstraint(
            "alert_kind = 'stock_out_reported' OR configuration_version_id IS NOT NULL",
            # Shortened by hand: the convention would generate a
            # 70-character identifier, above PostgreSQL's 63-character limit.
            name="classified_alerts_need_config",
        ),
        # Severity likewise. UNCLASSIFIED is what MARS assigns on its own.
        CheckConstraint(
            "severity = 'unclassified' OR configuration_version_id IS NOT NULL",
            name="severity_requires_configuration",
        ),
        Index("ix_commodity_alert_facility", "facility_id", "period_start"),
        Index("ix_commodity_alert_kind", "alert_kind", "period_start"),
        {
            "schema": ANALYTICS,
            "comment": (
                "Operational supply-chain alerts. Deliberately not signals: a "
                "stock-out says nothing about transmission, treatment response "
                "or resistance, and nothing may convert one into a finding "
                "about the parasite."
            ),
        },
    )

    alert_kind: Mapped[CommodityAlertKind] = mapped_column(
        pg_enum(CommodityAlertKind, name="commodity_alert_kind", schema=ANALYTICS), nullable=False
    )
    commodity_code: Mapped[str] = mapped_column(String(48), nullable=False)
    commodity_label: Mapped[str | None] = mapped_column(String(160), nullable=True)

    facility_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("mars_core.facility.id", ondelete="CASCADE", name="fk_commodity_alert_facility"),
        nullable=False,
    )
    #: Denormalised for scoping without a join.
    district_geography_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    severity: Mapped[AlertSeverity] = mapped_column(
        pg_enum(AlertSeverity, name="alert_severity", schema=ANALYTICS),
        nullable=False,
        default=AlertSeverity.UNCLASSIFIED,
    )

    #: The facts this alert restates. A list of fact ids, so an alert never
    #: asserts anything its evidence does not.
    supporting_fact_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Plain operational language. Never epidemiological.
    statement: Mapped[str] = mapped_column(Text, nullable=False)

    configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    method_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cutoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    raised_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "AnalyticalResultEnvelope",
    "CommodityOperationalAlert",
    "CommodityStockFact",
    "TestingSurveillanceResult",
    "TreatmentSurveillanceResult",
]
