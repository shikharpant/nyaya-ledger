# Handoff: GST Version History Court-Readiness

Updated: 2026-06-22
Workspace: `/home/shikhar/openclaw-workspace/Projects/Git_for_Law`

## Verified State

Current verification commands completed:

- `python3 main.py version reconcile ...`
- `python3 main.py version confidence-tiers`
- `python3 main.py version phase3-backlog`
- `python3 main.py version html-report`

Focused regression tests completed:

- Rule 39/Rule 87 materializer regressions: `3 passed`
- Confidence tier regressions: `6 passed`
- Rule 46A and reconciliation-normalization regressions: `5 passed`
- Rule 124/125 and Rule 109A materializer regressions: `2 passed`
- Rule 107 baseline decontamination regression: `2 passed`
- Rule 96A materializer regressions: `4 passed`
- Rule 31A materializer regressions: `2 passed`
- Rule 129/130 compound split and reconciliation audit regressions: `3 passed`
- Rule 119, Rule 120A, and reconciliation footnote regressions: `4 passed`
- Rule 49 and Rule 94 materializer/reconciliation regressions: `3 passed`
- Full canonical corpus suite: `343 passed`

Test result: `343 passed`.

## Materialization

Coverage is not the active blocker.

```text
Rules: 384 applied, 0 coverage gaps
Act:   132 applied, 0 coverage gaps
Forms: 608 applied, 0 coverage gaps
```

## Reconciliation

The external TaxInformation checkpoint remains the validation source:

```text
checkpoint_date: 2026-06-17
checkpoint_source: derived/version_history/reconciliation_sources/current-taxinformation/checkpoint.xml
checkpoint_source_manifest: present
checkpoint_source_warnings: none
```

Current reconciliation outcome counts:

```text
exact_match:                    5
format_only_match:             71
minor_substantive_difference:   6
omitted_correct:               17
comparison_invalid:            13
true_substantive_mismatch:     56
```

New audit artifact:

```text
derived/version_history/cgst-rules-2017/reconciliation_unresolved_audit.json
```

Unresolved audit classes:

```text
compound_split_needed: 40
missing_substitution:  12
missing_insert_child:  2
manual_backfill_needed: 2
```

Each unresolved row now includes candidate event ids, candidate source documents, hashes, similarity, checkpoint preview, and a recommended action.

## Confidence

Current confidence tiers:

```text
A:  92
B: 577
C:  56
D:   0
Total: 725
```

Current citeable percentage: `92.3%` (`A+B`). There are no Tier D rule components.
The 56 Tier C components remain advisory because they have true substantive
checkpoint mismatches with source diagnostics.

## What Changed In This Pass

- Reconciliation now writes `unresolved_reconciliation_audit` inside the report.
- Reconciliation also writes `reconciliation_unresolved_audit.json` next to the report.
- Priority queue rows now include audit class, candidate event count, candidate event ids, candidate source documents, and recommended action.
- `phase3-backlog` consumes the reconciliation audit and groups the 60 unresolved items by audit class.
- Reconciliation audit classification now excludes form/table/outside-work UNKNOWN
  noise when choosing the audit class, while preserving those events as evidence
  candidates in the row.
- HTML report shows audit class, candidate count, candidate source docs, and event ids in the reconciliation table.
- Confidence tiers now treat a `needs_review` event as non-blocking when the materialization manifest proves that exact event changed the same component or a parent/child node.
- Confidence tiers also consume `already_reflected_events`, applied `INSERT_SIBLING` anchor evidence, and misrouted-but-applied events with `target_component_outside_work`.
- Confidence tiers now also consume manifest-backed `context_unresolved_events`
  so source-proven dependent context gaps remain Tier C instead of Tier D.
- Rule 39(1A) is materialized from Notification 12/2024 and then amended by
  Notification 13/2025; the earlier parser drift that overwrote Rule 54(1A) is
  regression-covered.
- Heading-prefix reconciliation normalization is guarded so near-zero similarity rows remain unresolved.
- Reconciliation annotation normalization now strips TaxInformation footnotes like
  `Inserted (w.e.f. ...) videNotification...`, including the glued footnote
  marker and extra-parenthesis variant seen on Rule 47A. This resolved Rules
  `16A`, `47A`, and one additional annotation-only row from the unresolved queue.
