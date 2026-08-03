# Architecture & MCP Tool Surface

**Date:** 2026-08-03

This document is the concise architecture reference for operators and tool
authors. Deep design notes for individual subsystems live in dedicated docs:

- Rate / law-change MCP tools → `docs/mcp_rate_law_changes.md`
- Source / corpus integrity debt → `docs/source_integrity_debt.md`
- GST rules reconciliation status → `docs/reconciliation_status.md`

## Trust boundaries

1. **`corpus/` is the source of truth** — Akoma Ntoso XML, git-versioned,
   cryptographically provenanced (`sourceHash` per element). Never modified by
   derived-artifact rebuilds or serving code.
2. **`derived/` is rebuildable** — version history, rate schedules, graph,
   search index, vector chunks. Gitignored; regenerated from `corpus/` + source
   archives by deterministic pipelines.
3. **The LLM is untrusted and stateless.** MCP tools are deterministic,
   read-only retrieval. No tool calls a model server or mutates state.
4. **Time-travel provenance is mandatory.** Any historical answer carries
   `version_id`, `text_sha256`, `event_chain`, and `source_basis`.

## MCP server

Entry point: `scripts/serve_mcp.py` (`create_mcp_server()`), built on FastMCP.
Two backing services:

- `NyayaToolService` (`src/legal_corpus/serving.py`) — corpus lookup, graph,
  semantic search, and the original version-history tools.
- `RateLawService` (`src/legal_corpus/rate_law_mcp.py`) — the rate-change and
  law-change tools added 2026-08-03.

Both are lazily-built singletons (module-level `service()` / `rate_law_service()`)
so caches persist across calls in a long-running server process (per the
latency work in commit `b8b6534`).

## Tool surface (25 tools)

### Corpus & graph (NyayaToolService)
`lookup_provision`, `semantic_search`, `provision_search`, `resolve_citation`,
`get_incoming_refs`, `get_outgoing_refs`, `trace_rule_to_act`,
`find_related_provisions`, `explain_reference_path`, `get_forms_for_rule`,
`get_form_structure`.

### Version history — original (NyayaToolService)
`compare_versions`, `get_provision_as_of_date`, `list_amendments`,
`get_provision_timeline`, `query_law_as_of_date`.

### Rate-change tools (RateLawService) — NEW
| Tool | Reads | Returns |
|---|---|---|
| `get_rate_for_hsn(hsn, date, jurisdiction?)` | rate schedules | rate entry + CGST/SGST/IGST/cess breakdown + conditions + setting notification |
| `trace_rate_changes(hsn, from_date?, to_date?)` | rate events | chronological rate-change list |
| `get_rate_conditions(locator, as_of_date?)` | rate schedules | conditions, provisos, explanations, exemptions |
| `compare_rates(hsn_codes[], date)` | rate schedules | side-by-side comparison |

### Law-change tools (RateLawService) — NEW
| Tool | Reads | Returns |
|---|---|---|
| `get_law_as_of(citation, date)` | version history | text + full provenance at date |
| `trace_amendments(citation, include_unreviewed?)` | node_versions + candidates | reviewed amendments; unreviewed kept separate |
| `get_amendment_instrument(citation@date)` | version history + corpus | the amending Finance Act / Notification / Circular |
| `get_commencement_chain(citation, date)` | version history | enactment + commencement + saving/transition |
| `compare_law_versions(citation, dateA, dateB)` | version history | both texts + unified diff + events between |

## Response envelope (rate/law-change tools)

Every `RateLawService` method returns:

```json
{
  "result": "ok | unresolved | error",
  "snapshot_id": "nyaya-vh-<12-hex>",
  "retrieved_at": "<ISO8601 UTC>",
  "coverage_warning": "string | null",
  "unresolved_gaps": ["..."],
  "source_refs": [{"document_id", "artifact_sha256", "locator"}],
  "...tool-specific fields..."
}
```

Invariants enforced everywhere:

- **Temporal correctness.** A version for date `D` satisfies
  `effective_from <= D` and (`effective_to` is null or `> D`); else
  `result=unresolved` — never a silent fallback to the current version.
- **Unreviewed separation.** Unreviewed amendment candidates never appear in the
  verified `amendments[]` list; with `include_unreviewed=true` they go in a
  separate `unreviewed_candidates[]` array with a `coverage_warning`.
- **HSN normalization.** Spaces/leading zeros preserved; 4/6/8-digit forms;
  prefix matching; `Chapter NN` handled; junk tokens (e.g. `00]`) rejected.

## Caching & invalidation

`NyayaToolService` and the version readers cache immutable derived artifacts
in-memory (`_lookup`, `_json_cache`, `node_versions` cache). After regenerating
`derived/`, restart the MCP/API process — there is no TTL or file-watch
invalidation. The structured-citation fast path (`b8b6534`) keeps composed
queries ~80 ms cold / ~13 ms warm.

## Verification

- Full gate: `make verify` (test + compile + pipeline + diff-check). Note:
  `pipeline verify` currently fails on pre-existing source/corpus integrity debt
  documented in `docs/source_integrity_debt.md`; it is independent of the
  MCP/version-history surface.
- Rate/law-change tools: `python3 -m pytest tests/test_rate_law_mcp.py -q`
  (32 tests).
- MCP output invariance: `python3 -m pytest tests/test_mcp_output_invariance.py`.
