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
    scope_type: "unresolved",
    mapping_status: "mapped",
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

  it("sends a district user to the district overview, never a national map they cannot populate", () => {
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
    expect(path).toBe("/district/00000000-0000-4000-8000-000000000304");
  });

  it("sends a facility-only user to their facility", () => {
    const path = resolveLandingPath(
      user({
        facility_scope_ids: ["00000000-0000-4000-8000-00000000f001"],
      }),
    );
    expect(path).toBe("/facility/00000000-0000-4000-8000-00000000f001");
  });

  it("sends an unscoped account to no-authorised-scope, not to national data", () => {
    expect(resolveLandingPath(user())).toBe("/no-authorised-scope");
  });

  it("does not use the username to choose a route", () => {
    const scopes = [
      {
        geography_unit_id: "00000000-0000-4000-8000-000000000312",
        preferred_code: "312",
        level: "district",
        name: "Pader",
      },
    ];
    expect(resolveLandingPath(user({ username: "district.pader", geography_scopes: scopes }))).toBe(
      resolveLandingPath(user({ username: "someone.else", geography_scopes: scopes })),
    );
  });

  it("sends multiple districts to authorised-scope, not national", () => {
    const path = resolveLandingPath(
      user({
        geography_scopes: [
          {
            geography_unit_id: "00000000-0000-4000-8000-000000000304",
            preferred_code: "304",
            level: "district",
            name: "Gulu",
          },
          {
            geography_unit_id: "00000000-0000-4000-8000-000000000312",
            preferred_code: "312",
            level: "district",
            name: "Pader",
          },
        ],
      }),
    );
    expect(path).toBe("/authorised-scope");
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
