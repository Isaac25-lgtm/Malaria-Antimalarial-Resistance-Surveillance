# Investigation workflow

Where MARS stops being an analysis and becomes operational software. A signal
nobody acts on is a signal that was never worth generating.

| Table | Holds |
| --- | --- |
| `investigation` | One programme response to one signal |
| `investigation_event` | The append-only timeline |
| `investigation_evidence_request` | A request for evidence MARS cannot produce |
| `investigation_feedback` | Labelled outcomes for later method review |

## The state machine

```
NEW ──▶ TRIAGED ──▶ ASSIGNED ──▶ UNDER_INVESTIGATION ──▶ CLOSED
 │         │            │  ▲              │  │
 │         │            └──┘ (reassign)   │  └────────▶ ESCALATED
 └─────────┴───────────────────────────────┘  (close early)
```

Transitions are **validated, not advisory**. An investigation that jumped from
new to closed would record a decision no reviewer made.

| From | May go to |
| --- | --- |
| `new` | `triaged`, `closed` |
| `triaged` | `assigned`, `closed` |
| `assigned` | `under_investigation`, `assigned` (reassign), `closed` |
| `under_investigation` | `closed`, `escalated`, `assigned` (reassign) |
| `closed` | — |
| `escalated` | — |

Closed and escalated are terminal, and reopening is deliberately absent. A
conclusion that can be quietly withdrawn is not a conclusion; a genuine change
of mind belongs in a new investigation that cites the old one.

Reassignment is permitted from both working states, because people go on leave
mid-investigation, and it is recorded as its own event kind so a timeline never
hides a handover.

## The signal is never mutated

The foreign key points one way, and it is `RESTRICT`. Concluding an
investigation does not edit the signal's evidence, score or status. The
analysis said what it said on the day it ran, and a later human judgement sits
beside it rather than correcting it — which is what keeps the analytical audit
trail worth having.

## Outcomes

| Outcome | Means |
| --- | --- |
| `validated_signal` | The pattern held up and warrants programme action |
| `explained` | A benign explanation was found — a new clinician, a referral change, a reporting change |
| `data_issue` | The pattern was an artefact of the data rather than of malaria |
| `insufficient_evidence` | Review could not settle it either way |

**`validated_signal` is not confirmed resistance.** It is the strongest thing a
reviewer working from routine data may record. Confirmation reaches MARS from
an external reference laboratory through the separately governed evidence lane,
and there is no column in these tables that could hold such a claim.

`explained` is a first-class outcome and in practice the commonest. A
vocabulary that only recorded confirmations would make every closed
investigation look like a finding.

## The timeline is append-only

`investigation_event` is never updated and never deleted. An investigation
whose history can be rewritten cannot support the decision it led to.

Every act writes an entry: who, when, what changed, and any note. `actor_label`
holds the operator's own name, which is theirs. No patient identifier is ever
written to these tables.

## Evidence requests

A request records what was asked for. When a result comes back, MARS stores a
**reference** into the system that holds it under its own governance —
`a_received_result_has_a_reference` enforces that a received request names one.

MARS never stores the clinical content of an external result. That separation
is what keeps the confirmed-evidence lane distinct from routine surveillance.

## Concurrency and idempotency

`record_version` is an optimistic-concurrency token. Two reviewers who both
loaded an investigation and both press close must not silently overwrite one
another: the second write is refused with a conflict and the reviewer re-reads.
Losing one of two contradictory conclusions is worse than making someone press
the button again.

`idempotency_key` makes opening safe to retry. A repeated open — or a second
open for a signal that already has an investigation — returns the existing
record rather than splitting the timeline in two, which would leave two people
each believing the other had it.

## Permissions

Each command declares its own. One blanket `investigation:write` would let
whoever can add a note also close the case.

| Action | Permission |
| --- | --- |
| Read, queues | `surveillance:view_aggregate` |
| Open, triage | `investigation:triage` |
| Assign, reassign | `investigation:assign` |
| Start, note, evidence request, external result | `investigation:update` |
| Close, escalate | `investigation:close` |

Scope is applied in SQL. A facility-restricted account sees only
investigations for its own facilities; a not-found is returned for anything
else, deliberately indistinguishable from absent — confirming that an
investigation exists but is not yours to read would disclose that something was
flagged there.

## Action centre queues

`new`, `high_priority`, `assigned_to_me`, `under_investigation`,
`awaiting_external_result`, `resolved`.

**There is no overdue queue.** It requires an approved
`investigation_sla` configuration, and MARS ships no value: how long a district
has to triage a signal is a programme commitment, and inventing one would put
real people behind an imaginary deadline. `GET /investigations/queues` reports
that the overdue queue is unavailable and names the missing configuration,
because an empty overdue list would say "nothing is late".

## The learning loop is inert

`investigation_feedback` records what a reviewer concluded against the method
version **in force when the signal was generated**, together with the signal's
input fingerprint, so a later governed method review has labelled evidence tied
to the exact evidence set that produced it.

It moves no threshold, changes no weight and adjusts no rule. Automatic tuning
from field outcomes is exactly the quiet drift that makes a surveillance system
unauditable — any change to a governed method still goes through governance.

## Limitations

* Programme metrics (median time to triage, validated-signal yield, backlog)
  are not computed. Several of them depend on an approved SLA.
* Attachments are referenced, not stored. MARS holds a pointer to an external
  record, never a file.
* Closure does not notify anyone. Notification delivery is a deployment
  concern and is not implemented here.
