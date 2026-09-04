# Human authentication boundary

Upstream DHIS2/eRegisters authentication and human MARS authentication are
separate.

- React never receives DHIS2 credentials, tokens or passwords.
- The browser never calls `eregisters.health.go.ug`.
- Development authentication (`MARS_DEV_AUTH_ENABLED`) is synthetic and must
  remain visibly labelled. It is not production authentication.
- A GET-restricted DHIS2 PAT belongs only in the API/worker process environment.

A "Sign in with eRegisters" button that posts a DHIS2 password to MARS, or that
reuses that password as the MARS session, is forbidden.

If a human must authenticate *through* DHIS2, the only acceptable design is a
DHIS2 OAuth 2 authorisation-code flow with an OAuth client registered by the
DHIS2 administrator. That is not implemented here.

## Remaining decision (not taken)

The dashboard can be visually completed before this is settled. It cannot be
declared production-ready until one of the following is **explicitly approved**.

### Option A — preferred production path

Ministry OIDC/OAuth identity. Required in staging and production by current
settings guards.

### Option B — explicitly approved Pader pilot only

A MARS-local user implementation, only if a Pader pilot cannot yet join Ministry
identity. If approved, it must include:

- Argon2id password hashing;
- a secure HttpOnly session cookie;
- CSRF protection;
- login throttling and account lockout;
- a short session lifetime;
- password rotation;
- audit events;
- no default credentials;
- a hidden account-provisioning prompt.

Option B is **not** implemented. Do not treat development accounts as Option B.

Until that decision, local sign-in remains development authentication and every
session is marked synthetic when `is_synthetic` is true.
