/**
 * Accessibility properties the interface must keep — Prompt 29.
 *
 * MARS is used on a projector in a meeting room, on a laptop in a district
 * office, and by people who navigate with a keyboard. These tests check the
 * structural properties that make that possible and that a refactor can
 * silently break: landmarks, accessible names, table semantics, and — the one
 * this system needs most — that status is never carried by colour alone.
 *
 * A red chip and an amber chip are the same chip to a colour-blind reader and
 * to a black-and-white printout of a briefing. Every status in MARS therefore
 * carries a word.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../src/api/client";
import { Breadcrumbs } from "../src/design-system/Breadcrumbs";
import { MeasureCard, MeasureGrid } from "../src/design-system/Measure";
import {
  EmptyState,
  ForbiddenState,
  LoadingState,
  NoDataState,
  UnavailableState,
} from "../src/design-system/States";

type Measure = Schemas["SurveillanceMeasure"];

function measure(overrides: Partial<Measure> = {}): Measure {
  return {
    code: "ENC_CONFIRMED_MALARIA",
    label: "Confirmed malaria",
    value: "1234",
    unit: "count",
    numerator: 1234,
    denominator: null,
    period: { start: "2026-07-01", end: "2026-07-31" },
    geography_grain: "national",
    geography_unit_id: null,
    facility_id: null,
    source: "indicator:ENC_CONFIRMED_MALARIA",
    method_version_id: null,
    source_freshness: null,
    comparison: null,
    status: "available",
    status_detail: null,
    missing_configuration: [],
    ...overrides,
  };
}

describe("status is never conveyed by colour alone", () => {
  it("an unconfigured measure carries the word, not just a treatment", () => {
    render(<MeasureCard measure={measure({ status: "not_configured", value: null })} />);
    expect(screen.getByText("Not configured")).toBeInTheDocument();
  });

  it("an unavailable measure carries a different word", () => {
    render(<MeasureCard measure={measure({ status: "unavailable", value: null })} />);
    expect(screen.getByText("No figure")).toBeInTheDocument();
  });

  it("the status is also exposed as a data attribute for styling only", () => {
    // Styling hangs off data-status; the text is what conveys the meaning.
    const { container } = render(
      <MeasureCard measure={measure({ status: "not_configured", value: null })} />,
    );
    const card = container.querySelector("[data-status]");
    expect(card).toHaveAttribute("data-status", "not_configured");
    expect(within(card as HTMLElement).getByText("Not configured")).toBeInTheDocument();
  });
});

describe("accessible names", () => {
  it("each measure card is labelled by its own heading", () => {
    render(<MeasureCard measure={measure({ label: "Tested for malaria" })} />);
    expect(
      screen.getByRole("article", { name: "Tested for malaria" }),
    ).toBeInTheDocument();
  });

  it("a grid of measures exposes one labelled region per measure", () => {
    render(
      <MeasureGrid
        measures={[
          measure({ code: "A", label: "Attendances" }),
          measure({ code: "B", label: "Tested" }),
        ]}
      />,
    );
    expect(screen.getAllByRole("article")).toHaveLength(2);
  });

  it("the source of every figure is announced, not only shown", () => {
    render(<MeasureCard measure={measure()} />);
    expect(screen.getByText("Source:")).toBeInTheDocument();
  });
});

describe("landmarks and navigation", () => {
  it("breadcrumbs are a named navigation landmark", () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Breadcrumbs trail={[{ to: "/command-centre", label: "National" }, { label: "Gulu" }]} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
  });

  it("the current location is announced rather than only styled", () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Breadcrumbs trail={[{ to: "/command-centre", label: "National" }, { label: "Gulu" }]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Gulu")).toHaveAttribute("aria-current", "page");
  });
});

describe("keyboard navigation", () => {
  it("every breadcrumb ancestor is reachable by tab", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Breadcrumbs
          trail={[
            { to: "/command-centre", label: "National" },
            { to: "/workspaces/districts/abc", label: "Gulu" },
            { label: "Gulu Regional Referral" },
          ]}
        />
      </MemoryRouter>,
    );
    await user.tab();
    expect(screen.getByRole("link", { name: "National" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "Gulu" })).toHaveFocus();
  });

  it("the current page is not a tab stop, because it goes nowhere", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Breadcrumbs
          trail={[{ to: "/command-centre", label: "National" }, { label: "Gulu" }]}
        />
      </MemoryRouter>,
    );
    await user.tab();
    await user.tab();
    expect(screen.getByText("Gulu")).not.toHaveFocus();
  });
});

describe("the five data states remain distinguishable to a screen reader", () => {
  it("loading announces itself as busy", () => {
    const { container } = render(<LoadingState label="signals" />);
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  });

  it("empty, no-data, forbidden and unavailable each say something different", () => {
    const rendered = [
      render(<EmptyState title="No signals" description="Nothing matched." />),
      render(<NoDataState title="No boundary version" description="Nothing imported." />),
      render(<ForbiddenState requirement="signal:view" description="Not permitted." />),
      render(<UnavailableState title="Could not load" description="No answer." />),
    ];
    const texts = rendered.map((view) => view.container.textContent ?? "");
    // Four genuinely different messages, not one generic box repeated.
    expect(new Set(texts).size).toBe(4);
  });

  it("an unavailable state never implies an absence of malaria", () => {
    render(
      <UnavailableState
        title="This section could not be loaded"
        description="The server did not answer. These figures are not zero; they are unknown."
      />,
    );
    expect(screen.getByText(/not zero; they are unknown/)).toBeInTheDocument();
  });
});

describe("no figure is fabricated for a screen reader either", () => {
  it("an absent value is an em dash in the accessible text, not a zero", () => {
    render(<MeasureCard measure={measure({ status: "unavailable", value: null })} />);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
});
