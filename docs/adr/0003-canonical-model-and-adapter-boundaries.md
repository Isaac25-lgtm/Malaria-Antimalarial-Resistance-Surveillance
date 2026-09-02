# ADR 0003: Canonical model and adapter boundaries

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 1-2

## Context

MARS ingests from sources it does not control and cannot freeze: an OPD 002
e-register whose electronic implementation is not yet specified, HMIS 033b and
105 aggregate reporting, DHIS2, and administrative boundary files whose coding
scheme has not been confirmed as authoritative.

Every one of these will change. Uganda creates districts. Form versions are
revised. A DHIS2 upgrade changes an endpoint. If the shape of any source reaches
the analytical core, each of those events becomes a migration of the whole
system rather than a change to one adapter.

The supplied boundary data makes the risk concrete. `FScode` is a well-formed
six-digit hierarchical code that would be tempting to use as a primary key. It
is a national-statistics code, not a confirmed Ministry or DHIS2 identifier.
`OBJECTID` looks like a key and is duplicated across two unrelated subcounties
in the supplied file.

## Decision

MARS defines a canonical model. Every source reaches it through an adapter, and
no domain module imports an adapter.

**Internal identity is always a UUID.** Source identifiers - `FScode`, `FID`,
`OBJECTID`, a DHIS2 UID, a facility code - are stored in their own columns and
are never promoted to a primary key.

**Source codes are aliases.** `geography_unit_alias` and `facility_identifier`
map any source system code to a MARS entity, with an effective date and a review
status. A mapping starts as `proposed`; nothing is silently promoted to
`confirmed`.

**Raw values are preserved.** A raw name and a normalised name are stored
separately. The normalised form supports lookup; the raw form is what a user
sees and what an audit compares against.

**Unresolved stays unresolved.** Where a source value cannot be mapped
confidently, it is recorded as unmapped and reported as a data-quality issue.
MARS never guesses a geography match.

## Consequences

- A source change is an adapter change.
- An authoritative national code can replace `preferred_code` later while every
  internal identifier and foreign key stays fixed.
- More tables, and one more join on source-keyed lookups. Accepted.

## Revisit when

The Ministry confirms an authoritative organisation-unit coding scheme. That
would change which code is `preferred_code`; it would not change this decision.
