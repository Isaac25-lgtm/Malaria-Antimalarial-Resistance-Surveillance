import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RecurrenceDefinitionPanel } from "../src/features/recurrence/RecurrenceDefinitionPanel";

describe("recurrence definition panel", () => {
  it("submits an explicit frozen exploratory definition", async () => {
    const apply = vi.fn();
    render(<RecurrenceDefinitionPanel
      period={{ start: "2026-08-01", end: "2026-08-31" }}
      liveMode
      programme={{ method_code: "positive-recurrence", active: false, detail: "No approved method" }}
      definitions={[]}
      busy={false}
      saving={false}
      onSave={vi.fn()}
      onApply={apply}
    />);
    await userEvent.clear(screen.getByLabelText("Minimum days between positives"));
    await userEvent.type(screen.getByLabelText("Minimum days between positives"), "3");
    await userEvent.selectOptions(screen.getByLabelText("Positive encounters required"), "3");
    await userEvent.selectOptions(screen.getByLabelText("Same-day positives"), "review_separately");
    await userEvent.click(screen.getByText("Advanced cohort filters"));
    await userEvent.selectOptions(screen.getByLabelText("Sex"), ["female"]);
    await userEvent.type(screen.getByLabelText("Positive test methods"), "RDT");
    await userEvent.click(screen.getByRole("button", { name: "Apply definition" }));

    expect(apply).toHaveBeenCalledOnce();
    expect(apply.mock.calls[0]?.[0]).toMatchObject({
      period_start: "2026-08-01",
      period_end: "2026-08-31",
      source: "live_tracker",
      mode: "exploratory",
      parameters: {
        minimum_gap_days: 3,
        maximum_window_days: 28,
        minimum_positive_encounters: 3,
        same_day_policy: "review_separately",
      },
      filters: { sexes: ["female"], test_methods: ["RDT"] },
    });
  }, 15_000);

  it("does not offer programme mode when no approved method is active", () => {
    render(<RecurrenceDefinitionPanel
      period={{ start: "2026-08-01", end: "2026-08-31" }} liveMode={false}
      programme={{ method_code: "positive-recurrence", active: false, detail: "No approved method" }}
      definitions={[]} busy={false} saving={false} onSave={vi.fn()} onApply={vi.fn()} />);
    expect(screen.getByRole("option", { name: "Approved programme method" })).toBeDisabled();
    expect(screen.getByText(/exploratory analysis remains clearly labelled/i)).toBeInTheDocument();
  });
});
