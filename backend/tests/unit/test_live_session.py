"""Opaque live sessions: hashed ids, rotation, expiry, credential drop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from mars.security.live_session import (
    SESSION_ID_BYTES,
    InMemoryCredentialHolder,
    InMemorySessionStore,
    hash_session_id,
)
from mars.security.permissions import Permission, SensitivityLevel, SystemRole
from mars.security.principal import AuthenticatedPrincipal
from mars.security.remote_authorization import (
    AUTHORIZATION_SCHEMA_VERSION,
    PENDING_MAPPING,
    SOURCE_DHIS2,
    UNAVAILABLE_READINESS,
    AuthenticatedSourceIdentity,
    LiveAuthorizationState,
    RemoteAuthorizationContext,
    RemoteWorkspaceScope,
)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid.uuid4(),
        subject="dhis2:u1",
        username="officer",
        display_name="Officer",
        roles=frozenset({SystemRole.DISTRICT_HSD.value}),
        permissions=frozenset({Permission.GEOGRAPHY_VIEW}),
        max_sensitivity=SensitivityLevel.AGGREGATE,
        auth_method="dhis2_pilot",
        is_synthetic=False,
    )


def _authorization(*, schema_version: int = AUTHORIZATION_SCHEMA_VERSION) -> LiveAuthorizationState:
    return LiveAuthorizationState(
        schema_version=schema_version,
        identity=AuthenticatedSourceIdentity(
            source_system=SOURCE_DHIS2,
            remote_user_id="User1Uid001",
            username="officer",
            display_name="Officer",
        ),
        remote_authorization=RemoteAuthorizationContext(
            capture_scope=(),
            data_view_scope=(),
            tracker_search_scope=(),
            authorities=(),
            fallback_policy="documented",
            fallback_used=False,
            fallback_source=None,
            fallback_reason=None,
            data_view_field_present=True,
        ),
        workspace=RemoteWorkspaceScope(
            status="resolved",
            scope_type="district",
            source=SOURCE_DHIS2,
            external_uid="PaderDist01",
            name="Pader",
            code=None,
            level=3,
            path="/UgandanRoot/PaderDist01",
            parent_uid="UgandanRoot",
        ),
        mapping=PENDING_MAPPING,
        readiness=UNAVAILABLE_READINESS,
        landing_path="/live/dhis2/district/PaderDist01",
    )


class TestOpaqueSessionStore:
    def test_creates_hashed_session_and_csrf(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        raw, record = store.create(_principal(), _authorization(), source_status="connected")
        assert len(raw) >= SESSION_ID_BYTES
        assert record.id_hash == hash_session_id(raw)
        assert record.id_hash != raw
        assert record.csrf_token
        assert store.get(raw) is not None
        assert record.authorization.workspace.name == "Pader"
        assert record.authorization.mapping.status == "pending"

    def test_expiry_invalidates_and_drops_credentials(self) -> None:
        store = InMemorySessionStore(idle_seconds=1, absolute_seconds=1)
        holder = InMemoryCredentialHolder()
        raw, _record = store.create(_principal(), _authorization(), source_status="connected")
        holder.store(raw, "officer", "sentinel-session-secret")
        later = datetime.now(tz=UTC) + timedelta(seconds=5)
        assert store.get(raw, now=later) is None
        holder.drop(raw)
        assert not holder.has(raw)

    def test_logout_destroys_session_and_credential(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        holder = InMemoryCredentialHolder()
        raw, _record = store.create(_principal(), _authorization(), source_status="connected")
        holder.store(raw, "officer", "sentinel-session-secret")
        store.invalidate(raw)
        holder.drop(raw)
        assert store.get(raw) is None
        assert not holder.has(raw)

    def test_rotation_preserves_remote_authorization(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        holder = InMemoryCredentialHolder()
        raw, _record = store.create(_principal(), _authorization(), source_status="connected")
        holder.store(raw, "officer", "sentinel-session-secret")
        rotated = store.rotate(raw)
        assert rotated is not None
        new_raw, new_record = rotated
        holder.transfer(raw, new_raw)
        assert new_raw != raw
        assert store.get(raw) is None
        assert store.get(new_raw) is not None
        assert new_record.csrf_token
        assert new_record.authorization.workspace.external_uid == "PaderDist01"
        assert holder.has(new_raw)
        assert not holder.has(raw)

    def test_stale_authorization_schema_is_rejected(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        raw, _record = store.create(
            _principal(), _authorization(schema_version=0), source_status="connected"
        )
        assert store.get(raw) is None

    def test_credential_holder_does_not_expose_password_via_repr(self) -> None:
        holder = InMemoryCredentialHolder()
        holder.store("sid", "officer", "sentinel-session-secret")
        assert "sentinel-session-secret" not in repr(holder)

    def test_authorization_context_has_no_password_field(self) -> None:
        state = _authorization()
        assert "password" not in state.__dataclass_fields__
        assert "password" not in state.identity.__dataclass_fields__
