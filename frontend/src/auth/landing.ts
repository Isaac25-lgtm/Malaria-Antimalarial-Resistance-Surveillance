/**
 * Where a signed-in user should land.
 *
 * Kept in its own module so the provider file exports only a component,
 * which is what React Fast Refresh requires.
 *
 * Routing follows remote authorization first, then local mapping.
 * Usernames never enter the decision. The backend calculates landing_path.
 */

import type { Schemas, SessionStatus } from "../api/client";

export type CurrentUser = Schemas["CurrentUserResponse"] & {
  landing_path?: string | null;
  scope_type?: string;
  mapping_status?: string;
  source_status?: SessionStatus["source_status"];
  workspace?: SessionStatus["workspace"];
  mapping?: SessionStatus["mapping"];
  data_readiness?: SessionStatus["data_readiness"];
};

const DHIS2_UID = /^[A-Za-z][A-Za-z0-9]{10}$/;

export function isDhis2Uid(value: string): boolean {
  return DHIS2_UID.test(value);
}

export function resolveLandingPath(user: CurrentUser | null): string {
  if (!user) return "/sign-in";
  if (user.landing_path) return user.landing_path;

  const workspace = user.workspace;
  if (workspace && workspace.authorization_status === "resolved") {
    const mapped = user.mapping?.status === "resolved";
    if (mapped) {
      if (workspace.scope_type === "national" || user.has_national_scope) return "/command-centre";
      const districts = user.geography_scopes.filter((scope) => scope.level === "district");
      const district = districts[0];
      if (workspace.scope_type === "district" && district) {
        return `/district/${district.geography_unit_id}`;
      }
      if (workspace.scope_type === "facility" && user.facility_scope_ids[0]) {
        return `/facility/${user.facility_scope_ids[0]}`;
      }
      return "/authorised-scope";
    }
    if (workspace.scope_type === "district" && workspace.external_uid && isDhis2Uid(workspace.external_uid)) {
      return `/live/dhis2/district/${workspace.external_uid}`;
    }
    if (workspace.scope_type === "facility" && workspace.external_uid && isDhis2Uid(workspace.external_uid)) {
      return `/live/dhis2/facility/${workspace.external_uid}`;
    }
    if (workspace.scope_type === "national") return "/live/dhis2/national";
    return "/authorised-scope";
  }

  const districts = user.geography_scopes.filter((scope) => scope.level === "district");
  const district = districts[0];
  const facilityId = user.facility_scope_ids[0];
  if (user.has_national_scope) return "/command-centre";
  if (user.facility_scope_ids.length === 1 && districts.length === 0 && facilityId) {
    return `/facility/${facilityId}`;
  }
  if (districts.length === 1 && district) return `/district/${district.geography_unit_id}`;
  if (districts.length > 1) return "/authorised-scope";
  if (facilityId) {
    return `/facility/${facilityId}`;
  }
  if (user.geography_scopes.length > 0) return "/authorised-scope";
  return "/no-authorised-scope";
}
