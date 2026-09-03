/**
 * Breadcrumbs.
 *
 * Blueprint 048 keeps the trail visible through every drill-down, so a reader
 * always knows which geography a figure belongs to. The current page carries
 * aria-current rather than being marked by styling alone.
 */

import { Link } from "react-router-dom";

export interface Crumb {
  to?: string;
  label: string;
}

export function Breadcrumbs({ trail }: { trail: Crumb[] }) {
  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      <ol>
        {trail.map((crumb, index) => {
          const last = index === trail.length - 1;
          return (
            <li key={`${crumb.label}-${String(index)}`}>
              {crumb.to && !last ? (
                <Link to={crumb.to}>{crumb.label}</Link>
              ) : (
                <span aria-current={last ? "page" : undefined}>{crumb.label}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
