"""Remote DHIS2 authorization is independent of local geography mapping.

Usernames never decide geography. Dashboard authorization uses
dataViewOrganisationUnits, not the union of capture and Tracker-search scopes.
"""

from __future__ import annotations

import uuid

from mars.integrations.dhis2.login.models import (
    LoginSnapshot,
    RemoteOrgUnit,
    RemoteOrgUnitLevel,
)
from mars.security.permissions import Permission, SensitivityLevel
from mars.security.principal import GeographyScope
from mars.security.remote_authorization import is_dhis2_uid
from mars.services.live_scope import (
    StaticGeographyLookup,
    build_live_principal,
    landing_path_for_scope,
    permissions_for_scope,
    resolve_live_scope,
)

COUNTRY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
PADER_ID = uuid.UUID("00000000-0000-4000-8000-000000000312")
GULU_ID = uuid.UUID("00000000-0000-4000-8000-000000000304")
FACILITY_ID = uuid.UUID("00000000-0000-4000-8000-00000000f001")

PADER_UID = "PaderDist01"
GULU_UID = "GuluDistr01"
ROOT_UID = "UgandanRoot"
FAC_UID = "Facility001"
UNKNOWN_UID = "UnknownUid0"
TRACKER_UID = "PaderFac001"

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
        ROOT_UID: COUNTRY,
        PADER_UID: PADER,
        GULU_UID: GULU,
    },
    facilities={FAC_UID: FACILITY_ID},
    codes={("312", "district"): PADER, ("304", "district"): GULU},
)

EMPTY_LOOKUP = StaticGeographyLookup()


def _snapshot(
    *,
    username: str,
    data_view: tuple[RemoteOrgUnit, ...] = (),
    capture: tuple[RemoteOrgUnit, ...] = (),
    tracker: tuple[RemoteOrgUnit, ...] = (),
    remote_user_id: str = "User1Uid001",
    data_view_field_present: bool = True,
) -> LoginSnapshot:
    return LoginSnapshot(
        remote_user_id=remote_user_id,
        username=username,
        display_name=username,
        authorities=(),
        organisation_units=capture,
        data_view_organisation_units=data_view,
        tei_search_organisation_units=tracker,
        organisation_unit_levels=LEVELS,
        organisation_unit_groups=(),
        system_name="eRegisters",
        system_version="2.40",
        requested_paths=("/api/me",),
        data_view_field_present=data_view_field_present,
    )


def _ou(
    uid: str,
    *,
    level: int,
    code: str | None = None,
    name: str = "Unit",
    path: str | None = None,
) -> RemoteOrgUnit:
    return RemoteOrgUnit(
        uid=uid,
        name=name,
        code=code,
        level=level,
        path=path or f"/UgandanRoot/{uid}",
        parent_uid="UgandanRoot" if uid != ROOT_UID else None,
    )


