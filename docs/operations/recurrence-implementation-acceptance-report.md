# Configurable recurrence: implementation and acceptance report

> **Historical checkpoint:** the tables below describe the 11 September
> calculation-library checkpoint and are intentionally preserved as history.
> Persistence, the recurrence API, patient-origin investigations and the
> integrated frontend workflow were implemented afterward. Use
> [the 12 September completion handoff](claude-recurrence-completion-handoff.md)
> for the current code and verification state. PostgreSQL integration, live
> Ministry/browser validation and the higher-level corroborated signal method
> remain explicitly unverified or outstanding there.

Prepared on 11 September 2026. It maps the 30 requirements of the original brief,
and the 22 mandatory cases in section 10 of
[the handoff](claude-repair-first-recurrence-handoff.md), to code and tests.
Statuses:

- **Done**: implemented and covered by a passing test.
- **Partial**: the engine or data layer is done, but the API, UI or persistence
  it needs is not.
- **Not done**: not implemented.

This report makes no claim that the feature is complete. Section 4 lists what
remains.

The [repair report](current-system-repair-report.md) covers the repair phase.
[Configurable recurrence](../methods/configurable-recurrence.md) is the
semantic contract.

## 1. What was built

| Layer | Location | Tests |
| --- | --- | --- |
| Canonical contract | `backend/src/mars/domain/longitudinal.py` | Exercised through all the tests below |
| Duplicate classifier | `backend/src/mars/analytics/duplicate_detection.py` | `test_duplicate_detection.py` (12) |
| Recurrence engine | `backend/src/mars/analytics/positive_recurrence.py` | `test_positive_recurrence.py` (55) |
| Projections | `backend/src/mars/analytics/recurrence_views.py` | `test_recurrence_views.py` (13) |
| Live adapter | `backend/src/mars/integrations/dhis2/tracker/clinical_adapter.py` | `test_clinical_adapter.py` (16) |
| Stored adapter | `backend/src/mars/services/stored_encounter_adapter.py` | `test_clinical_adapter.py` (parity) |
| Live snapshot evaluation | `backend/src/mars/services/live_recurrence.py`, called from `integrations/dhis2/live_dashboard.py` | `test_live_dashboard.py` (19) |
| Staged retrieval `tracker-plan/2` | `integrations/dhis2/live_dashboard.py` | `test_live_dashboard.py` |
| Stored patient path | `services/patient_surveillance.py`, `api/v1/patients.py` (`offset`) | `test_patient_surveillance.py` (6) |
| API schema | `api/v1/schemas.py`: named interval fields, determination, definition, coverage. All additions are defaulted, so older snapshots still validate. | `contracts/openapi.json` regenerated; `--check` passes |
| Frontend | `features/recurrence/recurrenceLabels.ts`, `features/patients/PatientSurveillanceView.tsx`, `features/command-centre/CommandCentreView.tsx`, `patient-surveillance.css` | `frontend/tests/recurrence-labels.test.ts` |

The documents updated are:

- `docs/methods/configurable-recurrence.md`, which is new;
- `docs/data-dictionary/recurrence.md`;
- `docs/data-dictionary/episodes.md`;
- `docs/operations/live-synchronization.md`.

`signals-and-explanations.md` and `docs/security/live-sessions.md` were not
changed, because neither behaviour has changed yet.

### Deviations from the handoff's file map

- **The contract lives in `mars/domain/longitudinal.py`, not
  `analytics/longitudinal.py`.** The DHIS2 adapter may not import
  `mars.analytics`, a rule enforced by `tests/unit/test_module_boundaries.py`.
  The adapter already imports `mars.domain`.
- **`services/live_recurrence.py` is new.** It runs the engine for the live
  snapshot on the adapter's behalf, for the same reason.
- **The live snapshot applies the labelled exploratory preset (7/28/N=2/exclude).**
  Persisted definitions and runs do not exist yet.

## 2. The 30 requirements

