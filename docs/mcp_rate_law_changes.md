# Rate-Change & Law-Change MCP Tools — Design

**Date:** 2026-08-03
**Status:** Design + implementation plan for additive MCP tools on the Nyaya Ledger
corpus server (`scripts/serve_mcp.py` → `src/legal_corpus/serving.py`).

> **Note on architecture mapping.** This project is Nyaya Ledger, not the
> "gst_agent + SQLite snapshot" skeleton described in some task briefs. There is
> no `corpus.sqlite3` projections store, `temporal_determinations` table, or
> `StatelessGateway` here. The canonical inputs these tools read are the
> deterministic, event-sourced artifacts under `derived/version_history/`
> (rules/act/forms) and `derived/version_history/rate-schedules/` (goods +
> service + cess rate schedules), plus the `corpus/` Akoma Ntoso XML. The tool
> *semantics* below are exactly those requested; the *storage* is the real one.
> The "snapshot" concept maps to a version-history data root + a content marker;
> see §3.

## 1. Goals

Add two deterministic, read-only tool families to the MCP server:

- **Rate-change tools** — answer "what GST rate applied to HSN X on date D, and
  how did it change?" from the materialized rate schedules.
- **Law-change tools** — answer "what did provision P say on date D, and what
  amended it?" from the event-sourced version history, with a hard separation
  between **reviewed** amendment determinations and **unreviewed** candidate
  events.

These tools make no model calls. They are pure SQLite/JSONL/XML retrieval.

## 2. Hard constraints (all tools)

1. **Read-only.** No writes, no model calls, no evidence approval.
2. **Temporal correctness.** A rate/law version returned for date `D` must
   satisfy `effective_from <= D` and (`effective_to` is null or `effective_to > D`).
   If no version covers `D`, return `unresolved` — **never** silently fall back
   to the current version.
3. **Unreviewed separation.** Unreviewed amendment candidates never appear as
   verified amendments. With `include_unreviewed=true` they go in a separate
   `unreviewed_candidates[]` array carrying a warning. Only reviewed
   determinations appear in the main result. (See §6 for how review state is
   read.)
4. **HSN normalization.** Strip spaces; accept 4/6/8-digit forms; match on
   prefix when the query is shorter than the stored tariff item; preserve
   leading zeros.
5. **Envelope.** Every tool returns:
   ```json
   {
     "result": "...|unresolved|error",
     "snapshot_id": "<string>",
     "retrieved_at": "<ISO8601>",
     "coverage_warning": "<string|null>",
     "unresolved_gaps": ["..."],
     "source_refs": [{"document_id": "...", "artifact_sha256": "...", "locator": "..."}],
     "...tool-specific fields..."
   }
   ```
6. **Additive only.** No existing MCP tool signature changes (per `AGENTS.md`).
   New tools are registered with additional `@server.tool()` blocks.
7. **Provenance.** Rate results cite the notification that set the rate; law
   results carry `version_id`, `text_sha256`, `event_chain`, `source_basis`.

## 3. Snapshot model (mapped)

There is no SQLite snapshot directory. The "snapshot" these tools read is:

- `RATE_DATA_ROOT = derived/version_history/rate-schedules/`
  - `base_<notification>.json` (one per rate instrument: `1-2017`, `2-2017`,
    `11-2017-ct-rate`, cess variants, …)
  - `rate_amendment_events.jsonl`, `cess_amendment_events.jsonl` (event ledgers)
- `LAW_VERSION_ROOT` = per-work dirs: `derived/version_history/cgst-rules-2017/`,
  `derived/version_history/act-cgst-2017/`, `derived/version_history/forms/`
  - `node_versions.jsonl` (materialized versions; the "determinations")
  - `amendment_events_reviewed.jsonl` (reviewed amendment events)
  - `llm_candidates.jsonl` (unreviewed candidate events)

`snapshot_id` is a deterministic string: `nyaya-vh-<short sha of RATE_DATA_ROOT
manifest + LAW node_versions mtime bucket>`. For tests it is overridable. This
gives callers a stable handle for "the data I queried" without inventing a
projections store.

## 4. Rate-change tools

Backed by `src/legal_corpus/rate_schedule_materializer.py`:
`materialize_schedule(base_json_path, events_jsonl_path, target_notification,
checkpoint_date=...)` → `{schedules: {sid: {rate_pct, entries:[{sno,
tariff_item, description, is_omitted, rate, rate_pct}]}}, applied_events,
checkpoint_date, ...}`. Goods schedules (`1-2017`, `2-2017`, cess) carry the
HSN in `tariff_item`; the service schedule (`11-2017-ct-rate`) is keyed by S.No
+ Heading (9954 …) and is queried via `get_rate_conditions` / by S.No, not HSN.

### 4.1 `get_rate_for_hsn(hsn_code, as_of_date, jurisdiction?)`
- **Returns:** matched entries across goods + cess schedules with CGST/SGST/IGST
  breakdown (derived from schedule id: `1-2017` CGST, `2-2017` IGST, cess), the
  rate, the entry description (conditions), `is_omitted`, and the notification
  id + effective date that set the current rate (last `RATE_SUBSTITUTE_COLUMN`
  /`RATE_SET` event touching that sno on or before `as_of_date`).
- **Temporal rule:** materialize at `as_of_date`; if no non-omitted entry covers
  the HSN on that date → `result=unresolved`, `unresolved_gaps` explains.
- **HSN normalization:** strip spaces; prefix-match shorter codes against the
  stored `tariff_item`; never match an `is_omitted=true` entry as the live rate.
