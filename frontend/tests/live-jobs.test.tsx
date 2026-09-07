import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Schemas } from "../src/api/client";
import { useLiveDashboard } from "../src/features/operations/useLiveDashboard";

vi.mock("../src/auth/context", () => ({ useAuth: () => ({ user: { source_status: { mode: "live" } } }) }));
const period = { start: "2026-08-01", end: "2026-08-31" };
const range = { period_start: period.start, period_end: period.end };
const saved = { snapshot_id: "saved", status: "synchronized" } as Schemas["LiveDashboardSnapshot"];
const receipt = { id: "job", status: "queued", ...range, completed_steps: 0, total_steps: 3,
  created_at: "2026-09-07T10:00:00Z", updated_at: "2026-09-07T10:00:00Z", error_code: null } as Schemas["LiveSyncJobSummary"];

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, wrapper };
}

beforeEach(() => {
  vi.spyOn(api, "latestLiveDashboard").mockResolvedValue(saved);
  vi.spyOn(api, "latestLiveDashboardJob").mockResolvedValue(null);
  vi.spyOn(api, "submitLiveDashboardJob").mockResolvedValue(receipt);
});

describe("durable live dashboard UI", () => {
  it("keeps the saved snapshot while a job runs and refreshes it on completion", async () => {
    const { client, wrapper } = harness();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useLiveDashboard(period), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(saved));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.synchronization.isPending).toBe(true));
    expect(result.current.data).toEqual(saved);
    expect(result.current.isLoading).toBe(false);
    vi.mocked(api.latestLiveDashboard).mockResolvedValue({ ...saved, snapshot_id: "new" });
    act(() => { client.setQueryData(["live", "dashboard-job", range], { ...receipt, status: "completed" }); });
    await waitFor(() => expect(result.current.data?.snapshot_id).toBe("new"));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["live", "patient"] });
  });

  it("shows refresh failure without hiding previously retrieved figures", async () => {
    vi.mocked(api.latestLiveDashboardJob).mockResolvedValue({ ...receipt, status: "failed", error_code: "synchronization_failed" });
    const { wrapper } = harness();
    const { result } = renderHook(() => useLiveDashboard(period), { wrapper });
    await waitFor(() => expect(result.current.refreshError?.message).toContain("latest refresh failed"));
    expect(result.current.data).toEqual(saved);
    expect(result.current.error).toBeNull();
  });

  it("rejoins an existing job after navigation without submitting another", async () => {
    vi.mocked(api.latestLiveDashboard).mockResolvedValue(null);
    vi.mocked(api.latestLiveDashboardJob).mockResolvedValue({ ...receipt, status: "running" });
    const { wrapper } = harness();
    const { result } = renderHook(() => useLiveDashboard(period, true, true), { wrapper });
    await waitFor(() => expect(result.current.synchronization.isPending).toBe(true));
    expect(api.submitLiveDashboardJob).not.toHaveBeenCalled();
  });

  it("does not place an old period's delayed job receipt in the new period cache", async () => {
    let resolve!: (job: Schemas["LiveSyncJobSummary"]) => void;
    vi.mocked(api.submitLiveDashboardJob).mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { client, wrapper } = harness();
    const { result, rerender } = renderHook(({ selected }) => useLiveDashboard(selected), { wrapper, initialProps: { selected: period } });
    await waitFor(() => expect(result.current.data).toEqual(saved));
    act(() => result.current.refresh());
    await waitFor(() => expect(resolve).toBeDefined());
    rerender({ selected: { start: "2026-07-01", end: "2026-07-31" } });
    act(() => { resolve(receipt); });
    await waitFor(() => expect(client.getQueryData(["live", "dashboard-job", range])).toEqual(receipt));
    expect(client.getQueryData(["live", "dashboard-job", { period_start: "2026-07-01", period_end: "2026-07-31" }])).not.toEqual(receipt);
  });
});
