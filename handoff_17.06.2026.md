# Handoff: CGST Rules/Act Version History

Date: 2026-06-19 (Phase 3 backlog slice verified)
**UPDATED 2026-06-20: Phase 3 gap closure sprint — see corrected numbers below.**
Workspace: `/home/shikhar/openclaw-workspace/Projects/Git_for_Law`

## User Goal

Build an event-sourced legal version-history system for Indian legal text (CGST Rules, Act, Forms) so that at any point in time we can know what the legal statutes were in force with certainty good enough for litigation.

The architecture is event-sourcing: source archive → event ledger → validated component versions → reconstruction/compare → reconciliation against independent checkpoint. The system refuses clean answers when coverage is incomplete — this is the correct posture for litigation use.

Do not mark the overall goal complete. Phase 0 (proof point) delivered baseline decontamination + confidence tiers. Phases 1-4 remain: Act baseline purity, unified form/table/schedule operations, evidence bundles, LLM/vision extraction at scale.

## Phase 3 Current Verified State (19 June 2026)

Phase 3 is now an explicit backlog and remediation track on top of the Phase 2
post-hoc recovery. The current pass kept unresolved rows as explicit gaps,
improved the Rules materializer so the unresolved Rule 164 context-recovered row
does not become an `apply_failed` gap, split the Forms backlog into true
pending-baseline versus suspected over-routing buckets, added the Act audit
summary into the Phase 3 backlog, and regenerated the milestone HTML report.

Do not mark the overall goal complete. Remaining work is tracked in:

```text
derived/version_history/phase3_backlog.json
derived/version_history/review_report.html
```

### Rules Materialization (CORRECTED 2026-06-20)

> **The numbers in the original section below (274 applied, 75 gaps, 0 apply_failed)
> are STALE.** Current verified state after Phase 3 gap closure:

```text
event_count:                         1394
applied_count:                        316
coverage_gap_count:                    38
apply_failed_count:                    20
already_reflected_count:              12
conflict_count:                        0
forms_lane_routed_count:               27
metadata_only_count:                   58
context_recovered_count:              559
forms_lane_pending_baseline_count:   900
context_unresolved_count:              10
rules_table_lane_routed_count:         15
```

Gap breakdown:
```text
apply_failed:                          20
  (14 substitution, 4 anchor, 1 payload, 1 other)
event_status_not_validated:            10
target_not_in_store:                    7
partial_apply:                          2
```

**"0 apply failures" is no longer accurate — 20 apply_failed gaps remain.**

### OLD Rules Materialization (STALE — kept for history)

```text
event_count:                         1394
applied_count:                        274
coverage_gap_count:                    75
apply_failed_count:                     0
forms_lane_routed_count:                6
metadata_only_count:                    8
context_recovered_count:              560
forms_lane_pending_baseline_count:    882
context_unresolved_count:              88
rules_table_lane_routed_count:         31
```

All remaining Rules gaps are explicit review gaps:

```text
event_status_not_validated:            75
```

The previous noisy Rule 164 apply failure is now retained as an explicit
`event_status_not_validated` gap until its recovered target/anchor can be
deterministically validated.

### Phase 3 Backlog Summary

```text
rules_coverage_gap_count:                         75
forms_pending_baseline_count:                    790
forms_true_pending_baseline_count:               679
forms_lane_overrouted_count:                     111
forms_pending_baseline_unclassified_count:       223
portal_missing_source_notification_count:          0
portal_source_present_unlinked_notification_count: 28
act_coverage_gap_count:                           79
item_count:                                       11
```

Forms unclassified bucket split:

```text
form_slug_unresolved:                  4
suspected_rule_text_overrouted:       25
suspected_table_or_formula_overrouted: 86
unclassified:                        108
```

Top Phase 3 items:

```text
rules-explicit-gaps:                  75
portal-source-present-unlinked:       28
forms-lane-overrouted-rules:         111
forms-unclassified-pending-baseline: 112
act-pipeline-backlog:                 79
```

Top-five form baseline queue remains:

```text
gst-rfd-01: 19
gst-drc-03: 24
gstr-1:     98
gst-drc-01:  7
gst-tran-1: 18
```

### Confidence And Portal Completeness

Confidence tiers (CORRECTED 2026-06-20):

```text
Tier A:  46
Tier B: 438
Tier C: 145
Tier D: 170
Total:  799
```

OLD confidence tiers (STALE):

```text
Tier A:  46
Tier B: 444
Tier C: 125
Tier D: 170
```

Portal completeness:

```text
rule_count:                                  129
missing_source_notification_count:             0
source_present_unlinked_notification_count:   28
external_reference_notification_count:       123
```

### Act Track (CORRECTED 2026-06-20)

```text
event_count:                         178
applied_count:                        96
coverage_gap_count:                   62
conflict_count:                        0
metadata_only_classified_count:       10
schedule_lane_pending_baseline_count:  4
act_out_of_scope_count:                4
```

### OLD Act Track (STALE)

```text
event_count:                         178
applied_count:                        81
coverage_gap_count:                   79
metadata_only_classified_count:       10
schedule_lane_pending_baseline_count:  4
act_out_of_scope_count:                4
Act tiers: A=1, C=115, D=75
```

### Verified Commands

```bash
python3 main.py version materialize
python3 main.py version phase3-backlog
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

Test result (CORRECTED 2026-06-20):

```text
full canonical suite: 306 passed
```

OLD test result (STALE):

Regenerated milestone report:

```text
derived/version_history/review_report.html
```

## Phase 2 Verified State (19 June 2026)

Phase 2 post-hoc recovery is implemented and verified. This pass preserved event IDs, recovered full-notification context for Rules work-ID events, moved form/table metadata out of Rules text materialization, produced candidate-only LLM re-extraction artifacts, applied deterministic corrigenda patches, refreshed Forms pending-baseline routing, and regenerated the HTML report only at the milestone.

### Context Recovery

```text
input_count:                         1386
context_recovered_count:              159
forms_lane_pending_baseline_count:    856
metadata_only_count:                    2
rules_table_lane_count:                31
baseline_source_only_count:             0
context_unresolved_count:              51
deterministic_reextraction_count:      37
llm_reextraction_candidate_count:       9
```

Artifacts:

```text
derived/version_history/context_recovery_decisions.json
derived/version_history/context_recovery_report.json
derived/version_history/llm_reextraction_candidates.json
derived/version_history/llm_reextraction_report.json
```

### Rules Materialization After Recovery

```text
event_count:                         1392
applied_count:                        211
coverage_gap_count:                   135
forms_lane_routed_count:                6
metadata_only_count:                    8
context_recovered_count:              567
forms_lane_pending_baseline_count:    882
context_unresolved_count:              88
```

The gap count is now lower because unsupported form/table rows are explicitly routed or classified instead of inflating Rules text gaps.

### Forms Lane

`derived/version_history/form_registry.json` now tracks the top-five pending baselines:

```text
gst-rfd-01
gst-drc-03
gstr-1
gst-drc-01
gst-tran-1
```

Forms materialization:

```text
event_count:                         284
applied_count:                        71
statement_applied_count:              39
coverage_gap_count:                 1001
forms_lane_pending_baseline_count:   790
version_count:                       250
versions_with_text:                  215
```

RFD-01 statement materialization remains active; unsupported form rows stay in `forms_lane_pending_baseline` until structured baselines exist.

### Corrigenda

```text
corrigendum_count:                   34
corrigenda_with_corrections_count:    7
applied_event_count:                  2
```

Artifact:

```text
derived/version_history/corrigendum_application_report.json
```

Corrected events preserve original event provenance plus corrigendum event/source-span provenance in `payload.corrigendum_applications`.

### Confidence And Portal Completeness

```text
Tier A:  45
Tier B: 439
Tier C: 121
Tier D: 173
```

Portal completeness:

```text
rule_count:                                  129
missing_source_notification_count:            12
source_present_unlinked_notification_count:   50
external_reference_notification_count:        12
```

### Act Track

The Act audit remains separate from Rules recovery.

```text
event_count:             178
applied_count:            81
coverage_gap_count:       79
baseline_blocked_count:    0
act_component_count:     191
Act tiers: A=1, C=115, D=75
```

Artifact:

```text
derived/version_history/cgst-act-2017/act_pipeline_audit.json
```

### Verified Commands

```bash
python3 main.py version context-recovery
python3 main.py version corrigendum-application
python3 main.py version materialize
python3 main.py version materialize-forms
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version act-audit
python3 main.py version html-report
pytest -q tests/test_canonical_corpus.py -k "context_recovery or codex_review_decisions"
pytest -q tests/test_canonical_corpus.py
```

Test result:

```text
focused recovery/codex selector: 43 passed
full canonical suite:           253 passed
```

The regenerated HTML review report is:

```text
derived/version_history/review_report.html
```

## Current Verified State (as of 18 June 2026, Part 4)

### Rules Materialization

```text
applied_count:                  251
coverage_gap_count:            1029
conflict_count:                   1
blocked_baseline_component_count: 0  (was 28 — FIXED via decontamination)
baseline_component_count:       552  (was 578 — 26 post-2017 insertions dropped)
total events:                  1386  (231 validated, 1050 needs_review, 105 rejected)
```

### Act Materialization

```text
applied_count:    75
coverage_gap_count: 101
conflict_count:     0
total events:     176
```

### Forms Materialization

```text
version_count:           210
versions_with_text:      191 (91%)
applied_count:           81
coverage_gap_count:     255
```

### Confidence Tiers (NEW — `confidence_tiers.json`)

```text
Tier A (Court-ready):      52 / 756  (6.9%)
Tier B (High confidence): 416 / 756  (55.0%)
Tier C (Advisory):        131 / 756  (17.3%)
Tier D (Do not cite):     157 / 756  (20.8%)
```

### Reconciliation (Rules, checkpoint 2022-12-26)

```text
matched:      17
format_only:  42
mismatched:  133
missing:      98
correct:      59/290 (20.3%)
```

### Tests

```bash
pytest tests/test_canonical_corpus.py -q
# 179 passed
```

### CLI Commands

```bash
# Full pipeline (correct sequence)
python3 main.py version build-baseline --target-work /in/union/rules/cgst-rules-2017 --registry data/Law/statute_identity_registry.json
python3 main.py version materialize --target-work /in/union/rules/cgst-rules-2017 --events derived/version_history/amendment_events_reviewed.jsonl --output-dir derived/version_history/cgst-rules-2017 --registry data/Law/statute_identity_registry.json
python3 main.py version reconcile --target-work /in/union/rules/cgst-rules-2017 --checkpoint-path corpus/in/union/rules/cgst-rules-2017/rules.xml --checkpoint-date 2022-12-26 --output derived/version_history/cgst-rules-2017/reconciliation_report.json --registry data/Law/statute_identity_registry.json --version-dir derived/version_history/cgst-rules-2017
python3 main.py version confidence-tiers --version-dir derived/version_history/cgst-rules-2017 --amendment-events derived/version_history/amendment_events_reviewed.jsonl --baseline-components derived/version_history/baselines/cgst-rules-2017/2017-06-19/baseline_components.jsonl --output derived/version_history/confidence_tiers.json
```

HTML review board:

```text
http://127.0.0.1:8879/review_report.html
```

If the server is down:

```bash
cd derived/version_history && python3 -m http.server 8879 --bind 0.0.0.0
```

## Baseline Status (RESOLVED)

The Rules baseline PDF (`data/Law/base_laws/cgst-rules-2017-part-a-rules.pdf`) is a **2021 CBIC consolidated edition**, not the original 2017 Gazette. This was the root cause of all baseline contamination:

- PDF metadata: `CreationDate: D:20210521104949+05'30'` (21 May 2021)
- 330 `vide Notf no. X/YYYY-CT` annotations baked into rule text
- 172 `Inserted vide Notf` footnotes, 78 `Substituted vide Notf` footnotes

**This is now fixed** via `decontaminate_baseline_text()` in `baselines.py`:

1. Editorial annotations stripped (`Inserted/Substituted/Omitted vide Notf...`)
2. Bracketed post-2017 insertions removed (`[(2A)...]33`)
3. Stray footnote numbers cleaned
4. 26 post-2017 rule insertions (96A, 138A-E, 9B, etc.) dropped from baseline
5. Result: **28 blocked → 0 blocked**, 213 components decontaminated

The chronological event-sourcing model is correctly implemented. The materializer applies events in date order with multi-pass retry for sequencing dependencies. The only issue was the source data quality, now resolved for Rules.

## Latest Continuation Update

The CBIC original Rules notification chunks were evaluated and used as conservative repair sources:

```text
data/Law/cbic_tax_portal/notifications/3-2017-central-tax-notifying-the-cgst-rules-2017-on-registration-and-composition_1000872.json
data/Law/cbic_tax_portal/notifications/10-2017-central-tax-seeks-to-amend-cgst-rules-notification-no-3-2017-central-tax_1000865.json
data/Law/cbic_tax_portal/notifications/15-2017-central-tax-amending-cgst-rules-notification-10-2017-ct-dt-28-06-2017_1000860.json
```

Findings:

- The PDF has `88` pages and clean original text for rules `1` through `26`.
- Notification `10/2017` provides clean original text for rules `27` through `138`.
- Notification `15/2017` provides clean original text for rules `139` through `162`.
- It contains `24. Migration...` and `25. Physical verification...` without the later annotations found in the current base PDF.
- Checked contamination markers such as `Inserted vide`, `Page 148 of 164`, and `0308 Aquatic` were absent in this notification text.
- Each source is bounded by rule range, so form/table labels inside the PDFs cannot overwrite unrelated rule IDs.

Implemented behavior:

- `src/legal_corpus/baselines.py` now parses Rules text from both file PDF and CBIC notification JSON PDF payloads.
- `DEFAULT_RULES_REPAIR_SOURCES` defines bounded repair ranges:
  - `3/2017`: rules `1-26`
  - `10/2017`: rules `27-138`
  - `15/2017`: rules `139-162`
- `repair_rules_baseline_from_source` replaces only source-covered components inside the configured range.
- Replacement text must pass the existing baseline quality checks; otherwise it is rejected.
- Every replacement is recorded in `baseline_reconciliation.json` under `source_repairs`.
- `baseline_components.jsonl` and `baseline.xml` now include `source_basis`.

Outcome (updated Part 4):

- Source-priority repairs applied: `385`
- Blocked baseline components: `28 → 0` (decontamination added in Part 4)
- Baseline components: `578 → 552` (26 post-2017 insertions dropped)
- Rules materialized events: `251`
- Rules coverage gaps: `1029`
- Open legal-review items: `0`
- Confidence Tier A (court-ready): `52` components

## Latest Parser-Support Pass

After the multi-source baseline repair, the largest remaining bucket was `parser_support_required`.

Change made:

- `src/legal_corpus/review_decisions.py`
  - `_inherited_rule_context` now recognizes context headers of the form:
    - `with effect from ..., in rule X -`
    - `with retrospective effect from ..., in rule X -`
  - This allowed additional split amendment fragments to inherit the correct rule label and be reviewed by the existing Codex decision logic.

Outcome:

- Codex decisions are now `98` after stale parent-span approvals were replaced by stricter detached-component handling.
- Promoted review decisions are now `101`.
- Rules applied events increased: `190 -> 191`.
- Rules coverage gaps are now `1174`, including explicit partial-apply warnings for detached Rule 96(10) versions.
- Review queue remains closed: `open_count = 0`

Additional inherited-context insertion support:

- `src/legal_corpus/review_decisions.py`
  - Added inherited effective-date context from source headers like `with effect from ..., in rule X -`.
  - Extended inherited rule context to classified-but-unresolved events, not only `UNKNOWN`.
  - Fixed `INSERT_CHILD` review so embedded subrule anchors can use `source_backed_parent_span_insert_child`.
- `src/legal_corpus/version_snapshots.py`
  - Avoids duplicated standalone text for parent-span inserted subrules where content already begins with `(label)`.

Rule 96 status:

- `evt_cbic_ce16b79c3bda7398` now materializes `/in/union/rules/cgst-rules-2017/rule/96/subrule/9`.
- Its `valid_from` is `2017-10-23`, inherited from the surrounding source context.
- Source basis is preserved:
  - source document: `/in/union/notifications/cbic/central-tax/2017/75-2017`
  - source span hash: `707440706d94f7a5d74a9ddbfbf87486606b138e665d13a6c5feba8f687ad2ff`
- Rule 96(10) now has detached first-class component versions:
  - `2017-10-23`: `evt_cbic_e32d6c82385a2b56`
  - `2018-09-04`: `evt_cbic_4b97cd62a67e4be1`
  - `2018-10-09`: `evt_cbic_f71bca8f1cfeffcf`
  - `2024-10-08`: `evt_cbic_44d6be8f142fc2e7`, text `[Omitted]`
- These four Rule 96(10) events are classified as `partial_materialized`, not clean `materialized`, because parent Rule 96 lacks a unique top-level `(10)` span. Coverage remains incomplete via `partial_apply: parent_subrule_span_missing` gaps.

Remaining large blocker:

- Most `parser_support_required` items are still `event_status_not_validated`, often due to unresolved anchors, target inference, or compound amendment blocks.
- Rule 96 parent snapshot still has incomplete structure for subrule 10. Do not claim full parent Rule 96 coverage until a trusted source or parser repair can place subrule 10 into the parent text without relying on section-reference false positives.

## Recent Code Changes

### `src/legal_corpus/baselines.py` (Updated Part 4)

- **NEW: `decontaminate_baseline_text()`** — strips editorial annotations from consolidated-PDF baseline:
  - Annotation blocks (`Inserted/Substituted/Omitted vide Notf...`)
  - Bracketed post-2017 insertions (`[(2A)...]33`, `[(4A)...]`)
  - Stray footnote numbers, page headers, "wef" markers
  - Unicode artefact cleanup
- **NEW: `decontaminate_baseline()`** — applies decontamination + drops post-2017 insertions
- **NEW: `_POST_2017_RULE_PREFIXES`** — registry of rules not in original 2017 (96A, 138A-E, 9B, etc.)
- **NEW: `_is_post_2017_rule_insertion()` / `_is_post_2017_subrule_insertion()`**
- **NEW: Stray page number cleanup** — `; 19 (d)` → `; (d)` between list items
- Rule heading parser accepts compact headings (`24.Migration...`) and bracketed headings (`25[Physical verification...]`)
- Baseline quality blockers: `table_or_tariff_row_misparsed_as_rule`, `post_2017_amendment_annotation_in_baseline`, `notification_footnote_in_baseline`, `embedded_later_subrule_marker_in_baseline`
- `build_baseline()` now calls `decontaminate_baseline()` after repair sources, before quality-flagging
- Exported: `baseline_component_quality_flags`, `apply_baseline_quality_flags`, `decontaminate_baseline`, `decontaminate_baseline_text`

### `src/legal_corpus/version_snapshots.py` (Updated Part 4)

- **NEW: `_ensure_structural_targets()`** — pre-creates components for SUBSTITUTE events with `structural_text` targeting non-existent components (handles post-2017 rules whose INSERT events are missing)
- `materialize_versions()` calls `_ensure_structural_targets()` after baseline load, before apply loop
- Added `_baseline_blocked_components(baseline_dir)`, `_blocked_baseline_target(event, blocked_components)`
- `materialize_versions` reads blocked baseline components and skips events whose target/anchor/parent is blocked
- Manifest includes `blocked_baseline_component_count`, `blocked_baseline_components`
- Parent-span subrule substitutes create detached first-class component versions when source-backed and validated
- Detached versions emit `partial_apply: parent_subrule_span_missing` coverage gaps

### `src/legal_corpus/confidence_tiers.py` (NEW — Part 4)

- Per-component tier computation (A/B/C/D) for litigation readiness
- Inputs: node_versions, coverage_gaps, reconciliation_report, amendment_events, baseline_components
- Output: `confidence_tiers.json` with per-component tier + reasoning
- CLI: `python3 main.py version confidence-tiers`

### `src/legal_corpus/review_completion.py`

- Terminal states: `baseline_blocked`, `partial_materialized`, `commencement_blocked`, `corrigendum_rescind_deferred`, `forms_lane_resolved`, `llm_extraction_required`, `materialized`, `parser_support_required`, `rejected_non_rules_text`
- `_coverage_reason_index` maps gap reasons to terminal states

### `src/legal_corpus/version_history_report.py`

- HTML summary includes `Blocked baseline`
- Board has pagination, full-text expansion, decision controls, Mermaid diagrams, `Approved by Codex` audit output

### `main.py` (Updated Part 4)

- **NEW: `confidence-tiers` subcommand** under `version` group
- `materialize` and `materialize-forms` default to `amendment_events_reviewed.jsonl`
- Full CLI group: `build-baseline`, `compile-events`, `backfill-missing-anchors`, `merge-events`, `triage-review`, `auto/codex/dependency-review-decisions`, `apply-review-decisions`, `materialize`, `materialize-forms`, `reconcile`, `complete-review`, `html-report`, `confidence-tiers`, `fetch-consolidated`, `alias-checkpoint`

### `tests/test_canonical_corpus.py`

- 179 tests passing
- Key tests: baseline reconciliation, materializer detached components, parent-span substitute, same-date conflicts, structural target creation

## Current Implementation Surface

Core files involved in this feature include:

- `main.py` — CLI entry point with `version` command group
- `src/legal_corpus/identity_registry.py` — statute identity registry
- `src/legal_corpus/amendment_events.py` — Rules event compiler + LLM extraction
- `src/legal_corpus/act_amendment_events.py` — Act event compiler
- `src/legal_corpus/version_snapshots.py` — Rules/Act materializer
- `src/legal_corpus/form_version_snapshots.py` — Forms materializer
- `src/legal_corpus/version_compare.py` — diff between point-in-time versions
- `src/legal_corpus/version_reconstruct.py` — point-in-time reconstruction
- `src/legal_corpus/baselines.py` — baseline builder + **decontamination**
- `src/legal_corpus/confidence_tiers.py` — **NEW: A/B/C/D tier computation**
- `src/legal_corpus/omlx_client.py` — LMS (LM Studio) client
- `src/legal_corpus/review_triage.py` — review triage
- `src/legal_corpus/review_decisions.py` — review decision application
- `src/legal_corpus/review_completion.py` — terminal state classification
- `src/legal_corpus/version_history_report.py` — HTML report generator
- `src/legal_corpus/reconciliation.py` — checkpoint reconciliation
- `src/legal_corpus/missing_anchor_backfill.py` — anchor backfill
- `src/legal_corpus/consolidated_fetch.py` — checkpoint fetcher
- `src/legal_corpus/component_spans.py` — sub-rule span finder
- `src/legal_corpus/event_ledgers.py` — event merging
- `src/legal_corpus/renderer.py` — XML rendering (`render_rule`, `write_xml`)
- `src/anchor_resolver.py` — SPLICE anchor resolution
- `src/schemas/amendment_event.schema.json` — event schema
- `data/Law/statute_identity_registry.json` — curated work registry

Many of these files are untracked. Do not delete or revert them.

## OMLX Extraction + Detached-Component Fix + GLM Review Session

### Bug Fixes

1. **`review_decisions.py:846`** — Fixed `NameError: source_archive_root` in `_safe_insert_rule`. Auto-review-decisions was crashing silently because the function body referenced `source_archive_root` instead of the actual parameter `event`.

2. **`version_snapshots.py:659,667`** — Fixed detached-component entry for SUBSTITUTE. The `_apply_parent_subrule_substitute` path was only triggered by `apply_to_parent_subrule_span` events. Events with `allow_detached_component_version: True` were silently ignored, producing no materialization and no gap. Added `allow_detached_component_version` to the entry condition for both the target-present and target-None paths.

3. **`version_snapshots.py:848`** — Fixed `detached_omit_allowed` by removing the `component_id in component_paths` requirement. This requirement blocked detached omits for components that never existed in the baseline (which is exactly when detached handling is needed).

4. **`review_decisions.py:1984`** — Added `component_id` handling to the SPLICE promote block. Review-approved SPLICE events were promoted but their `component_id` was not set, causing them to fail materializer's work_id filtering.

5. **`review_completion.py:329`** — Added `same_effective_date_conflict` gap classification as `parser_support_required`. Events with this gap reason were falling through to `requires_legal_review` because the gap reason didn't match any apply_failed pattern and the events weren't in triage (they were already validated before the triage run).

### OMLX Extraction Passes

Ran two `compile-events --use-llm` passes:

- **Pass 1:** 96 new OMLX calls. Cache grew 1229 → 1325.
- **Pass 2:** 49 new OMLX calls. Cache grew 1325 → 1458.
- `llm_extraction_required` reduced: 40 → 27 → 26.
- 19 items remain `llm_limit_not_attempted` (higher `--llm-limit` or targeted extraction would help).

### GLM-5.1 Review Decisions

Created `derived/version_history/glm_review_decisions.json` with 2 SPLICE promotions:

- `evt_cbic_1559765ee5fcbe1e`: rule/124/subrule/5, SPLICE, notification 13/2018-Central Tax.
- `evt_cbic_1208e1c3783db8c8`: rule/54/subrule/2, SPLICE, notification 46/2017-Central Tax.

Both verified: target exists in baseline, anchor text confirmed against source evidence, source-backed payloads. Both successfully materialized after the detached-component and SPLICE fixes.

### Pipeline Regeneration Order (correct sequence)

```bash
# 1. Compile events (with OMLX extraction)
python3 main.py version compile-events \
  --target-work /in/union/rules/cgst-rules-2017 \
  --registry data/Law/statute_identity_registry.json \
  --source data/Law/cbic_tax_portal/notifications \
  --output derived/version_history/amendment_events.jsonl \
  --use-llm --extract-pdf-text --llm-concurrency 8 --llm-limit 200