- **`jurisdiction`** optional filter (`cgst`/`igst`/`utgst`/`cess`) selects
  schedule families.

### 4.2 `trace_rate_changes(hsn_code, from_date?, to_date?)`
- **Returns:** chronological change list for the HSN: each `{effective_date,
  old_rate, new_rate, amending_notification, operation, retrospective_flag,
  conditions_added_removed}`. Computed by materializing at each distinct event
  effective-date ≤ `to_date` (and ≥ `from_date`) and diffing the entry for that
  HSN.
- **Handles:** `is_omitted` (rate removed), cess vs general, supersession
  (the materializer's `SUPERSESSION_MAP` is honored).

### 4.3 `get_rate_conditions(rate_entry_id_or_hsn_plus_date)`
- **Accepts:** `"11/2017-ct-rate::sno=3"`, `"1/2017::hsn=0101@2024-01-01"`, or a
  raw entry id.
- **Returns:** the entry description (the substantive conditions), plus
  inherited schedule `opening_paragraph`, `explanations`, and any `Provided
  that`/exemption provisos, each with source span where present.

### 4.4 `compare_rates(hsn_codes[], as_of_date)`
- Batch `get_rate_for_hsn`; returns side-by-side entries for each HSN.

## 5. Law-change tools

Backed by `src/legal_corpus/version_reconstruct.py:reconstruct_component` and
`src/legal_corpus/version_compare.py:compare_component_versions`, plus the event
ledgers. Existing tools `query_law_as_of_date`, `get_provision_as_of_date`,
`compare_versions`, `list_amendments`, `get_provision_timeline` already cover
the happy path; the new tools are **additive** and add: explicit unresolved
handling, the reviewed/unreviewed split, amendment-instrument lookup, and the
commencement chain.

### 5.1 `get_law_as_of(citation, as_of_date)`
- Resolves citation → component_id (via the existing resolver), calls
  `reconstruct_component(..., date=as_of_date)`. If `status=not_found` →
  envelope `result=unresolved` with `unresolved_gaps=["no version covers date"]`.
- **Returns:** text, `version_id`, `text_sha256`, `applicability_start/end`,
  `event_chain`, `source_basis`.

### 5.2 `trace_amendments(citation, include_unreviewed?)`
- **Reviewed (main result):** from `node_versions.jsonl` rows for the component
  where `created_by_event_id` is present, ordered by applicability date — same
  source as `list_amendments`, enriched with operation type, old→new via
  adjacent-version diff, effective date, retrospective flag (from the event's
  `legal_time.retrospective`).
- **Unreviewed (separate):** only if `include_unreviewed=true`, scan
  `llm_candidates.jsonl` (and any events whose `review.status` is not
  `accepted`/`validated`) for the component; placed in
  `unreviewed_candidates[]` with `coverage_warning`. Never merged into the main
  list.

### 5.3 `get_amendment_instrument(amendment_id_or_citation_plus_date)`
- Resolves the source notification/act for an amendment event
  (`source_basis.source_document_id`) → `lookup_provision` of that instrument.
- **Returns:** instrument document_id, title, text, effective/commencement date,
  any saving/transition clauses found in its text.

### 5.4 `get_commencement_chain(citation, amendment_date)`
- **Hardest tool.** Reads the event's `legal_time` (enactment) vs
  `system_time`/`effective_date` (commencement) and the instrument's
  "come into force" clause.
- **Returns:** `{enactment_date, commencement_date, retrospective_operation,
  saving_clauses[], transition_provisions[]}`. Where commencement is absent,
  marks it `commencement_unspecified` and lists the gap.

### 5.5 `compare_law_versions(citation, version_a_date, version_b_date)`
- Wraps `compare_component_versions`; returns both versions' text, a unified
  diff, and the amendment event responsible for the change between them.

## 6. Review-state model (how unreviewed is determined)

Each amendment event carries `review`, `status`, and `validation` fields
(inspected in `amendment_events_reviewed.jsonl`). A row is **reviewed** when
`review.status` ∈ {`accepted`, `validated`} or it is present in the
materialized `node_versions.jsonl` (which is the product of the reviewed
ledger). A row is **unreviewed** when it lives in `llm_candidates.jsonl` or its
review state is `proposed`/`needs_review`/absent. The tools enforce:

- default (`include_unreviewed=false`): only reviewed rows; if none, state it.
- `include_unreviewed=true`: reviewed rows in `amendments[]`, unreviewed in
  `unreviewed_candidates[]` + warning. The two lists never overlap.

## 7. Implementation

- New module `src/legal_corpus/rate_law_mcp.py` exposing a `RateLawService`
  class with one method per tool, returning the §2 envelope. Paths are
  injectable for tests. It calls `materialize_schedule`, `reconstruct_component`,
  `compare_component_versions`, and reads the event ledgers directly.
- `scripts/serve_mcp.py` registers nine additive `@server.tool()` entries
  delegating to a module-level getter (mirroring the existing `service()`
  pattern). No existing tool is touched.
- `HSN` normalization lives in `rate_law_mcp._normalize_hsn`.

## 8. Tests — `tests/test_rate_law_mcp.py`

≥3 per tool: (a) happy path against real fixtures/derived data, (b) missing data
→ `result=unresolved`, (c) temporal edge (date before first version / between
versions / after supersession). Rate tests use
`tests/fixtures/service_checkpoint_closure/rate-schedules/` + the goods base
JSONs; law tests use the CGST rules version dir. Tests inject paths and assert
the envelope fields.
