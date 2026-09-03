# Temporal anomalies and persistence

One period's observation, judged against the baseline built for it — and how
long each departure has been running.

| Table | Holds |
| --- | --- |
| `anomaly_build` | One detection run, or its refusal |
| `temporal_anomaly_result` | One observation judged against one baseline |
| `anomaly_persistence` | An unbroken run of flagged periods |

## Quiet is not the same as quiet

This is the property the module exists to protect. A map with no flags can mean
two opposite things, and a surveillance system that stores the second as the
first is quietly useless while looking identical from the outside.

| Outcome | Means |
| --- | --- |
| `flagged` | Evaluated; departure exceeded the approved threshold |
| `not_flagged` | Evaluated; departure did not |
| `not_evaluated_no_observation` | The source reported no usable value |
| `not_evaluated_no_baseline` | No baseline with sufficient history |
| `not_evaluated_below_minimum_count` | Fewer cases than the approved minimum |
| `not_evaluated_count_unknown` | The measure carries no case count at all |
| `not_evaluated_method_inapplicable` | The approved method cannot be applied to this baseline |

Enforced by `not_flagged_means_evaluated`: a row may say `not_flagged` only if
it carries a deviation **and** cites the baseline result that produced it. Any
future code path writing to the table is held to the same rule.

Every unevaluated row keeps its reason in `notes`. None is a silent skip.

## MARS does not decide how large a departure has to be

That number decides how many districts get an alert on Monday morning.

The governed rule is `temporal_anomaly_rule`, of kind `signal_rule`.

| Parameter | Required | Meaning |
| --- | --- | --- |
| `detection_method` | Yes | One of the three implemented methods |
| `deviation_threshold` | Yes | How large a departure has to be, in the method's own units |
| `minimum_case_count` | Yes | Fewest cases an observation must carry to be judged at all |
| `persistence_periods` | No | How many consecutive flagged periods make a run *sustained* |

With no approved rule the run is stored as `not_configured` with the missing
parameter names, and nothing is judged. `latest_detection()` returns only
**completed** runs, so a caller cannot read "no flags" off a run that judged
nothing.

## Methods, and no fallback

| Method | Compares | Needs |
| --- | --- | --- |
| `robust_z_score` | Deviation in units of the baseline's own spread | A non-zero dispersion |
| `relative_deviation` | Deviation as a proportion of the expected level | A non-zero expected level |
| `exceeds_uncertainty_band` | Observation outside the baseline's band | An approved uncertainty multiplier |

When the approved method cannot be applied — a z-score against a baseline with
no spread, a relative deviation from an expected zero, a band test with no band
— the observation is recorded as `not_evaluated_method_inapplicable` with the
reason.

**MARS does not substitute another method.** Falling back would apply a rule
nobody approved to a real district, and the substitution would be invisible in
the output.

The arithmetic is still recorded on those rows. Only the judgement is withheld.

## What a flagged row carries

`a_flag_carries_its_evidence` requires a flag to cite the baseline result, the
method version that judged it, and the threshold applied.

| Column | Why |
| --- | --- |
| `observed_value`, `expected_value` | The comparison itself |
| `absolute_deviation` | Trivial at 0.8, dramatic at 0.02 |
| `relative_deviation` | Trivial at two cases, serious at two hundred |
| `deviation_score` | Deviation in units of the baseline's spread; null when there is none |
| `uncertainty_lower`, `uncertainty_upper` | The band, when the baseline had one |
| `deviation_threshold` | Copied onto the row, so a later rule change cannot rewrite what a past detection meant |
| `case_count`, `minimum_case_count` | What it was judged on, and what it was held to |
| `history_periods_used` | A flag against four periods and one against twenty-four are different claims |
| `direction` | A rise may be transmission; a fall may be a testing collapse |

The threshold is stored, not joined. A rule revised in November must not change
what a district was told in September.

## Persistence: counting is not labelling

A one-period spike and a six-month rise need different responses, and
presenting them identically is how alert fatigue starts. But deciding where the
line falls is a programme decision.

| Column | Kind |
| --- | --- |
| `consecutive_periods` | **Arithmetic.** Always recorded |
| `is_sustained` | **Judgement.** Null until `persistence_periods` is approved |

Enforced by `sustained_requires_configuration`.

A run is identified by (series, scope, `first_period_start`). A flag extends
the run whose `last_period_end` is exactly the preceding period; otherwise it
opens a new one. Re-running detection over a period already in the run changes
nothing, so a re-run cannot make a spike look sustained.

A persistence row is a running tally over immutable results, not an analytical
claim that gets rewritten: extending it only ever moves the end forward and
appends to `contributing_result_ids`. Nothing already recorded changes meaning.

## What a flag is not

Every row carries, in `quality_context.interpretation_limit`:

> A departure from this series' own history, larger than an approved threshold.
> It is a reason to look, not a finding. It does not establish a cause, and it
> is not evidence of treatment failure or antimalarial resistance.

A flagged testing-measure period may reflect a stock-out, a new clinician, a
transcription change, a referral pattern, or transmission. The engine
distinguishes none of them, and does not pretend to.

## Superseded observations

Results elsewhere in MARS are immutable, so one period can hold several rows
for the same scope. Detection judges the latest `computed_at` only. Flagging a
figure nobody is looking at wastes an investigation.

## Limitations

* EWMA and CUSUM-style surveillance (blueprint 035) are not implemented. Both
  carry their own governed parameters and belong with the methods a programme
  can select; adding them is a matter of extending
  `AnomalyDetectionMethod` and `_apply`.
* Detection runs at the grain the source results were written at. Rolling a
  facility series up to a district before detection is the spatial engine's
  concern, not this one's.
* `case_count` is read from the source measure's numerator. A measure with no
  numerator cannot be held to a minimum case count, and says so rather than
  being judged anyway.
