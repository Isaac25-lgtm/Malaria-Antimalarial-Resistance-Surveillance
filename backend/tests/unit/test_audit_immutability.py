"""Audit immutability.

Three layers protect the audit trail. These tests cover the two that live in
Python; the database trigger is exercised by the integration suite, which skips
when no PostgreSQL is available.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from mars.domain.audit import (
    AuditEvent,
    AuditEventImmutableError,
    _block_audit_delete,
    _block_audit_mutation_in_session,
    _block_audit_update,
)
from mars.domain.enums import AuditAction, AuditOutcome


def _event() -> AuditEvent:
    return AuditEvent(
        id=uuid.uuid4(),
        actor_kind="user",
        actor_user_id=uuid.uuid4(),
        actor_label="synthetic.user",
        action=AuditAction.LOGIN_SUCCEEDED,
        outcome=AuditOutcome.SUCCEEDED,
    )


class _FakeSession:
    """Minimal stand-in exposing the attributes the flush guard reads."""

    def __init__(self, dirty: list[Any] | None = None, deleted: list[Any] | None = None):
        self.dirty = dirty or []
        self.deleted = deleted or []

    def is_modified(self, _obj: Any, include_collections: bool = True) -> bool:
        return True


class TestMapperLevelGuards:
    def test_update_listener_raises(self) -> None:
        with pytest.raises(AuditEventImmutableError, match="cannot be updated"):
            _block_audit_update(None, None, _event())  # type: ignore[arg-type]

    def test_delete_listener_raises(self) -> None:
        with pytest.raises(AuditEventImmutableError, match="cannot be deleted"):
            _block_audit_delete(None, None, _event())  # type: ignore[arg-type]


class TestSessionFlushGuard:
    """Catches an attempt that would otherwise bypass the mapper listeners."""

    def test_dirty_audit_event_is_rejected(self) -> None:
        session = _FakeSession(dirty=[_event()])
        with pytest.raises(AuditEventImmutableError, match="cannot be updated"):
            _block_audit_mutation_in_session(session, None, None)  # type: ignore[arg-type]

    def test_deleted_audit_event_is_rejected(self) -> None:
        session = _FakeSession(deleted=[_event()])
        with pytest.raises(AuditEventImmutableError, match="cannot be deleted"):
            _block_audit_mutation_in_session(session, None, None)  # type: ignore[arg-type]

    def test_other_dirty_objects_are_untouched(self) -> None:
        """The guard must not interfere with ordinary writes."""

        class Unrelated:
            pass

        session = _FakeSession(dirty=[Unrelated()], deleted=[Unrelated()])
        _block_audit_mutation_in_session(session, None, None)  # type: ignore[arg-type]

    def test_clean_session_passes(self) -> None:
        _block_audit_mutation_in_session(_FakeSession(), None, None)  # type: ignore[arg-type]


class TestMigrationInstallsDatabaseTrigger:
    """The Python guards can be bypassed by raw SQL; the trigger cannot."""

    def test_trigger_and_function_are_created(self) -> None:
        from pathlib import Path

        migrations = Path(__file__).resolve().parents[2] / "migrations" / "versions"
        source = "\n".join(p.read_text(encoding="utf-8") for p in migrations.glob("*.py"))
        assert "CREATE TRIGGER audit_event_append_only" in source
        assert "BEFORE UPDATE OR DELETE ON mars_audit.audit_event" in source
        assert "reject_audit_mutation" in source

    def test_trigger_is_dropped_on_downgrade(self) -> None:
        from pathlib import Path

        migrations = Path(__file__).resolve().parents[2] / "migrations" / "versions"
        source = "\n".join(p.read_text(encoding="utf-8") for p in migrations.glob("*.py"))
        assert "DROP TRIGGER IF EXISTS audit_event_append_only" in source


class TestAuditVocabulary:
    def test_actions_cover_the_blueprint_event_list(self) -> None:
        """Blueprint section 066 enumerates what must be reconstructable."""
        required = {
            "login_succeeded",
            "login_failed",
            "access_denied",
            "configuration_changed",
            "method_promoted",
            "role_assigned",
            "geography_scope_changed",
            "sensitivity_scope_changed",
            "case_evidence_accessed",
            "reidentification_performed",
            "export_generated",
            "data_imported",
            "signal_created",
            "signal_triaged",
            "investigation_updated",
            "ai_request_submitted",
        }
        available = {action.value for action in AuditAction}
        assert required <= available, f"missing audit actions: {required - available}"

    def test_outcomes_distinguish_denial_from_failure(self) -> None:
        """A denied action and a failed action are different facts."""
        assert {o.value for o in AuditOutcome} == {"succeeded", "denied", "failed"}


class TestAuditServiceExposesNoMutation:
    def test_service_has_no_update_or_delete_method(self) -> None:
        """The absence of a write path is part of the design, so assert it."""
        from mars.services.audit_service import AuditService

        forbidden = [
            name
            for name in dir(AuditService)
            if not name.startswith("_")
            and any(verb in name.lower() for verb in ("update", "delete", "remove", "edit"))
        ]
        assert not forbidden, f"AuditService exposes mutation methods: {forbidden}"
