# Configurable positive-to-positive recurrence

Engine version `positive-recurrence/1.1.0`. Duplicate rules `duplicate-rules/1.1.0`.

This document is the contract the code implements. The defaults below are
**implementation defaults for exploratory analysis**, not a clinically validated
method. A repeat-positive pattern is a reason to review a patient. It is not
evidence of treatment failure, recrudescence or antimalarial resistance, and no
output of this engine says otherwise.

Code: `backend/src/mars/domain/longitudinal.py` (the source-neutral contract, in
`mars.domain` because the DHIS2 adapter may not import `mars.analytics`),
`duplicate_detection.py` (duplicate classification), `positive_recurrence.py`
(evaluation) and `recurrence_views.py` (projections) in `mars/analytics`, and
`services/live_recurrence.py`, which runs the engine for the live snapshot on
the adapter's behalf.

## 1. One engine, two source adapters

| Adapter | Input | Output |
| --- | --- | --- |
| `integrations/dhis2/tracker/clinical_adapter.py` | Live Tracker events (laboratory, medical-visit, medicine stages) | `CanonicalEncounter` |
| `services/stored_encounter_adapter.py` | Normalised local `OpdEncounter` rows | `CanonicalEncounter` |

Both produce the same contract, and only the canonical contract reaches the
engine. Equivalent evidence through either adapter produces identical findings,
intervals and totals. A test enforces this.

The historical episode engine (`analytics/episodes.py`, gap between successive
*attendances*) and the recurrence measures built on it are **a different
method** and are not reinterpreted. Their stored results keep their original
meaning and method version.

## 2. Canonical evidence

A `CanonicalEncounter` carries a stable patient linkage key *within a verified
source namespace*, the encounter's facility, date and date precision, every
component test, diagnoses, observations, referrals, outcome, medications, every
source record revision it was built from, per-field availability and quality
flags.

Absence is typed and never collapsed into a negative:

| Value | Meaning |
| --- | --- |
| `recorded` | The source recorded a value |
| `not_recorded` | The source record exists and the field is empty |
| `not_mapped` | MARS holds no verified mapping for this field |
| `not_returned` | The retrieval did not bring the record or field back |
| `not_authorized` | The current user may not see it |

A test outcome is `positive`, `negative`, `not_done`, `unmapped` (a value the
mapping cannot interpret) or `not_returned`. **Only `positive` is a confirmed
positive.** An unmapped result is never a negative.

Medicines record what the source says: `prescribed`, `dispensed`, or `recorded`
where the source does not distinguish. A prescription is never shown as
dispensing, and dose, adherence or administration are never inferred from a
drug name or quantity.

### Dates

Local calendar days use the configured reporting timezone, initially
`Africa/Kampala`. A timestamp-precision value is converted to that zone before
its day is taken. A date-only value is kept as recorded; no time is invented. An
encounter with no usable date is flagged `DATE_QUALITY_ISSUE`, stays visible in
the history, and cannot take part in a sequence.

DHIS2 event dates (`occurredAt`) are event dates entered at the facility. The
live adapter treats them as **date precision**, taking the date as recorded. It
does not shift them through a timezone conversion.

### Linkage

Cross-facility linkage requires the same stable source patient reference within
one verified source namespace. MARS never links by name or approximate
demographics. It never treats a local UUID as equivalent to a DHIS2 identifier.
An encounter without a stable patient reference is kept, counted as an unlinked
positive where it is positive, and never takes part in recurrence.

### Grouping tests and attaching treatment

Tests that share a verified clinical parent reference form **one encounter**,
and every test is retained. RDT plus microscopy on one visit is one positive
encounter, not two. Records without a parent are **not** merged. They remain
distinct candidates, and the same-day policy governs how they count.

A medicine attaches to an encounter only through the same verified parent
reference. Same patient and a nearby date are not enough. An unlinked medicine
is kept as contextual evidence for the patient.

## 3. Duplicate classification

The classifier runs before recurrence evaluation. It never deletes, edits or
hides a record. Rule version `duplicate-rules/1.1.0` distinguishes:

| Kind | Meaning | Confidence |
| --- | --- | --- |
| `source_revision` | The same source record was returned again or updated. The latest revision is kept; the rest are recorded. | `exact` |
| `shared_parent_tests` | Several tests belong to one verified encounter | `verified` |
| `near_duplicate` | Distinct encounters, same patient, same local day, same facility, with every compared field that is present on both sides agreeing | `probable` |
| `same_day_distinct` | Distinct encounters for the same patient on the same local day that do not meet the near-duplicate rule, including across facilities | `possible` |