# 2. Backfill missing anchors
python3 main.py version backfill-missing-anchors \
  --events derived/version_history/amendment_events.jsonl \
  --output derived/version_history/amendment_events_backfill.jsonl

# 3. Merge
python3 -c "
import json
from legal_corpus.missing_anchor_backfill import merge_backfill_events
merge_backfill_events(
    'derived/version_history/amendment_events.jsonl',
    'derived/version_history/amendment_events_backfill.jsonl',
    'derived/version_history/amendment_events_with_backfill.jsonl'
)
"

# 4. Triage (MUST run on with_backfill, not pre-merge)
python3 main.py version triage-review \
  --events derived/version_history/amendment_events_with_backfill.jsonl \
  --output derived/version_history/review_triage.json

# 5. Generate decisions (codex on with_backfill for max coverage)
python3 main.py version codex-review-decisions \
  --events derived/version_history/amendment_events_with_backfill.jsonl \
  --node-versions derived/version_history/cgst-rules-2017/node_versions.jsonl \
  --output derived/version_history/codex_review_decisions.json

python3 main.py version auto-review-decisions \
  --events derived/version_history/amendment_events_with_backfill.jsonl \
  --output derived/version_history/auto_review_decisions.json

# 6. Apply decisions (all decision files including glm)
python3 main.py version apply-review-decisions \
  --events derived/version_history/amendment_events_with_backfill.jsonl \
  --decisions derived/version_history/codex_review_decisions.json \
  --decisions derived/version_history/auto_review_decisions.json \
  --decisions derived/version_history/glm_review_decisions.json \
  --output derived/version_history/amendment_events_reviewed.jsonl

# 7. Materialize
python3 main.py version materialize \
  --target-work /in/union/rules/cgst-rules-2017 \
  --events derived/version_history/amendment_events_reviewed.jsonl \
  --registry data/Law/statute_identity_registry.json \
  --corpus-dir corpus \
  --output-dir derived/version_history/cgst-rules-2017

# 8. Forms
python3 main.py version materialize-forms \
  --events derived/version_history/amendment_events_reviewed.jsonl \
  --corpus-dir corpus \
  --output-dir derived/version_history/forms

# 9. Complete review
python3 main.py version complete-review

# 10. HTML report
python3 main.py version html-report \
  --output derived/version_history/review_report.html
```

**Key insight:** Triage must run on `amendment_events_with_backfill.jsonl` (post-merge), not `amendment_events.jsonl` (pre-merge). Otherwise, backfill-merged events won't be in the triage and may fall through to `requires_legal_review` in complete-review.

### Coverage Gap Analysis

All 7 "Substitution text not found" gaps are sequencing dependencies (prior amendments not yet applied to the base text), NOT normalization bugs. The materializer's `_normalized_match_spans` collapses whitespace and does case-insensitive comparison but does NOT handle unicode normalization (smart quotes, em-dashes). This is acceptable — these are genuine sequencing issues.

## Phase 1: LMS Migration + Extraction + Conflict Fix

### LMS Endpoint Migration

OMLX endpoint (`100.79.90.123:8000`) is down. Migrated to LM Studio at `127.0.0.1:1234`.

Changes in `src/legal_corpus/omlx_client.py`:
- `DEFAULT_BASE_URL` → `http://127.0.0.1:1234/v1`
- `DEFAULT_MODEL` → `qwopus3.6-27b-v2-mtp`
- Removed `response_format: {"type": "json_object"}` from `chat_json` payload — LMS rejects this (only supports `text` or `json_schema`)
- Removed `chat_template_kwargs` and `enable_thinking` — LMS ignores them
- Added `.strip()` to response content before `json.loads()` — LMS prepends whitespace to content

`.env` updated:
- `OMLX_BASE_URL=http://127.0.0.1:1234/v1`
- `OMLX_MODEL=qwopus3.6-27b-v2-mtp`
- `OMLX_API_KEY=lm-studio` (LMS accepts any dummy key)

Smoke test passed: correct model list, valid JSON completion.

LMS behavioral note: `/no_think` prefix does NOT suppress reasoning on LMS (model still produces `reasoning_content` tokens). This wastes ~40 tokens per call but doesn't affect output quality — valid JSON appears in `content` field with `text` response format.

### OMLX Extraction Final Pass

Ran `compile-events --use-llm --llm-limit 50`:
- 15 new LLM calls succeeded, 5 failed (timeouts), 20 total scheduled
- Cache grew 1508 → 1528
- `llm_extraction_required` remains at 26 (new candidates need deterministic validation)

### Substitution Text Failure Analysis (M2c — not needed)

Investigated all 7 "Substitution text not found" gaps. All `old_text` strings are pure ASCII — NO unicode normalization issues found. All failures are sequencing dependencies: the `old_text` was introduced by a prior amendment not yet applied to the base. M2c is not needed.

### Conflict Resolution: Document ID Normalization

Fixed false conflict between GLM SPLICE event (`evt_cbic_1208e1c3783db8c8`) and backfill SUBSTITUTE event (`evt_cbic_8739d092c4f28ae8`) for rule/54/subrule/2. Both are legitimate amendments from notification 45/2017 (SPLICE inserts new text, SUBSTITUTE changes "tax invoice" → "consolidated tax invoice"). The conflict was triggered because the events had different document_id formats: `45-2017` (from CBIC JSON compiler) vs `45-2017-central-tax` (from corpus XML backfill).

Fix in `version_snapshots.py`:
- Added `_normalize_document_id()` that strips `-central-tax` / `-union-tax` / `-integrated-tax` suffixes
- `_same_source_ordered_text_edits_allowed` now normalizes document_ids before comparison
- Both events now correctly apply without conflict

### Applied Count Regression Note

Applied count dropped from 195 → 188 after re-compilation. Root cause: compile-events changed some events' content (LLM extraction updated candidates), which changed some event IDs (content-stable hashes). A few previously-validated events lost their validated status. This is a known non-idempotency issue with pipeline regeneration. The 7-event regression is in `parser_support_required` (128, up from 123). The review queue remains closed.

## M2a: Sequencing Dependency Resolution

### Infrastructure Fix

Expanded `_retryable_apply_error` in `version_snapshots.py` to include text-matching failures:
- `"Substitution text not found"` — SUBSTITUTE old_text not in component (prior amendment may introduce it)
- `"Anchor not found:"` — SPLICE/INSERT anchor text not in component

These are now retried in the multi-pass materializer loop (same mechanism as "Anchor component missing"). If a prior amendment is applied in the same pass, the dependent event succeeds on retry. If no progress is made, the event becomes a coverage gap (no infinite loop).

### Analysis: Why No Immediate Wins

Investigated all 12 apply_failed events (7 substitution + 5 anchor). None can benefit from the retry fix yet:

**5 SPLICE failures — empty anchor_text:**
All have `anchor_text: ""` in payload. The compiler failed to extract the anchor from the source excerpt. These are compiler/parsing bugs (M2b domain):
- `evt_cbic_7ee72407f76da607` (rule/12): insert "deduct or" after [empty anchor]
- `evt_cbic_044c4d55cb7fb986` (rule/25): insert "or due to not opting for Aadhaar authentication" after [empty anchor]
- `evt_cbic_77586a8d25f96dd2` (rule/132): insert "Authority," before [empty anchor]
- `evt_cbic_2e9861790075092c` (rule/127): insert "day" after [empty anchor]
- `evt_cbic_f9f1f96fd0f0e679` (rule/36): insert [empty] after [empty anchor]

**7 SUBSTITUTE failures — multi-level chain dependencies:**
All prior amendments in the chain are `needs_review` (not validated). Even promoting the direct prior would fail because IT also has a sequencing dependency on an even earlier unvalidated amendment:
- `evt_cbic_64b4c848b468ead6` (rule/44): "sub-rules (2) and (3)" — introduced by unknown earlier amendment
- `evt_cbic_a55addc1351a6797` (rule/89): "third proviso" — 36-event chain, most needs_review
- `evt_cbic_659de6170f492d5f` (rule/83/3): "eighteen months" — prior substitutes "one year" which is also not in baseline
- `evt_cbic_001cf1c32bf91009` (rule/129/6): "three" — wrong payload (new_text is full phrase, not "six")
- `evt_cbic_773433d89f5fe5fa` (rule/117/4): "31st January, 2020" — prior substitutes "30th April, 2019" which is also not in baseline
- `evt_cbic_8e62222199d9ad8c` (rule/26): "31st day of May, 2021" — 2 prior needs_review events
- `evt_cbic_eb18afb06f304825` (rule/46): long recipient address text — 10-event chain, most needs_review

### Conclusion

The retry infrastructure is correct and will produce results once M2b (compound amendment splitting) validates prior chain events. No immediate coverage improvement from M2a alone.

## M2b: Conflict Detection + Review Gate Improvements + Reconciliation

### Document ID Normalization (event_ledgers.py + version_snapshots.py)

Events from the same CBIC notification can have different document_id formats depending on source representation:
- CBIC JSON compiler: `45-2017` (from notification metadata)
- Corpus XML backfill: `45-2017-central-tax` (from corpus file path)

Both `event_ledgers.py:flag_cross_source_conflicts` and `version_snapshots.py:_same_source_ordered_text_edits_allowed` now normalize document_ids by stripping `-central-tax` / `-union-tax` / `-integrated-tax` suffixes before comparison. This prevents false conflict flags between events from the same notification.

### Hard Review Reasons Relaxation (review_decisions.py)

Removed `same_effective_date_conflict` from `_hard_review_reasons` set (line 243). This was the GATEKEEPER that prevented codex/auto review from even attempting to promote events with this flag. The materializer already handles real conflicts via `_same_source_ordered_text_edits_allowed` and explicit coverage gaps — the compiler/merge step should not be the gatekeeper.

Impact: 31 events (12 SPLICE + 16 SUBSTITUTE + 2 INSERT_SIBLING + 1 COMMENCE) now pass the hard-reasons gate. They still fail individual codex decision function checks (text matching, source verification), but the infrastructure is correct for future improvements.

Important: `same_effective_date_conflict` was kept in all 7 `allowed_reasons` sets within individual decision functions (e.g., `_codex_insert_rule_decision`). These sets work inversely — they define which reasons are ACCEPTABLE. Removing from them would reject events that should be allowed.

### Sequencing Dependency Analysis (M2a/M2b)

All 12 apply_failed events analyzed:
- 5 SPLICE failures: All have empty `anchor_text` — the anchor was introduced by a prior unvalidated amendment
- 7 SUBSTITUTE failures: All have multi-level chain dependencies where ALL prior events are also unvalidated

None of the old_text/anchor strings exist in the 2017 baseline. The chains are:
- rule/44: "sub-rules (2) and (3)" — introduced by unknown earlier amendment
- rule/89: "third proviso" — 36-event chain, most needs_review
- rule/83/3: "eighteen months" — prior substitutes "one year" which is also not in baseline
- rule/129/6: "three" — wrong payload (new_text is full phrase instead of "six")
- rule/117/4: "31st January, 2020" — prior substitutes "30th April, 2019" also not in baseline
- rule/26: "31st day of May, 2021" — 2 prior needs_review events
- rule/46: long recipient address text — 10-event chain

Conclusion: Multi-pass materializer retry infrastructure is in place but produces no immediate wins. The real bottleneck is M2b compound splitting — improving the compiler to validate chain events.

### M7: Rules Reconciliation Against 2022 Checkpoint

Ran `reconcile()` against `taxinformation-2022-12-26` checkpoint (60 components):

| Category | Count | Notes |
|----------|-------|-------|
| Matched | 1 | rule/31 |
| Format-only mismatch | 9 | >99% similarity (rules 4,5,6,15,18,29,47,32a,34) |
| Substantive mismatch | 43 | Expected — most amendments not yet applied |
| Missing from reconstruction | 7 | Post-2017 rules (14a,16a,31b,31c,31d,47a,9a) |
| Priority review queue | 50 | Linked to coverage gaps |
| Commencement-blocked | 5 | rule/10a,10b,16a,23,46 |

Coverage: `incomplete` (expected at current materialization stage).

### 803 UNKNOWN Events Analysis

Categorized the 803 UNKNOWN-operation needs_review events:
- 93 `unknown_with_conflict`: Compound blocks with same_effective_date_conflict
- 64 `unknown_unsupported_op`: Includes form/table content misrouted to rules
- 43 `form_table_routed_to_rules`: Form GST REG/CMP amendments in rules pipeline
- Remaining: Various anchor/target resolution failures

The UNKNOWN events are mostly compound amendment blocks the compiler can't split, and form/table mutations that belong in the forms lane.

## M3: COMMENCE/RESCIND/CORRIGENDUM Materializer Operations

### Implementation Overview

Added full parsing, preprocessing, and classification support for the three special operations:

**New code in `version_snapshots.py`:**
- `SPECIAL_OPS = {"CORRIGENDUM", "RESCIND", "COMMENCE", "SUPERSEDE"}` alongside existing `SUPPORTED_OPS`
- `_parse_corrigendum(text)`: Extracts `refers_to_notifications`, `rule_references`, `corrections[]` (old_text→new_text pairs), and `targets_rules` flag from raw corrigendum text using curly-quote-aware regex
- `_parse_rescind(text)`: Extracts `rescinds_notifications` and `rule_references` from rescind/supersede text
- `_parse_commence(event)`: Extracts commencement date and description
- `_doc_id_matches_rescinded(doc_id, rescinded_nums)`: Matches document IDs against rescinded notification numbers (with normalization)
- `_preprocess_special_ops(events)`: Pre-materialization step that identifies rescinded notifications, flags events from those notifications, and parses corrigendum/commence data
- `_apply_corrigendum(...)`: Materializer handler that applies corrigendum corrections as text substitutions on rule components (only when `targets_rules=True`)
- Manifest now includes `rescinded_notifications`, `rescinded_event_count`, `corrigendum_parsed_count`, `special_ops_processed`

**`review_completion.py` updates:**
- Added `notification_rescinded` to `TERMINAL_STATES` and `_coverage_impact` (classified as `not_rules_text_gap`)
- New gap reason classifications: `notification_rescinded`, `corrigendum_targets_notification`, `rescind_notification_processed`, `commence_no_text_change`
- Classification order: rescinded events → corrigendum → rescind processed → commence → existing flows

### Results

| Metric | Before M3 | After M3 |
|--------|-----------|----------|
| Applied count | 188 | 183 (5 rescinded events excluded) |
| Coverage gaps | 1178 | 1182 |
| Terminal: notification_rescinded | 0 | 7 |
| Terminal: corrigendum_rescind_deferred | 22 | 22 |
| Terminal: commencement_blocked | 20 | 20 |
| Coverage: incomplete | 215 | 212 |
| Coverage: not_rules_text_gap | 963 | 970 |
| Review queue open | 0 | 0 |
| Tests | 165 | 171 |

### Analysis

**CORRIGENDUM (19 events):** All 19 correct NOTIFICATIONS, not rules directly (0 rule_references found). 9 text corrections extracted from "for X read Y" patterns. All classified as `corrigendum_targets_notification` → `corrigendum_rescind_deferred`. Future: chain-apply corrections to the notification's rule amendments.

**RESCIND (3 events):** Rescind notifications 6/2018, 76/2020, 20/2018. Notification 6/2018 had 15 events (3 previously applied) — now correctly flagged as `notification_rescinded`. 16 total events from rescinded notifications identified. Applied count dropped by 5 (correct — these amendments are no longer in force).