| # | Requirement | Status | Code and tests |
| --- | --- | --- | --- |
| 1 | The unit is a confirmed positive encounter, and re-attendance alone does not qualify | Done | `positive_recurrence.py`; `test_two_attendances_with_one_positive_do_not_qualify`, `test_negative_context_cannot_bridge_an_anchored_window` |
| 2 | Same-day positives may be duplicates; the user chooses exclude, include or review; nothing is deleted | Partial | Three policies in the engine: `test_exclude_counts_one_same_day_positive_and_keeps_every_record`, `test_review_separately_counts_the_same_but_queues_the_group`, `test_include_counts_distinct_same_day_encounters_but_obeys_the_minimum_gap`. **No UI or API to choose the policy yet.** |
| 3 | Adjustable minimum interval, including values above 10 days | Partial | Definition field, validated: `test_invalid_definitions_are_refused`, `test_windows_beyond_ten_days_are_supported`. **No interface control.** |
| 4 | Adjustable maximum anchored window | Partial | `test_the_briefs_worked_examples` (days 3, 12 and 35) and `test_both_bounds_are_inclusive`. **No interface control.** |
| 5 | Change the definition from the dashboard, and Apply recalculates | Not done | Only the applied definition is shown (`data-testid="applied-definition"`). Needs the definition panel, runs and stored evidence. |
| 6 | At least N positives | Partial | `test_positive_count_thresholds` (N = 2, 3, 4). **No interface control.** |
| 7 | Duplicate detection is separate from recurrence | Done | The classifier runs before the sequence step. See `test_duplicate_detection.py`, and `test_near_duplicate_same_day_records_are_classified_probable`. |
| 8 | Drill-down for every qualifying patient | Partial | The live and stored timelines show dates, facilities, results, named intervals and the engine's explanation. **Diagnosis, fever and treatment are held in canonical evidence, but the snapshot row carries only tests, so the UI does not show them yet.** |
| 9 | Detailed treatment history | Partial | The medicine name, type and quantity are kept as recorded. Dose, frequency and duration are `not_mapped`, because no verified source field exists. See `test_recorded_medicine_is_never_upgraded_and_dose_is_never_invented`. **Not displayed.** |
| 10 | No drug-resistance inference | Done | `test_no_explanation_claims_resistance_or_treatment_failure`, frontend `labels an exploratory definition as not approved`, and `scripts/terminology_lint.py` |
| 11 | Whole care pathway | Partial | The contract carries diagnoses, observations, referrals, outcome and medicines, and the live adapter maps the visit fields. **There is no care-timeline component.** |
| 12 | Cross-facility journeys, never linked by name | Done | `cross_facility` on findings; `test_missing_stable_identity_never_links_encounters`, `test_different_patient_keys_are_never_merged`, `test_a_parent_claimed_by_two_people_never_merges_them` |
| 13 | Several analytical views at once | Partial | All of them are computed in `recurrence_views.py` and tested. **None is exposed through the API or UI yet**, beyond the list and the KPI. |
| 14 | Interval distribution with configurable bands | Partial | `DEFAULT_INTERVAL_BANDS` matches the brief, and `validate_bands` checks them. See `test_observed_intervals_keep_gaps_below_the_threshold_visible` and `test_values_in_a_gap_between_bands_go_to_an_explicit_bucket`. **No chart.** |
| 15 | Inspect same-day records | Partial | Groups carry their reasons, compared fields and confidence, and the snapshot counts `possible_duplicate_positive_groups`. **No review table or endpoint.** |
| 16 | Programme mode and exploratory mode | Not done | Only the preset exists, and it is labelled "not approved". |
| 17 | Versioned saved definitions | Not done | The definition is immutable and carries its engine version, but nothing persists it. |
| 18 | Comparison mode | Partial | The pure `compare()` gives overlap and refuses differing coverage: `test_comparison_overlap_is_deterministic`, `test_comparison_across_different_evidence_is_refused`. **No runs, API or UI.** |
| 19 | Aggregate flags stay active and cross-referenced | Partial | Aggregate assembly is unchanged and its tests pass. **No cross-reference to commodities, positivity or completeness yet.** |
| 20 | Data-quality statuses control entry to higher-level signals | Partial | All six flags exist: `test_valid_never_coexists_with_another_flag`, `test_missing_treatment_is_flagged_but_does_not_erase_the_positive`. **The signal engine does not consume them yet.** |
| 21 | Patient finding versus cluster versus signal | Not done | The output is a patient-level "case for review" only. |
| 22 | Explainability panel | Partial | The patient panel is "Why this patient is listed". **Explanations for signals, treatment between positives and linkage quality are not shown.** |
| 23 | Overview → facility → list → patient → investigation | Partial | Overview → patients → timeline works. **The facility filter and patient-origin investigation do not exist.** |
| 24 | Filters | Not done | Only the period and "meets the definition only". `CohortFilter` exists in `recurrence_views.py` (`test_unknown_sex_is_an_explicit_category`) but is not exposed. |
| 25 | Interval from positive to positive, including later intervals | Done | `test_days_0_9_23_expose_9_14_and_23_with_their_endpoints`; named API fields; UI columns "Qualifying interval" and "Positive-to-positive gaps" |
| 26 | Keep every positive encounter | Done | One `EncounterDecision` per encounter, with its reason; the observed count, eligible count and chain length are kept separately. See `test_frequency_counts_qualifying_patients_by_chain_length`. |
| 27 | Treatment attached to its encounter | Partial | `test_medicine_attaches_only_through_the_verified_parent`, `test_the_treatment_matrix_counts_transitions_not_patients`. **Not displayed.** |
| 28 | Treatment-response matrix | Partial | `treatment_matrix` counts transitions and keeps unlinked medicines as their own row: `test_an_unlinked_treatment_stays_an_explicit_row`. **No UI.** |
| 29 | Thresholds are parameters | Partial | The definition fields are gap, window, N, same-day policy, permitted methods, treatment requirement, quality exclusions, bands, duplicate rule version and engine version. `RecurrenceCeilings` types the resource limits. **No setting supplies those limits and nothing enforces them yet** (see remaining work, item 2). **Geographic scope and reporting period are query inputs, but no UI exposes them.** |
| 30 | Dynamic, explainable surveillance queries | Partial | The engine answers any valid definition deterministically and explains itself. **Interactive querying needs requirements 5, 16, 17 and 24.** |

