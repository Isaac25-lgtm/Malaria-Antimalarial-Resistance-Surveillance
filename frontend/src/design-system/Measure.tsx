/**
 * Rendering a governed measure.
 *
 * The single rule this file exists to enforce: **a measure with no value never
 * renders as a number.** The API distinguishes available, unavailable and
 * not-configured, and the interface has to keep those apart all the way to the
 * pixel - a blank or a zero on a malaria screen is read as "no malaria here",
 * and only one of these states means anything like that.
 *
 * Nothing here computes. The value, the numerator, the denominator, the
 * comparison and the period all arrive from the server; this formats them.
 */

import type { ReactNode } from "react";

import type { Schemas } from "../api/client";
import { formatMeasureValue } from "./period";
import "./measure.css";

type Measure = Schemas["SurveillanceMeasure"];

/** The denominator a proportion was taken over, when the server supplied one. */
function denominatorNote(measure: Measure): string | null {
  if (measure.numerator === null || measure.denominator === null) return null;
  return `${measure.numerator.toLocaleString("en-GB")} of ${measure.denominator.toLocaleString(
    "en-GB",
  )}`;
}

interface DirectionProps {
  comparison: Measure["comparison"];
  compact?: boolean;
}

/**
 * Direction of travel against the preceding window.
 *
 * The comparison period is always named. An arrow with no stated window is a
 * claim nobody can check, and a rise against an unnamed baseline is the kind
 * of figure that ends up in a briefing without its caveat.
 */
function Direction({ comparison, compact = false }: DirectionProps) {
  if (!comparison || comparison.status !== "available" || !comparison.direction) {
    return null;
  }
  const symbol =
    comparison.direction === "up" ? "▲" : comparison.direction === "down" ? "▼" : "▪";
  return (
    <p className="measure__comparison">
      <span aria-hidden="true">{symbol}</span>{" "}
      <span>
        {compact
          ? `${comparison.value ?? comparison.direction} vs previous period`
          : `${comparison.direction} from ${comparison.value ?? "—"} in the preceding period (${comparison.period.start} to ${comparison.period.end})`}
      </span>
    </p>
  );
}

interface MeasureCardProps {
  measure: Measure;
  /** Rendered under the value when the measure has one. */
  children?: ReactNode;
  /** Executive strip: short state, no implementation strings. */
  compact?: boolean;
}

/**
 * One KPI card.
 *
 * Neutral by default. Blueprint 047: only status marks carry alert colour, so
 * a card stays grey-on-white however bad its number is. That restraint is what
 * keeps a genuinely urgent chip legible.
 */
export function MeasureCard({ measure, children, compact = false }: MeasureCardProps) {
  const unavailable = measure.status !== "available";
  const note = denominatorNote(measure);
  const statusWord = compact
    ? compactStatus(measure.status)
    : measure.status === "not_configured"
      ? "Not configured"
      : "No figure";
  const detail = [measure.status_detail, measure.missing_configuration.join(", ")]
    .filter(Boolean)
    .join(" · ");

  return (
    <article
      className={compact ? "measure measure--compact" : "measure"}
      data-status={measure.status}
      aria-labelledby={`measure-${measure.code}`}
      title={unavailable ? detail || undefined : undefined}
    >
      <h3 className="measure__label" id={`measure-${measure.code}`}>
        {measure.label}
      </h3>

      {unavailable ? (
        <>
          <p className="measure__absent">
            <span className={`chip chip--${unavailable ? "unavailable" : "neutral"}`}>
              {statusWord}
            </span>
          </p>
          {!compact && measure.status_detail ? (
            <p className="measure__detail">{measure.status_detail}</p>
          ) : null}
          {!compact && measure.missing_configuration.length > 0 ? (
            <p className="measure__missing">
              Awaiting: {measure.missing_configuration.join(", ")}
            </p>
          ) : null}
        </>
      ) : (
        <>
          <p className="measure__value">{formatMeasureValue(measure)}</p>
          {note ? <p className="measure__note">{note}</p> : null}
          <Direction comparison={measure.comparison} compact={compact} />
          {children}
        </>
      )}

      {!compact ? (
        <p className="measure__source">
          <span className="visually-hidden">Source: </span>
          <span className="mono">{measure.source}</span>
          {measure.source_freshness ? (
            <>
              {" · "}
              <span>as at {new Date(measure.source_freshness).toLocaleDateString("en-GB")}</span>
            </>
          ) : null}
        </p>
      ) : null}
    </article>
  );
}

function compactStatus(status: string): string {
  switch (status) {
    case "not_configured":
      return "Not configured";
    case "unavailable":
      return "Unavailable";
    case "outside_scope":
      return "No accessible data";
    default:
      return "No figure";
  }
}

interface MeasureGridProps {
  measures: Measure[];
  compact?: boolean;
}

/** The KPI strip. */
export function MeasureGrid({ measures, compact = false }: MeasureGridProps) {
  return (
    <div className={compact ? "measure-grid measure-grid--compact" : "measure-grid"}>
      {measures.map((measure) => (
        <MeasureCard key={measure.code} measure={measure} compact={compact} />
      ))}
    </div>
  );
}