**COMMENCE (1 event):** Classified as `commence_no_text_change` → `commencement_blocked`. No text change needed; commencement date tracking only.

**commencement_blocked (20 events):** These are mostly structured amendments (INSERT_CHILD, SUBSTITUTE, SPLICE, etc.) that lack effective dates. They remain in this terminal state — M3 doesn't resolve their date issues.

## M2b Deeper: Retry-Eligibility for Sequencing-Dependent Events

### Problem

1137 events had `event_status_not_validated` — the single biggest coverage gap. Analysis revealed:
- **799 UNKNOWN ops** — mostly form/table content or unparseable compound blocks (not compound rule amendments)
- **268 structured ops** (SUBSTITUTE/INSERT/SPLICE/OMIT) targeting rules — blocked by:
  - 73 `anchor_not_resolved` — anchor text not in static baseline (introduced by prior amendments)
  - 72 `target_not_resolved` — target component not in baseline (created by prior amendments)
  - Other: same-date conflicts, component already exists, incomplete payloads

The root cause: compile-time validation checks anchors/targets against the **static baseline**, but the materializer has a **retry loop** that handles sequencing. Events that fail static validation never reach the retry loop because `status="needs_review"` blocks them at the readiness gate.

### Solution

Added `_is_retry_eligible(event)` to `version_snapshots.py`:
- Allows `needs_review` events with structured operations (SUBSTITUTE, INSERT_CHILD, INSERT_SIBLING, SPLICE, OMIT) through to the materializer's retry loop
- Requires: rule-level target (not work-level), effective date, adequate payload
- Excludes hard blockers: `inserted_component_already_exists`, `document_scope_target_not_materializable`, `compound_block_contains_multiple_amendments`
- The materializer's retry loop handles failures gracefully — events that can't be applied after all retries end up as coverage gaps with specific apply_failed reasons

### Results

| Metric | Before M2b deeper | After M2b deeper |
|--------|-------------------|-------------------|
| Applied count | 183 | 227 (+44) |
| Coverage gaps | 1182 | 1138 (-44) |
| Terminal: materialized | 179 | 223 (+44) |
| Terminal: parser_support_required | 128 | 143 (+15) |
| Terminal: forms_lane_resolved | 810 | 767 (-43) |
| Terminal: commencement_blocked | 20 | 11 (-9) |
| Coverage: complete | 179 | 223 (+44) |
| Coverage: incomplete | 212 | 220 (+8) |
| Review queue open | 0 | 0 |

**44 newly applied events breakdown:**
- 30 INSERT_CHILD — parent components found via retry loop after prior INSERTs
- 7 SUBSTITUTE — anchor text found via retry loop after prior amendments
- 4 SPLICE — same
- 2 OMIT — same
- 1 INSERT_SIBLING — same

**Gap reasons after retry:**
- 984 `event_status_not_validated` (down from 1104)
- ~40 `apply_failed` events (tried but dependencies genuinely unsatisfied)
- 19 `corrigendum_targets_notification`
- 16 `notification_rescinded`

### Key Insight

The "compound block splitting" hypothesis was wrong — the 304 work-level UNKNOWN events are mostly form/table content, not compound rule amendments. The real win was letting sequencing-dependent structured ops reach the materializer's retry loop.

## M6: Forms Lane Materialization (Targeted)

### Implementation

Rewrote `form_version_snapshots.py` from a stub into a functional form version materializer:

**`_extract_form_amendments(events)`**: Scans ALL events (not just form-targeting) for form amendment patterns:
- SUBSTITUTE: `for FORM GST X [and FORM GST Y], the following form(s) shall be substituted`
- INSERT: `after/before FORM GST X, the following FORM shall be inserted`
- Extracts: form_slug, operation, date, source_document_id
- Deduplicates by (event_id, form_slug, operation)
- Handles multi-form substitutions (one event substituting multiple forms)

**`materialize_form_versions()`**: Creates versioned form snapshots:
- Loads 110 baseline form versions from corpus XML
- For each form amendment, creates a version boundary (valid_from/valid_to)
- Text is empty for substituted versions (full-text extraction deferred — excerpt is 500 chars, new form text extends beyond)
- Records source_basis as `form_substitution_from_notification` with event_id reference
- Outputs node_versions.jsonl (186 versions), coverage_gaps.json, materialization_manifest.json

**`review_completion.py` updates:**
- New terminal state `forms_materialized` (classified as `not_rules_text_gap`)
- Added `forms_manifest_path` parameter to `complete_review()`
- Events in `forms_manifest.applied_event_ids` → `forms_materialized` terminal state
- `--forms-manifest` CLI argument added

### Results

| Metric | Before M6 | After M6 |
|--------|-----------|----------|
| forms_lane_resolved | 767 | 717 (-50) |
| forms_materialized | 0 | 52 (NEW) |
| Version count (forms) | 110 (baseline only) | 186 (110 + 76 amendment boundaries) |
| Baseline forms loaded | 110 | 110 |
| Missing forms | 19 | 19 |
| Review queue open | 0 | 0 |

**52 events materialized across 42 unique forms:**
- 35 SUBSTITUTE operations
- 22 INSERT operations (some overlap with SUBSTITUTE in same notification)
- Top forms: gst-drc-03 (3 amendments), gst-drc-07 (3), gst-ewb-01 (2), gst-reg-20 (2)

### Limitations

- **New form text not extracted** — excerpts are 500 chars, full form text extends beyond. Version boundaries are recorded but text is empty. Future work: load source notification text from archive to extract full form text.
- **711 events remain as forms_lane_resolved** — these are form/table content within notifications without clear amendment patterns. They need full-form reconstruction from notification text or LLM-assisted extraction.
- **19 missing forms** — form directories not in corpus (newer forms created by post-2017 notifications).

## Immediate Continuation Thread

Before compaction, work had started on finding a cleaner 2017 Rules baseline source.

Searches found no obvious alternate clean full baseline:

```text
data/Law/base_laws/cgst-rules-2017-part-a-rules.pdf
data/Law/base_laws/cgst-rules-2017-part-b-forms.pdf
corpus/in/union/rules/cgst-rules-2017
corpus/in/union/rules/central-goods-and-services-tax-rules-2017
```

The existing corpus directory only has a partial XML set:

```text
corpus/in/union/rules/cgst-rules-2017/rule-008.xml
corpus/in/union/rules/cgst-rules-2017/rule-009.xml
corpus/in/union/rules/cgst-rules-2017/rule-010.xml
corpus/in/union/rules/cgst-rules-2017/rules.xml
```

A potentially useful CBIC source was found:

```text
data/Law/cbic_tax_portal/notifications/3-2017-central-tax-notifying-the-cgst-rules-2017-on-registration-and-composition_1000872.json
```

Metadata:

- `no`: `3 /2017 - Central Tax`
- `name`: `Notifying the CGST Rules, 2017 on registration and composition levy`
- `contentText`: empty
- `contentPdfBase64`: present

Caveat: this notification may only include registration and composition chapters, not the full Rules. Treat it as a candidate clean partial baseline source, not automatically authoritative.

Recommended next action:

1. Extract PDF text from notification `3/2017-Central Tax`.
2. Inspect whether it is cleaner than the current baseline for components it covers.
3. If clean but partial, implement a baseline merge or source-priority lane:
   - current PDF components where not blocked
   - principal notification components where cleaner and source-covered
   - preserve per-component source basis
   - leave uncovered or conflicting components blocked
4. Regenerate baseline, decisions, materialization, completion, forms, and HTML.
5. Re-run tests and board health check.

Useful extraction command:

```bash
python3 - <<'PY'
import json, base64, io
from pathlib import Path
import pdfplumber

p = Path('data/Law/cbic_tax_portal/notifications/3-2017-central-tax-notifying-the-cgst-rules-2017-on-registration-and-composition_1000872.json')
j = json.load(open(p))
raw = base64.b64decode(j.get('contentPdfBase64') or '')
pages = []
with pdfplumber.open(io.BytesIO(raw)) as pdf:
    for page in pdf.pages:
        pages.append(page.extract_text(x_tolerance=1, y_tolerance=3) or '')
text = '\n\n'.join(pages)
print('len', len(text))
for pat in ['24.Migration','24. Migration','25[Physical','Inserted vide','Page 148 of 164','0308 Aquatic']:
    i = text.lower().find(pat.lower())
    print('\\nPAT', pat, 'idx', i)
    print(text[max(0, i-250): i+900] if i != -1 else '')
PY
```

Earlier attempted import failed because `extract_text_from_pdf_bytes` does not exist in `amendment_events.py`. Use `pdfplumber` directly or check for the existing internal helper `_extract_pdf_base64_text(record)`.

## Regeneration Commands

Run these after relevant code/artifact changes:

```bash
python3 main.py version build-baseline \
  --target-work /in/union/rules/cgst-rules-2017 \
  --registry data/Law/statute_identity_registry.json

python3 main.py version codex-review-decisions \
  --events derived/version_history/amendment_events_reviewed.jsonl \
  --node-versions derived/version_history/cgst-rules-2017/node_versions.jsonl \
  --output derived/version_history/codex_review_decisions.json

python3 main.py version apply-review-decisions \
  --events derived/version_history/amendment_events_with_backfill.jsonl \
  --decisions derived/version_history/review_decisions.json \
  --decisions derived/version_history/auto_review_decisions.json \
  --decisions derived/version_history/dependency_review_decisions.json \
  --decisions derived/version_history/codex_review_decisions.json \
  --output derived/version_history/amendment_events_reviewed.jsonl

python3 main.py version materialize \
  --target-work /in/union/rules/cgst-rules-2017 \
  --events derived/version_history/amendment_events_reviewed.jsonl \
  --registry data/Law/statute_identity_registry.json \
  --corpus-dir corpus \
  --output-dir derived/version_history/cgst-rules-2017

python3 main.py version materialize-forms \
  --events derived/version_history/amendment_events_reviewed.jsonl \
  --corpus-dir corpus \
  --output-dir derived/version_history/forms

python3 main.py version complete-review

python3 main.py version html-report \
  --output derived/version_history/review_report.html

pytest tests/test_canonical_corpus.py -q

curl -I http://127.0.0.1:8879/review_report.html
```

Note: `build-baseline` may return `ok false` while blocked components remain. That is expected and honest until the baseline is repaired or replaced.

## LMS / LLM Notes

LM Studio (LMS) endpoint (replaces OMLX):

```text
http://127.0.0.1:1234/v1
```

Model:

```text
qwopus3.6-27b-v2-mtp
```

Operational notes:

- LMS does NOT support `response_format: {"type": "json_object"}` — only `text` or `json_schema`.
- LMS accepts any dummy API key (using `lm-studio`).
- `/no_think` prefix does NOT suppress reasoning on LMS (model still produces `reasoning_content`). Output JSON is in `content` field.
- CLI flags: `--llm-base-url http://127.0.0.1:1234/v1 --llm-model qwopus3.6-27b-v2-mtp`
- Other models available on this LMS: `qwen3-vl-32b-instruct`, `qwen3.6-35b-a3b-mtp`, `qwen3.6-27b-mtp`, `gemma-4-31b-it`
- Deterministic validation remains mandatory: source span, target, date, operation, and anchor where applicable.

Current unresolved LLM bucket:

- `llm_extraction_required`: `26`
- `llm_limit_not_attempted`: `19` (could be reduced with higher `--llm-limit` or targeted extraction)
- LLM cache at `derived/version_history/llm_candidates.jsonl`: `1458` entries

## Remaining Milestones

### 1. Repair Rules 2017 Baseline

This is the top priority. Current baseline contamination blocks valid replay. Evaluate principal notification `3/2017` and any other source candidates. Implement per-component source basis and keep unresolved/disputed components blocked.

### 2. Reduce Parser-Support Coverage Gaps

Current `parser_support_required`: `128`.

Many are real text amendments that need better resolver/materializer support, but do not force them through while the target baseline component is blocked or polluted.

All 7 "Substitution text not found" gaps are sequencing dependencies (prior amendments not applied), NOT normalization bugs.

### 3. Run More OMLX Extraction

Current `llm_extraction_required`: `26` (LMS endpoint now at `127.0.0.1:1234`).

Use OMLX to propose structured candidates, then validate deterministically. Failed validation should stay incomplete with explicit reasons.

### 4. Commencement / Corrigendum / Rescind Lanes

Current:

- `commencement_blocked`: `24`
- `corrigendum_rescind_deferred`: `22`

These need dedicated legal-effect handling before they can influence text history or coverage.

### 5. Forms Lane

Current:

- `forms_lane_resolved`: `810`

Forms are separated from Rules text history but not truly materialized as first-class forms version history yet. Do not report this lane as complete text history.

### 6. Act Pipeline

Act is registered and baseline work exists, but Finance Act / Amendment Act extraction and materialization still need full implementation and verification.

Keep 2023 source de-dup and conflict checks strict: separate sources, separate commencement dates, same-section/effective-date conflicts flagged, no silent last-writer-wins.

### 7. Reconciliation

Reconcile replayed history against trusted consolidated checkpoints:

- existing 2021 corpus
- 2022-12-26 portal source
- latest downloaded CBIC Rules source

The latest downloaded Rules should contain `31C` and `88C`. If replay mismatches a checkpoint, affected components must remain `coverage="incomplete"`.

## Cautions

- The review queue being closed does not mean legal history coverage is complete.
- Do not silently close coverage gaps.
- Do not materialize events targeting blocked baseline components.
- Do not materialize LLM candidates without deterministic validation.
- Do not treat forms/table mutations as Rules text amendments.
- Do not use `git reset --hard`, `git checkout --`, or other revert/destructive operations.
- The worktree is intentionally dirty and has many untracked files; preserve them.
- Regenerate `derived/version_history/review_report.html` after changing decisions, materialization, completion, reconciliation, or baseline state.

## Worktree Notes

At prior inspection, tracked changes and many untracked feature files existed. `git diff --stat` is not enough to understand the feature because many modules are untracked.

Known dirty/untracked areas include:

```text
.gitignore
main.py
scripts/insert_missing_act_sections.py
src/legal_corpus/serving.py
tests/test_canonical_corpus.py
data/
pytest.ini
src/legal_corpus/*.py version-history modules
src/schemas/amendment_event.schema.json
```

Treat the filesystem state as the source of truth and read files before editing.

---

## Session Update: 2026-06-17 (Act Pipeline — M4b/M4c, M5, Commencement Fixes)

### Summary

Implemented all 5 remaining next steps: Act materialization, commencement date resolution, Act triage, Act reconciliation, and verified Rules baseline.

### Changes Made

#### 1. M4b/M4c: Act Materialization (`materialize_versions`)
- Ran `version_snapshots.py` materializer against Act baseline (174 components) with merged events.
- **Result: applied=57, gaps=119, conflicts=0.** The Rules materializer works on Act section-level components without adaptation.
- Operations applied: SPLICE, SUBSTITUTE, OMIT all functioning.

#### 2. Commencement Date Fixes (`act_amendment_events.py`)
- **Fixed `_parse_named_date`**: Now prefers named dates ("1st day of January, 2020") over DD/MM/YYYY patterns. Previously picked up file numbers like `F.No.20/06/09/2019-GST` as dates.
- **Added `_HARDCODED_COMMENCEMENT` fallback map**: 31 entries for FA 2018-2026 sections missing from CBIC notification corpus.
- **Added `_MONTHS` dict** as module-level constant (was duplicated inline in `_effective_date_from_act`).
- **Fixed `_target_section_number`**: Added patterns for `"For section X of the Central Goods"` (common in Finance Acts) and `"Omission of sections X"` and `"Section X ... shall be"`.
- **Fixed `_operation`**: Added `"subsituted"` (typo found in FA 2023 §151) and `"renumbered"` handling.
- **Added non-CGST filters**: Skip Customs Act and Economic Offences Act amendments that were incorrectly included.
- **Added `taxation-acts` to CLI `--source-family` choices** in `main.py` and routing in `cmd_version_compile_events`.
- **Results: undated events reduced from 36→6.** 170/176 events now dated (97%). Validated events 8→11.
  - Date basis breakdown: 126 central_tax_commencement_notification, 27 hardcoded_commencement, 14 act_publication_date, 1 finance_act_commencement_clause, 8 unresolved.
  - Remaining 6 undated: 4 compound (no section target), 2 from CBIC/Taxation acts with deferred commencement.

#### 3. Act Review Triage (`review_triage.py`)
- Ran LLM-assisted triage on 165 needs_review Act events.
- **Results**: 145 needs_parser_support, 9 auto_reject_candidate, 7 human_review, 4 forms_lane.
- Auto-review and codex-review found 0 new decisions (Act events need text-level baseline verification).
- LLM stats: 7 scheduled, 6 schema_invalid, 1 attempted.

#### 4. M5: Act Reconciliation (`reconciliation.py`)
- Ran reconciliation against `corpus/in/union/acts/cgst-act-2017/act.xml` (CBIC consolidated, includes FA 2026 sections, checkpoint date 2026-04-01).
- **Results**: 2 matched, 172 substantive mismatches, 16 missing components (newly inserted sections not yet materialized), 2 commencement-blocked (§168, §168a), 0 format-only, 188 priority review queue items.
- Coverage: `incomplete` — expected since only 57/176 events applied.

#### 5. Rules Baseline Verification
- Verified all 578 Rules baseline components have non-empty text (0 blocked).
- The "28 blocked" from prior context was resolved by multi-source repair in a previous session.
- `baseline_reconciliation.json` shows all components as "repaired" status.

### Act Event Pipeline State

```text
Finance Act events:  159
CBIC Act events:      14
Taxation Act events:   3
Merged total:        176
Dated:               170  (97%)
Validated:             9
Needs review:        165
Conflicts:             9
Materialized:         57 applied, 119 gaps
```

### Act Date Resolution by Year

| Year | Dated | Total | % |
|------|-------|-------|---|
| 2017 | 0     | 1     | 0% |
| 2018 | 1     | 1     | 100% |
| 2019 | 21    | 21    | 100% |
| 2020 | 13    | 18    | 72% |
| 2021 | 16    | 16    | 100% |
| 2022 | 23    | 23    | 100% |
| 2023 | 34    | 36    | 94% |
| 2024 | 43    | 43    | 100% |
| 2025 | 13    | 13    | 100% |
| 2026 | 4     | 4     | 100% |

### Tests

```bash
pytest tests/test_canonical_corpus.py -q
# 179 passed in 103.39s
```

### Files Changed

- `src/legal_corpus/act_amendment_events.py`: `_parse_named_date`, `_MONTHS`, `_HARDCODED_COMMENCEMENT`, `_effective_date_from_act`, `_target_section_number`, `_operation`, compile filter logic.
- `main.py`: Added `taxation-acts` to source-family choices and routing.

### Next Steps

1. **Act materialization improvement**: More events could be applied with better target resolution and text verification.
2. **Re-scrape FA 2024 No.2**: All 41 CGST sections already captured; 113 non-CGST sections missing but irrelevant to CGST pipeline. Low priority.
3. **Fix remaining 6 undated events**: 4 compound (schedule/notification amendments) + 2 CBIC/taxation act events with deferred commencement.
4. **Pipeline hardening**: Fix default `--events` path to use reviewed events instead of raw compiled events.
5. **Deeper M6**: Load source notification texts for full form substitution text.

---

## Session: 18 June 2026 — Act Materialization Depth & Review Completion

### Summary

Continued improving Act materialization coverage, completed Act review pipeline (triage → complete-review), and re-ran all materializations and reconciliation. Act applied events improved from 57→75 (32% increase) via structural_text extraction for whole-section substitutions.

### M4b/M4c: Act Materialization Improvements

#### Structural Text Extraction (SUBSTITUTE/SPLICE payloads)
- **`_extract_quoted_after_namely()`**: New function in `act_amendment_events.py`. Extracts replacement text from "namely:––" patterns in Finance Act clauses. Handles `:` + en-dash separator variants.
- **`_payload()` updated**: Detects "For section X...shall be subst?ituted" patterns → sets `structural_text` (full section replacement text). Detects "marginal heading" → sets `structural_heading`.
- **Critical regex bug fix**: `subs[ie]tuted` didn't match "substituted" (missing 't'). Changed to `subst?ituted` throughout.
- **Validation updated**: New `structural_substitute_verified` validation path — events with `structural_text` are materializable without `old_text` matching (since the entire section is replaced).

#### Results: Act materialization applied=75 (up from 57)
- 32 SPLICE (insert new sub-sections/provisos at component position 0 when anchor empty)
- 26 SUBSTITUTE (including 16 via structural_text whole-section replacement)
- 17 OMIT (remove text passages)

