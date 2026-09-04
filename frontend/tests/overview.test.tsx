/**
 * Overview dashboard: compact empty states, GeoJSON map, Pader never labelled national.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type Schemas } from "../src/api/client";
import { AuthContext, type AuthContextValue } from "../src/auth/context";
import { CommandCentreView } from "../src/features/command-centre/CommandCentreView";

const PERIOD = { start: "2026-07-01", end: "2026-07-31" };
const PADER = "11111111-0000-4000-8000-000000000312";

const SECTION = {
  availability: "not_configured",
  requested_scope: "national",
  reporting_period: PERIOD,
  source: "table:indicator_result",
  source_period: PERIOD,
  freshness: null,
  last_successful_synchronization: null,
  method_version_id: null,
  refusal_reason: "No approved indicator version.",
};

function measure(code: string, label: string): Schemas["SurveillanceMeasure"] {
  return {
    code,
    label,
    value: null,
    unit: "count",
    numerator: null,
    denominator: null,
    period: PERIOD,
    geography_grain: "national",
    geography_unit_id: null,
    facility_id: null,
    source: `indicator:${code}`,
    method_version_id: null,
    source_freshness: null,
    comparison: null,
    status: "not_configured",
    status_detail: "No approved indicator version.",
    missing_configuration: ["approved_indicator_version"],
  };
}

const SNAPSHOT: Schemas["OverviewSnapshot"] = {
  title: "National Overview",
  subtitle: "Malaria surveillance from routine health information systems.",
  interpretation_boundary:
    "Routine surveillance data identifies patterns requiring investigation.",
  data_mode: "synthetic",
  data_mode_detail:
    "This deployment is serving synthetic demonstration data. It is not a live Ministry feed.",
  demo_mode_enabled: true,
  requested_scope: "national",
  has_national_scope: true,
  reporting_period: PERIOD,
  provenance: {
    period: PERIOD,
    indicators_registered: 0,
    indicators_approved: 0,
    analytics_refreshed_at: null,
    signals_generated_at: null,
    interpretation_boundary: "Routine surveillance data identifies patterns requiring investigation.",
    analytically_configured: false,
    configuration_detail: "No indicator versions are approved.",
  },
  last_successful_synchronization: null,
  kpis: {
    ...SECTION,
    items: [
      measure("ENC_ATTENDANCE_TOTAL", "Outpatient attendances"),
      measure("ENC_SUSPECTED_MALARIA", "Suspected malaria"),
      measure("ENC_TESTED_MALARIA", "Tested for malaria"),
      measure("ENC_CONFIRMED_MALARIA", "Confirmed malaria"),
      measure("ENC_REPEAT_POSITIVE_INPUT", "Repeat-positive encounters"),
      measure("RPT_COMPLETENESS", "Reporting completeness"),
      measure("ACTIVE_SIGNALS", "Active signals"),
    ],
  },
  signals_by_priority: { ...SECTION, items: [] },
  investigations_by_status: {
    ...SECTION,
    availability: "available",
    source: "table:investigation",
    refusal_reason: null,
    items: [{ code: "new", label: "new", count: 0, status: "available", detail: null }],
  },
  districts_requiring_review: { ...SECTION, availability: "empty", items: [] },
  commodity_alerts: { ...SECTION, availability: "empty", items: [] },
  needs_attention: {
    ...SECTION,
    availability: "available",
    source: "composed:overview",
    items: [
      {
        code: "investigations_overdue",
        label: "Investigations overdue",
        count: null,
        status: "not_configured",
        detail: "Omitted until an approved SLA exists.",
      },
    ],
  },
  recent_signals: { ...SECTION, availability: "empty", items: [] },
  confirmed_malaria_trend: { ...SECTION, items: [] },
  testing_positivity: { ...SECTION, items: [] },
};

const MAP_META: Schemas["MapMetadataResponse"] = {
  boundary_version_code: "UG-ADMIN-TEST",
  boundary_version_id: "22222222-0000-4000-8000-000000000001",
  boundary_version_label: "Uganda administrative boundaries",
  generated_at: "2026-07-01T00:00:00Z",
  geometry_resolution: "simplified",
  imported_at: "2026-07-01T00:00:00Z",
  initial_bounds: { min_lon: 30, min_lat: 0, max_lon: 31, max_lat: 1 },
  initial_unit_id: null,
  initial_unit_level: "country",
  initial_unit_name: "Uganda",
  is_available: true,
  levels: [],
  max_features: 400,
  source_checksum: null,
  source_name: "UBOS",
};

const GULU = "11111111-0000-4000-8000-000000000304";

const FEATURES: Schemas["MapFeatureCollection"] = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      id: GULU,
      geometry: {
        type: "MultiPolygon",
        coordinates: [[[[30, 0], [31, 0], [31, 1], [30, 1], [30, 0]]]],
      },
      properties: {
        unit_id: GULU,
        level: "district",
        code: "304",
        name: "GULU",
        parent_id: null,
        path: "UG/304",
        area_sq_km: 1,
        is_active: true,
        in_scope: false,
      },
    },
    {
      type: "Feature",
      id: PADER,
      geometry: {
        type: "MultiPolygon",
        coordinates: [[[[31, 0], [32, 0], [32, 1], [31, 1], [31, 0]]]],
      },
      properties: {
        unit_id: PADER,
        level: "district",
        code: "312",
        name: "PADER",
        parent_id: null,
        path: "UG/312",
        area_sq_km: 1,
        is_active: true,
        in_scope: true,
      },
    },
  ],
  bbox: [30, 0, 32, 1],
  mars: {
    boundary_version_id: MAP_META.boundary_version_id,
    boundary_version_code: MAP_META.boundary_version_code,
    level: "district",
    parent_id: null,
    within_id: null,
    geometry_resolution: "simplified",
    feature_count: 2,
    matched_count: 2,
    truncated: false,
  },
};

const auth: AuthContextValue = {
  status: "authenticated",
  user: {
    user_id: "00000000-0000-4000-8000-000000000001",
    username: "synthetic.user",
    display_name: "Synthetic User",
    email: null,
    organisation_label: null,
    roles: ["national_programme"],
    permissions: ["surveillance:view_aggregate", "geography:view", "report:generate"],
    max_sensitivity: "aggregate",
    geography_scopes: [],
    facility_scope_ids: [],
    has_national_scope: true,
    auth_method: "development",
    is_synthetic: true,
    scope_type: "national",
    mapping_status: "mapped",
  },
  error: null,
  signInAsDevelopmentUser: () => Promise.resolve(),
  signInWithEregisters: () => Promise.resolve(),
  signOut: () => Promise.resolve(),
  can: () => true,
  canAccessSensitivity: () => false,
  landingPath: "/command-centre",
};

function renderOverview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AuthContext.Provider value={auth}>
      <QueryClientProvider client={client}>
        <MemoryRouter
          initialEntries={["/command-centre"]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <Routes>
            <Route path="/command-centre" element={<CommandCentreView />} />
            <Route path="/workspaces/districts/:unitId" element={<p>District workspace</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

function stubApis(snapshot: Schemas["OverviewSnapshot"] = SNAPSHOT) {
  vi.spyOn(api, "overview").mockResolvedValue(snapshot);
  vi.spyOn(api, "mapMetadata").mockResolvedValue(MAP_META);
  vi.spyOn(api, "mapContext").mockResolvedValue(FEATURES);
  vi.spyOn(api, "mapFeatures").mockResolvedValue(FEATURES);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("operational overview", () => {
  it("keeps a compact six-card layout when figures are unconfigured", async () => {
    stubApis();
    renderOverview();

    expect(await screen.findByRole("heading", { name: "National Overview" })).toBeInTheDocument();
    expect(screen.getByText(/not a live Ministry feed/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Investigations" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Commodity security" })).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(6);
    expect(screen.queryByRole("heading", { name: "Active signals" })).not.toBeInTheDocument();
    expect(screen.getAllByText("Not configured").length).toBeGreaterThan(0);
    expect(screen.queryByText("indicator:ENC_ATTENDANCE_TOTAL")).not.toBeInTheDocument();
    expect(screen.queryByText("table:surveillance_signal")).not.toBeInTheDocument();
    expect(screen.queryByText("Investigation workflow is not part of this build")).not.toBeInTheDocument();
  });

  it("draws GeoJSON when analytics are unconfigured", async () => {
    stubApis();
    renderOverview();
    const svg = await screen.findByTestId("boundary-svg");
    expect(svg).toHaveAttribute("data-feature-count", "2");
    expect(svg.querySelectorAll("path")).toHaveLength(2);
  });

  it("never labels a Pader snapshot as national", async () => {
    stubApis({
      ...SNAPSHOT,
      title: "Pader Overview",
      requested_scope: "pader",
      has_national_scope: false,
    });
    renderOverview();
    expect(await screen.findByRole("heading", { name: "Pader Overview" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "National Overview" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Pader" })).toBeInTheDocument();
  });

  it("opens an in-scope district from the map", async () => {
    const user = userEvent.setup();
    stubApis();
    renderOverview();
    const path = await screen.findByLabelText("PADER");
    await user.click(path);
    expect(await screen.findByText("District workspace")).toBeInTheDocument();
  });

  it("does not open an out-of-scope district from the map", async () => {
    const user = userEvent.setup();
    stubApis();
    renderOverview();
    const path = await screen.findByLabelText("GULU");
    await user.click(path);
    expect(screen.queryByText("District workspace")).not.toBeInTheDocument();
  });

  it("loads district and subcounty geography for a one-district live user", async () => {
    const nationalUser = auth.user;
    if (!nationalUser) throw new Error("expected an authenticated test user");
    const districtAuth: AuthContextValue = {
      ...auth,
      user: {
        ...nationalUser,
        is_synthetic: false,
        has_national_scope: false,
        username: "officer",
        auth_method: "dhis2_pilot",
        scope_type: "district",
        mapping_status: "mapped",
        geography_scopes: [
          {
            geography_unit_id: PADER,
            preferred_code: "312",
            level: "district",
            name: "Pader",
          },
        ],
        source_status: {
          mode: "live",
          source: "eRegisters",
          authentication: "connected",
          mapping: "mapped",
          last_sync: null,
        },
      },
    };
    stubApis({
      ...SNAPSHOT,
      title: "Pader Overview",
      requested_scope: "pader",
      has_national_scope: false,
      data_mode: "unavailable",
      demo_mode_enabled: false,
      last_successful_synchronization: null,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AuthContext.Provider value={districtAuth}>
        <QueryClientProvider client={client}>
          <MemoryRouter
            initialEntries={[`/district/${PADER}`]}
            future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
          >
            <Routes>
              <Route path="/district/:unitId" element={<CommandCentreView />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    expect(await screen.findByRole("heading", { name: "Pader Overview" })).toBeInTheDocument();
    expect(screen.getByText("LIVE — eRegisters connected")).toBeInTheDocument();
    expect(screen.getAllByText(/Last sync:\s*Not yet run/).length).toBeGreaterThan(0);
    expect(await screen.findByRole("heading", { name: "District and subcounty geography" })).toBeInTheDocument();
    expect(api.mapContext).not.toHaveBeenCalled();
    expect(api.mapFeatures).toHaveBeenCalledWith({
      level: "subcounty",
      within_id: PADER,
    });
  });
});
