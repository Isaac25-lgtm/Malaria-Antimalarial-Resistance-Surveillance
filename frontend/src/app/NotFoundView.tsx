/**
 * Unknown route.
 *
 * Kept distinct from "no data" and from "not permitted": a wrong URL is a
 * different problem from an empty district or a missing grant, and the three
 * must not look alike.
 */

import { Link } from "react-router-dom";

export function NotFoundView() {
  return (
    <div className="state state--empty">
      <p className="state__title">This page does not exist</p>
      <p className="state__description">
        The address may be mistyped, or it may belong to a part of MARS that has not been
        built yet. The surveillance workspaces arrive with later phases.
      </p>
      <Link className="button" to="/status">
        Go to system status
      </Link>
    </div>
  );
}
