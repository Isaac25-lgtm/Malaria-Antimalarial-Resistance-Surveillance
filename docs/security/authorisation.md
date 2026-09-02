# Authorisation model

Three independent checks, all enforced server-side. Hiding a control in the
interface is not access control.

## The three axes

### 1. Permission

May this caller perform this kind of action at all? Permissions are granted to
roles, roles to users. The default is denial.

### 2. Geography scope

Does the caller assigned area cover the requested area? A scope names a
geography unit; the caller may read that unit and everything beneath it, matched
on the materialised path.

Two properties matter:

- **Path containment respects segment boundaries.** A scope of `UG/3/30` does
  not cover `UG/3/304`. Without the separator check it would, and a district
  user would silently gain a neighbour.
- **An empty scope grants nothing.** It is never read as national access, which
  is the safe interpretation of an unprovisioned account.

Ancestors of a scope root are visible so a district user can render the
breadcrumb *Uganda / Northern / Gulu* without seeing sibling districts.

### 3. Sensitivity scope

May the caller see this level of patient detail?

| Tier | Value | Grants |
| --- | ---: | --- |
| `aggregate` | 10 | Counts, rates, signals over administrative areas |
| `pseudonymous_case` | 20 | Case evidence keyed by MARS display alias |
| `direct_identity` | 30 | Name, national identity number, contact. Re-identification |

The effective ceiling is the lower of what the user was granted and what their
roles permit. A generous user grant cannot exceed the role ceiling, and a
generous role cannot exceed the user grant.

**A permission above the ceiling is dropped, not honoured.** Holding
`case:view_pseudonymous_evidence` with only aggregate sensitivity is a
misconfiguration, and it fails closed rather than silently upgrading the caller.

## Roles

| Role | May see | Notably may not |
| --- | --- | --- |
| National programme | Aggregate surveillance across Uganda, investigation workflow, reporting | Patient-level evidence |
| District / HSD | Aggregate plus pseudonymous case evidence, within their geography | Any other district |
| Facility | Their own facility, its data quality and case evidence | Sibling facilities in the same district |
| Analyst | Configuration, method registry, aggregate surveillance | Any patient-level data |
| Administrator | Users, geography, organisation units, integrations, audit | Surveillance data or patient data |

Two deliberate omissions, both from blueprint section 009:

- **No role grants `patient:reidentify`.** It is an individual grant with a
  recorded reason, audited on every use. A test asserts this for every role.
- **Analyst and administrator receive no patient-evidence permission.** Managing
  definitions or managing users does not confer access to patient data.

## Facility scope

A separate, narrower axis. A facility user is scoped to named facilities, not to
their district: sharing a district with another facility grants nothing. It is
intersected with geography scope, so even an erroneous cross-district facility
assignment cannot broaden access.

## Enforcement

Authorisation lives in `mars.api.dependencies` and nowhere else. A handler
receives an already-authorised principal; it never decides for itself whether
the caller was allowed in.

```python
GeographyViewer = Annotated[
    AuthenticatedPrincipal, Depends(require_permissions(Permission.GEOGRAPHY_VIEW))
]
```

Scoping is applied **inside the query**, not as a post-filter, so there is no
window in which out-of-scope data exists in the process.

A denial names the missing permission, never the resource, so a 403 does not
confirm that something exists. Denials of sensitive actions are audited through
a separate short-lived transaction, because the rejected request transaction is
rolled back by design. The audit commit never commits work from that request.

Alias resolution and single-record lookups carry the same SQL-level geography
predicates as list queries. An organisation unit without a geography link is
globally visible only when its type is explicitly `national`; an accidentally
unlinked district or HSD is not treated as national.

## Authentication

Production uses OIDC behind a `TokenVerifier` interface; the domain never learns
which provider. **Roles and scopes always come from the MARS database, never
from token claims**, so a provider misconfiguration can fail to authenticate
someone but cannot grant them surveillance access.

A valid token for an unknown subject is rejected. MARS does not create an
account because a provider vouched for someone; provisioning is an
administrative act.

### Development mode

Synthetic accounts, guarded three ways: enabled explicitly, environment not
protected, and the settings validator refuses the combination outright. Every
subject is prefixed `dev:` and flagged synthetic through to the audit trail. The
routes are not registered outside development, so they cannot appear in a
production OpenAPI document.

| Account | Role | Scope |
| --- | --- | --- |
| `national.programme` | National programme | Uganda, aggregate only |
| `district.gulu` | District / HSD | Gulu only |
| `district.pader` | District / HSD | Pader only - proves cross-district denial |
| `facility.gulu` | Facility | One facility in Gulu |
| `analyst` | Analyst | Uganda, no patient access |
| `administrator` | Administrator | No surveillance access |

## The permission matrix test suite

`backend/tests/security/test_permission_matrix.py` is the specification of who
may do what. If one of those tests fails, the access model has changed, and that
change is a decision requiring approval - not a test to update.

It covers: national aggregate access without patient evidence; district
cross-boundary denial including the path-prefix collision case; facility
isolation from siblings; analyst and administrator without patient access;
re-identification requiring both permission and sensitivity tier; and an
unscoped account reading nothing.
