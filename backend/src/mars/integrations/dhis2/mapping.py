"""Resolving DHIS2 identifiers against the MARS crosswalks.

A DHIS2 UID is never a MARS key. It lives in ``geography_unit_alias`` or
``facility_identifier`` beside every other source system's code, and a MARS
geography unit keeps its own identity when DHIS2 renumbers, merges or renames.

**Nothing here matches by name.** Two Ugandan districts with similar names are
exactly the case a fuzzy match gets wrong, and the failure is invisible: the
figures still load, under the wrong district, and look entirely plausible. An
unresolved UID becomes a proposal a person has to answer.

The whole module is therefore short, and that is the point. Resolution is a
dictionary lookup against an explicitly recorded mapping, or it is a refusal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.domain.enums import AliasMatchStatus, MappingProposalStatus
from mars.domain.geography import GeographyUnitAlias
from mars.domain.integration import IntegrationMappingProposal
from mars.domain.organisation import Facility, FacilityIdentifier

#: The crosswalk's name for DHIS2 rows. Matches the value the geography alias
#: model's own docstring already anticipates.
SOURCE_SYSTEM = "dhis2"

REMOTE_TYPE_ORGANISATION_UNIT = "organisation_unit"
REMOTE_TYPE_DATA_ELEMENT = "data_element"
REMOTE_TYPE_DATASET = "dataset"
REMOTE_TYPE_CATEGORY_OPTION_COMBO = "category_option_combo"


@dataclass(frozen=True, slots=True)
class ResolvedUnit:
    """What a remote organisation-unit UID turned out to be.

    A UID can map to a geography unit, a facility, or both - DHIS2 keeps
    districts and health units in one hierarchy while MARS keeps administrative
    geography and the facility master apart. Both slots are returned so the
    caller can say which it needed rather than being handed a guess.
    """

    geography_unit_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None

    @property
    def is_resolved(self) -> bool:
        return self.geography_unit_id is not None or self.facility_id is not None


class Dhis2Crosswalk:
    """Looks DHIS2 identifiers up, and records the ones it cannot."""

    def __init__(self, session: Session, *, system: str = SOURCE_SYSTEM) -> None:
        self._session = session
        self._system = system
        self._geography: dict[str, uuid.UUID] | None = None
        self._facility: dict[str, uuid.UUID] | None = None

    # -- Lookups -----------------------------------------------------------
    def resolve_organisation_unit(self, remote_id: str) -> ResolvedUnit:
        """The MARS geography unit and/or facility a UID maps to.

        Only mappings a person has accepted count. A ``proposed`` alias is a
        question that has not been answered, and treating it as an answer would
        make the proposal mechanism decorative.
        """
        return ResolvedUnit(
            geography_unit_id=self._geography_map().get(remote_id),
            facility_id=self._facility_map().get(remote_id),
        )

    def _geography_map(self) -> dict[str, uuid.UUID]:
        if self._geography is None:
            rows = self._session.execute(
                select(GeographyUnitAlias.source_code, GeographyUnitAlias.geography_unit_id).where(
                    GeographyUnitAlias.source_system == self._system,
                    GeographyUnitAlias.match_status == AliasMatchStatus.CONFIRMED,
                )
            ).all()
            self._geography = dict(rows)  # type: ignore[arg-type]
        return self._geography

    def _facility_map(self) -> dict[str, uuid.UUID]:
        if self._facility is None:
            rows = self._session.execute(
                select(FacilityIdentifier.external_id, FacilityIdentifier.facility_id)
                .join(Facility, Facility.id == FacilityIdentifier.facility_id)
                .where(
                    FacilityIdentifier.source_system == self._system,
                    Facility.is_active.is_(True),
                )
            ).all()
            self._facility = dict(rows)  # type: ignore[arg-type]
        return self._facility

    def invalidate(self) -> None:
        """Forget the cached maps.

        Called after a governance action promotes a proposal, so a long-running
        process picks the new mapping up without a restart.
        """
        self._geography = None
        self._facility = None

    # -- Proposals ---------------------------------------------------------
    def record_unresolved(
        self,
        *,
        remote_type: str,
        remote_id: str,
        remote_name: str | None = None,
        remote_parent_id: str | None = None,
        run_id: uuid.UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> IntegrationMappingProposal:
        """Record a remote identifier MARS could not place.

        Upserted rather than appended: a UID that appears in every weekly pull
        is one configuration gap seen fifty times, not fifty gaps, and an
        operator reading a list of thousands stops reading.
        """
        now = datetime.now(UTC)
        existing = self._session.execute(
            select(IntegrationMappingProposal).where(
                IntegrationMappingProposal.system == self._system,
                IntegrationMappingProposal.remote_type == remote_type,
                IntegrationMappingProposal.remote_id == remote_id,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.occurrences += 1
            existing.last_seen_at = now
            existing.remote_name = remote_name or existing.remote_name
            existing.remote_parent_id = remote_parent_id or existing.remote_parent_id
            if detail:
                existing.detail = detail
            # A previously rejected mapping that reappears is worth re-asking:
            # the remote system is still sending it, so the decision has not
            # taken effect over there.
            if existing.proposal_status is MappingProposalStatus.REJECTED:
                existing.proposal_status = MappingProposalStatus.PROPOSED
            if run_id is not None:
                existing.integration_run_id = run_id
            return existing

        proposal = IntegrationMappingProposal(
            integration_run_id=run_id,
            system=self._system,
            remote_type=remote_type,
            remote_id=remote_id,
            remote_name=remote_name,
            remote_parent_id=remote_parent_id,
            proposal_status=MappingProposalStatus.PROPOSED,
            occurrences=1,
            first_seen_at=now,
            last_seen_at=now,
            detail=detail,
        )
        self._session.add(proposal)
        return proposal


__all__ = [
    "REMOTE_TYPE_CATEGORY_OPTION_COMBO",
    "REMOTE_TYPE_DATASET",
    "REMOTE_TYPE_DATA_ELEMENT",
    "REMOTE_TYPE_ORGANISATION_UNIT",
    "SOURCE_SYSTEM",
    "Dhis2Crosswalk",
    "ResolvedUnit",
]
