import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, SESSION_EXPIRED_EVENT } from "../src/api/client";
import { AuthProvider } from "../src/auth/AuthProvider";
import { useAuth } from "../src/auth/context";

afterEach(() => vi.restoreAllMocks());

function SessionLabel() {
  const auth = useAuth();
  return <span>{auth.status}</span>;
}

it("removes private cached evidence when the session expires", async () => {
  vi.spyOn(api, "session").mockResolvedValue({
    authenticated: true,
    auth_mode: "live",
    profile: {
      user_id: "00000000-0000-4000-8000-000000000001",
      username: "officer",
      display_name: "Officer",
      email: null,
      organisation_label: "Pader District",
      roles: [],
      permissions: [],
      max_sensitivity: "aggregate",
      geography_scopes: [],
      facility_scope_ids: [],
      has_national_scope: false,
      auth_method: "dhis2_pilot",
      is_synthetic: false,
    },
  } as never);
  const client = new QueryClient();
  client.setQueryData(["live", "dashboard", "latest"], { privateEvidence: true });
  render(<QueryClientProvider client={client}><AuthProvider><SessionLabel /></AuthProvider></QueryClientProvider>);
  expect(await screen.findByText("authenticated")).toBeInTheDocument();
  act(() => { window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT)); });
  expect(await screen.findByText("anonymous")).toBeInTheDocument();
  expect(client.getQueryData(["live", "dashboard", "latest"])).toBeUndefined();
});
