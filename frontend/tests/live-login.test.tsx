/**
 * Live credential form: one Ministry login, no DHIS2 from the browser.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../src/api/client";
import { AuthProvider } from "../src/auth/AuthProvider";
import { SignInView } from "../src/features/auth/SignInView";

function renderLiveSignIn() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={["/sign-in"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <AuthProvider>
          <SignInView />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("live sign-in form", () => {
  it("shows username and password, never a synthetic account chooser", async () => {
    vi.spyOn(api, "session").mockResolvedValue({ authenticated: false, auth_mode: "live" });
    vi.spyOn(api, "version").mockResolvedValue({
      live_login_enabled: true,
      auth_mode: "live",
      development_auth_active: false,
      demo_mode_enabled: false,
    } as never);
    const users = vi.spyOn(api, "developmentUsers");

    renderLiveSignIn();

    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByText("Use your authorised Ministry eRegisters account.")).toBeInTheDocument();
    expect(screen.queryByText("Choose an account")).not.toBeInTheDocument();
    expect(users).not.toHaveBeenCalled();
  });

  it("clears the password after submit and never calls DHIS2", async () => {
    vi.spyOn(api, "session").mockResolvedValue({ authenticated: false, auth_mode: "live" });
    vi.spyOn(api, "version").mockResolvedValue({
      live_login_enabled: true,
      auth_mode: "live",
      development_auth_active: false,
    } as never);
    const login = vi.spyOn(api, "liveLogin").mockRejectedValue(
      new ApiError(
        401,
        {
          type: "",
          title: "Authentication required",
          status: 401,
          code: "unauthenticated",
          detail: "Invalid username or password",
        },
        "Invalid username or password",
      ),
    );
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    renderLiveSignIn();
    const password = await screen.findByLabelText("Password");
    await userEvent.type(screen.getByLabelText("Username"), "officer");
    await userEvent.type(password, "temporary-secret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(login).toHaveBeenCalled());
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(
      fetchSpy.mock.calls.every((call) => {
        const target = call[0];
        if (typeof target === "string") return !target.includes("eregisters");
        if (target instanceof URL) return !target.href.includes("eregisters");
        if (target instanceof Request) return !target.url.includes("eregisters");
        return true;
      }),
    ).toBe(true);
  });
});

describe("pending live workspace", () => {
  it("renders the remote Pader workspace without synthetic figures", async () => {
    const { LiveRemoteWorkspaceView } = await import("../src/features/profile/LiveRemoteWorkspaceView");
    const { AuthContext } = await import("../src/auth/context");
    const user = {
      user_id: "00000000-0000-4000-8000-000000000001",
      username: "officer",
      display_name: "ISAAC OMODING",
      email: null,
      organisation_label: null,
      roles: [],
      permissions: [],
      max_sensitivity: "aggregate",
      geography_scopes: [],
      facility_scope_ids: [],
      has_national_scope: false,
      auth_method: "dhis2_pilot",
      is_synthetic: false,
      scope_type: "district",
      mapping_status: "pending",
      landing_path: "/live/dhis2/district/PaderDist01",
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
      mapping: { status: "pending", geography_unit_id: null, facility_id: null },
      data_readiness: {
        geography: "pending",
        malaria_metadata: "pending",
        aggregate_sync: "pending",
        tracker_sync: "not_started",
      },
    };
    render(
      <MemoryRouter
        initialEntries={["/live/dhis2/district/PaderDist01"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <AuthContext.Provider
          value={{
            status: "authenticated",
            user,
            error: null,
            signInAsDevelopmentUser: () => Promise.resolve(),
            signInWithEregisters: () => Promise.resolve(),
            signOut: () => Promise.resolve(),
            can: () => false,
            canAccessSensitivity: () => false,
            landingPath: "/live/dhis2/district/PaderDist01",
          }}
        >
          <LiveRemoteWorkspaceView />
        </AuthContext.Provider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Pader Live Pilot" })).toBeInTheDocument();
    expect(screen.getByText("ISAAC OMODING · Pader District")).toBeInTheDocument();
    expect(screen.getByText("Pader authorization confirmed")).toBeInTheDocument();
    expect(screen.getByText("Geography mapping pending")).toBeInTheDocument();
    expect(screen.getByText("Pader boundary mapping pending")).toBeInTheDocument();
    expect(screen.queryByText("No authorised scope")).not.toBeInTheDocument();
    expect(screen.queryByText(/synthetic/i)).not.toBeInTheDocument();
  });
});
