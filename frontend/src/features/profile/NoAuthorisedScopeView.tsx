/**
 * Shown when authentication succeeded but MARS geography could not be mapped.
 */

import { useAuth } from "../../auth/context";

export function NoAuthorisedScopeView() {
  const { user } = useAuth();
  const pending = user?.mapping_status === "pending";

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <h1>No authorised scope</h1>
          <p className="page__lede">
            {pending
              ? "Your eRegisters account is signed in, but MARS does not yet have an approved geography mapping for it. Surveillance figures are withheld until that mapping is confirmed."
              : "This account has no usable geography scope in MARS. Access is not broadened, and national data is not shown."}
          </p>
        </div>
      </header>
    </div>
  );
}
