import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type Schemas } from "../src/api/client";
import { DefinitionComparison } from "../src/features/recurrence/DefinitionComparison";

afterEach(() => vi.restoreAllMocks());

const run = (id: string, name: string): Schemas["RunView"] => ({
  id, status: "completed", error_code: null, mode: "exploratory", source: "stored_encounter",
  definition_name: name, definition: {}, definition_checksum: id, definition_version_id: null,
  method_version_id: null, exploratory: true, engine_version: "positive-recurrence/1.0.0",
  period_start: "2026-08-01", period_end: "2026-08-31", timezone: "Africa/Kampala",
  facility_count: 1, national_scope: false, filters: {}, manifest_checksum: id,
  created_by: "epidemiologist", created_at: "2026-09-11T00:00:00Z", completed_at: "2026-09-11T00:01:00Z",
  progress_completed: 3, progress_total: 3, dataset: {}, summary: {}, coverage: {},
  interpretation: "Not proof of resistance.",
});

describe("definition comparison", () => {
  it("shows overlap and differences returned by the server", async () => {
    vi.spyOn(api, "compareRecurrenceRuns").mockResolvedValue({ overlap: { a_only: ["A"], both: ["B", "C"], b_only: [] }, differences: { facilities: [{ key: "Pader HC III", a: 4, b: 2, delta: -2 }] } });
    const client = new QueryClient();
    render(<QueryClientProvider client={client}><DefinitionComparison current={run("a", "Gap 3")}
      runs={[run("a", "Gap 3"), run("b", "Gap 7")]} /></QueryClientProvider>);
    await userEvent.selectOptions(screen.getByLabelText("Comparison run"), "b");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByText("Pader HC III")).toBeInTheDocument();
    expect(screen.getByText("Both definitions").nextSibling).toHaveTextContent("2");
  });
});
