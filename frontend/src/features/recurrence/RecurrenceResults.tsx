import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas } from "../../api/client";
import { EmptyState, LoadingState, UnavailableState } from "../../design-system/States";
import { DefinitionComparison } from "./DefinitionComparison";
import { DuplicateReviewTable } from "./DuplicateReviewTable";
import { RecurrenceCharts } from "./RecurrenceCharts";
import { TreatmentResponseMatrix } from "./TreatmentResponseMatrix";
import { number, object, text, type ProjectionView } from "./types";

const PROJECTIONS = ["summary", "intervals", "facilities", "weekly", "frequency", "treatments", "geography"] as const;

interface PatientFilters {
  sex: string;
  ageGroup: string;
  quality: string;
  facility: string;
  testMethod: string;
  treatment: string;
  investigationStatus: string;
}

const EMPTY_PATIENT_FILTERS: PatientFilters = {
  sex: "", ageGroup: "", quality: "", facility: "", testMethod: "", treatment: "",
  investigationStatus: "",
};

export function RecurrenceResults({ run }: { run: Schemas["RunView"] }) {
  const [patientCursor, setPatientCursor] = useState<string | null>(null);
  const [duplicateCursor, setDuplicateCursor] = useState<string | null>(null);
  const [patientFilters, setPatientFilters] = useState(EMPTY_PATIENT_FILTERS);
  const projections = useQuery({
    queryKey: ["recurrence", run.id, "projections"],
    queryFn: async () => Object.fromEntries(await Promise.all(PROJECTIONS.map(async (name) => [name, await api.recurrenceProjection(run.id, name)]))) as Record<string, ProjectionView>,
    enabled: run.status === "completed",
    retry: false,
  });
  const patientQuery = {
    limit: 25,
    cursor: patientCursor ?? undefined,
    determination: ["qualifies"],
    sex: patientFilters.sex ? [patientFilters.sex] : undefined,
    age_group: patientFilters.ageGroup ? [patientFilters.ageGroup] : undefined,
    quality: patientFilters.quality ? [patientFilters.quality] : undefined,
    facility: patientFilters.facility ? [patientFilters.facility] : undefined,
    test_method: patientFilters.testMethod ? [patientFilters.testMethod] : undefined,
    treatment: patientFilters.treatment ? [patientFilters.treatment.toLocaleLowerCase()] : undefined,
    investigation_status: patientFilters.investigationStatus ? [patientFilters.investigationStatus] : undefined,
  };
  const patients = useQuery({ queryKey: ["recurrence", run.id, "patients", patientCursor, patientFilters], queryFn: () => api.recurrencePatients(run.id, patientQuery), enabled: run.status === "completed", retry: false });
  const duplicates = useQuery({ queryKey: ["recurrence", run.id, "duplicates", duplicateCursor], queryFn: () => api.recurrenceDuplicates(run.id, duplicateCursor ?? undefined), enabled: run.status === "completed", retry: false });
  const runs = useQuery({ queryKey: ["recurrence", "runs"], queryFn: () => api.recurrenceRuns(), enabled: run.status === "completed", retry: false });

  if (run.status === "queued" || run.status === "running") return <LoadingState label={`recurrence analysis ${run.progress_completed} of ${run.progress_total}`} rows={5} />;
  if (run.status === "failed") return <UnavailableState title="Recurrence analysis failed" description={`The frozen run could not finish (${run.error_code ?? "unknown failure"}).`} />;
  if (projections.isPending || patients.isPending) return <LoadingState label="recurrence results" rows={6} />;
  if (projections.error || patients.error) return <UnavailableState title="Recurrence results unavailable" description={(projections.error ?? patients.error)?.message ?? "The results could not be read."} />;

  const summary = object(projections.data?.summary?.rows);
  const qualifying = number(summary.qualifying_patients);
  const denominator = number(summary.denominator_patients);
  const facilityOptions = Array.isArray(projections.data?.facilities?.rows)
    ? projections.data.facilities.rows as Record<string, unknown>[] : [];
  const updatePatientFilter = (name: keyof PatientFilters, value: string) => {
    setPatientCursor(null);
    setPatientFilters((current) => ({ ...current, [name]: value }));
  };
  return (
    <div className="recurrence-results" aria-live="polite">
      <header className="recurrence-results__provenance">
        <div><strong>{run.definition_name}</strong><span>Engine {run.engine_version} · {run.period_start} to {run.period_end}</span></div>
        <span className={`chip ${run.exploratory ? "chip--attention" : ""}`}>{run.exploratory ? "Exploratory result" : "Programme result"}</span>
      </header>
      <div className="recurrence-kpis">
        <article><span>Qualifying patients</span><strong>{qualifying.toLocaleString()}</strong></article>
        <article><span>Evaluated patients</span><strong>{denominator.toLocaleString()}</strong></article>
        <article><span>Repeat-positive proportion</span><strong>{denominator ? `${(qualifying / denominator * 100).toFixed(1)}%` : "—"}</strong></article>
        <article><span>Evidence coverage</span><strong>{text(object(run.coverage).status, "Not reported")}</strong></article>
      </div>
      <RecurrenceCharts projections={projections.data ?? {}} />
      <TreatmentResponseMatrix projection={projections.data?.treatments} />
      <section className="panel" aria-labelledby="qualifying-patients-heading">
        <div className="panel__header"><h3 id="qualifying-patients-heading">Qualifying repeat-positive patients</h3><span className="chip">{patients.data?.total ?? 0}</span></div>
        <div className="panel__body">
          <div className="recurrence-patient-filters" aria-label="Patient result filters">
            <label>Sex<select value={patientFilters.sex} onChange={(event) => updatePatientFilter("sex", event.target.value)}>
              <option value="">All</option><option value="female">Female</option><option value="male">Male</option><option value="unknown">Unknown</option>
            </select></label>
            <label>Age group<select value={patientFilters.ageGroup} onChange={(event) => updatePatientFilter("ageGroup", event.target.value)}>
              <option value="">All</option><option value="under_5">Under 5</option><option value="5_to_14">5 to 14</option><option value="15_and_over">15 and over</option><option value="unknown">Unknown</option>
            </select></label>
            <label>Data quality<select value={patientFilters.quality} onChange={(event) => updatePatientFilter("quality", event.target.value)}>
              <option value="">All</option>{["VALID", "POSSIBLE_DUPLICATE", "INCOMPLETE_TEST_EVIDENCE", "INCOMPLETE_TREATMENT_EVIDENCE", "IDENTITY_LINKAGE_UNCERTAIN", "DATE_QUALITY_ISSUE"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
            </select></label>
            <label>Facility<select value={patientFilters.facility} onChange={(event) => updatePatientFilter("facility", event.target.value)}>
              <option value="">All authorised facilities</option>{facilityOptions.map((facility) => <option key={text(facility.facility_ref)} value={text(facility.facility_ref)}>{text(facility.facility_name)}</option>)}
            </select></label>
            <label>Test method<input value={patientFilters.testMethod} onChange={(event) => updatePatientFilter("testMethod", event.target.value)} placeholder="Any mapped test" /></label>
            <label>Treatment<input value={patientFilters.treatment} onChange={(event) => updatePatientFilter("treatment", event.target.value)} placeholder="Any recorded regimen" /></label>
            <label>Investigation<select value={patientFilters.investigationStatus} onChange={(event) => updatePatientFilter("investigationStatus", event.target.value)}>
              <option value="">All</option><option value="none">Not opened</option><option value="new">New</option><option value="triaged">Triaged</option><option value="assigned">Assigned</option><option value="under_investigation">Under investigation</option><option value="closed">Closed</option><option value="escalated">Escalated</option>
            </select></label>
            <button className="button" disabled={Object.values(patientFilters).every((value) => !value)}
              onClick={() => { setPatientFilters(EMPTY_PATIENT_FILTERS); setPatientCursor(null); }}>Clear filters</button>
          </div>
          <div className="table-scroll">
          {!patients.data?.items.length ? <EmptyState title="No qualifying patients" description="The run completed over the stated coverage and no patient met this definition." /> : (
            <table className="table"><thead><tr><th>Patient</th><th>Positive encounters</th><th>Interval</th><th>Latest facility</th><th>Quality</th><th>Review</th></tr></thead>
              <tbody>{patients.data.items.map((patient, index) => <tr key={text(patient.patient_alias, String(index))}><th className="mono">{text(patient.patient_alias)}</th><td>{number(patient.eligible_positive_count)}</td><td>{number(patient.chain_interval_days)} days</td><td>{text(patient.final_facility_name)}</td><td>{Array.isArray(patient.quality_flags) ? patient.quality_flags.join(", ") : "Valid"}</td><td><Link to={`/recurrence/${run.id}/patients/${encodeURIComponent(text(patient.patient_alias))}`}>Open care timeline</Link></td></tr>)}</tbody>
            </table>
          )}
          </div>
          <div className="recurrence-pagination">
            <button className="button" disabled={!patients.data.previous_cursor}
              onClick={() => setPatientCursor(patients.data?.previous_cursor ?? null)}>Previous</button>
            <span>{patients.data.total} qualifying patients</span>
            <button className="button" disabled={!patients.data.next_cursor}
              onClick={() => setPatientCursor(patients.data?.next_cursor ?? null)}>Next</button>
          </div>
        </div>
      </section>
      <DuplicateReviewTable page={duplicates.data} onPage={setDuplicateCursor} />
      <DefinitionComparison current={run} runs={runs.data ?? []} />
      <aside className="boundary"><p className="boundary__text">{run.interpretation}</p></aside>
    </div>
  );
}
