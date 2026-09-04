/**
 * Where a signed-in user should land.
 *
 * Kept in its own module so the provider file exports only a component,
 * which is what React Fast Refresh requires.
 *
 * Routing follows the resolved geographic scope. Usernames never enter the
 * decision.
 */

import type { Schemas, SessionStatus } from "../api/client";

export type CurrentUser = Schemas["CurrentUserResponse"] & {
  landing_path?: string | null;
  scope_type?: string;
  mapping_status?: string;
  source_status?: SessionStatus["source_status"];
};

export function resolveLandingPath(user: CurrentUser | null): string {
  if (!user) return "/sign-in";
  if (user.landing_path) return user.landing_path;

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
