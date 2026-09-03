"""The anomaly vocabulary, without a database.

Small tests, but they guard the property the whole module exists for: that
"MARS could not judge this" can never be stored as "MARS judged this normal".
A future refactor that collapses the unevaluated outcomes into one, or folds
them into ``not_flagged``, would pass every integration test that only looks at
flagged rows. These would fail.
"""

from __future__ import annotations

from mars.analytics.anomaly import (
    ANOMALY_RULE_CODE,
    INTERPRETATION_LIMIT,
    PERSISTENCE_PARAMETER,
    REQUIRED_PARAMETERS,
)
from mars.domain.anomaly import AnomalyBuild, AnomalyPersistence, TemporalAnomalyResult
from mars.domain.enums import AnomalyDetectionMethod, AnomalyOutcome

UNEVALUATED = {
    AnomalyOutcome.NOT_EVALUATED_NO_OBSERVATION,
    AnomalyOutcome.NOT_EVALUATED_NO_BASELINE,
    AnomalyOutcome.NOT_EVALUATED_BELOW_MINIMUM_COUNT,
    AnomalyOutcome.NOT_EVALUATED_COUNT_UNKNOWN,
    AnomalyOutcome.NOT_EVALUATED_METHOD_INAPPLICABLE,
}


def constraints(model: type) -> set[str]:
    return {c.name for c in model.__table__.constraints if c.name}


class TestCouldNotJudgeIsNeverJudgedNormal:
    def test_the_conclusions_are_exactly_two(self) -> None:
        assert set(AnomalyOutcome) - UNEVALUATED == {
            AnomalyOutcome.FLAGGED,
            AnomalyOutcome.NOT_FLAGGED,
        }

    def test_each_reason_for_not_judging_is_its_own_outcome(self) -> None:
        """Five distinct reasons, five distinct values. Collapsing them would
        lose the difference between a facility with no history and a facility
        with three cases."""
        assert len(UNEVALUATED) == 5
        assert len({outcome.value for outcome in UNEVALUATED}) == 5

    def test_the_schema_enforces_that_not_flagged_was_evaluated(self) -> None:
        assert "ck_temporal_anomaly_result_not_flagged_means_evaluated" in constraints(
            TemporalAnomalyResult
        )

    def test_the_schema_enforces_that_a_flag_carries_its_evidence(self) -> None:
        assert "ck_temporal_anomaly_result_a_flag_carries_its_evidence" in constraints(
            TemporalAnomalyResult
        )


class TestPersistenceSeparatesCountingFromLabelling:
    def test_the_count_and_the_label_are_different_columns(self) -> None:
        names = {c.name for c in AnomalyPersistence.__table__.columns}
        assert "consecutive_periods" in names
        assert "is_sustained" in names

    def test_the_label_requires_configuration(self) -> None:
        assert "ck_anomaly_persistence_sustained_requires_configuration" in constraints(
            AnomalyPersistence
        )


class TestConfigurationIsNamedNotShipped:
    def test_the_rule_code_is_only_a_name(self) -> None:
        assert ANOMALY_RULE_CODE == "temporal_anomaly_rule"

    def test_no_threshold_value_is_defined_in_this_module(self) -> None:
        assert REQUIRED_PARAMETERS == (
            "detection_method",
            "deviation_threshold",
            "minimum_case_count",
        )
        assert all(isinstance(name, str) for name in REQUIRED_PARAMETERS)

    def test_persistence_is_optional_and_separate(self) -> None:
        """Without it MARS counts consecutive periods and labels nothing."""
        assert PERSISTENCE_PARAMETER not in REQUIRED_PARAMETERS

    def test_a_completed_run_must_record_the_rule_it_applied(self) -> None:
        assert "ck_anomaly_build_completed_runs_carry_their_rule" in constraints(AnomalyBuild)


class TestNothingClaimsACause:
    def test_the_interpretation_limit_denies_the_readings_it_invites(self) -> None:
        assert "reason to look, not a finding" in INTERPRETATION_LIMIT
        assert "does not establish a cause" in INTERPRETATION_LIMIT
        assert "resistance" in INTERPRETATION_LIMIT

    def test_every_implemented_method_is_a_statistical_test(self) -> None:
        """None of them names a clinical conclusion."""
        forbidden = ("resistance", "failure", "efficacy", "outbreak")
        for method in AnomalyDetectionMethod:
            assert not any(word in method.value for word in forbidden)
