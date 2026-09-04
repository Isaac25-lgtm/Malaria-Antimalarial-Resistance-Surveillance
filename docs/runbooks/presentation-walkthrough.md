# Presentation walkthrough

Twenty minutes, five screens, one argument: **MARS finds patterns worth
investigating and never claims more than that.**

Everything below runs against the synthetic demonstration dataset. Say so at
the start; a demonstration presented as production is the thing this project has
spent thirty prompts refusing to do.

## Before you start

```bash
python -m mars.demo.cli generate --out-dir ./demo
python -m mars.demo.cli register --out-dir ./demo
```

Then activate the governed methods, or don't — see [The unconfigured
opening](#the-unconfigured-opening) for why leaving them off is the stronger
demonstration.

## The unconfigured opening

**Open the command centre before activating anything.**

Every measure says *not configured* and names the approval it is waiting for.
The provenance bar says no indicator has an approved version.

This is the most important thirty seconds of the demonstration. Say:

> This is what MARS shows a Ministry that has installed it and not yet decided
> what to measure. It does not show zeroes. A country of zeroes would look
> finished, and it would be a lie — the difference between *no malaria* and *no
> analysis* is the difference this whole system is built around.

Then activate the methods and reload. The same screen fills with figures, each
carrying its own period, source and method version.

## 1. National command centre — 4 minutes

Point at the KPI strip. Each card names its governed indicator code as its
source, and a proportion shows the denominator it was taken over.

Point at a card with a comparison. The arrow always states the window it is
comparing against — *"up from 0.31 in the preceding period (2026-06-01 to
2026-06-30)"* — because a rise against an unnamed baseline is a figure nobody
can check.

Point at the commodity panel, which sits apart from the signals:

> A stock-out needs a district pharmacist. An epidemiological signal needs an
> investigation. They are different problems with different owners, and one list
> would let the first quietly become the second.

Point at the interpretation boundary. It is rendered from the server's own
words, not a string in the browser bundle.

## 2. District workspace — 3 minutes

Click a priority district. The breadcrumb stays visible.

Go to the facility contribution table, and find a facility that reported
nothing. It is listed with a dash and *No return*, not a zero:

> A district total that falls because a large facility stopped reporting looks
> exactly like a district total that falls because transmission fell. This table
> is how you tell them apart. Dropping the non-reporters would hide the
> explanation.

## 3. Facility workspace — 2 minutes

Click through to a facility. Note there is **no district figure anywhere on the
page**.

> A facility user's district scope proves that the facility sits inside the
> district. It does not grant the district-wide surveillance picture. That rule
> is enforced in SQL, in every read model, and there is a test for it in each
> one.

## 4. Signal evidence — 5 minutes

The screen the whole system exists to justify.

Show the hero, then the *why this was flagged* panel — deterministic, generated
by an engine, not by a model. Then the evidence table, and stop on a
counter-evidence row:

> Counter-evidence gets equal billing. A screen that showed only what agrees
> with the flag would be an advocacy document. In practice the counter-evidence
> is the more useful half when a district officer decides whether to send
> someone.

Show the method version, the rule code and the input fingerprint:

> This is what makes the result arguable with. Someone can come back in six
> months, take this fingerprint, and reconstruct exactly which evidence produced
> this signal under which approved rule.

Then read the interpretation limit aloud. It is on every signal, every report
and every export.

## 5. Action centre — 3 minutes

Show the queues. Then point at what is missing:

> There is no overdue queue. An overdue list needs an agreed SLA — how long a
> district has to triage a signal — and that is a programme commitment nobody
> has made. An empty overdue list would say *nothing is late*. MARS says *I have
> not been told what late means*.

Open an investigation and show the append-only timeline.

## 6. Reports — 2 minutes

Download the national brief. Open it in a spreadsheet.

The interpretation limit is the second row, above the header. An unconfigured
measure is an empty cell with the reason in its own column — not a zero:

> A spreadsheet cell has nowhere to put a caveat, so the caveat gets a column.

## Closing — 1 minute

> Routine HMIS and e-register data can tell you where to look. It cannot tell
> you that a drug has stopped working. Confirming resistance takes a reference
> laboratory and a study design, and that evidence reaches MARS through a
> separately governed lane it cannot write into.
>
> Everything you have seen is the first half done honestly, so that the second
> half is worth doing.

## Questions you will be asked

**"Can it tell us where resistance is?"**
No, and no system built on routine data can. It tells you where a pattern is
strong enough to be worth a visit, with the evidence for and against.

**"Why is that district grey?"**
Grey is *no or insufficient data* — never *low*. The absence of an alert may
reflect the absence of usable data, and the map says which.

**"Can we get the patient list?"**
Pseudonymous case evidence, with the right permission and sensitivity tier.
Names, phone numbers and national identifiers never leave the identity vault,
and no analytical table has a column that could hold one.

**"Is this running anywhere?"**
Not yet. The deployment artefacts are complete and tested; no environment has
been provisioned. Nobody has claimed otherwise in this repository.
