"""The permission matrix.

These tests are the specification of who may do what. If one of them fails, the
access model has changed, and that change is a decision requiring approval - not
a test to update.
"""

from __future__ import annotations

import pytest

from mars.security.permissions import (
    PERMISSION_CATALOGUE,
    ROLE_DEFAULT_SENSITIVITY,
    ROLE_PERMISSIONS,
    Permission,
    SensitivityLevel,
    SystemRole,
)
from mars.security.principal import AuthenticatedPrincipal
from tests.conftest import (
    GULU_FACILITY_ID,
    GULU_SCOPE,
    PADER_FACILITY_ID,
    PADER_SCOPE,
    make_principal,
)


class TestCatalogueIntegrity:
    def test_every_permission_has_a_catalogue_entry(self) -> None:
        """A permission with no entry has no documented sensitivity requirement."""
        missing = set(Permission) - set(PERMISSION_CATALOGUE)
        assert not missing, f"permissions missing from the catalogue: {missing}"

    def test_every_role_has_a_default_sensitivity(self) -> None:
        missing = set(SystemRole) - set(ROLE_DEFAULT_SENSITIVITY)
        assert not missing, f"roles missing a default sensitivity: {missing}"

    def test_every_role_has_a_permission_set(self) -> None:
        missing = set(SystemRole) - set(ROLE_PERMISSIONS)
        assert not missing

    def test_role_grants_reference_only_known_permissions(self) -> None:
        for role, permissions in ROLE_PERMISSIONS.items():
            unknown = permissions - set(Permission)
            assert not unknown, f"{role} grants unknown permissions: {unknown}"


class TestReidentificationIsNeverImpliedByARole:
    """Blueprint section 009: re-identification is separately permissioned."""

    @pytest.mark.parametrize("role", list(SystemRole))
    def test_no_role_grants_reidentification(self, role: SystemRole) -> None:
        assert Permission.PATIENT_REIDENTIFY not in ROLE_PERMISSIONS[role], (
            f"{role.value} grants re-identification by default. It must be an "
            "individual grant with a recorded reason, never a role default."
        )

    @pytest.mark.parametrize("role", list(SystemRole))
    def test_no_role_defaults_to_direct_identity_sensitivity(self, role: SystemRole) -> None:
        assert ROLE_DEFAULT_SENSITIVITY[role] is not SensitivityLevel.DIRECT_IDENTITY


class TestNationalAggregateUser:
    def test_can_view_aggregate_surveillance(
        self, national_principal: AuthenticatedPrincipal
    ) -> None:
        assert national_principal.has_permission(Permission.SURVEILLANCE_VIEW_AGGREGATE)

    def test_has_national_geography_scope(self, national_principal: AuthenticatedPrincipal) -> None:
        assert national_principal.has_national_scope
        assert national_principal.covers_geography(GULU_SCOPE.geography_unit_id, GULU_SCOPE.path)
        assert national_principal.covers_geography(PADER_SCOPE.geography_unit_id, PADER_SCOPE.path)

    def test_cannot_view_pseudonymous_case_evidence(
        self, national_principal: AuthenticatedPrincipal
    ) -> None:
        """National users see aggregates. Patient names are never shown."""
        assert not national_principal.has_permission(Permission.CASE_EVIDENCE_VIEW)
        assert not national_principal.can_access_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE)

    def test_cannot_reidentify(self, national_principal: AuthenticatedPrincipal) -> None:
        assert not national_principal.has_permission(Permission.PATIENT_REIDENTIFY)
        assert not national_principal.can_access_sensitivity(SensitivityLevel.DIRECT_IDENTITY)