## 3. Handoff section 10, mandatory cases

| Case | Status | Evidence |
| --- | --- | --- |
| 1. One positive among two attendances; negative context | Done | `test_two_attendances_with_one_positive_do_not_qualify`, `test_negative_context_cannot_bridge_an_anchored_window` |
| 2. Repeated retrieval; updated revision | Done | `test_the_latest_revision_wins_and_the_repeat_is_recorded`, `test_identical_revisions_resolve_the_same_way_every_time`, `test_repeated_retrieval_of_one_event_counts_once_and_is_recorded` |
| 3. RDT plus microscopy on one parent | Done | `test_rdt_and_microscopy_on_one_parent_are_one_positive_encounter`, `test_rdt_and_microscopy_sharing_a_parent_become_one_encounter_with_both_tests`, `test_two_tests_on_same_visit_are_not_repeat_positive_encounters` |
| 4. Same-day policies; include obeys the minimum gap | Done | The three policy tests, plus `test_include_with_a_zero_gap_lets_same_day_encounters_chain` |
| 5. UTC and local days; date-only values | Done | `test_a_timestamp_takes_its_day_in_the_reporting_timezone`, `test_a_date_only_value_is_kept_exactly_as_recorded`, `test_a_utc_evening_timestamp_shares_a_local_day_with_a_date_only_record` |
| 6. 0/9/23; inclusive bounds; N = 2/3/4; later anchor | Done | `test_days_0_9_23_expose_9_14_and_23_with_their_endpoints`, `test_both_bounds_are_inclusive`, `test_positive_count_thresholds`, `test_a_patient_can_qualify_on_a_later_anchor` |
| 7. 0/20/40 with max 28 and N = 3 | Done | `test_short_adjacent_gaps_do_not_make_an_anchored_three_chain` |
| 8. Cross-month lookback; partial is not zero | Done | `test_a_chain_starting_before_the_period_is_found_through_lookback`, `test_unobserved_lookback_is_indeterminate_never_zero`, `test_runner_reads_three_stages_over_the_recurrence_lookback` |
| 9. No linkage without identity; restricted facilities | Done at unit level | `test_missing_stable_identity_never_links_encounters`, `test_live_service_refuses_unbounded_or_unscoped_reads` |
| 10. Unmapped results, ambiguous dates, incomplete retrieval | Done | `test_an_unmapped_result_is_not_a_negative_and_makes_absence_indeterminate`, `test_a_naive_timestamp_is_a_date_quality_issue_not_a_guess`, `test_incomplete_retrieval_cannot_report_a_reliable_absence`, `test_partial_tracker_cannot_claim_complete_sync_or_zero_recurrence` |
| 11. Medicines, missing dose, prescribed versus dispensed | Done in the data layer | `test_medicine_attaches_only_through_the_verified_parent`, `test_prescribed_is_never_reported_as_dispensed`, `test_unretrieved_context_is_not_reported_as_no_treatment` |
| 12. Parity between live and stored adapters; history unchanged | Done | `test_live_and_stored_adapters_give_identical_findings`. The episode engine is untouched, and its tests pass. |
| 13. More than 5,000 encounters; page size | Done | `test_more_than_five_thousand_encounters_keep_every_patient`, `test_page_size_never_changes_who_is_counted`, `test_totals_stay_complete_beyond_five_thousand_encounters`, `test_repeat_patients_are_never_truncated` |
| 14. A/B determinism; differing coverage refused | Done in the pure layer | `test_comparison_overlap_is_deterministic`, `test_comparison_across_different_evidence_is_refused` |
| 15. Immutable runs; concurrency; no self-approval | Not done | Needs persistence and governance endpoints |
| 16. Scope and sensitivity on the new endpoints | Not done | The endpoints do not exist; existing endpoints keep their scope checks |
| 17. Logout, revoked rights, stale jobs, late Apply | Partial | Existing tests cover fenced stale workers. Apply does not exist. |
| 18. Failed refresh keeps the last good snapshot; completed retrieval does not seed | Done | `test_durable_live_sync.py`, including `test_checkpoint_lineage_does_not_depend_on_clock_resolution` |
| 19. Investigations from a patient finding and from a signal | Not done | |
| 20. Aggregates still work; zero versus unavailable | Partial | Unit and frontend tests pass, and the KPI is `unavailable` when coverage is partial. **Not rendered against live data.** |
| 21. No resistance claims | Done | Engine test, frontend test and terminology lint |
| 22. Full owner workflow, end to end | Not done | |