class TestLiveScopeResolver:
    def test_national_root(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="anyone",
                data_view=(_ou(ROOT_UID, level=1, name="Uganda"),),
            ),
            LOOKUP,
        )
        assert scope.workspace.status == "resolved"
        assert scope.scope_type == "national"
        assert scope.national_access is True
        assert scope.mapping.status == "resolved"
        assert landing_path_for_scope(scope) == "/command-centre"

    def test_one_district_pader_with_confirmed_alias(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="not-pader",
                data_view=(_ou(PADER_UID, level=3, code="312", name="Pader"),),
            ),
            LOOKUP,
        )
        assert scope.scope_type == "district"
        assert scope.workspace.name == "Pader"
        assert scope.mapping.status == "resolved"
        assert scope.mapping.geography_unit_id == PADER_ID
        assert scope.national_access is False
        assert landing_path_for_scope(scope) == f"/district/{PADER_ID}"
        assert Permission.SURVEILLANCE_VIEW_AGGREGATE in permissions_for_scope(scope)

    def test_another_district_gulu(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="not-gulu",
                data_view=(_ou(GULU_UID, level=3, code="304", name="Gulu"),),
            ),
            LOOKUP,
        )
        assert scope.scope_type == "district"
        assert scope.workspace.name == "Gulu"
        assert landing_path_for_scope(scope) == f"/district/{GULU_ID}"

    def test_multiple_districts_are_not_national(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="multi",
                data_view=(
                    _ou(PADER_UID, level=3, name="Pader"),
                    _ou(GULU_UID, level=3, name="Gulu"),
                ),
            ),
            LOOKUP,
        )
        assert scope.scope_type == "multi_district"
        assert scope.national_access is False
        assert landing_path_for_scope(scope) == "/authorised-scope"

    def test_facility_only(self) -> None:
        scope = resolve_live_scope(
            _snapshot(username="clinician", data_view=(_ou(FAC_UID, level=4, name="HC III"),)),
            LOOKUP,
        )
        assert scope.scope_type == "facility"
        assert scope.mapping.status == "resolved"
        assert landing_path_for_scope(scope) == f"/facility/{FACILITY_ID}"

    def test_remote_district_without_local_alias_is_authorized_pending(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="district.pader",
                data_view=(_ou(UNKNOWN_UID, level=3, name="Pader"),),
            ),
            EMPTY_LOOKUP,
        )
        assert scope.workspace.status == "resolved"
        assert scope.scope_type == "district"
        assert scope.workspace.name == "Pader"
        assert scope.workspace.external_uid == UNKNOWN_UID
        assert scope.mapping.status == "pending"
        assert scope.mapping.geography_unit_id is None
        assert scope.national_access is False
        assert landing_path_for_scope(scope) == f"/live/dhis2/district/{UNKNOWN_UID}"
        assert landing_path_for_scope(scope) != "/no-authorised-scope"
        assert not permissions_for_scope(scope)

    def test_username_does_not_change_landing(self) -> None:
        units = (_ou(PADER_UID, level=3, name="Pader"),)
        first = resolve_live_scope(_snapshot(username="district.pader", data_view=units), LOOKUP)
        second = resolve_live_scope(_snapshot(username="someone.else", data_view=units), LOOKUP)
        assert landing_path_for_scope(first) == landing_path_for_scope(second)
        assert first.scope_type == second.scope_type == "district"

    def test_name_alone_does_not_map(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="x",
                data_view=(_ou(UNKNOWN_UID, level=3, name="Pader", code=None),),
            ),
            LOOKUP,
        )
        assert scope.workspace.status == "resolved"
        assert scope.scope_type == "district"
        assert scope.mapping.status == "pending"
        assert scope.mapping.geography_unit_id is None

    def test_empty_data_view_does_not_use_capture_scope(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="officer",
                data_view=(),
                capture=(_ou(PADER_UID, level=3, name="Pader"),),
                data_view_field_present=True,
            ),
            LOOKUP,
        )
        assert scope.workspace.status == "unresolved"
        assert scope.scope_type == "unresolved"
        assert scope.remote_authorization.fallback_used is False
        assert landing_path_for_scope(scope) == "/no-authorised-scope"
        assert not permissions_for_scope(scope)

    def test_absent_data_view_field_uses_documented_capture_fallback(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="officer",
                data_view=(),
                capture=(_ou(PADER_UID, level=3, name="Pader"),),
                data_view_field_present=False,
            ),
            LOOKUP,
        )
        assert scope.remote_authorization.fallback_used is True
        assert scope.remote_authorization.fallback_source == "organisationUnits"
        assert scope.scope_type == "district"
        assert scope.mapping.status == "resolved"

    def test_tracker_search_does_not_widen_or_narrow_dashboard(self) -> None:
        scope = resolve_live_scope(
            _snapshot(
                username="officer",
                data_view=(_ou(PADER_UID, level=3, name="Pader"),),
                tracker=(_ou(TRACKER_UID, level=4, name="Pader HC III"),),
            ),
            LOOKUP,
        )
        assert scope.scope_type == "district"
        assert scope.workspace.external_uid == PADER_UID
        assert len(scope.remote_authorization.tracker_search_scope) == 1
        assert scope.remote_authorization.tracker_search_scope[0].uid == TRACKER_UID
        assert (
            scope.remote_authorization.tracker_search_scope[0].uid != scope.workspace.external_uid
        )
        assert all(unit.uid != TRACKER_UID for unit in scope.remote_authorization.data_view_scope)
        principal = build_live_principal(
            _snapshot(
                username="officer",
                data_view=(_ou(PADER_UID, level=3, name="Pader"),),
                tracker=(_ou(TRACKER_UID, level=4, name="Pader HC III"),),
            ),
            scope,
            session_reference="sid",
        )
        assert Permission.CASE_EVIDENCE_VIEW in principal.permissions
        assert principal.max_sensitivity is SensitivityLevel.PSEUDONYMOUS_CASE
        assert Permission.PATIENT_REIDENTIFY not in principal.permissions

    def test_no_usable_remote_authorization(self) -> None:
        scope = resolve_live_scope(_snapshot(username="empty"), EMPTY_LOOKUP)
        assert scope.scope_type == "unresolved"
        assert landing_path_for_scope(scope) == "/no-authorised-scope"

    def test_principal_is_not_synthetic_and_has_aggregate_ceiling(self) -> None:
        snapshot = _snapshot(username="officer", data_view=(_ou(PADER_UID, level=3),))
        scope = resolve_live_scope(snapshot, LOOKUP)
        principal = build_live_principal(snapshot, scope, session_reference="sid")
        assert principal.is_synthetic is False
        assert principal.auth_method == "dhis2_pilot"
        assert principal.max_sensitivity.name == "AGGREGATE"
        assert "patient:reidentify" not in {p.value for p in principal.permissions}

    def test_pending_mapping_principal_cannot_query_surveillance(self) -> None:
        snapshot = _snapshot(
            username="officer",
            data_view=(_ou(UNKNOWN_UID, level=3, name="Pader"),),
        )
        scope = resolve_live_scope(snapshot, EMPTY_LOOKUP)
        principal = build_live_principal(snapshot, scope, session_reference="sid")
        assert principal.geography_scopes == ()
        assert not principal.has_national_scope
        assert "surveillance:view_aggregate" not in {p.value for p in principal.permissions}

    def test_dhis2_uid_syntax(self) -> None:
        assert is_dhis2_uid(PADER_UID)
        assert not is_dhis2_uid("PaderUid")
        assert not is_dhis2_uid(str(PADER_ID))
