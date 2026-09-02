/**
 * Authentication provider.
 *
 * Holds the session and the caller's effective authorisation. The access token
 * lives in memory only - never in localStorage or a cookie - so closing the tab
 * ends the session and no other script on the origin can read it.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, api, setAccessToken } from "../api/client";
import {
  AuthContext,
  SENSITIVITY_ORDER,
  type AuthContextValue,
  type AuthStatus,
  type CurrentUser,
  type SensitivityTier,
} from "./context";
import { resolveLandingPath } from "./landing";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("initialising");
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  // Probe for an existing session on mount. Without a token this returns 401,
  // which is the expected anonymous state rather than an error.
  useEffect(() => {
    let cancelled = false;

    async function probe() {
      try {
        const profile = await api.currentUser();
        if (!cancelled) {
          setUser(profile);
          setStatus("authenticated");
        }
      } catch (caught) {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.isUnauthenticated) {
          setStatus("anonymous");
        } else if (caught instanceof ApiError) {
          // A dependency failure is not the same as being signed out, and the
          // shell must not present it as one.
          setError(caught);
          setStatus("unavailable");
        } else {
          setStatus("anonymous");
        }
      }
    }

    void probe();
    return () => {
      cancelled = true;
    };
  }, []);

  const signInAsDevelopmentUser = useCallback(async (username: string) => {
    setError(null);
    const session = await api.developmentLogin(username);
    setAccessToken(session.access_token);
    const profile = await api.currentUser();
    setUser(profile);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      // Clear locally even if the server call failed: a session must not
      // survive a sign-out because the network was down.
      setAccessToken(null);
      setUser(null);
      setStatus("anonymous");
    }
  }, []);

  const can = useCallback(
    (permission: string) => user?.permissions.includes(permission) ?? false,
    [user],
  );

  const canAccessSensitivity = useCallback(
    (tier: SensitivityTier) => {
      if (!user) return false;
      const held = SENSITIVITY_ORDER[user.max_sensitivity as SensitivityTier] ?? 0;
      return held >= SENSITIVITY_ORDER[tier];
    },
    [user],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      error,
      signInAsDevelopmentUser,
      signOut,
      can,
      canAccessSensitivity,
      landingPath: resolveLandingPath(user),
    }),
    [status, user, error, signInAsDevelopmentUser, signOut, can, canAccessSensitivity],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
