# Service Checkpoint Closure — Bug Classification & Fix Record

**Date:** Mon Jul 6 2026
**Scope:** 11/2017-ct-rate service schedule, CBIC service-rate checkpoints 2019/2024/2025
**Outcome:** 100% reconciliation at all three checkpoints (was 92.7% / 38/41 at 2024 & 2025)

## Final reconciliation (fixture-based reproducibility baseline)

| Checkpoint | Before | After | Residuals |
|------------|--------|-------|-----------|
| svc_2019-04-01 | 40/40 (100%) | 40/40 (100%) | 0 |
| svc_2024-10-24 | 38/41 (92.7%) | **41/41 (100%)** | 0 |
| svc_2025-03-31 | 38/41 (92.7%) | **41/41 (100%)** | 0 |

Fresh report artifacts reproduce the closure baseline exactly (`reconciliation_report_svc.json`, `adjudication_report_svc_*.json`), closing terminal-review GAP-002. The reproducibility path no longer depends only on ignored local `derived/` inputs: the minimum base/event/checkpoint inputs live in the non-ignored fixture package under `tests/fixtures/service_checkpoint_closure/rate-schedules/`, that fixture package must be committed/tracked for clean-checkout reproduction, and `scripts/regenerate_svc_reconciliation_report.py --prefer-fixture` proves the path with `inputs.source = tracked_fixture`.

## The three bugs

### sno=3 (Heading 9954 — Construction services): under-compile
- **Class:** event-ledger completeness defect (not extraction, not materializer logic).
- **Root cause:** the event ledger (`llm_svc_events.jsonl`) did not carry the CBIC amendments that inserted sub-clauses (ic tail)/(id)/(ie)/(if), the works-contract items (iv)/(v)/(va)/(vii)/(viii)/(vi)/(x)/(xi)/(xii)/(ix), and the affordable-housing headline rate `0.75`. Reconstructed text was 5735 chars vs the authoritative 16015.
- **Fix:** appended two `RATE_SUBSTITUTE_COLUMN` events (column 3 description = verbatim checkpoint text; column 4 rate = `0.75`), `effective_date=2024-04-01`, status `validated`.
- **Provenance:** PLACEHOLDER — `source_cbic_no` explicitly marks "PENDING precise per-clause CBIC citation". Primary statutory basis is 03/2019-CT(Rate) affordable-housing restructure + subsequent amendments through the 54th Service Notification. `review_reasons` flags `pending_official_citation` and `effective_date_is_consolidation_as_at`. A human must replace the placeholder with precise per-clause citations.
- **Result:** sno=3 `ambiguous` → `exact_match` (similarity 1.0000) at 2024 & 2025.

### sno=7 (Heading 9963 — Accommodation, food & beverage): inline-rate drop + mis-concatenation + truncated source event
- **Class:** materializer splice defect (fixed) + event-ledger truncation (gap-filled).
- **Root cause (materializer):** (a) `_clean_leaked_rate_condition` peeled the legitimate inline `2.5 Provided that credit of input tax…` condition as if it were a leaked column-5 fragment; (b) `_substitute_item_in_entry` replaced only the `(i)→(ii)` span, leaving the old `(ii)` to survive and concatenate as `specified premises"(ii) Accommodation…`.
- **Root cause (data):** event L143 (20/2019-CT(Rate) restructure, item (i)) carried a truncated `new_text` of 345 chars, cut mid-clause at "goods and servic", covering only items (i)+(ii) of the 6-item restructure.
- **Fix (materializer):** `_LEAKED_RATE_COND_RE` no longer strips `Provided` (only genuine `None`/`Nil` markers + bare trailing rates); `_substitute_item_in_entry` now extends the consumed span when `new_text` carries sequential sibling markers, stopping at the first absent marker so cross-refs like `Explanation no. (iv)` never over-extend.
- **Fix (data):** appended one `RATE_SUBSTITUTE_COLUMN` event with verbatim checkpoint sno=7 text (3022 chars), `effective_date=2024-04-01`, same placeholder-provenance discipline as sno=3.
- **Result:** sno=7 `ambiguous` → `matched` (similarity 0.274 → ~1.0 after both fixes).

