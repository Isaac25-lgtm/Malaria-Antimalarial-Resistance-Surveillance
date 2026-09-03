/**
 * Shared surveillance furniture: period control, provenance, boundary notice.
 *
 * These three appear on every analytical screen and always together. A figure
 * without its period cannot be checked, a figure without its freshness cannot
 * be trusted, and a malaria figure without its interpretation boundary invites
 * exactly the reading MARS exists to prevent.
 */

import type { Schemas } from "../api/client";
import {
  formatMoment,
  formatPeriod,
  monthPeriod,
  type PeriodSelection,
} from "./period";
import "./surveillance.css";

type Provenance = Schemas["SurveillanceProvenance"];

interface PeriodControlProps {
  period: PeriodSelection;
  onChange: (period: PeriodSelection) => void;
  /** How many whole months back the control may reach. */
  monthsAvailable?: number;
}

/**
 * Reporting-period selector.
 *
 * Whole months only. HMIS 105 is monthly and 033b is weekly; offering an
 * arbitrary date range would invite comparisons across windows the source
 * never reported, which is how a partial month becomes a decline.
 */
export function PeriodControl({
  period,
  onChange,
  monthsAvailable = 12,
}: PeriodControlProps) {
  const options = Array.from({ length: monthsAvailable }, (_, index) => monthPeriod(-index));

  return (
    <div className="period-control">
      <label className="label" htmlFor="reporting-period">
        Reporting period
      </label>
      <select
        id="reporting-period"
        className="period-control__select"
        value={period.start}
        onChange={(event) => {
          const chosen = options.find((option) => option.start === event.target.value);
          if (chosen) onChange(chosen);
        }}
      >
        {options.map((option) => (
          <option key={option.start} value={option.start}>
            {formatPeriod(option)}
          </option>
        ))}
      </select>
    </div>
  );
}

interface ProvenanceBarProps {
  provenance: Provenance | undefined;
}

/**
 * Freshness and configuration state.
 *
 * When nothing is approved this is the most important row on the page: it is
 * the difference between "the country is quiet" and "MARS has not been told
 * what to measure".
 */
export function ProvenanceBar({ provenance }: ProvenanceBarProps) {
  if (!provenance) return null;

  return (
    <div className="provenance" role="status">
      <dl className="provenance__items">
        <div>
          <dt>Analytics refreshed</dt>
          <dd>{formatMoment(provenance.analytics_refreshed_at)}</dd>
        </div>
        <div>
          <dt>Signals generated</dt>
          <dd>{formatMoment(provenance.signals_generated_at)}</dd>
        </div>
        <div>
          <dt>Approved indicators</dt>
          <dd>
            {provenance.indicators_approved} of {provenance.indicators_registered}
          </dd>
        </div>
      </dl>

      {!provenance.analytically_configured ? (
        <p className="provenance__notice">
          <span className="chip chip--unavailable">Not configured</span>{" "}
          {provenance.configuration_detail}
        </p>
      ) : null}
    </div>
  );
}

interface BoundaryProps {
  statement: string | undefined;
}

/**
 * The permanent scientific interpretation boundary.
 *
 * Rendered on every analytical screen, from the server's own words rather than
 * a string in the bundle - the sentence is part of the governed output, not
 * decoration the frontend could quietly reword.
 */
export function InterpretationBoundary({ statement }: BoundaryProps) {
  if (!statement) return null;
  return (
    <aside className="boundary" aria-label="Interpretation boundary">
      <p className="boundary__text">{statement}</p>
    </aside>
  );
}