#### Remaining 101 Act gaps
- 66 `event_status_not_validated` — payloads empty (no insert_text/old_text extracted)
- 14 `apply_failed: Substitution text not found` — old_text genuinely not in baseline (baseline quality issue)
- 12 `apply_failed: Partial omission text not found` — same baseline mismatch
- 6 `event_date_unresolved` — compound/schedule events without commencement dates
- 3 `apply_failed: Target component missing` — sections created by later amendments not in 2017 baseline

### Act Review Pipeline: COMPLETE

#### Triage
- LLM-assisted triage on 165 needs-review events.
- Results: 145 `needs_parser_support`, 9 `auto_reject`, 7 `human_review`, 4 `forms_lane`.

#### Complete-Review
- 176/176 closed: 57 `materialized`, 66 `rejected_non_rules_text`, 30 `parser_support_required`, 16 `forms_lane_resolved`, 6 `commencement_blocked`, 1 `covered_by_source_backed_event`.

### Act Reconciliation (re-run)
- Against CBIC consolidated act.xml (checkpoint 2026-04-01):
  - 1 matched, 173 substantive mismatches, 16 missing, 2 commencement_blocked.
  - Coverage: incomplete (expected — baseline has 174 sections, consolidated has 394).

### CLI Defaults Fixed
- `materialize` and `materialize-forms` now default to `amendment_events_reviewed.jsonl` (was `amendment_events.jsonl`).
- Ensures post-review decisions are picked up automatically.

### Current Materialization State (all three lanes)

```text
RULES:  applied=227, gaps=1138, conflicts=0  (1361 events, review queue closed)
ACT:    applied=75,  gaps=101,  conflicts=0  (176 events, review queue closed)
FORMS:  versions=186, applied=57, gaps=289   (296 form events processed)
```

### Rules UNKNOWN Events Analysis
- 799 UNKNOWN operations: 469 form mutations (routed to forms materializer), 324 rule-level (156 same_effective_date_conflict + 168 validation failures).
- Form mutations are dual-counted (appear in both Rules and Forms gap lists).

### Tests
- 179 passed in 101.67s.

### Files Changed This Session
- `src/legal_corpus/act_amendment_events.py`: `_extract_quoted_after_namely()`, structural_text in `_payload()`, `subst?ituted` regex fix, `structural_substitute_verified` validation.
- `src/legal_corpus/version_snapshots.py`: No changes needed — existing `structural_text` handling in SUBSTITUTE already works.
- `main.py`: CLI default events path fix for `materialize` and `materialize-forms`.

### Next Steps

1. **Improve Act extraction**: Extract insert_text for the 20 SPLICE events with empty payloads (compound clauses that need splitting).
2. **Rules materialization depth**: 324 rule-level UNKNOWN events could benefit from better deterministic patterns or LLM extraction improvements.
3. **Forms full-text reconstruction**: Load notification source texts to build complete form version text (289 gaps, 717 forms_lane_resolved events).
4. **Act baseline quality**: 26 gaps from old_text/omit_text not matching baseline — may need 2017 base text correction.
5. **Deeper reconciliation**: Build schedule-level comparison and improve format normalization for closer matches.

---

## Session: 18 June 2026 (Part 2) — Forms Full-Text Reconstruction & Rules UNKNOWN Extraction

### Summary

Two major improvements: (1) Forms materializer now extracts full form text from notification PDFs — versions with text jumped from 110→167 (90%). (2) Rules event compiler gains a whole-rule substitution pattern — applied events improved from 227→244 (+17), reconciliation matches from 1→17.

### M6b: Forms Full-Text Reconstruction

#### Approach
- Enhanced `form_version_snapshots.py` with PDF text extraction from CBIC notification PDFs.
- `_notification_text()`: Loads and caches notification PDF text using `fitz` (pymupdf).
- `_extract_form_text_from_notif()`: Finds "for FORM GST X...shall be substituted, namely:- [text]" pattern in notification PDF text and extracts the full form text.
- `_apply_excerpt_substitutions()`: Applies word-level substitutions from event excerpts to cumulative form text (fallback for partial amendments).
- Three extraction methods tracked: `full_form` (from PDF), `word_sub` (from excerpt patterns), `carry_forward` (previous text preserved).

