/**
 * Authentication context object, types and accessor hook.
 *
 * Separated from the provider component so that each module exports one kind of
 * thing: this file exports types and a hook, `AuthProvider.tsx` exports a
 * component. React Fast Refresh requires that split, and it also keeps the
 * contract readable on its own.
 */

import { createContext, useContext } from "react";

import type { ApiError } from "../api/client";
import type { CurrentUser } from "./landing";

export type { CurrentUser };

/** Session lifecycle, so the shell can distinguish "checking" from "signed out". */
export type AuthStatus = "initialising" | "anonymous" | "authenticated" | "unavailable";

/** Data sensitivity tiers, mirroring the server's SensitivityLevel. */
export type SensitivityTier = "aggregate" | "pseudonymous_case" | "direct_identity";

export const SENSITIVITY_ORDER: Record<SensitivityTier, number> = {
  aggregate: 10,
  pseudonymous_case: 20,
  direct_identity: 30,
};

export interface AuthContextValue {
  status: AuthStatus;
  user: CurrentUser | null;
  error: ApiError | null;
  signInAsDevelopmentUser: (username: string) => Promise<void>;
  signOut: () => Promise<void>;
  /**
   * Whether the signed-in user holds a permission.
   *
   * Decides what the interface *renders*, never what the caller may *do*: every
   * endpoint re-checks server-side, so a client that ignores this gains nothing
   * (blueprint section 057).
   */
  can: (permission: string) => boolean;
  /** Whether the user may reach the given data sensitivity tier. */
  canAccessSensitivity: (tier: SensitivityTier) => boolean;
  /** Where this user should land after signing in. */
  landingPath: string;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside an AuthProvider");
  }
  return context;
}
