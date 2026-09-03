# Geographic aggregation, hotspots and map privacy

A malaria map is the output most likely to be believed and least likely to be
questioned. Most of what follows exists because of that.

| Table / service | Holds |
| --- | --- |
| `spatial_run` | One aggregation or hotspot run, or its refusal |
| `geographic_aggregation_result` | One measure for one administrative unit |
| `hotspot_result` | One area evaluated against a governed definition |
| `mars.services.spatial_availability` | What a map cell is allowed to say |

## Recompute, never average

A district's positivity is its positives divided by its tests — **not** the
mean of its facilities' rates.

The two differ whenever facilities are unequal in size, and they are always
unequal in size. Averaging gives a clinic with four tests the same voice as a
hospital with four hundred, which is how a rural district acquires an alarming
rate it does not have.

Worked from the test fixture: a hospital at 30/100 and a clinic at 2/4 make a
district of **32/104 = 0.3077**. The mean of the two rates would be 0.40.

`a_rate_needs_a_denominator` refuses a value whose denominator is zero, and
`not_a_facility_grain` keeps the table to administrative units — mapping
patient-derived figures onto facility points is what blueprint 036 forbids
outright.

## Where care was given is not where people live

| Basis | Counts by | Points at |
| --- | --- | --- |
| `facility_location` | The reporting facility's district or subcounty | A clinic |
| `residence` | The patient's recorded residence | A village |

A patient may attend a clinic outside their own district. Both roll-ups are
useful and they answer different questions, so `aggregation_basis` is on every
row and nothing can sum the two together.

Residence figures are **recomputed from encounters**, because a facility's
figures carry no residence. Only measures an encounter can be counted into are
produced this way — `testing_coverage` and `test_positivity`. A series that
cannot be counted by residence is named in the run report rather than silently
omitted.

`unresolved_contributions` counts encounters whose residence never resolved to
a unit. Their absence always makes a residence map look emptier than the truth.

## How much of the area reported

Every row carries `contributing_facilities`, `expected_facilities` and
`reporting_completeness`, with `contributors_within_expected` between the first
two. The expected count comes from the facility register, not from who
reported — the facilities that did not report are exactly the ones a
completeness figure exists to reveal.

A district figure built from three of its twenty facilities is not a district
figure, and a reader who cannot see that will treat it as one.

## A hotspot must have a method

Blueprint 037: *a hotspot must have a method, not just a red colour.*

The governed definition is `hotspot_definition`, of kind `spatial_method`.

| Parameter | Required | Meaning |
| --- | --- | --- |
| `detection_method` | Yes | Which departure test to apply |
| `deviation_threshold` | Yes | How large a departure has to be |
| `minimum_case_count` | Yes | Fewest cases an area must carry to be judged |
| `minimum_completeness` | Yes | How much of the area must have reported |
| `persistence_periods` | No | How many consecutive periods make a hotspot *persistent* |

Metric, geography and time window come from the run itself; the baseline comes
from the approved temporal baseline method. Without **both** an approved
definition and an approved baseline method, the run is stored as
`not_configured` naming every missing parameter, and no area is judged.

`a_hotspot_carries_its_method` requires two governed versions on the row —
`method_version_id` for the definition that called it a hotspot, and
`baseline_method_version_id` for the method that produced the expectation.
They are separate decisions and separate columns.

The threshold, minimum case count and required completeness are copied onto the
row. A definition revised in November must not rewrite what a map meant in
September.

### The expectation comes from the area's own history

Not from its facilities' histories — a district compared against a quantity
nobody reports. Not from last month — a district compared against the season.
The area's own series in `geographic_aggregation_result`, summarised under the
approved baseline method, is the comparison that means something.

This implies the aggregation must have been run for the historical periods
before hotspots can be evaluated. A deployment that has aggregated one month
finds every area reported as having no baseline, which is the correct answer
rather than a fault.

### Not a hotspot means examined

| Outcome | Means |
| --- | --- |
| `hotspot` | Examined; departure exceeded the approved threshold |
| `not_hotspot` | Examined; departure did not |
| `not_evaluated_no_observation` | The area has no usable figure |
| `not_evaluated_no_baseline` | Not enough of the area's own history, or the method cannot be applied to it |
| `not_evaluated_below_minimum_count` | Fewer cases than the approved minimum |
| `not_evaluated_incomplete_reporting` | Too little of the area reported |

Enforced by `not_hotspot_means_examined`. A red-free map is worth nothing if it
cannot say which areas were looked at.

Completeness is a gate, not a footnote: a figure built from part of an area
does not describe the area, and colouring it red or green would both mislead.

