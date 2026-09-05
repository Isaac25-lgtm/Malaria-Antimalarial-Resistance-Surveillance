import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api, type Schemas } from "../../api/client";
import { ForbiddenState, UnavailableState } from "../../design-system/States";
import { useAuth } from "../../auth/context";
import "./patient-surveillance.css";

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
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const live = useQuery({
    queryKey: ["live", "dashboard", "latest"],
    queryFn: () => api.latestLiveDashboard(),
    enabled: liveMode,
    retry: false,
  });
  const patients = useQuery({
    queryKey: ["patients", "of-interest"],
    queryFn: () => api.patientsOfInterest({ limit: 100 }),
    retry: false,
  });
  if (patients.error instanceof ApiError && patients.error.isForbidden) {
    return <ForbiddenState requirement="case:view_pseudonymous_evidence" />;
  }
  if (liveMode && live.error) return <UnavailableState title="Live patient evidence could not be loaded" description={live.error.message} />;
  const positivePatients = live.data?.positive_patients ?? [];
  const displayedPatients = repeatsOnly ? positivePatients.filter((row) => row.positive_encounter_count > 1) : positivePatients;
  return (
    <div className="page patient-surveillance">
      <header className="page__header">
        <div>
          <p className="eyebrow">Pader live pilot</p>
          <h1>Patient Surveillance</h1>
          <p className="page__lede">
            Longitudinal malaria evidence under stable MARS patient aliases. Direct identity is
            excluded from this list.
          </p>
        </div>
        <span className="chip">Pseudonymous</span>
      </header>
      <section className="panel" aria-labelledby="patients-heading">
        <div className="panel__header">
          <h2 id="patients-heading">Patients with positive malaria evidence</h2>
        </div>
        <div className="panel__body">
          {liveMode && live.data ? <><p>Reporting window: {live.data.period_start} to {live.data.period_end}. {live.data.tracker_failed_facility_count ? "Some facilities could not be retrieved." : ""}</p><label><input type="checkbox" checked={repeatsOnly} onChange={(event) => { setRepeatsOnly(event.target.checked); setPage(0); }} /> Repeat-positive encounters only</label></> : null}
          {liveMode && live.isPending ? (
            <p>Loading the current live snapshot…</p>
          ) : liveMode && live.data && displayedPatients.length > 0 ? (
            <><LivePatientTable patients={displayedPatients.slice(page * 25, (page + 1) * 25)} /><nav aria-label="Patient pages"><button className="button" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button><span> Page {page + 1} </span><button className="button" disabled={(page + 1) * 25 >= displayedPatients.length} onClick={() => setPage(page + 1)}>Next</button></nav></>
          ) : liveMode && live.data ? (
            <div className="patient-surveillance__empty">
              <strong>{live.data.status !== "synchronized" ? "Patient evidence may be incomplete for this reporting window." : repeatsOnly ? "No repeat-positive patient was identified in the retrieved evidence." : "No patient with a mapped positive result was identified in the retrieved evidence."}</strong>
              <span>
                MARS read {live.data.tracker_event_count.toLocaleString()} Tracker events, mapped{" "}
                {live.data.malaria_lab_event_count.toLocaleString()} malaria-test events and{" "}
                {live.data.positive_malaria_event_count.toLocaleString()} positive malaria events.
              </span>
              <span>This is real Pader source evidence; no synthetic patient was inserted.</span>
            </div>
          ) : patients.isPending ? (
            <p>Loading authorised evidence…</p>
          ) : patients.error instanceof ApiError && patients.error.isUnavailable ? (
            <UnavailableState title="Patient aliases are not configured" description={patients.error.message} />
          ) : (patients.data?.length ?? 0) === 0 ? (
            <div className="patient-surveillance__empty">
              <strong>No validated patient evidence has been synchronised.</strong>
              <span>This is not a zero-patient claim. Run the approved bounded Tracker sync first.</span>
            </div>
          ) : (
            <PatientTable patients={patients.data ?? []} />
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
            <th>Interval</th>
            <th>Positive encounters</th>
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
              <td>{patient.interval_days} days</td>
              <td>{patient.positive_encounter_count}</td>
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
  const query = useQuery({ queryKey: ["live", "patient", alias], queryFn: () => api.livePatientEvidence(alias), retry: false });
  if (query.error) return <UnavailableState title="Patient evidence unavailable" description={query.error.message} />;
  return <div className="page patient-surveillance"><Link to="/patients">Back to patients</Link><h1>{alias}</h1><p>Recorded malaria tests in the synchronized reporting window.</p>{query.isPending ? <p>Loading evidence…</p> : <ol className="patient-timeline">{(query.data.tests ?? []).map((test, index) => <li className="panel patient-timeline__event" key={`${test.occurred_on}:${index}`}><strong>{test.occurred_on}</strong><div>{test.facility_name}<p>Malaria result: {test.result}</p></div></li>)}</ol>}</div>;
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
            <th scope="col">Interval</th>
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
              <td>{patient.interval_days == null ? "—" : `${patient.interval_days} days`}</td>
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
  const timeline = useQuery({
    queryKey: ["patients", patientReferenceId],
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
                  <p>{encounter.tests.map((test) => `${test.method}: ${test.result}`).join(" · ") || "No mapped malaria test"}</p>
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
