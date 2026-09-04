# Operations runbook

Day-to-day running: data refresh, DHIS2, governance activation, monitoring, and
the demonstration dataset.

## Data refresh

Cadence follows source availability. Nothing runs on a timer MARS invented.

| Source | Cadence | Job |
| --- | --- | --- |
| e-register encounters | As files arrive | `mars.ingestion.encounters.cli` |
| HMIS 033b (weekly) | Weekly | `mars.ingestion.aggregate.cli` |
| HMIS 105 (monthly) | Monthly, due the 7th | `mars.ingestion.aggregate.cli` |
| Reconciliation | Nightly | `mars.ingestion.aggregate.cli reconcile` |
| Indicator materialisation | After ingestion | `mars.workers.indicator_materialisation` |
| Episodes, recurrence | After materialisation | `mars.workers.episode_build`, `recurrence_compute` |
| Surveillance, baselines | After episodes | `mars.workers.surveillance_compute`, `baseline_compute` |
| Anomalies, spatial, clustering | After baselines | `anomaly_detect`, `spatial_compute`, `spatial_cluster` |
| Signals, explanations | Last | `signal_generate`, `explanation_build` |

The order matters and is not arbitrary: an anomaly needs a baseline, a baseline
needs history, a hotspot needs the area's own aggregated history, and a signal
needs the evidence it cites. Running them out of order does not corrupt
anything — each engine reports honestly that its input is missing — but it
produces a run where everything says "no baseline".

Every job is idempotent on an input fingerprint. Re-running over unchanged
evidence writes nothing.

## DHIS2

Disabled and unconfigured by default. A deployment that has not been given a URL
and credentials reports the integration as **unconfigured** — it does not fail
at the first request and does not quietly do nothing.

```bash
MARS_DHIS2_ENABLED=true
MARS_DHIS2_BASE_URL=https://dhis2.example.org
MARS_DHIS2_USERNAME=...        # through the environment only
MARS_DHIS2_PASSWORD=...        # never in a file, a log or a commit
```

Check status, then run:

```bash
curl -fsS .../api/v1/integrations/dhis2/status
python -m mars.integrations.dhis2.cli sync-metadata
python -m mars.integrations.dhis2.cli pull-aggregate --from 202607
```

A run that fails part-way records a cursor and is resumable with `--resume`;
it restarts from where it stopped rather than from page one.

**Mapping proposals are proposals.** DHIS2 organisation units are matched to
MARS geography and a proposal is written for review. Nothing is auto-accepted:
a wrong mapping silently attributes a district's cases to its neighbour.

## Governance activation

A fresh deployment computes nothing until a programme approves the methods. This
is the intended state, and the interface says so rather than showing zeroes.

What must be approved before each capability produces output:

| Capability | Needs |
| --- | --- |
| Indicator figures | An active `IndicatorDefinitionVersion` per indicator |
| Episodes | `malaria_episode_rule` with `episode_window_days` |
| Recurrence bands | `recurrence_interval_bands_days` |
| Commodity classifications | `commodity_alert_rules` |
| Baselines | `historical_baseline` with method, window, minimums |
| Temporal anomalies | `temporal_anomaly_rule` with method, threshold, minimum cases |
| Hotspots | `hotspot_definition` **and** an approved baseline method |
| Spatial clustering | `spatial_cluster_method` |
| Map detail | `spatial_privacy_policy` with minimum cell count and aggregation level |
| Signals | `signal_prioritisation` rules |
| Overdue investigation queue | `investigation_sla` |

Until each is approved the corresponding output reports `not_configured` and
names the missing key. **MARS ships no default for any of them.** Every one is a
programme decision with real consequences — a threshold decides how many
districts get an alert, a privacy minimum decides what may be shown, an SLA puts
people behind a deadline.

## Monitoring

Logs are structured JSON on stdout, one object per line, every line carrying a
request identifier.

| Watch | Why |
| --- | --- |
| `request_failed` rate | The obvious one |
| `record_denial` rate | A spike means a scope misconfiguration or a probe |
| Job completion, per job | A signal run that stops is silent otherwise |
| `analytics_refreshed_at` age | The dashboard shows it; alert before a user notices |
| Migration head vs image | A rolled-back deployment against a newer schema |
| `ask_mars_identifier_blocked` | **Any occurrence.** It means analytics contained something resembling a direct identifier — a data defect, not a routine event |

Sensitive query parameters (`token`, `access_token`, `code`, `state`, `nin`) are
redacted before logging. Question text sent to Ask MARS is never logged.

## The demonstration dataset

Deterministic synthetic data, loaded through the real ingestion path so that a
demonstration exercises the same code a real deployment runs.

```bash
python -m mars.demo.cli generate --out-dir ./demo
python -m mars.demo.cli register --out-dir ./demo
```

`generate` writes the dataset and `register` creates its synthetic facilities.
Load the generated batch files separately with `mars.ingestion.encounters.cli`;
that explicit step exercises the same ingestion and quarantine path used for a
real deployment. Both demo commands are deterministic given `--seed`, so two
people running the same command see the same demonstration.

Facilities are prefixed `DEMO-HF` and patient references `SYN`. No real patient
data exists anywhere in this repository, and no synthetic record carries a
coordinate — the generator does not produce one, so no demonstration can show a
household on a map.

`MARS_DEMO_MODE_ENABLED` marks every screen as carrying synthetic data. It is
**refused in a protected environment**: a demonstration that looked like
production is the mistake this guard prevents.

### Demo reset

```bash
# Confirm which database you are pointed at before removing anything.
psql -d "$DEMO_DB" -c "SELECT current_database()"

python -m mars.demo.cli purge              # dry run: reports what it would delete
python -m mars.demo.cli purge --confirm    # actually deletes
python -m mars.demo.cli generate --out-dir ./demo
python -m mars.demo.cli register --out-dir ./demo
```

`purge` without `--confirm` deletes nothing and reports what it would remove.

It is scoped by the demo facility code prefix and **by nothing else** — no date
range, no district. A purge that accepted either would eventually be pointed at
real data, and the prefix is the only filter that cannot be aimed at a real
facility by mistake.

## Known limitations

Stated rather than discovered.

* **Population denominators are not available.** No incidence per head of
  population is computed anywhere. Every rate has a denominator drawn from the
  reported data itself.
* **Parish and village geography is empty.** No boundary data has been supplied
  and MARS does not fabricate geography.
* **Secondary suppression is not implemented.** Single small cells are
  suppressed; a differencing attack against a published total and its parts is
  not defended.
* **Rate limiting is at the proxy**, not in the application.
* **No provider ships for Ask MARS.** It is off, and enabling it requires a
  procurement decision.
* **Programme metrics** (time to triage, validated-signal yield) are not
  computed; several depend on an approved SLA.
* **Notifications are not delivered.** Closing an investigation notifies nobody.
* **EWMA and CUSUM** detection methods are not implemented.
* **Residence aggregation** covers testing coverage and positivity only.
