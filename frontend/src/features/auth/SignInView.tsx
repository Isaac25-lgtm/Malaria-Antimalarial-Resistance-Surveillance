/**
 * Sign-in.
 *
 * Live mode: one eRegisters username and password form. Credentials go only
 * to the MARS API. Demo mode keeps the synthetic account chooser.
 */

import { useId, useRef, useState } from "react";
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
  const version = useQuery({
    queryKey: ["meta", "version"],
    queryFn: api.version,
    retry: false,
    staleTime: 30_000,
  });

  if (version.isPending) {
    return (
      <main className="sign-in">
        <div className="sign-in__panel">
          <SignInHeader />
          <LoadingState label="sign-in" rows={3} />
        </div>
      </main>
    );
  }

  const data = version.data as {
    live_login_enabled?: boolean;
    auth_mode?: string;
    development_auth_active?: boolean;
    demo_mode_enabled?: boolean;
  } | undefined;
  const live = data?.live_login_enabled === true || data?.auth_mode === "live";
  const demo = data?.development_auth_active === true || data?.demo_mode_enabled === true;

  if (live || !demo) {
    return <LiveSignInForm />;
  }
  return <DemoSignInChooser />;
}

function SignInHeader() {
  return (
    <header className="sign-in__header">
      <span className="sign-in__mark" aria-hidden="true">
        M
      </span>
      <div>
        <h1>MARS</h1>
        <p className="sign-in__subtitle">Malaria Antimalarial Resistance Surveillance</p>
        <p className="sign-in__tagline">
          Routine-data early warning and malaria surveillance
        </p>
      </div>
    </header>
  );
}

function LiveSignInForm() {
  const { signInWithEregisters } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const usernameId = useId();
  const passwordId = useId();
  const errorId = useId();
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorKind, setErrorKind] = useState<"credentials" | "upstream" | "other" | null>(
    null,
  );

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    const usernameValue = data.get("username");
    const passwordValue = data.get("password");
    const username = typeof usernameValue === "string" ? usernameValue : "";
    const password = typeof passwordValue === "string" ? passwordValue : "";
    setSubmitting(true);
    setError(null);
    setErrorKind(null);
    try {
      await signInWithEregisters(username, password);
      const intended = (location.state as LocationState | null)?.from;
      navigate(intended && intended !== "/sign-in" ? intended : "/", { replace: true });
    } catch (caught) {
      if (caught instanceof ApiError && caught.isUnavailable) {
        setError("Unable to connect to eRegisters");
        setErrorKind("upstream");
      } else if (caught instanceof ApiError && caught.isUnauthenticated) {
        setError("Invalid username or password");
        setErrorKind("credentials");
      } else {
        setError("Invalid username or password");
        setErrorKind("credentials");
      }
      usernameRef.current?.focus();
    } finally {
      if (passwordRef.current) passwordRef.current.value = "";
      form.reset();
      if (usernameRef.current && username) usernameRef.current.value = username;
      setSubmitting(false);
    }
  }

  return (
    <main className="sign-in">
      <div className="sign-in__panel">
        <SignInHeader />
        <form className="sign-in__form" onSubmit={(event) => void handleSubmit(event)} noValidate>
          {error ? (
            <p className="sign-in__error" role="alert" id={errorId}>
              {error}
            </p>
          ) : null}
          <div className="sign-in__field">
            <label htmlFor={usernameId}>Username</label>
            <input
              ref={usernameRef}
              id={usernameId}
              name="username"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              disabled={submitting}
              aria-invalid={errorKind === "credentials"}
              aria-describedby={error ? errorId : undefined}
            />
          </div>
          <div className="sign-in__field">
            <label htmlFor={passwordId}>Password</label>
            <input
              ref={passwordRef}
              id={passwordId}
              name="password"
              type="password"
              autoComplete="current-password"
              required
              disabled={submitting}
              aria-invalid={errorKind === "credentials"}
              aria-describedby={error ? errorId : undefined}
            />
          </div>
          <button type="submit" className="button button--primary" disabled={submitting}>
            {submitting ? "Signing in" : "Sign in"}
          </button>
          <p className="sign-in__hint">Use your authorised Ministry eRegisters account.</p>
        </form>
        <p className="sign-in__boundary">
          MARS signals indicate patterns requiring investigation. They do not confirm
          antimalarial resistance.
        </p>
      </div>
    </main>
  );
}

function DemoSignInChooser() {
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
        <SignInHeader />

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
