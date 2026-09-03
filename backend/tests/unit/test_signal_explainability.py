"""Prompt 21-22 vocabulary and persistence-boundary guarantees."""

from mars.domain.enums import (
    SignalEvidenceRole,
    SignalPriority,
    SignalType,
)
from mars.domain.explanation import SignalExplanation
from mars.domain.signal import SignalEvidence, SignalGenerationRun, SurveillanceSignal
from mars.explainability.engine import INHERENT_MISSING_INFORMATION, INTERPRETATION_LIMIT
from mars.signals.engine import SIGNAL_METHOD_CODE, SignalEngine


def _constraints(model: type) -> set[str]:
    return {constraint.name for constraint in model.__table__.constraints if constraint.name}


def _columns(model: type) -> set[str]:
    return {column.name for column in model.__table__.columns}


def test_signal_taxonomy_covers_the_governed_routine_patterns() -> None:
    assert {item.value for item in SignalType} == {
        "repeat_positive",
        "recurrence_cluster",
        "temporal_anomaly",
        "spatial_cluster",
        "testing_anomaly",
        "treatment_anomaly",
        "commodity_associated",
        "facility_anomaly",
        "data_quality",
        "reconciliation",
    }


def test_signal_scoring_has_no_built_in_rule_or_priority() -> None:
    assert SIGNAL_METHOD_CODE == "signal_prioritisation"
    assert SignalPriority.UNCLASSIFIED.value == "unclassified"
    assert "default" not in SIGNAL_METHOD_CODE


def test_context_is_not_supporting_evidence() -> None:
    assert SignalEvidenceRole.CONTEXT is not SignalEvidenceRole.SUPPORTING


def test_a_classified_signal_requires_governance_and_keeps_rule_snapshot() -> None:
    assert "ck_surveillance_signal_classified_priority_requires_governance" in _constraints(
        SurveillanceSignal
    )
    assert {"rule_code", "rule_snapshot", "method_version_id"} <= _columns(SurveillanceSignal)


def test_signal_evidence_kind_must_match_source_table() -> None:
    assert "ck_signal_evidence_evidence_kind_matches_source_table" in _constraints(SignalEvidence)


def test_unconfigured_signal_generation_is_a_record() -> None:
    assert "ck_signal_generation_run_refusal_names_missing_configuration" in _constraints(
        SignalGenerationRun
    )


def test_explanation_contains_every_required_dimension() -> None:
    assert {
        "why_flagged",
        "evidence",
        "counter_evidence",
        "data_quality",
        "method_steps",
        "uncertainty",
        "missing_information",
        "recommended_actions",
        "interpretation_limit",
    } <= _columns(SignalExplanation)


def test_explanation_language_denies_unavailable_conclusions() -> None:
    assert "does not confirm" in INTERPRETATION_LIMIT
    assert "resistance" in INTERPRETATION_LIMIT
    joined = " ".join(INHERENT_MISSING_INFORMATION).lower()
    assert "recrudescence" in joined
    assert "reinfection" in joined
    assert "genotype" in joined


def test_signal_rules_refuse_boolean_nan_and_context_only_parameters() -> None:
    engine = SignalEngine.__new__(SignalEngine)
    base = {
        "code": "TEST",
        "signal_type": "spatial_cluster",
        "source_kinds": ["spatial_cluster"],
        "minimum_evidence": 1,
        "minimum_score": 1,
        "weights": {"spatial_cluster": 1},
    }
    for replacement in (
        {"minimum_evidence": True},
        {"minimum_score": "NaN"},
        {
            "source_kinds": ["commodity_alert"],
            "weights": {"commodity_alert": 1},
        },
    ):
        candidate = {**base, **replacement}
        try:
            engine._parse_rule(candidate)
        except ValueError:
            pass
        else:  # pragma: no cover - failure branch gives a useful assertion
            raise AssertionError(f"unsafe rule was accepted: {replacement}")


def test_priority_cannot_decrease_as_score_rises() -> None:
    engine = SignalEngine.__new__(SignalEngine)
    rule = {
        "code": "TEST",
        "signal_type": "spatial_cluster",
        "source_kinds": ["spatial_cluster"],
        "minimum_evidence": 1,
        "minimum_score": 1,
        "weights": {"spatial_cluster": 1},
        "priority_bands": [
            {"priority": "high", "minimum_score": 1},
            {"priority": "attention", "minimum_score": 2},
        ],
    }
    try:
        engine._parse_rule(rule)
    except ValueError as exc:
        assert "increase" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("decreasing priority rule was accepted")