#### Results
- **versions_with_text: 167/186 (90%)** — up from 110/186 (59%).
- `full_form` extraction: 49 amendments got text directly from notification PDFs.
- `carry_forward`: 8 amendments preserved previous text.
- `word_sub`: 0 (patterns didn't match form-level amendment excerpts).

### M2c: Rules Whole-Rule Substitution Pattern

#### Problem
12 events had "for rule X, the following shall be substituted, namely:- [full text]" patterns but were classified as UNKNOWN/form mutations because the `form_match` pattern (`\bin\s+FORM\s+GST\s+`) fired first for blocks mentioning both forms AND rules.

#### Fix
Added `whole_rule_sub_match` pattern in `amendment_events.py` BEFORE the `form_match` check. The pattern matches:
```
for rule X, the following (rule )?shall be substituted, namely: [full rule text]
```
Creates SUBSTITUTE events with `structural_text` payload targeting the specific rule.

#### Pipeline Rebuild
- Recompiled all CBIC Central Tax notifications (846 base events, 548 LLM cache hits).
- Applied all review decisions (auto, codex, dependency, GLM, completion) to merged events.
- Combined with 520 events from previous compilation (LLM-extracted, backfilled) for total 1386 events.

#### Results
- **Rules applied: 244** (up from 227, +17 events).
- SUBSTITUTE: 79 (up from ~60), INSERT_CHILD: 54, SPLICE: 46, INSERT_SIBLING: 41, OMIT: 24.
- Rules reconciliation (2022-12-26): **17 matched** (up from 1), 130 mismatched, 98 missing.
- 6 whole-rule substitution events confirmed applied (rules 138, 100, 142, 67A, 109, 111).

### Current Materialization State (all three lanes)

```text
RULES:  applied=244, gaps=1146, conflicts=1  (1386 events, 231 validated)
ACT:    applied=75,  gaps=101,  conflicts=0  (176 events, review queue closed)
FORMS:  versions=186, versions_with_text=167 (90%), applied=57, gaps=277
```

### Rules UNKNOWN Events (Updated)
- 796 UNKNOWN operations (down from 799): 469 form mutations, 129 form/table content noise, ~200 rule-level events needing better extraction.
- Form mutations are dual-counted (appear in both Rules and Forms gap lists).
- The 129 form/table content events are form text snippets that leaked into rules stream — not fixable as rule amendments.

### Tests
- 179 passed in 104.05s.

### Files Changed This Session (Part 2)
- `src/legal_corpus/form_version_snapshots.py`: Added `_notification_text()`, `_extract_form_text_from_notif()`, `_apply_excerpt_substitutions()`, enhanced `materialize_form_versions()` with PDF text loading and cumulative text tracking.
- `src/legal_corpus/amendment_events.py`: Added `whole_rule_sub_match` pattern before `form_match` check.
- `derived/version_history/amendment_events_reviewed.jsonl`: Rebuilt with clean compilation + all review decisions.
- `derived/version_history/amendment_events.jsonl`: New compilation output (846 events).
- `derived/version_history/merged_amendment_events.jsonl`: Merged + review decisions applied (866 events).

### Next Steps

1. **Improve Rules LLM extraction**: 200 rule-level UNKNOWN events need better deterministic patterns or LLM assistance. Many are form/table content noise; others are compound blocks needing splitting.
2. **Improve Forms word-level substitution**: The `_RE_WORD_SUB` pattern didn't match any form amendment excerpts. Need to handle GST-specific amendment phrasing like "for serial number X and the entries related thereto".
3. **Act extraction improvements**: 66 event_status_not_validated gaps have empty payloads. Need better extraction for compound clauses.
4. **Rules reconciliation depth**: 130 mismatches need investigation — some may be format normalization issues.
5. **Act baseline quality**: 26 gaps from old_text not matching baseline text.
6. **Notification PDF OCR**: 727/734 central tax notifications have PDF text; remaining 7 may need OCR.

---

## Session: 18 June 2026 (Part 3) — Reconciliation Deep-Dive, Noise Filtering, Forms Enhancement, Pipeline Regen

### Summary

Five tasks completed: (1) Reconciliation mismatch analysis — 30/33 high-similarity mismatches traced to duplicate headings (already normalized) and stray baseline numbers. (2) Filtered 105 form/table content noise events from Rules gaps. (3) Enhanced forms materializer with `in Form GST X` amendment detection (+24 versions, +24 applied). (4) Confirmed Act compiler already handles whole-section substitutions. (5) Full pipeline regeneration — all lanes consistent.

### Task 1: Rules Reconciliation Analysis

- **130 substantive mismatches**: 33 at ≥0.90 similarity (30 had duplicate headings — already handled by `_reconciliation_text` normalization), 48 at <0.50 similarity (no amendments applied or fundamentally different text).
- **45 format-only mismatches**: 35 at ≥0.95 — annotation noise (footnote refs, page numbers). Already classified as format-only.
- **Root cause of remaining mismatches**: Stray numbers from baseline PDF extraction (e.g., "50" in rule 104 from page number artifact), missing amendments for low-similarity components, 98 missing components (rules added after 2017 not in baseline).
- **No code changes needed** — normalization is already sophisticated. Remaining fixes require baseline quality improvements.

### Task 2: Form/Table Content Noise Filtering

- Identified 105 UNKNOWN events whose excerpts are form/table content (GSTR-3B payment tables, verification sections, registration details) rather than amendment instructions.
- Marked as `status=rejected, payload.baseline_source_only=True` → excluded from gap count by materializer's `_is_non_gap_rejected_event` check.
- **Rules gaps: 1146 → 1041** (−105).

### Task 3: Forms Word-Level Amendment Detection

- Added `_RE_IN_FORM_AMEND` pattern: matches "in Form GST X, for the words/figures/letters... shall be substituted/omitted".
- Added `_RE_QUOTED_PAIRS` pattern: extracts old/new text pairs from smart-quoted text near "substituted" keyword.
- Enhanced `_apply_excerpt_substitutions` to use the new quoted-pair pattern.
- **Forms: versions 186 → 210, versions_with_text 167 → 191, applied 57 → 81, gaps 277 → 255.**
- Text extraction: full_form=50, word_sub=1, carry_forward=30.

### Task 4: Act Whole-Section Substitution

- Confirmed Act compiler already handles whole-section substitutions via `_extract_quoted_after_namely()` and `structural_text` payload (16 events from prior session).
- 12 additional events have sub-section level substitution language but are classified as OMIT — these are compound blocks needing better parsing. Minor improvement opportunity.

### Task 5: Full Pipeline Regeneration

- All three lanes materialized consistently.
- Rules and Act reconciliation re-run.
- HTML report regenerated.
- 179 tests passing.

### Final Materialization State

```text
RULES:  applied=244, gaps=1041, conflicts=1  (1386 events, 231 validated)
ACT:    applied=75,  gaps=101,  conflicts=0  (176 events, review queue closed)
FORMS:  versions=210, versions_with_text=191 (91%), applied=81, gaps=255
```

### Reconciliation State

```text
Rules (2022-12-26):  matched=17, format_only=45, mismatched=130, missing=98
Act   (2026-04-01):  matched=1,  format_only=0,  mismatched=173, missing=16
```

### Files Changed This Session (Part 3)
- `src/legal_corpus/form_version_snapshots.py`: Added `_RE_IN_FORM_AMEND`, `_RE_QUOTED_PAIRS` patterns, updated `_apply_excerpt_substitutions`.
- `derived/version_history/amendment_events_reviewed.jsonl`: 105 events marked as rejected (form_table_content_noise).

### Next Steps

1. **Rules baseline quality**: Fix stray numbers from PDF extraction in baseline components (affects reconciliation and materialization accuracy).
2. **Rules 98 missing components**: Rules added after 2017 baseline — need INSERT_SIBLING events from notifications to create them.
3. **Rules UNKNOWN extraction**: ~200 remaining rule-level UNKNOWN events need better patterns or LLM assistance.
4. **Act sub-section substitution**: 12 OMIT events with substitution language need reclassification.
5. **Forms word-level application**: Improve quoted-text matching between notification excerpts and form text.
6. **Deeper reconciliation**: Address the 48 low-similarity Rules mismatches and 173 Act mismatches.

---

## Session: 18 June 2026 (Part 4) — Authority Hardening Phase 0

### Summary

Implemented the "authority hardening" direction agreed with the user. Phase 0 delivers: (1) baseline decontamination (28 blocked → 0), (2) structural target creation for post-2017 rules, (3) confidence tier system (A/B/C/D), and (4) full pipeline regeneration. 52 rules are now Tier A (court-ready).

### Phase 0 Accomplishments

#### 1. Baseline Decontamination (`baselines.py`)
- **Root cause**: The "2017 baseline" PDF was a 2021 consolidated edition with 330 editorial annotations baked into rule text.
- **`decontaminate_baseline_text()`**: Strips editorial annotations (`Inserted/Substituted vide Notf...`), bracketed post-2017 insertions (`[(2A)...]33`), stray footnote numbers, page headers, and "wef" markers.
- **Post-2017 insertion dropping**: Rules 96A, 138A-E, 9B, and their sub-rules removed from baseline (they're created by INSERT events from their respective notifications).
- **Result**: 578 components → 552 (26 dropped), 28 blocked → **0 blocked**, 213 decontaminated.

#### 2. Structural Target Creation (`version_snapshots.py`)
- **`_ensure_structural_targets()`**: Pre-creates components for SUBSTITUTE events with `structural_text` that target non-existent components. Handles post-2017 rules whose original INSERT_SIBLING events are missing from the stream.
- Rules 138A-D now created from SUBSTITUTE structural_text. Applied events: 244 → **251** (+7). Gaps: 1041 → **1029** (-12).

#### 3. Confidence Tier System (`confidence_tiers.py`)
- New module computing per-component tiers:
  - **Tier A (Court-ready)**: 52 components — reconciled, all events validated, clean baseline
  - **Tier B (High confidence)**: 416 components — all events validated, clean baseline
  - **Tier C (Advisory)**: 131 components — some events need review
  - **Tier D (Do not cite)**: 157 components — gaps, missing, or contaminated
- CLI command: `python3 main.py version confidence-tiers`
- Output: `derived/version_history/confidence_tiers.json`

#### 4. Top 10 Litigation-Critical Rules Status
All 10 target rules (89, 96, 36, 46, 142, 117, 129, 21, 53, 138) are Tier C or D. Main blockers:
- **UNKNOWN events**: 48 UNKNOWN events targeting these rules need classification (15 for R89, 10 for R142, 8 for R96, etc.)
- **Sequencing chains**: 17 `subst_text_not_found` gaps from unapplied prior amendments
- **Baseline text quality**: Stray page numbers and missing words from PDF extraction

### Final Materialization State

```text
RULES:  applied=251, gaps=1029, conflicts=1  (1386 events, baseline=552 components, 0 blocked)
ACT:    applied=75,  gaps=101,  conflicts=0
FORMS:  versions=210, versions_with_text=191 (91%), applied=81, gaps=255
```

### Confidence Tier Distribution

```text
Tier A (Court-ready):      52 / 756  (6.9%)
Tier B (High confidence): 416 / 756  (55.0%)
Tier C (Advisory):        131 / 756  (17.3%)
Tier D (Do not cite):     157 / 756  (20.8%)
```

### Files Changed This Session (Part 4)
- `src/legal_corpus/baselines.py`: Added `decontaminate_baseline_text()`, `decontaminate_baseline()`, `_POST_2017_RULE_PREFIXES`, `_is_post_2017_rule_insertion()`, `_is_post_2017_subrule_insertion()`. Integrated into `build_baseline()`.
- `src/legal_corpus/version_snapshots.py`: Added `_ensure_structural_targets()`. Integrated into `materialize_versions()`.
- `src/legal_corpus/confidence_tiers.py`: **New file.** Tier computation module.
- `main.py`: Added `cmd_version_confidence_tiers()` and `confidence-tiers` CLI subcommand.
- `src/legal_corpus/reconciliation.py`: No net change (normalization fix reverted to preserve existing test behavior).

### Tests
- 179 passed.

### Next Steps for Phase 0 Completion

1. **Classify UNKNOWN events for top 10 rules**: 48 events need deterministic patterns or LLM extraction. Rule 89's 15 Statement substitutions are the highest priority.
2. **Fix sequencing chains**: 17 `subst_text_not_found` gaps need prior amendments applied first.
3. **Fix anchor text matching**: 8 `anchor_not_found` gaps need encoding normalization (e.g., `FORM GSTR- 1` vs `FORM GSTR-1`).
4. **Create INSERT_SIBLING events for 96A, 138E**: Needed for downstream events to resolve.
5. **Baseline text quality**: Stray page numbers (`; 19 (d)` → `; (d)`) partially handled; needs more patterns.

### Phase 1-4 Roadmap (Unchanged)

1. **Phase 1**: Full baseline purity (Rules ✓, Act pending, form baselines pending)
2. **Phase 2**: Unified component schema + form/table/schedule operations
3. **Phase 3**: Evidence bundles + confidence tiers (tier system ✓)
4. **Phase 4**: LLM/vision extraction at scale on new operations

---

## Session: 18 June 2026 (Part 5) — Phase 1 Implementation

### Summary

Implemented the three-lane split (rules text / form-statement / source quality) for the top 10 litigation-critical rules. Delivered: top10 gap report, anchor normalization, component_id format fixes, structural target creation extension, Statement/Form lane routing, and tier_blockers in confidence output.

### What Was Delivered

#### Step 1: Top-10 Gap Report (`top10_gap_report.json`)
- Auditable per-event gap report for rules 89, 96, 36, 46, 142, 117, 129, 21, 53, 138
- Each gap classified into lane: `form_statement`, `target_creation`, `anchor_normalization`, `sequencing_chain`, `duplicate_insert`
- Output: `derived/version_history/cgst-rules-2017/top10_gap_report.json`

#### Step 2: Anchor Normalization + Component_id Fixes
- **`src/anchor_resolver.py`**: Enhanced `normalize_text()`:
  - Normalize `―` (U+2015, horizontal bar), `—` (em-dash), `–` (en-dash) → `-`
  - Normalize `‖` (U+2016, double vertical line) → `"`
  - Normalize all Unicode curly quotes → standard quotes
  - Normalize form reference spacing: `GSTR- 1` → `GSTR-1`
- **`src/legal_corpus/version_snapshots.py`**: Component_id normalization at event load:
  - `sub-rule` → `subrule`, `sub-section` → `subsection` in all target/payload IDs

#### Step 3: Structural Target Creation Extension
- **`_ensure_structural_targets()`** now has 3 passes:
  - Pass 1: SUBSTITUTE with `structural_text` targeting missing components (unchanged)
  - Pass 2: **NEW** — `needs_review` INSERT_SIBLING with content → creates 96A, 96B from their notification text
  - Pass 3: **NEW** — SPLICE/SUBSTITUTE targeting missing sub-rules whose parent was created → creates the sub-rule
- Extracted `_create_synthetic_component()` helper for reuse

#### Step 5: Statement/Form Lane Routing
- **`_is_form_statement_event()`**: detects events targeting FORM GST RFD-01 Statements and Declarations
  - Matches `old_text` starting with "Statement 1A", "Statement-2", "DECLARATION", etc.
  - Matches UNKNOWN events with verification text
- Routed events emit `routed_to_forms_lane` gap (excluded from Rules coverage gap count)
- **21 events routed** out of Rules materializer

#### Step 7: Confidence Tier Enhancements
- **`tier_blockers`** field added to per-component confidence output
- Each blocker includes: event_id, source notification, operation, reason, review_reasons
- Indexes both parent rule and sub-rule events (via prefix matching)
- A lawyer can now see exactly WHY a component is Tier C/D and which events need resolution

### Impact on Top-10 Rules

| Rule | Before (P0) | After (P1) | Change |
|------|-------------|------------|--------|
| 89   | 12 gaps     | 4 gaps     | **-8** (Statement routing + target creation) |
| 96   | 6 gaps      | 1 gap      | **-5** (96A created from INSERT_SIBLING) |
| 36   | 6 gaps      | 6 gaps     | 0 (anchors genuinely not in text — sequencing) |
| 46   | 2 gaps      | 2 gaps     | 0 |
| 142  | 7 gaps      | 7 gaps     | 0 (target_missing + anchor issues persist) |
| 117  | 3 gaps      | 3 gaps     | 0 |
| 129  | 1 gap       | 1 gap      | 0 |
| 21   | 0 gaps ✓    | 0 gaps ✓   | 0 |
| 53   | 0 gaps ✓    | 0 gaps ✓   | 0 |
| 138  | 5 gaps      | 5 gaps     | 0 |
| **Total** | **42** | **29** | **-13 (31% reduction)** |

### Overall Materialization State

```text
RULES:  applied=255 (+4 from P0), gaps=1030, conflicts=1
        21 events routed to forms lane
        Tier A=53, B=417, C=135, D=156
```

### Files Changed This Session (Part 5)
- `src/anchor_resolver.py`: Enhanced `normalize_text()` with Unicode quote/dash normalization and form reference spacing
- `src/legal_corpus/version_snapshots.py`: Component_id normalization at load; 3-pass `_ensure_structural_targets()`; `_create_synthetic_component()` helper; `_is_form_statement_event()` routing
- `src/legal_corpus/confidence_tiers.py`: `tier_blockers` with per-event reasoning; child component indexing
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`: **New.** Auditable per-event gap report

### Tests
- 179 passed

### Remaining Top-10 Blockers (from tier_blockers)

- **Rule 89** (39 blockers): 36 needs_review events (Statement substitutions, INSERT_CHILD, form content) + reconciliation mismatch
- **Rule 142** (19 blockers): 18 needs_review events (SPLICE anchor failures, UNKNOWN form content) + reconciliation mismatch
- **Rule 138** (14 blockers): 13 needs_review events (INSERT_CHILD for 138A-E sub-rules, UNKNOWN form content)
- **Rule 96** (11 blockers): 9 needs_review events (96A sub-rule events, UNKNOWN)
- **Rule 46** (10 blockers): 7 needs_review events (proviso substitutions, INSERT_CHILD)
- **Rule 36** (8 blockers): 6 needs_review events (anchor failures, compound blocks)

### Next Steps

1. **Classify the 36 Rule 89 needs_review events**: Most are Statement substitutions (need STATEMENT_SUBSTITUTE operation) and form content (need form lane routing)
2. **Fix Rule 142 target_creation**: Create sub-rules 3, 5 from notification structural_text
3. **Fix Rule 138 parent_missing**: Create 138E from INSERT_SIBLING content
4. **Build STATEMENT_SUBSTITUTE materializer operation** for Phase 2
5. **Act baseline**: Apply same decontamination + structural target approach to Act pipeline

---

## 2026-06-19 Iteration: Phase 2 Portal Completeness, RFD-01 Statements, Corrigenda Ledger

### Scope Implemented

This iteration moved Phase 2 from generic form-table extraction toward the revised litigation-grade plan:

1. Added portal completeness checks before deeper extraction.
2. Added first-class RFD-01 Statement component versions.
3. Routed RFD-01/form statement events out of Rules text gaps.
4. Added an explicit corrigendum chronology ledger.
5. Surfaced portal completeness, statement evidence, and confidence tiers in compare/report outputs.
6. Regenerated the static HTML review board after the iteration.

### Code Changes

- `src/legal_corpus/portal_completeness.py`
  - New module.
  - Extracts notification references from `derived/version_history/reconciliation_sources/current-taxinformation/html/...`.
  - Compares portal-listed references with event-ledger notification sources.
  - Writes `derived/version_history/portal_completeness_report.json`.
  - Annotates `derived/version_history/cgst-rules-2017/top10_gap_report.json` with `missing_source_notification` blockers.

- `src/legal_corpus/form_version_snapshots.py`
  - Added focused RFD-01 Statement pilot.
  - Materializes statement components such as `/in/union/forms/gst-rfd-01/statement/1a`.
  - Supports statement-level event normalization as:
    - `STATEMENT_INSERT`
    - `STATEMENT_SUBSTITUTE`
    - `STATEMENT_OMIT`
    - `STATEMENT_TEXT_SUBSTITUTE`
  - Current output: `statement_applied_count=39`.

- `src/legal_corpus/version_snapshots.py`
  - Rules materializer now records form/statement routed events in the manifest instead of counting them as Rules coverage gaps.
  - Added `forms_lane_routed_count` and `forms_lane_routed_events`.
  - Writes `derived/version_history/cgst-rules-2017/corrigendum_ledger.jsonl`.
  - Corrigendum ledger rows preserve:
    - corrected notification references
    - corrigendum publication/effect date
    - retrospective flag
    - date basis
    - correction payloads

- `src/legal_corpus/confidence_tiers.py`
  - Accepts `portal_completeness_report.json`.
  - Portal-only missing notifications become Tier D blockers with reason `missing_source_notification`.

- `src/legal_corpus/version_compare.py`
  - Compare responses now include:
    - `confidence_tier`
    - `confidence_detail`
    - `portal_completeness`
    - `form_statement_evidence`

- `src/legal_corpus/version_history_report.py`
  - HTML report now embeds and displays:
    - portal completeness path/counts
    - corrigendum ledger path/counts
    - forms-lane routed count
    - RFD-01 statement materialization count

- `main.py`
  - Added `python3 main.py version portal-completeness`.
  - Added `--portal-completeness` input for `version confidence-tiers`.

### Current Artifact State

Rules materialization:

```text
applied_count=255
coverage_gap_count=1009
forms_lane_routed_count=21
corrigendum_ledger_count=34
blocked_baseline_component_count=0
```

Forms materialization:

```text
applied_count=81
statement_applied_count=39
coverage_gap_count=246
version_count=262
versions_with_text=230
```

Portal completeness:

```text
rule_count=129
missing_source_notification_count=71
Rule 89 portal_completeness_status=incomplete
top10_gap_report portal_missing_notification_blockers_added=71
```

Confidence tiers after portal blockers:

```text
A=50
B=416
C=120
D=175
total_components=761
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/corrigendum_ledger.jsonl`
- `derived/version_history/forms/materialization_manifest.json`
- `derived/version_history/forms/coverage_gaps.json`
- `derived/version_history/forms/node_versions.jsonl`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version materialize
python3 main.py version materialize-forms
python3 main.py version confidence-tiers
python3 main.py version html-report
python3 main.py version compare /in/union/rules/cgst-rules-2017/rule/89 --from-date 2018-01-01 --to-date 2024-12-31 --version-dir derived/version_history/cgst-rules-2017
```

### Verification Results

```text
pytest tests/test_canonical_corpus.py -q
184 passed in 96.90s
```

Rule 89 compare smoke test:

```text
confidence_tier=D
portal_completeness.status=incomplete
form_statement_evidence_count=28
```

HTML report:

```text
derived/version_history/review_report.html regenerated at 2026-06-19 00:24:47 +0530
Visible metrics now include Forms routed, Corrigenda, Portal missing, and RFD-01 statements.
```

### Remaining Work

- Portal completeness currently reports 71 missing source notifications. Some are true source gaps; some may be false positives from references to rate/customs notifications inside Rules text. Next pass should classify portal refs by notification class before treating all refs as CGST Rules source gaps.
- RFD-01 Statement lane is first-class, but extraction is still linear/payload-driven. It should be hardened with statement-boundary parsing from notification text before adding simple forms like REG-01 and DRC-03.
- Corrigendum ledger is explicit, but notification-derived candidate rewriting is not fully applied yet. Current ledger preserves chronology for the later candidate rewrite/materialization pass.
- Top-10 repair work remains for `117/1A`, `138A/B/E`, `142/3`, `142/5`, anchor normalization variants, and sequencing-chain substitutions.
- Duplicate/already-reflected cleanup remains to be audited with source/text proof before suppressing coverage gaps.

---

## 2026-06-19 Iteration: Portal Reference Classification

### Scope Implemented

This iteration tightened portal completeness so portal citations are not over-counted as Rules source gaps when the TaxInformation text is merely referring to rate/customs notifications inside rule text.

### Code Changes

- `src/legal_corpus/portal_completeness.py`
  - Added deterministic `notification_class` for extracted portal citations.
  - Classifies `Central Tax (Rate)`, `Integrated Tax (Rate)`, and `Customs` citations as `external_reference_notification`.
  - Only `rules_source_notification` citations can become `missing_source_notification` blockers.
  - Added top-level `external_reference_notification_count`.
  - Made `annotate_top10_gap_report()` idempotent by removing stale `portal_completeness` rows before writing the current blocker set.

- `src/legal_corpus/version_history_report.py`
  - Added visible `Portal external` overview metric to the static HTML report.

- `tests/test_canonical_corpus.py`
  - Added coverage for rate/customs exclusion.
  - Added coverage for stale top-10 portal blocker replacement.

### Artifact Impact

Portal completeness:

```text
missing_source_notification_count: 71 -> 61
external_reference_notification_count: 10
Rule 89 external references excluded: 4
Rule 89 remaining source blockers: 20/2024, 26/2022, 38/2021, 48/2017
```

Top-10 Rule 89 report:

```text
gap_count=8
portal_completeness blockers=4
form_statement blockers=2
sequencing_chain blockers=2
```

Confidence tiers:

```text
Before: A=50, B=416, C=120, D=175
After:  A=50, B=416, C=121, D=174
```

### Regenerated Artifacts

- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'portal_completeness or top10_portal or confidence_tiers_use_portal'
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
4 passed, 182 deselected
pytest tests/test_canonical_corpus.py -q
186 passed in 95.49s
```

### Remaining Work

- `48/2017-Central Tax` remains a Rule 89 source blocker because it is cited as Central Tax, not Central Tax (Rate), in the portal text. It needs source-ledger confirmation before reclassification.
- Remaining portal blockers are now narrower but still need per-rule source ingestion or matching against work-level commencement/corrigendum events.

---

## 2026-06-19 Iteration: Rule 142 Target Creation And Top-10 Rebuild

### Scope Implemented

This iteration closed the stale Rule 142 target-creation blockers for `/rule/142/subrule/3` and `/rule/142/subrule/5` and made the top-10 gap report regenerate from the current Rules materialization before portal completeness is appended.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Extended structural-target creation for `SPLICE` and `SUBSTITUTE` events that target missing subrules.
  - Added parent-span subrule carving from the baseline rule text so a unique top-level subrule can become a first-class synthetic component before applying the event.
  - Preserves the parent rule text while writing the subrule node and parent/child path metadata.

- `src/legal_corpus/portal_completeness.py`
  - Added `rebuild_top10_gap_report()` to build `top10_gap_report.json` from current `coverage_gaps.json`.
  - Groups gaps by rule, ranks the top rules by current gap count, and assigns lanes such as `target_creation`, `anchor_normalization`, `form_statement`, `duplicate_insert`, and `event_resolution`.
  - Portal completeness annotation now runs after this rebuild, so stale portal or target-creation rows do not survive.

- `main.py`
  - `python3 main.py version portal-completeness` now rebuilds the top-10 report before adding portal blockers.
  - Added `--coverage-gaps` and `--top-n` options for report generation.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for creating a missing subrule target from a parent span.
  - Added coverage for rebuilding the top-10 report from current coverage gaps.

### Artifact Impact

Rules materialization:

```text
applied_count: 261
coverage_gap_count: 1003
forms_lane_routed_count: 21
corrigendum_ledger_count: 34
```

Rule 142 top-10 status:

```text
gap_count: 12
lane_counts: anchor_normalization=4, event_resolution=8
target_creation: 0
```

Portal completeness:

```text
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
A=50, B=418, C=125, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'missing_subrule_target_from_parent_span or portal_completeness or top10'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
5 passed, 183 deselected
188 passed in 97.48s
```

Artifact sanity check:

```text
Rule 142 target_creation gaps: []
review_report.html regenerated at 2026-06-19 00:39:21 +0530
```

### Remaining Work

- Rule 142 still has 12 unresolved rows, now all anchor-normalization or event-status validation work.
- Top-10 target creation remains for other rules, including `117/1A` and selected e-way-bill/rule-138 targets.
- Next Phase 2 iteration should continue Stage 4 repairs while keeping form/table/statement events routed out of the Rules text materializer.

---

## 2026-06-19 Iteration: Rule 117(1A) Chain Repair

### Scope Implemented

This iteration closed the Rule 117 target-creation work for `/rule/117/subrule/1a` and materialized its source-backed date-substitution chain.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Added narrow materializer repairs for known source-proven extraction misses:
    - Notification `48/2018-Central Tax` now materializes Rule 117(1A) as an `INSERT_CHILD`.
    - The same notification adds the Rule 117(4)(b)(iii) TRAN-2 proviso as a source-backed `SPLICE`.
    - Notification `49/2019-Central Tax` now supplies the missing substitution payload for Rule 117(1A): `31st March, 2019` to `31st December, 2019`.
  - Preserves the original source event chain and records repair notes in review metadata.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for the full Rule 117(1A) chain:
    - 2018 insertion from Notification 48/2018.
    - 2019 substitution from Notification 49/2019.
    - 2020 substitution to `31st March, 2020`.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 267
coverage_gap_count: 998
forms_lane_routed_count: 21
corrigendum_ledger_count: 34
```

Rule 117 status:

```text
Rule 117 no longer appears in top10_gap_report.json.
Rule 117 coverage gaps: 0
Rule 117(1A) versions: 2018-09-10, 2019-10-09, 2020-01-01
```

Portal completeness:

```text
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=768
A=50, B=418, C=126, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_117_1a_source_wrapper_event or missing_subrule_target_from_parent_span'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
2 passed, 187 deselected
189 passed in 97.16s
review_report.html regenerated at 2026-06-19 00:51:44 +0530
```

### Remaining Work

- Continue Stage 4 with Rule 138/e-way-bill target creation and event-resolution gaps.
- Rule 89 remains the largest top-10 gap cluster; keep RFD-01 statement/table events out of Rules text materialization.
- Portal completeness still has 61 source-notification blockers that require source ingestion or reference reconciliation.

---

## 2026-06-19 Iteration: Rule 138A Child Parent Carving

### Scope Implemented

This iteration generalized structural target creation for `INSERT_CHILD` events whose parent subrule exists only as text inside the parent rule. It unblocks a source-backed Rule 138A proviso insertion without adding a rule-specific repair.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Extended `_ensure_structural_targets()` with an `INSERT_CHILD` parent pass.
  - When an insertion targets a child under a missing `/subrule/...` parent, the materializer now carves that parent subrule from the existing parent rule text before applying the child event.
  - This uses the same top-level subrule span logic already used for `SPLICE` and `SUBSTITUTE` target creation.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for a Rule 138A-style proviso insertion where `/rule/138a/subrule/1` is embedded in `/rule/138a` text but not split as a component.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 268
coverage_gap_count: 997
forms_lane_routed_count: 21
corrigendum_ledger_count: 34
```

Rule 138A impact:

```text
Materialized /rule/138a/subrule/1/proviso/providedfurtherthat-70c0a24702
Source event: evt_cbic_26d87e92b603535e
Source notification: 39/2018-Central Tax
Effect: imported goods bill-of-entry proviso now has a first-class component version.
```

Top-10 note:

```text
Overall coverage gaps: 998 -> 997
Rule 138 top-10 row remains gap_count=10 because this repair closed a related Rule 138A parent-missing gap, not one of the Rule 138 row gaps.
```

Confidence tiers:

```text
total_components=770
A=50, B=420, C=126, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'insert_child_subrule_parent or missing_subrule_target_from_parent_span or rule_117_1a_source_wrapper_event'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
3 passed, 187 deselected
190 passed in 99.04s
review_report.html regenerated at 2026-06-19 00:57:09 +0530
```

### Remaining Work

- Continue Rule 138/e-way-bill cleanup with the unvalidated `UNKNOWN` and form rows.
- Rule 138E is still missing as a parent for later clause/proviso amendments; it needs a source-backed first-class rule insertion before its child events can materialize.
- Rule 89 remains the largest top-10 cluster and still needs clause/statement routing cleanup.

## 2026-06-19 Iteration: Rule 138E Parent Creation

### Objective

Resolve the Rule 138E parent-missing blocker so later Rule 138E clause insertions can materialize against a first-class rule component.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Added a source-backed materializer repair for `evt_cbic_aafc21449b573369` from Notification `74/2018-Central Tax`.
  - Reclassified the extraction from the incorrect Rule 96 target to `INSERT_SIBLING` after Rule 138D.
  - Created `/in/union/rules/cgst-rules-2017/rule/138e` with the full source text for `138E. Restriction on furnishing of information in PART A of FORM GST EWB-01`.
  - Preserved the source caveat that the rule was inserted "from a date to be notified later" in repair metadata while keeping the current event date for coverage continuity until the commencement notification is linked.

- `tests/test_canonical_corpus.py`
  - Added regression coverage proving the repaired Rule 138E parent is created first and the Notification `75/2019-Central Tax` clause `(c)` insertion then applies.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 270
coverage_gap_count: 995
forms_lane_routed_count: 21
corrigendum_ledger_count: 34
```

Rule 138E impact:

```text
Materialized /rule/138e from Notification 74/2018-Central Tax.
Materialized /rule/138e/subrule/(c) from Notification 75/2019-Central Tax.
Removed the parent-missing gap for evt_cbic_249183a480b8385f.
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=771
A=50, B=420, C=127, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_138e_parent or insert_child_subrule_parent or rule_117_1a_source_wrapper_event'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
3 passed, 188 deselected
191 passed in 97.36s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 138E still has later unvalidated events to resolve, including `evt_cbic_0c43dc0b8e195471`, `evt_cbic_ed2a8531ecc39fe2`, and `evt_cbic_cbcd524d7f43f130`.
- Link the true Rule 138E commencement notification so the deferred effective-date caveat can be represented chronologically instead of only noted as repair metadata.
- Continue Rule 138/e-way-bill and Rule 89 top-10 repairs, keeping form and statement events out of the Rules text lane.

## 2026-06-19 Iteration: Rule 138E COVID Provisos And Opening Substitution

### Objective

Resolve the remaining source-proven Rule 138E chronology gaps after the Rule 138E parent component was created.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Repaired `evt_cbic_ed2a8531ecc39fe2` from Notification `79/2020-Central Tax` as a Rule 138E proviso insertion, effective `2020-03-20` from the express source text.
  - Repaired `evt_cbic_0c43dc0b8e195471` from Notification `15/2021-Central Tax` as a Rule 138E opening-phrase substitution.
  - Repaired `evt_cbic_cbcd524d7f43f130` from Notification `32/2021-Central Tax` as a Rule 138E proviso insertion, effective `2021-05-01` from the express source text.
  - Kept these repairs narrowly scoped to Rule 138E and preserved the original event IDs/source chains.

- `tests/test_canonical_corpus.py`
  - Extended the Rule 138E regression to cover parent creation, clause `(c)`, both COVID-period provisos, and the 2021 opening substitution.
  - Added assertions that the proviso label is not duplicated in materialized component text.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 273
coverage_gap_count: 992
forms_lane_routed_count: 21
corrigendum_ledger_count: 34
```

Rule 138E impact:

```text
Materialized /rule/138e/proviso/covid-2020 at 2020-03-20.
Materialized /rule/138e/proviso/covid-2021 at 2021-05-01.
Applied the 2021 substitution changing the opening phrase to "any outward movement of goods".
Rule 138E now has 8 node-version rows and no duplicated "Provided also" labels.
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=773
A=50, B=422, C=127, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_138e_parent'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
1 passed, 190 deselected
191 passed in 96.78s
review_report.html regenerated after confidence-tier refresh
Remaining local Rule 138E/e-way-bill gap: evt_cbic_649de854081f52c7 (Rule 138F insertion after Rule 138E)
```

### Remaining Work

- Repair `evt_cbic_649de854081f52c7` from Notification `38/2023-Central Tax` as the source-backed Rule 138F insertion after Rule 138E.
- Link the true Rule 138E commencement notification so the original deferred effective-date caveat can be represented chronologically.
- Continue Rule 138/e-way-bill and Rule 89 top-10 repairs, keeping form and statement events out of the Rules text lane.

## 2026-06-19 Iteration: Rule 138F Insertion After Rule 138E

### Objective

Resolve the remaining local Rule 138E/138F e-way-bill gap by materializing Rule 138F from Notification `38/2023-Central Tax`.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Repaired `evt_cbic_649de854081f52c7` from Notification `38/2023-Central Tax`.
  - Corrected the extraction target from `rule_10` to `/in/union/rules/cgst-rules-2017/rule/138f`.
  - Anchored the insertion after `/in/union/rules/cgst-rules-2017/rule/138e`.
  - Materialized the full Rule 138F text for intra-State movement of gold, precious stones, etc. and e-way bill generation.

- `tests/test_canonical_corpus.py`
  - Extended the Rule 138E sequence regression to include the bad Rule 138F extraction and assert that Rule 138F is materialized on `2023-08-04`.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 274
coverage_gap_count: 991
forms_lane_routed_count: 21
corrigendum_ledger_count: 34
```

Rule 138F impact:

```text
Materialized /rule/138f at 2023-08-04.
The local Rule 138E/138F e-way-bill cluster has no remaining gap rows mentioning Rule 138E or Rule 138F.
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=773
A=50, B=422, C=127, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_138e_parent'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
1 passed, 190 deselected
191 passed in 97.32s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Link the true Rule 138E commencement notification so the original deferred effective-date caveat can be represented chronologically.
- Continue Rule 138/e-way-bill cleanup outside the now-clean Rule 138E/138F local cluster.
- Continue Rule 89 top-10 repairs, keeping RFD-01 statement/form events out of the Rules text lane.

## 2026-06-19 Iteration: General Form-Mutation Routing Out Of Rules Gaps

### Objective

Keep explicit form substitutions and insertions out of the Rules text materializer once they have a first-class forms lane, starting with the Rule 100 form-only gaps.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Added a conservative first-class form mutation detector for excerpts that explicitly amend `FORM GST ...` and say the form shall be substituted, inserted, or omitted.
  - Routed those events to `forms_lane_routed_events` before Rules text materialization.
  - Preserved the existing RFD-01 statement/declaration routing behavior.

- `tests/test_canonical_corpus.py`
  - Added regression coverage proving a Rule 100 event that substitutes `FORM GST ASMT-13` is routed to the forms lane and no longer counted as a Rules coverage gap.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 274
coverage_gap_count: 940
forms_lane_routed_count: 72
corrigendum_ledger_count: 34
```

Routing impact:

```text
coverage_gap_count: 991 -> 940
forms_lane_routed_count: 21 -> 72
Rule 100 dropped out of the top-10 gap report after its form-only rows were routed.
```

Current top-10 target rules:

```text
rule/89, rule/96, rule/80, rule/138, rule/142, rule/36, rule/164, rule/46, rule/8, rule/24
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=773
A=50, B=422, C=127, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'routes_form_substitutions or routes_rfd01_statement'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
pytest tests/test_canonical_corpus.py -q
```

### Verification Results

```text
2 passed, 190 deselected
192 passed in 96.91s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Continue Rule 89 and Rule 96 event-resolution repairs from the refreshed top-10 report.
- Continue developing the forms lane beyond routing so routed non-RFD forms such as ASMT, DRC, REG, and EWB forms can be materialized first-class.
- Link the true Rule 138E commencement notification so the original deferred effective-date caveat can be represented chronologically.

## 2026-06-19 Iteration: Broader Rule 89 RFD-01 Statement Routing

### Objective

Continue the Phase 2 Rule 89 cleanup by routing RFD-01 Statement and Declaration amendments out of the Rules text materializer when they are clearly form-lane content.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Extended `_is_form_statement_event()` beyond `old_text` matching.
  - Now routes statement/declaration events based on:
    - payload `label` such as `Statement 5B`.
    - payload `node_type` of `statement` or `declaration`.
    - source excerpts such as `after Statement 5A, the following Statement shall be inserted`.
    - RFD-01 statement/table excerpts written as `FORM-GST-RFD-01` or `FORM GST RFD-01`.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for a Rule 89 `INSERT_SIBLING` Statement 5B event.
  - Added regression coverage for an UNKNOWN `FORM-GST-RFD-01` statement/table block.
  - Existing RFD-01 and generic form-routing tests continue to pass.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 271
coverage_gap_count: 929
forms_lane_routed_count: 86
corrigendum_ledger_count: 34
```

Routing impact:

```text
coverage_gap_count: 940 -> 929
forms_lane_routed_count: 72 -> 86
Rule 89 gap_count: 23 -> 19
Rule 89 form_statement blockers: 1 -> 0
```

Current top-10 target rules:

```text
rule/89, rule/96, rule/80, rule/138, rule/142, rule/36, rule/164, rule/46, rule/8, rule/24
```

Current top-10 lane counts:

```text
rule/89: 19 {'anchor_normalization': 2, 'event_resolution': 12, 'portal_completeness': 4, 'target_creation': 1}
rule/96: 15 {'anchor_normalization': 1, 'event_resolution': 10, 'portal_completeness': 4}
rule/80: 9 {'event_resolution': 9}
rule/138: 7 {'anchor_normalization': 1, 'event_resolution': 6}
rule/142: 7 {'anchor_normalization': 4, 'event_resolution': 3}
rule/36: 7 {'anchor_normalization': 5, 'event_resolution': 2}
rule/164: 6 {'event_resolution': 6}
rule/46: 7 {'anchor_normalization': 2, 'event_resolution': 4, 'portal_completeness': 1}
rule/8: 8 {'anchor_normalization': 1, 'event_resolution': 5, 'portal_completeness': 2}
rule/24: 4 {'anchor_normalization': 3, 'event_resolution': 1}
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=422, C=124, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'routes_statement_insert_sibling or routes_rfd01_statement_block or routes_rfd01_statement_events or routes_form_substitutions'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
pytest tests/test_canonical_corpus.py -q
python3 main.py version html-report
```

### Verification Results

```text
4 passed, 190 deselected
194 passed in 95.53s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Continue Rule 89 text repairs for the remaining real Rules events, especially Rule 89(2)(f), Rule 89(4)(C), Rule 89(5), and the 19/2022 compound block.
- Investigate the current applied-count baseline (`271`) against the previous handoff count (`274`) before assuming a legal regression; newly routed rows are needs-review form/statement rows, not validated Rules amendments.
- Build the first-class RFD-01 statement materializer far enough that routed Statement 1A, 4, 5, 5B, and Declaration rows become versioned form components rather than merely excluded from Rules gaps.

## 2026-06-19 Iteration: Rule 89 Third Proviso Text Repair

### Objective

Continue Rule 89 top-10 repairs by resolving a source-reviewed, validated Rule 89 text amendment that was still counted as an anchor-normalization gap.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a narrow materializer repair for `evt_cbic_a55addc1351a6797`.
  - The reviewed event came from Notification `47/2017-Central Tax` and substituted the third proviso to Rule 89(1).
  - The extractor preserved the positional phrase `third proviso` as `old_text`; the repair replaces the actual pre-amendment deemed-export proviso text in the reconstructed Rule 89 baseline.

- `tests/test_canonical_corpus.py`
  - Added regression coverage proving the Rule 89 deemed-export proviso changes from the recipient-only filing text to the recipient-or-supplier filing text.
  - The test asserts the event applies, produces no coverage gap, and preserves the Rule 89 version chain.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 272
coverage_gap_count: 928
forms_lane_routed_count: 86
corrigendum_ledger_count: 34
```

Repair impact:

```text
applied_count: 271 -> 272
coverage_gap_count: 929 -> 928
Rule 89 gap_count: 19 -> 18
Rule 89 anchor_normalization blockers: 2 -> 1
```

Current top-10 target rules:

```text
rule/89, rule/96, rule/80, rule/138, rule/142, rule/36, rule/164, rule/46, rule/8, rule/24
```

Current top-10 lane counts:

```text
rule/89: 18 {'anchor_normalization': 1, 'event_resolution': 12, 'portal_completeness': 4, 'target_creation': 1}
rule/96: 15 {'anchor_normalization': 1, 'event_resolution': 10, 'portal_completeness': 4}
rule/80: 9 {'event_resolution': 9}
rule/138: 7 {'anchor_normalization': 1, 'event_resolution': 6}
rule/142: 7 {'anchor_normalization': 4, 'event_resolution': 3}
rule/36: 7 {'anchor_normalization': 5, 'event_resolution': 2}
rule/164: 6 {'event_resolution': 6}
rule/46: 7 {'anchor_normalization': 2, 'event_resolution': 4, 'portal_completeness': 1}
rule/8: 8 {'anchor_normalization': 1, 'event_resolution': 5, 'portal_completeness': 2}
rule/24: 4 {'anchor_normalization': 3, 'event_resolution': 1}
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=422, C=124, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_89_third_proviso or routes_statement_insert_sibling or routes_rfd01_statement_block'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
pytest tests/test_canonical_corpus.py -q
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 192 deselected
195 passed in 96.16s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Continue Rule 89 repairs for the remaining true Rules text events: Rule 89(2)(f), Rule 89(4)(C), Rule 89(5), and the 19/2022 compound block.
- Continue separating remaining form/table rows into the first-class forms lane rather than merely excluding them from Rules coverage gaps.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.

## 2026-06-19 Iteration: Rule 89(2)(f) Clause Substitution Repair

### Objective

Continue Rule 89 top-10 repairs by resolving the target-creation blocker for Notification `03/2019-Central Tax`, which substituted Rule 89(2)(f).

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a narrow materializer repair for `evt_cbic_9cc935da632e9aac`.
  - The source event had complete replacement text, but targeted `/rule/89/subrule/2/clause/f`, which is not split as a standalone baseline component.
  - The repair retargets the operation to Rule 89 and replaces the actual pre-amendment clause (f) text in the reconstructed rule paragraph.

- `tests/test_canonical_corpus.py`
  - Added regression coverage proving Rule 89(2)(f) changes from the SEZ-unit ITC-not-availed declaration to the tax-not-collected declaration.
  - The test asserts the event applies with no coverage gap and the old clause text is removed from the post-amendment version.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 273
coverage_gap_count: 927
forms_lane_routed_count: 86
corrigendum_ledger_count: 34
```

Repair impact:

```text
applied_count: 272 -> 273
coverage_gap_count: 928 -> 927
Rule 89 gap_count: 18 -> 17
Rule 89 target_creation blockers: 1 -> 0
```

Current top-10 target rules:

```text
rule/89, rule/96, rule/80, rule/138, rule/142, rule/36, rule/164, rule/46, rule/8, rule/24
```

Current top-10 lane counts:

```text
rule/89: 17 {'anchor_normalization': 1, 'event_resolution': 12, 'portal_completeness': 4}
rule/96: 15 {'anchor_normalization': 1, 'event_resolution': 10, 'portal_completeness': 4}
rule/80: 9 {'event_resolution': 9}
rule/138: 7 {'anchor_normalization': 1, 'event_resolution': 6}
rule/142: 7 {'anchor_normalization': 4, 'event_resolution': 3}
rule/36: 7 {'anchor_normalization': 5, 'event_resolution': 2}
rule/164: 6 {'event_resolution': 6}
rule/46: 7 {'anchor_normalization': 2, 'event_resolution': 4, 'portal_completeness': 1}
rule/8: 8 {'anchor_normalization': 1, 'event_resolution': 5, 'portal_completeness': 2}
rule/24: 4 {'anchor_normalization': 3, 'event_resolution': 1}
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=422, C=124, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_89_subrule_2_clause_f or rule_89_third_proviso'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
pytest tests/test_canonical_corpus.py -q
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 194 deselected
196 passed in 97.62s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Continue Rule 89 repairs for the remaining true Rules text events: Rule 89(4)(C), Rule 89(5), and the 19/2022 compound block.
- Classify or route remaining Rule 89 UNKNOWN rows that are still event-resolution blockers.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.

## 2026-06-19 Iteration: Rule 89(4)(C) Clause Substitution Repair

### Objective

Continue Rule 89 top-10 repairs by resolving the anchor-normalization blocker for Notification `16/2020-Central Tax`, which substituted Rule 89(4)(C).

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a narrow materializer repair for `evt_cbic_e23ce17aa2de96c4`.
  - The event source span and local notification text prove the replacement clause, but the reviewed payload captured only `clause (4)` and no replacement text.
  - The repair retargets the operation to Rule 89 and replaces the pre-2020 `Turnover of zero-rated supply of goods` clause with the 1.5-times like-goods valuation text from Notification `16/2020-Central Tax`.

- `tests/test_canonical_corpus.py`
  - Added regression coverage proving Rule 89(4)(C) gains the `1.5 times the value of like goods domestically supplied` language.
  - The test asserts the event applies with no coverage gap and preserves the Rule 89 version chain.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 274
coverage_gap_count: 926
forms_lane_routed_count: 86
corrigendum_ledger_count: 34
```

Repair impact:

```text
applied_count: 273 -> 274
coverage_gap_count: 927 -> 926
Rule 89 gap_count: 17 -> 16
Rule 89 anchor_normalization blockers: 1 -> 0
```

Current top-10 target rules:

```text
rule/89, rule/96, rule/80, rule/138, rule/142, rule/36, rule/164, rule/46, rule/8, rule/24
```

Current top-10 lane counts:

```text
rule/89: 16 {'event_resolution': 12, 'portal_completeness': 4}
rule/96: 15 {'anchor_normalization': 1, 'event_resolution': 10, 'portal_completeness': 4}
rule/80: 9 {'event_resolution': 9}
rule/138: 7 {'anchor_normalization': 1, 'event_resolution': 6}
rule/142: 7 {'anchor_normalization': 4, 'event_resolution': 3}
rule/36: 7 {'anchor_normalization': 5, 'event_resolution': 2}
rule/164: 6 {'event_resolution': 6}
rule/46: 7 {'anchor_normalization': 2, 'event_resolution': 4, 'portal_completeness': 1}
rule/8: 8 {'anchor_normalization': 1, 'event_resolution': 5, 'portal_completeness': 2}
rule/24: 4 {'anchor_normalization': 3, 'event_resolution': 1}
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=422, C=124, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_89_subrule_4_clause_c or rule_89_subrule_2_clause_f or rule_89_third_proviso'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
pytest tests/test_canonical_corpus.py -q
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 194 deselected
197 passed in 97.90s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Continue Rule 89 repairs for the remaining true Rules text events: Rule 89(5) and the 19/2022 compound block.
- Classify or route remaining Rule 89 UNKNOWN rows that are still event-resolution blockers.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.

## 2026-06-19 Iteration: Rule 89(5) Formula Substitution Repair

### Objective

Continue Rule 89 repairs by resolving the event-resolution blocker for Notification `14/2022-Central Tax`, which substituted the Rule 89(5) inverted-duty formula deduction phrase.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a narrow materializer repair for `evt_cbic_d8f4a0a217fe1492`.
  - The reviewed payload omitted the old and new formula text, but the source span and TaxInformation portal both identify the substitution for `tax payable on such inverted rated supply of goods and services`.
  - The repair retargets the operation to Rule 89 and substitutes the formula deduction with `{tax payable on such inverted rated supply of goods and services x (Net ITC ÷ ITC availed on inputs and input services)}`.

- `tests/test_canonical_corpus.py`
  - Added regression coverage proving the Rule 89(5) formula event applies, creates a new version from the notification date, and does not remain a coverage gap.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 275
coverage_gap_count: 925
forms_lane_routed_count: 86
corrigendum_ledger_count: 34
```

Repair impact:

```text
applied_count: 274 -> 275
coverage_gap_count: 926 -> 925
Rule 89 gap_count: 16 -> 15
Rule 89 event_resolution blockers: 12 -> 11
Rule 89 portal_completeness blockers: 4 -> 4
```

Current Rule 89 blockers:

```text
rule/89: 15 {'event_resolution': 11, 'portal_completeness': 4}
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=422, C=124, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_89_subrule_5_formula or rule_89_subrule_4_clause_c or rule_89_subrule_2_clause_f'
python3 main.py version materialize
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
pytest tests/test_canonical_corpus.py -q
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 195 deselected
198 passed in 96.81s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Continue Rule 89 repairs for the 19/2022 compound block and remaining true Rules text events.
- Classify or route remaining Rule 89 UNKNOWN rows that are still event-resolution blockers.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.
- Continue first-class forms lane materialization so RFD-01 statement evidence is no longer represented as Rules text incompleteness.

## 2026-06-19 Iteration: Rule 89(1) 19/2022 Compound Block Repair

### Objective

Resolve the remaining true Rule 89 text blocker from Notification `19/2022-Central Tax`, where one source block amended Rule 89(1) by inserting the cash-ledger refund phrase, omitting the first proviso, and renumbering the following proviso openings.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a source-backed materializer repair for `evt_cbic_28517a6b6d58aacb`.
  - The reviewed event was a single `SPLICE` with `compound_block_contains_multiple_amendments` and `compound_block_contains_unsupported_omission`.
  - The repair applies the compound legal effect as one contiguous `SUBSTITUTE` over the affected Rule 89(1) opening span, preserving the event chain while avoiding same-date multi-edit conflicts in the current materializer.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for the 19/2022 Rule 89(1) compound block.
  - The test proves the post-event text contains `claiming refund of any balance in the electronic cash ledger`, omits the old cash-ledger-return proviso, and renumbers deemed-export text to `Provided further that`.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 276
coverage_gap_count: 924
forms_lane_routed_count: 86
corrigendum_ledger_count: 34
```

Repair impact:

```text
applied_count: 275 -> 276
coverage_gap_count: 925 -> 924
Rule 89 gap_count: 15 -> 14
Rule 89 event_resolution blockers: 11 -> 10
Rule 89 portal_completeness blockers: 4 -> 4
```

Current Rule 89 blockers:

```text
rule/89: 14 {'event_resolution': 10, 'portal_completeness': 4}
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=422, C=124, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_89_19_2022_compound or rule_89_subrule_5_formula'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 197 deselected
199 passed in 96.89s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Classify or route remaining Rule 89 UNKNOWN rows that are form/statement/table mutations or bad extractions.
- Investigate the validated-but-rescinded Rule 89(5) event `evt_cbic_b2652b978e5f3741` so rescission does not remain a Rule 89 blocker if its effect is superseded.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.
- Continue first-class forms lane materialization so RFD-01 statement evidence is represented outside Rules text.

## 2026-06-19 Iteration: Rule 89 Forms-Lane Routing And Already-Reflected Cleanup

### Objective

Remove RFD-01 statement/form fragments and a proven duplicate Rule 88C row from Rule 89 Rules-text coverage gaps, keeping them out of the Rules materializer until the first-class forms lane owns those components.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Broadened first-class form routing to catch `the following Statement shall be inserted/substituted` fragments.
  - Added routing for numbered RFD-01 instruction fragments, including declaration blocks, Statement instructions, and Rule 89(4) instruction lines embedded in FORM GST RFD-01.
  - Added routing for form serial-number mutations such as FORM GST TRAN-2 serial-number substitutions that were mis-targeted to Rule 89.
  - Marked `evt_cbic_803f696fd8e1d231` as already reflected by canonical XML event `evt_cbic_xml_cff41664511daf24`; the reviewed row targeted Rule 89, but its own source text is a Rule 88C splice that is already materialized.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for RFD-01 statement fragments, RFD-01 declaration/instruction fragments, FORM GST TRAN-2 serial-number routing, and the already-reflected Rule 88C duplicate row.

### Artifact Impact

Rules materialization:

```text
event_count: 1387
applied_count: 275
coverage_gap_count: 908
forms_lane_routed_count: 102
corrigendum_ledger_count: 34
```

Routing impact:

```text
applied_count: 276 -> 275
coverage_gap_count: 924 -> 908
forms_lane_routed_count: 86 -> 102
Rule 89 top10 gap_count: 14 -> 4
Rule 89 top10 event_resolution blockers: 10 -> 0
Rule 89 top10 portal_completeness blockers: 4 -> 4
```

Note: `applied_count` decreases by one because one previously retry-applied form-like event is now routed to the forms lane instead of being treated as Rules text.

Current Rule 89 top-10 blockers:

```text
rule/89: 4 {'portal_completeness': 4}
portal_missing::20/2024
portal_missing::26/2022
portal_missing::38/2021
portal_missing::48/2017
```

Underlying Rule 89 coverage rows still present outside the portal-augmented top-10 view:

```text
evt_cbic_b2652b978e5f3741: notification_rescinded
evt_cbic_777f0208dde2bedb: event_status_not_validated
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=769
A=50, B=422, C=123, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'routes_rfd01_statement_fragments or routes_form_serial_mutations or already_reflected_rule_88c'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 199 deselected
202 passed in 97.10s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Resolve or classify `evt_cbic_777f0208dde2bedb`; the source block is Notification `35/2021-Central Tax` Rule 89 text, but the reviewed row is only a wrapper fragment.
- Investigate `evt_cbic_b2652b978e5f3741`; it is a validated Rule 89(5) substitution whose source notification is marked rescinded.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.
- Continue first-class forms lane materialization for routed RFD-01 statements and declarations.

## 2026-06-19 Iteration: Rule 89 35/2021 Commencement Repair

### Objective

Resolve the remaining Rule 89 wrapper extraction gap for Notification `35/2021-Central Tax` by materializing the source-backed Rule 89(1) and Rule 89(1A) effects with the correct commencement chronology from Notification `38/2021-Central Tax`.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a narrow materializer repair for `evt_cbic_777f0208dde2bedb`.
  - Split the wrapper row into two validated materializer events:
    - `evt_cbic_777f0208dde2bedb_rule89_1_rule10b_splice`: inserts `, subject to the provisions of rule 10B,` after `may file` in Rule 89(1).
    - `evt_cbic_777f0208dde2bedb_rule89_1a_insert`: inserts new Rule 89(1A) for section 77 refund applications.
  - Set both repaired events to `applicability_start=2022-01-01` with `date_basis=commencement_notification_38_2021_rule_2_subrule_2`.
  - Updated the existing Notification `19/2022-Central Tax` Rule 89(1) compound repair to expect the already-applied Rule 10B phrase, preserving chronological sequencing.

- `tests/test_canonical_corpus.py`
  - Added regression coverage for the 35/2021 Rule 89 repair and 38/2021 commencement date.
  - Updated the 19/2022 Rule 89(1) compound repair fixture so it starts from the post-35/2021 text.

### Artifact Impact

Rules materialization:

```text
event_count: 1388
applied_count: 277
coverage_gap_count: 907
forms_lane_routed_count: 102
corrigendum_ledger_count: 34
```

Rule 89 impact:

```text
evt_cbic_777f0208dde2bedb: event_status_not_validated -> applied as two repaired events
Rule 89 raw non-portal coverage rows: 2 -> 1
Remaining raw Rule 89 non-portal row:
  evt_cbic_b2652b978e5f3741: notification_rescinded
Rule 89 top10 gap_count: 4
Rule 89 top10 lane_counts: {'portal_completeness': 4}
```

Current Rule 89 top-10 portal blockers:

```text
portal_missing::20/2024
portal_missing::26/2022
portal_missing::38/2021
portal_missing::48/2017
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=770
A=50, B=423, C=123, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_89_35_2021_commenced_subrule_1_and_1a'
pytest tests/test_canonical_corpus.py -q -k 'rule_89_35_2021_commenced_subrule_1_and_1a or rule_89_19_2022_compound or rule_89_subrule_5_formula'
python3 main.py version materialize
python3 main.py version portal-completeness
pytest tests/test_canonical_corpus.py -q
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
1 passed, 202 deselected
3 passed, 200 deselected
203 passed in 97.54s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Investigate `evt_cbic_b2652b978e5f3741`; it is a validated Rule 89(5) substitution whose source notification is marked rescinded.
- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.
- Continue first-class forms lane materialization for routed RFD-01 statements and declarations.
- Continue duplicate/already-reflected cleanup beyond the narrow Rule 88C row already resolved.

## 2026-06-19 Iteration: Exact Rescinded Notification Matching

### Objective

Fix the remaining raw Rule 89 non-portal coverage gap by determining whether Notification `26/2018-Central Tax` was genuinely rescinded or was being caught by an over-broad rescission matcher.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Tightened `_doc_id_matches_rescinded` so rescinded notification numbers match exact notification references only.
  - Prevented `6/2018` from matching `26/2018-Central Tax`.
  - Preserved matching for both slash and hyphen forms such as `6/2018-Central Tax` and `/2018/6-2018`.

- `tests/test_canonical_corpus.py`
  - Added regression assertions that `6/2018` does not match `26/2018-Central Tax` or `/2018/26-2018`.

### Artifact Impact

Rules materialization:

```text
event_count: 1388
applied_count: 282
coverage_gap_count: 902
forms_lane_routed_count: 102
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Rule 89 impact:

```text
evt_cbic_b2652b978e5f3741: notification_rescinded -> applied
Rule 89 raw non-portal coverage rows: 1 -> 0
Rule 89 top10 gap_count: 4
Rule 89 top10 lane_counts: {'portal_completeness': 4}
```

Current Rule 89 top-10 blockers remain portal-source gaps:

```text
portal_missing::20/2024
portal_missing::26/2022
portal_missing::38/2021
portal_missing::48/2017
```

Portal completeness:

```text
rule_count: 129
missing_source_notification_count: 61
external_reference_notification_count: 10
```

Confidence tiers:

```text
total_components=773
A=50, B=426, C=123, D=174
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/node_versions.jsonl`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'doc_id_matches_rescinded or preprocess_special_ops_flags_rescinded_events'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 201 deselected
203 passed in 96.97s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Resolve portal-source gaps for Rule 89 notifications `20/2024`, `26/2022`, `38/2021`, and `48/2017`.
- Continue first-class forms lane materialization for routed RFD-01 statements and declarations.
- Continue duplicate/already-reflected cleanup beyond the narrow Rule 88C row already resolved.
- Review other top-10 rules affected by the rescission matcher fix; rescinded-event skips dropped from 16 to 1, so several formerly skipped source-valid events now materialize.

## 2026-06-19 Iteration: Portal Completeness Classification Split

### Objective

Tighten portal completeness so `missing_source_notification` means the source notification is absent from the event ledger, not merely unlinked to a clean rule-targeted event. This addresses Rule 89 portal blockers after the Rule text gaps were cleared.

### Changes Made

- `src/legal_corpus/portal_completeness.py`
  - Added parsing for bare event instrument numbers such as `20/2024-Central Tax`.
  - Added extraction of commencement references from `legal_time.date_basis`, e.g. `commencement_notification_38_2021_rule_2_subrule_2`.
  - Classified benefit-notification citations such as `benefit of notification No. 48/2017-Central Tax` as `external_reference_notification`.
  - Added `source_present_unlinked_notification` classification when a portal-listed notification exists in the ledger but is not linked to that specific rule.
  - Kept top-10 portal blockers limited to true missing-source notifications.

- `tests/test_canonical_corpus.py`
  - Extended portal completeness tests for benefit-notification external references.
  - Added coverage for source-present-but-unlinked portal references and commencement refs from date basis metadata.

### Artifact Impact

Rules materialization was unchanged from the prior iteration:

```text
event_count: 1388
applied_count: 282
coverage_gap_count: 902
forms_lane_routed_count: 102
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Portal completeness:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Rule 89 portal impact:

```text
48/2017: missing_source_notification -> external_reference_notification
20/2024: missing_source_notification -> source_present_unlinked_notification
26/2022: missing_source_notification -> source_present_unlinked_notification
38/2021: missing_source_notification -> source_present_unlinked_notification
Rule 89 top10 entry: removed
```

Confidence tiers:

```text
total_components=773
A=53, B=426, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'portal_completeness'
python3 main.py version portal-completeness
pytest tests/test_canonical_corpus.py -q
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 201 deselected
204 passed in 98.12s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Resolve the Rule 89 `source_present_unlinked_notification` rows by repairing/linking the underlying `20/2024`, `26/2022`, and `38/2021` event evidence where needed.
- Continue first-class forms lane materialization for routed RFD-01 statements and declarations.
- Continue duplicate/already-reflected cleanup beyond the narrow Rule 88C row already resolved.
- Work through the current top-10 gap report now that Rule 89 has dropped out.

## 2026-06-19 Iteration: Named Form Block Routing

### Objective

Reduce Rules text gaps caused by full-form insertions/substitutions that were still being counted against rule provisions, especially GSTR-9/GSTR-9C annual-return forms under Rule 80 and DRC/ENR form blocks around Rules 138 and 142.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added named-form block detection for amendments phrased as `after/for/in FORM ... the following FORM(S) shall be inserted/substituted/omitted`.
  - Added quoted form-header detection for both bracketed and unbracketed `See rule` form headers, including `FORM GSTR-9C See rule 80(3)`.
  - Routed these events to the forms lane instead of reporting them as Rules text materialization gaps.

- `tests/test_canonical_corpus.py`
  - Added coverage for named form block routing across GSTR-9 insertion, GSTR-9 substitution, and GST DRC-01A substitution examples.

### Artifact Impact

Rules materialization:

```text
event_count: 1388
applied_count: 280
coverage_gap_count: 885
forms_lane_routed_count: 121
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Top-10 movement:

```text
rule/80: 9 -> 5 gaps
rule/138: 7 -> 6 gaps
rule/142: 7 -> 6 gaps
rule/164: 6 -> 5 gaps
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=773
A=53, B=426, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'named_form_blocks or routes_form_serial_mutations or routes_rfd01_statement_fragments'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 202 deselected
205 passed in 97.92s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 80 still has five real rule/event-resolution rows, including annual-return deadline/threshold notifications and a Rule 80(3) proviso substitution.
- Rules 138 and 142 still need anchor/sequencing repairs for genuine rule text amendments after the form-only blocks were removed.
- Continue building first-class form materialization so routed form blocks become cited form histories instead of only being excluded from Rules text gaps.

## 2026-06-19 Iteration: Rule 142(4) Section 74A Repair

### Objective

Continue Stage 4 top-10 rule text repairs by applying a source-proven Rule 142 amendment from Notification 20/2024-Central Tax that was previously blocked as a same-date whole-rule conflict.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a narrow materializer repair for `evt_cbic_4e3d7e920de72e7d`.
  - Retargeted the event from whole Rule 142 to `/in/union/rules/cgst-rules-2017/rule/142/subrule/4`.
  - Preserved the source event chain while applying the insertion after `of section 74`: `or sub-section (6) of section 74A`.

- `tests/test_canonical_corpus.py`
  - Added a focused regression test proving that the Rule 142(4) repair creates a subrule-level version containing `sub-section (6) of section 74A`.

### Artifact Impact

Rules materialization:

```text
event_count: 1388
applied_count: 281
coverage_gap_count: 884
forms_lane_routed_count: 121
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Top-10 movement:

```text
rule/142: 6 -> 5 gaps
remaining rule/142 lanes: anchor_normalization=3, event_resolution=2
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers:

```text
total_components=774
A=53, B=427, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_142_20_2024_subrule_4_section_74a'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
1 passed, 205 deselected
206 passed in 96.10s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 142 still has five gaps: the 2020 compound Rule 142(1A) substitution, the 2022 wrapper row, the 2019 Rule 142(2) insertion, the 2024 Rule 142(2A) DRC-01A insertion, and the 2024 Rule 142(1A) section 74A insertion.
- The remaining Rule 142 rows need stronger sequencing/anchor normalization because the current reconstructed parent text does not expose the exact source anchors for subrules 1A and 2A.
- Continue Rule 138 and Rule 80 repairs after choosing similarly source-proven, narrowly materializable rows.

## 2026-06-19 Iteration: Rule 24 Deadline Chain Repair

### Objective

Continue Stage 4 top-10 rule text repairs by resolving the Rule 24(4) migration-deadline extension chain, where the parent Rule 24 text had the amended deadline but the split subrule component remained at the stale baseline text.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added materializer repairs for:
    - `evt_cbic_ba5635bfca1625c3` from Notification 36/2017-Central Tax.
    - `evt_cbic_f14c69c16e5d3094` from Notification 51/2017-Central Tax.
    - `evt_cbic_daa2627a546cc11a` from Notification 03/2018-Central Tax.
  - Retargeted the three Rule 24(4) deadline substitutions from the stale split subrule component to the parent Rule 24 text where the current deadline phrase is materialized.
  - Normalized the source-extracted line-break variants in the old text while preserving each source event chain.

- `tests/test_canonical_corpus.py`
  - Added a focused regression test proving the three-event Rule 24 deadline chain materializes through `on or before 31st March, 2018`.

### Artifact Impact

Rules materialization:

```text
event_count: 1388
applied_count: 284
coverage_gap_count: 881
forms_lane_routed_count: 121
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Top-10 movement:

```text
rule/24: removed from top gap report
rule/24 prior state: 4 gaps, including 3 anchor-normalization failures
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=774
A=53, B=427, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_24_deadline_chain'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
1 passed, 206 deselected
207 passed in 97.48s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 23 still has one anchor-normalization row around the `may file`/`may submit` wording for Rule 10B commencement.
- Rule 138 still has one Annexure/table anchor row that likely belongs in a table-aware lane before safe materialization.
- Rule 142 still needs sequencing/anchor repair for subrules 1A and 2A where the current parent text does not expose the exact source anchors.

## 2026-06-19 Iteration: Rule 23 Rule 10B Commencement Repair

### Objective

Continue Stage 4 top-10 rule text repairs by resolving the Rule 23(1) Rule 10B condition inserted by Notification 35/2021-Central Tax and brought into force by Notification 38/2021-Central Tax from 01.01.2022.

### Changes Made

- `src/legal_corpus/version_snapshots.py`
  - Added a materializer repair for `evt_cbic_cc2573caef6b8746`.
  - Preserved the source event chain while correcting the effective date to `2022-01-01` with `date_basis=commencement_notification_38_2021_rule_2_subrule_2`.
  - Reconciled the source anchor `may file` against the reconstructed Rule 23 wording `may submit` by applying a parent Rule 23 substitution:
    `may submit` -> `may, subject to the provisions of rule 10B, submit`.
  - Restored the prior Rule 142(4) repair operation to `SPLICE` after catching an accidental local edit during this iteration.

- `tests/test_canonical_corpus.py`
  - Added a focused regression test proving the Rule 23 repair materializes from `2022-01-01` and contains the Rule 10B condition.
  - Re-ran the existing Rule 142(4) focused repair test alongside the new Rule 23 test to guard the restored operation.

### Artifact Impact

Rules materialization:

```text
event_count: 1388
applied_count: 285
coverage_gap_count: 880
forms_lane_routed_count: 121
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Top-10 movement:

```text
rule/23: removed from top gap report
rule/23 prior state: 3 gaps, including 1 anchor-normalization failure
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=774
A=53, B=427, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_23_10b_condition'
pytest tests/test_canonical_corpus.py -q -k 'rule_142_20_2024_subrule_4_section_74a or rule_23_10b_condition'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
1 passed, 207 deselected
2 passed, 206 deselected
208 passed in 97.87s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 138 still has one Annexure/table anchor row for Chapter 71 that should likely move to a table-aware lane before materialization.
- Rule 142 still needs sequencing/anchor repair for subrules 1A and 2A where current reconstructed parent text does not expose the exact source anchors.
- Rule 26 now appears in the top report with two anchor-normalization rows and one event-resolution row; inspect it before choosing the next text repair.

## 2026-06-19 Iteration: Rule 26 EVC Proviso Chain Cleanup

### Scope

Resolved the Rule 26 top-gap cluster around the Companies Act registered-person EVC provisos and the already-reflected Chapter IV insertion.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Marked `evt_cbic_be6914a5115d56f3` as rejected/already-reflected because Notification 10/2017 inserts Chapter IV after Rule 26 and the baseline already carries that chapter/rules from the same source.
  - Split `evt_cbic_7a1f16dca7f92d9f` into two materializer repair events:
    - `..._gstr3b_period`: substitutes `30th day of June, 2020` with `30th day of September, 2020` on the existing Rule 26(1) GSTR-3B EVC proviso child.
    - `..._gstr1_proviso`: inserts the source-proven GSTR-1 EVC proviso as a first-class Rule 26(1) proviso child.
  - Retargeted `evt_cbic_8e62222199d9ad8c` to the Rule 26(1) fourth proviso child, where `31st day of May, 2021` actually resolves, and substitutes it with `31st day of August, 2021`.

- `tests/test_canonical_corpus.py`
  - Added focused tests for the Rule 26 EVC proviso chain and the already-reflected Chapter IV insertion.

### Artifact Impact

Rules materialization:

```text
event_count: 1389
applied_count: 288
coverage_gap_count: 877
forms_lane_routed_count: 121
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Top-gap movement:

```text
rule/26: removed from refreshed top gap report
rule/26 prior state: 3 gaps, including 2 anchor-normalization rows and 1 event-resolution row
rule/61: now appears in the refreshed top report with 3 gaps
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers:

```text
total_components=775
A=53, B=428, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_26_evc_proviso_chain or rule_26_chapter_heading'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 208 deselected
210 passed in 96.11s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 138 still has one Annexure/table anchor row for Chapter 71 that should likely move to a table-aware lane before materialization.
- Rule 142 still needs sequencing/anchor repair for subrules 1A and 2A where current reconstructed parent text does not expose the exact source anchors.
- Rule 61 newly appears in the refreshed top report with two event-resolution rows and one anchor-normalization row; inspect before choosing the next text repair.

## 2026-06-19 Iteration: Rule 61 Direct Text And Metadata-Only Cleanup

### Scope

Resolved two of the three newly surfaced Rule 61 top-gap rows: the direct 2017 Rule 61(5) word substitution and the 2018 GSTR-3B due-date extension notification that does not amend Rule 61 text.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Added a materializer repair for `evt_cbic_b852babb7c93019c`, retargeting the Notification 22/2017 substitution to `/rule/61/subrule/5` and recording its explicit effective date as `2017-07-01`.
  - Added a metadata-only rejection for `evt_cbic_a41be90129af4539` because Notification 62/2018 amends Notification 34/2018 filing-date extensions under Rule 61(5), not the Rule 61 text.
  - Extended non-gap rejected-event handling so `payload.metadata_only=true` rejected events are not counted as text coverage gaps.

- `tests/test_canonical_corpus.py`
  - Added focused tests for the Rule 61(5) substitution and metadata-only notification-extension handling.

### Artifact Impact

Rules materialization:

```text
event_count: 1389
applied_count: 289
coverage_gap_count: 875
forms_lane_routed_count: 121
rescinded_event_count: 1
corrigendum_ledger_count: 34
```

Top-gap movement:

```text
rule/61: removed from refreshed top gap report
rule/61 prior state: 3 gaps, including 1 anchor-normalization row and 2 event-resolution rows
rule/7: now appears in the refreshed top report with 3 gaps
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=775
A=53, B=428, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_61_subrule_5_2017_substitution or rule_61_notification_extension'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 210 deselected
212 passed in 96.81s
review_report.html regenerated after confidence-tier refresh
```

### Remaining Work

- Rule 138 still has one Annexure/table anchor row for Chapter 71 that should likely move to a table-aware lane before materialization.
- Rule 142 still needs sequencing/anchor repair for subrules 1A and 2A where current reconstructed parent text does not expose the exact source anchors.
- Rule 7 now appears in the refreshed top report with two event-resolution rows and one anchor-normalization row; inspect before choosing the next text repair.
- Rule 61 still has the deeper Notification 49/2019 retrospective Rule 61(5)/(6) rewrite in the global coverage gaps. It should be handled in the corrigenda/retrospective chronology lane rather than as a quick anchor normalization.

## 2026-06-19 Iteration: Rule Table Lane Routing For Rule 7

### Scope

Moved rule-table mutations out of the prose Rules materializer when the notification text targets a Rule table rather than a rule/subrule sentence. This addressed the refreshed Rule 7 cluster and one Rule 138 distance-table row without changing applied prose behavior.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Added rule-table mutation detection for Rule-targeted events whose evidence says a Table cell or whole Table is substituted, inserted, or omitted.
  - Added `routed_to_rules_table_lane` gap records and manifest fields:
    - `rules_table_lane_routed_count`
    - `rules_table_lane_routed_events`
  - Guarded the router so already validated/materializable prose events remain applied.

- `tests/test_canonical_corpus.py`
  - Added a regression test proving Rule table mutations route out of Rules text gaps while a validated prose event with incidental table/form wording still applies.

### Artifact Impact

Rules materialization:

```text
event_count: 1389
applied_count: 289
coverage_gap_count: 871
forms_lane_routed_count: 121
rules_table_lane_routed_count: 4
corrigendum_ledger_count: 34
```

Routed table-lane events:

```text
evt_cbic_7044fc15c2cd35bb  rule/7 table rate-cell substitutions
evt_cbic_345635753a671931  rule/7 serial 3 column 3 cell substitution
evt_cbic_83648ef72ce89ca5  rule/7 whole Table substitution
evt_cbic_941ffeaee19206cc  rule/138(10) distance-table substitutions
```

Top-gap movement:

```text
rule/7: removed from refreshed top gap report
rule/138: reduced to 5 gaps
coverage_gap_count: 875 -> 871
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=775
A=53, B=428, C=135, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_table_mutations or form_substitutions_out_of_rules_gaps'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 211 deselected
213 passed in 97.00s
review_report.html regenerated after portal/confidence refresh
```

### Current Refreshed Top Queue

```text
rule/96: 11 gaps
rule/36: 7 gaps
rule/46: 7 gaps
rule/8: 7 gaps
rule/138: 5 gaps
rule/142: 5 gaps
rule/164: 5 gaps
rule/80: 5 gaps
rule/87: 4 gaps
rule/45: 3 gaps
```

### Remaining Work

- Build the first-class rule-table lane, analogous to the RFD-01 statement lane, so routed table edits can materialize with source spans instead of remaining parked.
- Rule 138 still needs inspection for non-table Annexure/Chapter 71 and sequencing issues after the distance-table row moved out.
- Rule 142 remains the next priority text repair from the original plan, especially subrules 1A and 2A.
- Portal completeness gaps are unchanged and should continue to be treated as coverage blockers even when no event exists in the ledger.

## 2026-06-19 Iteration: Rule 142 Sequencing Repair For 1A/2A

### Scope

Repaired the Rule 142 amendment chain where later source-proven events could not apply because the 2019 Rule 142 substitution was truncated and the 49/2019 insertion of subrules 1A and 2A had not created first-class components. This clears the concrete Rule 142 anchor failures for subrules 1A and 2A while leaving the remaining unsupported 40/2021 wrapper as an explicit unresolved event.

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Replaced the truncated `evt_cbic_ba1f7eea0bfef2d7` Rule 142 structural payload with the full Notification 16/2019 substituted rule text from the source archive.
  - Split `evt_cbic_2c1e4044c813bf37` from Notification 49/2019 into:
    - Rule 142(1A) insertion.
    - Rule 142(2) own-ascertainment splice.
    - Rule 142(2A) insertion.
  - Split `evt_cbic_9869af72fcfd6dc2` from Notification 79/2020 into the two ordered Rule 142(1A) substitutions.
  - Retargeted `evt_cbic_d56f9b1cfb5e2603` to Rule 142(2A).
  - Retargeted `evt_cbic_e6ed17e56a016068` to Rule 142(1A), distinct from the existing Rule 142(4) section 74A repair.

- `tests/test_canonical_corpus.py`
  - Added a Rule 142 sequencing regression that starts from the old parent rule text, applies the 16/2019 full substitution, creates 1A/2A from 49/2019, and then applies the 79/2020, 12/2024, and 20/2024 edits against those first-class components.

### Artifact Impact

Rules materialization:

```text
event_count: 1392
applied_count: 296
coverage_gap_count: 867
forms_lane_routed_count: 121
rules_table_lane_routed_count: 4
corrigendum_ledger_count: 34
```

Applied Rule 142 repairs:

```text
evt_cbic_ba1f7eea0bfef2d7                                full Rule 142 substitution
evt_cbic_2c1e4044c813bf37_rule142_1a_insert              Rule 142(1A)
evt_cbic_2c1e4044c813bf37_rule142_2_own_ascertainment_splice
evt_cbic_2c1e4044c813bf37_rule142_2a_insert              Rule 142(2A)
evt_cbic_9869af72fcfd6dc2_proper_officer_may             Rule 142(1A)
evt_cbic_9869af72fcfd6dc2_communicate                    Rule 142(1A)
evt_cbic_d56f9b1cfb5e2603                                Rule 142(2A)
evt_cbic_e6ed17e56a016068                                Rule 142(1A)
```

Top-gap movement:

```text
rule/142: removed from refreshed top gap report
rule/142 remaining global gap: evt_cbic_3a0aefde8de80afe (Notification 40/2021 UNKNOWN wrapper)
coverage_gap_count: 871 -> 867
applied_count: 289 -> 296
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers moved because Rule 142(1A) and Rule 142(2A) now have first-class versions:

```text
total_components=777
A=53, B=429, C=136, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_142_1a_2a_sequence or rule_142_20_2024_subrule_4'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 212 deselected
214 passed in 96.47s
review_report.html regenerated after portal/confidence refresh
```

### Current Refreshed Top Queue

```text
rule/96: 11 gaps
rule/36: 7 gaps
rule/46: 7 gaps
rule/8: 7 gaps
rule/138: 5 gaps
rule/164: 5 gaps
rule/80: 5 gaps
rule/87: 4 gaps
rule/92: 4 gaps
rule/45: 3 gaps
```

### Remaining Work

- Resolve or classify the remaining Rule 142 Notification 40/2021 UNKNOWN wrapper; it is no longer a top-queue blocker.
- Rule 138 still needs a table/Annexure-aware lane for the Chapter 71 Annexure row and event classification for the remaining wrapper rows.
- Rule 96 is now the largest top blocker and should be inspected next.
- Portal completeness blockers remain unchanged and should continue to feed confidence-tier decisions.

## 2026-06-19 Iteration: Rule 96(3) GSTR-3B Anchor Repair

### Scope

Resolved the concrete Rule 96 anchor-normalization row from Notification 19/2022. The source amends Rule 96(3), but the reviewed event targeted parent Rule 96 with a line-break/spacing variant of `FORM GSTR-3`. The repair retargets the substitution to first-class Rule 96(3), avoiding the separate `FORM GSTR-3` occurrence in Rule 96(1).

### Code Changes

- `src/legal_corpus/version_snapshots.py`
  - Added a materializer repair for `evt_cbic_7aabc9f26c533448`.
  - Retargeted the event to `/in/union/rules/cgst-rules-2017/rule/96/subrule/3`.
  - Normalized the old text to `FORM GSTR-3` and substituted `FORM GSTR-3B`.

- `tests/test_canonical_corpus.py`
  - Added a regression fixture where parent Rule 96 contains two `FORM GSTR-3` occurrences but subrule 3 contains the legally amended occurrence. The test proves only Rule 96(3) is amended.

### Artifact Impact

Rules materialization:

```text
event_count: 1392
applied_count: 297
coverage_gap_count: 866
forms_lane_routed_count: 121
rules_table_lane_routed_count: 4
corrigendum_ledger_count: 34
```

Top-gap movement:

```text
rule/96: 11 -> 10 gaps
rule/96 anchor_normalization: 1 -> 0
coverage_gap_count: 867 -> 866
applied_count: 296 -> 297
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=777
A=53, B=429, C=136, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_96_subrule_3_gstr3b_substitution or rule_142_1a_2a_sequence'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 213 deselected
215 passed in 97.95s
review_report.html regenerated after portal/confidence refresh
```

### Current Refreshed Top Queue

```text
rule/96: 10 gaps
rule/36: 7 gaps
rule/46: 7 gaps
rule/8: 7 gaps
rule/138: 5 gaps
rule/164: 5 gaps
rule/80: 5 gaps
rule/87: 4 gaps
rule/92: 4 gaps
rule/45: 3 gaps
```

### Remaining Work

- Rule 96 still has ten event-resolution gaps. The validated Rule 96(10) substitution/omission chain remains explicit because parent Rule 96 lacks a unique embedded subrule 10 span; the current materializer creates detached component versions and correctly treats parent integration as incomplete.
- Several Rule 96 UNKNOWN wrapper rows should be split or routed, especially the 96A/96B/96C-adjacent rows that may belong to sibling-rule or form lanes.
- Rule 36, Rule 46, and Rule 8 are now tied for the largest non-Rule-96 top queues.

## 2026-06-19 Iteration: Form Instruction Routing For Rule 46

### Objective

Route form instruction amendments, specifically Rule 46-adjacent `FORM GSTR-1` instruction text, out of the Rules text coverage-gap queue and into the first-class forms lane.

### Code Changes

- Added `_RE_FORM_INSTRUCTION_MUTATION_TEXT` in `src/legal_corpus/version_snapshots.py`.
- Included the new detector in `_is_first_class_form_mutation_event`.
- Added regression coverage in `tests/test_canonical_corpus.py`:
  - `test_rule_materializer_routes_form_instruction_mutations_out_of_rules_gaps`

### Current Counts After Regeneration

```text
event_count: 1392
applied_count: 297
coverage_gap_count: 854
forms_lane_routed_count: 133
rules_table_lane_routed_count: 4
corrigendum_ledger_count: 34
```

Top-gap movement from the prior baseline:

```text
coverage_gap_count: 866 -> 854
forms_lane_routed_count: 121 -> 133
rule/46: 7 -> 6 blockers
```

The new regex routed more than the single inspected Rule 46 row because the corpus contains a cluster of form instruction fragments with the same legal shape. These are now explicit forms-lane items, not Rules text gaps.

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=777
A=53, B=429, C=136, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'form_instruction_mutations or form_serial_mutations or named_form_blocks'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 213 deselected
216 passed in 96.79s
review_report.html regenerated at derived/version_history/review_report.html
```

### Current Refreshed Top Queue

```text
rule/96: 10 gaps
rule/36: 7 gaps
rule/8: 7 gaps
rule/138: 5 gaps
rule/164: 5 gaps
rule/80: 5 gaps
rule/87: 4 gaps
rule/92: 4 gaps
rule/45: 3 gaps
rule/46: 6 blockers including portal completeness
```

### Remaining Work

- Rule 36 still has the largest non-Rule-96 text queue and needs sequencing repair for the Rule 36(4) insertion/substitution chain.
- Rule 8 still contains RFD-01 form fragments and registration-rule text gaps; the RFD-01 fragments should be routed once the detector is broadened for `FORM RFD-01` variants.
- Rule 46 remaining blockers are now real Rule 46 text/proviso issues plus one portal completeness blocker, not GSTR-1 instruction-form rows.

## 2026-06-19 Iteration: RFD-01 Variant Routing For Rule 8

### Objective

Route RFD-01 form content that was incorrectly counted under Rule 8 into the forms lane, including `FORM RFD-01` variants without the `GST` token and RFD-01 instruction fragments with incorrect rule targets.

### Code Changes

- Broadened `_RE_RFD01_STATEMENT_TEXT` in `src/legal_corpus/version_snapshots.py` to match both `FORM GST RFD-01` and `FORM RFD-01`.
- Removed the Rule 89 target restriction for `_RE_RFD01_INSTRUCTION_FRAGMENT`; these fragments are identifiable by content and may be mis-targeted by extraction.
- Added regression coverage in `tests/test_canonical_corpus.py`:
  - `test_rule_materializer_routes_rfd01_variants_mistargeted_to_rule_8_out_of_rules_gaps`

### Current Counts After Regeneration

```text
event_count: 1392
applied_count: 297
coverage_gap_count: 852
forms_lane_routed_count: 135
rules_table_lane_routed_count: 4
corrigendum_ledger_count: 34
```

Top-gap movement from the prior iteration:

```text
coverage_gap_count: 854 -> 852
forms_lane_routed_count: 133 -> 135
rule/8: 7 -> 5 blockers
```

The production rows routed in this iteration:

```text
evt_cbic_83e3ed1312aba96d -> routed_to_forms_lane
evt_cbic_60500a38590fae21 -> routed_to_forms_lane
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=777
A=53, B=429, C=136, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rfd01_variants_mistargeted_to_rule_8 or rfd01_statement_fragments or rfd01_statement_block'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
3 passed, 214 deselected
217 passed in 97.26s
review_report.html regenerated at derived/version_history/review_report.html
```

### Current Refreshed Top Queue

```text
rule/96: 10 gaps
rule/36: 7 gaps
rule/138: 5 gaps
rule/164: 5 gaps
rule/8: 5 blockers including portal completeness
rule/80: 5 gaps
rule/87: 4 gaps
rule/92: 4 gaps
rule/45: 3 gaps
rule/46: 6 blockers including portal completeness
```

### Remaining Work

- Rule 36 is now the largest non-Rule-96 text queue and should be next; its Rule 36(4) insertion/substitution chain needs sequencing repair rather than forms routing.
- Remaining Rule 8 rows are registration-rule text or portal completeness issues, not RFD-01 statement leakage.
- Continue routing form/table events before attempting broader compound-event extraction.

## 2026-06-19 Iteration: Rule 36(3) Section 74 Parent Anchor Repair

### Objective

Apply Notification 20/2024-Central Tax’s Rule 36(3) insertion of `under section 74` after `suppression of facts`, despite the stale split Rule 36(3) child component lacking the source anchor.

### Code Changes

- Added a materializer repair for `evt_cbic_4274a8ccd0fc33f3` in `src/legal_corpus/version_snapshots.py`.
- The repair retargets the event to parent `/in/union/rules/cgst-rules-2017/rule/36`, where the Rule 36(3) sentence is present.
- The source insertion is applied as a punctuation-preserving equivalent substitution:
  - `suppression of facts.`
  - `suppression of facts under section 74.`
- Added regression coverage in `tests/test_canonical_corpus.py`:
  - `test_materializer_repairs_rule_36_subrule_3_section_74_parent_anchor`

### Current Counts After Regeneration

```text
event_count: 1392
applied_count: 298
coverage_gap_count: 851
forms_lane_routed_count: 135
rules_table_lane_routed_count: 4
corrigendum_ledger_count: 34
```

Top-gap movement from the prior iteration:

```text
coverage_gap_count: 852 -> 851
applied_count: 297 -> 298
rule/36: 7 -> 6 blockers
rule/36 anchor_normalization: 5 -> 4
```

The production row applied in this iteration:

```text
evt_cbic_4274a8ccd0fc33f3 -> applied as SUBSTITUTE on parent Rule 36
changed_components: /in/union/rules/cgst-rules-2017/rule/36
```

Portal completeness remains:

```text
missing_source_notification_count: 12
source_present_unlinked_notification_count: 46
external_reference_notification_count: 12
```

Confidence tiers remain:

```text
total_components=777
A=53, B=429, C=136, D=159
```

### Regenerated Artifacts

- `derived/version_history/cgst-rules-2017/materialization_manifest.json`
- `derived/version_history/cgst-rules-2017/coverage_gaps.json`
- `derived/version_history/cgst-rules-2017/top10_gap_report.json`
- `derived/version_history/portal_completeness_report.json`
- `derived/version_history/confidence_tiers.json`
- `derived/version_history/review_report.html`

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'rule_36_subrule_3_section_74_parent_anchor or rule_96_subrule_3_gstr3b_substitution'
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
2 passed, 216 deselected
218 passed in 96.54s
review_report.html regenerated at derived/version_history/review_report.html
```

### Current Refreshed Top Queue

```text
rule/96: 10 gaps
rule/36: 6 blockers
rule/46: 6 blockers including portal completeness
rule/138: 5 gaps
rule/164: 5 gaps
rule/8: 5 blockers including portal completeness
rule/80: 5 gaps
rule/87: 4 gaps
rule/92: 4 gaps
rule/45: 3 gaps
```

### Remaining Work

- Rule 36 still needs deeper sequencing for the Rule 36(4) 49/2019 insertion, 75/2019 percentage substitution, and 94/2020 compound substitution/splice block.
- Rule 46 has remaining real Rule text/proviso blockers plus portal completeness.
- Rule 96 remains the largest unresolved queue and will require sibling-rule/form routing plus subrule 10 parent integration work.

## Phase 2 Milestone 1: Context Recovery And Lane Triage

Completed on 2026-06-19.

Implemented `python3 main.py version context-recovery` as a post-hoc recovery pass over `derived/version_history/amendment_events_reviewed.jsonl`. The pass preserves event IDs and source provenance, prefers full source archive text under `sources/.../extracted_text.json`, falls back to notification `contentPdfBase64` extraction when needed, and writes:

- `derived/version_history/context_recovery_decisions.json`
- `derived/version_history/context_recovery_report.json`
- `derived/version_history/llm_reextraction_candidates.json`
- `derived/version_history/llm_reextraction_report.json`

Recovery output:

```text
input_count: 1386
context_recovered_count: 551
forms_lane_pending_baseline_count: 642
metadata_only_count: 7
context_unresolved_count: 186
canonical_id_normalized_count: 27
llm_reextraction_candidate_count: 34
```

Materializer manifest now exposes the explicit Phase 2 lane counts:

```text
event_count: 1392
applied_count: 86
coverage_gap_count: 415
metadata_only_count: 8
context_recovered_count: 541
forms_lane_pending_baseline_count: 637
context_unresolved_count: 183
forms_lane_routed_count: 26
rules_table_lane_routed_count: 2
conflict_count: 2
```

Added `derived/version_history/form_registry.json` with the top-five pending structured baselines:

```text
gst-rfd-01
gst-drc-03
gstr-1
gst-drc-01
gst-tran-1
```

Regenerated `derived/version_history/review_report.html` at this major milestone.

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'context_recovery'
python3 main.py version context-recovery
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
5 passed, 218 deselected
223 passed in 97.04s
portal-completeness completed; top10 gap report rebuilt
confidence tiers: total=721, A=43, B=371, C=124, D=183
review_report.html regenerated at derived/version_history/review_report.html
```

### Remaining Work

- The current recovery heuristic is intentionally conservative for materialization: recovered targets remain non-materializable unless their existing payload/anchor was already deterministically valid.
- The high form-lane count is now explicit and should be handled by the forms baseline expansion milestone before more Rules text surgery.
- Corrigendum application is still pending; `corrigendum_ledger.jsonl` remains generated but corrections are not yet applied to affected payloads/anchors.

## Phase 2 Milestone 2: Forms Pending-Baseline Registry

Completed on 2026-06-19.

Integrated `derived/version_history/form_registry.json` into the forms materializer. `python3 main.py version materialize-forms` now consumes the registry by default, keeps the top-five priority forms in `forms_lane_pending_baseline`, and preserves the existing RFD-01 statement materialization lane.

Registry-pending forms:

```text
gst-rfd-01
gst-drc-03
gstr-1
gst-drc-01
gst-tran-1
```

Forms materializer output:

```text
event_count: 284
form_amendment_count: 81
statement_amendment_count: 39
applied_count: 71
statement_applied_count: 39
forms_lane_pending_baseline_count: 568
coverage_gap_count: 779
form_registry_pending_baseline_count: 5
missing_form_count: 20
```

Rules materializer output remained stable:

```text
event_count: 1392
applied_count: 86
coverage_gap_count: 415
metadata_only_count: 8
context_recovered_count: 541
forms_lane_pending_baseline_count: 637
context_unresolved_count: 183
forms_lane_routed_count: 26
rules_table_lane_routed_count: 2
conflict_count: 2
```

Regenerated `derived/version_history/review_report.html` at this major milestone.

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'form_registry or rfd01_statement_materializer or materialize_form_versions or routes_form'
python3 main.py version materialize-forms
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
5 passed, 219 deselected
224 passed in 97.16s
portal-completeness completed; top10 gap report rebuilt
confidence tiers: total=721, A=43, B=371, C=124, D=183
review_report.html regenerated at derived/version_history/review_report.html
```

### Remaining Work

- Corrigendum application remains the next major milestone.
- The registry currently records pending baselines only; structured baselines still need to be added before these forms can safely leave `forms_lane_pending_baseline`.
- Full-form materialization for non-registry-pending forms still includes carry-forward rows and should not be treated as court-ready without later baseline/provenance hardening.

## Phase 2 Milestone 3: Corrigendum Application

Completed on 2026-06-19.

Added `python3 main.py version corrigendum-application` and `src/legal_corpus/corrigenda.py`. The pass parses corrigendum correction pairs, maps them to corrected notification refs, applies only exact deterministic replacements to matching event payload/target/evidence text fields, and preserves original/corrected provenance in `derived/version_history/corrigendum_application_report.json`.

Corrigendum application output:

```text
corrigendum_count: 34
corrigenda_with_corrections_count: 7
candidate_patch_scan_count: 4
applied_event_count: 4
```

Patched event IDs:

```text
evt_cbic_bb9cd43a5aceb5f1
evt_cbic_3583f97db8f3cfc9
evt_cbic_6d93f741ca4bb916
evt_cbic_d54fbe115db92573
```

Each patched event now carries `payload.corrigendum_applications[]` with:

```text
original event/source span in corrigendum_application_report.json
corrigendum event ID
corrigendum source span and hash
old/new correction text
patched field path
old/new field hashes
retrospective flag
date basis
```

Rules and forms materialization remained stable after the corrigendum pass:

```text
rules applied_count: 86
rules coverage_gap_count: 415
forms applied_count: 71
forms statement_applied_count: 39
forms_lane_pending_baseline_count: 568
```

Regenerated `derived/version_history/review_report.html` at this major milestone.

### Commands Run

```bash
pytest tests/test_canonical_corpus.py -q -k 'corrigendum'
python3 main.py version corrigendum-application
python3 main.py version materialize-forms
python3 main.py version materialize
pytest tests/test_canonical_corpus.py -q
python3 main.py version portal-completeness
python3 main.py version confidence-tiers
python3 main.py version html-report
```

### Verification Results

```text
6 passed, 219 deselected
225 passed in 96.92s
portal-completeness completed; top10 gap report rebuilt
confidence tiers: total=721, A=43, B=371, C=124, D=183
review_report.html regenerated at derived/version_history/review_report.html
```

### Remaining Work

- Corrigenda without parsed correction pairs remain ledger-only; fuller source text extraction would be needed to recover more.
- The application pass intentionally does not mark corrected events materializable or resolve anchors by itself.
- Next major work should rebuild the top-gap queue and perform surgical fixes for Rule 36(4), Rule 46 provisos, Rule 45 job-worker sequencing, Rule 138 annexure/table references, and spacing variants like `FORM GSTR- 1`.
