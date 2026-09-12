/**
 * Wording for repeat-positive evidence, shared by every panel.
 *
 * Pure functions, kept out of component files so fast refresh stays sound and so
 * one intervals label cannot drift between the overview, the patient list and
 * the drill-down. Nothing here computes a surveillance value: it phrases what the
 * server's shared recurrence engine returned.
 */

import type { Schemas } from "../../api/client";

type LivePatient = Schemas["LiveRepeatPositivePatient"];
type Definition = NonNullable<Schemas["LiveDashboardSnapshot"]["repeat_positive_definition"]>;

const RESULT_LABELS: Record<string, string> = {
  positive: "Positive",
  negative: "Negative",
  not_recorded: "Result not recorded",
  unmapped: "Result not interpretable (unmapped)",
  not_done: "Not done",
};

/** A result as a person should read it. Never turns a gap into a negative. */
export function resultLabel(result: string): string {
  return RESULT_LABELS[result] ?? result;
}

const DETERMINATION_LABELS: Record<string, string> = {
  qualifies: "Meets definition",
  does_not_qualify: "Does not meet definition",
  indeterminate: "Cannot be determined — evidence incomplete",
  not_in_period: "No positive in this period",
};

export function determinationLabel(value: string | null | undefined): string {
  if (!value) return "Not evaluated";
  return DETERMINATION_LABELS[value] ?? value;
}

/** A day count, or a dash when there is no interval to state. */
export function days(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value} day${value === 1 ? "" : "s"}`;
}

/** Positive-to-positive gaps in order, e.g. "9d → 14d". */
export function gapsLabel(values: readonly number[] | null | undefined): string {
  if (!values || values.length === 0) return "—";
  return values.map((value) => `${value}d`).join(" → ");
}

/**
 * Whether a patient meets the applied definition.
 *
 * Deliberately not ``positive_encounter_count > 1``: two positives one day apart
 * are two eligible positives, not a repeat-positive pattern under a definition
 * with a minimum gap.
 */
export function isRepeatPositive(patient: LivePatient): boolean {
  return patient.determination === "qualifies";
}

const SAME_DAY_LABELS: Record<string, string> = {
  exclude: "same-day records counted once",
  review_separately: "same-day records counted once and queued for review",
  include: "same-day records counted separately",
};

/** The applied definition in one sentence, with its exploratory status first. */
export function definitionSummary(definition: Definition | null | undefined): string {
  if (!definition) return "This snapshot does not report which repeat-positive definition it used.";
  const status = definition.exploratory
    ? "Exploratory preset, not an approved programme method"
    : definition.name;
  const sameDay = SAME_DAY_LABELS[definition.same_day_policy] ?? definition.same_day_policy;
  return (
    `${status}: at least ${definition.minimum_positive_encounters} confirmed positive encounters, ` +
    `each at least ${days(definition.minimum_gap_days)} after the previous and all within ` +
    `${days(definition.maximum_window_days)} of the first; ${sameDay}.`
  );
}

/**
 * Treatment context is a separate coverage dimension from the recurrence
 * calculation: laboratory evidence can be complete while visit or medicine
 * records are missing, and missing is never "no treatment".
 */
export function treatmentContextNote(value: string | null | undefined): string {
  if (value === "partial") {
    return "Treatment context is partial: visit or medicine records were not returned for some facilities, so treatment there is unavailable, not absent.";
  }
  if (value === "not_mapped") return "Treatment context is not mapped for this source.";
  if (value === "complete") return "Treatment context was retrieved for every facility.";
  return "";
}

type ScopedUser = {
  username?: string | null;
  max_sensitivity?: string | null;
  geography_scopes?: unknown;
  facility_scopes?: unknown;
} | null | undefined;

/**
 * Who is asking, and over what scope, as one stable string for query keys.
 *
 * Patient queries carry it so a cached page can never answer another user or
 * a changed scope; sign-out and session expiry also clear the whole cache.
 */
export function scopeIdentity(user: ScopedUser): string {
  if (!user) return "anonymous";
  return JSON.stringify([
    user.username ?? "",
    user.max_sensitivity ?? "",
    user.geography_scopes ?? [],
    user.facility_scopes ?? [],
  ]);
}
