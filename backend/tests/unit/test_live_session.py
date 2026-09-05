"""Opaque live sessions: hashed ids, rotation, expiry, credential drop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

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
from mars.services.live_discovery import (
    LiveDiscoveryUnavailableError,
    LiveMetadataDiscoveryService,
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

    def test_credential_holder_invokes_without_returning_credentials(self) -> None:
        holder = InMemoryCredentialHolder()
        holder.store("sid", "officer", "sentinel-session-secret")
        observed: list[tuple[str, str]] = []

        result = holder.invoke(
            "sid", lambda username, password: observed.append((username, password))
        )

        assert result is None
        assert observed == [("officer", "sentinel-session-secret")]
        assert holder.has("sid")

    def test_live_discovery_retains_only_a_sanitized_summary(self) -> None:
        holder = InMemoryCredentialHolder()
        holder.store("sid", "officer", "sentinel-session-secret")
        service = LiveMetadataDiscoveryService(
            holder,
            lambda _username, _password: {
                "stop_before_patient_data": True,
                "generated_at": "2026-09-04T12:00:00Z",
                "system": {"version": "2.40"},
                "api_generation": "modern_tracker_preferred_legacy_deprecated",
                "programmes": [{"id": "program-1"}],
                "program_stages": [{"id": "stage-1"}],
                "data_elements": [{"id": "element-1"}],
                "accessible_facilities": [
                    {"id": "facility-1", "name": "Pader HC III", "path": "/ug/root-1/facility-1"}
                ],
                "tracker_search_organisation_units": [{"id": "root-1", "path": "/ug/root-1"}],
                "candidate_mappings": [{"remote_id": "element-1"}],
                "report_files": {"json": "safe.json", "markdown": "safe.md"},
            },
        )

        result = service.discover("sid")

        assert result["patient_data_retrieved"] is False
        assert result["dhis2_version"] == "2.40"
        assert result["accessible_facility_count"] == 1
        assert result["tracker_facilities"] == [
            {
                "id": "facility-1",
                "name": "Pader HC III",
                "code": None,
                "parent_id": None,
                "path": "/ug/root-1/facility-1",
                "latitude": None,
                "longitude": None,
            }
        ]
        assert service.tracker_facility_uids("sid") == frozenset({"facility-1"})
        assert "programmes" not in result
        assert "sentinel-session-secret" not in repr(result)
        assert service.latest("sid") == result
        service.drop("sid")
        assert service.latest("sid") is None

    def test_live_discovery_requires_an_active_upstream_credential(self) -> None:
        service = LiveMetadataDiscoveryService(
            InMemoryCredentialHolder(),
            lambda _username, _password: {"stop_before_patient_data": True},
        )
        with pytest.raises(LiveDiscoveryUnavailableError):
            service.discover("missing")

    def test_authorization_context_has_no_password_field(self) -> None:
        state = _authorization()
        assert "password" not in state.__dataclass_fields__
        assert "password" not in state.identity.__dataclass_fields__
