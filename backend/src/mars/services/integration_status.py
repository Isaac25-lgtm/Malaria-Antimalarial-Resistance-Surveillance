"""Reading integration state for the API.

A service rather than router code, for the ordinary reason: routers speak in
response schemas and hold no queries (ADR 0002).

**Nothing here returns a credential.** The status object says whether
credentials are configured; it never says what they are, and the base URL is
returned with any userinfo stripped.

**Nothing here imports an adapter.** ADR 0003 keeps ``services`` free of every
external system's shape, and a status endpoint is not a reason to make an
exception. Configuration comes from settings; the adapter version comes from
the runs themselves, which is the more truthful answer anyway - "the version
that last ran" is a fact, where a compile-time constant is only a claim about
the next run.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mars.core.settings import Settings
from mars.core.urls import strip_url_credentials
from mars.domain.enums import MappingProposalStatus
from mars.domain.integration import IntegrationMappingProposal, IntegrationRun


class IntegrationStatusService:
    """What an operator needs to know about an exchange, and nothing more."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def status(self, system: str = "dhis2") -> dict[str, object]:
        """Configuration and activity for one external system."""
        configured = bool(self._settings.dhis2_enabled and self._settings.dhis2_base_url)
        credentials_present = bool(
            self._settings.dhis2_token
            or (self._settings.dhis2_username and self._settings.dhis2_password)
        )

        latest = (
            self._session.execute(
                select(IntegrationRun)
                .where(IntegrationRun.system == system)
                .order_by(IntegrationRun.started_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        total = self._session.execute(
            select(func.count()).select_from(IntegrationRun).where(IntegrationRun.system == system)
        ).scalar_one()
        unresolved = self._session.execute(
            select(func.count())
            .select_from(IntegrationMappingProposal)
            .where(
                IntegrationMappingProposal.system == system,
                IntegrationMappingProposal.proposal_status == MappingProposalStatus.PROPOSED,
            )
        ).scalar_one()

        return {
            "system": system,
            "enabled": self._settings.dhis2_enabled,
            "configured": configured,
            "credentials_present": credentials_present,
            "tls_verification": self._settings.dhis2_verify_tls,
            "outbound_push_enabled": self._settings.dhis2_push_enabled,
            # The version that last ran, not a compile-time constant. Null
            # before the first exchange, which is honest: nothing has run.
            "adapter_version": latest.adapter_version if latest else None,
            "base_url": (
                strip_url_credentials(self._settings.dhis2_base_url) if configured else None
            ),
            "total_runs": int(total),
            "last_run_at": latest.started_at if latest else None,
            "last_run_status": latest.run_status.value if latest else None,
            "unresolved_mappings": int(unresolved),
        }

    def list_runs(self, system: str = "dhis2", limit: int = 50) -> list[IntegrationRun]:
        return list(
            self._session.execute(
                select(IntegrationRun)
                .where(IntegrationRun.system == system)
                .order_by(IntegrationRun.started_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    def get_run(self, run_id: uuid.UUID) -> IntegrationRun | None:
        return self._session.get(IntegrationRun, run_id)

    def list_unresolved_mappings(
        self, system: str = "dhis2", limit: int = 200
    ) -> list[IntegrationMappingProposal]:
        """Unresolved mappings, most frequent first.

        Ordered by occurrences because a UID appearing in every weekly pull is
        a more urgent configuration gap than one that appeared once, and an
        operator reading an unordered list of thousands stops reading.
        """
        return list(
            self._session.execute(
                select(IntegrationMappingProposal)
                .where(
                    IntegrationMappingProposal.system == system,
                    IntegrationMappingProposal.proposal_status == MappingProposalStatus.PROPOSED,
                )
                .order_by(IntegrationMappingProposal.occurrences.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )


__all__ = ["IntegrationStatusService"]
