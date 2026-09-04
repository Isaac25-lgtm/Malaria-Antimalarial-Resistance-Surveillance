"""Dynamic DHIS2 scope resolution. Usernames never decide geography."""

from __future__ import annotations

import uuid

from mars.integrations.dhis2.login.models import (
    LoginSnapshot,
    RemoteOrgUnit,
    RemoteOrgUnitLevel,
)
from mars.security.principal import GeographyScope
from mars.services.live_scope import (
    StaticGeographyLookup,
    build_live_principal,
    landing_path_for_scope,
    resolve_live_scope,
)

COUNTRY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
PADER_ID = uuid.UUID("00000000-0000-4000-8000-000000000312")
GULU_ID = uuid.UUID("00000000-0000-4000-8000-000000000304")
FACILITY_ID = uuid.UUID("00000000-0000-4000-8000-00000000f001")

COUNTRY = GeographyScope(
    geography_unit_id=COUNTRY_ID,
    preferred_code="UG",
    level="country",
    name="Uganda",
    path="UG",
)
PADER = GeographyScope(
    geography_unit_id=PADER_ID,
    preferred_code="312",
    level="district",
    name="Pader",
    path="UG/3/312",
)
GULU = GeographyScope(
    geography_unit_id=GULU_ID,
    preferred_code="304",
    level="district",
    name="Gulu",
    path="UG/3/304",
)

LEVELS = (
    RemoteOrgUnitLevel(1, "Country"),
    RemoteOrgUnitLevel(2, "Region"),
    RemoteOrgUnitLevel(3, "District"),
    RemoteOrgUnitLevel(4, "Facility"),
)

LOOKUP = StaticGeographyLookup(
    uids={
        "UgRootUid": COUNTRY,
        "PaderUid": PADER,
        "GuluUid": GULU,
    },
    facilities={"FacUid": FACILITY_ID},
    codes={("312", "district"): PADER, ("304", "district"): GULU},
)


def _snapshot(
    *,
    username: str,
    units: tuple[RemoteOrgUnit, ...],
    remote_user_id: str = "User1",
) -> LoginSnapshot:
    return LoginSnapshot(
        remote_user_id=remote_user_id,
        username=username,
        display_name=username,
        authorities=(),
        organisation_units=units,
        data_view_organisation_units=(),
        tei_search_organisation_units=(),
        organisation_unit_levels=LEVELS,
        organisation_unit_groups=(),
        system_name="eRegisters",
        system_version="2.40",
        requested_paths=("/api/me",),
    )


def _ou(uid: str, *, level: int, code: str | None = None, name: str = "Unit") -> RemoteOrgUnit:
    return RemoteOrgUnit(uid=uid, name=name, code=code, level=level, path=f"/root/{uid}")


class TestLiveScopeResolver:
    def test_national_root(self) -> None:
        scope = resolve_live_scope(
            _snapshot(username="anyone", units=(_ou("UgRootUid", level=1, name="Uganda"),)),
            LOOKUP,
        )
        assert scope.scope_type == "national"
        assert scope.national_access is True
        assert landing_path_for_scope(scope) == "/command-centre"

    def test_one_district_pader(self) -> None:
        scope = resolve_live_scope(
            _snapshot(username="not-pader", units=(_ou("PaderUid", level=3, code="312"),)),
            LOOKUP,
        )
        assert scope.scope_type == "district"
        assert scope.org_unit_name == "Pader"
        assert scope.national_access is False
        assert landing_path_for_scope(scope) == f"/district/{PADER_ID}"

    def test_another_district_gulu(self) -> None:
        scope = resolve_live_scope(
            _snapshot(username="not-gulu", units=(_ou("GuluUid", level=3, code="304"),)),
            LOOKUP,
        )
        assert scope.scope_type == "district"
        assert scope.org_unit_name == "Gulu"
        assert landing_path_for_scope(scope) == f"/district/{GULU_ID}"

    def test_multiple_districts_are_not_national(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="multi",
                units=(
                    _ou("PaderUid", level=3),
                    _ou("GuluUid", level=3),
                ),
            ),
            LOOKUP,
        )
        assert scope.scope_type == "multi_district"
        assert scope.national_access is False
        assert landing_path_for_scope(scope) == "/authorised-scope"

    def test_facility_only(self) -> None:
        scope = resolve_live_scope(
            _snapshot(username="clinician", units=(_ou("FacUid", level=4),)),
            LOOKUP,
        )
        assert scope.scope_type == "facility"
        assert landing_path_for_scope(scope) == f"/facility/{FACILITY_ID}"

    def test_unresolved_mapping_does_not_become_national(self) -> None:
        scope = resolve_live_scope(
            _snapshot(username="district.pader", units=(_ou("UnknownUid", level=3),)),
            LOOKUP,
        )
        assert scope.scope_type == "unresolved"
        assert scope.mapping_status == "pending"
        assert scope.national_access is False
        assert landing_path_for_scope(scope) == "/no-authorised-scope"

    def test_username_does_not_change_landing(self) -> None:
        units = (_ou("PaderUid", level=3),)
        first = resolve_live_scope(_snapshot(username="district.pader", units=units), LOOKUP)
        second = resolve_live_scope(_snapshot(username="someone.else", units=units), LOOKUP)
        assert landing_path_for_scope(first) == landing_path_for_scope(second)
        assert first.scope_type == second.scope_type == "district"

    def test_name_alone_does_not_map(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="x",
                units=(_ou("Unmapped", level=3, name="Pader", code=None),),
            ),
            LOOKUP,
        )
        assert scope.scope_type == "unresolved"

    def test_principal_is_not_synthetic_and_has_aggregate_ceiling(self) -> None:
        snapshot = _snapshot(username="officer", units=(_ou("PaderUid", level=3),))
        scope = resolve_live_scope(snapshot, LOOKUP)
        principal = build_live_principal(snapshot, scope, session_reference="sid")
        assert principal.is_synthetic is False
        assert principal.auth_method == "dhis2_pilot"
        assert principal.max_sensitivity.name == "AGGREGATE"
        assert "patient:reidentify" not in {p.value for p in principal.permissions}
