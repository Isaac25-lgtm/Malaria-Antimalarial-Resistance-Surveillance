/**
 * The four states a data view can be in.
 *
 * Blueprint appendices 144 and 145: an empty screen must say whether there are
 * no signals, no data, no permission, or a failed refresh - and those states
 * must not look identical. In a surveillance system the difference between
 * "no signals here" and "no usable data was submitted" is the difference
 * between reassurance and a blind spot.
 */

import type { ReactNode } from "react";
import "./states.css";

interface LoadingProps {
  /** What is being loaded, for the screen-reader announcement. */
  label: string;
  /** Number of skeleton rows, matched to the shape of the real content. */
  rows?: number;
}

/**
 * Loading placeholder.
 *
 * A skeleton matched to the final layout rather than a spinner, so the page
 * does not jump when data arrives, and never a blank screen - which reads as a
 * failure (appendix 145).
 */
export function LoadingState({ label, rows = 3 }: LoadingProps) {
  return (
    <div className="state state--loading" aria-busy="true" aria-live="polite">
      <span className="visually-hidden">{`Loading ${label}`}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton" aria-hidden="true" />
      ))}
    </div>
  );
}

interface EmptyProps {
  title: string;
  description: string;
  action?: ReactNode;
}

/**
 * Nothing matched, but the system is working and the data is present.
 *
 * Distinct from NoDataState: this says "we looked and found none", which is a
 * finding in its own right.
 */
export function EmptyState({ title, description, action }: EmptyProps) {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      <p className="state__description">{description}</p>
      {action}
    </div>
  );
}

interface NoDataProps {
  title: string;
  description: string;
  /** What still has to happen before this view can show anything. */
  awaiting?: string;
}

/**
 * No data has been loaded for this view yet.
 *
 * The distinction from EmptyState matters: blueprint section 053 warns that the
 * absence of alerts may reflect the absence of usable data, and the interface
 * must never let one be mistaken for the other.
 */
export function NoDataState({ title, description, awaiting }: NoDataProps) {
  return (
    <div className="state state--no-data">
      <span className="chip chip--unavailable">No data</span>
      <p className="state__title">{title}</p>
      <p className="state__description">{description}</p>
      {awaiting ? <p className="state__meta">{`Awaiting: ${awaiting}`}</p> : null}
    </div>
  );
}

interface ForbiddenProps {
  /** The permission or scope the caller does not hold. */
  requirement: string;
  description?: string;
}

/**
 * The caller is authenticated but not authorised.
 *
 * Names the missing grant rather than the resource, so the message never
 * confirms that something exists.
 */
export function ForbiddenState({ requirement, description }: ForbiddenProps) {
  return (
    <div className="state state--forbidden" role="alert">
      <span className="chip chip--attention">Not permitted</span>
      <p className="state__title">You do not have access to this view</p>
      <p className="state__description">
        {description ?? "Your account does not hold the required access."}
      </p>
      <p className="state__meta mono">{`Requires: ${requirement}`}</p>
    </div>
  );
}

interface UnavailableProps {
  title: string;
  description: string;
  /** Diagnostic identifier for a support conversation. */
  requestId?: string | null;
  onRetry?: () => void;
}

/**
 * A dependency failed. The system cannot answer, and says so.
 *
 * Blueprint section 083: never present stale or absent data as current. An
 * unavailable view is honest; an empty one in its place would not be.
 */
export function UnavailableState({
  title,
  description,
  requestId,
  onRetry,
}: UnavailableProps) {
  return (
    <div className="state state--unavailable" role="alert">
      <span className="chip chip--priority">Unavailable</span>
      <p className="state__title">{title}</p>
      <p className="state__description">{description}</p>
      {requestId ? (
        <p className="state__meta mono">{`Request ID: ${requestId}`}</p>
      ) : null}
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

interface StaleProps {
  /** When the data being shown was last successfully refreshed. */
  lastRefreshed: string;
  /** Which indicators or sources are affected. */
  affected?: string;
}

/**
 * The view is showing the last good data after a failed refresh.
 *
 * Kept visible rather than dismissible: a stale national dashboard presented as
 * current is the failure mode blueprint section 083 exists to prevent.
 */
export function StaleBanner({ lastRefreshed, affected }: StaleProps) {
  return (
    <div className="notice notice--attention" role="status">
      <div>
        <div className="notice__title">Showing data from an earlier refresh</div>
        <div>
          {`Last successful refresh: ${lastRefreshed}.`}
          {affected ? ` Affected: ${affected}.` : ""}
        </div>
      </div>
    </div>
  );
}
