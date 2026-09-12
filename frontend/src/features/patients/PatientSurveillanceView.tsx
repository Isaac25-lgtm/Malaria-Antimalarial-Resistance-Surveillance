import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api, type Schemas } from "../../api/client";
import { ForbiddenState, UnavailableState } from "../../design-system/States";
import { useAuth } from "../../auth/context";
import "./patient-surveillance.css";
import { useLiveDashboard } from "../operations/useLiveDashboard";
import { useReportingPeriod } from "../operations/useReportingPeriod";
import { LiveSnapshotStatus } from "../operations/LiveSnapshotStatus";
import { PeriodControl } from "../../design-system/Surveillance";
import {
  days,
  definitionSummary,
  determinationLabel,
  gapsLabel,
  isRepeatPositive,
  resultLabel,
  scopeIdentity,
  treatmentContextNote,
} from "../recurrence/recurrenceLabels";
import { RecurrenceWorkspace } from "../recurrence/RecurrenceWorkspace";

const STORED_PAGE_SIZE = 25;

export function PatientSurveillanceView() {
  const { patientReferenceId } = useParams();
  if (patientReferenceId?.startsWith("MARS-PT-")) return <LivePatientTimeline alias={patientReferenceId} />;
  return patientReferenceId ? (
    <PatientTimelineView patientReferenceId={patientReferenceId} />
  ) : (
    <PatientListView />
  );
}