Compared fields for `near_duplicate` are facility, confirmed-positive outcome,
test methods and diagnoses. **A field missing on either side is recorded as
`missing` and is not evidence of similarity.** At least two fields must match
and none may differ. Cross-facility same-day records are never called near
duplicates, and they are never asserted to be erroneous.

### Representative selection

When a same-day group contributes one positive, the representative is chosen
deterministically:

1. the earliest timestamp, when every member carries timestamp precision;
2. otherwise, or on a tie, the lexicographically smallest encounter key.

The source records are unchanged. The choice is recorded against every member.

### Same-day policy

| Policy | Counting | Review |
| --- | --- | --- |
| `exclude` | At most one representative positive per patient per local day | All records remain visible |
| `review_separately` | As `exclude` | The group enters the review queue, and the patient carries an unresolved `POSSIBLE_DUPLICATE` flag |
| `include` | Distinct encounters on the same day may each count | Exact source repeats and tests of one verified encounter still count once |

`include` does not override the minimum gap. A same-day pair can only chain
when `minimum_gap_days = 0`.

## 4. The sequence rule

Definition fields: `minimum_gap_days`, `maximum_window_days`,
`minimum_positive_encounters` (N), `same_day_policy`,
`permitted_positive_methods`, `require_linked_treatment`, `quality_exclusions`,
`interval_bands`, `duplicate_rule_version`, `engine_version`.

Validation: `minimum_gap_days >= 0`, `maximum_window_days >= minimum_gap_days`,
`N >= 2`, `period_end >= period_start`.

Resource ceilings are a separate check, `RecurrenceCeilings.check()`. Its
default limits are a 366-day window, 12 positives and a 366-day period, and they
are meant to come from settings rather than being hard-coded in the interface.
**Not yet wired.** No setting supplies these limits, and no caller runs the
check. That arrives with the analysis-run service.

Semantics:

- Only confirmed positive encounters contribute. A negative or unmapped visit is
  context and **cannot bridge a window**.
- The minimum gap is **inclusive** between consecutive selected positives.
- The maximum window is **inclusive and anchored**: it runs from the first
  selected positive to the last selected positive. It is not a rolling window
  that stretches with every attendance.
- A qualifying chain holds at least N distinct selected positives.
- **Every positive is tried as an anchor.** A patient can qualify on a later
  chain even when an earlier positive is too distant.
- With `require_linked_treatment`, every non-final member of a chain must carry
  a linked medicine. This is the "post-treatment repeat-positive" question.

Worked examples, with `min = 7`, `max = 28`, `N = 2` and positives on day 0:

| Second positive | Result | Why |
| --- | --- | --- |
| Day 3 | Does not qualify against day 0 | Gap 3 is below the minimum 7 |
| Day 12 | Qualifies | Gap 12 is within 7..28 |
| Day 35 | Does not qualify against day 0 | Span 35 exceeds 28, but it may still qualify against another anchor |

For days 0, 20 and 40 with `max = 28` and `N = 3`, there is **no** three-positive
chain. Each adjacent gap is short enough, but no anchored 28-day window holds
all three.

### Algorithm

For each anchor `i`, a longest-chain table over the sorted positives inside
`[day(i), day(i) + max]` is filled left to right. A two-pointer prefix maximum
gives each position its best permitted predecessor, one whose gap is at least
the minimum. The cost is `O(P^2)` per patient in the number of positives. There
is no subset enumeration.

### Deterministic witness

Among all qualifying `(anchor, final)` pairs whose final positive falls inside
the reporting period, the engine chooses:

1. the longest chain;
2. then the earliest final date;
3. then the earliest anchor date;
4. then the lowest position.

It then backtracks choosing the latest permitted predecessor. The patient's full
history, including positives the chain skipped, is always kept.

### Intervals

Intervals are named so the old ambiguous `interval_days` field is no longer the
only reading:

| Field | Meaning |
| --- | --- |
| `adjacent_intervals` | Consecutive eligible positives: P1→P2, P2→P3, ... |
| `anchor_intervals` | First eligible positive to each later positive: P1→P2, P1→P3, ... |
| `chain_interval` | First to last positive of the qualifying witness chain |

