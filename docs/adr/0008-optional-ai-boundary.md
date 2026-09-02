# ADR 0008: The optional AI assistant boundary

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 1 (boundary), 13 (implementation)

## Context

An assistant that answers questions over MARS output would be genuinely useful:
explaining why a district is high priority, comparing two districts, drafting an
investigation brief. It is also the component most likely to undermine the
system, because a language model will produce a fluent sentence whether or not
the evidence supports it.

The dangerous sentence is easy to picture. Asked about a district with a
repeat-positive cluster, a model reading a page headed "resistance surveillance"
will readily produce a claim that resistance has been confirmed there — a
statement no routine dataset can support, phrased with more confidence than any
of the underlying evidence carries.

Blueprint sections 018, 055 and 056 are unambiguous: AI is optional and
secondary, the core must work fully without it, and it must not have authority
to alter metrics, change signal classification, invent values, diagnose
resistance or expose patient identifiers.

## Decision

The AI assistant is **out of scope for phases 1 and 2** and disabled by default.
`MARS_AI_ASSISTANT_ENABLED` defaults to false, and `/api/v1/meta/version`
reports it, so its absence is visible rather than assumed.

The boundary is recorded now, before anything is built against it:

**It is a leaf.** No module depends on `mars.ai`. Disabling it removes a panel
and changes nothing else. A test will assert the absence of that dependency.

**It consumes approved objects only.** The assistant receives metric, signal and
explainability objects that have already passed validation, plus a controlled
glossary. It never queries the database and never sees a raw row.

**It cannot write.** No route exposed to the assistant alters a metric, a signal
status, a priority or a score. It is not a permission it lacks; those code paths
do not exist for it.

**Identifiers are refused, not stripped.** A redaction pass rejects a request
containing an identifier-shaped field rather than removing it and continuing. A
prompt that should not have been constructed is a defect to surface, not to
paper over.

**It cites.** Responses reference the MARS object identifiers and periods used,
so a reader can check the claim against the underlying object.

**The terminology lint applies to it.** Prompt templates and canned responses
are scanned like any other copy (ADR 0005).

**Every request is logged**: request type, object identifiers supplied, provider
and model version, response hash - and never the content of prohibited fields.

**There is a kill switch**, and its failure mode is graceful: if the provider is
unavailable the panel disappears and no surveillance capability is lost.

## Consequences

- MARS can be demonstrated, deployed and operated with no AI provider at all.
- The assistant, when built, is a thin grounded layer rather than a second
  analytical engine.
- Some questions will be unanswerable because the necessary structured object
  does not exist. The correct response is to say so, not to reason from raw
  data.

## Revisit when

Phase 13 begins. The boundary itself is not open for revision; the provider
choice and prompt templates are.
