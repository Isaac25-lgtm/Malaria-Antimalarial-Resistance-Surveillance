"""Registering, versioning and approving indicator definitions.

Registration and approval are separate acts, and this module keeps them
separate. Seeding puts the shipped catalogue into the database as **drafts**; a
programme approves a version, and only then can it produce a published figure.

That ordering is what lets MARS ship a complete registry with no approved
parameters: a deployment that has not reviewed anything gets a readable
catalogue that computes nothing, which is the right behaviour for figures a
district will act on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mars.analytics.indicator_catalogue import CATALOGUE, CatalogueEntry
from mars.core.logging import get_logger
from mars.domain.enums import LifecycleStatus
from mars.domain.indicator import IndicatorDefinition, IndicatorDefinitionVersion

logger = get_logger(__name__)


class IndicatorApprovalError(RuntimeError):
    """An approval was requested that governance does not permit."""


@dataclass(slots=True)
class SeedReport:
    """What a seeding run did."""

    definitions_created: int = 0
    definitions_unchanged: int = 0
    versions_created: int = 0
    versions_superseded: int = 0
    approved_left_alone: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "definitions_created": self.definitions_created,
            "definitions_unchanged": self.definitions_unchanged,
            "versions_created": self.versions_created,
            "versions_superseded": self.versions_superseded,
            "approved_left_alone": self.approved_left_alone,
        }


class IndicatorRegistryService:
    """Reads and maintains the indicator registry."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Seeding -----------------------------------------------------------
    def seed_catalogue(self, entries: tuple[CatalogueEntry, ...] = CATALOGUE) -> SeedReport:
        """Register the shipped catalogue as drafts.

        Idempotent, and deliberately conservative about anything a person has
        touched:

        * a definition that already exists keeps its identity and its history;
        * a version whose specification checksum already matches is left alone;
        * a **changed** specification creates a new draft version rather than
          editing the old one, because the old one may already have produced
          figures somebody acted on;
        * an approved or active version is never demoted or overwritten. If the
          shipped specification has since changed, the new draft sits beside it
          and waits for the programme to decide.
        """
        report = SeedReport()

        for entry in entries:
            definition = self._session.execute(
                select(IndicatorDefinition).where(IndicatorDefinition.code == entry.code)
            ).scalar_one_or_none()

            if definition is None:
                definition = IndicatorDefinition(
                    code=entry.code,
                    label=entry.label,
                    purpose=entry.purpose,
                    interpretation=entry.interpretation,
                    unit=entry.unit,
                    source_domain=entry.source_domain,
                    period_grain=entry.period_grain,
                    base_geography_grain=entry.base_geography_grain,
                    evidence_lane=entry.evidence_lane,
                    definition_source=entry.definition_source,
                )
                self._session.add(definition)
                self._session.flush()
                report.definitions_created += 1
            else:
                report.definitions_unchanged += 1

            existing_versions = (
                self._session.execute(
                    select(IndicatorDefinitionVersion)
                    .where(IndicatorDefinitionVersion.indicator_definition_id == definition.id)
                    .order_by(IndicatorDefinitionVersion.version_number)
                )
                .scalars()
                .all()
            )

            if any(v.specification_checksum == entry.checksum for v in existing_versions):
                # This exact specification is already registered, whatever its
                # lifecycle state. Nothing to do.
                if any(
                    v.specification_checksum == entry.checksum
                    and v.status in {LifecycleStatus.APPROVED, LifecycleStatus.ACTIVE}
                    for v in existing_versions
                ):
                    report.approved_left_alone += 1
                continue

            next_number = max((v.version_number for v in existing_versions), default=0) + 1
            self._session.add(
                IndicatorDefinitionVersion(
                    indicator_definition_id=definition.id,
                    version_number=next_number,
                    semantic_version=f"{next_number}.0.0",
                    # Draft. Registering a definition and putting it in force
                    # are different acts, and only one of them is MARS's.
                    status=LifecycleStatus.DRAFT,
                    numerator_specification=entry.numerator,
                    denominator_specification=entry.denominator,
                    blank_handling=entry.blank_handling,
                    exclusion_rules=entry.exclusions,
                    permitted_dimensions=entry.permitted_dimensions,
                    specification_checksum=entry.checksum,
                    reason_for_change=entry.reason_for_change,
                    notes=entry.notes,
                )
            )
            report.versions_created += 1
            if existing_versions:
                report.versions_superseded += 1

        self._session.flush()
        logger.info("indicator_catalogue_seeded", **report.as_dict())
        return report

    # -- Reading -----------------------------------------------------------
    def list_definitions(self) -> list[IndicatorDefinition]:
        return list(
            self._session.execute(select(IndicatorDefinition).order_by(IndicatorDefinition.code))
            .scalars()
            .all()
        )

    def get_definition(self, code: str) -> IndicatorDefinition | None:
        return self._session.execute(
            select(IndicatorDefinition).where(IndicatorDefinition.code == code)
        ).scalar_one_or_none()

    def active_version(self, code: str) -> IndicatorDefinitionVersion | None:
        """The version in force for a code, or ``None``.

        ``None`` is not an error. It means the programme has not approved a
        definition, and every caller must treat that as "this indicator cannot
        be computed" rather than falling back to a draft.
        """
        definition = self.get_definition(code)
        return definition.active_version if definition else None

    def active_versions(self) -> dict[str, IndicatorDefinitionVersion]:
        rows = (
            self._session.execute(
                select(IndicatorDefinitionVersion, IndicatorDefinition.code)
                .join(
                    IndicatorDefinition,
                    IndicatorDefinition.id == IndicatorDefinitionVersion.indicator_definition_id,
                )
                .where(IndicatorDefinitionVersion.status == LifecycleStatus.ACTIVE)
            )
            .tuples()
            .all()
        )
        return {code: version for version, code in rows}

    # -- Lifecycle ---------------------------------------------------------
    def approve_version(
        self,
        version_id: uuid.UUID,
        *,
        approved_by: str,
        effective_from: date | None = None,
    ) -> IndicatorDefinitionVersion:
        """Move a draft to approved.

        Requires a named approver. The database enforces it too, but failing
        here gives a usable message instead of a constraint violation.
        """
        version = self._session.get(IndicatorDefinitionVersion, version_id)
        if version is None:
            raise IndicatorApprovalError("No such indicator definition version.")
        if not approved_by.strip():
            raise IndicatorApprovalError(
                "An approver must be named. An active definition with nobody's "
                "name on it is an ungoverned definition."
            )
        if version.status not in {LifecycleStatus.DRAFT, LifecycleStatus.IN_REVIEW}:
            raise IndicatorApprovalError(
                f"Only a draft or in-review version can be approved; this one is "
                f"{version.status.value}."
            )

        version.status = LifecycleStatus.APPROVED
        version.approved_by = approved_by
        version.approved_at = datetime.now(UTC)
        version.effective_from = effective_from
        self._session.flush()
        return version

    def activate_version(self, version_id: uuid.UUID) -> IndicatorDefinitionVersion:
        """Put an approved version in force, retiring the one it replaces.

        Exactly one version of a definition is active at a time. The previous
        one is **retired**, not deleted: figures computed under it are still in
        the database and still have to be explicable.
        """
        version = self._session.get(IndicatorDefinitionVersion, version_id)
        if version is None:
            raise IndicatorApprovalError("No such indicator definition version.")
        if version.status is not LifecycleStatus.APPROVED:
            raise IndicatorApprovalError(
                f"Only an approved version can be activated; this one is {version.status.value}."
            )

        current = (
            self._session.execute(
                select(IndicatorDefinitionVersion).where(
                    IndicatorDefinitionVersion.indicator_definition_id
                    == version.indicator_definition_id,
                    IndicatorDefinitionVersion.status == LifecycleStatus.ACTIVE,
                )
            )
            .scalars()
            .all()
        )
        for previous in current:
            previous.status = LifecycleStatus.RETIRED
            previous.effective_to = date.today()

        # Flushed before promotion so the database never holds two active
        # versions of one definition, even momentarily.
        self._session.flush()
        version.status = LifecycleStatus.ACTIVE
        self._session.flush()
        return version


__all__ = ["IndicatorApprovalError", "IndicatorRegistryService", "SeedReport"]
