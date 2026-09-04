/**
 * The signal evidence screen must be arguable with.
 *
 * A signal that a district officer cannot check is a signal they will either
 * ignore or over-trust. These tests pin the properties that make it checkable:
 * counter-evidence is shown beside supporting evidence, the governed rule and
 * method version are on the page, and a signal outside your scope is
 * indistinguishable from one that does not exist.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../src/api/client";
import { SignalEvidenceView } from "../src/features/signals/SignalEvidenceView";

const SIGNAL = {
  id: "11111111-1111-4111-8111-111111111111",
  signal_type: "facility_temporal_anomaly",
  status: "active",
  priority: "high",
  geography_unit_id: null,
  facility_id: null,
  period_start: "2026-07-01",
  period_end: "2026-07-31",
  title: "Facility Temporal Anomaly",
  statement: "A pattern requiring investigation.",
  score: 3.5,
  evidence_count: 1,
  counter_evidence_count: 1,
  data_quality: { sources: [] },
  uncertainty: [
    "This routine-data signal identifies a pattern requiring investigation. It does not confirm treatment failure, recrudescence, reinfection, or resistance.",
  ],
  recommended_action_codes: [],
  method_version_id: "22222222-2222-4222-8222-222222222222",
  rule_code: "temporal_rule",
  input_fingerprint: "a".repeat(64),
  group_key: "b".repeat(64),
  source_cutoff: "2026-08-01T00:00:00Z",
  generated_at: "2026-08-02T00:00:00Z",
  supersedes_id: null,
  superseded_by_id: null,
  evidence: [
    {
      kind: "temporal_anomaly",
      role: "supporting",
      source_table: "temporal_anomaly_result",
      source_record_id: "33333333-3333-4333-8333-333333333333",
      contribution: 2,
      summary: "Positivity departed from its own history.",
      facts: {},
      quality_context: null,
    },
    {
      kind: "reconciliation",
      role: "counter",
      source_table: "reconciliation_finding",
      source_record_id: "44444444-4444-4444-8444-444444444444",
      contribution: -1,
      summary: "Aggregate return disagrees with the encounter count.",
      facts: {},
      quality_context: null,
    },
  ],
};

function renderView() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[`/signals/${SIGNAL.id}`]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/signals/:signalId" element={<SignalEvidenceView />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SignalEvidenceView", () => {
  it("shows counter-evidence beside supporting evidence", async () => {
    vi.spyOn(api, "signal").mockResolvedValue(SIGNAL);
    vi.spyOn(api, "signalExplanation").mockRejectedValue(
      new ApiError(404, null, "no explanation"),
    );

    renderView();

    await waitFor(() => {
      expect(
        screen.getByText("Aggregate return disagrees with the encounter count."),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("supporting")).toBeInTheDocument();
    expect(screen.getByText("counter")).toBeInTheDocument();
  });

  it("shows the governed rule and method version that produced it", async () => {
    vi.spyOn(api, "signal").mockResolvedValue(SIGNAL);
    vi.spyOn(api, "signalExplanation").mockRejectedValue(
      new ApiError(404, null, "no explanation"),
    );

    renderView();

    await waitFor(() => {
      expect(screen.getByText("temporal_rule")).toBeInTheDocument();
    });
    expect(screen.getByText(SIGNAL.method_version_id)).toBeInTheDocument();
    expect(screen.getByText(SIGNAL.input_fingerprint)).toBeInTheDocument();
  });

  it("carries the interpretation boundary from the signal's own words", async () => {
    vi.spyOn(api, "signal").mockResolvedValue(SIGNAL);
    vi.spyOn(api, "signalExplanation").mockRejectedValue(
      new ApiError(404, null, "no explanation"),
    );

    renderView();

    await waitFor(() => {
      expect(screen.getByText(/does not confirm treatment failure/)).toBeInTheDocument();
    });
  });

  it("does not invent an explanation when none has been generated", async () => {
    vi.spyOn(api, "signal").mockResolvedValue(SIGNAL);
    vi.spyOn(api, "signalExplanation").mockRejectedValue(
      new ApiError(404, null, "no explanation"),
    );

    renderView();

    await waitFor(() => {
      expect(screen.getByText("No explanation has been generated")).toBeInTheDocument();
    });
    expect(screen.getByText(/rather than composing a description of its own/)).toBeInTheDocument();
  });

  it("treats an out-of-scope signal the same as one that does not exist", async () => {
    // Distinguishing them would disclose that something was flagged there.
    vi.spyOn(api, "signal").mockRejectedValue(new ApiError(403, null, "forbidden"));
    vi.spyOn(api, "signalExplanation").mockRejectedValue(
      new ApiError(403, null, "forbidden"),
    );

    renderView();

    await waitFor(() => {
      expect(
        screen.getByText("Signal not found, or outside your authorised scope"),
      ).toBeInTheDocument();
    });
  });

  it("distinguishes a server failure from an absent signal", async () => {
    vi.spyOn(api, "signal").mockRejectedValue(new ApiError(503, null, "unavailable"));
    vi.spyOn(api, "signalExplanation").mockRejectedValue(
      new ApiError(503, null, "unavailable"),
    );

    renderView();

    await waitFor(() => {
      expect(screen.getByText("The signal could not be loaded")).toBeInTheDocument();
    });
    expect(screen.getByText(/not a statement that the signal was withdrawn/)).toBeInTheDocument();
  });
});
