# Live cookie sessions (Pader eRegisters password pilot)

This is a **local, single-process** authentication path. It is not Ministry
OIDC and it is not production.

## What is stored

- The browser holds an opaque session identifier in an HttpOnly `mars_session`
  cookie (SameSite=Lax, Path=/, Secure outside local HTTP). The cookie has no
  credential content.
- The API stores a SHA-256 hash of that identifier in process memory, with
  idle and absolute expiry.
- A non-secret CSRF value is issued separately (`mars_csrf` cookie and
  `X-CSRF-Token` header). Unsafe methods require both an approved Origin and
  a matching CSRF header.
- DHIS2 username/password for the active session live only in
  `InMemoryCredentialHolder`, keyed by the raw session id.

## What is never stored

Credentials are never written to PostgreSQL, a file, Redis, a cookie, a JWT,
an API response, browser storage, or a log.

## Honest limitations

- **Single process.** A second uvicorn worker cannot see these sessions.
  Restarting the API signs everyone out. Replace the holder and the session
  store before any multi-process deployment.
- **Python strings cannot be reliably zeroed.** Logout and expiry drop
  references so the values become unreachable. They do not wipe bytes from
  memory.
- **Temporary Basic authentication.** `Dhis2BasicAuthProvider` can be replaced
  by PAT/OAuth/OIDC without rewriting MARS sessions or scope enforcement.

## Isolation

Live mode (`MARS_AUTH_MODE=live`) requires database `mars_live` and refuses
`mars_local`, demo mode, and development authentication. Failed live login
never falls back to synthetic accounts.
