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
  },
  error: null,
  signInAsDevelopmentUser: () => Promise.resolve(),
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
