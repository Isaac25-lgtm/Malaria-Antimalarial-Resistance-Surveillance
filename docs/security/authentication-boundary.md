# Human authentication boundary

React never receives DHIS2 credentials, tokens or passwords. The browser never
calls `eregisters.health.go.ug`.

## Live local pilot (`MARS_AUTH_MODE=live`)

An authorised Ministry user types their eRegisters username and password on the
MARS login page. The browser posts those values **only** to the MARS API. The
API authenticates server-to-server against `https://eregisters.health.go.ug`
over verified HTTPS (Basic authentication behind
`AuthenticationProvider`, replaceable later by PAT/OAuth/OIDC).

MARS then issues an opaque HttpOnly session cookie. Upstream credentials stay
in process memory for that session only. See `docs/security/live-sessions.md`.

This path is refused in staging and production. Those environments still
require Ministry OIDC (`oidc_issuer`).

## Demo (`MARS_AUTH_MODE=demo`)

Development authentication (`MARS_DEV_AUTH_ENABLED`) is synthetic and must
remain visibly labelled. It is not production authentication and it is not a
fallback for a failed live login.

## Discovery tokens

A GET-restricted DHIS2 PAT belongs only in the API/worker process environment
for metadata discovery. It is not a human login.
