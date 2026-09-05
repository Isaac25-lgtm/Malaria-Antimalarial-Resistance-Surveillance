import { afterEach, describe, expect, it, vi } from "vitest";
import { api, request, SESSION_EXPIRED_EVENT, validateLiveSnapshot } from "../src/api/client";

afterEach(() => vi.restoreAllMocks());

describe("live deployment contract", () => {
  it("rejects an old running API instead of showing zero positives and empty alerts", () => {
    expect(() => validateLiveSnapshot({ status: "synchronized" } as never)).toThrow("running API is older");
  });
  it("accepts a missing snapshot without inventing one", () => {
    expect(validateLiveSnapshot(null)).toBeNull();
  });
  it("requests precisely the selected reporting period", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("null", { status: 200 }));
    await api.latestLiveDashboard({ period_start: "2026-07-01", period_end: "2026-07-31" });
    expect(fetchMock.mock.calls[0]?.[0]).toContain("period_start=2026-07-01&period_end=2026-07-31");
  });
  it("notifies authentication when an investigation request has an expired session", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response('{"type":"about:blank","title":"Authentication required","status":401,"code":"unauthenticated","detail":"Session expired"}', { status: 401 }));
    const handler = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, handler);
    try {
      await expect(request("/investigations/queues/new")).rejects.toThrow("Session expired");
      expect(handler).toHaveBeenCalledOnce();
    } finally { window.removeEventListener(SESSION_EXPIRED_EVENT, handler); }
  });
});
