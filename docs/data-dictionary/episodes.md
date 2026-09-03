# Malaria episode candidates

An episode is a grouping of one patient's encounters that **may** be a single
illness. The word *candidate* is load-bearing, and so is the uncertainty it
names.

## What routine data cannot say

**It cannot distinguish recrudescence from reinfection.** A second positive
result forty days after the first may be the same infection that survived
treatment, or a new one from a new bite. Nothing in an e-register separates
them; that needs parasite genotyping.

**It cannot prove adherence or drug exposure.** A prescription line says a drug
was *prescribed*. It does not say it was dispensed, taken, taken correctly, or
of adequate quality. An episode recording "treated" is recording a register
entry, not a pharmacological fact.

So MARS records the visits, the actual intervals, the results and whether
treatment was written down — and calls a repeat positive what it is: a pattern
worth investigating.

Every repeat-positive episode carries this on the row itself, in
`uncertainty.interpretation_limit`. It is not a footnote for a UI to add.

## MARS supplies no episode window

The engine reads its window from a governed `malaria_episode_rule` method
version and **refuses to run without one**, recording a `not_configured` build
that names the missing parameter.

That refusal is the most important behaviour in the module. Whether two
positives are one illness or two depends on the drug, the setting and the
programme's guidance. There is no defensible universal answer, so MARS does not
invent one.

```
Required: an approved MethodVersion with
  code       = malaria_episode_rule
  kind       = episode_rule
  status     = active
  parameters = { "episode_window_days": <programme-approved integer> }
```

An active rule whose parameter is missing or nonsensical is treated as
**absent**, not repaired — repairing it would mean choosing the window.

## Grouping

A new episode starts when the gap since the **previous encounter** exceeds the
window. Measured from the previous encounter rather than from the episode's
first, so an illness with weekly follow-ups is one episode rather than being
split arbitrarily at the window boundary.

## Identity

Grouping is by `patient_reference_id` and nothing else. The engine imports
nothing from `mars.identity`, the vault is never queried, and no episode column
can hold a name — both enforced by tests.

**Unlinked encounters are counted, never invented.** An encounter with no usable
identifier cannot join an episode. `episode_build.encounters_unlinked` is the
size of what MARS cannot see, and a recurrence rate computed without it would be
quietly overstated.

## Intervals are days, never bands

`episode_member.days_since_previous` stores actual days. Interval bands are
governed configuration; an interval recorded as a band cannot be re-banded when
the programme changes them.

## What is recorded

| Table | Holds |
| --- | --- |
| `episode_build` | rule version, period, input fingerprint, source cutoff, engine version, counts including unlinked |
| `episode_candidate` | span, encounter/positive/tested/treated counts, index facility, residence, uncertainty |
| `episode_member` | sequence, role (index / follow-up / repeat positive), actual interval, denormalised evidence |

Episode statuses: `candidate`, `open_at_period_end` (the window has not closed,
so the episode may continue — saying so beats presenting it as finished), and
`qualified` (grouped, with something a reviewer must see first).

## Builds are idempotent and immutable

A build is keyed by rule version, period and a fingerprint of the encounters it
read — including each encounter's `updated_at`. Re-running over unchanged
evidence returns the existing build. A corrected encounter produces a **new**
build, because episodes a clinician has already read must not silently change.

## Worker

`episode.build` — runs for a period. Reports `not_configured` when no rule is
approved.

## What the programme still has to supply

| Input | Needed for |
| --- | --- |
| An approved `malaria_episode_rule` with `episode_window_days` | Any episode at all |
| Recurrence interval bands | Prompt 15; deliberately absent here |
