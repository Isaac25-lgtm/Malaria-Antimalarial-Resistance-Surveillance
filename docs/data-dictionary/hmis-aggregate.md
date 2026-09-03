# HMIS 033b and 105 — aggregate reporting

MARS ingests two printed forms, transcribed in
`backend/src/mars/domain/hmis_elements.py`:

| Form | Frequency | What MARS takes from it |
| --- | --- | --- |
| **HMIS 033b** Health Unit Weekly Epidemiological Surveillance Report | Every Monday, Monday–Sunday | Malaria cases and deaths, the section 5 tested/treated block, OPD attendance, tracer stock balances |
| **HMIS 105** Health Unit Outpatient Monthly Report | 7th day of the following month | OPD attendance, the EP01 malaria block, laboratory parasitology, commodity stock status |

Both are Print Version July 2024, as supplied.

## What is transcribed and what is assigned

Where a form prints a code, MARS uses it **verbatim** — including 033b's
trailing full stop (`MA.`) and 105's section numbering (`EP01a`).

Where a form prints no code, MARS assigns one under the `M033B_` prefix and
marks the element `code_assigned_by_mars`. The malaria summary and stock blocks
on 033b are unlabelled columns under a single row marker (`MAT.`, `TRA.`), so
every element MARS derives from them carries an assigned code. A reader can
tell a transcribed code from an assigned one without opening the PDF.

**This is not the whole of either form.** HMIS 105 lists several hundred
diagnoses across nine sub-sections; MARS ingests the elements it uses. The
storage model is keyed by element code rather than by column, so adding one is
a registry entry, not a migration.

### The malaria block on HMIS 105 (section 1.3.1, EP01)

| Code | Label |
| --- | --- |
| `EP01a` | Suspected Malaria (fever) |
| `EP01b` | Malaria Tested (B/s & RDT) |
| `EP01c` | Malaria confirmed (B/s & RDT) |
| `EP01d` | Confirmed Malaria cases treated |
| `EP01e` | Total malaria cases treated |

All five are disaggregated by the form's five age bands (`0–28 days`,
`29 days – 4 yrs`, `5–9 yrs`, `10–19 yrs`, `20 yrs & above`) and by sex.

`EP01e − EP01d` is treatment without a confirmed result. The form collects
both, so the difference is **reported rather than inferred** — and it is a
statement about testing practice, never about the parasite.

### The malaria block on HMIS 033b (section 5)

Ten columns, single totals: suspected; tested with RDT; RDT positive; tested
with microscopy; microscopy positive; not tested cases treated; RDT negative
cases treated; RDT positive cases treated; microscopy negative cases treated;
microscopy positive cases treated.

The form collects negative-treated and not-tested-treated explicitly, so MARS
does not have to infer either.

Section 1 row 1 (`MA.`, Malaria Confirmed) prints cases and deaths; its
*Tested* and *Pos(+ve)* cells are blacked out on the printed form, because
section 5 carries the testing detail. MARS ingests them the way the form
divides them.

### Laboratory (HMIS 105 section 10.2.1)

`PS01` Malaria Microscopy and `PS02` Malaria RDTs, each with *Number Done* and
*Number Positive*. Stored **apart from** the OPD diagnosis block: the
laboratory counts tests it performed, the OPD block counts patients it
diagnosed, and where the two disagree, the disagreement is the finding.

### Commodities

HMIS 105 section 6.1 prints four columns per commodity — quantity consumed,
days out of stock, stock on hand, quantity expired — and defines out of stock
as *none left in the health unit STORE*. 033b section 7 prints a single weekly
balance instead. Both land in `commodity_stock_observation` with a `metric`
saying which measure a value is, so a weekly balance is never mistaken for a
monthly consumption figure.

MARS holds the malaria-relevant commodities: `SS01` Artemether/Lumefantrine,
`SS02` Artesunate, `SS03` LLINs, `SS24` Sulfadoxine/Pyrimethamine, `SS34`
Malaria Rapid Diagnostic; and on 033b the AL, artesunate, SP and RDT tracer
items.

## Three rules the schema carries

**A blank cell is not a zero.** Every value column is nullable. 033b
instruction 7 requires reporting every week "whether there are cases or not",
so a reported zero is a statement the facility made and a blank is a statement
it did not make. Treating a blank as zero turns a reporting gap into an
apparent improvement. The ingestion report counts blanks and zeros separately
for the same reason: in a total they look identical.

