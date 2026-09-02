/**
 * Where a signed-in user should land.
 *
 * Kept in its own module so the provider file exports only a component,
 * which is what React Fast Refresh requires.
 */

import type { Schemas } from "../api/client";

export type CurrentUser = Schemas["CurrentUserResponse"];

/**
 * Choose the workspace a user lands on.
 *
 * A user is routed to the highest geography they are actually scoped to, so a
 * district officer never opens a national view they cannot populate. A user
 * with no scope lands on their profile, where the missing scope is visible and
 * explained rather than presenting as an empty dashboard.
 */
export function resolveLandingPath(user: CurrentUser | null): string {
  if (!user) return "/sign-in";

  if (user.facility_scope_ids.length === 1 && !user.has_national_scope) {
    return `/facilities/${user.facility_scope_ids[0]}`;
  }
  if (user.has_national_scope) {
    return "/national";
  }
  const district = user.geography_scopes.find((scope) => scope.level === "district");
  if (district) {
    return `/districts/${district.preferred_code}`;
  }
  if (user.geography_scopes.length > 0) {
    return "/geography";
  }
  return "/profile";
}
