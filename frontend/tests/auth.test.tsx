/**
 * Authentication and routing behaviour.
 *
 * These assert the interface's side of the access model: which workspace a user
 * lands on, and that a view refused by the server renders an explanation rather
 * than an empty page.
 */

import { describe, expect, it } from "vitest";

import { ApiError } from "../src/api/client";
import { resolveLandingPath, type CurrentUser } from "../src/auth/landing";

function user(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    user_id: "00000000-0000-4000-8000-000000000001",
    username: "synthetic.user",
    display_name: "Synthetic User",
    email: null,
    organisation_label: null,
    roles: ["district_hsd"],
    permissions: ["surveillance:view_aggregate", "geography:view"],
    max_sensitivity: "aggregate",
    geography_scopes: [],
    facility_scope_ids: [],
    has_national_scope: false,
    auth_method: "development",
    is_synthetic: true,
    ...overrides,
  };
}

describe("landing path", () => {
  it("sends an anonymous visitor to sign-in", () => {
    expect(resolveLandingPath(null)).toBe("/sign-in");
  });

  it("sends a national user to the overview", () => {
    expect(resolveLandingPath(user({ has_national_scope: true }))).toBe("/command-centre");
  });

  it("sends a district user to the scoped overview, never a national map they cannot populate", () => {
    const path = resolveLandingPath(
      user({
        geography_scopes: [
          {
            geography_unit_id: "00000000-0000-4000-8000-000000000304",
            preferred_code: "304",
            level: "district",
            name: "GULU",
          },
        ],
      }),
    );
    expect(path).toBe("/command-centre");
  });

  it("sends a facility user to their own facility", () => {
    const path = resolveLandingPath(
      user({
        facility_scope_ids: ["00000000-0000-4000-8000-00000000f001"],
        geography_scopes: [
          {
            geography_unit_id: "00000000-0000-4000-8000-000000000304",
            preferred_code: "304",
            level: "district",
            name: "GULU",
          },
        ],
      }),
    );
    expect(path).toBe("/facilities/00000000-0000-4000-8000-00000000f001");
  });

  it("sends an unscoped account to their access profile, not to a dashboard", () => {
    // An empty scope is a misconfiguration. Landing on an empty national view
    // would hide it; landing on the profile makes it visible and explained.
    expect(resolveLandingPath(user())).toBe("/profile");
  });

  it("prefers the facility scope over the district scope", () => {
    const path = resolveLandingPath(
      user({
        facility_scope_ids: ["facility-a"],
        geography_scopes: [
          {
            geography_unit_id: "district-id",
            preferred_code: "304",
            level: "district",
            name: "GULU",
          },
        ],
      }),
    );
    expect(path).toBe("/facilities/facility-a");
  });
});

describe("ApiError", () => {
  it("classifies an unauthenticated response", () => {
    const error = new ApiError(
      401,
      { type: "", title: "", status: 401, code: "unauthenticated" },
      "",
    );
    expect(error.isUnauthenticated).toBe(true);
    expect(error.isForbidden).toBe(false);
  });

  it("classifies a permission denial and extracts the requirement", () => {
    const error = new ApiError(
      403,
      {
        type: "",
        title: "Permission denied",
        status: 403,
        code: "permission_denied",
        detail: "This action requires: organisation:view",
      },
      "",
    );
    expect(error.isForbidden).toBe(true);
    expect(error.requirement).toBe("organisation:view");
  });

  it("classifies a dependency failure separately from an empty result", () => {
    const error = new ApiError(
      503,
      { type: "", title: "", status: 503, code: "dependency_unavailable" },
      "",
    );
    expect(error.isUnavailable).toBe(true);
  });

  it("carries the request identifier for support", () => {
    const error = new ApiError(
      500,
      {
        type: "",
        title: "",
        status: 500,
        code: "internal_error",
        request_id: "abc-123",
      },
      "",
    );
    expect(error.requestId).toBe("abc-123");
  });

  it("does not claim a requirement for a non-403", () => {
    const error = new ApiError(
      404,
      { type: "", title: "", status: 404, code: "not_found" },
      "",
    );
    expect(error.requirement).toBeNull();
  });
});