class TestDistrictUserCannotCrossGeographyBoundary:
    def test_covers_own_district(self, gulu_district_principal: AuthenticatedPrincipal) -> None:
        assert gulu_district_principal.covers_geography(
            GULU_SCOPE.geography_unit_id, GULU_SCOPE.path
        )

    def test_denied_neighbouring_district(
        self, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        assert not gulu_district_principal.covers_geography(
            PADER_SCOPE.geography_unit_id, PADER_SCOPE.path
        )

    def test_covers_descendants_of_own_district(
        self, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        """A subcounty inside Gulu is inside a Gulu user's scope."""
        import uuid

        subcounty_id = uuid.uuid4()
        assert gulu_district_principal.covers_geography(subcounty_id, "UG/3/304/3041/304101")

    def test_denied_descendants_of_another_district(
        self, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        import uuid

        subcounty_id = uuid.uuid4()
        assert not gulu_district_principal.covers_geography(subcounty_id, "UG/3/312/3121/312101")

    def test_prefix_collision_does_not_grant_access(self) -> None:
        """A path prefix must match on a segment boundary.

        Without the separator check, a scope of ``UG/3/30`` would wrongly cover
        ``UG/3/304``. This is the bug the containment helper exists to prevent.
        """
        import uuid

        from mars.security.principal import GeographyScope

        narrow = make_principal(
            role=SystemRole.DISTRICT_HSD,
            scopes=(
                GeographyScope(
                    geography_unit_id=uuid.uuid4(),
                    preferred_code="30",
                    level="district",
                    name="PREFIX",
                    path="UG/3/30",
                ),
            ),
        )
        assert not narrow.covers_geography(uuid.uuid4(), "UG/3/304")
        assert narrow.covers_geography(uuid.uuid4(), "UG/3/30/301")

    def test_district_user_may_view_pseudonymous_evidence(
        self, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        assert gulu_district_principal.has_permission(Permission.CASE_EVIDENCE_VIEW)
        assert gulu_district_principal.can_access_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE)

    def test_district_user_still_cannot_reidentify(
        self, gulu_district_principal: AuthenticatedPrincipal
    ) -> None:
        assert not gulu_district_principal.has_permission(Permission.PATIENT_REIDENTIFY)
        assert not gulu_district_principal.can_access_sensitivity(SensitivityLevel.DIRECT_IDENTITY)


class TestFacilityUserCannotSeeOtherFacilities:
    def test_covers_own_facility(self, gulu_facility_principal: AuthenticatedPrincipal) -> None:
        assert gulu_facility_principal.covers_facility(GULU_FACILITY_ID)

    def test_denied_other_facility(self, gulu_facility_principal: AuthenticatedPrincipal) -> None:
        assert not gulu_facility_principal.covers_facility(PADER_FACILITY_ID)

    def test_denied_a_sibling_facility_in_the_same_district(
        self, gulu_facility_principal: AuthenticatedPrincipal
    ) -> None:
        """Sharing a district is not enough. The facility must be named."""
        import uuid

        sibling = uuid.uuid4()
        assert not gulu_facility_principal.covers_facility(sibling)

    def test_is_marked_facility_restricted(
        self, gulu_facility_principal: AuthenticatedPrincipal
    ) -> None:
        assert gulu_facility_principal.is_facility_restricted

    def test_unrestricted_user_is_not_facility_limited(
        self, national_principal: AuthenticatedPrincipal
    ) -> None:
        import uuid

        assert not national_principal.is_facility_restricted
        assert national_principal.covers_facility(uuid.uuid4())


class TestAnalystHasNoPatientAccess:
    """Managing definitions and methods must not confer patient access."""

    def test_can_manage_configuration_and_approve_methods(
        self, analyst_principal: AuthenticatedPrincipal
    ) -> None:
        assert analyst_principal.has_permission(Permission.CONFIGURATION_MANAGE)
        assert analyst_principal.has_permission(Permission.METHOD_APPROVE)

    def test_cannot_view_case_evidence(self, analyst_principal: AuthenticatedPrincipal) -> None:
        assert not analyst_principal.has_permission(Permission.CASE_EVIDENCE_VIEW)

    def test_cannot_reidentify(self, analyst_principal: AuthenticatedPrincipal) -> None:
        assert not analyst_principal.has_permission(Permission.PATIENT_REIDENTIFY)

    def test_sensitivity_ceiling_is_aggregate(
        self, analyst_principal: AuthenticatedPrincipal
    ) -> None:
        assert analyst_principal.max_sensitivity is SensitivityLevel.AGGREGATE
        assert not analyst_principal.can_access_sensitivity(SensitivityLevel.PSEUDONYMOUS_CASE)


class TestAdministratorHasNoPatientOrSurveillanceAccess:
    """Managing users must not confer access to what the users can see."""

    def test_can_administer_users(self, administrator_principal: AuthenticatedPrincipal) -> None:
        assert administrator_principal.has_permission(Permission.USER_ADMINISTER)

    def test_cannot_view_aggregate_surveillance(
        self, administrator_principal: AuthenticatedPrincipal
    ) -> None:
        assert not administrator_principal.has_permission(Permission.SURVEILLANCE_VIEW_AGGREGATE)

    def test_cannot_view_case_evidence(
        self, administrator_principal: AuthenticatedPrincipal
    ) -> None:
        assert not administrator_principal.has_permission(Permission.CASE_EVIDENCE_VIEW)

    def test_cannot_reidentify(self, administrator_principal: AuthenticatedPrincipal) -> None:
        assert not administrator_principal.has_permission(Permission.PATIENT_REIDENTIFY)


class TestExplicitReidentificationGrant:
    """Re-identification requires both the permission and the sensitivity tier."""

    def test_permission_without_sensitivity_is_unusable(self) -> None:
        """Granting the permission alone must not enable the action.

        ``make_principal`` mirrors AuthService: a permission whose minimum
        sensitivity exceeds the caller's ceiling is dropped, so a partial grant
        fails closed rather than half-working.
        """
        principal = make_principal(
            role=SystemRole.DISTRICT_HSD,
            scopes=(GULU_SCOPE,),
            permissions=frozenset(
                {Permission.PATIENT_REIDENTIFY, Permission.SURVEILLANCE_VIEW_AGGREGATE}
            ),
            max_sensitivity=SensitivityLevel.PSEUDONYMOUS_CASE,
        )
        assert not principal.has_permission(Permission.PATIENT_REIDENTIFY)
        assert principal.has_permission(Permission.SURVEILLANCE_VIEW_AGGREGATE)

    def test_both_grants_together_enable_it(self) -> None:
        principal = make_principal(
            role=SystemRole.DISTRICT_HSD,
            scopes=(GULU_SCOPE,),
            permissions=frozenset({Permission.PATIENT_REIDENTIFY}),
            max_sensitivity=SensitivityLevel.DIRECT_IDENTITY,
        )
        assert principal.has_permission(Permission.PATIENT_REIDENTIFY)
        assert principal.can_access_sensitivity(SensitivityLevel.DIRECT_IDENTITY)


class TestUnscopedAccountReadsNothing:
    def test_empty_scope_is_not_national_scope(
        self, unscoped_principal: AuthenticatedPrincipal
    ) -> None:
        """An empty scope must never be read as 'everywhere'."""
        assert not unscoped_principal.has_national_scope

    def test_empty_scope_covers_no_geography(
        self, unscoped_principal: AuthenticatedPrincipal
    ) -> None:
        assert not unscoped_principal.covers_geography(
            GULU_SCOPE.geography_unit_id, GULU_SCOPE.path
        )
        assert not unscoped_principal.covers_geography(
            PADER_SCOPE.geography_unit_id, PADER_SCOPE.path
        )


class TestSensitivityOrdering:
    def test_higher_tiers_cover_lower_ones(self) -> None:
        assert SensitivityLevel.DIRECT_IDENTITY.covers(SensitivityLevel.AGGREGATE)
        assert SensitivityLevel.DIRECT_IDENTITY.covers(SensitivityLevel.PSEUDONYMOUS_CASE)
        assert SensitivityLevel.PSEUDONYMOUS_CASE.covers(SensitivityLevel.AGGREGATE)

    def test_lower_tiers_do_not_cover_higher_ones(self) -> None:
        assert not SensitivityLevel.AGGREGATE.covers(SensitivityLevel.PSEUDONYMOUS_CASE)
        assert not SensitivityLevel.AGGREGATE.covers(SensitivityLevel.DIRECT_IDENTITY)
        assert not SensitivityLevel.PSEUDONYMOUS_CASE.covers(SensitivityLevel.DIRECT_IDENTITY)
