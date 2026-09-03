"""Governed, deterministic signal generation — Prompt 21.

The engine does not contain a score, threshold, weight, or priority default.
It combines only evidence sources an active rule names and records both
supporting and counter-evidence. Commodity operational alerts remain their own
records; a signal may cite one as context but cannot relabel or rescore it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.domain.aggregate import AggregateSubmission, ReconciliationFinding
from mars.domain.anomaly import TemporalAnomalyResult
from mars.domain.clustering import SpatialClusterResult
from mars.domain.enums import (
    AnomalyOutcome,
    BaselineSeriesKind,
    ClusterOutcome,
    HotspotOutcome,
    IndicatorValueStatus,
    LifecycleStatus,
    MethodKind,
    ReconciliationStatus,
    RecurrenceScopeKind,
    SignalEvidenceKind,
    SignalEvidenceRole,
    SignalGenerationStatus,
    SignalPriority,
    SignalStatus,
    SignalType,
)
from mars.domain.governance import MethodDefinition, MethodVersion
from mars.domain.recurrence import RecurrenceResult
from mars.domain.signal import SignalEvidence, SignalGenerationRun, SurveillanceSignal
from mars.domain.spatial import HotspotResult
from mars.domain.surveillance import CommodityOperationalAlert

ENGINE_VERSION = "1.0.0"
SIGNAL_METHOD_CODE = "signal_prioritisation"
INTERPRETATION_LIMIT = (
    "This routine-data signal identifies a pattern requiring investigation. "
    "It does not confirm treatment failure, recrudescence, reinfection, or resistance."
)


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    kind: SignalEvidenceKind
    source_table: str
    source_record_id: uuid.UUID
    source_fingerprint: str
    geography_unit_id: uuid.UUID | None
    facility_id: uuid.UUID | None
    period_start: date
    period_end: date
    source_cutoff: datetime
    summary: str
    facts: dict[str, Any]
    quality_context: dict[str, Any] = field(default_factory=dict)
    role: SignalEvidenceRole = SignalEvidenceRole.SUPPORTING


@dataclass(frozen=True, slots=True)
class PriorityBand:
    priority: SignalPriority
    minimum_score: Decimal


@dataclass(frozen=True, slots=True)
class SignalRule:
    code: str
    signal_type: SignalType
    source_kinds: frozenset[SignalEvidenceKind]
    minimum_evidence: int
    minimum_score: Decimal
    weights: dict[SignalEvidenceKind, Decimal]
    priority_bands: tuple[PriorityBand, ...]
    recommended_action_codes: tuple[str, ...]


@dataclass(slots=True)
class SignalReport:
    run_id: uuid.UUID
    status: SignalGenerationStatus
    candidates_examined: int = 0
    signals_created: int = 0
    signals_unchanged: int = 0
    signals_superseded: int = 0
    missing_configuration: list[str] = field(default_factory=list)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SignalEngine:
    def __init__(self, session: Session) -> None:
        self._session = session

    def rules(self) -> tuple[uuid.UUID | None, tuple[SignalRule, ...], list[str]]:
        version = (
            self._session.execute(
                select(MethodVersion)
                .join(MethodDefinition)
                .where(
                    MethodDefinition.code == SIGNAL_METHOD_CODE,
                    MethodDefinition.kind == MethodKind.SIGNAL_RULE,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .first()
        )
        if version is None:
            return None, (), [f"method:{SIGNAL_METHOD_CODE}"]
        parameters = version.parameters if isinstance(version.parameters, dict) else {}
        raw_rules = parameters.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            return None, (), ["rules"]
        parsed: list[SignalRule] = []
        errors: list[str] = []
        for index, raw in enumerate(raw_rules):
            try:
                parsed.append(self._parse_rule(raw))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"rules[{index}]:{exc}")
        if errors:
            return None, (), errors
        codes = [rule.code for rule in parsed]
        if len(codes) != len(set(codes)):
            return None, (), ["rules:duplicate_code"]
        return version.id, tuple(parsed), []

    def _parse_rule(self, raw: object) -> SignalRule:
        if not isinstance(raw, dict):
            raise TypeError("must be an object")
        code = str(raw["code"]).strip()
        if not code:
            raise ValueError("code is empty")
        signal_type = SignalType(raw["signal_type"])
        raw_source_kinds = raw["source_kinds"]
        if not isinstance(raw_source_kinds, list):
            raise TypeError("source_kinds must be a list")
        source_kinds = frozenset(SignalEvidenceKind(value) for value in raw_source_kinds)
        if not source_kinds:
            raise ValueError("source_kinds is empty")
        if source_kinds == {SignalEvidenceKind.COMMODITY_ALERT}:
            raise ValueError("commodity_alert is context only")
        raw_minimum_evidence = raw["minimum_evidence"]
        if isinstance(raw_minimum_evidence, bool):
            raise ValueError("minimum_evidence must be an integer")
        minimum_evidence = int(raw_minimum_evidence)
        minimum_score = _decimal(raw["minimum_score"])
        if minimum_evidence < 1:
            raise ValueError("minimum_evidence must be positive")
        if minimum_score < 0:
            raise ValueError("minimum_score must not be negative")
        weights_raw = raw.get("weights")
        if not isinstance(weights_raw, dict):
            raise TypeError("weights must be an object")
        weights = {kind: _decimal(weights_raw[kind.value]) for kind in source_kinds}
        if any(weight < 0 for weight in weights.values()):
            raise ValueError("weights must not be negative")
        bands_raw = raw.get("priority_bands", [])
        if not isinstance(bands_raw, list):
            raise TypeError("priority_bands must be a list")
        bands = tuple(
            sorted(
                (
                    PriorityBand(SignalPriority(item["priority"]), _decimal(item["minimum_score"]))
                    for item in bands_raw
                ),
                key=lambda item: item.minimum_score,
            )
        )
        if any(item.minimum_score < 0 for item in bands):
            raise ValueError("priority band scores must not be negative")
        if len({item.minimum_score for item in bands}) != len(bands):
            raise ValueError("priority band scores must be unique")
        priority_rank = {
            SignalPriority.UNCLASSIFIED: 0,
            SignalPriority.INFORMATIONAL: 1,
            SignalPriority.ATTENTION: 2,
            SignalPriority.HIGH: 3,
            SignalPriority.URGENT: 4,
        }
        ranks = [priority_rank[item.priority] for item in bands]
        if any(current <= previous for previous, current in pairwise(ranks)):
            raise ValueError("priority must increase with score")
        actions_raw = raw.get("recommended_action_codes", [])
        if not isinstance(actions_raw, list) or any(not isinstance(v, str) for v in actions_raw):
            raise TypeError("recommended_action_codes must be a list of strings")
        actions = tuple(dict.fromkeys(value.strip() for value in actions_raw if value.strip()))
        return SignalRule(
            code,
            signal_type,
            source_kinds,
            minimum_evidence,
            minimum_score,
            weights,
            bands,
            actions,
        )

    def collect(self, period_start: date, period_end: date) -> list[EvidenceCandidate]:
        """Collect only already-judged upstream evidence for this period."""
        candidates: list[EvidenceCandidate] = []
        anomalies = self._session.execute(
            select(TemporalAnomalyResult).where(
                TemporalAnomalyResult.period_start >= period_start,
                TemporalAnomalyResult.period_end <= period_end,
                TemporalAnomalyResult.outcome.in_(
                    [AnomalyOutcome.FLAGGED, AnomalyOutcome.NOT_FLAGGED]
                ),
            )
        ).scalars()
        for anomaly in anomalies:
            kind = {
                BaselineSeriesKind.INDICATOR: SignalEvidenceKind.TEMPORAL_ANOMALY,
                BaselineSeriesKind.TESTING_MEASURE: SignalEvidenceKind.TESTING,
                BaselineSeriesKind.TREATMENT_MEASURE: SignalEvidenceKind.TREATMENT,
            }[anomaly.series_kind]
            role = (
                SignalEvidenceRole.SUPPORTING
                if anomaly.outcome is AnomalyOutcome.FLAGGED
                else SignalEvidenceRole.COUNTER
            )
            candidates.append(
                EvidenceCandidate(
                    kind,
                    "temporal_anomaly_result",
                    anomaly.id,
                    anomaly.input_fingerprint,
                    anomaly.geography_unit_id,
                    anomaly.facility_id,
                    anomaly.period_start,
                    anomaly.period_end,
                    anomaly.source_cutoff,
                    f"{anomaly.series_key} departed from its governed historical baseline.",
                    {
                        "series_key": anomaly.series_key,
                        "observed": str(anomaly.observed_value),
                        "expected": str(anomaly.expected_value),
                        "direction": anomaly.direction.value if anomaly.direction else None,
                        "deviation": str(anomaly.absolute_deviation),
                    },
                    anomaly.quality_context or {},
                    role,
                )
            )
        hotspots = self._session.execute(
            select(HotspotResult).where(
                HotspotResult.period_start >= period_start,
                HotspotResult.period_end <= period_end,
                HotspotResult.outcome.in_([HotspotOutcome.HOTSPOT, HotspotOutcome.NOT_HOTSPOT]),
            )
        ).scalars()
        for hotspot in hotspots:
            role = (
                SignalEvidenceRole.SUPPORTING
                if hotspot.outcome is HotspotOutcome.HOTSPOT
                else SignalEvidenceRole.COUNTER
            )
            candidates.append(
                EvidenceCandidate(
                    SignalEvidenceKind.HOTSPOT,
                    "hotspot_result",
                    hotspot.id,
                    hotspot.input_fingerprint,
                    hotspot.geography_unit_id,
                    None,
                    hotspot.period_start,
                    hotspot.period_end,
                    hotspot.source_cutoff,
                    f"{hotspot.series_key} met its governed hotspot definition.",
                    {
                        "series_key": hotspot.series_key,
                        "observed": str(hotspot.observed_value),
                        "expected": str(hotspot.expected_value),
                        "consecutive_periods": hotspot.consecutive_periods,
                    },
                    hotspot.quality_context or {},
                    role,
                )
            )
        clusters = self._session.execute(
            select(SpatialClusterResult).where(
                SpatialClusterResult.period_start >= period_start,
                SpatialClusterResult.period_end <= period_end,
                SpatialClusterResult.outcome.in_(
                    [ClusterOutcome.CLUSTERED, ClusterOutcome.NOT_CLUSTERED]
                ),
            )
        ).scalars()
        for cluster in clusters:
            role = (
                SignalEvidenceRole.SUPPORTING
                if cluster.outcome is ClusterOutcome.CLUSTERED
                else SignalEvidenceRole.COUNTER
            )
            candidates.append(
                EvidenceCandidate(
                    SignalEvidenceKind.SPATIAL_CLUSTER,
                    "spatial_cluster_result",
                    cluster.id,
                    cluster.input_fingerprint,
                    cluster.geography_unit_id,
                    None,
                    cluster.period_start,
                    cluster.period_end,
                    cluster.source_cutoff,
                    "The area met its governed spatial-clustering definition.",
                    {
                        "observed": str(cluster.observed_value),
                        "neighbourhood_value": str(cluster.neighbourhood_value),
                        "cluster_group_size": cluster.cluster_group_size,
                    },
                    cluster.quality_context or {},
                    role,
                )
            )
        recurrence_rows = self._session.execute(
            select(RecurrenceResult).where(
                RecurrenceResult.period_start >= period_start,
                RecurrenceResult.period_end <= period_end,
                RecurrenceResult.value_status == IndicatorValueStatus.AVAILABLE,
            )
        ).scalars()
        for recurrence in recurrence_rows:
            observed = recurrence.value if recurrence.value is not None else recurrence.numerator
            role = (
                SignalEvidenceRole.SUPPORTING
                if observed is not None and Decimal(str(observed)) > 0
                else SignalEvidenceRole.COUNTER
            )
            is_facility = recurrence.scope_kind is RecurrenceScopeKind.FACILITY
            candidates.append(
                EvidenceCandidate(
                    SignalEvidenceKind.RECURRENCE,
                    "recurrence_result",
                    recurrence.id,
                    recurrence.input_fingerprint,
                    None if is_facility else recurrence.scope_id,
                    recurrence.scope_id if is_facility else None,
                    recurrence.period_start,
                    recurrence.period_end,
                    recurrence.source_cutoff,
                    f"{recurrence.measure.value} was observed in recurrence surveillance.",
                    {
                        "measure": recurrence.measure.value,
                        "scope_kind": recurrence.scope_kind.value,
                        "numerator": recurrence.numerator,
                        "denominator": recurrence.denominator,
                        "value": str(recurrence.value) if recurrence.value is not None else None,
                        "eligible_patients": recurrence.eligible_patients,
                        "excluded_unlinked_encounters": recurrence.excluded_unlinked_encounters,
                    },
                    recurrence.interpretation_context or {},
                    role,
                )
            )
        reconciliation_rows = self._session.execute(
            select(ReconciliationFinding, AggregateSubmission)
            .join(
                AggregateSubmission,
                AggregateSubmission.id == ReconciliationFinding.aggregate_submission_id,
            )
            .where(
                AggregateSubmission.period_start >= period_start,
                AggregateSubmission.period_end <= period_end,
                ReconciliationFinding.reconciliation_status != ReconciliationStatus.UNCOMPARABLE,
            )
        ).all()
        for finding, submission in reconciliation_rows:
            supporting = finding.reconciliation_status in {
                ReconciliationStatus.DIFFERS,
                ReconciliationStatus.REPORTED_ONLY,
                ReconciliationStatus.DERIVED_ONLY,
            }
            candidates.append(
                EvidenceCandidate(
                    SignalEvidenceKind.RECONCILIATION,
                    "reconciliation_finding",
                    finding.id,
                    finding.input_checksum,
                    None,
                    submission.facility_id,
                    submission.period_start,
                    submission.period_end,
                    max(finding.created_at, submission.received_at),
                    (
                        f"Reported and patient-derived {finding.element_code} figures "
                        f"were {finding.reconciliation_status.value}."
                    ),
                    {
                        "element_code": finding.element_code,
                        "status": finding.reconciliation_status.value,
                        "reported": finding.reported_value,
                        "derived": finding.derived_value,
                        "difference": finding.difference,
                        "derived_denominator": finding.derived_denominator,
                        "absolute_tolerance": finding.absolute_tolerance,
                    },
                    finding.detail or {},
                    (SignalEvidenceRole.SUPPORTING if supporting else SignalEvidenceRole.COUNTER),
                )
            )
        # Operational commodity alerts are cited as context only. They remain
        # separate records and never acquire a signal score of their own.
        alerts = self._session.execute(
            select(CommodityOperationalAlert).where(
                CommodityOperationalAlert.period_start >= period_start,
                CommodityOperationalAlert.period_end <= period_end,
            )
        ).scalars()
        for alert in alerts:
            scopes: list[tuple[uuid.UUID | None, uuid.UUID | None]] = [(None, alert.facility_id)]
            if alert.district_geography_unit_id is not None:
                scopes.append((alert.district_geography_unit_id, None))
            for geography_id, facility_id in scopes:
                candidates.append(
                    EvidenceCandidate(
                        SignalEvidenceKind.COMMODITY_ALERT,
                        "commodity_operational_alert",
                        alert.id,
                        alert.input_fingerprint,
                        geography_id,
                        facility_id,
                        alert.period_start,
                        alert.period_end,
                        alert.source_cutoff,
                        alert.statement,
                        {
                            "commodity_code": alert.commodity_code,
                            "alert_kind": alert.alert_kind.value,
                            "facility_id": str(alert.facility_id),
                        },
                        {},
                        SignalEvidenceRole.CONTEXT,
                    )
                )
        return candidates

    def generate(self, period_start: date, period_end: date) -> SignalReport:
        if period_end < period_start:
            raise ValueError("period_end must be on or after period_start")
        started = datetime.now(UTC)
        method_id, rules, missing = self.rules()
        if method_id is None:
            run = SignalGenerationRun(
                run_status=SignalGenerationStatus.NOT_CONFIGURED,
                period_start=period_start,
                period_end=period_end,
                missing_configuration={"parameters": missing},
                candidates_examined=0,
                signals_created=0,
                signals_superseded=0,
                engine_version=ENGINE_VERSION,
                started_at=started,
                finished_at=datetime.now(UTC),
                notes="No evidence was scored. This is not evidence that no signal exists.",
            )
            self._session.add(run)
            self._session.flush()
            return SignalReport(
                run.id, SignalGenerationStatus.NOT_CONFIGURED, missing_configuration=missing
            )

        candidates = self.collect(period_start, period_end)
        run = SignalGenerationRun(
            run_status=SignalGenerationStatus.RUNNING,
            method_version_id=method_id,
            period_start=period_start,
            period_end=period_end,
            candidates_examined=len(candidates),
            signals_created=0,
            signals_superseded=0,
            engine_version=ENGINE_VERSION,
            started_at=started,
        )
        self._session.add(run)
        self._session.flush()
        report = SignalReport(run.id, SignalGenerationStatus.RUNNING, len(candidates))
        for rule in rules:
            matching = [
                candidate for candidate in candidates if candidate.kind in rule.source_kinds
            ]
            groups: dict[
                tuple[uuid.UUID | None, uuid.UUID | None, date, date], list[EvidenceCandidate]
            ] = {}
            for candidate in matching:
                key = (
                    candidate.geography_unit_id,
                    candidate.facility_id,
                    candidate.period_start,
                    candidate.period_end,
                )
                groups.setdefault(key, []).append(candidate)
            for key, evidence in groups.items():
                self._apply_rule(run, report, rule, key, evidence)
        run.run_status = SignalGenerationStatus.COMPLETED
        run.signals_created = report.signals_created
        run.signals_superseded = report.signals_superseded
        run.finished_at = datetime.now(UTC)
        report.status = SignalGenerationStatus.COMPLETED
        self._session.flush()
        return report

    def _apply_rule(
        self,
        run: SignalGenerationRun,
        report: SignalReport,
        rule: SignalRule,
        key: tuple[uuid.UUID | None, uuid.UUID | None, date, date],
        evidence: list[EvidenceCandidate],
    ) -> None:
        # Re-running an upstream immutable engine may write a new row carrying
        # the same input fingerprint. It is the same evidence, not a second
        # vote. Deduplicate by stable evidence identity before counting or
        # scoring, and sort so database row order cannot alter a fingerprint.
        deduplicated: dict[tuple[str, str, str], EvidenceCandidate] = {}
        for item in sorted(
            evidence,
            key=lambda candidate: (
                candidate.kind.value,
                candidate.source_fingerprint,
                candidate.role.value,
                str(candidate.source_record_id),
            ),
        ):
            evidence_key = (
                item.kind.value,
                item.source_fingerprint,
                item.role.value,
            )
            deduplicated.setdefault(evidence_key, item)
        evidence = list(deduplicated.values())
        supporting = [item for item in evidence if item.role is SignalEvidenceRole.SUPPORTING]
        if len(supporting) < rule.minimum_evidence:
            return
        score = sum(
            (
                rule.weights[item.kind]
                * (Decimal(-1) if item.role is SignalEvidenceRole.COUNTER else Decimal(1))
                if item.role is not SignalEvidenceRole.CONTEXT
                else Decimal(0)
            )
            for item in evidence
        )
        if score < rule.minimum_score:
            return
        geography_id, facility_id, start, end = key
        group_key = _fingerprint(
            [rule.code, rule.signal_type.value, str(geography_id), str(facility_id), start, end]
        )
        input_fingerprint = _fingerprint(
            [
                str(run.method_version_id),
                rule.code,
                [(item.kind.value, item.source_fingerprint, item.role.value) for item in evidence],
            ]
        )
        existing_same = self._session.execute(
            select(SurveillanceSignal).where(
                SurveillanceSignal.input_fingerprint == input_fingerprint
            )
        ).scalar_one_or_none()
        if existing_same is not None:
            report.signals_unchanged += 1
            return
        previous = self._session.execute(
            select(SurveillanceSignal).where(
                SurveillanceSignal.group_key == group_key,
                SurveillanceSignal.signal_status == SignalStatus.ACTIVE,
            )
        ).scalar_one_or_none()
        priority = SignalPriority.UNCLASSIFIED
        for band in rule.priority_bands:
            if score >= band.minimum_score:
                priority = band.priority
        signal_id = uuid.uuid4()
        initial_status = SignalStatus.SUPERSEDED if previous is not None else SignalStatus.ACTIVE
        signal = SurveillanceSignal(
            id=signal_id,
            generation_run_id=run.id,
            method_version_id=run.method_version_id,
            signal_type=rule.signal_type,
            signal_status=initial_status,
            priority=priority,
            group_key=group_key,
            geography_unit_id=geography_id,
            facility_id=facility_id,
            period_start=start,
            period_end=end,
            title=rule.signal_type.value.replace("_", " ").title(),
            statement=(
                f"{rule.signal_type.value.replace('_', ' ')} pattern requiring investigation."
            ),
            rule_code=rule.code,
            rule_snapshot={
                "source_kinds": sorted(kind.value for kind in rule.source_kinds),
                "minimum_evidence": rule.minimum_evidence,
                "minimum_score": str(rule.minimum_score),
                "weights": {kind.value: str(weight) for kind, weight in rule.weights.items()},
                "priority_bands": [
                    {"priority": band.priority.value, "minimum_score": str(band.minimum_score)}
                    for band in rule.priority_bands
                ],
                "recommended_action_codes": list(rule.recommended_action_codes),
            },
            score=score,
            evidence_count=len(supporting),
            counter_evidence_count=sum(
                item.role is SignalEvidenceRole.COUNTER for item in evidence
            ),
            data_quality={"sources": [item.quality_context for item in evidence]},
            uncertainty=[INTERPRETATION_LIMIT],
            recommended_action_codes=list(rule.recommended_action_codes),
            input_fingerprint=input_fingerprint,
            source_cutoff=max(item.source_cutoff for item in evidence),
            generated_at=datetime.now(UTC),
            supersedes_id=previous.id if previous else None,
            # A self-reference is a transaction-local staging state that
            # satisfies the immutable-row constraint while the old ACTIVE row
            # still owns the partial unique key. It is replaced before this
            # method can return or the transaction can commit.
            superseded_by_id=signal_id if previous is not None else None,
        )
        self._session.add(signal)
        self._session.flush()
        if previous is not None:
            previous.signal_status = SignalStatus.SUPERSEDED
            previous.superseded_by_id = signal_id
            self._session.flush()
            signal.signal_status = SignalStatus.ACTIVE
            signal.superseded_by_id = None
            self._session.flush()
            report.signals_superseded += 1
        for item in evidence:
            self._session.add(
                SignalEvidence(
                    signal_id=signal.id,
                    evidence_kind=item.kind,
                    role=item.role,
                    source_table=item.source_table,
                    source_record_id=item.source_record_id,
                    contribution=(
                        None
                        if item.role is SignalEvidenceRole.CONTEXT
                        else rule.weights[item.kind]
                        * (Decimal(-1) if item.role is SignalEvidenceRole.COUNTER else Decimal(1))
                    ),
                    summary=item.summary,
                    facts=item.facts,
                    quality_context=item.quality_context,
                )
            )
        report.signals_created += 1


__all__ = [
    "ENGINE_VERSION",
    "INTERPRETATION_LIMIT",
    "SIGNAL_METHOD_CODE",
    "EvidenceCandidate",
    "PriorityBand",
    "SignalEngine",
    "SignalReport",
    "SignalRule",
]
