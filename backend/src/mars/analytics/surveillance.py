"""Testing, treatment and commodity surveillance engines.

Three engines, independently testable, sharing a provenance envelope and
nothing else. Each answers a different question and each has a different way of
being wrong:

**Testing** describes what a facility did with its tests. Its failure mode is
being read as a statement about disease - a fall in confirmed cases during a
fall in testing is a testing finding, and calling it an improvement is the
commonest way malaria surveillance misleads itself. Every testing result
therefore carries whatever commodity context overlapped its period.

**Treatment** describes what was prescribed. Its failure mode is being read as
what a patient received. Routine data records a prescription line; it does not
record dispensing, adherence, or drug quality.

**Commodity** restates what a facility reported about its stock. Its failure
mode is inventing a judgement: "prolonged", "repeated", "low" and "imminent"
are all thresholds, and a threshold with no approved rule behind it is an
engineer's opinion driving a supply decision. Only the reported fact -
zero on hand, days out of stock - is raised without configuration.

A commodity alert is never converted into an epidemiological signal here or
anywhere. Later signal work may cite one as context; the citation runs one way.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Select, false, func, select
from sqlalchemy.orm import Session

from mars.core.logging import get_logger
from mars.domain.aggregate import (
    AggregateSubmission,
    CommodityStockObservation,
)
from mars.domain.encounter import OpdEncounter, OpdEncounterPrescription, OpdEncounterTest
from mars.domain.enums import (
    AggregateSubmissionStatus,
    AlertSeverity,
    CommodityAlertKind,
    CommodityFactKind,
    GeographyGrain,
    IndicatorValueStatus,
    LifecycleStatus,
    MalariaTestMethod,
    MalariaTestResult,
    PeriodGrain,
    StockMetric,
    TestingMeasure,
    TreatmentMeasure,
)
from mars.domain.governance import ConfigurationKey, ConfigurationVersion
from mars.domain.organisation import Facility
from mars.domain.surveillance import (
    CommodityOperationalAlert,
    CommodityStockFact,
    TestingSurveillanceResult,
    TreatmentSurveillanceResult,
)

logger = get_logger(__name__)

#: Bumped when a change here could alter a figure for unchanged inputs.
ENGINE_VERSION = "1.0.0"

#: The governed configuration supplying commodity alert classification -
#: what counts as prolonged, repeated, low or imminent, and how each maps to a
#: severity. Registered by governance; **not** shipped with values.
COMMODITY_RULES_KEY = "commodity_alert_rules"

#: Antimalarial and diagnostic commodity codes MARS watches, from HMIS 105
#: section 6.1. Which commodities matter is a transcription fact, not a
#: threshold - these are the rows the form prints.
WATCHED_COMMODITIES: dict[str, str] = {
    "SS01": "Artemether/Lumefantrine 20/120mg",
    "SS02": "Artesunate 60mg",
    "SS24": "Sulfadoxine/ Pyrimethamine tablet 500/25mg",
    "SS34": "Malaria Rapid Diagnostic",
}

#: Commodities whose absence bears on testing, and whose absence bears on
#: treatment. Kept apart because the two contexts answer different questions.
TESTING_COMMODITIES = ("SS34",)
TREATMENT_COMMODITIES = ("SS01", "SS02", "SS24")


@dataclass(slots=True)
class Envelope:
    """The provenance every result carries."""

    period_start: date
    period_end: date
    period_grain: PeriodGrain
    source_cutoff: datetime
    geography_grain: GeographyGrain = GeographyGrain.FACILITY
    geography_unit_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None
    boundary_version_id: uuid.UUID | None = None
    configuration_version_id: uuid.UUID | None = None
    indicator_version_id: uuid.UUID | None = None
    method_version_id: uuid.UUID | None = None
    contributing_units: int | None = None
    expected_units: int | None = None

    def as_columns(self) -> dict[str, object]:
        return {
            "geography_grain": self.geography_grain,
            "geography_unit_id": self.geography_unit_id,
            "facility_id": self.facility_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "period_grain": self.period_grain,
            "indicator_version_id": self.indicator_version_id,
            "method_version_id": self.method_version_id,
            "configuration_version_id": self.configuration_version_id,
            "boundary_version_id": self.boundary_version_id,
            "source_cutoff": self.source_cutoff,
            "engine_version": ENGINE_VERSION,
            "computed_at": datetime.now(UTC),
            "contributing_units": self.contributing_units,
            "expected_units": self.expected_units,
        }


@dataclass(slots=True)
class SurveillanceReport:
    """What one engine run produced."""

    domain: str = ""
    results_written: int = 0
    results_unchanged: int = 0
    unavailable: int = 0
    facilities: int = 0
    alerts_raised: int = 0
    classifications_skipped: list[str] = field(default_factory=list)
    notes: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "results_written": self.results_written,
            "results_unchanged": self.results_unchanged,
            "unavailable": self.unavailable,
            "facilities": self.facilities,
            "alerts_raised": self.alerts_raised,
            "classifications_skipped": sorted(self.classifications_skipped),
            "notes": self.notes,
        }


def _fingerprint(**material: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {k: str(v) for k, v in sorted(material.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _proportion(
    numerator: int | None, denominator: int | None
) -> tuple[Decimal | None, IndicatorValueStatus]:
    """A rate, or an explicit statement that there is none.

    Zero and null denominators both yield no value. A facility that tested
    nobody has no positivity - not a positivity of zero.
    """
    if denominator is None or denominator == 0 or numerator is None:
        return None, IndicatorValueStatus.UNAVAILABLE_NO_DENOMINATOR
    return (
        (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")),
        IndicatorValueStatus.AVAILABLE,
    )


def _count(total: int | None) -> tuple[Decimal | None, IndicatorValueStatus]:
    if total is None:
        return None, IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA
    return Decimal(total), IndicatorValueStatus.AVAILABLE


class _EngineBase:
    """Shared plumbing: facilities, cutoffs, and encounter counting."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def facilities(self, district_id: uuid.UUID | None = None) -> list[Facility]:
        query = select(Facility).where(Facility.is_active.is_(True))
        if district_id is not None:
            query = query.where(Facility.district_geography_unit_id == district_id)
        return list(self._session.execute(query).scalars().all())

    def source_cutoff(self) -> datetime:
        encounter = self._session.execute(
            select(func.max(OpdEncounter.updated_at))
        ).scalar_one_or_none()
        submission = self._session.execute(
            select(func.max(AggregateSubmission.updated_at))
        ).scalar_one_or_none()
        candidates = [v for v in (encounter, submission) if v is not None]
        return max(candidates) if candidates else datetime.now(UTC)

    def _encounters(self, facility_id: uuid.UUID, start: date, end: date) -> Select[tuple[int]]:
        return (
            select(func.count(func.distinct(OpdEncounter.id)))
            .select_from(OpdEncounter)
            .outerjoin(OpdEncounterTest, OpdEncounterTest.opd_encounter_id == OpdEncounter.id)
            .where(
                OpdEncounter.facility_id == facility_id,
                OpdEncounter.encounter_date >= start,
                OpdEncounter.encounter_date <= end,
            )
        )

    def _scalar(self, query: Select[tuple[int]]) -> int:
        return int(self._session.execute(query).scalar_one())


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
class TestingSurveillanceEngine(_EngineBase):
    """Measures what a facility did with its tests."""

    def compute_facility(
        self,
        facility: Facility,
        period_start: date,
        period_end: date,
        *,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
        report: SurveillanceReport | None = None,
        previous_period: tuple[date, date] | None = None,
    ) -> SurveillanceReport:
        """Every testing measure for one facility and period."""
        report = report or SurveillanceReport(domain="testing")
        cutoff = self.source_cutoff()

        attendances = self._scalar(self._encounters(facility.id, period_start, period_end))
        tested = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.method != MalariaTestMethod.NOT_DONE
            )
        )
        rdt = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.method == MalariaTestMethod.RDT
            )
        )
        microscopy = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.method == MalariaTestMethod.MICROSCOPY
            )
        )
        positive = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.result == MalariaTestResult.POSITIVE
            )
        )
        negative = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.result == MalariaTestResult.NEGATIVE
            )
        )
        # A test was performed but its result is unknown. Different from "not
        # tested": something was attempted and the outcome is missing.
        missing_results = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.method != MalariaTestMethod.NOT_DONE,
                OpdEncounterTest.result == MalariaTestResult.UNKNOWN,
            )
        )
        untested = attendances - tested

        treated_ids = self._treated_encounter_ids(facility.id, period_start, period_end)
        negative_treated = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.result == MalariaTestResult.NEGATIVE,
                OpdEncounter.id.in_(treated_ids) if treated_ids else false(),
            )
        )
        untested_treated = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.method == MalariaTestMethod.NOT_DONE,
                OpdEncounter.id.in_(treated_ids) if treated_ids else false(),
            )
        )

        commodity_context = self._commodity_context(
            facility.id, period_start, period_end, TESTING_COMMODITIES
        )

        envelope = Envelope(
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            source_cutoff=cutoff,
            facility_id=facility.id,
            geography_grain=GeographyGrain.FACILITY,
        )

        measures: list[tuple[TestingMeasure, int | None, int | None]] = [
            (TestingMeasure.TESTING_COVERAGE, tested, attendances),
            (TestingMeasure.RDT_SHARE, rdt, tested),
            (TestingMeasure.MICROSCOPY_SHARE, microscopy, tested),
            (TestingMeasure.TEST_POSITIVITY, positive, tested),
            (TestingMeasure.NEGATIVE_CASES_TREATED, negative_treated, None),
            (TestingMeasure.UNTESTED_CASES_TREATED, untested_treated, None),
            (TestingMeasure.MISSING_RESULT_COUNT, missing_results, None),
        ]

        if previous_period is not None:
            previous_tested = self._scalar(
                self._encounters(facility.id, *previous_period).where(
                    OpdEncounterTest.method != MalariaTestMethod.NOT_DONE
                )
            )
            # Change relative to the previous period. Reported as a ratio with
            # its own denominator so a reader can see what it was measured
            # against; if the previous period had no tests there is no ratio,
            # which is different from "testing did not change".
            measures.append((TestingMeasure.TESTING_VOLUME_CHANGE, tested, previous_tested))

        for measure, numerator, denominator in measures:
            value, status = (
                _proportion(numerator, denominator)
                if denominator is not None
                else _count(numerator)
            )
            self._write_testing(
                measure=measure,
                envelope=envelope,
                numerator=numerator,
                denominator=denominator,
                value=value,
                status=status,
                missing_results=missing_results,
                untested=untested,
                commodity_context=commodity_context,
                report=report,
                negative_count=negative,
            )

        report.facilities += 1
        self._session.flush()
        return report

    def _treated_encounter_ids(
        self, facility_id: uuid.UUID, start: date, end: date
    ) -> list[uuid.UUID]:
        return list(
            self._session.execute(
                select(OpdEncounterPrescription.opd_encounter_id)
                .distinct()
                .join(
                    OpdEncounter,
                    OpdEncounter.id == OpdEncounterPrescription.opd_encounter_id,
                )
                .where(
                    OpdEncounter.facility_id == facility_id,
                    OpdEncounter.encounter_date >= start,
                    OpdEncounter.encounter_date <= end,
                    OpdEncounterPrescription.drug_name_normalised.is_not(None),
                )
            )
            .scalars()
            .all()
        )

    def _commodity_context(
        self, facility_id: uuid.UUID, start: date, end: date, codes: tuple[str, ...]
    ) -> dict[str, object] | None:
        """Reported stock conditions overlapping this period.

        Attached to every testing and treatment result so a decline is never
        read without its supply context. A fall in tests during an RDT
        stock-out has a commodity explanation, and a reader who cannot see that
        will reach for an epidemiological one.
        """
        facts = (
            self._session.execute(
                select(CommodityStockFact).where(
                    CommodityStockFact.facility_id == facility_id,
                    CommodityStockFact.commodity_code.in_(list(codes)),
                    CommodityStockFact.period_start <= end,
                    CommodityStockFact.period_end >= start,
                    CommodityStockFact.fact_kind != CommodityFactKind.STOCK_NOT_REPORTED,
                )
            )
            .scalars()
            .all()
        )
        if not facts:
            return None
        return {
            "stock_conditions": [
                {
                    "commodity": fact.commodity_code,
                    "kind": fact.fact_kind.value,
                    "days_out_of_stock": fact.days_out_of_stock,
                }
                for fact in facts
            ],
            "reading": (
                "A change in testing volume that overlaps a reported stock-out "
                "has a commodity explanation. It is not evidence about malaria "
                "transmission."
            ),
        }

    def _write_testing(
        self,
        *,
        measure: TestingMeasure,
        envelope: Envelope,
        numerator: int | None,
        denominator: int | None,
        value: Decimal | None,
        status: IndicatorValueStatus,
        missing_results: int,
        untested: int,
        commodity_context: dict[str, object] | None,
        report: SurveillanceReport,
        negative_count: int,
    ) -> None:
        fingerprint = _fingerprint(
            measure=measure.value,
            facility=envelope.facility_id,
            period=envelope.period_start,
            numerator=numerator,
            denominator=denominator,
            missing=missing_results,
            untested=untested,
            negative=negative_count,
        )
        existing = self._session.execute(
            select(TestingSurveillanceResult).where(
                TestingSurveillanceResult.measure == measure,
                TestingSurveillanceResult.facility_id == envelope.facility_id,
                TestingSurveillanceResult.period_start == envelope.period_start,
                TestingSurveillanceResult.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            report.results_unchanged += 1
            return

        self._session.add(
            TestingSurveillanceResult(
                measure=measure,
                numerator=numerator,
                denominator=denominator,
                value=value,
                value_status=status,
                missing_results=missing_results,
                untested_encounters=untested,
                commodity_context=commodity_context,
                input_fingerprint=fingerprint,
                quality_context={
                    "domain_limit": (
                        "Testing practice, not disease burden. A fall in "
                        "confirmed cases alongside a fall in testing is a "
                        "testing finding."
                    )
                },
                **envelope.as_columns(),
            )
        )
        report.results_written += 1
        if status is not IndicatorValueStatus.AVAILABLE:
            report.unavailable += 1


# ---------------------------------------------------------------------------
# Treatment
# ---------------------------------------------------------------------------
class TreatmentSurveillanceEngine(_EngineBase):
    """Measures what was prescribed, as the register records it."""

    def compute_facility(
        self,
        facility: Facility,
        period_start: date,
        period_end: date,
        *,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
        report: SurveillanceReport | None = None,
    ) -> SurveillanceReport:
        report = report or SurveillanceReport(domain="treatment")
        cutoff = self.source_cutoff()

        treated_ids = self._treated_ids(facility.id, period_start, period_end)
        confirmed = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.result == MalariaTestResult.POSITIVE
            )
        )
        confirmed_treated = self._scalar(
            self._encounters(facility.id, period_start, period_end).where(
                OpdEncounterTest.result == MalariaTestResult.POSITIVE,
                OpdEncounter.id.in_(treated_ids) if treated_ids else false(),
            )
        )
        treated_total = len(treated_ids)
        treated_without_confirmation = treated_total - confirmed_treated

        # An encounter with no prescription row at all. Reported separately
        # from "not treated": a facility that records nothing and a facility
        # that treated nobody are different facts about that facility.
        with_any_prescription = self._scalar(
            select(func.count(func.distinct(OpdEncounter.id)))
            .select_from(OpdEncounter)
            .join(
                OpdEncounterPrescription,
                OpdEncounterPrescription.opd_encounter_id == OpdEncounter.id,
            )
            .where(
                OpdEncounter.facility_id == facility.id,
                OpdEncounter.encounter_date >= period_start,
                OpdEncounter.encounter_date <= period_end,
            )
        )
        attendances = self._scalar(self._encounters(facility.id, period_start, period_end))
        missing_information = attendances - with_any_prescription

        commodity_context = TestingSurveillanceEngine(self._session)._commodity_context(
            facility.id, period_start, period_end, TREATMENT_COMMODITIES
        )

        envelope = Envelope(
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            source_cutoff=cutoff,
            facility_id=facility.id,
            geography_grain=GeographyGrain.FACILITY,
        )

        measures: list[tuple[TreatmentMeasure, int | None, int | None]] = [
            (TreatmentMeasure.CONFIRMED_TREATED, confirmed_treated, confirmed),
            (TreatmentMeasure.CONFIRMED_NOT_TREATED, confirmed - confirmed_treated, None),
            (
                TreatmentMeasure.TREATED_WITHOUT_CONFIRMATION,
                treated_without_confirmation,
                None,
            ),
            (TreatmentMeasure.MISSING_TREATMENT_INFORMATION, missing_information, None),
        ]

        for measure, numerator, denominator in measures:
            value, status = (
                _proportion(numerator, denominator)
                if denominator is not None
                else _count(numerator)
            )
            self._write_treatment(
                measure=measure,
                envelope=envelope,
                numerator=numerator,
                denominator=denominator,
                value=value,
                status=status,
                missing_information=missing_information,
                confirmed_without_treatment=confirmed - confirmed_treated,
                commodity_context=commodity_context,
                report=report,
            )

        report.facilities += 1
        self._session.flush()
        return report

    def _treated_ids(self, facility_id: uuid.UUID, start: date, end: date) -> list[uuid.UUID]:
        return TestingSurveillanceEngine(self._session)._treated_encounter_ids(
            facility_id, start, end
        )

    def _write_treatment(
        self,
        *,
        measure: TreatmentMeasure,
        envelope: Envelope,
        numerator: int | None,
        denominator: int | None,
        value: Decimal | None,
        status: IndicatorValueStatus,
        missing_information: int,
        confirmed_without_treatment: int,
        commodity_context: dict[str, object] | None,
        report: SurveillanceReport,
    ) -> None:
        fingerprint = _fingerprint(
            measure=measure.value,
            facility=envelope.facility_id,
            period=envelope.period_start,
            numerator=numerator,
            denominator=denominator,
            missing=missing_information,
        )
        existing = self._session.execute(
            select(TreatmentSurveillanceResult).where(
                TreatmentSurveillanceResult.measure == measure,
                TreatmentSurveillanceResult.facility_id == envelope.facility_id,
                TreatmentSurveillanceResult.period_start == envelope.period_start,
                TreatmentSurveillanceResult.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            report.results_unchanged += 1
            return

        self._session.add(
            TreatmentSurveillanceResult(
                measure=measure,
                numerator=numerator,
                denominator=denominator,
                value=value,
                value_status=status,
                missing_treatment_information=missing_information,
                confirmed_without_treatment=confirmed_without_treatment,
                commodity_context=commodity_context,
                input_fingerprint=fingerprint,
                quality_context={
                    "domain_limit": (
                        "Records what was prescribed. Routine data cannot "
                        "establish that a patient received, took or completed "
                        "a drug, or that a drug was of adequate quality."
                    )
                },
                **envelope.as_columns(),
            )
        )
        report.results_written += 1
        if status is not IndicatorValueStatus.AVAILABLE:
            report.unavailable += 1


# ---------------------------------------------------------------------------
# Commodity
# ---------------------------------------------------------------------------
class CommoditySurveillanceEngine(_EngineBase):
    """Restates reported stock conditions, and classifies only when told how."""

    def classification_rules(self) -> tuple[dict[str, object] | None, uuid.UUID | None]:
        """The approved commodity alert rules, or ``None``.

        ``None`` is the expected state before a programme approves them. The
        engine then records the reported facts and raises only the alert that
        restates one, skipping every classification and saying which.
        """
        version = (
            self._session.execute(
                select(ConfigurationVersion)
                .join(
                    ConfigurationKey,
                    ConfigurationKey.id == ConfigurationVersion.configuration_key_id,
                )
                .where(
                    ConfigurationKey.key == COMMODITY_RULES_KEY,
                    ConfigurationVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if version is None or not isinstance(version.value, dict) or not version.value:
            return None, None
        return version.value, version.id

    def compute_facility(
        self,
        facility: Facility,
        period_start: date,
        period_end: date,
        *,
        period_grain: PeriodGrain = PeriodGrain.MONTH,
        report: SurveillanceReport | None = None,
    ) -> SurveillanceReport:
        """Record reported stock conditions and raise what they support."""
        report = report or SurveillanceReport(domain="commodity")
        rules, configuration_version_id = self.classification_rules()
        cutoff = self.source_cutoff()

        if rules is None:
            report.notes = (
                f"No approved {COMMODITY_RULES_KEY}. Reported stock facts are "
                "recorded and a stock-out reported by the facility is raised, "
                "because both restate what the source said. Prolonged, "
                "repeated, low and imminent classifications require governed "
                "thresholds and are not produced."
            )
            report.classifications_skipped = [
                CommodityAlertKind.PROLONGED_STOCK_OUT.value,
                CommodityAlertKind.REPEATED_STOCK_OUT.value,
                CommodityAlertKind.MULTI_COMMODITY_STOCK_OUT.value,
                CommodityAlertKind.LOW_STOCK.value,
                CommodityAlertKind.IMMINENT_STOCK_OUT.value,
            ]

        envelope = Envelope(
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            source_cutoff=cutoff,
            facility_id=facility.id,
            geography_grain=GeographyGrain.FACILITY,
            configuration_version_id=configuration_version_id,
        )

        for code, label in WATCHED_COMMODITIES.items():
            self._record_commodity(
                facility=facility,
                code=code,
                label=label,
                envelope=envelope,
                report=report,
                configuration_version_id=configuration_version_id,
                cutoff=cutoff,
            )

        report.facilities += 1
        self._session.flush()
        logger.info("commodity_surveillance_finished", **report.as_dict())
        return report

    def _record_commodity(
        self,
        *,
        facility: Facility,
        code: str,
        label: str,
        envelope: Envelope,
        report: SurveillanceReport,
        configuration_version_id: uuid.UUID | None,
        cutoff: datetime,
    ) -> None:
        submission_ids = list(
            self._session.execute(
                select(AggregateSubmission.id).where(
                    AggregateSubmission.facility_id == facility.id,
                    AggregateSubmission.period_start >= envelope.period_start,
                    AggregateSubmission.period_end <= envelope.period_end,
                    AggregateSubmission.submission_status == AggregateSubmissionStatus.ACCEPTED,
                )
            )
            .scalars()
            .all()
        )
        if not submission_ids:
            return

        rows = self._session.execute(
            select(
                CommodityStockObservation.metric,
                CommodityStockObservation.value,
                CommodityStockObservation.unit_of_issue,
                CommodityStockObservation.aggregate_submission_id,
            ).where(
                CommodityStockObservation.aggregate_submission_id.in_(submission_ids),
                CommodityStockObservation.commodity_code == code,
            )
        ).all()

        if not rows:
            return

        reported = {
            metric: (value, unit, submission_id)
            for metric, value, unit, submission_id in rows
            if value is not None
        }
        unit = next((r[1] for r in rows if r[1]), None)
        submission_id = next((r[3] for r in rows), None)

        if not reported:
            # Every cell blank. A reporting gap, not a stock-out - and the
            # difference matters most exactly when supply has failed.
            self._write_fact(
                kind=CommodityFactKind.STOCK_NOT_REPORTED,
                code=code,
                label=label,
                unit=unit,
                envelope=envelope,
                report=report,
                submission_id=submission_id,
                value=None,
                status=IndicatorValueStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                notes=(
                    "Every stock cell for this commodity was blank. The facility "
                    "did not report, which is not the same as reporting none."
                ),
            )
            return

        days_value = reported.get(StockMetric.DAYS_OUT_OF_STOCK)
        balance_value = reported.get(StockMetric.STOCK_ON_HAND)
        consumed_value = reported.get(StockMetric.QUANTITY_CONSUMED)

        stock_out_facts: list[CommodityStockFact] = []

        if days_value is not None and days_value[0] is not None and days_value[0] > 0:
            fact = self._write_fact(
                kind=CommodityFactKind.DAYS_OUT_OF_STOCK_REPORTED,
                code=code,
                label=label,
                unit=unit,
                envelope=envelope,
                report=report,
                submission_id=days_value[2],
                value=Decimal(days_value[0]),
                status=IndicatorValueStatus.AVAILABLE,
                days_out_of_stock=int(days_value[0]),
                quantity_consumed=consumed_value[0] if consumed_value else None,
            )
            if fact is not None:
                stock_out_facts.append(fact)

        if balance_value is not None and balance_value[0] is not None and balance_value[0] == 0:
            fact = self._write_fact(
                kind=CommodityFactKind.STOCK_ON_HAND_ZERO,
                code=code,
                label=label,
                unit=unit,
                envelope=envelope,
                report=report,
                submission_id=balance_value[2],
                value=Decimal(0),
                status=IndicatorValueStatus.AVAILABLE,
                stock_on_hand=Decimal(0),
                quantity_consumed=consumed_value[0] if consumed_value else None,
            )
            if fact is not None:
                stock_out_facts.append(fact)

        if stock_out_facts:
            self._raise_reported_stock_out(
                facility=facility,
                code=code,
                label=label,
                facts=stock_out_facts,
                envelope=envelope,
                report=report,
                configuration_version_id=configuration_version_id,
                cutoff=cutoff,
            )

    def _write_fact(
        self,
        *,
        kind: CommodityFactKind,
        code: str,
        label: str,
        unit: str | None,
        envelope: Envelope,
        report: SurveillanceReport,
        submission_id: uuid.UUID | None,
        value: Decimal | None,
        status: IndicatorValueStatus,
        days_out_of_stock: int | None = None,
        stock_on_hand: Decimal | None = None,
        quantity_consumed: Decimal | None = None,
        notes: str | None = None,
    ) -> CommodityStockFact | None:
        fingerprint = _fingerprint(
            kind=kind.value,
            code=code,
            facility=envelope.facility_id,
            period=envelope.period_start,
            days=days_out_of_stock,
            balance=stock_on_hand,
        )
        existing = self._session.execute(
            select(CommodityStockFact).where(
                CommodityStockFact.fact_kind == kind,
                CommodityStockFact.commodity_code == code,
                CommodityStockFact.facility_id == envelope.facility_id,
                CommodityStockFact.period_start == envelope.period_start,
                CommodityStockFact.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            report.results_unchanged += 1
            return existing

        fact = CommodityStockFact(
            fact_kind=kind,
            commodity_code=code,
            commodity_label=label,
            unit_of_issue=unit,
            stock_on_hand=stock_on_hand,
            days_out_of_stock=days_out_of_stock,
            quantity_consumed=quantity_consumed,
            value=value,
            value_status=status,
            aggregate_submission_id=submission_id,
            input_fingerprint=fingerprint,
            notes=notes,
            quality_context={
                "domain_limit": (
                    "A reported stock condition. Operational, not "
                    "epidemiological: it says nothing about transmission, "
                    "treatment response or resistance."
                )
            },
            **envelope.as_columns(),
        )
        self._session.add(fact)
        self._session.flush()
        report.results_written += 1
        if status is not IndicatorValueStatus.AVAILABLE:
            report.unavailable += 1
        return fact

    def _raise_reported_stock_out(
        self,
        *,
        facility: Facility,
        code: str,
        label: str,
        facts: list[CommodityStockFact],
        envelope: Envelope,
        report: SurveillanceReport,
        configuration_version_id: uuid.UUID | None,
        cutoff: datetime,
    ) -> None:
        """Raise the one alert that needs no threshold.

        It restates what the facility itself reported: there was none in the
        store. Severity stays ``unclassified`` without governed rules, because
        how urgent a stock-out is depends on resupply times and buffer stocks
        that MARS does not know.
        """
        fingerprint = _fingerprint(
            kind=CommodityAlertKind.STOCK_OUT_REPORTED.value,
            code=code,
            facility=facility.id,
            period=envelope.period_start,
            facts=sorted(str(fact.id) for fact in facts),
        )
        existing = self._session.execute(
            select(CommodityOperationalAlert).where(
                CommodityOperationalAlert.alert_kind == CommodityAlertKind.STOCK_OUT_REPORTED,
                CommodityOperationalAlert.commodity_code == code,
                CommodityOperationalAlert.facility_id == facility.id,
                CommodityOperationalAlert.period_start == envelope.period_start,
                CommodityOperationalAlert.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        days = next((f.days_out_of_stock for f in facts if f.days_out_of_stock), None)
        detail = f" for {days} day(s)" if days else ""

        self._session.add(
            CommodityOperationalAlert(
                alert_kind=CommodityAlertKind.STOCK_OUT_REPORTED,
                commodity_code=code,
                commodity_label=label,
                facility_id=facility.id,
                district_geography_unit_id=facility.district_geography_unit_id,
                period_start=envelope.period_start,
                period_end=envelope.period_end,
                # Unclassified unless a governed rule says otherwise. How
                # urgent a stock-out is depends on resupply time and buffer
                # stock, which MARS does not know.
                severity=AlertSeverity.UNCLASSIFIED,
                supporting_fact_ids={"facts": [str(fact.id) for fact in facts]},
                statement=(
                    f"{label} was reported out of stock{detail}. This is a "
                    "supply-chain observation reported by the facility. It is "
                    "not a finding about malaria transmission, treatment "
                    "response or antimalarial resistance."
                ),
                configuration_version_id=configuration_version_id,
                input_fingerprint=fingerprint,
                source_cutoff=cutoff,
                engine_version=ENGINE_VERSION,
                raised_at=datetime.now(UTC),
            )
        )
        report.alerts_raised += 1


__all__ = [
    "COMMODITY_RULES_KEY",
    "ENGINE_VERSION",
    "TESTING_COMMODITIES",
    "TREATMENT_COMMODITIES",
    "WATCHED_COMMODITIES",
    "CommoditySurveillanceEngine",
    "Envelope",
    "SurveillanceReport",
    "TestingSurveillanceEngine",
    "TreatmentSurveillanceEngine",
]
