/**
 * Sign-in.
 *
 * In staging and production this screen redirects to the configured OIDC
 * provider. This build has no provider, so it offers the synthetic development
 * accounts - visibly marked as such, because a development session must never
 * be mistaken for a real one.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { useAuth } from "../../auth/context";
import { LoadingState, UnavailableState } from "../../design-system/States";
import "./sign-in.css";

interface LocationState {
  from?: string;
}

export function SignInView() {
  const { signInAsDevelopmentUser } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [signingIn, setSigningIn] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const users = useQuery({
    queryKey: ["auth", "dev-users"],
    queryFn: api.developmentUsers,
    retry: false,
  });

  async function handleSignIn(username: string) {
    setSigningIn(username);
    setError(null);
    try {
      await signInAsDevelopmentUser(username);
      const intended = (location.state as LocationState | null)?.from;
      navigate(intended && intended !== "/sign-in" ? intended : "/", { replace: true });
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? (caught.problem?.detail ?? caught.message)
          : "Sign-in failed.",
      );
    } finally {
      setSigningIn(null);
    }
  }

  return (
    <main className="sign-in">
      <div className="sign-in__panel">
        <header className="sign-in__header">
          <span className="sign-in__mark" aria-hidden="true">
            M
          </span>
          <div>
            <h1>MARS</h1>
            <p className="sign-in__subtitle">
              Malaria Antimalarial Resistance Surveillance
            </p>
          </div>
        </header>

        <div className="notice notice--attention">
          <div>
            <div className="notice__title">Development authentication</div>
            <div>
              No identity provider is configured for this deployment. The accounts below
              are synthetic and exist only to exercise the access model. They are refused
              outright in staging and production.
            </div>
          </div>
        </div>

        {users.isPending ? (
          <LoadingState label="available accounts" rows={4} />
        ) : users.isError ? (
          <UnavailableState
            title="Sign-in is unavailable"
            description={
              users.error instanceof ApiError && users.error.isUnavailable
                ? "Development authentication is not enabled on this deployment."
                : "The MARS API could not be reached."
            }
            requestId={users.error instanceof ApiError ? users.error.requestId : null}
            onRetry={() => void users.refetch()}
          />
        ) : (
          <>
            <h2 className="sign-in__list-heading">Choose an account</h2>
            <ul className="sign-in__users">
              {users.data.map((user) => (
                <li key={user.username}>
                  <button
                    type="button"
                    className="sign-in__user"
                    onClick={() => void handleSignIn(user.username)}
                    disabled={signingIn !== null}
                    aria-busy={signingIn === user.username}
                  >
                    <span className="sign-in__user-name">{user.display_name}</span>
                    <span className="sign-in__user-role mono">{user.role}</span>
                    <span className="sign-in__user-scope">{user.scope_description}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}

        {error ? (
          <p className="sign-in__error" role="alert">
            {error}
          </p>
        ) : null}

        <p className="sign-in__boundary">
          MARS signals indicate patterns requiring investigation. They do not confirm
          antimalarial resistance.
        </p>
      </div>
    </main>
  );
}
