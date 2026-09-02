"""Configuration and method registry services.

Both registries enforce the same discipline: a published version is immutable,
exactly one version may be active for a given key at a time, and activation
records who approved it and why.

No surveillance threshold, episode window or signal weight is created here. Those
values are programme decisions; this service is the mechanism that records them
once they are supplied and approved.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.core.errors import ConflictError, NotFoundError, ValidationFailedError
from mars.core.timeutils import utc_now
from mars.domain.enums import AuditAction, LifecycleStatus, MethodKind
from mars.domain.governance import (
    ConfigurationKey,
    ConfigurationVersion,
    MethodDefinition,
    MethodVersion,
)
from mars.security.principal import AuthenticatedPrincipal
from mars.services.audit_service import AuditService


def canonical_checksum(value: Any) -> str:
    """SHA-256 over a canonical JSON serialisation.

    Sorted keys and compact separators, so the same logical value always yields
    the same checksum regardless of how it was constructed.
    """
    serialised = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


#: Lifecycle transitions permitted by change control.
_ALLOWED_TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.DRAFT: frozenset({LifecycleStatus.IN_REVIEW, LifecycleStatus.REJECTED}),
    LifecycleStatus.IN_REVIEW: frozenset(
        {LifecycleStatus.APPROVED, LifecycleStatus.REJECTED, LifecycleStatus.DRAFT}
    ),
    LifecycleStatus.APPROVED: frozenset({LifecycleStatus.ACTIVE, LifecycleStatus.REJECTED}),
    LifecycleStatus.ACTIVE: frozenset({LifecycleStatus.RETIRED}),
    LifecycleStatus.RETIRED: frozenset(),
    LifecycleStatus.REJECTED: frozenset(),
}


def can_transition(current: LifecycleStatus, target: LifecycleStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


class ConfigurationService:
    """Manages governed configuration keys and their versions."""

    def __init__(self, session: Session, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    # -- Keys -------------------------------------------------------------
    def create_key(
        self,
        *,
        key: str,
        label: str,
        description: str,
        category: str,
        requires_programme_approval: bool = True,
        value_schema: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> ConfigurationKey:
        existing = self._session.execute(
            select(ConfigurationKey).where(ConfigurationKey.key == key)
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(f"configuration key {key!r} already exists")

        record = ConfigurationKey(
            key=key,
            label=label,
            description=description,
            category=category,
            requires_programme_approval=requires_programme_approval,
            value_schema=value_schema,
            owner=owner,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get_key(self, key: str) -> ConfigurationKey:
        record = self._session.execute(
            select(ConfigurationKey).where(ConfigurationKey.key == key)
        ).scalar_one_or_none()
        if record is None:
            raise NotFoundError(f"configuration key {key!r} not found")
        return record

    def list_keys(self) -> list[ConfigurationKey]:
        return list(
            self._session.execute(
                select(ConfigurationKey).order_by(ConfigurationKey.category, ConfigurationKey.key)
            )
            .scalars()
            .all()
        )

    # -- Versions ---------------------------------------------------------
    def draft_version(
        self,
        *,
        key: str,
        value: dict[str, Any],
        reason_for_change: str,
        provenance: str,
        owner: str | None = None,
        effective_from: date | None = None,
    ) -> ConfigurationVersion:
        """Create a new draft version of a configuration value."""
        config_key = self.get_key(key)
        next_number = 1 + len(config_key.versions)

        version = ConfigurationVersion(
            configuration_key_id=config_key.id,
            version_number=next_number,
            status=LifecycleStatus.DRAFT,
            value=value,
            value_checksum=canonical_checksum(value),
            reason_for_change=reason_for_change,
            provenance=provenance,
            owner=owner,
            effective_from=effective_from,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def transition(
        self,
        *,
        version_id: uuid.UUID,
        target: LifecycleStatus,
        principal: AuthenticatedPrincipal,
        reason: str,
        effective_from: date | None = None,
    ) -> ConfigurationVersion:
        """Move a configuration version through the change-control lifecycle."""
        version = self._session.get(ConfigurationVersion, version_id)
        if version is None:
            raise NotFoundError("configuration version not found")

        if not can_transition(version.status, target):
            raise ValidationFailedError(
                f"cannot move a configuration version from {version.status.value} to {target.value}"
            )

        previous_status = version.status

        if target in (LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE):
            version.approved_by = principal.username
            version.approved_at = utc_now()

        if target is LifecycleStatus.ACTIVE:
            if effective_from is None and version.effective_from is None:
                raise ValidationFailedError(
                    "an active configuration version requires an effective_from date"
                )
            if effective_from is not None:
                version.effective_from = effective_from
            self._retire_current_active(version)

        version.status = target
        self._session.flush()

        self._audit.record(
            action=(
                AuditAction.CONFIGURATION_ACTIVATED
                if target is LifecycleStatus.ACTIVE
                else AuditAction.CONFIGURATION_CHANGED
            ),
            principal=principal,
            object_type="configuration_version",
            object_id=str(version.id),
            before_state={"status": previous_status.value},
            after_state={"status": target.value, "checksum": version.value_checksum},
            reason=reason,
        )
        return version

    def _retire_current_active(self, incoming: ConfigurationVersion) -> None:
        """Retire whichever version is currently active for this key."""
        current = (
            self._session.execute(
                select(ConfigurationVersion).where(
                    ConfigurationVersion.configuration_key_id == incoming.configuration_key_id,
                    ConfigurationVersion.status == LifecycleStatus.ACTIVE,
                    ConfigurationVersion.id != incoming.id,
                )
            )
            .scalars()
            .all()
        )
        for version in current:
            version.status = LifecycleStatus.RETIRED
            if version.effective_to is None and incoming.effective_from is not None:
                version.effective_to = incoming.effective_from

    def active_version(self, key: str) -> ConfigurationVersion | None:
        """Return the active version for ``key``, or None when nothing is active.

        Returning None rather than a default is deliberate. A missing
        configuration is a governance gap that the caller must surface, not a
        prompt to invent a value.
        """
        config_key = self.get_key(key)
        return self._session.execute(
            select(ConfigurationVersion).where(
                ConfigurationVersion.configuration_key_id == config_key.id,
                ConfigurationVersion.status == LifecycleStatus.ACTIVE,
            )
        ).scalar_one_or_none()


class MethodRegistryService:
    """Manages analytical method definitions and their versions."""

    def __init__(self, session: Session, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    def register_method(
        self,
        *,
        code: str,
        label: str,
        kind: MethodKind,
        purpose: str,
        owner: str | None = None,
        principal: AuthenticatedPrincipal | None = None,
    ) -> MethodDefinition:
        existing = self._session.execute(
            select(MethodDefinition).where(MethodDefinition.code == code)
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(f"method {code!r} already exists")

        method = MethodDefinition(code=code, label=label, kind=kind, purpose=purpose, owner=owner)
        self._session.add(method)
        self._session.flush()

        if principal is not None:
            self._audit.record(
                action=AuditAction.METHOD_REGISTERED,
                principal=principal,
                object_type="method_definition",
                object_id=str(method.id),
                after_state={"code": code, "kind": kind.value},
            )
        return method

    def get_method(self, code: str) -> MethodDefinition:
        method = self._session.execute(
            select(MethodDefinition).where(MethodDefinition.code == code)
        ).scalar_one_or_none()
        if method is None:
            raise NotFoundError(f"method {code!r} not found")
        return method

    def list_methods(self) -> list[MethodDefinition]:
        return list(
            self._session.execute(select(MethodDefinition).order_by(MethodDefinition.code))
            .scalars()
            .all()
        )

    def draft_version(
        self,
        *,
        code: str,
        semantic_version: str,
        summary: str,
        parameters: dict[str, Any] | None = None,
        artifact_reference: str | None = None,
        artifact_checksum: str | None = None,
        owner: str | None = None,
    ) -> MethodVersion:
        method = self.get_method(code)
        duplicate = self._session.execute(
            select(MethodVersion).where(
                MethodVersion.method_definition_id == method.id,
                MethodVersion.semantic_version == semantic_version,
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise ConflictError(f"{code}@{semantic_version} already exists")

        version = MethodVersion(
            method_definition_id=method.id,
            semantic_version=semantic_version,
            status=LifecycleStatus.DRAFT,
            summary=summary,
            parameters=parameters,
            artifact_reference=artifact_reference,
            artifact_checksum=artifact_checksum,
            owner=owner,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def promote(
        self,
        *,
        version_id: uuid.UUID,
        target: LifecycleStatus,
        principal: AuthenticatedPrincipal,
        reason: str,
        effective_from: date | None = None,
    ) -> MethodVersion:
        """Move a method version through candidate -> approved -> active -> retired."""
        version = self._session.get(MethodVersion, version_id)
        if version is None:
            raise NotFoundError("method version not found")

        if not can_transition(version.status, target):
            raise ValidationFailedError(
                f"cannot move a method version from {version.status.value} to {target.value}"
            )

        previous_status = version.status

        if target in (LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE):
            version.approved_by = principal.username
            version.approved_at = utc_now()

        if target is LifecycleStatus.ACTIVE:
            if effective_from is not None:
                version.effective_from = effective_from
            self._retire_current_active(version)

        version.status = target
        self._session.flush()

        self._audit.record(
            action=AuditAction.METHOD_PROMOTED,
            principal=principal,
            object_type="method_version",
            object_id=str(version.id),
            before_state={"status": previous_status.value},
            after_state={"status": target.value, "version": version.semantic_version},
            reason=reason,
        )
        return version

    def rollback(
        self,
        *,
        from_version_id: uuid.UUID,
        to_version_id: uuid.UUID,
        principal: AuthenticatedPrincipal,
        reason: str,
    ) -> MethodVersion:
        """Restore a previous method version without rebuilding raw data."""
        retiring = self._session.get(MethodVersion, from_version_id)
        restoring = self._session.get(MethodVersion, to_version_id)
        if retiring is None or restoring is None:
            raise NotFoundError("method version not found")
        if retiring.method_definition_id != restoring.method_definition_id:
            raise ValidationFailedError("rollback must stay within one method definition")

        retiring.status = LifecycleStatus.RETIRED
        restoring.status = LifecycleStatus.ACTIVE
        restoring.rolled_back_from_id = retiring.id
        restoring.rollback_reason = reason
        self._session.flush()

        self._audit.record(
            action=AuditAction.METHOD_ROLLED_BACK,
            principal=principal,
            object_type="method_version",
            object_id=str(restoring.id),
            before_state={"active_version": retiring.semantic_version},
            after_state={"active_version": restoring.semantic_version},
            reason=reason,
        )
        return restoring

    def _retire_current_active(self, incoming: MethodVersion) -> None:
        current = (
            self._session.execute(
                select(MethodVersion).where(
                    MethodVersion.method_definition_id == incoming.method_definition_id,
                    MethodVersion.status == LifecycleStatus.ACTIVE,
                    MethodVersion.id != incoming.id,
                )
            )
            .scalars()
            .all()
        )
        for version in current:
            version.status = LifecycleStatus.RETIRED

    def active_versions(self) -> list[MethodVersion]:
        """Every currently active method version, for the metadata endpoint."""
        return list(
            self._session.execute(
                select(MethodVersion).where(MethodVersion.status == LifecycleStatus.ACTIVE)
            )
            .scalars()
            .all()
        )
