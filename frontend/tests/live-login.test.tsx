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
