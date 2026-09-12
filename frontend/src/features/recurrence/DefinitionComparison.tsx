import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Schemas } from "../../api/client";
import { number, object, rows, text } from "./types";

function DifferenceTable({ title, data }: { title: string; data: Record<string, unknown>[] }) {
  if (!data.length) return null;
  return <div className="recurrence-comparison__table">
    <h4>{title}</h4>
    <div className="table-scroll"><table className="table">
      <thead><tr><th>Group</th><th>Current</th><th>Comparison</th><th>Difference</th></tr></thead>
      <tbody>{data.map((row, index) => <tr key={`${text(row.key)}:${index}`}>
        <th>{text(row.key)}</th><td>{number(row.a)}</td><td>{number(row.b)}</td>
        <td>{number(row.delta) > 0 ? "+" : ""}{number(row.delta)}</td>
      </tr>)}</tbody>
    </table></div>
  </div>;
}

export function DefinitionComparison({ current, runs }: { current: Schemas["RunView"]; runs: Schemas["RunView"][] }) {
  const candidates = runs.filter((run) => run.id !== current.id && run.status === "completed");
  const [other, setOther] = useState("");
  const comparison = useMutation({ mutationFn: () => api.compareRecurrenceRuns(current.id, other) });
  const result = comparison.data;
  const overlap = object(result?.overlap);
  const differences = object(result?.differences);
  return (
    <section className="panel" aria-labelledby="comparison-heading">
      <div className="panel__header"><h3 id="comparison-heading">Compare definitions on the same evidence</h3></div>
      <div className="panel__body">
        <div className="recurrence-comparison__controls">
          <label>Comparison run<select value={other} onChange={(event) => setOther(event.target.value)}>
            <option value="">Select a completed run</option>
            {candidates.map((run) => <option key={run.id} value={run.id}>{run.definition_name} / {run.period_start}</option>)}
          </select></label>
          <button className="button" disabled={!other || comparison.isPending}
            onClick={() => comparison.mutate()}>Compare</button>
        </div>
        {comparison.error ? <p role="alert">{comparison.error.message}</p> : null}
        {result ? <>
          <div className="recurrence-facts">
            <div><span>Current only</span><strong>{Array.isArray(overlap.a_only) ? overlap.a_only.length : number(overlap.a_only)}</strong></div>
            <div><span>Both definitions</span><strong>{Array.isArray(overlap.both) ? overlap.both.length : number(overlap.both)}</strong></div>
            <div><span>Comparison only</span><strong>{Array.isArray(overlap.b_only) ? overlap.b_only.length : number(overlap.b_only)}</strong></div>
          </div>
          <div className="recurrence-comparison__tables">
            <DifferenceTable title="Facility differences" data={rows(differences.facilities)} />
            <DifferenceTable title="Treatment differences" data={rows(differences.treatments)} />
            <DifferenceTable title="Geographic differences" data={rows(differences.geography)} />
          </div>
        </> : null}
      </div>
    </section>
  );
}
