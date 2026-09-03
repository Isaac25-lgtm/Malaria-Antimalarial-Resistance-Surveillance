/**
 * Period and measure formatting.
 *
 * Pure functions, kept out of the component files so fast refresh stays sound
 * and so the formatting rules have one home. Nothing here computes an
 * analytical value: it turns what the server returned into what a reader sees.
 */

import type { Schemas } from "../api/client";

type Measure = Schemas["SurveillanceMeasure"];

/** An ISO date string, the form the API takes and returns. */
export type IsoDate = string;

export interface PeriodSelection {
  start: IsoDate;
  end: IsoDate;
}

function iso(value: Date): IsoDate {
  return value.toISOString().slice(0, 10);
}

/**
 * A whole calendar month, offset from the current one.
 *
 * Whole months only. HMIS 105 is monthly and 033b is weekly; an arbitrary date
 * range would invite comparing windows the source never reported, which is how
 * a partial month becomes an apparent decline in cases.
 */
export function monthPeriod(offsetMonths = 0): PeriodSelection {
  const now = new Date();
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + offsetMonths, 1));
  const end = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0));
  return { start: iso(start), end: iso(end) };
}

export function formatPeriod(period: PeriodSelection): string {
  const start = new Date(`${period.start}T00:00:00Z`);
  return start.toLocaleDateString("en-GB", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function formatMoment(value: string | null | undefined): string {
  if (!value) return "Never";
  return new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

/**
 * Format a measure's value without inventing precision.
 *
 * Counts render whole. Proportions render to one decimal place, which is the
 * most a routine surveillance denominator supports; more digits would be fake
 * precision (blueprint 047).
 */
export function formatMeasureValue(measure: Measure): string {
  if (measure.value === null || measure.value === undefined) return "—";
  const numeric = Number(measure.value);
  if (Number.isNaN(numeric)) return measure.value;
  if (measure.unit === "proportion") {
    return `${(numeric * 100).toFixed(1)}%`;
  }
  return numeric.toLocaleString("en-GB", { maximumFractionDigits: 0 });
}
