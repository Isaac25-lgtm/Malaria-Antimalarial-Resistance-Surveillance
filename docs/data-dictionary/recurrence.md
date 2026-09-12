# Recurrence surveillance

Counts of observed patterns. Nothing here is a clinical outcome.

## What these figures are not

**Not treatment failure. Not recrudescence. Not reinfection. Not resistance.**
Establishing any of those requires knowing whether the parasite persisted,
whether the patient took the drug, and what the parasite's genotype was. An
e-register knows none of them.

A repeat positive is a reason to look.

Every row carries this in `interpretation_context.interpretation_limit` — on the
row, not added by a presentation layer, because a figure that reaches a report
without it is a figure someone will over-read.

## Measures

| Measure | Counts |
| --- | --- |
| `repeat_positive_patients` | Linked patients with ≥2 positives in an episode |
| `repeat_positive_episodes` | Episodes containing ≥2 positives |
| `patients_with_multiple_episodes` | Patients with more than one distinct episode |
| `repeat_positive_proportion` | Repeat-positive patients ÷ linked patients with any positive |
| `interval_band_count` | Return intervals falling in a governed band |

## Facility and residence never merge

`scope_kind` is `facility`, `residence_district` or `residence_subcounty`, and
the same evidence is counted separately into each.

A patient may attend a clinic outside their own district. Merging the two
attributes a pattern to the wrong place, and the questions differ: a facility
concentration points at a clinic, a residence concentration points at a village.

An episode whose residence did not resolve contributes to facility measures only,
and `residence_unresolved_episodes` says how many.

## Every row carries its denominator and its exclusions

| Column | Why |
| --- | --- |
| `eligible_patients` | The population recurrence is measured *within* |
| `excluded_unlinked_encounters` | Patients MARS could not follow. Their absence always makes recurrence look **rarer** than it is |
| `positives_without_treatment_record` | The ordinary explanation for a repeat positive, and the first thing to rule out |
| `residence_unresolved_episodes` | Why a residence figure may be smaller than its facility counterpart |

A proportion with no eligible population is **unavailable**, never zero.
Reporting 0.0 would put a real-looking "no recurrence here" into every district
summary. A check constraint enforces it.

## Interval bands are governed, not shipped

MARS records **actual return intervals in days** on `episode_member`. What
counts as an early or late return is a clinical judgement the programme
approves.

```
Required: an active ConfigurationVersion for
  key   = recurrence_interval_bands_days
  value = { "bands": [ { "label": ..., "lower_days": ..., "upper_days": ... }, ... ] }
```

With no approved bands, the engine reports every other measure and marks the
band breakdown unavailable, saying so in the run's notes. It does not choose cut
points. A configuration whose bands are empty or malformed is treated as
**absent**, not repaired.

Only intervals **between positive results** are banded. An interval measured from
a negative follow-up visit is a return interval for something else, and mixing
them makes the distribution unreadable.

## Results are immutable

Keyed by episode build, measure, scope, period, band and an input fingerprint
that includes the episodes **and** the bands. The same episodes banded
differently are a different result: overwriting would change what a district was
shown with no record of it.

Every row records `episode_rule_version_id` — recurrence read under a 28-day
window is a different quantity from recurrence read under 42.

## Worker

`recurrence.compute` — reads the latest **completed** episode build for a
period. A `not_configured` build has no episodes, and computing from it would
report a confident zero for every facility.

## What the programme still has to supply

| Input | Needed for |
| --- | --- |
| An approved `malaria_episode_rule` | Any episode, therefore any recurrence figure |
| An approved `recurrence_interval_bands_days` | The interval-band breakdown |

## Two recurrence methods, and which one a figure came from

The measures above are produced by the **episode-based recurrence engine**
(`analytics/recurrence.py`, engine `1.0.0`). It counts positives *within an
episode*, and an episode groups encounters by the gap between successive
attendances. Its stored results keep that meaning and their method version, and
nothing reinterprets them.

Since 11 September 2026 the live snapshot and the patient-surveillance views use
a **different method**: the shared positive-to-positive engine
(`analytics/positive_recurrence.py`, engine `positive-recurrence/1.1.0`),
specified in [configurable recurrence](../methods/configurable-recurrence.md).
Its unit is a confirmed positive encounter, and its window is anchored at a
positive and bounded by a maximum. Duplicates are classified before counting.
Live and stored evidence reach it through adapters that share one canonical
contract, so the two paths cannot disagree about a patient.

The two methods answer different questions and are not interchangeable. A
figure always states which one produced it:

- live snapshot rows carry `repeat_positive_definition` and `determination`;
- episode-based rows carry their `method_version`.

### The old `interval_days`

`interval_days` used to mean first-to-latest positive on the live path and
latest-to-previous on the stored path. It now has **one** documented meaning on
both: the qualifying chain interval when the patient qualifies, otherwise the
latest adjacent interval, and null with fewer than two eligible positives. New
consumers read the named fields instead:

- `chain_interval_days`;
- `adjacent_interval_days`;
- `anchor_interval_days`.
