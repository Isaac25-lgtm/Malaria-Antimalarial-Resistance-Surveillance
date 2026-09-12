import type { Schemas } from "../../api/client";

export type RunView = Schemas["RunView"];
export type ProjectionView = Schemas["ProjectionView"];
export type PatientPage = Schemas["PatientPageView"];
export type PatientDetail = Schemas["PatientDetailView"];

export interface RecurrenceParameters {
  minimum_gap_days: number;
  maximum_window_days: number;
  minimum_positive_encounters: number;
  same_day_policy: "exclude" | "include" | "review_separately";
  permitted_positive_methods: string[];
  require_linked_treatment: boolean;
  quality_exclusions: string[];
}

export interface RecurrenceFilters {
  facility_refs: string[];
  sexes: string[];
  age_groups: string[];
  quality: string[];
  test_methods: string[];
  treatments: string[];
}

export const STARTING_PARAMETERS: RecurrenceParameters = {
  minimum_gap_days: 7,
  maximum_window_days: 28,
  minimum_positive_encounters: 2,
  same_day_policy: "exclude",
  permitted_positive_methods: [],
  require_linked_treatment: false,
  quality_exclusions: [],
};

export const STARTING_FILTERS: RecurrenceFilters = {
  facility_refs: [],
  sexes: [],
  age_groups: [],
  quality: [],
  test_methods: [],
  treatments: [],
};

export type JsonRow = Record<string, unknown>;

export function rows(value: unknown): JsonRow[] {
  return Array.isArray(value)
    ? value.filter((item): item is JsonRow => typeof item === "object" && item !== null)
    : [];
}

export function object(value: unknown): JsonRow {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonRow
    : {};
}

export function text(value: unknown, fallback = "Not recorded"): string {
  return typeof value === "string" && value ? value : fallback;
}

export function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
