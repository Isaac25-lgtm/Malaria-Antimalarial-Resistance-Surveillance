# Deployment runbook

How to stand MARS up, and what has to be true before you do.

**MARS is not currently hosted anywhere.** This repository produces the
artefacts a deployment needs; no external environment has been provisioned and
no credentials for one exist. See [Release status](#release-status).

## What a deployment needs

| Requirement | Detail |
| --- | --- |
| PostgreSQL | 16 or later, with PostGIS 3.4+. Managed, backed up and patched by whoever operates it. |
| Two database roles | The application role, and a separate restricted role for `mars_identity`. See `scripts/provision_identity_roles.sql`. |
| An OIDC provider | Issuer and audience. MARS consumes tokens; it does not implement authentication factors. |
| Encryption keys | For the identity vault, as `version:secret` pairs. Supplied through the environment only. |
| A reverse proxy | Terminates TLS, sets forwarded headers, enforces rate limits. |
| Container registry | For the two published images. |

## Environment

Every setting arrives through the environment. Nothing is baked into an image,
so one artefact runs in every environment.

### Required

```
MARS_ENVIRONMENT=production
MARS_DATABASE_URL=postgresql+psycopg://USER@HOST:5432/mars
MARS_IDENTITY_DATABASE_URL=postgresql+psycopg://IDENTITY_USER@HOST:5432/mars
MARS_ENCRYPTION_KEYS=v1:...
MARS_OIDC_ISSUER=https://id.example.org/realms/moh
MARS_OIDC_AUDIENCE=mars
```

Passwords are supplied through `PGPASSWORD`, a `.pgpass` file or the
orchestrator's secret store — never in a URL that appears in a process list or
a log line.

### Optional, and off unless set

```
MARS_CORS_ALLOW_ORIGINS=https://mars.example.org
MARS_AI_ASSISTANT_ENABLED=false
MARS_DHIS2_ENABLED=false
MARS_API_WORKERS=4
MARS_TRUSTED_PROXY_IPS=10.0.0.0/8
```

`MARS_CORS_ALLOW_ORIGINS` **may not contain `*`**. MARS sends credentials
cross-origin, and the application refuses to start with a wildcard rather than
silently narrowing it: a misconfiguration that ships quietly is worse than one
that fails loudly.

### Refused in a protected environment

`MARS_DEV_AUTH_ENABLED` and `MARS_DEMO_MODE_ENABLED` are refused when
`MARS_ENVIRONMENT` is `staging` or `production`. The application will not start.

## Reverse proxy and TLS

TLS terminates at the proxy. Nothing in the stack listens on a public
interface.

The proxy must:

* set `X-Forwarded-For` and `X-Forwarded-Proto`, and be listed in
  `MARS_TRUSTED_PROXY_IPS` — otherwise the client address in the audit log is
  the proxy's;
* **enforce rate limiting.** MARS does not implement it. A token bucket inside
  the application gives a false sense of protection against a distributed
  source, and the proxy is where the request is cheapest to reject;
* route `/api/` to the API service and everything else to the frontend;
* not cache API responses. Every response carries `Cache-Control: no-store`,
  and a proxy that overrides it would serve one district officer's data to the
  next.

## Startup order

1. `migrate` runs `alembic upgrade head` to completion.
2. `api` and `worker` start only after it succeeds.

A database at an older revision than the code is the failure mode this ordering
prevents. The compose file expresses it with
`condition: service_completed_successfully`.

## Health checks

| Endpoint | Use | Meaning |
| --- | --- | --- |
| `/api/v1/health/live` | Container liveness | The process is running. Restart on failure. |
| `/api/v1/health/ready` | Proxy readiness | Dependencies answer. Take out of rotation on failure — **do not restart**. |
| `/api/v1/health/schema` | Deployment verification | Reports the migration head. |

The distinction matters: a database blip should remove an instance from
rotation, not restart it. Restarting will not reach the database any faster and
loses the in-flight requests.

None of the three exposes a credential, a connection string or any surveillance
content; a test asserts this.

## Workers

The worker runs as its own service from the same image, so a long analytical
run cannot starve request serving, and the two processes can never drift apart
in dependency versions. A worker computing an indicator differently from the
API that serves it would be an invisible and serious defect.

Jobs are idempotent and keyed by an input fingerprint: re-running over unchanged
evidence writes nothing, and changed evidence writes a new row beside the old.
A worker can be restarted mid-run without corrupting a result.

## Verifying a deployment

```bash
curl -fsS https://mars.example.org/api/v1/health/ready
curl -fsS https://mars.example.org/api/v1/health/schema      # expect head 0023_active_signal_index
curl -fsS https://mars.example.org/api/v1/meta/version
curl -fsSI https://mars.example.org/api/v1/health/live | grep -i x-frame-options
```

A fresh deployment is **analytically unconfigured**: every measure reports
`not_configured` and names the approval it is waiting for. That is correct, and
it is what the command centre will show until a programme approves indicator
versions, baseline methods, anomaly rules, hotspot definitions and a spatial
privacy policy. See [governance activation](./operations.md#governance-activation).

## Release status

1. **Local implementation: complete.** All prompts through 30 are implemented,
   tested and committed.
2. **Production artefacts: complete.** Images, compose topology, migration
   procedure, runbooks and a tested restore drill.
3. **External hosting: pending.** No environment has been provisioned, no
   credentials exist, and nothing has been deployed anywhere. MARS is not
   running at any URL, and this document does not claim otherwise.