**A correction does not overwrite.** `revision` is part of the submission's
unique key, and exactly one revision per facility/form/period may be
`accepted`. A higher revision marks the earlier one `superseded`; a late older
revision is retained as superseded and can never displace the latest. Reusing a
revision with changed content is quarantined. The district acted on the first
figures; a record showing only the corrected number cannot explain what anyone
did.

**MARS does not re-band.** An aggregate arrives already summarised. A
disaggregated element must carry one of the form's own bands; a single-total
element must not carry one at all. Splitting `29 days – 4 yrs` into finer ages
would be inventing detail no facility reported.

## Reconciliation

MARS separately holds the e-register encounters these forms summarise, so it
can compute the same quantities and compare. `ReconciliationService` writes a
`reconciliation_finding` per element with **both** values and their difference.

| Status | Meaning |
| --- | --- |
| `matched` | Reported equals derived |
| `within_tolerance` | Differs by no more than the configured tolerance |
| `differs` | A real discrepancy for the district to resolve |
| `reported_only` | The facility reported a figure; no encounter matched the rule |
| `derived_only` | The cell was blank; MARS has encounters. Not a discrepancy — the facility made no statement |
| `uncomparable` | MARS holds no encounters for that facility and period. A completeness question, not a discrepancy |

**Neither value is corrected.** Preferring the aggregate would hide the
register's detail; preferring the derived figure would mean MARS publishing
numbers no facility ever submitted, which is not MARS's to do.

**Every finding states its denominator.** A difference of four computed from
four encounters and one computed from four hundred deserve different attention.

The default tolerance is **0 — exact agreement**. No supplied source defines an
acceptable transcription variance, so MARS does not invent one; a tolerance is
a deployment's explicit choice, recorded against the finding's method version.

Each finding records the method version, configured absolute tolerance, and a
SHA-256 fingerprint of the exact submission/encounter/test snapshot it read.
Re-running the same evidence is idempotent; changed source evidence creates a
new finding and never rewrites the one a district previously reviewed.

Each derivation rule states the reason it is that query and not a similar one.
For example `EP01b` counts encounters with a test actually performed and
excludes those recording *not done*: a denominator inflated by untested
attendances understates positivity everywhere.

## Running it

```
mars-import-aggregate dry-run   --file returns.jsonl
mars-import-aggregate validate  --file returns.jsonl
mars-import-aggregate load      --file returns.jsonl
mars-import-aggregate load      --file returns.jsonl --resume
mars-import-aggregate reconcile --facility HF-401 --from 2026-03-01 --to 2026-03-31
```

Exit codes: `0` loaded and every comparison agreed; `1` quarantined
submissions or reconciliation differences — both need a person, neither is a
system failure; `2` usage error; `3` the batch failed as a whole.

Every non-dry-run writes to the shared import lifecycle ledger with
`import_domain = aggregate`: artefact checksum, stages, terminal status,
per-submission source rows, validation issues and the canonical submission id.
An exact artefact replay returns the existing batch id and performs no write.
`validate` persists actionable quarantine findings but writes no aggregate
submission; `dry-run` writes nothing at all.

## An aggregate return is counts, never people

The inbound contract **refuses** a submission carrying an identity-shaped field
— `patient_name`, `nin`, `national_id`, `surname`, `phone`, `identity`,
`line_list` and their neighbours. HMIS 033b and 105 have no such field, so a
correct producer never trips this.

It is refused rather than stripped, exactly as the encounter contract refuses
next of kin: a producer that believes MARS is holding the value needs to be
told it is not.

The guard exists because the whole inbound submission is stored on
`import_source_row.payload_redacted`, whose contract is that identity has
already been removed, and that table is read by operators, analysts and anyone
debugging an import — none of whom hold the re-identification permission. A
mis-mapped export that attached a line listing would otherwise land patient
data in `mars_core` with no error at all.

## Lane discipline

Everything here is **Lane A**: routine-derived. Aggregate returns can produce a
surveillance signal — a testing collapse, a negative-treated rate, a
reconciliation gap. None of it is, or can become, a confirmed antimalarial
resistance finding. That is Lane B, established externally by a reference
laboratory under separate governance.