## 4. Remaining work

Listed in the handoff's execution order:

1. **Persistence.**
   - Migration `0028` after `0027_live_sync`, covering `ClinicalEvidenceDataset`,
     `RecurrenceDefinition` with its versions, `RecurrenceAnalysisRun` and
     `PatientRecurrenceFinding`, with restricted-role grants.
   - Encryption in `services/clinical_evidence_store.py`.
   - Fresh, upgrade and downgrade tests on a disposable database, never
     `mars_live`.
2. **The run service and `/api/v1/recurrence`.**
   - Idempotent runs that return 202.
   - Cursor pagination and projections.
   - Checks of current scope and sensitivity.
   - Resource ceilings. `RecurrenceCeilings.check()` exists, with default limits
     of 366 days, 12 positives and 366 days, but nothing calls it yet. Supply
     its limits from settings and call it in the run service.
   - `backend/tests/api/test_recurrence_api.py` and
     `backend/tests/integration/test_recurrence_analysis.py`.
3. **Governance.** Draft and transition endpoints over the existing governance
   service, respecting `CONFIGURATION_MANAGE` and `METHOD_APPROVE`.
4. **Investigations.** `patient_finding_id` as an alternative source, with a
   database constraint enforcing exactly one source.
5. **The frontend recurrence module.**
   - Components: the definition panel, analysis hook, results, care timeline,
     duplicate table, charts, treatment matrix, comparison and filters.
   - Tests: `recurrence-definition`, `patient-care-timeline`,
     `recurrence-comparison`, and the e2e `recurrence.spec.ts`.
6. **Signals.** Consume the quality flags, and separate a case for review from
   a cluster from a signal, all under a versioned method.
7. **Verification on an isolated stack.**
   - The integration suite, migration tests and Playwright.
   - A controlled cutover.
   - An authorised live workflow.
   - Re-verifying the stage, element and option codes against current Ministry
     metadata.

## 5. Live-source fields

| Field | Status |
| --- | --- |
| Laboratory type and result; the parent event; the visit's diagnosis, signs, malaria-treated flag, patient type, outcome and referral fields; medicine type and quantity | Named in `config/dhis2/pader-live-v1.json` and used by the adapter. **Not re-verified against current metadata in this session.** |
| Medicine formulation, dose, frequency and duration; dispensing status and date | `not_mapped`: no verified source field |
| Age and sex on the live path | Not mapped. On the stored path, age falls into `under_5`, `5_to_14` or `15_and_over`, and unknown stays unknown. |
| OPD register number | Deliberately never carried (`test_the_opd_register_number_is_never_carried`) |