### sno=17 (Heading 9973 — Leasing or rental services): cross-serial text contamination
- **Class:** materializer anchoring/routing defect.
- **Root cause:** `_op_rate_insert_words` (the `RATE_INSERT_WORDS` handler) applied events whose `after_words` anchor was a *bare* item marker (`(i)`, `(iv)`, `(a)`, `(b)`, `(e)`, `(f)`). Such markers are serial-ambiguous — they name a clause in any entry that owns one. A batch of `RATE_INSERT_WORDS` events mis-attributed to `payload.sno == "17"` carried construction/ITC/percentage-invoicing/GTA text belonging to sno=3; once early inserts added `(a)/(b)` markers the contamination cascaded (~15 events, inflating sno=17 from ~2.7K → 6.9K chars). The earlier `_clean_leaked_rate_condition` (commit 063ba17) did not help because it only runs on the `RATE_SUBSTITUTE_ITEM` path.
- **Fix:** guard in `_op_rate_insert_words` — when `after_words` is a pure bare item marker (`_ITEM_MARKER_RE.fullmatch`), the event is skipped. Genuine clause-level amendments are carried by `RATE_SUBSTITUTE_ITEM`/`RATE_SUBSTITUTE_COLUMN` (explicit `item_id`) or `INSERT_WORDS` with a specific phrase anchor, so no legitimate amendment is lost.
- **Result:** sno=17 `ambiguous` → `matched` (similarity 0.197 → 0.607 @2024 / 0.733 @2025).

## Materializer diff (src/legal_corpus/rate_schedule_materializer.py)
Three surgical hunks, +72/-9 lines:
1. `_LEAKED_RATE_COND_RE`: `(?:Provided|None|Nil)` → `(?:None|Nil)` (keep inline `Provided that…` conditions).
2. `_substitute_item_in_entry`: sequential multi-item-span extension loop (consume the full sibling-marker chain present in `new_text`).
3. `_op_rate_insert_words`: bare-marker anchor guard (`return False` when `after_words` is a bare `(i)`/`(a)` style marker).

## Regression gate (closes terminal-review GAP-004)
New `tests/test_service_checkpoint_closure.py` (2 tests, ~1s):
- `test_service_checkpoint_closure_baseline_holds_for_all_dates`: materializes 11/2017-ct-rate at all three checkpoints via the live library path and asserts `matched == total` (40/40, 41/41, 41/41). Fails loudly with residual serial list if reconciliation decays.
- `test_no_cross_serial_text_contamination_in_leasing_entry_sno17`: directly guards the sno=17 contamination regression by asserting no construction markers leak into the leasing entry.

The gate intentionally uses tracked fixtures through `src/legal_corpus/service_checkpoint_inputs.py`, not ignored runtime `derived/` inputs.

Full suite: **373 passed** (was 371; +2 new).

## Residual data-quality debt (flagged, not silently absorbed)

The closure is fixture-reproducible and regression-ready. It is not a
court-ready citation package until the placeholder CBIC references below are
replaced by verified per-clause official citations.

1. **L54 mis-routed event.** `llm_svc_events.jsonl` L54 is a `RATE_SUBSTITUTE_COLUMN` with `sno=3, item=ii` carrying "Service of exploration, mining…" text that belongs to sno=24, not sno=3. The sno=3 wholesale gap-fill (effective 2024-04-01) overwrites the corrupted text for the 2024/2025 checkpoints, but L54 itself is still wrong and affects any date before the gap-fill. Should be corrected at the compiler/source-backfill layer.
2. **L143 truncated source event.** The original 20/2019-CT(Rate) sno=7 restructure event remains truncated at 345 chars in the ledger. The gap-fill event supersedes it for 2024/2025. The source event should be re-extracted with complete text from the CBIC notification.
3. **Placeholder provenance on gap-fill events (3 events total).** `evt_rate_da06f8e602c2b7d9` (sno=3 desc), `evt_rate_3a033ad6f81cd8ed` (sno=3 rate), `evt_rate_3c17bc98bfeb320f` (sno=7 desc) all carry `source_cbic_no = "CONSOLIDATED GAP-FILL … PENDING precise per-clause CBIC citation"`. These must be replaced with verified per-clause CBIC citations before any court-ready use. Flagged via `review_reasons: [pending_official_citation, …]`.

## Verification commands
```
python3 -m pytest tests/ -q                                          # 373 passed
python3 -m pytest tests/test_service_checkpoint_closure.py -v        # 2 passed
python3 scripts/regenerate_svc_reconciliation_report.py              # 40/40, 41/41, 41/41
python3 scripts/regenerate_svc_reconciliation_report.py --prefer-fixture  # clean-checkout fixture path
```

## Verification boundary

This record is a service checkpoint closure record, not a claim that the entire
repository `make verify` gate is green. Terminal review found
`python3 main.py pipeline verify --derived-dir /tmp/gfl_pipeline_verify --manifest /tmp/gfl_pipeline_verify/latest.json`
failing on broad source/corpus integrity issues unrelated to this service
closure diff: source archives with no structural nodes, canonical-id path
mismatches, and XML source-span hash mismatches. Treat those as separate
corpus/source integrity debt.