- Rule 46A now materializes the complete source-backed 2017 insertion from
  Notification 45/2017 and the correct 2022 proviso from Notification 26/2022.
  Reconciliation treats the remaining checkpoint delta as format-only
  TaxInformation footnote/proviso-label structure.
- Rules 97A and 107A now reconcile as format-only TaxInformation duplicated
  label/heading prefixes. Rule 120A remains a true mismatch because the
  reconstructed text is missing an opening legal sentence; a negative regression
  test prevents that from being normalized away.
- Rule 121 now reconciles as format-only parser drift after stripping a trailing
  standalone chapter heading bleed from substantive comparison.
- Rules 124 and 125 now materialize as omitted from Notification 24/2022 clause
  2(b), split from the mis-targeted compound omission row and regression-covered.
- Rule 109A now applies both Notification 60/2018 substitutions to the
  materialized parent rule text, replacing the clause (b) phrase in sub-rules
  (1) and (2); it now reconciles as format-only checkpoint drift.
- Rule 107 now reconciles as format-only after baseline decontamination strips
  the trailing `Chapter - XIII Appeals and Revision` heading that the source
  repair stage had glued to the rule body.
- Rule 96A now materializes the Notification 51/2017 provisos, the Notification
  12/2024 clause (b) substitution, and the reviewed 2024 `FORM GSTR-1A` splice
  against the parent Rule 96A text. The previous context-recovery prefix match
  attached the 2024 clause (b) event to Rule 96; this is now regression-covered.
- Rule 31A now materializes the complete source-backed 2018 insertion, the clean
  2020 sub-rule (2) substitution, and the later 2025 rate substitution. It now
  reconciles as format-only checkpoint drift, and regressions prevent
  notification note/signature contamination.
- Rule 129 now receives the source-backed Notification 29/2018 anti-profiteering
  substitution split from the same compound event that also amended Rule 130.
  Rule 130 now applies the substitution to the parent rule text and reconciles
  as format-only checkpoint drift after annotation normalization strips
  TaxInformation `Substituted for the word ... vide Notification...` footnotes.
- Rule 38 now materializes the source-backed Notification 19/2022 clause
  `(a)(ii)` omission, clause `(c)` substitution, and clause `(d)` omission. It
  now reconciles as format-only checkpoint drift caused by TaxInformation
  annotation markers, removing the last `missing_omit` audit row and increasing
  applied Rules events to 380.
- Rule 119 now retargets the Notification 34/2017 time-limit substitution from
  the incorrectly compiled Rule 117 target to Rule 119. This improved Rule 119
  similarity, but it remains in the source-audited `missing_substitution` queue.
- Rule 120A now materializes its source-backed Notification 34/2017 insertion
  and Notification 36/2017 marginal-heading update. It now reconciles as
  format-only checkpoint drift, removing one manual-backfill item and increasing
  applied Rules events to 382.
- Rule 49 now materializes the source-backed Notification 74/2018 electronic
  bill of supply signature proviso and Notification 31/2019 QR-code proviso. It
  now reconciles as format-only checkpoint drift, reducing the
  `missing_insert_child` queue.
- Rule 94 now splits Notification 38/2023 into a parent sub-rule `(1)`
  renumbering and sub-rule `(2)` insertion, with reconciliation normalization
  for TaxInformation heading and inline-marker noise. It now reconciles as
  format-only checkpoint drift, increasing applied Rules events to 384.
- `plan.md` was replaced with the current reconciliation-focused recovery plan.

## Next Work

1. Keep Tier D at `0`.
2. Generalize compiler compound splitting for the 40 `compound_split_needed` rows.
3. Work the separated queues: 12 `missing_substitution` and 2 `missing_insert_child`.
4. Use source-backed manual backfills only for the 2 narrow hard cases.

Target for 95%+ citeable confidence:

- Tier D: `0`
- `A+B >= 95%`
- all materialization coverage gaps remain `0`
- remaining Tier C rows, if any, retain source attribution and diagnostics
