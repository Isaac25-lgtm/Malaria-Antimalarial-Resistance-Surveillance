# ADR 0005: Scientific terminology and the two evidence lanes

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 1, binding on every later phase

## Context

MARS analyses routine outpatient encounters, weekly surveillance and monthly
reporting. From these it can identify repeat-positive encounters, short-interval
recurrence, unusual treatment and testing combinations, geographic concentration
and facility deviations.

It cannot do more than that. Routine data cannot distinguish recrudescence from
reinfection, prove that a patient took the drug or absorbed it, identify a
parasite genotype, or establish a molecular marker. A repeat-positive encounter
twelve days after treatment is consistent with reinfection, non-adherence, an
incomplete treatment record, a diagnostic error, or reduced drug susceptibility,
and the record contains nothing that separates them.

This is not a caveat to add to a report. It determines what the system may say,
and a system that overstates it would mislead a national malaria programme about
its own drug policy.

The pressure to overstate is real and will recur. A demonstration audience finds
"resistance detected in Gulu" more compelling than "repeat-positive pattern
requiring investigation". The existing board prototype already uses the stronger
phrasing. Any mechanism relying on each future contributor remembering the rule
will eventually fail.

## Decision

### Two structurally separate evidence lanes

**Lane A - routine-derived surveillance signals.** Fed by OPD 002, HMIS 033b,
HMIS 105 and geography. Produces scored, explained, investigable signals. This
lane may never emit a finding of resistance. Its strongest permitted statement
is *priority resistance-surveillance signal*.

**Lane B - externally confirmed findings.** Fed only by therapeutic efficacy
studies, molecular marker results and validated laboratory confirmation, under
separate governance. Stored in its own tables with its own provenance. This is
the only lane whose findings may use confirmatory language, and it enters the
interface as corroborating evidence attached to a Lane A signal - never as an
output of one.

The separation is structural, not editorial. Lane B tables live in their own
module, and no code path writes to them from routine data.

### Permitted language

Potential treatment-response signal; repeat-positive pattern; unusual recurrence
pattern; epidemiological signal requiring investigation; suspected
treatment-response anomaly; priority resistance-surveillance signal.

### Enforcement

1. `scripts/terminology_lint.py` scans source, copy and documentation for
   claims that routine data confirm resistance, and fails the build. It
   distinguishes assertion from discussion, so a sentence stating that MARS does
   not confirm resistance passes, and it exempts the Lane B module.
2. The lint runs first in CI, before the more expensive jobs.
3. `GET /api/v1/meta/evidence-lanes` serves the boundary from one authoritative
   place, so the dashboard, generated reports and documentation cannot disagree
   about where it sits.
4. The interface carries a permanent, non-dismissible statement on every
   surveillance screen.
5. Every signal object will carry a mandatory `counter_evidence[]` and a
   `missing_information[]` naming what routine data cannot establish - adherence
   unknown, drug exposure not measured, genotype unavailable, molecular marker
   unavailable.

## Consequences

- A contributor cannot accidentally ship a resistance claim; they would have to
  defeat a CI gate deliberately.
- Some phrasing is more laborious than the shorter alternative. That is the
  point.
- The lint will occasionally produce a false positive. The inline exemption
  requires a written justification, which makes the override visible in review.

## Revisit when

Never for the boundary itself. The lint patterns will be extended as new
phrasings appear.
