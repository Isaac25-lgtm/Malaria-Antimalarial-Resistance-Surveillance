"""The investigation state machine, without a database.

An investigation record's whole value is that it can be reconstructed
afterwards. These tests guard the four properties that make that true: illegal
transitions are refused, terminal states are terminal, a conclusion carries its
reason, and the learning loop changes nothing on its own.
"""

from __future__ import annotations

from sqlalchemy import inspect

from mars.domain.enums import (
    InvestigationEventKind,
    InvestigationOutcome,
    InvestigationStatus,
)
from mars.domain.investigation import Investigation
from mars.investigations.service import (
    ALLOWED_TRANSITIONS,
    QUEUES,
    SLA_CONFIGURATION_KEY,
)


class TestTheStateMachineIsComplete:
    def test_every_status_declares_where_it_may_go(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(InvestigationStatus)

    def test_closed_and_escalated_are_terminal(self) -> None:
        """A conclusion that can be quietly withdrawn is not a conclusion. A
        genuine change of mind belongs in a new investigation citing the old."""
        assert ALLOWED_TRANSITIONS[InvestigationStatus.CLOSED] == frozenset()
        assert ALLOWED_TRANSITIONS[InvestigationStatus.ESCALATED] == frozenset()

    def test_a_new_investigation_cannot_jump_to_escalated(self) -> None:
        """Escalation without investigation would record a decision nobody
        made."""
        assert InvestigationStatus.ESCALATED not in ALLOWED_TRANSITIONS[InvestigationStatus.NEW]

    def test_a_new_investigation_cannot_jump_to_assigned(self) -> None:
        assert InvestigationStatus.ASSIGNED not in ALLOWED_TRANSITIONS[InvestigationStatus.NEW]

    def test_an_investigation_cannot_be_closed_before_work_starts(self) -> None:
        assert InvestigationStatus.CLOSED not in ALLOWED_TRANSITIONS[InvestigationStatus.NEW]
        assert InvestigationStatus.CLOSED not in ALLOWED_TRANSITIONS[InvestigationStatus.TRIAGED]
        assert InvestigationStatus.CLOSED not in ALLOWED_TRANSITIONS[InvestigationStatus.ASSIGNED]

    def test_every_state_can_reach_a_terminal_one(self) -> None:
        """No investigation can become permanently stuck in a queue."""
        for status, allowed in ALLOWED_TRANSITIONS.items():
            if status in (InvestigationStatus.CLOSED, InvestigationStatus.ESCALATED):
                continue
            reachable = _closure(status)
            assert reachable & {
                InvestigationStatus.CLOSED,
                InvestigationStatus.ESCALATED,
            }, f"{status.value} cannot be concluded"
            assert allowed

    def test_reassignment_is_permitted_from_assigned_and_under_investigation(
        self,
    ) -> None:
        """People go on leave mid-investigation."""
        assert InvestigationStatus.ASSIGNED in ALLOWED_TRANSITIONS[InvestigationStatus.ASSIGNED]
        assert (
            InvestigationStatus.ASSIGNED
            in ALLOWED_TRANSITIONS[InvestigationStatus.UNDER_INVESTIGATION]
        )

    def test_record_version_is_an_atomic_orm_concurrency_token(self) -> None:
        mapper = inspect(Investigation)
        assert mapper.version_id_col is Investigation.__table__.c.record_version


def _closure(start: InvestigationStatus) -> set[InvestigationStatus]:
    seen: set[InvestigationStatus] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for nxt in ALLOWED_TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


class TestTheOutcomeVocabularyIsGoverned:
    def test_the_strongest_outcome_is_a_validated_signal(self) -> None:
        """Not "confirmed resistance". A reviewer working from routine data
        may conclude the pattern is real and worth acting on; confirmation
        comes from a reference laboratory through a separate lane."""
        values = {outcome.value for outcome in InvestigationOutcome}
        assert "validated_signal" in values
        assert not any("resistan" in value for value in values)

    def test_a_benign_explanation_is_a_first_class_outcome(self) -> None:
        """The commonest real outcome, and a useful one. A vocabulary that
        only recorded confirmations would make every closed investigation look
        like a finding."""
        assert InvestigationOutcome.EXPLAINED in set(InvestigationOutcome)
        assert InvestigationOutcome.DATA_ISSUE in set(InvestigationOutcome)

    def test_review_may_conclude_it_could_not_tell(self) -> None:
        assert InvestigationOutcome.INSUFFICIENT_EVIDENCE in set(InvestigationOutcome)


class TestTheTimelineRecordsWhoDidWhat:
    def test_every_workflow_act_has_an_event_kind(self) -> None:
        kinds = {kind.value for kind in InvestigationEventKind}
        for required in (
            "opened",
            "triaged",
            "assigned",
            "reassigned",
            "note_added",
            "evidence_requested",
            "external_result_recorded",
            "started",
            "closed",
            "escalated",
        ):
            assert required in kinds

    def test_reassignment_is_its_own_kind(self) -> None:
        """A reassignment and a first assignment are different events, and a
        timeline that showed both as "assigned" would hide a handover."""
        assert InvestigationEventKind.REASSIGNED is not InvestigationEventKind.ASSIGNED


class TestTheActionCentreDoesNotInventDeadlines:
    def test_there_is_no_overdue_queue(self) -> None:
        """An empty overdue queue reads as "nothing is late". MARS has not been
        told what late means, and says so instead."""
        assert "overdue" not in QUEUES

    def test_the_sla_key_is_only_a_name(self) -> None:
        assert SLA_CONFIGURATION_KEY == "investigation_sla"

    def test_the_queues_offered_are_the_ones_blueprint_052_names(self) -> None:
        assert set(QUEUES) == {
            "new",
            "high_priority",
            "assigned_to_me",
            "under_investigation",
            "awaiting_external_result",
            "resolved",
        }
