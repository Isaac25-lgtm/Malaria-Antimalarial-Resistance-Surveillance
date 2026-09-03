/**
 * A KPI card must never turn an absence into a number.
 *
 * This is the single most consequential rendering rule in MARS. A zero on the
 * national screen is read as "no malaria here"; a blank is read the same way.
 * The API distinguishes available, unavailable and not-configured, and these
 * tests assert the interface carries that distinction all the way to the text
 * a Ministry of Health user actually reads.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../src/api/client";
import { MeasureCard, MeasureGrid } from "../src/design-system/Measure";
import { formatMeasureValue } from "../src/design-system/period";

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
    source: "indicator:ENC_CONFIRMED_MALARIA",
    method_version_id: null,
    source_freshness: null,
    comparison: null,
    status: "available",
    status_detail: null,
    missing_configuration: [],
    ...overrides,
  } as Measure;
}

describe("a measure with no value", () => {
  it("never renders a zero when it is not configured", () => {
    render(
      <MeasureCard
        measure={measure({
          status: "not_configured",
          value: null,
          numerator: null,
          status_detail: "No approved version.",
          missing_configuration: ["indicator_version:ENC_CONFIRMED_MALARIA"],
        })}
      />,
    );
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
  });

  it("names what configuration is missing", () => {
    render(
      <MeasureCard
        measure={measure({
          status: "not_configured",
          value: null,
          missing_configuration: ["indicator_version:ENC_TESTED_MALARIA"],
        })}
      />,
    );
    expect(
      screen.getByText(/indicator_version:ENC_TESTED_MALARIA/),
    ).toBeInTheDocument();
  });

  it("repeats the server's own explanation rather than inventing one", () => {
    const detail = "No result has been materialised for this period and scope.";
    render(
      <MeasureCard
        measure={measure({ status: "unavailable", value: null, status_detail: detail })}
      />,
    );
    expect(screen.getByText(detail)).toBeInTheDocument();
  });

  it("distinguishes an unconfigured measure from one with no figure", () => {
    const { rerender } = render(
      <MeasureCard measure={measure({ status: "not_configured", value: null })} />,
    );
    expect(screen.getByText("Not configured")).toBeInTheDocument();

    rerender(<MeasureCard measure={measure({ status: "unavailable", value: null })} />);
    expect(screen.getByText("No figure")).toBeInTheDocument();
    expect(screen.queryByText("Not configured")).not.toBeInTheDocument();
  });
});

describe("a measure with a value", () => {
  it("renders a genuine zero as a figure", () => {
    // Zero confirmed cases is a real answer. Only an *absent* value is hidden.
    render(<MeasureCard measure={measure({ value: "0", numerator: 0 })} />);
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("shows the denominator a proportion was taken over", () => {
    render(
      <MeasureCard
        measure={measure({
          unit: "proportion",
          value: "0.732000",
          numerator: 732,
          denominator: 1000,
        })}
      />,
    );
    expect(screen.getByText("73.2%")).toBeInTheDocument();
    expect(screen.getByText("732 of 1,000")).toBeInTheDocument();
  });

  it("names the comparison period rather than showing a bare arrow", () => {
    render(
      <MeasureCard
        measure={measure({
          comparison: {
            period: { start: "2026-06-01", end: "2026-06-30" },
            value: "1000",
            direction: "up",
            status: "available",
            status_detail: null,
          },
        })}
      />,
    );
    expect(screen.getByText(/2026-06-01 to 2026-06-30/)).toBeInTheDocument();
  });

  it("always states the source", () => {
    render(<MeasureCard measure={measure()} />);
    expect(screen.getByText("indicator:ENC_CONFIRMED_MALARIA")).toBeInTheDocument();
  });
});

describe("formatMeasureValue", () => {
  it("does not invent precision beyond one decimal for a proportion", () => {
    expect(
      formatMeasureValue(measure({ unit: "proportion", value: "0.123456" })),
    ).toBe("12.3%");
  });

  it("renders an absent value as an em dash, never as zero", () => {
    expect(formatMeasureValue(measure({ value: null }))).toBe("—");
  });
});

describe("MeasureGrid", () => {
  it("renders one card per governed measure", () => {
    render(
      <MeasureGrid
        measures={[
          measure({ code: "A", label: "Attendances" }),
          measure({ code: "B", label: "Tested", status: "not_configured", value: null }),
        ]}
      />,
    );
    expect(screen.getByText("Attendances")).toBeInTheDocument();
    expect(screen.getByText("Tested")).toBeInTheDocument();
  });
});
