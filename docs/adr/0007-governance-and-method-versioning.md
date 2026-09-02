# ADR 0007: Configuration governance and method versioning

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 2, binding on phases 6-10

## Context

MARS will compute indicators, construct malaria episodes using an interval rule,
compare observations against a baseline, and score signals. Every one of those
involves a number that is a programme decision rather than an engineering
choice: how long a surveillance window is, how many cases are enough to report,
how much weight recurrence carries in a score.

Blueprint section 019 is explicit that no single interval may be hard-coded as
scientific truth, and section 112 that developers implement approved definitions
rather than inferring clinical thresholds.

Two failures follow if this is not designed for. A threshold written into code
becomes invisible: nobody can see what MARS is using, and changing it needs a
deployment. And an analytical result whose governing rule is not recorded cannot
be reproduced after that rule changes - the signal count moves and nobody can
say why.

None of these values has been supplied. The malaria programme has not provided
surveillance windows, thresholds, minimum counts or signal weights.

## Decision

### Two registries, both versioned

`ConfigurationKey` / `ConfigurationVersion` holds governed operational
parameters. `MethodDefinition` / `MethodVersion` holds the analytical methods
and rule sets themselves, with validation references, artifact checksums and a
rollback relationship.

### Change control

Both follow draft, in-review, approved, active, retired, with rejection possible
from the earlier states. Transitions are validated against an explicit table, so
a version cannot jump from draft to active. Approval records who approved it and
when; activation requires an effective date and retires the previously active
version, so exactly one version of a key is ever in force.

A published version is immutable. A change creates a new version.

### Reproducibility

Every configuration version carries a SHA-256 of its canonical JSON. Every
method version carries a qualified identifier - `IND-TPR@1.2.0` - which
analytical results will stamp. A result can therefore prove which rule content
produced it, years later, after the rule has changed twice.

Rollback restores a previous version and records what it replaced and why,
without rebuilding raw data.

### Nothing is seeded

The registries ship empty. `/api/v1/meta/version` reports the active method
versions and configuration keys, and reports them as empty, which is the honest
state. `active_version()` returns `None` rather than a default: a missing
configuration is a governance gap the caller must surface, not a prompt to
invent a value.

`requires_programme_approval` marks the parameters an engineer may not set
alone, and `provenance` records where a value came from - a programme document,
a working group decision, or an explicit statement that it is a MARS default
pending approval.

## Consequences

- No surveillance threshold is invented anywhere in the codebase.
- Analytical results stay reproducible across rule changes.
- Setting up analytics requires a governance step before a computation step.
  This is intended: an unapproved threshold should be visibly unapproved rather
  than quietly operating.
- More tables and a lifecycle to maintain, before any analytics exist. Accepted:
  retrofitting versioning after results have been produced would leave those
  results unattributable.

## Revisit when

The programme supplies approved parameters, or a signal task needs more than one
active method version at a time - which the blueprint permits only for an
explicitly approved ensemble.
