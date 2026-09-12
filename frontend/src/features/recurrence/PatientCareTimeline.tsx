import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { LoadingState, UnavailableState } from "../../design-system/States";
import { number, object, rows, strings, text } from "./types";

function medicationSummary(item: Record<string, unknown>): string {
  const detail = [
    text(item.formulation, ""),
    item.dose ? `${number(item.dose)} ${text(item.dose_unit, "")}`.trim() : "",
    item.frequency ? text(item.frequency, "") : "",
    item.duration_days ? `${number(item.duration_days)} days` : "",
    item.quantity ? `quantity ${number(item.quantity)}` : "",
  ].filter(Boolean).join(", ");
  return `${text(item.name)} (${text(item.evidence)})${detail ? ` - ${detail}` : ""}`;
}

function feverLabel(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value !== "string" || !value.trim()) return "Not recorded";
  const normalized = value.trim().toLowerCase();
  if (normalized === "yes") return "Yes";
  if (normalized === "no") return "No";
  return value.trim();
}

function CarePath({ entries }: { entries: Record<string, unknown>[] }) {
  if (!entries.length) return <p>No encounter detail was retained.</p>;
  return (
    <section aria-labelledby="care-pathway-heading">
      <h2 id="care-pathway-heading">Care pathway</h2>
      <ol className="recurrence-timeline">{entries.map((entry, index) => {
        const tests = rows(entry.tests);
        const medicines = rows(entry.medications);
        const decision = object(entry.decision);
        return <li className="panel" key={text(entry.ref, String(index))}>
          <div className="recurrence-timeline__date">
            <strong>{text(entry.encounter_date)}</strong>
            <span>{text(entry.facility_name, "Authorised facility")}</span>
            {entry.facility_changed ? <span className="chip chip--attention">Facility changed</span> : null}
          </div>
          <div>
            <h3>{tests.some((test) => test.result === "positive")
              ? "Confirmed positive malaria encounter" : "Context encounter"}</h3>
            <p>{tests.map((test) => `${text(test.method)}: ${text(test.result)}`).join(" / ")
              || "No mapped malaria test"}</p>
            <p>Diagnoses: {strings(entry.diagnoses).join("; ") || "Not recorded"}</p>
            <p>Clinical observations: {strings(entry.observations).join("; ") || "Not recorded"}</p>
            <p>Fever: {feverLabel(entry.fever)}; attendance: {text(entry.attendance)}</p>
            <p>Treatment: {medicines.map(medicationSummary).join("; ")
              || "Not available from mapped evidence"}</p>
            <p>Referrals: {strings(entry.referrals).join("; ") || "Not recorded"}</p>
            <p>Outcome: {text(entry.outcome)}</p>
            {Object.keys(decision).length ? <p><strong>Algorithm decision:</strong> {decision.counted ? "counted" : "not counted"} - {text(decision.reason)}</p> : null}
          </div>
        </li>;
      })}</ol>
    </section>
  );
}

function IntervalList({ transitions }: { transitions: Record<string, unknown>[] }) {
  if (!transitions.length) return null;
  return <section className="panel">
    <div className="panel__header"><h2>Positive-to-positive intervals</h2></div>
    <div className="panel__body"><ul>{transitions.map((item, index) => (
      <li key={`${text(item.from_ref, String(index))}-${text(item.to_ref, String(index))}`}>
        {text(item.from_ref)} to {text(item.to_ref)}: {number(item.days)} days; prior treatment: {strings(item.prior_medications).join(", ") || text(item.prior_treatment_evidence)}
      </li>
    ))}</ul></div>
  </section>;
}

export function PatientCareTimeline({ runId, alias }: { runId: string; alias: string }) {
  const detail = useQuery({
    queryKey: ["recurrence", runId, "patient", alias],
    queryFn: () => api.recurrencePatient(runId, alias),
    retry: false,
  });
  const investigation = useMutation({
    mutationFn: () => api.openRecurrenceInvestigation(runId, alias, crypto.randomUUID()),
  });

  if (detail.isPending) return <LoadingState label="patient care timeline" rows={4} />;
  if (detail.error) {
    return <UnavailableState title="Patient timeline unavailable" description={detail.error.message}
      requestId={detail.error instanceof ApiError ? detail.error.requestId : null} />;
  }

  const finding = object(detail.data.finding);
  const timeline = object(detail.data.timeline);
  const entries = rows(timeline.entries);
  const transitions = rows(timeline.transitions);

  return (
    <div className="page recurrence-workspace">
      <header className="page__header">
        <div>
          <Link to={`/patients?run=${encodeURIComponent(runId)}`}>&larr; Recurrence results</Link>
          <h1>{alias}</h1>
          <p className="page__lede">Complete pseudonymous malaria care pathway for this frozen analysis run.</p>
        </div>
        <button className="button button--primary"
          disabled={investigation.isPending || investigation.isSuccess}
          onClick={() => investigation.mutate()}>
          {investigation.isSuccess ? "Investigation opened" : investigation.isPending ? "Opening..." : "Open investigation"}
        </button>
      </header>
      {investigation.error ? <p role="alert">{investigation.error.message}</p> : null}

      <section className="panel recurrence-explanation">
        <div className="panel__header"><h2>Why this patient was included</h2></div>
        <div className="panel__body recurrence-facts">
          <div><span>Determination</span><strong>{text(finding.determination)}</strong></div>
          <div><span>Eligible positives</span><strong>{number(finding.eligible_positive_count)}</strong></div>
          <div><span>First to latest</span><strong>{number(finding.chain_interval_days)} days</strong></div>
          <div><span>Facilities</span><strong>{number(finding.facility_count)}</strong></div>
          <div><span>Quality</span><strong>{strings(finding.quality_flags).join(", ") || "Valid"}</strong></div>
          <div><span>Method</span><strong>{detail.data.definition_name} / {detail.data.engine_version}</strong></div>
        </div>
        {strings(timeline.explanation).length ? <div className="panel__body"><ul>
          {strings(timeline.explanation).map((reason) => <li key={reason}>{reason}</li>)}
        </ul></div> : null}
      </section>

      <CarePath entries={entries} />
      <IntervalList transitions={transitions} />
      <aside className="boundary"><p className="boundary__text">{detail.data.interpretation}</p></aside>
    </div>
  );
}
