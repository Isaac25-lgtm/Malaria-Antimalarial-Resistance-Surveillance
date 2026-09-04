/**
 * Authentication provider.
 *
 * Live sessions are an HttpOnly cookie plus an in-memory CSRF value.
 * Demo sessions may still use a memory-only bearer token.
 * Nothing is written to localStorage or sessionStorage.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, api, getAccessToken, setAccessToken, setCsrfToken } from "../api/client";
import {
  AuthContext,
  SENSITIVITY_ORDER,
  type AuthContextValue,
  type AuthStatus,
  type CurrentUser,
  type SensitivityTier,
} from "./context";
import { resolveLandingPath } from "./landing";

function profileFromSession(
  session: Awaited<ReturnType<typeof api.session>>,
): CurrentUser | null {
  if (!session.authenticated || !session.profile) return null;
  return {
    ...session.profile,
    source_status: session.source_status ?? null,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("initialising");
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const session = await api.session();
        if (cancelled) return;
        if (session.authenticated) {
          setCsrfToken(session.csrf_token ?? null);
          setUser(profileFromSession(session));
          setStatus("authenticated");
          return;
        }
        if (getAccessToken()) {
          const profile = await api.currentUser();
          if (cancelled) return;
          setUser(profile);
          setStatus("authenticated");
          return;
        }
        setStatus("anonymous");
      } catch (caught) {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.isUnauthenticated) {
          setStatus("anonymous");
        } else if (caught instanceof ApiError) {
          setError(caught);
          setStatus("unavailable");
        } else {
          setStatus("anonymous");
        }
      }
    }

    void bootstrap();
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

  const signInWithEregisters = useCallback(async (username: string, password: string) => {
    setError(null);
    const session = await api.liveLogin(username, password);
    setCsrfToken(session.csrf_token ?? null);
    const profile = profileFromSession(session);
    if (!profile) {
      throw new ApiError(401, null, "Sign-in failed.");
    }
    setUser(profile);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setAccessToken(null);
      setCsrfToken(null);
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
      signInWithEregisters,
      signOut,
      can,
      canAccessSensitivity,
      landingPath: resolveLandingPath(user),
    }),
    [
      status,
      user,
      error,
      signInAsDevelopmentUser,
      signInWithEregisters,
      signOut,
      can,
      canAccessSensitivity,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
