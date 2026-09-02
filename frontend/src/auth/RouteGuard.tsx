/**
 * Route guards.
 *
 * These decide what to *render*. Server-side enforcement is what decides access;
 * a guard that is bypassed reveals nothing, because the API refuses the call.
 */

import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { ForbiddenState, LoadingState, UnavailableState } from "../design-system/States";
import { useAuth, type SensitivityTier } from "./context";

interface RequireAuthProps {
  children: ReactNode;
  /** Permissions the view needs. All must be held. */
  permissions?: string[];
  /** Sensitivity tier the view needs. */
  sensitivity?: SensitivityTier;
}

/**
 * Gate a route behind authentication and, optionally, authorisation.
 *
 * Anonymous callers are sent to sign-in with their intended destination
 * preserved. Authenticated-but-unauthorised callers see an explanation naming
 * the missing grant - not a redirect, which would leave them guessing.
 */
export function RequireAuth({ children, permissions, sensitivity }: RequireAuthProps) {
  const { status, user, error, can, canAccessSensitivity } = useAuth();
  const location = useLocation();

  if (status === "initialising") {
    return <LoadingState label="your session" rows={2} />;
  }

  if (status === "unavailable") {
    return (
      <UnavailableState
        title="MARS is not reachable"
        description="Your session could not be checked because the API did not respond."
        requestId={error?.requestId ?? null}
        onRetry={() => window.location.reload()}
      />
    );
  }

  if (status === "anonymous" || !user) {
    return <Navigate to="/sign-in" state={{ from: location.pathname }} replace />;
  }

  const missing = (permissions ?? []).filter((permission) => !can(permission));
  if (missing.length > 0) {
    return (
      <ForbiddenState
        requirement={missing.join(", ")}
        description="This view requires access your account does not hold. An administrator can grant it."
      />
    );
  }

  if (sensitivity && !canAccessSensitivity(sensitivity)) {
    return (
      <ForbiddenState
        requirement={`${sensitivity.replace(/_/g, " ")} sensitivity scope`}
        description={
          "This view shows patient-level evidence. Your account is limited to " +
          `${user.max_sensitivity.replace(/_/g, " ")} data.`
        }
      />
    );
  }

  return <>{children}</>;
}

/** Redirect a signed-in user away from the sign-in screen. */
export function RedirectIfAuthenticated({ children }: { children: ReactNode }) {
  const { status, landingPath } = useAuth();
  if (status === "authenticated") {
    return <Navigate to={landingPath} replace />;
  }
  return <>{children}</>;
}
