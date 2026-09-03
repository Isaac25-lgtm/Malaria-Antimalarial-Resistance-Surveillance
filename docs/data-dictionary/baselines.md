# Historical baselines

What a series usually looks like here. Nothing in MARS can call something
unusual until this has an answer, and MARS does not decide the answer itself.

| Table | Holds |
| --- | --- |
| `baseline_build` | One run: the governed method, the history asked for, or what is missing |
| `baseline_result` | One expected level, and the history behind it |

## MARS does not decide what normal means

How far back to look, how much of that history must be present, and which
summary to use are surveillance decisions with real consequences. A short
window makes a slow rise invisible. A long one makes a genuine change take a
season to surface. MARS implements the methods and declines to choose among
them.

The governed method is `historical_baseline`, of kind `temporal_baseline`.

| Parameter | Required | Meaning |
| --- | --- | --- |
| `baseline_method` | Yes | One of the three implemented methods |
| `history_periods` | Yes | How many comparable periods to look back over |
| `minimum_history_periods` | Yes | Fewest usable periods that still produce an expectation |
| `minimum_completeness` | Yes | Proportion of the window that must carry a usable value |
| `uncertainty_multiplier` | No | Half-width of the band, in dispersion units |

**None of these is shipped with a value.** With no approved method version the
run is stored as `not_configured`, the missing parameter names go into
`missing_configuration`, and no expected values are produced. That is the
expected state of a fresh deployment — and it is a statement about
configuration, not a statement that nothing is unusual.

An active version is treated as absent when it is incomplete, names a method
MARS has not implemented, or carries a minimum larger than its own window. Each
of those is reported as missing rather than repaired, because repairing it
would mean choosing the parameter.

## Methods

| Method | Centre | Dispersion |
| --- | --- | --- |
| `historical_median` | Median of the most recent comparable periods | Median absolute deviation |
| `historical_mean` | Mean of the same | Standard deviation |
| `seasonal_period_of_year_median` | Median of the same period-of-year across previous years | Median absolute deviation |

Centre and dispersion are paired, not chosen separately: a robust centre
reported with a fragile spread would understate how variable a series is at
exactly the moments that matter.

The seasonal method exists because malaria in Uganda is seasonal. Comparing
March against February flags the season rather than an event.

### Comparable periods

A monthly baseline walks back **calendar months**, not thirty-day blocks: a
calendar month is what the source reports, and a thirty-day block would
straddle two of them. A weekly baseline walks back seven days at a time and
stays Monday-aligned.

The seasonal method takes the same month, or the same ISO week, in each
previous year. A year with no ISO week 53, or a 29 February in a common year,
is **recorded** in `excluded_periods` rather than skipped: silently dropping it
would shorten the history behind an expectation without saying so.

The target period is never part of its own history.

## Sufficiency

| Sufficiency | Meaning | Expected value |
| --- | --- | --- |
| `sufficient` | Enough usable periods, enough completeness | Present |
| `insufficient_history` | Fewer usable periods than the approved minimum | **Absent** |
| `insufficient_completeness` | Enough periods, too few carrying a value | **Absent** |
| `no_history` | No comparable period carried a usable value | **Absent** |

Enforced by `sufficiency_matches_value_status`, so any code path writing to the
table is held to it. An expectation drawn from two periods is worse than none,
because a district can act on it.

Every row records `history_periods_available`, `history_periods_used` and
`history_periods_required`, because "8 of 12, needed 6" and "8 of 8, needed 6"
describe different confidence in the same number.

## Blank, zero, and unavailable in history

A period reported as **zero** counts towards a baseline. Zero positives out of
forty tests is a figure, and treating it as a gap would make an outbreak look
like a return to normal.

A period whose value is **unavailable** — no denominator, insufficient data —
does not count, and appears in `excluded_periods` with its status as the
reason. A baseline that silently drops half its history cannot be audited.

A period that is **absent** appears there too, as `period_absent`.

## Uncertainty bands

The band is `expected ± multiplier × dispersion`, and it exists only when a
programme has approved `uncertainty_multiplier`. How wide an interval should be
is a statistical choice MARS does not make on a programme's behalf, so a
baseline without one has a centre and no band.

`band_has_both_ends` refuses half a band: a one-ended band reads as a limit,
which is a different claim.

A single historical period has a centre and **no** spread —
`dispersion_measure` is `none` and `dispersion_value` is null. Recording that
spread as zero would make the series look perfectly stable.

## Series

| Series kind | Source | `series_key` |
| --- | --- | --- |
| `indicator` | `indicator_result` | Indicator code |
| `testing_measure` | `testing_surveillance_result` | Testing measure |
| `treatment_measure` | `treatment_surveillance_result` | Treatment measure |

Kept explicit rather than inferred from the key, because the same word can name
an indicator and a measure, and a baseline built from the wrong table would
compare a facility against a history that is not its own.

## Superseded figures do not vote twice

Results elsewhere in MARS are immutable: a recomputation writes a new row
beside the old one. A period can therefore hold several rows, and the baseline
engine reads the latest `computed_at` per period. Counting both would let a
corrected figure and the figure it corrected each shape the expectation.

## Refusals are records

`baseline_build` exists so that "no baselines were produced" is a row an
operator can read, not an absence they have to interpret.

`refusals_name_what_is_missing` requires a `not_configured` build to carry a
`missing_configuration` **object**. It tests `jsonb_typeof` as well as nullity,
because a JSONB column given a Python `None` is stored as JSON `null` rather
than SQL NULL — and a refusal naming nothing would otherwise pass the ordinary
null test.

`completed_builds_carry_their_method` is the other half: a run cannot report
expected values without recording the method that produced them.

`latest_build()` returns only **completed** builds. A `not_configured` build
has no baselines, and offering it would let a caller compare against nothing
and report the result as an expectation.

## What a baseline is not

Every row carries, in `quality_context.domain_limit`:

> An expected level derived from this series' own history. It describes what
> has been usual here, not what should be.

A facility with a persistently high positivity has a high baseline. That is a
description of its past, not an endorsement of it, and a deviation from it is
not the only thing worth investigating.

## Limitations

* Baselines are computed per series and per geography scope as those appear in
  the source table. A scope absent from history produces no row at all rather
  than an empty one.
* "New facilities or sparse geographies use simpler rules and wider
  uncertainty" (blueprint 035) is **not** implemented as an automatic fallback.
  Choosing a simpler rule is itself a governed decision; such facilities get an
  explicit `insufficient_history` or `no_history` row instead.
* EWMA and CUSUM-style surveillance are detection methods rather than
  baselines, and belong to the anomaly engine.
