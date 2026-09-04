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


class TestOpaqueSessionStore:
    def test_creates_hashed_session_and_csrf(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        raw, record = store.create(
            _principal(), mapping_status="mapped", source_status="connected", scope_type="district"
        )
        assert len(raw) >= SESSION_ID_BYTES
        assert record.id_hash == hash_session_id(raw)
        assert record.id_hash != raw
        assert record.csrf_token
        assert store.get(raw) is not None

    def test_expiry_invalidates_and_drops_credentials(self) -> None:
        store = InMemorySessionStore(idle_seconds=1, absolute_seconds=1)
        holder = InMemoryCredentialHolder()
        raw, _record = store.create(
            _principal(), mapping_status="mapped", source_status="connected", scope_type="district"
        )
        holder.store(raw, "officer", "sentinel-session-secret")
        later = datetime.now(tz=UTC) + timedelta(seconds=5)
        assert store.get(raw, now=later) is None
        holder.drop(raw)
        assert not holder.has(raw)

    def test_logout_destroys_session_and_credential(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        holder = InMemoryCredentialHolder()
        raw, _record = store.create(
            _principal(),
            mapping_status="pending",
            source_status="connected",
            scope_type="unresolved",
        )
        holder.store(raw, "officer", "sentinel-session-secret")
        store.invalidate(raw)
        holder.drop(raw)
        assert store.get(raw) is None
        assert not holder.has(raw)

    def test_rotation_issues_a_new_raw_id(self) -> None:
        store = InMemorySessionStore(idle_seconds=60, absolute_seconds=300)
        holder = InMemoryCredentialHolder()
        raw, _record = store.create(
            _principal(), mapping_status="mapped", source_status="connected", scope_type="district"
        )
        holder.store(raw, "officer", "sentinel-session-secret")
        rotated = store.rotate(raw)
        assert rotated is not None
        new_raw, new_record = rotated
        holder.transfer(raw, new_raw)
        assert new_raw != raw
        assert store.get(raw) is None
        assert store.get(new_raw) is not None
        assert new_record.csrf_token
        assert holder.has(new_raw)
        assert not holder.has(raw)

    def test_credential_holder_does_not_expose_password_via_repr(self) -> None:
        holder = InMemoryCredentialHolder()
        holder.store("sid", "officer", "sentinel-session-secret")
        assert "sentinel-session-secret" not in repr(holder)
