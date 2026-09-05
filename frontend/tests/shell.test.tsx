/**
 * Application shell landmarks, interpretation boundary, and scoped navigation.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api/client";
import { AppShell } from "../src/app/AppShell";
import { AuthContext, type AuthContextValue } from "../src/auth/context";

const auth: AuthContextValue = {
  status: "authenticated",
  user: {
    user_id: "00000000-0000-4000-8000-000000000001",
    username: "synthetic.user",
    display_name: "Synthetic User",
    email: null,
    organisation_label: null,
    roles: ["national_programme"],
    permissions: [
      "surveillance:view_aggregate",
      "geography:view",
      "facility:view",
      "report:generate",
      "configuration:view",
    ],
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

function renderShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AuthContext.Provider value={auth}>
      <QueryClientProvider client={client}>
        <MemoryRouter
          initialEntries={["/command-centre"]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <Routes>
            <Route element={<AppShell />}>
              <Route path="/command-centre" element={<p>Overview content</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("application shell", () => {
  it("exposes skip navigation, a named sidebar, and the interpretation boundary", () => {
    vi.spyOn(api, "overview").mockResolvedValue({
      signals_by_priority: { availability: "not_configured", items: [] },
    } as never);

    renderShell();

    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute(
      "href",
      "#main-content",
    );
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Investigations" })).toBeInTheDocument();
    expect(screen.getByText("Ministry of Health Uganda")).toBeInTheDocument();
    expect(
      screen.getByText(/do not confirm antimalarial resistance/i),
    ).toBeInTheDocument();
  });

  it("announces a high-priority signal count in words, not colour alone", async () => {
    vi.spyOn(api, "overview").mockResolvedValue({
      signals_by_priority: {
        availability: "available",
        items: [
          { code: "urgent", count: 2 },
          { code: "high", count: 3 },
        ],
      },
    } as never);

    renderShell();

    expect(
      await screen.findByRole("link", { name: "Signals, 5 high-priority signals" }),
    ).toBeInTheDocument();
  });
});

describe("live source status", () => {
  function renderLiveShell(user: AuthContextValue["user"]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const liveAuth: AuthContextValue = {
      ...auth,
      user,
    };
    return render(
      <AuthContext.Provider value={liveAuth}>
        <QueryClientProvider client={client}>
          <MemoryRouter
            initialEntries={["/command-centre"]}
            future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
          >
            <Routes>
              <Route element={<AppShell />}>
                <Route path="/command-centre" element={<p>Overview content</p>} />
              </Route>
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
  }

  const liveUser: AuthContextValue["user"] = {
    user_id: "00000000-0000-4000-8000-000000000001",
    username: "officer",
    display_name: "Officer",
    email: null,
    organisation_label: null,
    roles: [],
    permissions: ["surveillance:view_aggregate", "geography:view"],
    max_sensitivity: "aggregate",
    geography_scopes: [
      {
        geography_unit_id: "00000000-0000-4000-8000-000000000312",
        preferred_code: "312",
        level: "district",
        name: "Pader",
      },
    ],
    facility_scope_ids: [],
    has_national_scope: false,
    auth_method: "dhis2_pilot",
    is_synthetic: false,
    source_status: {
      mode: "live",
      source: "eRegisters",
      authentication: "connected",
      mapping: "pending",
      last_sync: null,
    },
    scope_type: "district",
    mapping_status: "pending",
    workspace: {
      authorization_status: "resolved",
      scope_type: "district",
      source: "dhis2",
      external_uid: "PaderDist01",
      name: "Pader",
      capture_count: 0,
      data_view_count: 1,
      tracker_search_count: 0,
      fallback_used: false,
    },
    mapping: { status: "pending", geography_unit_id: null, facility_id: null, evidence: [] },
    data_readiness: {
      geography: "pending",
      malaria_metadata: "pending",
      aggregate_sync: "pending",
      tracker_sync: "not_started",
    },
  };

  it("shows mapping-pending status and never a development session chip", () => {
    vi.spyOn(api, "overview").mockResolvedValue({
      signals_by_priority: { availability: "not_configured", items: [] },
    } as never);
    renderLiveShell(liveUser);
    expect(screen.getByText("AUTHORIZED — MAPPING PENDING")).toBeInTheDocument();
    expect(screen.queryByText("Development session")).not.toBeInTheDocument();
    expect(screen.queryByText(/synthetic/i)).not.toBeInTheDocument();
    expect(screen.queryByText("No authorised scope")).not.toBeInTheDocument();
  });

  it("shows data-sync pending when geography is mapped but no live ingest exists", () => {
    vi.spyOn(api, "overview").mockResolvedValue({
      signals_by_priority: { availability: "not_configured", items: [] },
    } as never);
    renderLiveShell({
      ...liveUser,
      mapping_status: "resolved",
      mapping: {
        status: "resolved",
        geography_unit_id: "00000000-0000-4000-8000-000000000312",
        facility_id: null,
        evidence: [],
      },
      data_readiness: {
        geography: "resolved",
        malaria_metadata: "pending",
        aggregate_sync: "pending",
        tracker_sync: "not_started",
      },
      source_status: {
        mode: "live",
        source: "eRegisters",
        authentication: "connected",
        mapping: "resolved",
        last_sync: null,
      },
    });
    expect(screen.getByText("LIVE — PADER AUTHORIZED")).toBeInTheDocument();
  });
});
