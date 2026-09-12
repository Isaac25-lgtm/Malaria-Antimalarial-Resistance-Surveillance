import type { ProjectionView } from "./types";
import { number, rows, text } from "./types";

export function TreatmentResponseMatrix({ projection }: { projection: ProjectionView | undefined }) {
  const data = rows(projection?.rows);
  return (
    <section className="panel" aria-labelledby="treatment-matrix-heading">
      <div className="panel__header"><h3 id="treatment-matrix-heading">Treatment preceding a later positive</h3></div>
      <div className="panel__body table-scroll">
        {data.length === 0 ? <p>No linked treatment transitions were recorded.</p> : (
          <table className="table">
            <thead><tr><th>Recorded treatment</th><th>Interval band</th><th>Later-positive transitions</th></tr></thead>
            <tbody>{data.map((row, index) => (
              <tr key={`${text(row.prior_treatment)}:${text(row.interval_band)}:${index}`}>
                <th>{text(row.prior_treatment, "Treatment not available")}</th>
                <td>{text(row.interval_band)}</td>
                <td>{number(row.transitions).toLocaleString()}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
        <p className="recurrence-caption">This describes repeat-positive patterns after recorded care. It is not a treatment-failure or resistance rate.</p>
      </div>
    </section>
  );
}
