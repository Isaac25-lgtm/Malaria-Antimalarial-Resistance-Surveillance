import type { ProjectionView } from "./types";
import { number, object, rows, text } from "./types";

function BarList({ title, data, labelKey, valueKey }: {
  title: string;
  data: Record<string, unknown>[];
  labelKey: string;
  valueKey: string;
}) {
  const maximum = Math.max(1, ...data.map((row) => number(row[valueKey])));
  return (
    <section className="panel recurrence-chart" aria-label={title}>
      <div className="panel__header"><h3>{title}</h3></div>
      <div className="panel__body">
        {data.length === 0 ? <p>No values in this run.</p> : (
          <ul className="recurrence-bars">
            {data.map((row, index) => {
              const value = number(row[valueKey]);
              return (
                <li key={`${text(row[labelKey], String(index))}:${index}`}>
                  <span>{text(row[labelKey])}</span>
                  <span className="recurrence-bars__track" aria-hidden="true">
                    <span style={{ width: `${Math.max(2, value / maximum * 100)}%` }} />
                  </span>
                  <strong>{value.toLocaleString()}</strong>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

export function RecurrenceCharts({ projections }: { projections: Record<string, ProjectionView | undefined> }) {
  const intervals = object(projections.intervals?.rows);
  const observed = rows(intervals.observed_adjacent);
  const qualifyingIntervals = rows(intervals.qualifying_chain);
  const facilities = rows(projections.facilities?.rows);
  const facilityRates = facilities.map((row) => ({
    ...row,
    rate_percent: number(row.proportion) * 100,
  }));
  const weekly = rows(projections.weekly?.rows);
  const frequency = rows(projections.frequency?.rows);
  const geography = rows(projections.geography?.rows);
  return (
    <div className="recurrence-grid recurrence-grid--charts">
      <BarList title="Observed positive-to-positive intervals" data={observed}
        labelKey="label" valueKey="count" />
      <BarList title="Qualifying-chain intervals" data={qualifyingIntervals}
        labelKey="label" valueKey="count" />
      <BarList title="Repeat-positive patients by facility" data={facilities}
        labelKey="facility_name" valueKey="qualifying_patients" />
      <BarList title="Repeat-positive rate by facility (%)" data={facilityRates}
        labelKey="facility_name" valueKey="rate_percent" />
      <BarList title="Repeat-positive trend by week" data={weekly}
        labelKey="week_start" valueKey="qualifying_patients" />
      <BarList title="Number of positive encounters" data={frequency}
        labelKey="label" valueKey="patients" />
      <BarList title="Repeat-positive patients by geography" data={geography}
        labelKey="geography_ref" valueKey="qualifying_patients" />
    </div>
  );
}