For positives on days 0, 9 and 23: adjacent 9 and 14, anchor 9 and 23, and a
chain interval of 23 when all three are selected. A negative visit between them
changes none of these.

**Compatibility.** The legacy `interval_days` field is kept for existing
consumers. It carries `chain_interval.days` for a qualifying patient. Otherwise
it carries the latest adjacent interval, or null with fewer than two eligible
positives. New consumers read the named fields.

## 5. Attribution, lookback and determination

A qualifying chain is attributed to the reporting period that contains its
**final** positive. Evidence is required back to
`lookback_start = period_start - maximum_window_days`.

| Determination | Meaning |
| --- | --- |
| `qualifies` | A qualifying chain ends in the period |
| `does_not_qualify` | The patient has an eligible positive in the period, the evidence is complete, and no chain qualifies |
| `indeterminate` | No chain was found, but absence is not reliable |
| `not_in_period` | No eligible positive falls in the period; not part of the denominator |

A patient is `indeterminate` rather than `does_not_qualify` when any of these
holds:

- retrieval coverage is incomplete;
- the observed evidence starts after `day(p) - maximum_window_days` for one of
  their in-period positives;
- a malaria test in the relevant window has an unmapped or unreturned result.

**Incompletely observed lookback is never reported as a reliable absence of
recurrence.** A qualification found in partial evidence is still a real
observed chain.

## 6. Counting units and denominators

- **Qualifying patients.** Distinct linked patients whose chain qualifies; each
  person counts once.
- **Denominator.** Distinct linked patients with an eligible positive in the
  reporting period, under the same filters and coverage. The numerator is always
  a subset of it. Both are shown. **A repeat-positive proportion is not a
  resistance rate or a treatment-failure rate.**
- **Facility attribution.** The qualifying chain's final-positive facility. A
  facility's denominator counts patients with an eligible in-period positive
  there. A patient seen at several facilities appears in each denominator.
  Facility counts therefore **do not sum** to the district unique-patient total.
- **Weekly trend.** A qualifying patient is placed in the ISO week, Monday
  start, of their final positive. Weekly counts need not sum to the period total
  when a period spans part-weeks.
- **Frequency.** Qualifying patients by witness chain length, in bands of 2, 3
  and 4 or more.
- **Interval distributions.** Two views over the same cohort. *Observed
  adjacent intervals* come before thresholds, so a seven-day minimum does not
  imply shorter intervals are absent from the evidence. *Qualifying chain
  intervals* come after thresholds.
- **Treatment matrix.** Cells count **linked positive-to-positive transitions**
  within qualifying chains, not patients. Rows are the medicine linked to the
  earlier positive, or `not linked` or `not recorded`. Columns are interval
  bands. Prescribed and dispensed evidence are counted separately. Label:
  "Repeat-positive after recorded treatment", never a drug resistance rate.

Interval bands must be ordered, non-overlapping and non-negative. Only the last
band may be open-ended. A value in a gap between configured bands goes to an
explicit `outside configured bands` bucket; it is never dropped.

## 7. Filters

| Filter | Applies to |
| --- | --- |
| Reporting period | Qualifying final positive; denominator in-period positive |
| Facility / subcounty / district | Attribution facility for the numerator; in-period positive facility for the denominator |
| Sex, age group | Patient cohort, from the latest in-period positive encounter; unknown stays an explicit category |
| Quality status | Patient cohort |
| Permitted test methods | The definition itself, because it changes the sequence |

Context encounters are never removed before the sequence is reconstructed.

## 8. Quality flags

`VALID`, `POSSIBLE_DUPLICATE`, `INCOMPLETE_TEST_EVIDENCE`,
`INCOMPLETE_TREATMENT_EVIDENCE`, `IDENTITY_LINKAGE_UNCERTAIN` and
`DATE_QUALITY_ISSUE`. `VALID` never coexists with another flag. Missing
treatment does not erase a confirmed positive. It flags
`INCOMPLETE_TREATMENT_EVIDENCE` and can make a treatment-response question
ineligible.

## 9. Exploratory preset

**Exploratory starting preset (not an approved programme method):**
`minimum_gap_days = 7`, `maximum_window_days = 28`, `N = 2`,
`same_day_policy = exclude`. The values come from the illustrative examples in
the enhancement brief. Programme mode uses an actually active governed method
and says so precisely when none is active.
