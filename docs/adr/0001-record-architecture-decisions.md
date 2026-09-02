# ADR 0001: Record architecture decisions

**Status:** Accepted
**Date:** 2026-09-01
**Phase:** 1

## Context

MARS will be built over roughly thirty implementation steps by more than one
person, across a period long enough that the reasons for early decisions will be
forgotten. Several of those decisions are not matters of engineering taste: they
are commitments about what the system is permitted to claim, who may see patient
data, and which version of a rule produced a number on a screen.

A decision whose rationale is lost gets reversed by someone acting reasonably on
incomplete information. For the scientific boundary in particular (ADR 0005),
that reversal would be a serious harm rather than a refactor.

## Decision

Architecturally significant decisions are recorded here as numbered, immutable
documents. A decision is architecturally significant when it is expensive to
reverse, when it constrains what later phases may do, or when it encodes a
commitment to the malaria programme rather than a preference of the implementer.

Each record states the context, the decision, the consequences accepted, and
what would justify revisiting it. Records are never edited after acceptance: a
changed decision gets a new record that supersedes the old one, so the history
of the reasoning stays legible.

## Consequences

- The cost of a decision is paid once, in writing, rather than repeatedly in
  conversation.
- A reviewer can see whether a change contradicts a prior commitment.
- Records will occasionally be wrong; the superseding mechanism handles that
  without erasing the earlier reasoning.

## Revisit when

Never in principle. The format may change; the practice should not.
