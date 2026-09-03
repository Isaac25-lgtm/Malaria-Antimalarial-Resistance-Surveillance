# Testing, treatment and commodity surveillance

Three domains, three engines, three evidence shapes. They share a provenance
envelope and nothing else.

| Domain | Engine | Table | Answers |
| --- | --- | --- | --- |
| Testing | `TestingSurveillanceEngine` | `testing_surveillance_result` | What did the facility do with its tests? |
| Treatment | `TreatmentSurveillanceEngine` | `treatment_surveillance_result` | What was prescribed, as recorded? |
| Commodity | `CommoditySurveillanceEngine` | `commodity_stock_fact` | What did the facility report about its stock? |
| Commodity | `CommoditySurveillanceEngine` | `commodity_operational_alert` | Which supply conditions need someone told? |

Each engine can be run alone. The worker `mars.workers.surveillance_compute`
runs all three, commodity first, so the reported stock conditions exist before
testing and treatment look for the supply context that explains a decline.

## What each domain is not

**Testing is not disease.** A fall in confirmed cases alongside a fall in
testing is a testing finding. Calling it an improvement is the commonest way
malaria surveillance misleads itself, so every testing row carries

> Testing practice, not disease burden. A fall in confirmed cases alongside a
> fall in testing is a testing finding.

in `quality_context.domain_limit`, and every testing row carries whatever
diagnostic stock condition overlapped its period in `commodity_context`.

**Treatment is not receipt.** The register records a prescription line. It does
not record dispensing, adherence, or drug quality. Every treatment row carries

> Records what was prescribed. Routine data cannot establish that a patient
> received, took or completed a drug, or that a drug was of adequate quality.

**Commodity is not epidemiology.** A stock-out is a supply-chain observation.
It says nothing about transmission, treatment response or resistance, and the
alert statement says so on the row.

## Testing measures

| Measure | Numerator | Denominator |
| --- | --- | --- |
| `testing_coverage` | Attendances with a test performed | Attendances |
| `rdt_share` | RDT tests | Tests performed |
| `microscopy_share` | Microscopy tests | Tests performed |
| `test_positivity` | Positive results | Tests performed |
| `negative_cases_treated` | Negative results with an antimalarial prescribed | — |
| `untested_cases_treated` | Untested attendances with an antimalarial prescribed | — |
| `testing_volume_change` | Tests this period | Tests in the previous period |
| `missing_result_count` | Tests performed whose result was never recorded | — |

`missing_result_count` is deliberately separate from `untested_encounters`. A
test that was done and never written down is not an untested patient: folding
the two together understates testing effort and overstates the gap.

`testing_volume_change` is produced only when a previous period is supplied,
and it is a ratio with its own denominator so a reader can see what it was
measured against. A previous period with no tests yields **unavailable**, which
is a different statement from "testing did not change".

## Treatment measures

| Measure | Numerator | Denominator |
| --- | --- | --- |
| `confirmed_treated` | Confirmed cases with an antimalarial prescribed | Confirmed cases |
| `confirmed_not_treated` | Confirmed cases with no antimalarial prescribed | — |
| `treated_without_confirmation` | Antimalarials prescribed without a positive test | — |
| `repeat_treatment_episodes` | Reserved for the episode-linked measure | — |
| `missing_treatment_information` | Attendances with no treatment line recorded at all | — |

`missing_treatment_information` is not `confirmed_not_treated`. A facility that
records nothing and a facility that treated nobody are different facts about
that facility, and only one of them is a clinical concern.

## Commodity facts

MARS watches the four commodities HMIS 105 section 6.1 prints:

| Code | Commodity | Bears on |
| --- | --- | --- |
| `SS34` | Malaria Rapid Diagnostic | Testing |
| `SS01` | Artemether/Lumefantrine 20/120mg | Treatment |
| `SS02` | Artesunate 60mg | Treatment |
| `SS24` | Sulfadoxine/Pyrimethamine 500/25mg | Treatment |

Which commodities matter is a transcription fact — these are the rows the form
prints — not a threshold.

| Fact kind | Raised when |
| --- | --- |
| `days_out_of_stock_reported` | The facility reported days out of stock above zero |
| `stock_on_hand_zero` | The facility reported a balance of zero |
| `stock_not_reported` | Every stock cell for the commodity was blank |

