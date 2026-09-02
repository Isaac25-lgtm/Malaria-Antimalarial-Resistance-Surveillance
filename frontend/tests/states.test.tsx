/**
 * The four data states must be distinguishable.
 *
 * Blueprint appendix 144: empty, no-data, forbidden and unavailable must not
 * look identical. These tests assert that each renders its own wording and its
 * own status treatment, so a future refactor cannot quietly collapse them into
 * one generic "nothing here" box.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  EmptyState,
  ForbiddenState,
  LoadingState,
  NoDataState,
  StaleBanner,
  UnavailableState,
} from "../src/design-system/States";

describe("LoadingState", () => {
  it("announces what is loading to assistive technology", () => {
    render(<LoadingState label="facilities" />);
    expect(screen.getByText("Loading facilities")).toBeInTheDocument();
  });

  it("marks the region as busy", () => {
    const { container } = render(<LoadingState label="facilities" />);
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("states that nothing matched without implying a fault", () => {
    render(<EmptyState title="No signals in this period" description="Nothing matched." />);
    expect(screen.getByText("No signals in this period")).toBeInTheDocument();
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
  });
});

describe("NoDataState", () => {
  it("is visually and textually distinct from an empty result", () => {
    const { container } = render(
      <NoDataState title="No boundary version" description="Nothing imported." />,
    );
    // Carries its own chip, which EmptyState does not.
    expect(screen.getByText("No data")).toBeInTheDocument();
    expect(container.querySelector(".state--no-data")).toBeInTheDocument();
    expect(container.querySelector(".state--empty")).not.toBeInTheDocument();
  });

  it("names what it is waiting for, so the gap is actionable", () => {
    render(
      <NoDataState
        title="No facilities"
        description="None loaded."
        awaiting="the national facility master"
      />,
    );
    expect(screen.getByText(/Awaiting: the national facility master/)).toBeInTheDocument();
  });
});

describe("ForbiddenState", () => {
  it("names the missing grant rather than the resource", () => {
    render(<ForbiddenState requirement="organisation:view" />);
    expect(screen.getByText("Requires: organisation:view")).toBeInTheDocument();
  });

  it("is announced as an alert", () => {
    render(<ForbiddenState requirement="facility:view" />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("does not reuse the no-data treatment", () => {
    const { container } = render(<ForbiddenState requirement="audit:view" />);
    expect(container.querySelector(".state--forbidden")).toBeInTheDocument();
    expect(container.querySelector(".state--no-data")).not.toBeInTheDocument();
  });
});

describe("UnavailableState", () => {
  it("surfaces the request identifier for a support conversation", () => {
    render(
      <UnavailableState
        title="Readiness unknown"
        description="The API did not answer."
        requestId="req-42"
      />,
    );
    expect(screen.getByText("Request ID: req-42")).toBeInTheDocument();
  });

  it("offers a retry when one is possible", () => {
    render(
      <UnavailableState title="Failed" description="No answer." onRetry={() => undefined} />,
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("is the only state that uses the priority treatment", () => {
    const { container } = render(<UnavailableState title="Failed" description="No answer." />);
    expect(container.querySelector(".chip--priority")).toBeInTheDocument();
  });
});

describe("StaleBanner", () => {
  it("states when the data was last good rather than hiding it", () => {
    render(<StaleBanner lastRefreshed="04:12 today" affected="monthly indicators" />);
    expect(screen.getByRole("status")).toHaveTextContent("04:12 today");
    expect(screen.getByRole("status")).toHaveTextContent("monthly indicators");
  });
});
