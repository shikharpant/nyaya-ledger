# GST Rules Reconciliation — Verified Status

**Date:** 2026-08-03
**Scope:** `/in/union/rules/cgst-rules-2017` event-sourced version history vs. the
TaxInformation checkpoint XML (2026-06-17).
**Purpose:** Record the verified reconciliation state against the targets in
`plan.md`, and classify the residual `apply_failed` coverage gaps.

## Summary

The `plan.md` execution-order targets are **met and exceeded**. The compound-split,
missing-substitution, and missing-insert-child queues it describes are stale — they
were resolved in committed code (the `063ba17`/`f85d208`/`b8b6534` batch and earlier
materializer/compiler work) and reflected in the local `derived/` artifacts. This
document records that verified state so a future run does not re-open closed work.

## Verified metrics (regenerated this session)

Commands run from a clean tree at `b8b6534`:

```
python3 main.py version materialize
python3 main.py version reconcile \
  --target-work /in/union/rules/cgst-rules-2017 \
  --checkpoint-path derived/version_history/reconciliation_sources/current-taxinformation/checkpoint.xml \
  --checkpoint-date 2026-06-17 \
  --output derived/version_history/cgst-rules-2017/reconciliation_report.json \
  --version-dir derived/version_history/cgst-rules-2017
python3 main.py version confidence-tiers
python3 main.py version phase3-backlog
```

| Metric | `plan.md` target | Verified value |
|---|---|---|
| Rules coverage gaps | 0 | 0 (coverage = `complete`) |
| Reconciliation unresolved | ↓ toward 0 | **0** |
| Tier D components | 0 | **0** |
| A+B citeable share | ≥ 95% | **100.0%** (A=722, B=1, C=0, D=0, total=723) |
| phase3-backlog actionable count | ↓ toward 0 | **0** |

Reconciliation outcome counts: `exact_match=193`, `omitted_correct=19`,
`mismatched=0`, `missing=0`, `unresolved_reconciliation_count=0`.

The Tier D invariant ("never silently downgrade a component to make numbers look
better") holds: there are zero Tier D components and zero Tier C components.

## Note on `plan.md` staleness

`plan.md` (dated 2026-06-22) describes a queue of 40 `compound_split_needed`,
12 `missing_substitution`, 2 `missing_insert_child`, and 2 `manual_backfill_needed`
rows, and an A=92 / B=577 / C=56 breakdown. That state predates the materializer
and compiler fixes that landed between 2026-06-22 and the `b8b6534` head. After
re-running `materialize` + `reconcile` + `confidence-tiers` + `phase3-backlog`
against the current committed code, the audit classes are empty
(`audit_class_counts = {}`) and the confidence distribution is A=722 / B=1 / C=0 /
D=0. In other words, the queue `plan.md` says to work through has already been
drained. There is no actionable reconciliation backlog to process for this work.

## Residual: 4 `apply_failed` coverage gaps (all benign)

`materialization_manifest.json` reports `coverage_gap_count = 4`. None of these
four produce a checkpoint mismatch (reconciliation `unresolved_reconciliation_count`
is 0), and none drop any component below Tier B. They are events the materializer
correctly skipped because the target state is already present. They are recorded
here with their source citations for traceability; none requires a Tier D
downgrade or a fabricated backfill.

| # | Event | Date | Op | Target | Source instrument | `skip_reason` | Classification |
|---|---|---|---|---|---|---|---|
| 1 | `evt_cbic_ad72e292d9f041d1` | 2022-12-26 | SUBSTITUTE | rule 8 sub-rule 4B | Notification 4/2023-Central Tax | "Target component has no editable content paragraph" | Benign: text correct via baseline path; checkpoint matches. |
| 2 | `evt_cbic_eb18afb06f304825` | 2023-08-04 | SUBSTITUTE | rule 46 proviso `providedthat-de982caa10` | Notification 38/2023-Central Tax | "Target component has no editable content paragraph" | Benign structural no-op; checkpoint matches. |
| 3 | `evt_cbic_63c066be5d58113c` | 2024-11-01 | SUBSTITUTE | rule 46 ("Provided also" → "Provided further") | Notification 20/2024-Central Tax | "Substitution text not found" | Benign: the anchor was already rewritten to "Provided further" by an earlier event; this event is redundant. |
| 4 | `evt_cbic_11b43e13ef68e44e` | 2017-07-01 | INSERT_SIBLING | rule 88B (after rule 88A) | Notification 14/2022-Central Tax | "Inserted sibling already exists: rule/88b" | Benign: rule 88B already present; insert is a definitive no-op. |

### Optional follow-up (not blocking, not court-readiness)

Events 1 and 2 share the "no editable content paragraph" failure mode, which
indicates a structural mismatch between the event's target anchor and the
materialized node shape (the proviso / sub-rule exists but is not addressable as
an editable paragraph by the substitution operator). Because the checkpoint text
already matches, this is a materializer-addressability refinement, not a legal-text
defect. It can be taken up under a separate materializer-hardening task without
affecting reconciliation numbers. No action is required for the 95% citeable
target, which is already exceeded.

## Reproducibility note

`derived/` is gitignored, so the artifacts above are local-only. The CODE that
produces them is committed at `b8b6534` and pushed to `origin/main`. Re-running
the four commands in "Verified metrics" on a clean checkout regenerates the same
A=722 / B=1 / C=0 / D=0 outcome and the same `apply_failed` residual of 4.