### Persistence

`consecutive_periods` counts; `is_persistent` labels, and stays null until a
programme approves `persistence_periods` (`persistent_requires_configuration`).
`first_detected_period_start` and `last_detected_period_end` complete what
blueprint 037 asks for.

The count is read from the previous period's row rather than kept as a mutable
tally, so nothing already written changes meaning.

## Map privacy: refusal is structured, not silent, and not total

A blank cell on a malaria map is read as *no malaria here*. It almost never
means that.

| Cell status | Means |
| --- | --- |
| `available` | A value, **including zero** |
| `missing` | The unit reported nothing for the period |
| `suppressed` | A real value withheld because the cell is too small |
| `unavailable` | Computed, but the measure has no defined value here |
| `not_configured` | MARS refuses this detail; no approved privacy policy |
| `outside_scope` | Outside the requesting principal's authorised geography |

Six situations, one colour, and only one of them is good news. They are kept
apart all the way to the caller.

### The governed policy

`spatial_privacy_policy`, a configuration key registered by governance and
**shipped with no values**.

| Key | Meaning |
| --- | --- |
| `minimum_cell_count` | Smallest patient-derived cell that may be shown |
| `minimum_aggregation_level` | Finest geography permitted for patient-derived output |

Without both, a patient-derived layer is refused:

```json
{
  "status": "not_configured",
  "reason": "privacy_configuration_required",
  "missing_configuration": ["minimum_aggregation_level", "minimum_cell_count"],
  "highest_safe_geography": null,
  "note": "…This is a statement about configuration, not about malaria…"
}
```

There is **no `cells` key at all** — not even an empty list. An empty map is
read as an absence of disease, which is precisely the claim MARS must not make.

`highest_safe_geography` is reported when it is determinable without inventing
anything: if a programme has approved `minimum_aggregation_level` but not
`minimum_cell_count`, the level is named. With no policy at all it is `null`,
because MARS has no basis for choosing one.

Requesting a grain finer than the approved minimum is refused separately, with
`reason: geography_finer_than_approved_minimum` and the approved level as
`highest_safe_geography` — the coarser layer is available.

### What is not gated

Base geography, boundaries, hierarchy and navigation, and facility metadata
already permitted by scope are served by the geography services and never pass
through this gate. It covers one thing: analytic layers built from patient
encounters.

Every series kind MARS currently aggregates is patient-derived, so the gate
always applies to this service. `PATIENT_DERIVED_SERIES` is written out
explicitly so that a later operational layer — commodity stock conditions, say,
which are a fact about a store rather than about a person — is not swept into
it by default.

### Suppression protects people, not zeroes

A cell counting nobody has nobody in it to identify. Suppression therefore
applies to `0 < numerator < minimum_cell_count`.

Withholding zeroes would hide exactly the districts with no malaria — the good
news a programme most needs — and would make a genuine zero indistinguishable
from a withheld figure, which is the confusion this module exists to prevent.

A cell whose numerator is unknown is suppressed with `reason:
cell_size_unknown`: a value whose cell size cannot be checked is not assumed
safe.

### Scope

`authorised_paths` are the materialised paths of the principal's geography
scopes; `None` means national scope.

Units outside scope are returned as `outside_scope` rather than blanked: the
district's existence is public geography, its figure is not.

Naming a specific out-of-scope unit in `requested_unit_ids` raises
`GeographyScopeDeniedError` — rejected, not filtered, so a caller learns their
request was refused rather than quietly receiving less than they asked for.

## What a hotspot is not

Every row carries, in `quality_context.interpretation_limit`:

> An area whose figure departed from its own history by more than an approved
> threshold. It is an area worth visiting, not a diagnosis, not an outbreak
> declaration, and not evidence of antimalarial resistance.

## Limitations

* **Secondary suppression is not implemented.** Suppressing single small cells
  leaves a differencing risk where a total and its parts are both published.
  Closing it requires a governed rule about which additional cells to withhold,
  and MARS does not invent one.
* **Local clustering, adjacency-based concentration and scan statistics**
  (blueprint 036) are not implemented here; they require validation before use.
* **Parish and village levels** remain unavailable because no parish or village
  boundary data has been supplied. MARS does not fabricate geography.
* Residence aggregation covers `testing_coverage` and `test_positivity`. Other
  measures are facility-level constructions — a facility's
  missing-prescription count has no residence — and are produced on the
  facility-location basis only.
* Population denominators are not available, so incidence per head of
  population cannot be computed. Every rate here has a denominator drawn from
  the reported data itself.
