"""Geography read services, with geography scoping applied in the query.

Scoping is a WHERE clause, not a post-filter. A district user's query never
returns another district's rows, so there is no window in which out-of-scope
data exists in the process.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, false, or_, select
from sqlalchemy.orm import Session

from mars.core.errors import GeographyScopeDeniedError, NotFoundError
from mars.domain.enums import GeographyLevel
from mars.domain.geography import BoundaryVersion, GeographyUnit, GeographyUnitAlias
from mars.security.principal import AuthenticatedPrincipal


class GeographyService:
    """Reads the administrative hierarchy within the caller's scope."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- Scope predicate ---------------------------------------------------
    def _apply_scope(
        self, statement: Select[tuple[GeographyUnit]], principal: AuthenticatedPrincipal
    ) -> Select[tuple[GeographyUnit]]:
        """Restrict a geography query to the principal's scope.

        National scope passes everything. Otherwise a unit is visible when it is
        a scope root, a descendant of one by materialised path, or an ancestor of
        one - ancestors are included so a district user can still resolve the
        breadcrumb "Uganda / Northern / Gulu" without seeing sibling districts.
        """
        if principal.has_national_scope:
            return statement

        scopes = principal.geography_scopes
        if not scopes:
            # No scope means no access. An empty scope is never "everywhere".
            return statement.where(false())

        conditions = []
        for scope in scopes:
            conditions.append(GeographyUnit.id == scope.geography_unit_id)
            if scope.path:
                # Descendants: path begins with the scope path plus a separator.
                conditions.append(GeographyUnit.path.like(f"{scope.path}/%"))
                # Ancestors: the scope path begins with the candidate's path.
                for ancestor_path in _ancestor_paths(scope.path):
                    conditions.append(GeographyUnit.path == ancestor_path)

        return statement.where(or_(*conditions))

    # -- Reads -------------------------------------------------------------
    def list_units(
        self,
        principal: AuthenticatedPrincipal,
        *,
        level: GeographyLevel | None = None,
        parent_id: uuid.UUID | None = None,
        active_only: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> list[GeographyUnit]:
        statement: Select[tuple[GeographyUnit]] = select(GeographyUnit)
        if level is not None:
            statement = statement.where(GeographyUnit.level == level)
        if parent_id is not None:
            statement = statement.where(GeographyUnit.parent_id == parent_id)
        if active_only:
            statement = statement.where(GeographyUnit.is_active.is_(True))

        statement = self._apply_scope(statement, principal)
        statement = (
            statement.order_by(GeographyUnit.depth, GeographyUnit.normalised_name)
            .limit(limit)
            .offset(offset)
        )

        return list(self._session.execute(statement).scalars().all())

    def get_unit(self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID) -> GeographyUnit:
        unit = self._session.get(GeographyUnit, unit_id)
        if unit is None:
            raise NotFoundError("geography unit not found")
        if not principal.covers_geography(unit.id, unit.path):
            raise GeographyScopeDeniedError(
                "the requested area is outside your assigned geography scope"
            )
        return unit

    def get_unit_by_code(
        self, principal: AuthenticatedPrincipal, level: GeographyLevel, code: str
    ) -> GeographyUnit:
        unit = self._session.execute(
            select(GeographyUnit).where(
                GeographyUnit.level == level, GeographyUnit.preferred_code == code
            )
        ).scalar_one_or_none()
        if unit is None:
            raise NotFoundError(f"no {level.value} with code {code!r}")
        if not principal.covers_geography(unit.id, unit.path):
            raise GeographyScopeDeniedError(
                "the requested area is outside your assigned geography scope"
            )
        return unit

    def children_of(
        self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID
    ) -> list[GeographyUnit]:
        parent = self.get_unit(principal, unit_id)
        statement = select(GeographyUnit).where(GeographyUnit.parent_id == parent.id)
        statement = self._apply_scope(statement, principal)
        statement = statement.order_by(GeographyUnit.normalised_name)
        return list(self._session.execute(statement).scalars().all())

    def ancestors_of(
        self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID
    ) -> list[GeographyUnit]:
        """Walk up to the country, for breadcrumbs.

        Bounded by hierarchy depth rather than by a while-true, so a cycle
        introduced by a bad import cannot hang a request.
        """
        unit = self.get_unit(principal, unit_id)
        chain: list[GeographyUnit] = []
        current = unit
        for _ in range(len(GeographyLevel)):
            if current.parent_id is None:
                break
            parent = self._session.get(GeographyUnit, current.parent_id)
            if parent is None or parent.id == current.id:
                break
            chain.append(parent)
            current = parent
        return list(reversed(chain))

    def level_counts(self, principal: AuthenticatedPrincipal) -> dict[str, int]:
        """Unit counts per level within scope.

        Levels with no data are reported as zero rather than omitted, so an
        empty parish level reads as "none loaded" and not as "level missing".
        """
        counts = {level.value: 0 for level in GeographyLevel}
        statement = self._apply_scope(select(GeographyUnit), principal)
        for unit in self._session.execute(statement).scalars():
            counts[unit.level.value] += 1
        return counts

    def find_by_alias(self, source_system: str, source_code: str) -> list[GeographyUnitAlias]:
        """Resolve a source system's code. Returns every candidate.

        Multiple rows mean the mapping is ambiguous and must be reviewed. The
        service does not choose one; blueprint appendix 120 requires ambiguous
        source values to stay unresolved.
        """
        return list(
            self._session.execute(
                select(GeographyUnitAlias).where(
                    GeographyUnitAlias.source_system == source_system,
                    GeographyUnitAlias.source_code == source_code,
                )
            )
            .scalars()
            .all()
        )

    def list_boundary_versions(self) -> list[BoundaryVersion]:
        return list(
            self._session.execute(
                select(BoundaryVersion).order_by(BoundaryVersion.created_at.desc())
            )
            .scalars()
            .all()
        )


def _ancestor_paths(path: str) -> list[str]:
    """Every proper ancestor path of a materialised path.

    ``UG/3/314`` yields ``["UG", "UG/3"]``.
    """
    segments = path.split("/")
    return ["/".join(segments[: i + 1]) for i in range(len(segments) - 1)]
