import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { ApiError, api, type Schemas } from "../../api/client";
import { ForbiddenState, UnavailableState } from "../../design-system/States";
import { useAuth } from "../../auth/context";
import "./patient-surveillance.css";

export function PatientSurveillanceView() {
  const { patientReferenceId } = useParams();
  return patientReferenceId ? (
    <PatientTimelineView patientReferenceId={patientReferenceId} />
  ) : (
    <PatientListView />
  );
}

function PatientListView() {
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const live = useQuery({
    queryKey: ["live", "dashboard"],
    queryFn: api.latestLiveDashboard,
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
          <h2 id="patients-heading">Recent patients of interest</h2>
        </div>
        <div className="panel__body">
          {liveMode && live.isPending ? (
            <p>Loading the current live snapshot…</p>
          ) : liveMode && live.data && live.data.repeat_positive_patients.length > 0 ? (
            <LivePatientTable patients={live.data.repeat_positive_patients} />
          ) : liveMode && live.data ? (
            <div className="patient-surveillance__empty">
              <strong>No repeat-positive patient was identified in this reporting window.</strong>
              <span>
                MARS read {live.data.tracker_event_count.toLocaleString()} Tracker events, mapped{" "}
                {live.data.malaria_lab_event_count.toLocaleString()} malaria-test events and{" "}
                {(live.data.positive_malaria_event_count ?? 0).toLocaleString()} positive malaria events.
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
              <th className="mono">{patient.mars_patient_id}</th>
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
