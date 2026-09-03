# The indicator registry

Every number MARS publishes traces to a definition here. That is the point: a
positivity rate quoted in a dashboard, a report and an investigation packet must
be the same quantity computed the same way, and the only way to guarantee it is
for all three to read one definition rather than three implementations.

## Registration is not approval

A definition ships as a **draft**. Registering it and putting it in force are
different acts, and only the first is MARS's.

```
draft → in_review → approved → active → retired
```

`IndicatorRegistryService.seed_catalogue()` registers the shipped catalogue as
drafts. It is idempotent, and deliberately conservative about anything a person
has touched: an approved or active version is never demoted, and a changed
specification creates a **new** draft beside the old one rather than editing a
version that may already have produced figures somebody acted on.

Until a programme approves a version, `active_version` is `null` and the
aggregation engine computes nothing for that code. That is the correct
behaviour, not a gap to work around: publishing a figure computed by rules
nobody signed would be worse than publishing none.

## No threshold lives in the registry

A definition says **how to compute a figure**. What counts as too high is a
programme decision, held in the configuration registry and absent until
approved. A definition that shipped with a threshold would make every consumer
inherit a judgement nobody signed.

The catalogue tests enforce this: no shipped specification may contain the words
`threshold`, `target`, `alert_level`, `cutoff`, `trigger_at`, `severity` or
`must_exceed`. The one numeric constant in the catalogue —
`ENC_REPEAT_POSITIVE_INPUT`'s `minimum_occurrences: 2` — is named for what it is
(what "more than one" means) rather than as a `threshold`, precisely so the
guard can stay strict.

## What ships

Fifteen definitions, each citing the printed field or canonical column it comes
from.

| Code | Unit | Source |
| --- | --- | --- |
| `ENC_ATTENDANCE_TOTAL` | count | OPD 002 encounters |
| `ENC_SUSPECTED_MALARIA` | count | OPD 002 fever; HMIS 105 EP01a |
| `ENC_TESTED_MALARIA` | count | OPD 002 tests; HMIS 105 EP01b |
| `ENC_CONFIRMED_MALARIA` | count | OPD 002 results; HMIS 105 EP01c; 033b MA. |
| `ENC_TEST_POSITIVITY` | proportion | confirmed ÷ tested |
| `ENC_ANTIMALARIAL_TREATED` | count | OPD 002 prescriptions |
| `ENC_REPEAT_POSITIVE_INPUT` | count | linked encounters with ≥2 positives |
| `AGG105_CONFIRMED_MALARIA` | count | HMIS 105 EP01c, **as reported** |
| `AGG105_TESTED_MALARIA` | count | HMIS 105 EP01b, **as reported** |
| `AGG105_PRESUMPTIVE_TREATED` | count | HMIS 105 EP01e − EP01d |
| `AGG033B_NEGATIVE_TREATED` | count | HMIS 033b section 5 negative-treated columns |
| `LAB105_MALARIA_TESTS_DONE` | count | HMIS 105 section 10.2.1 PS01 + PS02 |
| `COM_RDT_DAYS_OUT_OF_STOCK` | days | HMIS 105 6.1 SS34 |
| `COM_AL_DAYS_OUT_OF_STOCK` | days | HMIS 105 6.1 SS01 |
| `RPT_COMPLETENESS` | proportion | accepted submissions ÷ active facilities |

## Reported and derived stay apart

`ENC_CONFIRMED_MALARIA` and `AGG105_CONFIRMED_MALARIA` are the same clinical
quantity measured two ways: once from the e-register MARS holds, once from what
the facility reported on paper. They have **different codes and different source
domains** so they can never be added together — adding them double-counts — and
so a disagreement between them stays visible. Where they differ, the difference
is the finding, and the Prompt 11 reconciliation records it.

## The rules the engine applies

**An undefined denominator produces no value.** Never zero. A positivity of 0.0
and a positivity that could not be computed look identical in a chart and are
opposite statements about a facility. The result carries
`value_status = unavailable_no_denominator`, and a database constraint
(`value_present_iff_available`) makes it impossible to store a value alongside a
non-available status.

**A blank input stays missing.** It does not contribute to a sum, and the count
of blanks travels with the result — a district total from four reporting
facilities is distinguishable from one from forty.

**Only the latest accepted source revision counts.** A superseded aggregate
submission is history. Summing every revision would count a corrected month
twice.

**Rollups recompute; they never average.** A district proportion is recomputed
from summed numerators and denominators. Averaging facility proportions weights
a clinic that tested four people the same as a hospital that tested four
hundred, and produces a district figure no facility reported and nobody can
reproduce.

**Rollups go upward only.** Facility → subcounty → district → national. A figure
a facility reported as a total is never split into detail the facility never
supplied.

## Results are immutable

`indicator_result` rows are keyed by definition version, grain, period,
dimensions **and** input fingerprint. Recomputing over unchanged inputs finds
the existing row and writes nothing; changed inputs write a new row beside the
old one. Nothing is edited in place, because a district acted on the figure that
was there.

Every result carries: definition version, input fingerprint, source cutoff,
boundary version, engine version, computed-at, contributing and expected unit
counts, missing-input count, and a quality context explaining any exclusion.

## API

| Endpoint | Permission |
| --- | --- |
| `GET /api/v1/indicators/definitions` | `method:view` |
| `GET /api/v1/indicators/definitions/{code}` | `method:view` |
| `GET /api/v1/indicators/summary` | `surveillance:view_aggregate` |

Understanding a definition is not the same as seeing a district's figures, so
they carry different permissions.

A geography unit outside the caller's scope is **refused** by the same scope
check the geography endpoints use — not filtered out. A caller who can tell "no
data" from "not yours" by watching a list length has been told something the
scope exists to withhold. The refusal is a 404, because a 403 confirms the unit
exists.

**The frontend receives final values.** No positivity, rollup or recurrence is
computed in a browser.

## Worker

`indicator.materialise` recomputes facility-grain values for a period. Safe to
run repeatedly. It reports which codes it **skipped because they are not
approved**, so an operator can see exactly what is waiting on the programme.

## What the programme still has to supply

| Input | Needed for |
| --- | --- |
| Approval of each definition version | Any figure at all |
| Alert thresholds and targets | Prompts 18 and 21; deliberately absent here |
| Population denominators | Incidence; MARS computes counts and proportions only |
| Which facilities are expected to report in a period | A completeness denominator better than the active facility master |
