"""Organisation unit and facility services, with scoping applied in the query.

Facility visibility is two independent restrictions, both enforced here:

* **Geography** - a facility is visible only when its district lies inside the
  caller's geography scope.
* **Facility scope** - a facility user additionally sees only the facilities
  named on their account, not every facility in their district.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, and_, false, or_, select
from sqlalchemy.orm import Session, aliased

from mars.core.errors import GeographyScopeDeniedError, NotFoundError, ValidationFailedError
from mars.domain.enums import FacilityLevel, OrganisationUnitType
from mars.domain.geography import GeographyUnit
from mars.domain.organisation import Facility, FacilityIdentifier, OrganisationUnit
from mars.security.principal import AuthenticatedPrincipal

#: Guards against a cycle introduced by a bad import. The health-sector
#: hierarchy is shallow; anything deeper is a defect, not a deep structure.
MAX_HIERARCHY_DEPTH = 8


class OrganisationService:
    """Reads the health-sector organisational hierarchy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_units(
        self,
        principal: AuthenticatedPrincipal,
        *,
        unit_type: OrganisationUnitType | None = None,
        parent_id: uuid.UUID | None = None,
        active_only: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> list[OrganisationUnit]:
        statement: Select[tuple[OrganisationUnit]] = select(OrganisationUnit)
        if unit_type is not None:
            statement = statement.where(OrganisationUnit.unit_type == unit_type)
        if parent_id is not None:
            statement = statement.where(OrganisationUnit.parent_id == parent_id)
        if active_only:
            statement = statement.where(OrganisationUnit.is_active.is_(True))

        statement = self._apply_scope(statement, principal)
        statement = (
            statement.order_by(OrganisationUnit.depth, OrganisationUnit.normalised_name)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(statement).scalars().all())

    def get_unit(self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID) -> OrganisationUnit:
        statement = self._apply_scope(
            select(OrganisationUnit).where(OrganisationUnit.id == unit_id), principal
        )
        unit = self._session.execute(statement).scalar_one_or_none()
        if unit is None:
            raise NotFoundError("organisation unit not found or outside your assigned scope")
        return unit

    def _apply_scope(
        self, statement: Select[tuple[OrganisationUnit]], principal: AuthenticatedPrincipal
    ) -> Select[tuple[OrganisationUnit]]:
        if principal.has_national_scope:
            return statement
        if not principal.geography_scopes:
            return statement.where(false())

        geography = aliased(GeographyUnit)
        conditions = []
        for scope in principal.geography_scopes:
            conditions.append(OrganisationUnit.primary_geography_unit_id == scope.geography_unit_id)
            if scope.path:
                conditions.append(geography.path.like(f"{scope.path}/%"))

        # Only explicitly national units may be globally visible without a
        # geography link. Treating every unlinked row as national would expose
        # an accidentally unlinked district or HSD to all scoped users.
        conditions.append(
            and_(
                OrganisationUnit.primary_geography_unit_id.is_(None),
                OrganisationUnit.unit_type == OrganisationUnitType.NATIONAL,
            )
        )

        return statement.outerjoin(
            geography, geography.id == OrganisationUnit.primary_geography_unit_id
        ).where(or_(*conditions))

    def _assert_in_scope(self, principal: AuthenticatedPrincipal, unit: OrganisationUnit) -> None:
        if principal.has_national_scope:
            return
        if unit.primary_geography_unit_id is None:
            if unit.unit_type == OrganisationUnitType.NATIONAL:
                return
            raise GeographyScopeDeniedError(
                "this unlinked organisation unit is outside your assigned geography scope"
            )
        geography = self._session.get(GeographyUnit, unit.primary_geography_unit_id)
        path = geography.path if geography else None
        if not principal.covers_geography(unit.primary_geography_unit_id, path):
            raise GeographyScopeDeniedError(
                "this organisation unit is outside your assigned geography scope"
            )

    def ancestors_of(
        self, principal: AuthenticatedPrincipal, unit_id: uuid.UUID
    ) -> list[OrganisationUnit]:
        unit = self.get_unit(principal, unit_id)
        chain: list[OrganisationUnit] = []
        current = unit
        for _ in range(MAX_HIERARCHY_DEPTH):
            if current.parent_id is None:
                break
            try:
                parent = self.get_unit(principal, current.parent_id)
            except NotFoundError:
                break
            if parent.id == current.id:
                break
            chain.append(parent)
            current = parent
        return list(reversed(chain))


class FacilityService:
    """Reads facility metadata within the caller's geography and facility scope."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _apply_scope(
        self, statement: Select[tuple[Facility]], principal: AuthenticatedPrincipal
    ) -> Select[tuple[Facility]]:
        # Facility and geography are independent restrictions. They must be
        # intersected; a mistaken cross-district facility assignment must not
        # bypass the user's geography scope.
        if principal.is_facility_restricted:
            statement = statement.where(Facility.id.in_(principal.facility_scopes))

        if principal.has_national_scope:
            return statement

        if not principal.geography_scopes:
            return statement.where(false())

        district = aliased(GeographyUnit)
        conditions = []
        for scope in principal.geography_scopes:
            conditions.append(Facility.district_geography_unit_id == scope.geography_unit_id)
            conditions.append(Facility.subcounty_geography_unit_id == scope.geography_unit_id)
            if scope.path:
                conditions.append(district.path.like(f"{scope.path}/%"))
                conditions.append(district.path == scope.path)

        return statement.outerjoin(
            district, district.id == Facility.district_geography_unit_id
        ).where(or_(*conditions))

    def list_facilities(
        self,
        principal: AuthenticatedPrincipal,
        *,
        district_id: uuid.UUID | None = None,
        subcounty_id: uuid.UUID | None = None,
        organisation_unit_id: uuid.UUID | None = None,
        facility_level: FacilityLevel | None = None,
        active_only: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Facility]:
        statement: Select[tuple[Facility]] = select(Facility)
        if district_id is not None:
            statement = statement.where(Facility.district_geography_unit_id == district_id)
        if subcounty_id is not None:
            statement = statement.where(Facility.subcounty_geography_unit_id == subcounty_id)
        if organisation_unit_id is not None:
            statement = statement.where(Facility.organisation_unit_id == organisation_unit_id)
        if facility_level is not None:
            statement = statement.where(Facility.facility_level == facility_level)
        if active_only:
            statement = statement.where(Facility.is_active.is_(True))

        statement = self._apply_scope(statement, principal)
        statement = statement.order_by(Facility.normalised_name).limit(limit).offset(offset)
        return list(self._session.execute(statement).scalars().all())

    def get_facility(self, principal: AuthenticatedPrincipal, facility_id: uuid.UUID) -> Facility:
        statement = self._apply_scope(select(Facility).where(Facility.id == facility_id), principal)
        facility = self._session.execute(statement).scalar_one_or_none()
        if facility is None:
            raise NotFoundError("facility not found or outside your assigned scope")
        return facility

    def create_facility(
        self,
        *,
        code: str,
        raw_name: str,
        normalised_name: str,
        facility_level: FacilityLevel = FacilityLevel.UNKNOWN,
        district_geography_unit_id: uuid.UUID | None = None,
        subcounty_geography_unit_id: uuid.UUID | None = None,
        organisation_unit_id: uuid.UUID | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        coordinate_source: str | None = None,
        is_synthetic: bool = False,
        source_system: str | None = None,
        source_record_id: str | None = None,
    ) -> Facility:
        """Create a facility record.

        A coordinate may only be stored together with a statement of where it
        came from. MARS does not hold an unattributed point, because an
        unattributed point cannot be verified or corrected later.
        """
        if (latitude is None) != (longitude is None):
            raise ValidationFailedError("latitude and longitude must be supplied together")
        if latitude is not None and not coordinate_source:
            raise ValidationFailedError(
                "a coordinate requires coordinate_source naming where it came from"
            )

        facility = Facility(
            code=code,
            raw_name=raw_name,
            normalised_name=normalised_name,
            facility_level=facility_level,
            district_geography_unit_id=district_geography_unit_id,
            subcounty_geography_unit_id=subcounty_geography_unit_id,
            organisation_unit_id=organisation_unit_id,
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source,
            is_synthetic=is_synthetic,
            source_system=source_system,
            source_record_id=source_record_id,
        )
        self._session.add(facility)
        self._session.flush()
        return facility

    def identifiers_for(
        self, principal: AuthenticatedPrincipal, facility_id: uuid.UUID
    ) -> list[FacilityIdentifier]:
        self.get_facility(principal, facility_id)
        return list(
            self._session.execute(
                select(FacilityIdentifier).where(FacilityIdentifier.facility_id == facility_id)
            )
            .scalars()
            .all()
        )
