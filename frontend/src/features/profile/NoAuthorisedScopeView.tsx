/**
 * Shown only when DHIS2 supplied no usable data-view authorization.
 *
 * A signed-in user with a resolved remote workspace and pending local
 * mapping must not land here.
 */

import { useAuth } from "../../auth/context";

export function NoAuthorisedScopeView() {
  const { user } = useAuth();
  const remote = user?.workspace;

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <h1>No authorised scope</h1>
          <p className="page__lede">
            {remote && remote.authorization_status === "resolved"
              ? "This page is for accounts with no usable remote authorization. Your workspace is elsewhere."
              : "This eRegisters account has no usable data-view organisation unit. Access is not broadened, and national data is not shown."}
          </p>
        </div>
      </header>
    </div>
  );
}
