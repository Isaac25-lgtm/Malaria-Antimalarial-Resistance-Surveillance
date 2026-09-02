# E-register inbound contract, v1

The shape MARS accepts outpatient encounters in, and the rules that decide
whether a batch loads, quarantines or is refused.

**This is a MARS-owned contract, not a vendor's.** No authoritative external
e-register API or schema has been supplied, and inventing one would mean
building an adapter against a system whose fields nobody has seen. MARS
therefore publishes what it will accept; a source system, or an adapter written
for one, is responsible for producing it.

Every field maps to HMIS OPD 002 as recorded in
[opd-002.md](opd-002.md). Nothing is accepted that the register cannot express.

## Transport

A **batch file** in [JSON Lines](https://jsonlines.org/): one JSON object per
line, UTF-8, no trailing commas. The first line is the **envelope**; every
subsequent line is a **row**.

JSONL rather than a single JSON document because a national extract is large and
a streaming reader must be able to reject line 40,000 without holding the first
39,999 in memory — and because a truncated file is detectable rather than merely
unparseable.

## The envelope

```json
{
  "record_type": "envelope",
  "schema_version": "1.0",
  "source_system": "ereg-demo",
  "facility_code": "HF-001",
  "extracted_at": "2026-03-04T08:00:00Z",
  "register_opened_on": "2026-01-01",
  "register_closed_on": null,
  "row_count": 128
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `record_type` | yes | Always `envelope` on the first line |
| `schema_version` | yes | Contract version. An unknown value **quarantines the whole batch** |
| `source_system` | yes | Stable identifier of the producing system |
| `facility_code` | yes | Resolved against `facility_identifier`; unresolvable quarantines the batch |
| `extracted_at` | yes | When the source produced this extract, RFC 3339, UTC |
| `register_opened_on` / `register_closed_on` | no | Book-level provenance from OPD 002 page 1 |
| `row_count` | yes | Declared count; a mismatch is a blocking issue, not a warning |

`row_count` is checked because a truncated upload is the most common way a batch
goes wrong, and a silently short import looks exactly like a quiet week.

## A row

```json
{
  "record_type": "encounter",
  "source_row_id": "book7-row12",
  "encounter_date": "2026-03-04",
  "date_source": "row_header",
  "serial_number": "001",
  "identity": {
    "identifier_type": "national_id",
    "identifier_value": "CM90210077",
    "surname": "Okello",
    "given_name": "Amina",
    "phone_contact": "0700999888"
  },
  "age": {"value": 7, "unit": "months"},
  "sex": "F",
  "patient_category": "N",
  "residence": {
    "district": "GULU",
    "subcounty": "BAR-DEGE",
    "parish": "KICHECHE",
    "village": "LAYIBI"
  },
  "attendance_type": "new_attendance",
  "fever_present": "yes",
  "presenting_complaint": "fever, headache",
  "notifiable_marked": false,
  "tests": [{"method": "rdt", "result": "positive"}],
  "diagnoses": ["Malaria"],
  "prescriptions": [
    {"text": "Coartem 4 x 2 x 3", "drug_name": "Coartem",
     "units_per_dose": 4, "doses_per_day": 2, "days": 3}
  ],
  "referrals": [{"direction": "outbound", "number": "REF-2026-014"}]
}
```

### Required on every row

`record_type`, `source_row_id`, `encounter_date`, `sex`.

`source_row_id` must be **stable for the life of the row in the source system**.
It is what makes a replay idempotent: `(source_system, source_row_id)` is unique
on `opd_encounter`, so re-sending a batch updates rather than duplicates. A
source that renumbers its rows between extracts will create duplicate
encounters, and no amount of MARS-side cleverness can prevent that — which is
why the requirement is stated here rather than assumed.

### `identity`

The only object carrying direct identifiers. It is consumed **inside the
identity boundary** and never reaches `mars_core`, a log, or a quarantine row.

| Field | Notes |
| --- | --- |
| `identifier_type` | `national_id`, `refugee_number`, `passport`, `phone`, `unspecified_scheme` |
| `identifier_value` | OPD 002 column 2 |
| `surname`, `given_name` | OPD 002 column 3 |
| `phone_contact` | OPD 002 column 3 |

Omit the whole object when the register row carried no identifier. The encounter
still loads, with no `patient_reference_id` — which is the honest outcome, and
the common one.

**Next of kin is not part of this contract.** OPD 002 column 8 exists on the
form; MARS stores it nowhere, so a producer must not send it. A row containing
`next_of_kin` is rejected rather than ignored, because silently dropping it
would leave the producer believing MARS holds it.

### Value sets

Every coded field takes exactly the codes the register prints, plus `unknown`
where MARS documents one:

| Field | Accepted |
| --- | --- |
| `sex` | `M`, `F` (also `male`, `female`, `unknown`) |
| `patient_category` | `N`, `R`, `F` |
| `age.unit` | `years`, `months`, `days` |
| `attendance_type` | `new_attendance`, `re_attendance` |
| `fever_present` | `yes`, `no`, `unknown` |
| `tests[].method` | `microscopy`, `rdt`, `not_done` |
| `tests[].result` | `positive`, `negative`, `not_done`, `not_applicable` |
| `referrals[].direction` | `inbound`, `outbound` |
| `date_source` | `row_header`, `carried_forward`, `source_supplied` |

An unrecognised code is **never coerced**. The row is quarantined with the field
path and the value that was not understood, so a producer can fix the mapping
rather than discovering months later that half their `B/S` results became
`unknown`.

### Blank is not zero

An absent field, `null`, and `""` all mean *not recorded*. None of them becomes
`0`, `false` or a default. A negative count, an age below zero, or a numeric
zero where the register would have left a blank are all blocking issues.

## What happens to a batch

```
received → validating → ┬→ loading → completed
                        │            partially_completed
                        ├→ quarantined      (nothing loadable)
                        └→ failed           (the batch itself is unusable)
```

| Status | Meaning |
| --- | --- |
| `received` | Artefact accepted, checksum recorded |
| `validating` | Rows being checked; nothing written to `mars_core` yet |
| `quarantined` | No row was loadable. The batch is retained in full for diagnosis |
| `loading` | Valid rows being written |
| `completed` | Every row loaded |
| `partially_completed` | Some rows loaded, some quarantined — the normal outcome for real data |
| `failed` | The envelope, checksum, schema version or facility could not be resolved |

`partially_completed` is a first-class success. Real registers contain
unreadable rows, and refusing a whole district's month because forty rows are
malformed would lose far more than it protects.

## Idempotency

Three guarantees, each enforced by a database constraint rather than by
application logic:

| Guarantee | Enforced by |
| --- | --- |
| The same artefact cannot create a second batch | `uq_import_batch_source_checksum` |
| The same source row cannot create a second encounter | `uq_opd_encounter_source_row` |
| The same row cannot be recorded twice within a batch | `uq_import_source_row_batch_reference` |

Re-sending an identical file returns the existing batch and loads nothing. A
file whose **content changed** under the same `source_row_id` values is a
**revision**: it creates a new batch, and each affected encounter is updated in
place with the new batch recorded as its source. Silently overwriting without a
new batch would leave no trace that the data had changed.

An interrupted run resumes: rows already written are recognised by their
`(source_system, source_row_id)` and skipped, counted as `unchanged`.

## Identity never leaves the boundary

The pipeline runs in stages, and this is the reason for the split:

```
read → validate → link identity → write canonical
                  (identity role)  (application role)
```

The **identity stage** is the only stage that sees the `identity` object. It
returns a `patient_reference_id` and a linkage confidence, and nothing else
crosses into the canonical stage. The raw row is discarded once linked.

A quarantined row is stored with its `identity` object **removed**, not
redacted in place — a masked value is still a value, and a quarantine table is
read by more people than the vault.

The two stages use different database roles and therefore different connections,
so a single transaction cannot span them. That is deliberate: a cross-role
transaction would need one connection with both privileges, which is the
arrangement the whole boundary exists to prevent. Failure between the stages is
handled by idempotency instead — the linkage is recorded in the vault, the
canonical write is retried, and re-running the batch reaches the same state.

## Adapters

`InboundAdapter` is the interface a future source system implements:

```python
class InboundAdapter(Protocol):
    source_system: str
    def envelope(self, artefact: Path) -> InboundEnvelope: ...
    def rows(self, artefact: Path) -> Iterator[InboundRow]: ...
```

The JSONL adapter is the reference implementation. A CSV adapter, or an adapter
speaking a real vendor API, produces the same `InboundRow` objects and the rest
of the pipeline is unchanged. **No adapter may invent a field**: a source that
does not carry a malaria result produces rows without one, and the encounter
records that no test was done rather than guessing.

## Versioning

`schema_version` is `MAJOR.MINOR`.

- A **minor** bump adds optional fields. An older producer keeps working.
- A **major** bump changes or removes a field. MARS refuses the batch unless
  that version is explicitly supported.

An unknown version **quarantines the whole batch** rather than attempting a
best-effort read. Guessing a mapping is how a field silently lands in the wrong
column, and the failure surfaces as clinical nonsense months later rather than
as an import error today.

## Operating an import

```
mars-import-encounters dry-run  --file batch.jsonl   # reads; writes nothing at all
mars-import-encounters validate --file batch.jsonl   # records the batch and its issues
mars-import-encounters load     --file batch.jsonl
mars-import-encounters resume   --file batch.jsonl   # finish an interrupted batch
mars-import-encounters status   --batch <uuid>
```

`--json <path>` writes the full report document; `--initiated-by` records a
service label on the batch and must never be a personal name.

### Exit codes

A scheduler branches on these, so they distinguish *who has work to do*:

| Code | Meaning | Whose problem |
| --- | --- | --- |
| 0 | Every row loaded, or was already loaded | nobody |
| 1 | Loaded, with quarantined rows | the producer |
| 2 | Usage error: bad arguments, missing file, unknown batch | the caller |
| 3 | The batch failed as a whole | the producer |
| 4 | The identity component is required and unavailable | the operator |

3 and 4 are separate because they page different people.

### Counters

Every counter is recorded separately on `import_batch` and reported by the CLI.
“1,000 rows processed” tells nobody whether the month loaded; *820 loaded, 140
unchanged, 40 quarantined* does.

`rows_received`, `rows_loaded`, `rows_updated`, `rows_unchanged`,
`rows_quarantined`, `rows_linked`, `rows_unlinked`, `unresolved_geography`,
`warning_count`, `error_count`.

The batch's own lifecycle column is `import_status`, not `status`: the schema
convention is that each lifecycle carries its own named column, so a reader
never has to ask which status a generic one means.

**No identity value appears in any counter, log event or report document.** The
completion event is built from the report's own dictionary, whose non-status
fields are all integers.

### Identity is required unless refused explicitly

`load` exits 4 when the identity component is not configured. Loading anyway
would record every encounter as a new person, and the damage is invisible until
somebody asks how many patients attended more than once. A deployment that
genuinely wants unlinked loading passes `--no-identity`, which is how an
operator says they meant it.
