/**
 * Breadcrumbs must say where a figure belongs.
 *
 * Every drill-down keeps the trail visible (blueprint 048). A district figure
 * read under a national heading, or a facility figure read under a district
 * one, is the same mistake the API spent two commits removing; the trail is
 * the interface's half of that guarantee.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { Breadcrumbs } from "../src/design-system/Breadcrumbs";

function renderTrail(trail: { to?: string; label: string }[]) {
  return render(
    <MemoryRouter>
      <Breadcrumbs trail={trail} />
    </MemoryRouter>,
  );
}

describe("Breadcrumbs", () => {
  it("is a navigation landmark assistive technology can find", () => {
    renderTrail([{ label: "National" }]);
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
  });

  it("marks the current page rather than relying on styling alone", () => {
    renderTrail([
      { to: "/command-centre", label: "National" },
      { label: "Gulu" },
    ]);
    const current = screen.getByText("Gulu");
    expect(current).toHaveAttribute("aria-current", "page");
  });

  it("links every ancestor so a reader can climb back out", () => {
    renderTrail([
      { to: "/command-centre", label: "National" },
      { to: "/workspaces/districts/abc", label: "Gulu" },
      { label: "Gulu Regional Referral" },
    ]);
    expect(screen.getByRole("link", { name: "National" })).toHaveAttribute(
      "href",
      "/command-centre",
    );
    expect(screen.getByRole("link", { name: "Gulu" })).toHaveAttribute(
      "href",
      "/workspaces/districts/abc",
    );
  });

  it("never links the current page to itself", () => {
    renderTrail([
      { to: "/command-centre", label: "National" },
      { to: "/workspaces/districts/abc", label: "Gulu" },
    ]);
    expect(screen.queryByRole("link", { name: "Gulu" })).not.toBeInTheDocument();
  });

  it("uses a list so the depth of the trail is conveyed structurally", () => {
    const { container } = renderTrail([
      { to: "/command-centre", label: "National" },
      { label: "Gulu" },
    ]);
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });
});
