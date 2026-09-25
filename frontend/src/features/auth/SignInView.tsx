/**
 * Sign-in.
 *
 * One eRegisters username and password form. Credentials go only to the MARS
 * API, and the account's eRegisters assignments decide what it can see.
 */

import { useId, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { useAuth } from "../../auth/context";
import "./sign-in.css";

interface LocationState {
  from?: string;
}

export function SignInView() {
  return <LiveSignInForm />;
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
        setError("Sign-in is temporarily unavailable. Please try again shortly.");
        setErrorKind("upstream");
      } else if (caught instanceof ApiError && caught.isUnauthenticated) {
        setError("Invalid username or password");
        setErrorKind("credentials");
      } else {
        setError("MARS could not complete sign-in. Please try again shortly.");
        setErrorKind("other");
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
