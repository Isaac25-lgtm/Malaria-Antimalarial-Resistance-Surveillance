# Ask MARS

An optional assistant that sits inside MARS and outside its authoritative
analytical core (blueprint 055–056).

**MARS ships with it switched off and with no model provider registered.** That
is not an oversight: choosing a vendor is a procurement and
information-governance decision, and shipping a client that reads an API key
from the environment would make it possible to enable the assistant by
accident.

## Every screen works without it

`ai_assistant_enabled` is `false` by default. When it is off, the assistant's
routes are not registered at all and `mars.ai` is never imported — a test
launches a subprocess, builds the router with the flag off, and asserts that
`mars.ai` is absent from `sys.modules`.

`GET /api/v1/meta/assistant` reports availability. It lives in `meta` and reads
only the feature flag, because the endpoint a client uses to *discover* the
assistant must not itself load it.

## The provider boundary

MARS declares a `Protocol` and ships no implementation that calls anything.

```python
class AssistantProvider(Protocol):
    name: str
    def complete(self, *, system: str, prompt: str) -> ProviderResponse: ...
```

A provider is given an already-assembled, already-redacted, already
scope-filtered prompt and returns text. It gets no tools, no database handle
and no way to call back into MARS.

`NullProvider` answers from the supplied context and nothing else. It exists so
the retrieval, grounding, citation and authorisation paths can be exercised
without a network — **no test ever calls a model.**

A deployment installs a provider with `register_provider(...)`. Until it does,
`/ai/ask` returns `available: false` with `reason: no_approved_provider`.

## What the assistant may be asked

| Topic | Answers |
| --- | --- |
| `district_priority` | Why a district appears in the priority list |
| `commodity_alerts` | Which districts have commodity alerts |
| `explain_signal` | A signal, in plainer language |
| `compare_recurrence` | Governed recurrence results side by side |
| `investigation_brief` | A draft brief from a signal and its explanation |

Anything else is refused with the list of what it can do. A bounded assistant
that says what it can answer is more useful than an open one that answers
badly. No topic offers a diagnosis, and none mentions resistance.

## What it cannot do

It cannot create a signal, change a metric, transition an investigation,
diagnose, invent a number, or write an analytical record. **There is no code
path in the package that could** — it holds a read-only service handle and
returns a response object.

## The four safety properties

### Retrieval is scoped in SQL

Context comes from `SurveillanceSummaryService` and `SignalQueryService`, the
same services the screens use. A question cannot reach a district the asker
cannot open; asking about a signal outside scope raises the same `NotFoundError`
the API returns, so a question cannot be used to probe for signals elsewhere.

### Nothing identifying leaves

Analytics holds no direct identifier, but the assembled context is scanned
anyway before anything is sent — a defence that depends on an upstream
invariant staying true is not a defence.

The scan checks forbidden field names (`patient_name`, `phone`, `nin`,
`national_id`, `next_of_kin`, …) and patterns that look like an identifier
wherever they sit, including inside free text: Ugandan NIN, phone numbers, and
email addresses. It recurses into nested structures.

A hit **refuses the request** rather than redacting it. A silent redaction
would hide the upstream defect that put a name in analytics.

### Answers are grounded and cited

Every response carries the MARS record IDs and periods that were supplied. When
retrieval finds nothing, the answer is an explicit *"no MARS records match that
question for this period and your scope … this is not a statement that nothing
happened"* — not prose about an empty result.

Every answer also carries the interpretation limit.

### Prompt content is untrusted

Retrieved records and the user's question are quoted into delimited blocks, and
the system prompt states that text inside them is data and never an
instruction, *whatever it appears to say*. That is the only structural defence
available when arbitrary record text has to be quoted into a prompt.

The system prompt additionally forbids inventing numbers, forbids stating or
implying that resistance, treatment failure, recrudescence or reinfection has
been confirmed, requires citation, and tells the model it cannot change any
MARS record.

## Audit

Each request records the topic, provider, model, a SHA-256 of the response, the
citation count and the record IDs supplied.

**The question text is not logged.** It is a user's own words and may name a
facility or a colleague; what is logged is enough to reconstruct what was
supplied and what came back.

## Limitations

* No provider implementation ships. A deployment must supply and approve one.
* The assistant has no conversational memory. Each question is answered from
  its own retrieval.
* Feedback capture on answers is not implemented.
* Answers are not translated; the controlled glossary of blueprint 055 is not
  yet built.
