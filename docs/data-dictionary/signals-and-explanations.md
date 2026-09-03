# Signals and explanations — Prompts 21–22

`signal_generation_run` records a completed governed run or a `not_configured`
refusal. MARS ships no signal weights, score threshold, minimum corroboration,
priority bands, or recommended-action configuration.

`surveillance_signal` is a versioned routine-data pattern requiring
investigation. It copies the exact rule snapshot, score, priority mapping,
source cutoff, quality context, uncertainty and evidence fingerprint. Changed
evidence creates a successor and links the previous record; it does not rewrite
the previous evidence.

`signal_evidence` points to one typed upstream analytical record. Supporting,
counter and contextual evidence are distinct. A commodity operational alert can
only be context: it remains a separate supply-chain record and is never
converted into a resistance or treatment-response conclusion.

The collector recognises recurrence results, temporal anomalies (including
testing and treatment series), hotspots, spatial clusters, reconciliation
findings and commodity context. Re-running an immutable upstream engine does
not count identical evidence twice: stable input fingerprints are deduplicated
before scoring.


`signal_explanation` is an immutable deterministic snapshot containing:

- why the signal was flagged;
- supporting/contextual and counter-evidence;
- data quality;
- the exact method steps and rule snapshot;
- uncertainty and missing information;
- governed recommended-action codes; and
- the routine-data interpretation limit.

No LLM or external service participates in generating this evidence. Routine
data cannot confirm resistance, treatment failure, recrudescence or reinfection.

Scope-safe read surfaces are `/api/v1/signals`,
`/api/v1/signals/{signal_id}` and
`/api/v1/signals/{signal_id}/explanation`. Out-of-scope identifiers return the
same 404 as absent identifiers. Facility restriction is intersected with
geography restriction; a facility user cannot read sibling-facility or
district-wide signals merely because their facility lies in that district.
