import { useEffect, useState } from "react";

import type { Schemas } from "../../api/client";
import type { PeriodSelection } from "../../design-system/period";
import {
  STARTING_FILTERS,
  STARTING_PARAMETERS,
  type RecurrenceFilters,
  type RecurrenceParameters,
} from "./types";

interface Props {
  period: PeriodSelection;
  liveMode: boolean;
  programme: Schemas["ProgrammeStatusView"] | undefined;
  definitions: Schemas["DefinitionView"][];
  busy: boolean;
  saving: boolean;
  onSave: (name: string, parameters: RecurrenceParameters) => void;
  onApply: (request: Schemas["RunCreate"]) => void;
}

function splitValues(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

export function RecurrenceDefinitionPanel({
  period,
  liveMode,
  programme,
  definitions,
  busy,
  saving,
  onSave,
  onApply,
}: Props) {
  const [parameters, setParameters] = useState(STARTING_PARAMETERS);
  const [filters, setFilters] = useState(STARTING_FILTERS);
  const [mode, setMode] = useState<"exploratory" | "programme">("exploratory");
  const [definitionVersionId, setDefinitionVersionId] = useState("");
  const [definitionName, setDefinitionName] = useState("");
  const [applied, setApplied] = useState("");
  const serialised = JSON.stringify({ parameters, filters, period, mode, liveMode, definitionVersionId });
  const dirty = applied !== serialised;

  useEffect(() => {
    if (mode === "programme" && !programme?.active) setMode("exploratory");
  }, [mode, programme?.active]);

  const update = <K extends keyof RecurrenceParameters>(key: K, value: RecurrenceParameters[K]) =>
    setParameters((current) => ({ ...current, [key]: value }));
  const updateFilter = <K extends keyof RecurrenceFilters>(key: K, value: RecurrenceFilters[K]) =>
    setFilters((current) => ({ ...current, [key]: value }));

  const selectDefinition = (versionId: string) => {
    setDefinitionVersionId(versionId);
    const version = definitions.flatMap((definition) => definition.versions)
      .find((candidate) => candidate.id === versionId);
    if (version) setParameters({ ...STARTING_PARAMETERS, ...version.parameters });
  };
  const activeFilters: Partial<RecurrenceFilters> = {};
  if (filters.facility_refs.length) activeFilters.facility_refs = filters.facility_refs;
  if (filters.sexes.length) activeFilters.sexes = filters.sexes;
  if (filters.age_groups.length) activeFilters.age_groups = filters.age_groups;
  if (filters.quality.length) activeFilters.quality = filters.quality;
  if (filters.test_methods.length) activeFilters.test_methods = filters.test_methods;
  if (filters.treatments.length) activeFilters.treatments = filters.treatments;

  return (
    <section className="panel recurrence-definition" aria-labelledby="definition-heading">
      <div className="panel__header recurrence-definition__heading">
        <div>
          <h2 id="definition-heading">Repeat-positive definition</h2>
          <p>Apply a frozen, reproducible surveillance query to authorised evidence.</p>
        </div>
        <span className={`chip ${mode === "programme" ? "" : "chip--attention"}`}>
          {mode === "programme" ? "Programme method" : "Exploratory"}
        </span>
      </div>
      <div className="panel__body recurrence-definition__body">
        <label>Analysis mode
          <select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
            <option value="exploratory">Exploratory analysis</option>
            <option value="programme" disabled={!programme?.active}>Approved programme method</option>
          </select>
        </label>
        <label>Saved definition
          <select value={definitionVersionId} disabled={mode === "programme"}
            onChange={(event) => selectDefinition(event.target.value)}>
            <option value="">Unsaved parameters</option>
            {definitions.flatMap((definition) => definition.versions.map((version) => (
              <option key={version.id} value={version.id}>{definition.name} · v{version.version_number}</option>
            )))}
          </select>
        </label>
        <label>Minimum days between positives
          <input type="number" min={0} max={366} value={parameters.minimum_gap_days}
            disabled={mode === "programme"}
            onChange={(event) => update("minimum_gap_days", Number(event.target.value))} />
        </label>
        <label>Maximum follow-up days
          <input type="number" min={parameters.minimum_gap_days} max={366}
            value={parameters.maximum_window_days} disabled={mode === "programme"}
            onChange={(event) => update("maximum_window_days", Number(event.target.value))} />
        </label>
        <label>Positive encounters required
          <select value={parameters.minimum_positive_encounters} disabled={mode === "programme"}
            onChange={(event) => update("minimum_positive_encounters", Number(event.target.value))}>
            {[2, 3, 4, 5, 6].map((value) => <option key={value} value={value}>At least {value}</option>)}
          </select>
        </label>
        <label>Same-day positives
          <select value={parameters.same_day_policy} disabled={mode === "programme"}
            onChange={(event) => update("same_day_policy", event.target.value as RecurrenceParameters["same_day_policy"])}>
            <option value="exclude">Exclude as possible duplicates</option>
            <option value="review_separately">Review separately</option>
            <option value="include">Include distinct encounters</option>
          </select>
        </label>
        <label className="recurrence-definition__check">
          <input type="checkbox" checked={parameters.require_linked_treatment}
            disabled={mode === "programme"}
            onChange={(event) => update("require_linked_treatment", event.target.checked)} />
          Require recorded linked treatment before a later positive
        </label>
        <details className="recurrence-definition__advanced">
          <summary>Advanced cohort filters</summary>
          <div>
            <label>Sex
              <select multiple value={filters.sexes}
                onChange={(event) => updateFilter("sexes", [...event.target.selectedOptions].map((option) => option.value))}>
                <option value="female">Female</option><option value="male">Male</option><option value="unknown">Unknown</option>
              </select>
            </label>
            <label>Age group
              <select multiple value={filters.age_groups}
                onChange={(event) => updateFilter("age_groups", [...event.target.selectedOptions].map((option) => option.value))}>
                {["under_5", "5_to_14", "15_and_over", "unknown"].map((value) =>
                  <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
              </select>
            </label>
            <label>Data-quality eligibility
              <select multiple value={filters.quality}
                onChange={(event) => updateFilter("quality", [...event.target.selectedOptions].map((option) => option.value))}>
                {["VALID", "POSSIBLE_DUPLICATE", "INCOMPLETE_TEST_EVIDENCE", "INCOMPLETE_TREATMENT_EVIDENCE", "IDENTITY_LINKAGE_UNCERTAIN", "DATE_QUALITY_ISSUE"].map((value) =>
                  <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
              </select>
            </label>
            <label>Facility source IDs
              <input value={filters.facility_refs.join(", ")} placeholder="All authorised facilities"
                onChange={(event) => updateFilter("facility_refs", splitValues(event.target.value))} />
            </label>
            <label>Positive test methods
              <input value={filters.test_methods.join(", ")} placeholder="All mapped methods"
                onChange={(event) => updateFilter("test_methods", splitValues(event.target.value))} />
            </label>
            <label>Treatment regimens
              <input value={filters.treatments.join(", ")} placeholder="All recorded treatments"
                onChange={(event) => updateFilter("treatments", splitValues(event.target.value).map((item) => item.toLocaleLowerCase()))} />
            </label>
          </div>
          <p>Use Ctrl/Command to select more than one option. Filters narrow patients after the complete longitudinal sequence is reconstructed.</p>
        </details>
        <div className="recurrence-definition__save">
          <input aria-label="Definition name" value={definitionName} maxLength={160}
            placeholder="Name this definition" onChange={(event) => setDefinitionName(event.target.value)} />
          <button type="button" className="button" disabled={saving || !definitionName.trim() || mode === "programme"}
            onClick={() => onSave(definitionName.trim(), parameters)}>
            {saving ? "Saving…" : "Save definition"}
          </button>
        </div>
        <button type="button" className="button button--primary recurrence-definition__apply"
          disabled={busy || !dirty || parameters.maximum_window_days < parameters.minimum_gap_days}
          onClick={() => {
            setApplied(serialised);
            onApply({
              period_start: period.start,
              period_end: period.end,
              source: liveMode ? "live_tracker" : "stored_encounter",
              mode,
              definition_version_id: mode === "exploratory" && definitionVersionId ? definitionVersionId : undefined,
              parameters: mode === "exploratory" && !definitionVersionId ? { ...parameters } : undefined,
              filters: Object.keys(activeFilters).length ? activeFilters : undefined,
              timezone: "Africa/Kampala",
              idempotency_key: crypto.randomUUID(),
            });
          }}>
          {busy ? "Starting analysis…" : dirty ? "Apply definition" : "Definition applied"}
        </button>
        <p className="recurrence-definition__note">
          {programme?.active ? programme.detail : "No approved programme definition is active; exploratory analysis remains clearly labelled."}
        </p>
      </div>
    </section>
  );
}