function PatientListView() {
  const [repeatsOnly, setRepeatsOnly] = useState(false);
  const [page, setPage] = useState(0);
  const [storedCursor, setStoredCursor] = useState<string | null>(null);
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const [period, setPeriod] = useReportingPeriod();
  const live = useLiveDashboard(period);
  // The key carries who is asking, their scope, the period, the filter and the
  // page, so a cached page can never answer a different scope or period.
  const patients = useQuery({
    queryKey: [
      "patients",
      "of-interest",
      scopeIdentity(user),
      period.start,
      period.end,
      repeatsOnly ? "qualifies" : "all",
      storedCursor,
    ],
    queryFn: () =>
      api.patientsOfInterest({
        period_from: period.start,
        period_to: period.end,
        limit: STORED_PAGE_SIZE,
        cursor: storedCursor ?? undefined,
        determination: repeatsOnly ? "qualifies" : undefined,
      }),
    enabled: !liveMode,
    retry: false,
  });
  const restart = () => {
    setStoredCursor(null);
    setPage(0);
  };
  if (patients.error instanceof ApiError && patients.error.isForbidden) {
    return <ForbiddenState requirement="case:view_pseudonymous_evidence" />;
  }
  if (liveMode && live.error) return <UnavailableState title="Live patient evidence could not be loaded" description={live.error.message} />;
  const positivePatients = live.data?.positive_patients ?? [];
  const displayedPatients = repeatsOnly ? positivePatients.filter(isRepeatPositive) : positivePatients;
  const scope = live.data?.scope ?? "Live source";
  const stored = patients.data;
  return (
    <div className="page patient-surveillance">
      <header className="page__header">
        <div>
          <p className="eyebrow">{liveMode ? `${scope} live pilot` : "Stored encounter evidence"}</p>
          <h1>Patient Surveillance</h1>
          <p className="page__lede">
            Longitudinal malaria evidence under stable MARS patient aliases. Direct identity is
            excluded from this list.
          </p>
        </div>
        <span className="chip">Pseudonymous</span>
        <PeriodControl
          period={period}
          onChange={(next) => {
            setPeriod(next);
            restart();
          }}
        />
      </header>
      <LiveSnapshotStatus live={live} />
      <RecurrenceWorkspace period={period} liveMode={liveMode} />
      {liveMode && live.data ? (
        <p className="patient-surveillance__definition" data-testid="applied-definition">
          <strong>Repeat-positive definition.</strong> {definitionSummary(live.data.repeat_positive_definition)}{" "}
          {live.data.repeat_positive_coverage === "partial"
            ? "Laboratory coverage is partial, so an absence of repeat positives here is not a reliable zero."
            : live.data.repeat_positive_coverage === "complete"
              ? "The recurrence calculation covers the lookback the definition needs."
              : "Coverage was not reported by this snapshot."}{" "}
          {treatmentContextNote(live.data.treatment_context_coverage)}
        </p>
      ) : null}
      {!liveMode && stored ? (
        <p className="patient-surveillance__definition" data-testid="applied-definition">
          <strong>Repeat-positive definition.</strong> {definitionSummary(stored.definition)}{" "}
          {stored.coverage_status === "partial"
            ? "Stored import coverage is partial for this span, so a patient without a qualifying chain is shown as undetermined rather than as a no."
            : "Completed imports cover every facility in scope for this span."}
        </p>
      ) : null}
      <section className="panel" aria-labelledby="patients-heading">
        <div className="panel__header">
          <h2 id="patients-heading">Patients with positive malaria evidence</h2>
        </div>
        <div className="panel__body">
          <label>
            <input
              type="checkbox"
              checked={repeatsOnly}
              onChange={(event) => {
                setRepeatsOnly(event.target.checked);
                restart();
              }}
            />{" "}
            Meets the repeat-positive definition only
          </label>
          {liveMode && live.data ? <p>Reporting window: {live.data.period_start} to {live.data.period_end}. {live.data.tracker_failed_facility_count ? "Some facilities could not be retrieved." : ""}</p> : null}
          {liveMode && live.isPending ? (
            <p>Loading the current live snapshot…</p>
          ) : liveMode && live.data && displayedPatients.length > 0 ? (
            <><LivePatientTable patients={displayedPatients.slice(page * 25, (page + 1) * 25)} /><nav aria-label="Patient pages"><button className="button" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button><span> Page {page + 1} </span><button className="button" disabled={(page + 1) * 25 >= displayedPatients.length} onClick={() => setPage(page + 1)}>Next</button></nav></>
          ) : liveMode && live.data ? (
            <div className="patient-surveillance__empty">
              <strong>{live.data.status !== "synchronized" ? "Patient evidence may be incomplete for this reporting window." : repeatsOnly ? "No patient meets the repeat-positive definition in the retrieved evidence." : "No patient with a mapped positive result was identified in the retrieved evidence."}</strong>
              <span>
                MARS read {live.data.tracker_event_count.toLocaleString()} Tracker events, mapped{" "}
                {live.data.malaria_lab_event_count.toLocaleString()} malaria-test events and{" "}
                {live.data.positive_malaria_event_count.toLocaleString()} positive malaria events.
              </span>
              <span>This is real source evidence for {scope}; no synthetic patient was inserted.</span>
            </div>
          ) : patients.isPending ? (
            <p>Loading authorised evidence…</p>
          ) : patients.error instanceof ApiError && patients.error.status === 409 ? (
            <div className="patient-surveillance__empty" role="status">
              <strong>The evidence changed while you were paging.</strong>
              <button className="button" onClick={restart}>Start again from the first page</button>
            </div>
          ) : patients.error instanceof ApiError && patients.error.isUnavailable ? (
            <UnavailableState title="Patient aliases are not configured" description={patients.error.message} />
          ) : !stored || stored.total === 0 ? (
            <div className="patient-surveillance__empty">
              <strong>No stored patient has a positive malaria result in {period.start} to {period.end}.</strong>
              <span>This is not a zero-patient claim for the district: it reflects only the stored, imported evidence in your scope.</span>
            </div>
          ) : (
            <>
              <p data-testid="stored-total">
                {stored.total.toLocaleString()} patient{stored.total === 1 ? "" : "s"} in {stored.period_start} to {stored.period_end}; every patient in scope was evaluated before this page was taken.
              </p>
              <PatientTable patients={stored.items} />
              <nav aria-label="Patient pages">
                <button className="button" disabled={!stored.previous_cursor} onClick={() => setStoredCursor(stored.previous_cursor ?? null)}>Previous</button>
                <button className="button" disabled={!stored.next_cursor} onClick={() => setStoredCursor(stored.next_cursor ?? null)}>Next</button>
              </nav>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

function LivePatientTable({
  patients,
}: {
  patients: Schemas["LiveRepeatPositivePatient"][];
}) {
  return (
    <div className="table-scroll">
      <table className="table patient-table">
        <thead>
          <tr>
            <th>MARS patient ID</th>
            <th>First positive</th>
            <th>Latest positive</th>
            <th scope="col">Qualifying interval</th>
            <th scope="col">Positive-to-positive gaps</th>
            <th scope="col">Positive encounters</th>
            <th scope="col">Definition</th>
            <th>Latest facility</th>
            <th>Cross-facility</th>
          </tr>
        </thead>
        <tbody>
          {patients.map((patient) => (
            <tr key={patient.mars_patient_id}>
              <th className="mono"><Link to={`/patients/${patient.mars_patient_id}`}>{patient.mars_patient_id}</Link></th>
              <td>{patient.first_positive_on}</td>
              <td>{patient.latest_positive_on}</td>
              <td>{days(patient.chain_interval_days)}</td>
              <td>{gapsLabel(patient.adjacent_interval_days)}</td>
              <td>{patient.positive_encounter_count}</td>
              <td>{determinationLabel(patient.determination)}</td>
              <td>{patient.facility_name}</td>
              <td>{patient.cross_facility ? "Yes" : "No"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LivePatientTimeline({ alias }: { alias: string }) {
  const [period] = useReportingPeriod();
  const range = { period_start: period.start, period_end: period.end };
  const query = useQuery({ queryKey: ["live", "patient", alias, range], queryFn: () => api.livePatientEvidence(alias, range), retry: false });
  if (query.error) return <UnavailableState title="Patient evidence unavailable" description={query.error.message} />;
  if (query.isPending) return <div className="page patient-surveillance"><Link to="/patients">Back to patients</Link><h1>{alias}</h1><p>Loading evidence…</p></div>;
  const patient = query.data;
  return (
    <div className="page patient-surveillance">
      <Link to="/patients">Back to patients</Link>
      <h1>{alias}</h1>
      <section className="panel" aria-labelledby="inclusion-heading">
        <div className="panel__header"><h2 id="inclusion-heading">Why this patient is listed</h2></div>
        <div className="panel__body">
          <dl className="patient-surveillance__facts">
            <dt>Definition</dt><dd>{determinationLabel(patient.determination)}</dd>
            <dt>Qualifying interval</dt><dd>{days(patient.chain_interval_days)}</dd>
            <dt>Positive-to-positive gaps</dt><dd>{gapsLabel(patient.adjacent_interval_days)}</dd>
            <dt>From first positive</dt><dd>{gapsLabel(patient.anchor_interval_days)}</dd>
            <dt>Eligible positive encounters</dt><dd>{patient.positive_encounter_count}</dd>
            <dt>Quality</dt><dd>{(patient.quality_flags ?? []).join(", ") || "Not reported"}</dd>
          </dl>
          {(patient.explanation ?? []).length > 0 ? (
            <ul className="patient-surveillance__explanation">
              {(patient.explanation ?? []).map((line) => <li key={line}>{line}</li>)}
            </ul>
          ) : null}
        </div>
      </section>
      <p>Recorded malaria tests in the retrieved evidence, dated by each test's own laboratory record, reaching back before the reporting period by the definition's lookback.</p>
      <ol className="patient-timeline">
        {(patient.tests ?? []).map((test, index) => (
          <li className="panel patient-timeline__event" key={`${test.occurred_on}:${index}`}>
            <strong>{test.occurred_on}</strong>
            <div>{test.facility_name}<p>Malaria result: {resultLabel(test.result)}</p></div>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function PatientTable({ patients }: { patients: Schemas["PatientOfInterestSummary"][] }) {
  return (
    <div className="table-scroll">
      <table className="table patient-table">
        <thead>
          <tr>
            <th scope="col">MARS patient ID</th>
            <th scope="col">Age / sex</th>
            <th scope="col">First positive</th>
            <th scope="col">Latest positive</th>
            <th scope="col">Qualifying interval</th>
            <th scope="col">Definition</th>
            <th scope="col">Facility</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {patients.map((patient) => (
            <tr key={patient.patient_reference_id}>
              <th scope="row" className="mono">{patient.mars_patient_id}</th>
              <td>{formatAgeSex(patient)}</td>
              <td>{patient.first_positive_on}</td>
              <td>{patient.latest_positive_on}</td>
              <td>{days(patient.chain_interval_days)}</td>
              <td>{determinationLabel(patient.determination)}</td>
              <td>{patient.facility_name}</td>
              <td>
                <Link to={`/patients/${patient.patient_reference_id}`}>Open timeline</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PatientTimelineView({ patientReferenceId }: { patientReferenceId: string }) {
  const { user } = useAuth();
  const timeline = useQuery({
    queryKey: ["patients", scopeIdentity(user), patientReferenceId],
    queryFn: () => api.patientTimeline(patientReferenceId),
    retry: false,
  });
  if (timeline.error instanceof ApiError && timeline.error.isForbidden) {
    return <ForbiddenState requirement="case:view_pseudonymous_evidence" />;
  }
  if (timeline.error instanceof ApiError) {
    return <UnavailableState title="Patient timeline unavailable" description={timeline.error.message} />;
  }
  const patient = timeline.data;
  return (
    <div className="page patient-surveillance">
      <header className="page__header">
        <div>
          <Link to="/patients">← Patient surveillance</Link>
          <h1>{patient?.mars_patient_id ?? "Patient timeline"}</h1>
          <p className="page__lede">Authorised pseudonymous encounter evidence.</p>
        </div>
        <span className="chip">Identity protected</span>
      </header>
      {patient ? (
        <>
          <p className="patient-surveillance__identity-note">{patient.identity_detail}</p>
          <ol className="patient-timeline">
            {patient.encounters.map((encounter) => (
              <li key={encounter.encounter_id} className="panel patient-timeline__event">
                <div className="patient-timeline__date">{encounter.encounter_date}</div>
                <div>
                  <strong>{encounter.facility_name}</strong>
                  <p>{encounter.tests.map((test) => `${test.method}: ${resultLabel(test.result ?? "not_recorded")}`).join(" · ") || "No mapped malaria test"}</p>
                  {encounter.treatments.length > 0 ? <p>Treatment: {encounter.treatments.join("; ")}</p> : null}
                  {encounter.diagnoses.length > 0 ? <p>Diagnosis: {encounter.diagnoses.join("; ")}</p> : null}
                </div>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p>Loading authorised evidence…</p>
      )}
    </div>
  );
}

function formatAgeSex(patient: Schemas["PatientOfInterestSummary"]): string {
  const age = patient.age_value == null ? "Age not recorded" : `${patient.age_value} ${patient.age_unit ?? ""}`.trim();
  return `${age} / ${patient.sex}`;
}
