# ADR 0006: Identity separation and the authorisation model

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 2

## Context

MARS will hold patient-level outpatient encounters, including - in the source
register - names, national identity numbers and contact details. It will be used
by national programme staff who need aggregate surveillance, district teams who
need case evidence, facility staff who need their own records, analysts who
manage definitions, and administrators who manage users.

Two failures are easy to build and expensive to correct. The first is letting
direct identifiers spread into analytical tables, after which removing them
means rewriting every query that touches them. The second is treating
authorisation as a single question - "may this user in?" - when it is three:
what may they do, where, and at what level of patient detail.

Blueprint section 009 is explicit that these are separate: national aggregate
access does not imply patient detail, and neither analyst nor administrator
gains patient access from their role.

## Decision

### Identity separation from the first migration

A `mars_identity` schema is created by migration 0001, empty. Direct identifiers
will live only there, from Prompt 8 onwards. It is owned by a separate database
role, and the application role holds no grant on it. A narrow re-identification
role is used by one explicitly permissioned endpoint, and every read writes an
audit row.

The boundary exists before the data does, because retrofitting it means
rewriting every foreign key that crosses it.

The `person_key` will be a salted HMAC of the approved deterministic identifier:
reproducible for linkage, not reversible outside the vault. A separate
non-sequential `MARS-PT-######` alias is what reaches a browser, so the linkage
key never leaves the server.

### Three orthogonal authorisation axes

**Permission** - may this role perform this kind of action at all?
**Geography scope** - does the assigned area cover the requested area?
**Sensitivity scope** - may the caller see this level of patient detail?

All three are enforced server-side, in a router dependency. The interface uses
the same information to decide what to render, but that is a courtesy: a client
that ignores it gains nothing.

Three properties follow, and each has a test:

- **No role grants re-identification.** It is an individual grant with a
  recorded reason, audited on every use.
- **A permission above the caller sensitivity ceiling is dropped**, not silently
  honoured. Holding `case:view_pseudonymous_evidence` without the matching tier
  is a misconfiguration, and it fails closed.
- **An empty geography scope grants nothing.** It is never read as national
  access, which is the safe interpretation of an unprovisioned account.

### Scoping is a query predicate

A district user query never returns another district row, because the scope is a
`WHERE` clause rather than a post-filter. There is no window in which
out-of-scope data exists in the process.

### Authentication is delegated

Production authenticates against an OIDC provider behind a `TokenVerifier`
interface; the domain never learns which one. Roles and scopes always come from
the MARS database, never from token claims, so a provider misconfiguration can
fail to authenticate someone but cannot grant them surveillance access.

The development mode issues synthetic tokens and is guarded three ways: it must
be enabled explicitly, the environment must not be protected, and the settings
validator refuses the combination. Its accounts are flagged synthetic through to
the audit trail, and its routes are not registered at all outside development,
so they cannot appear in a production OpenAPI document.

## Consequences

- Patient identifiers cannot leak into analytical tables through carelessness;
  it would take a deliberate schema change and a new grant.
- Provisioning is more work: a user needs a role, a geography scope and a
  sensitivity scope, and an account missing any of them can read nothing.
- The permission matrix test suite must be maintained as roles evolve. That is
  the point: a change to it is a change to the access model, and should be
  reviewed as one.

## Revisit when

A production identity provider is selected, or the programme defines a
re-identification policy - which would add workflow, not change this structure.
