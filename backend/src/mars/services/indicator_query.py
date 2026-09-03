"""Reading indicator definitions and results for the API.

A service rather than router code, for the ordinary reason: routers speak in
response schemas and hold no queries (ADR 0002).

**Scope is applied as a filter on the query, not on the results.** A caller
whose scope does not contain a geography unit gets a refusal, not a shorter
list - otherwise a caller could probe for which districts exist by watching a
list change length.

**The service returns plain dictionaries, not ORM rows.** ADR 0002 keeps ORM
models out of the API layer, and handing a router a mapped object makes the
boundary a matter of discipline rather than of structure.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from mars.domain.enums import GeographyGrain
from mars.domain.indicator import (
    IndicatorDefinition,
    IndicatorDefinitionVersion,
    IndicatorResult,
)


class IndicatorQueryService:
    """Reads the registry and the materialised results."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_definitions(self) -> list[IndicatorDefinition]:
        return list(
            self._session.execute(
                select(IndicatorDefinition)
                .options(selectinload(IndicatorDefinition.versions))
                .order_by(IndicatorDefinition.code)
            )
            .scalars()
            .all()
        )

    def get_definition(self, code: str) -> IndicatorDefinition | None:
        return self._session.execute(
            select(IndicatorDefinition)
            .options(selectinload(IndicatorDefinition.versions))
            .where(IndicatorDefinition.code == code)
        ).scalar_one_or_none()

    def version_count(self, definition_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                select(func.count())
                .select_from(IndicatorDefinitionVersion)
                .where(IndicatorDefinitionVersion.indicator_definition_id == definition_id)
            ).scalar_one()
        )

    def summary(
        self,
        *,
        codes: list[str] | None = None,
        grain: GeographyGrain | None = None,
        geography_unit_ids: list[uuid.UUID] | None = None,
        facility_ids: list[uuid.UUID] | None = None,
        period_from: date | None = None,
        period_to: date | None = None,
        limit: int = 500,
    ) -> list[IndicatorResult]:
        """Materialised figures matching a scope.

        ``geography_unit_ids`` and ``facility_ids`` are the caller's *allowed*
        set, resolved by the router before this is called. Passing an empty
        list means an empty scope and returns nothing, which is correct: a
        caller with no geography scope can see no geography.
        """
        query = select(IndicatorResult)

        if codes:
            query = query.where(IndicatorResult.indicator_code.in_(codes))
        if grain is not None:
            query = query.where(IndicatorResult.geography_grain == grain)
        scope_predicates = []
        if geography_unit_ids is not None:
            scope_predicates.append(IndicatorResult.geography_unit_id.in_(geography_unit_ids))
        if facility_ids is not None:
            scope_predicates.append(IndicatorResult.facility_id.in_(facility_ids))
        if scope_predicates:
            query = query.where(or_(*scope_predicates))
        if period_from is not None:
            query = query.where(IndicatorResult.period_start >= period_from)
        if period_to is not None:
            query = query.where(IndicatorResult.period_end <= period_to)

        return list(
            self._session.execute(
                query.order_by(
                    IndicatorResult.period_start.desc(), IndicatorResult.indicator_code
                ).limit(limit)
            )
            .scalars()
            .all()
        )

    # -- Presentation shapes ------------------------------------------------
    #
    # Returned as dictionaries so the API layer never holds an ORM row. A
    # router that could reach ``definition.versions`` would eventually issue a
    # query, and ADR 0002 exists because that is invisible until it is slow.
    @staticmethod
    def _version_shape(version: IndicatorDefinitionVersion) -> dict[str, object]:
        return {
            "id": version.id,
            "version_number": version.version_number,
            "semantic_version": version.semantic_version,
            "status": version.status.value,
            "blank_handling": version.blank_handling,
            "specification_checksum": version.specification_checksum,
            "numerator_specification": version.numerator_specification,
            "denominator_specification": version.denominator_specification,
            "permitted_dimensions": version.permitted_dimensions,
            "exclusion_rules": version.exclusion_rules,
            "effective_from": version.effective_from,
            "effective_to": version.effective_to,
            "approved_by": version.approved_by,
            "notes": version.notes,
        }

    def definition_shape(self, definition: IndicatorDefinition) -> dict[str, object]:
        active = definition.active_version
        return {
            "id": definition.id,
            "code": definition.code,
            "label": definition.label,
            "purpose": definition.purpose,
            "interpretation": definition.interpretation,
            "unit": definition.unit.value,
            "source_domain": definition.source_domain.value,
            "period_grain": definition.period_grain.value,
            "base_geography_grain": definition.base_geography_grain.value,
            "evidence_lane": definition.evidence_lane.value,
            "definition_source": definition.definition_source,
            "active_version": self._version_shape(active) if active else None,
            "version_count": len(definition.versions),
        }

    @staticmethod
    def result_shape(result: IndicatorResult) -> dict[str, object]:
        return {
            "id": result.id,
            "indicator_code": result.indicator_code,
            "geography_grain": result.geography_grain.value,
            "geography_unit_id": result.geography_unit_id,
            "facility_id": result.facility_id,
            "period_start": result.period_start,
            "period_end": result.period_end,
            "period_grain": result.period_grain.value,
            "age_band": result.age_band.value,
            "sex": result.sex.value,
            "numerator": result.numerator,
            "denominator": result.denominator,
            # Null stays null. Rendering an unavailable value as zero is the
            # one transformation this layer must never make.
            "value": float(result.value) if result.value is not None else None,
            "value_status": result.value_status.value,
            "contributing_units": result.contributing_units,
            "expected_units": result.expected_units,
            "missing_inputs": result.missing_inputs,
            "quality_context": result.quality_context,
            "source_cutoff": result.source_cutoff,
            "boundary_version_id": result.boundary_version_id,
            "engine_version": result.engine_version,
            "computed_at": result.computed_at,
        }


__all__ = ["IndicatorQueryService"]
