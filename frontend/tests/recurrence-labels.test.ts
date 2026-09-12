import { describe, expect, it } from "vitest";

import type { Schemas } from "../src/api/client";
import {
  days,
  definitionSummary,
  determinationLabel,
  gapsLabel,
  isRepeatPositive,
  resultLabel,
  scopeIdentity,
  treatmentContextNote,
} from "../src/features/recurrence/recurrenceLabels";

type LivePatient = Schemas["LiveRepeatPositivePatient"];

function patient(overrides: Partial<LivePatient>): LivePatient {
  return {
    mars_patient_id: "MARS-PT-TEST",
    first_positive_on: "2026-08-01",
    latest_positive_on: "2026-08-02",
    positive_encounter_count: 2,
    facility_name: "Facility",
    cross_facility: false,
    ...overrides,
  } as LivePatient;
}

describe("recurrence wording", () => {
  it("never presents a missing or unmapped result as negative", () => {
    expect(resultLabel("not_recorded")).toBe("Result not recorded");
    expect(resultLabel("unmapped")).toContain("unmapped");
    expect(resultLabel("negative")).toBe("Negative");
  });

  it("states indeterminate evidence rather than a zero", () => {
    expect(determinationLabel("indeterminate")).toContain("evidence incomplete");
    expect(determinationLabel(null)).toBe("Not evaluated");
  });

  it("names intervals and shows a dash when there is none", () => {
    expect(days(1)).toBe("1 day");
    expect(days(23)).toBe("23 days");
    expect(days(null)).toBe("—");
    expect(gapsLabel([9, 14])).toBe("9d → 14d");
    expect(gapsLabel([])).toBe("—");
  });

  it("uses the definition, not a bare positive count, for repeat-positive status", () => {
    expect(isRepeatPositive(patient({ positive_encounter_count: 2, determination: "does_not_qualify" }))).toBe(false);
    expect(isRepeatPositive(patient({ determination: "qualifies" }))).toBe(true);
  });

  it("labels an exploratory definition as not approved", () => {
    const text = definitionSummary({
      name: "Exploratory starting preset (not approved)",
      minimum_gap_days: 7,
      maximum_window_days: 28,
      minimum_positive_encounters: 2,
      same_day_policy: "exclude",
      engine_version: "positive-recurrence/1.0.0",
      exploratory: true,
    });
    expect(text).toContain("not an approved programme method");
    expect(text).toContain("at least 7 days after the previous");
    expect(text).toContain("within 28 days of the first");
    expect(text.toLowerCase()).not.toContain("resistan");
  });

  it("admits when a snapshot did not report its definition", () => {
    expect(definitionSummary(null)).toContain("does not report");
  });

  it("states partial treatment context as unavailable, never as no treatment", () => {
    const note = treatmentContextNote("partial");
    expect(note).toContain("unavailable, not absent");
    expect(note.toLowerCase()).not.toContain("no treatment was given");
    expect(treatmentContextNote("not_mapped")).toContain("not mapped");
    expect(treatmentContextNote(undefined)).toBe("");
  });

  it("keys patient queries by who is asking and over what scope", () => {
    const district = { username: "officer", max_sensitivity: "pseudonymous_case", geography_scopes: [{ id: "pader" }] };
    const other = { ...district, geography_scopes: [{ id: "gulu" }] };
    expect(scopeIdentity(district)).toBe(scopeIdentity({ ...district }));
    expect(scopeIdentity(district)).not.toBe(scopeIdentity(other));
    expect(scopeIdentity(null)).toBe("anonymous");
  });
});
