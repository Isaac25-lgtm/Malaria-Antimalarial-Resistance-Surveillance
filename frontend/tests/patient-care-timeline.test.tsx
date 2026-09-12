import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api/client";
import { PatientCareTimeline } from "../src/features/recurrence/PatientCareTimeline";

afterEach(() => vi.restoreAllMocks());

describe("recurrence patient care timeline", () => {
  it("shows encounter-linked treatment and the interpretation boundary", async () => {
    vi.spyOn(api, "recurrencePatient").mockResolvedValue({
      run_id: "run-1",
      finding: { determination: "qualifies", eligible_positive_count: 2, chain_interval_days: 9, facility_count: 1, quality_flags: ["VALID"] },
      timeline: { entries: [{ ref: "E1", encounter_date: "2026-08-04", facility_name: "Pader HC III", tests: [{ method: "RDT", result: "positive" }], diagnoses: ["Malaria"], observations: ["Fever: Yes"], fever: "yes", attendance: "Outpatient", medications: [{ name: "Artemether/Lumefantrine 20/120 mg", evidence: "recorded" }], referrals: [], outcome: "outpatient", decision: { counted: true, reason: "confirmed_positive" } }], transitions: [{ from_ref: "E1", to_ref: "E2", days: 9, prior_medications: ["Artemether/Lumefantrine"], prior_treatment_evidence: "recorded" }] },
      definition_name: "Exploratory definition",
      definition: {}, engine_version: "positive-recurrence/1.0.0", exploratory: true,
      interpretation: "Repeat positivity requires investigation and does not confirm resistance.",
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><PatientCareTimeline runId="run-1" alias="MARS-PT2-TEST" /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("Pader HC III")).toBeInTheDocument();
    expect(screen.getByText(/Artemether\/Lumefantrine 20\/120 mg/)).toBeInTheDocument();
    expect(screen.getByText("Fever: Yes; attendance: Outpatient")).toBeInTheDocument();
    expect(screen.getByText(/9 days; prior treatment/)).toBeInTheDocument();
    expect(screen.getByText(/does not confirm resistance/i)).toBeInTheDocument();
  });
});
