import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type Schemas } from "../src/api/client";
import { FacilityWorkspaceView } from "../src/features/workspaces/FacilityWorkspaceView";

vi.mock("../src/auth/context", () => ({
  useAuth: () => ({
    user: {
      source_status: { mode: "live" },
      permissions: ["case_evidence:view"],
    },
  }),
}));

afterEach(() => vi.restoreAllMocks());

it("drills into a DHIS2 facility using the scoped live snapshot", async () => {
  const snapshot = {
    status: "synchronized",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    synchronized_at: "2026-09-05T10:00:00Z",
    facility_count: 1,
    aggregate_reporting_facility_count: 1,
    tracker_reporting_facility_count: 1,
    tracker_retrieved_facility_count: 1,
    retrieval_complete: true,
    facilities: [{
      uid: "Facility01A",
      name: "Pader HC III",
      confirmed_malaria: 12,
      tested_for_malaria: 40,
      rdt_days_out_of_stock: 0,
      al_days_out_of_stock: 2,
      artesunate_days_out_of_stock: 0,
      aggregate_reported: true,
      tracker_reported: true,
    }],
    positive_patients: [],
  } as unknown as Schemas["LiveDashboardSnapshot"];
  vi.spyOn(api, "latestLiveDashboard").mockResolvedValue(snapshot);
  vi.spyOn(api, "latestLiveDashboardJob").mockResolvedValue(null);
  const localFacility = vi.spyOn(api, "facility");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/facility/Facility01A"]}>
        <Routes><Route path="/facility/:facilityId" element={<FacilityWorkspaceView />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("heading", { name: "Pader HC III" })).toBeInTheDocument();
  expect(screen.getByText("30.0%")).toBeInTheDocument();
  expect(localFacility).not.toHaveBeenCalled();
});