`stock_not_reported` exists because a blank column is a reporting gap, not a
stock-out — and the difference matters most exactly when supply has failed. A
reported zero **days** out of stock raises nothing at all: it is a real figure,
and a good one.

The check constraint `fact_carries_its_evidence` refuses a days-out-of-stock
fact with no days and a zero-balance fact with no balance, so no row asserts a
condition it has no evidence for. Its explicit `IS NOT NULL` tests are
load-bearing: a check constraint passes when it evaluates to NULL, so the
comparison alone would admit the row the constraint exists to refuse.

## Alerts are not signals

`commodity_operational_alert` is a separate table from every result table, and
it has nowhere to record a score, a suspicion, an efficacy or a resistance
claim. That is structural, not a naming convention.

A stock-out needs a storekeeper and a district pharmacist. A treatment-response
signal needs an epidemiologist and a laboratory. If both lived in one table with
a kind column, converting one into the other would be a one-line change — and
that conversion is the claim MARS must never make silently.

Later signal work may cite an alert as supporting or contextual evidence. The
citation runs one way: an alert is never rescored or relabelled as a
treatment-response or resistance signal.

## What requires governed configuration

| Alert kind | Needs approved rules? |
| --- | --- |
| `stock_out_reported` | **No** — it restates what the facility reported |
| `prolonged_stock_out` | Yes |
| `repeated_stock_out` | Yes |
| `multi_commodity_stock_out` | Yes |
| `low_stock` | Yes |
| `imminent_stock_out` | Yes |

"Prolonged", "repeated", "low" and "imminent" are judgements. Nine days out of
stock may or may not be prolonged; that depends on resupply times and buffer
stocks MARS does not know. A threshold with no approved rule behind it is an
engineer's opinion driving a supply decision.

Severity works the same way. It stays `unclassified` unless governed rules say
otherwise, enforced by `severity_requires_configuration`. The companion
constraint `classified_alerts_need_config` refuses any alert kind other than
`stock_out_reported` without a configuration version, so a future code path
writing directly to the table is held to the same rule the engine is.

### The configuration key

`commodity_alert_rules`, registered by governance and **shipped with no
values**. Until a programme approves a version, the engine:

* records the reported stock facts;
* raises `stock_out_reported` where the source supports it;
* leaves severity `unclassified`;
* lists the five skipped classifications in `classifications_skipped`;
* explains the gap in `notes`.

A fresh deployment producing no classifications is the expected state. It says
which configuration is missing rather than appearing broken or returning
zeroes that look like an absence of stock-outs.

## Blank, zero, unavailable

Kept apart everywhere, as in the rest of MARS.

| Situation | Value | Status |
| --- | --- | --- |
| Facility tested 40, none positive | `0.000000` | `available` |
| Facility tested nobody | `null` | `unavailable_no_denominator` |
| Commodity cells all blank | `null` | `unavailable_insufficient_data` |
| Facility reported 0 days out of stock | no fact raised | — |

A facility that tested nobody has no positivity. It does not have a positivity
of zero, because zero would be read as an absence of malaria when the truth is
an absence of testing.

## Provenance

Every result carries the same envelope: geography grain and unit, period and
grain, the indicator/method/configuration versions in force, boundary version,
source cutoff, engine version, computed-at, contributing and expected units,
and an input fingerprint.

Results are immutable. Recomputing over unchanged evidence writes nothing;
changed evidence writes a new row beside the old one, so the figure a district
acted on last week is still readable after this week's correction.

## Identity

None of these tables holds a patient reference, a name, a phone number, a
national identifier or a coordinate. Testing and treatment measures are counts
over encounters at a facility; commodity facts are counts over a supply return.

## Limitations

* `repeat_treatment_episodes` is declared and not yet computed; it depends on
  the episode engine's output and will be filled by the measure that reads it.
* Commodity context is attached from `commodity_stock_fact`, so it appears only
  for periods whose aggregate submission has been accepted and processed by the
  commodity engine.
* Treatment recognition rests on `drug_name_normalised` — a prescription line
  MARS could not normalise counts as a recorded line, not as a treatment.
