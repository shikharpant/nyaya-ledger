"""Write a static HTML review board for version-history artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from collections import Counter


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl_sample(path: Path, limit: int = 40) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row.get("text"), str) and len(row["text"]) > 900:
            row["text"] = row["text"][:900] + "..."
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _short(value: Any, limit: int = 700) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


def _compact_review(review: dict[str, Any]) -> dict[str, Any]:
    compact = dict(review)
    rows = []
    for row in review.get("non_validated_events") or []:
        next_row = dict(row)
        next_row["excerpt"] = _short(next_row.get("excerpt"), 700)
        rows.append(next_row)
    compact["non_validated_events"] = rows
    return compact


def _review_from_events(events_path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    events = _read_jsonl(events_path)
    if not events:
        return fallback
    status_counts = Counter(str(event.get("status") or "") for event in events)
    reason_counts = Counter(
        reason
        for event in events
        for reason in (event.get("review") or {}).get("review_reasons", [])
    )
    operation_counts = Counter(str(event.get("operation") or "") for event in events)
    year_counts = Counter(str((event.get("source") or {}).get("publication_date") or "")[:4] for event in events)
    target_counts = Counter(str((event.get("target") or {}).get("work_id") or "") for event in events)
    return {
        "event_count": len(events),
        "counts": {
            "by_status": dict(status_counts),
            "by_review_reason": dict(reason_counts),
            "by_operation": dict(operation_counts),
            "by_year": dict(year_counts),
            "by_target": dict(target_counts),
        },
        "non_validated_events": [
            {
                "event_id": event.get("event_id"),
                "operation": event.get("operation"),
                "target": event.get("target", {}),
                "source_document_id": (event.get("source") or {}).get("document_id"),
                "record_id": (event.get("source") or {}).get("record_id"),
                "source_span": (event.get("evidence") or {}).get("source_span", {}),
                "excerpt": (event.get("evidence") or {}).get("excerpt", ""),
                "parser_pattern": (event.get("evidence") or {}).get("parser_trace", {}).get("pattern_id"),
                "review_reasons": (event.get("review") or {}).get("review_reasons", []),
            }
            for event in events
            if event.get("status") != "validated"
        ],
    }


def _compact_triage(triage: dict[str, Any]) -> dict[str, Any]:
    compact = dict(triage)
    compact["items"] = [
        {
            "event_id": row.get("event_id"),
            "triage_class": row.get("triage_class"),
            "recommended_action": row.get("recommended_action"),
            "confidence": row.get("confidence"),
            "basis": row.get("basis"),
            "rationale": _short(row.get("rationale"), 360),
        }
        for row in triage.get("items") or []
    ]
    compact["groups"] = [
        {
            **row,
            "sample_excerpt": _short(row.get("sample_excerpt"), 520),
            "event_ids": (row.get("event_ids") or [])[:20],
        }
        for row in triage.get("groups") or []
    ]
    return compact


def _payload_full_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    for key in ("structural_text", "text", "insert_text", "replacement_text", "new_text", "delete_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    values = [value.strip() for value in payload.values() if isinstance(value, str) and value.strip()]
    if values:
        return "\n\n".join(values)
    return str((event.get("evidence") or {}).get("excerpt") or "").strip()


def _event_details(events_path: Path, event_ids: set[str]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    if not event_ids:
        return details
    for event in _read_jsonl(events_path):
        event_id = str(event.get("event_id") or "")
        if event_id not in event_ids:
            continue
        evidence = event.get("evidence") or {}
        source = event.get("source") or {}
        target = event.get("target") or {}
        details[event_id] = {
            "event_id": event_id,
            "operation": event.get("operation"),
            "target_component_id": target.get("component_id"),
            "anchor_text": target.get("anchor_text"),
            "source_document_id": source.get("document_id"),
            "source_span": evidence.get("source_span"),
            "excerpt": evidence.get("excerpt"),
            "payload_text": _payload_full_text(event),
            "payload": event.get("payload") or {},
        }
    return details


def _compact_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    compact = dict(coverage)
    rows = []
    for row in coverage.get("gaps") or []:
        next_row = dict(row)
        next_row["excerpt"] = _short(next_row.get("excerpt"), 650)
        rows.append(next_row)
    compact["gaps"] = rows
    return compact


def _compact_completion(completion: dict[str, Any]) -> dict[str, Any]:
    compact = dict(completion)
    compact["items"] = [
        {
            **row,
            "excerpt": _short(row.get("excerpt"), 560),
            "reconciliation": (row.get("reconciliation") or [])[:5],
        }
        for row in completion.get("items") or []
    ]
    compact["open_items"] = [
        {
            **row,
            "excerpt": _short(row.get("excerpt"), 560),
            "reconciliation": (row.get("reconciliation") or [])[:5],
        }
        for row in completion.get("open_items") or []
    ]
    return compact


def _compact_portal_completeness(portal: dict[str, Any]) -> dict[str, Any]:
    compact = dict(portal)
    compact["missing_source_notifications"] = [
        {
            **row,
            "excerpt": _short(row.get("excerpt"), 520),
        }
        for row in portal.get("missing_source_notifications", [])
    ]
    rules = {}
    for rule, row in (portal.get("rules") or {}).items():
        rules[rule] = {
            "portal_completeness_status": row.get("portal_completeness_status"),
            "portal_notification_count": len(row.get("portal_notification_refs") or []),
            "event_notification_count": len(row.get("event_notification_refs") or []),
            "missing_source_notification_count": len(row.get("missing_source_notifications") or []),
        }
    compact["rules"] = rules
    return compact


def write_version_history_report(
    *,
    output: Path,
    review_report: Path = Path("derived/version_history/review_report.json"),
    rules_manifest: Path = Path("derived/version_history/cgst-rules-2017/materialization_manifest.json"),
    rules_coverage: Path = Path("derived/version_history/cgst-rules-2017/coverage_gaps.json"),
    reconciliation_report: Path = Path("derived/version_history/cgst-rules-2017/reconciliation_report.json"),
    forms_manifest: Path = Path("derived/version_history/forms/materialization_manifest.json"),
    forms_coverage: Path = Path("derived/version_history/forms/coverage_gaps.json"),
    review_triage: Path = Path("derived/version_history/review_triage.json"),
    review_decisions: Path = Path("derived/version_history/review_decisions.json"),
    auto_review_decisions: Path = Path("derived/version_history/auto_review_decisions.json"),
    dependency_review_decisions: Path = Path("derived/version_history/dependency_review_decisions.json"),
    codex_review_decisions: Path = Path("derived/version_history/codex_review_decisions.json"),
    review_completion: Path = Path("derived/version_history/review_completion_report.json"),
    amendment_events: Path = Path("derived/version_history/amendment_events_reviewed.jsonl"),
    node_versions: Path = Path("derived/version_history/cgst-rules-2017/node_versions.jsonl"),
    portal_completeness: Path = Path("derived/version_history/portal_completeness_report.json"),
    corrigendum_ledger: Path = Path("derived/version_history/cgst-rules-2017/corrigendum_ledger.jsonl"),
    act_confidence_tiers: Path = None,
    act_reconciliation_report: Path = None,
    act_manifest: Path = None,
) -> dict[str, Any]:
    review = _compact_review(_review_from_events(amendment_events, _load(review_report)))
    manifest = _load(rules_manifest)
    coverage = _compact_coverage(_load(rules_coverage))
    reconciliation = _load(reconciliation_report)
    forms = _load(forms_manifest)
    form_gaps = _compact_coverage(_load(forms_coverage))
    triage = _compact_triage(_load(review_triage))
    manual_decisions = _load(review_decisions)
    auto_decisions = _load(auto_review_decisions)
    dependency_decisions = _load(dependency_review_decisions)
    codex_decisions = _load(codex_review_decisions)
    completion = _compact_completion(_load(review_completion))
    portal = _compact_portal_completeness(_load(portal_completeness))
    corrigenda = _jsonl_sample(corrigendum_ledger, limit=80)
    act_conf = _load(act_confidence_tiers) if act_confidence_tiers else {}
    act_recon = _load(act_reconciliation_report) if act_reconciliation_report else {}
    act_manifest_data = _load(act_manifest) if act_manifest else {}
    data = {
        "review": review,
        "manifest": manifest,
        "coverage": coverage,
        "reconciliation": reconciliation,
        "forms": forms,
        "formGaps": form_gaps,
        "triage": triage,
        "completion": completion,
        "portalCompleteness": portal,
        "corrigendumLedger": corrigenda,
        "actSummary": {
            "tier_counts": act_conf.get("tier_counts", {}),
            "total_components": act_conf.get("total_components", 0),
            "applied_count": act_manifest_data.get("applied_count", 0),
            "coverage_gap_count": act_manifest_data.get("coverage_gap_count", 0),
            "recon_matched": act_recon.get("matched_count", 0),
            "recon_mismatched": act_recon.get("mismatched_count", 0),
            "recon_missing": act_recon.get("missing_count", 0),
            "recon_format_only": act_recon.get("format_only_mismatch_count", 0),
            "unresolved": act_recon.get("unresolved_reconciliation_count", 0),
        },
        "generatedDecisions": {
            "manual": manual_decisions,
            "auto": auto_decisions,
            "dependency": dependency_decisions,
            "codex": codex_decisions,
        },
        "eventDetails": {},
        "nodeVersionSample": _jsonl_sample(node_versions),
        "artifactPaths": {
            "review": str(review_report),
            "reviewTriage": str(review_triage),
            "reviewDecisions": str(review_decisions),
            "autoReviewDecisions": str(auto_review_decisions),
            "dependencyReviewDecisions": str(dependency_review_decisions),
            "codexReviewDecisions": str(codex_review_decisions),
            "reviewCompletion": str(review_completion),
            "amendmentEvents": str(amendment_events),
            "rulesManifest": str(rules_manifest),
            "rulesCoverage": str(rules_coverage),
            "reconciliation": str(reconciliation_report),
            "formsManifest": str(forms_manifest),
            "formsCoverage": str(forms_coverage),
            "nodeVersions": str(node_versions),
            "portalCompleteness": str(portal_completeness),
            "corrigendumLedger": str(corrigendum_ledger),
            "actConfidenceTiers": str(act_confidence_tiers) if act_confidence_tiers else "",
            "actReconciliationReport": str(act_reconciliation_report) if act_reconciliation_report else "",
            "actManifest": str(act_manifest) if act_manifest else "",
        },
    }
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    document = HTML_TEMPLATE.replace("__VERSION_HISTORY_DATA__", data_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return {"ok": True, "output": str(output)}


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CGST Version History Review Board</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
  <style>
    :root {
      --bg: #f7f8fb;
      --panel: #ffffff;
      --text: #172026;
      --muted: #5c6670;
      --border: #d8dee4;
      --accent: #1168a8;
      --ok: #137333;
      --warn: #9a6700;
      --bad: #b42318;
      --chip: #eef2f7;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }
    header { position: sticky; top: 0; z-index: 5; background: #fff; border-bottom: 1px solid var(--border); padding: 14px 24px; }
    header h1 { margin: 0; font-size: 22px; }
    header p { margin: 4px 0 0; color: var(--muted); }
    nav { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    nav a, button, select, input, textarea { font: inherit; }
    nav a, .btn { border: 1px solid var(--border); background: #fff; color: var(--text); border-radius: 6px; padding: 7px 10px; text-decoration: none; cursor: pointer; }
    .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    .btn.danger { color: var(--bad); }
    main { padding: 22px 24px 60px; }
    section { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin: 0 0 18px; }
    h2 { margin: 0 0 12px; font-size: 18px; }
    h3 { margin: 18px 0 8px; font-size: 15px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
    .metric { border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: #fbfcfe; }
    .metric .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .metric .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
    .metric .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0; }
    input, select, textarea { border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; background: #fff; min-height: 34px; }
    input[type="search"] { min-width: 280px; }
    textarea { width: 100%; min-height: 90px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-top: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; }
    th { background: #f6f8fa; position: sticky; top: 91px; z-index: 2; }
    tr:hover td { background: #fbfdff; }
    code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .chip { display: inline-block; padding: 2px 7px; background: var(--chip); border-radius: 999px; margin: 1px; white-space: nowrap; }
    .status-approved { color: var(--ok); font-weight: 700; }
    .status-rejected { color: var(--bad); font-weight: 700; }
    .status-needs_more_info { color: var(--warn); font-weight: 700; }
    .excerpt { max-width: 560px; line-height: 1.35; }
    .small { color: var(--muted); font-size: 12px; }
    .cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
    .diagram { background: #fff; border: 1px dashed var(--border); border-radius: 8px; padding: 10px; overflow-x: auto; }
    .pager { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
    .pager .btn:disabled { opacity: .45; cursor: not-allowed; }
    details { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: #fbfcfe; margin: 8px 0; }
    summary { cursor: pointer; font-weight: 650; }
    summary.btn { display: inline-block; width: max-content; margin-top: 6px; }
    .full-text { margin-top: 8px; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: #fff; white-space: pre-wrap; max-height: 520px; overflow: auto; line-height: 1.4; }
    .hidden { display: none; }
    @media (max-width: 900px) { .cols { grid-template-columns: 1fr; } th { position: static; } }
  </style>
</head>
<body>
  <header>
    <h1>CGST Version History Review Board</h1>
    <p>Event-sourced legal history review for CGST Rules, forms, reconciliation, and materialization coverage.</p>
    <nav>
      <a href="#overview">Overview</a>
      <a href="#triage">Triage</a>
      <a href="#completion">Completion</a>
      <a href="#review">Review Queue</a>
      <a href="#coverage">Coverage Gaps</a>
      <a href="#forms">Forms Lane</a>
      <a href="#act">CGST Act 2017</a>
      <a href="#reconciliation">Reconciliation</a>
      <a href="#versions">Versions</a>
      <a href="#diagrams">Diagrams</a>
      <a href="#decisions">Decisions</a>
    </nav>
  </header>
  <main>
    <section id="overview">
      <h2>Overview</h2>
      <div id="metrics" class="grid"></div>
      <details open>
        <summary>Artifact paths</summary>
        <div id="artifactPaths"></div>
      </details>
    </section>

    <section id="triage">
      <h2>LLM Review Triage</h2>
      <p class="small">Triage groups reduce the human queue by classifying needs_review events without changing the authoritative ledger.</p>
      <div id="triageMetrics" class="grid"></div>
      <div class="toolbar">
        <input id="triageSearch" type="search" placeholder="Search triage groups">
        <select id="triageClass"></select>
        <button class="btn" onclick="resetPage('triage'); renderTriage()">Apply</button>
      </div>
      <div id="triageTable"></div>
    </section>

    <section id="completion">
      <h2>Review Completion</h2>
      <p class="small">Terminal review states close the code-review queue without changing materializer coverage truth.</p>
      <div id="completionMetrics" class="grid"></div>
      <div class="toolbar">
        <input id="completionSearch" type="search" placeholder="Search terminal decisions">
        <select id="completionTerminal"></select>
        <select id="completionImpact"></select>
        <button class="btn" onclick="resetPage('completion'); renderCompletion()">Apply</button>
      </div>
      <div id="completionTable"></div>
    </section>

    <section id="review">
      <h2>Review Queue</h2>
      <p class="small">Every row can be marked locally. Decisions are stored in browser localStorage and can be exported as JSON. Auto-reject candidates are hidden by default after triage.</p>
      <div class="toolbar">
        <input id="reviewSearch" type="search" placeholder="Search event, target, source, excerpt, reason">
        <select id="reviewOperation"></select>
        <select id="reviewReason"></select>
        <select id="reviewTriageClass"></select>
        <select id="reviewDecision">
          <option value="">Any decision</option>
          <option value="unreviewed">Unreviewed</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
          <option value="needs_more_info">Needs more info</option>
        </select>
        <button class="btn" onclick="resetPage('review'); renderReview()">Apply</button>
      </div>
      <div id="reviewTable"></div>
    </section>

    <section id="coverage">
      <h2>Rules Coverage Gaps</h2>
      <div class="toolbar">
        <input id="gapSearch" type="search" placeholder="Search coverage gaps">
        <select id="gapReason"></select>
        <button class="btn" onclick="resetPage('gaps'); renderGaps()">Apply</button>
      </div>
      <div id="gapTable"></div>
    </section>

    <section id="forms">
      <h2>Forms Lane</h2>
      <div id="formsSummary" class="grid"></div>
      <div class="toolbar">
        <input id="formSearch" type="search" placeholder="Search form events">
        <button class="btn" onclick="resetPage('forms'); renderForms()">Apply</button>
      </div>
      <div id="formsTable"></div>
    </section>

    <section id="act">
      <h2>CGST Act 2017</h2>
      <p class="small">Confidence tiers, materialization, and reconciliation summary for the CGST Act 2017 lane.</p>
      <div id="actSummary" class="grid"></div>
    </section>

    <section id="reconciliation">
      <h2>Reconciliation Priority Queue</h2>
      <p class="small">Components with checkpoint mismatch/missing reconstruction, linked to coverage-gap event IDs where available.</p>
      <div class="toolbar">
        <input id="reconSearch" type="search" placeholder="Search component or event">
        <button class="btn" onclick="resetPage('reconciliation'); renderReconciliation()">Apply</button>
      </div>
      <div id="reconciliationTable"></div>
    </section>

    <section id="versions">
      <h2>Node Version Sample</h2>
      <p class="small">Sample from node_versions.jsonl for quick inspection. Use exported artifacts for the full ledger.</p>
      <div class="toolbar">
        <input id="versionSearch" type="search" placeholder="Search component/version text">
        <button class="btn" onclick="resetPage('versions'); renderVersions()">Apply</button>
      </div>
      <div id="versionsTable"></div>
    </section>

    <section id="diagrams">
      <h2>Codebase And Process Diagrams</h2>
      <div class="cols">
        <div>
          <h3>Module Architecture</h3>
          <div class="diagram"><pre class="mermaid">
flowchart LR
  Registry[identity_registry.py<br/>work IDs and aliases] --> Compiler[amendment_events.py<br/>deterministic + OMLX candidates]
  OMLX[omlx_client.py<br/>Qwen no_think JSON] --> Compiler
  Baselines[baselines.py<br/>2017 Rules and Act baselines] --> Materializer[version_snapshots.py<br/>component versions]
  Compiler --> Ledger[amendment_events.jsonl]
  Ledger --> Materializer
  Materializer --> NodeVersions[node_versions.jsonl]
  Materializer --> Gaps[coverage_gaps.json]
  NodeVersions --> Compare[version_compare.py]
  Gaps --> Compare
  NodeVersions --> Reconcile[reconciliation.py]
  Reconcile --> ReconReport[reconciliation_report.json]
  Ledger --> Forms[form_version_snapshots.py]
  Forms --> FormsManifest[forms materialization manifest]
  ReconReport --> Report[version_history_report.py]
  Gaps --> Report
  FormsManifest --> Report
          </pre></div>
        </div>
        <div>
          <h3>Legal History Pipeline</h3>
          <div class="diagram"><pre class="mermaid">
sequenceDiagram
  participant S as CBIC source archive
  participant C as Event compiler
  participant L as OMLX/Qwen
  participant V as Validation gate
  participant M as Materializer
  participant R as Reconciliation
  participant B as Review board
  S->>C: notification JSON/PDF/source text
  C->>C: deterministic patterns
  C->>L: unresolved excerpt JSON prompt
  L-->>C: candidate operation/target/payload
  C->>V: verify date, target, anchor, span, payload
  V-->>C: validated or needs_review
  C->>M: validated materializable events
  M-->>M: update component versions
  M-->>R: reconstructed text at checkpoint
  R-->>B: priority mismatch queue
  C-->>B: non-validated events
  M-->>B: coverage gaps and manifests
          </pre></div>
        </div>
      </div>
      <div class="cols">
        <div>
          <h3>Review Decision Lifecycle</h3>
          <div class="diagram"><pre class="mermaid">
stateDiagram-v2
  [*] --> Unreviewed
  Unreviewed --> Approved: reviewer accepts event/payload
  Unreviewed --> Rejected: non-amendment or unsafe
  Unreviewed --> NeedsMoreInfo: requires parser/source fix
  NeedsMoreInfo --> Approved: fixed and verified
  NeedsMoreInfo --> Rejected: cannot support safely
  Approved --> Exported
  Rejected --> Exported
  Exported --> [*]
          </pre></div>
        </div>
        <div>
          <h3>Materialization Gate</h3>
          <div class="diagram"><pre class="mermaid">
flowchart TD
  Event[Candidate event] --> Target{Target resolved?}
  Target -- no --> Review[needs_review + coverage gap]
  Target -- yes --> Date{Legal date resolved?}
  Date -- no --> Review
  Date -- yes --> Span{Source span verified?}
  Span -- no --> Review
  Span -- yes --> Payload{Operation payload safe?}
  Payload -- no --> Review
  Payload -- yes --> Anchor{Anchor required and unique?}
  Anchor -- no --> Review
  Anchor -- yes --> Conflict{Same date conflict?}
  Conflict -- yes --> Review
  Conflict -- no --> Apply[materialized component version]
          </pre></div>
        </div>
      </div>
    </section>

    <section id="decisions">
      <h2>Reviewer Decisions</h2>
      <div id="generatedDecisionSummary" class="grid"></div>
      <div id="codexDecisionTable"></div>
      <div id="dependencyDecisionTable"></div>
      <div class="toolbar">
        <button class="btn primary" onclick="exportDecisions()">Export decisions JSON</button>
        <button class="btn" onclick="document.getElementById('decisionFile').click()">Import decisions JSON</button>
        <input id="decisionFile" type="file" accept="application/json" class="hidden" onchange="importDecisions(event)">
        <button class="btn danger" onclick="clearDecisions()">Clear local decisions</button>
      </div>
      <textarea id="decisionOutput" readonly placeholder="Exported decisions appear here."></textarea>
      <div id="decisionSummary"></div>
    </section>
  </main>
  <script>
    window.VERSION_HISTORY_DATA = __VERSION_HISTORY_DATA__;
  </script>
  <script>
    const DATA = window.VERSION_HISTORY_DATA;
    const STORAGE_KEY = "cgst-version-history-review-decisions-v1";
    const PAGE_SIZE = 10;
    const pages = { triage: 1, completion: 1, review: 1, gaps: 1, forms: 1, reconciliation: 1, versions: 1 };
    const TRIAGE_BY_EVENT = Object.fromEntries((DATA.triage.items || []).map(row => [row.event_id, row]));
    let EVENT_DETAILS = DATA.eventDetails || {};
    let EVENT_DETAILS_LOADED = Object.keys(EVENT_DETAILS).length > 0;
    let fullTextCounter = 0;
    let decisions = loadDecisions();

    function loadDecisions() {
      try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); }
      catch (_err) { return {}; }
    }
    function saveDecisions() {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(decisions));
      renderDecisionSummary();
    }
    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    function chips(values) {
      return (values || []).map(v => `<span class="chip">${esc(v)}</span>`).join(" ");
    }
    function payloadTextFromEvent(event) {
      const payload = event.payload || {};
      for (const key of ["structural_text", "text", "insert_text", "replacement_text", "new_text", "delete_text"]) {
        if (typeof payload[key] === "string" && payload[key].trim()) return payload[key].trim();
      }
      const values = Object.values(payload).filter(v => typeof v === "string" && v.trim()).map(v => v.trim());
      if (values.length) return values.join("\n\n");
      return (event.evidence || {}).excerpt || "";
    }
    async function ensureEventDetails() {
      if (EVENT_DETAILS_LOADED) return EVENT_DETAILS;
      const source = (DATA.artifactPaths.amendmentEvents || "amendment_events.jsonl").split("/").pop();
      const response = await fetch(source);
      if (!response.ok) throw new Error(`Unable to load ${source}: ${response.status}`);
      const text = await response.text();
      const details = {};
      for (const line of text.split(/\n+/)) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        const id = String(event.event_id || "");
        if (!id) continue;
        const evidence = event.evidence || {};
        const sourceObj = event.source || {};
        const target = event.target || {};
        details[id] = {
          event_id: id,
          operation: event.operation,
          target_component_id: target.component_id,
          source_document_id: sourceObj.document_id,
          source_span: evidence.source_span || {},
          excerpt: evidence.excerpt || "",
          payload_text: payloadTextFromEvent(event)
        };
      }
      EVENT_DETAILS = details;
      EVENT_DETAILS_LOADED = true;
      return EVENT_DETAILS;
    }
    async function loadFullTextBlock(id, encodedIds) {
      const target = document.getElementById(id);
      if (!target || target.dataset.loaded === "true") return;
      target.innerHTML = `<div class="small">Loading full text...</div>`;
      try {
        const details = await ensureEventDetails();
        const ids = encodedIds.split("|").filter(Boolean);
        const blocks = ids.map(eventId => details[eventId]).filter(Boolean);
        target.innerHTML = blocks.length ? blocks.map(detail => {
          const full = detail.payload_text || detail.excerpt || "";
          return `<div class="full-text"><div class="mono">${esc(detail.event_id)} · ${esc(detail.operation)} · ${esc(detail.target_component_id)}</div>
<div class="small">Source: ${esc(detail.source_document_id)} · span ${esc(detail.source_span?.start)}-${esc(detail.source_span?.end)}</div>
${esc(full)}</div>`;
        }).join("") : `<div class="small">No full text found for these event IDs.</div>`;
        target.dataset.loaded = "true";
      } catch (err) {
        target.innerHTML = `<div class="small">Could not load full text: ${esc(err.message || err)}</div>`;
      }
    }
    function fullTextBlock(eventIds) {
      const ids = (eventIds || []).filter(Boolean);
      if (!ids.length) return "";
      const id = `fulltext-${++fullTextCounter}`;
      return `<details ontoggle="if (this.open) loadFullTextBlock('${id}', '${esc(ids.join("|"))}')"><summary class="btn">Expand full text</summary><div id="${id}" class="small">Open to load full source text.</div></details>`;
    }
    function decisionFor(id) {
      return decisions[id] || { decision: "unreviewed", note: "" };
    }
    function setDecision(id, decision) {
      decisions[id] = { ...(decisions[id] || {}), decision, updated_at: new Date().toISOString() };
      saveDecisions();
      renderAllTables();
    }
    function setNote(id, note) {
      decisions[id] = { ...(decisions[id] || { decision: "unreviewed" }), note, updated_at: new Date().toISOString() };
      saveDecisions();
    }
    function decisionControls(id) {
      const d = decisionFor(id);
      return `<div>
        <select onchange="setDecision('${esc(id)}', this.value)">
          ${["unreviewed","approved","rejected","needs_more_info"].map(v => `<option value="${v}" ${d.decision === v ? "selected" : ""}>${v.replaceAll("_"," ")}</option>`).join("")}
        </select>
        <div class="status-${esc(d.decision)}">${esc(d.decision.replaceAll("_"," "))}</div>
        <input placeholder="review note" value="${esc(d.note || "")}" onchange="setNote('${esc(id)}', this.value)">
      </div>`;
    }
    function includes(row, needle) {
      return JSON.stringify(row).toLowerCase().includes(needle.toLowerCase());
    }
    function pageRows(name, rows) {
      const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
      pages[name] = Math.min(Math.max(1, pages[name] || 1), totalPages);
      const start = (pages[name] - 1) * PAGE_SIZE;
      return { rows: rows.slice(start, start + PAGE_SIZE), totalPages, start };
    }
    function setPage(name, page) {
      pages[name] = Math.max(1, Number(page) || 1);
      ({ triage: renderTriage, completion: renderCompletion, review: renderReview, gaps: renderGaps, forms: renderForms, reconciliation: renderReconciliation, versions: renderVersions }[name])();
    }
    function resetPage(name) {
      pages[name] = 1;
    }
    function renderMetrics() {
      const counts = DATA.review.counts || {};
      const status = counts.by_status || {};
      const completion = DATA.completion || {};
      const portal = DATA.portalCompleteness || {};
      const metrics = [
        ["Ledger events", DATA.review.event_count, "compiled amendment-event records"],
        ["Validated", status.validated || 0, "passed deterministic gates"],
        ["Needs review", status.needs_review || 0, "must not be silently applied"],
        ["Review closed", completion.closed_count || 0, "pending items with terminal code-review states"],
        ["Legal review open", completion.open_count || 0, "items still needing legal/parser intervention"],
        ["Rules applied", DATA.manifest.applied_count, "materialized component edits"],
        ["Rules gaps", DATA.manifest.coverage_gap_count, "skipped or failed materialization"],
        ["Forms routed", DATA.manifest.forms_lane_routed_count || 0, "events moved out of Rules text coverage"],
        ["Rules conflicts", DATA.manifest.conflict_count, "same-date component conflicts"],
        ["Blocked baseline", DATA.manifest.blocked_baseline_component_count || 0, "baseline components flagged as unsafe starting text"],
        ["Corrigenda", DATA.manifest.corrigendum_ledger_count || (DATA.corrigendumLedger || []).length || 0, "corrigendum chronology ledger rows"],
        ["Portal missing", portal.missing_source_notification_count || 0, "portal-listed notifications absent from event sources"],
        ["Portal external", portal.external_reference_notification_count || 0, "rate/customs references excluded from Rules source gaps"],
        ["Recon priority", DATA.reconciliation.priority_review_count, "checkpoint-driven queue"],
        ["Recon unresolved", DATA.reconciliation.unresolved_reconciliation_count || 0, "actionable checkpoint outcomes"],
        ["Commencement blocked", DATA.reconciliation.commencement_blocked_count || 0, "checkpoint-present but no notified date found"],
        ["Recon strict drift", DATA.reconciliation.strict_mismatch_count ?? DATA.reconciliation.mismatched_count, "strict text/hash differences"],
        ["Recon format-only", DATA.reconciliation.format_only_mismatch_count || 0, "annotation or parser-shape drift"],
        ["Recon substantive", DATA.reconciliation.substantive_mismatch_count ?? DATA.reconciliation.mismatched_count, "text differences still requiring work"],
        ["Forms events", DATA.forms.event_count, "separate forms lane"],
        ["RFD-01 statements", DATA.forms.statement_applied_count || 0, "statement-level form amendments materialized"],
        ["Baseline forms", DATA.forms.baseline_form_count, "forms with corpus baseline"],
        ["Triage groups", DATA.triage.group_count || 0, "collapsed human-review buckets"]
      ];
      document.getElementById("metrics").innerHTML = metrics.map(([label, value, sub]) =>
        `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value ?? "")}</div><div class="sub">${esc(sub)}</div></div>`
      ).join("");
      document.getElementById("artifactPaths").innerHTML = `<table><tbody>${Object.entries(DATA.artifactPaths).map(([k,v]) => `<tr><th>${esc(k)}</th><td class="mono">${esc(v)}</td></tr>`).join("")}</tbody></table>`;
    }
    function renderGeneratedDecisions() {
      const generated = DATA.generatedDecisions || {};
      const manual = generated.manual || {};
      const auto = generated.auto || {};
      const dependency = generated.dependency || {};
      const codex = generated.codex || {};
      const cards = [
        ["Manual approvals", (manual.decisions || []).length, "curated review_decisions.json"],
        ["Auto approvals", auto.decision_count || (auto.decisions || []).length || 0, "mechanically verifiable parser-support cases"],
        ["Dependency approvals", dependency.decision_count || (dependency.decisions || []).length || 0, "upstream insertions that unlock missing anchors"],
        ["Approved by Codex", codex.decision_count || (codex.decisions || []).length || 0, "exact text edits approved by Codex judgment"],
        ["Unresolved anchors", dependency.unresolved_count || (dependency.unresolved || []).length || 0, "still need source coverage or manual investigation"]
      ];
      document.getElementById("generatedDecisionSummary").innerHTML = cards.map(([label, value, sub]) =>
        `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`
      ).join("");
      const codexRows = codex.decisions || [];
      document.getElementById("codexDecisionTable").innerHTML = codexRows.length ? `<h3>Approved by Codex</h3><p class="small">These rows were approved by Codex after deterministic source/component checks. They are applied by the promoted ledger but kept here for later audit.</p><table><thead><tr><th>Event</th><th>Strategy</th><th>Target</th><th>Source</th><th>Notes</th><th>Full text</th></tr></thead><tbody>${codexRows.map(row => {
        const basis = row.source_basis || {};
        const promote = row.promote || {};
        const target = basis.target || {};
        return `<tr><td class="mono">${esc(row.event_id)}</td><td>${esc(promote.strategy)}</td><td class="mono">${esc(target.component_id)}</td><td><span class="mono">${esc(basis.source_document_id)}</span><div class="small">span ${esc(basis.source_span?.start)}-${esc(basis.source_span?.end)}</div></td><td>${esc(row.notes)}</td><td>${fullTextBlock([row.event_id])}</td></tr>`;
      }).join("")}</tbody></table>` : `<h3>Approved by Codex</h3><p class="small">No Codex-approved decisions have been generated yet.</p>`;
      const unresolved = dependency.unresolved || [];
      document.getElementById("dependencyDecisionTable").innerHTML = unresolved.length ? `<h3>Unresolved dependency anchors</h3><table><thead><tr><th>Missing component</th><th>Dependent events</th><th>Candidate events</th><th>Reason</th></tr></thead><tbody>${unresolved.map(row =>
        `<tr><td class="mono">${esc(row.missing_component_id)}</td><td>${(row.dependent_event_ids || []).map(id => `<span class="chip mono">${esc(id)}</span>`).join(" ")}</td><td>${(row.candidate_event_ids || []).map(id => `<span class="chip mono">${esc(id)}</span>`).join(" ") || "<span class='small'>none</span>"}</td><td>${esc(row.reason)}</td></tr>`
      ).join("")}</tbody></table>` : "";
    }
    function populateFilters() {
      const reviewRows = DATA.review.non_validated_events || [];
      const ops = [...new Set(reviewRows.map(r => r.operation).filter(Boolean))].sort();
      document.getElementById("reviewOperation").innerHTML = `<option value="">Any operation</option>` + ops.map(x => `<option>${esc(x)}</option>`).join("");
      const reasons = [...new Set(reviewRows.flatMap(r => r.review_reasons || []))].sort();
      document.getElementById("reviewReason").innerHTML = `<option value="">Any reason</option>` + reasons.map(x => `<option>${esc(x)}</option>`).join("");
      const gapReasons = [...new Set((DATA.coverage.gaps || []).flatMap(r => r.review_reasons || []))].sort();
      document.getElementById("gapReason").innerHTML = `<option value="">Any reason</option>` + gapReasons.map(x => `<option>${esc(x)}</option>`).join("");
      const triageClasses = [...new Set((DATA.triage.items || []).map(r => r.triage_class).filter(Boolean))].sort();
      const triageOptions = triageClasses.map(x => `<option>${esc(x)}</option>`).join("");
      document.getElementById("triageClass").innerHTML = `<option value="">Any triage class</option>` + triageOptions;
      document.getElementById("reviewTriageClass").innerHTML = `<option value="__active__">Active rule-text review</option><option value="">Any triage class</option>` + triageOptions;
      const completionRows = DATA.completion.items || [];
      const terminalStates = [...new Set(completionRows.map(r => r.terminal_state).filter(Boolean))].sort();
      document.getElementById("completionTerminal").innerHTML = `<option value="">Any terminal state</option>` + terminalStates.map(x => `<option>${esc(x)}</option>`).join("");
      const impacts = [...new Set(completionRows.map(r => r.coverage_impact).filter(Boolean))].sort();
      document.getElementById("completionImpact").innerHTML = `<option value="">Any coverage impact</option>` + impacts.map(x => `<option>${esc(x)}</option>`).join("");
    }
    function renderCompletion() {
      const completion = DATA.completion || {};
      const byState = completion.counts?.by_terminal_state || {};
      const byImpact = completion.counts?.by_coverage_impact || {};
      const cards = [
        ["Pending items", completion.pending_event_count || 0, "items classified into terminal review states"],
        ["Closed", completion.closed_count || 0, "closed for code-review audit"],
        ["Open legal", completion.open_count || 0, "requires legal/parser review"],
        ["Incomplete impact", byImpact.incomplete || 0, "coverage still cannot be called complete"]
      ];
      document.getElementById("completionMetrics").innerHTML = cards.map(([label, value, sub]) =>
        `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`
      ).join("") + `<div class="metric"><div class="label">Terminal states</div><div class="sub">${Object.entries(byState).map(([k,v]) => `${esc(k)}: ${esc(v)}`).join("<br>") || "No completion report found."}</div></div>`;
      const q = document.getElementById("completionSearch").value.trim();
      const state = document.getElementById("completionTerminal").value;
      const impact = document.getElementById("completionImpact").value;
      let rows = completion.items || [];
      rows = rows.filter(r => !q || includes(r, q));
      rows = rows.filter(r => !state || r.terminal_state === state);
      rows = rows.filter(r => !impact || r.coverage_impact === impact);
      const page = pageRows("completion", rows);
      document.getElementById("completionTable").innerHTML = table("completion", page.rows, [
        ["State", r => `<strong>${esc(r.terminal_state)}</strong><div class="small">${esc(r.coverage_impact)}</div>`],
        ["Event", r => `<div class="mono">${esc(r.event_id)}</div><div class="small">${esc(r.operation)} · ${esc(r.date || "")}</div>`],
        ["Triage", r => `<span class="chip">${esc(r.triage_class || "untriaged")}</span><div class="small">${esc(r.triage_action || "")}</div>`],
        ["Target", r => `<span class="mono">${esc(r.target?.component_id)}</span>`],
        ["Source", r => `<span class="mono">${esc(r.source_document_id)}</span><div class="small">span ${esc(r.source_span?.start)}-${esc(r.source_span?.end)}</div>`],
        ["Rationale", r => `${esc(r.rationale)}${r.covered_by_event_id ? `<div class="small">Covered by <span class="mono">${esc(r.covered_by_event_id)}</span></div>` : ""}`],
        ["Reasons", r => chips(r.review_reasons)],
        ["Excerpt", r => `<div class="excerpt">${esc(r.excerpt)}</div>${fullTextBlock([r.event_id, r.covered_by_event_id].filter(Boolean))}`]
      ], rows.length, page);
    }
    function renderTriage() {
      const counts = DATA.triage.counts || {};
      const byClass = counts.by_triage_class || {};
      document.getElementById("triageMetrics").innerHTML = Object.entries(byClass).sort((a,b) => b[1] - a[1]).map(([label, value]) =>
        `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`
      ).join("") || `<div class="small">No triage artifact found. Run <span class="mono">python3 main.py version triage-review</span>.</div>`;
      const q = document.getElementById("triageSearch").value.trim();
      const klass = document.getElementById("triageClass").value;
      let rows = DATA.triage.groups || [];
      rows = rows.filter(r => !q || includes(r, q));
      rows = rows.filter(r => !klass || r.triage_class === klass);
      const page = pageRows("triage", rows);
      document.getElementById("triageTable").innerHTML = table("triage", page.rows, [
        ["Class", r => `<strong>${esc(r.triage_class)}</strong><div class="small">${esc(r.recommended_action)}</div>`],
        ["Count", r => esc(r.count)],
        ["Operation", r => esc(r.operation)],
        ["Target", r => `<span class="mono">${esc(r.target_component_id)}</span>`],
        ["Source", r => `<span class="mono">${esc(r.source_document_id)}</span>`],
        ["Reasons", r => chips(r.review_reasons)],
        ["Sample", r => `<div class="excerpt">${esc(r.sample_excerpt)}</div>${fullTextBlock(r.event_ids)}`],
        ["Events", r => (r.event_ids || []).slice(0, 5).map(id => `<span class="chip mono">${esc(id)}</span>`).join(" ")]
      ], rows.length, page);
    }
    function renderReview() {
      const q = document.getElementById("reviewSearch").value.trim();
      const op = document.getElementById("reviewOperation").value;
      const reason = document.getElementById("reviewReason").value;
      const triageClass = document.getElementById("reviewTriageClass").value;
      const decision = document.getElementById("reviewDecision").value;
      let rows = DATA.review.non_validated_events || [];
      rows = rows.filter(r => !q || includes(r, q));
      rows = rows.filter(r => !op || r.operation === op);
      rows = rows.filter(r => !reason || (r.review_reasons || []).includes(reason));
      rows = rows.filter(r => {
        const triage = TRIAGE_BY_EVENT[r.event_id];
        if (triageClass === "__active__") return !triage || ["likely_materializable", "needs_parser_support", "human_review"].includes(triage.triage_class);
        return !triageClass || triage?.triage_class === triageClass;
      });
      rows = rows.filter(r => !decision || decisionFor(r.event_id).decision === decision);
      const page = pageRows("review", rows);
      document.getElementById("reviewTable").innerHTML = table("review", page.rows, [
        ["Decision", r => decisionControls(r.event_id)],
        ["Triage", r => {
          const t = TRIAGE_BY_EVENT[r.event_id] || {};
          return `<strong>${esc(t.triage_class || "untriaged")}</strong><div class="small">${esc(t.recommended_action || "")}</div><div class="small">${esc(t.rationale || "")}</div>`;
        }],
        ["Event", r => `<div class="mono">${esc(r.event_id)}</div><div class="small">${esc(r.parser_pattern)}</div>`],
        ["Operation", r => esc(r.operation)],
        ["Target", r => `<span class="mono">${esc(r.target?.component_id)}</span>`],
        ["Reasons", r => chips(r.review_reasons)],
        ["Source", r => `<span class="mono">${esc(r.source_document_id)}</span><div class="small">record ${esc(r.record_id)}</div>`],
        ["Excerpt", r => `<div class="excerpt">${esc(r.excerpt)}</div>${fullTextBlock([r.event_id])}`]
      ], rows.length, page);
    }
    function renderGaps() {
      const q = document.getElementById("gapSearch").value.trim();
      const reason = document.getElementById("gapReason").value;
      let rows = DATA.coverage.gaps || [];
      rows = rows.filter(r => !q || includes(r, q));
      rows = rows.filter(r => !reason || (r.review_reasons || []).includes(reason));
      const page = pageRows("gaps", rows);
      document.getElementById("gapTable").innerHTML = table("gaps", page.rows, [
        ["Decision", r => decisionControls(r.event_id)],
        ["Event", r => `<div class="mono">${esc(r.event_id)}</div><div class="small">${esc(r.skip_reason)}</div>`],
        ["Date", r => esc(r.date)],
        ["Operation", r => esc(r.operation)],
        ["Target", r => `<span class="mono">${esc(r.target?.component_id)}</span>`],
        ["Reasons", r => chips(r.review_reasons)],
        ["Excerpt", r => `<div class="excerpt">${esc(r.excerpt)}</div>`]
      ], rows.length, page);
    }
    function renderForms() {
      document.getElementById("formsSummary").innerHTML = [
        ["Form events", DATA.forms.event_count],
        ["Baseline forms", DATA.forms.baseline_form_count],
        ["Missing forms", DATA.formGaps.missing_form_count],
        ["Coverage gaps", DATA.forms.coverage_gap_count]
      ].map(([label, value]) => `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join("");
      const q = document.getElementById("formSearch").value.trim();
      let rows = DATA.formGaps.gaps || [];
      rows = rows.filter(r => !q || includes(r, q));
      const page = pageRows("forms", rows);
      document.getElementById("formsTable").innerHTML = table("forms", page.rows, [
        ["Decision", r => decisionControls(r.event_id)],
        ["Event", r => `<div class="mono">${esc(r.event_id)}</div><div class="small">${esc(r.skip_reason)}</div>`],
        ["Date", r => esc(r.date)],
        ["Target form", r => `<span class="mono">${esc(r.target?.component_id)}</span>`],
        ["Operation", r => esc(r.operation)],
        ["Reasons", r => chips(r.review_reasons)],
        ["Excerpt", r => `<div class="excerpt">${esc(r.excerpt)}</div>`]
      ], rows.length, page);
    }
    function renderActSummary() {
      const act = DATA.actSummary || {};
      const tiers = act.tier_counts || {};
      const cards = [
        ["Total components", act.total_components || 0, "Act components graded for citation readiness"],
        ["Tier A", tiers.A || 0, "court-ready: clean baseline, validated, reconciled"],
        ["Tier B", tiers.B || 0, "high confidence, not yet reconciled"],
        ["Tier C", tiers.C || 0, "advisory: events need review or baseline issues"],
        ["Tier D", tiers.D || 0, "do not cite: coverage gaps or contaminated baseline"],
        ["Act applied", act.applied_count || 0, "materialized component edits"],
        ["Act coverage gaps", act.coverage_gap_count || 0, "skipped or failed materialization"],
        ["Recon matched", act.recon_matched || 0, "checkpoint-confirmed reconstructions"],
        ["Recon format-only", act.recon_format_only || 0, "annotation or parser-shape drift"],
        ["Recon mismatched", act.recon_mismatched || 0, "text/hash differences requiring work"],
        ["Recon missing", act.recon_missing || 0, "checkpoint-present but no reconstruction"],
        ["Recon unresolved", act.unresolved || 0, "actionable checkpoint outcomes"]
      ];
      document.getElementById("actSummary").innerHTML = cards.map(([label, value, sub]) =>
        `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`
      ).join("") || `<div class="small">No Act artifacts found. Run the Act pipeline (confidence-tiers, materialize, reconcile).</div>`;
    }
    function renderReconciliation() {
      const q = document.getElementById("reconSearch").value.trim();
      let rows = DATA.reconciliation.priority_review_queue || [];
      rows = rows.filter(r => !q || includes(r, q));
      const page = pageRows("reconciliation", rows);
      document.getElementById("reconciliationTable").innerHTML = table("reconciliation", page.rows, [
        ["Component", r => `<span class="mono">${esc(r.component_id)}</span>`],
        ["Reason", r => `<strong>${esc(r.reason)}</strong><div class="small">${esc(r.audit_class || "")}</div>${r.blocker ? `<div class="small">Blocked: ${esc(r.blocker)}</div>` : ""}<div class="small">${esc(r.recommended_action || "")}</div>`],
        ["Similarity", r => esc(r.best_similarity)],
        ["Candidates", r => esc(r.candidate_event_count ?? r.related_gap_count)],
        ["Related event IDs", r => (r.related_event_ids || []).slice(0, 8).map(id => `<span class="chip mono">${esc(id)}</span>`).join(" ")],
        ["Source docs", r => (r.candidate_source_documents || []).slice(0, 4).map(id => `<div class="small mono">${esc(id)}</div>`).join("")],
        ["Blocker evidence", r => (r.related_gap_summaries || []).map(g => `<details><summary><span class="mono">${esc(g.event_id)}</span> ${chips(g.review_reasons || [])}</summary><div class="small mono">${esc(g.source_document_id || "")}</div><div class="excerpt">${esc(g.excerpt || "")}</div></details>`).join("") || "<span class='small'>none</span>"]
      ], rows.length, page);
    }
    function renderVersions() {
      const q = document.getElementById("versionSearch").value.trim();
      let rows = DATA.nodeVersionSample || [];
      rows = rows.filter(r => !q || includes(r, q));
      const page = pageRows("versions", rows);
      document.getElementById("versionsTable").innerHTML = table("versions", page.rows, [
        ["Component", r => `<span class="mono">${esc(r.component_id)}</span>`],
        ["From", r => esc(r.applicability_start || r.valid_from)],
        ["To", r => esc(r.applicability_end || r.valid_to || "open")],
        ["Event", r => `<span class="mono">${esc(r.created_by_event_id || "baseline")}</span>`],
        ["Text", r => `<div class="excerpt">${esc((r.text || "").slice(0, 420))}</div>`]
      ], rows.length, page);
    }
    function table(name, rows, columns, total, page) {
      const first = total ? page.start + 1 : 0;
      const last = page.start + rows.length;
      const pager = `<div class="pager">
        <span>Showing ${first}-${last} of ${total} rows. Page ${page.totalPages ? pages[name] : 0} of ${page.totalPages}.</span>
        <button class="btn" ${pages[name] <= 1 ? "disabled" : ""} onclick="setPage('${name}', 1)">First</button>
        <button class="btn" ${pages[name] <= 1 ? "disabled" : ""} onclick="setPage('${name}', ${pages[name] - 1})">Prev</button>
        <button class="btn" ${pages[name] >= page.totalPages ? "disabled" : ""} onclick="setPage('${name}', ${pages[name] + 1})">Next</button>
        <button class="btn" ${pages[name] >= page.totalPages ? "disabled" : ""} onclick="setPage('${name}', ${page.totalPages})">Last</button>
      </div>`;
      return `${pager}<table><thead><tr>${columns.map(([h]) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${columns.map(([,fn]) => `<td>${fn(r)}</td>`).join("")}</tr>`).join("")}</tbody></table>${pager}`;
    }
    function renderDecisionSummary() {
      const counts = Object.values(decisions).reduce((acc, row) => { acc[row.decision || "unreviewed"] = (acc[row.decision || "unreviewed"] || 0) + 1; return acc; }, {});
      document.getElementById("decisionSummary").innerHTML = `<h3>Local decision counts</h3><table><tbody>${Object.entries(counts).map(([k,v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</tbody></table>`;
    }
    function exportDecisions() {
      const payload = { exported_at: new Date().toISOString(), decisions };
      document.getElementById("decisionOutput").value = JSON.stringify(payload, null, 2);
    }
    function importDecisions(event) {
      const file = event.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const payload = JSON.parse(String(reader.result || "{}"));
        decisions = payload.decisions || payload || {};
        saveDecisions();
        renderAllTables();
      };
      reader.readAsText(file);
    }
    function clearDecisions() {
      if (!confirm("Clear all local review decisions?")) return;
      decisions = {};
      saveDecisions();
      renderAllTables();
    }
    function renderAllTables() {
      renderTriage();
      renderCompletion();
      renderReview();
      renderGaps();
      renderForms();
      renderActSummary();
      renderReconciliation();
      renderVersions();
      renderGeneratedDecisions();
      renderDecisionSummary();
    }
    renderMetrics();
    populateFilters();
    renderAllTables();
    if (window.mermaid) mermaid.initialize({ startOnLoad: true, securityLevel: "loose", theme: "default" });
  </script>
</body>
</html>
"""


__all__ = ["write_version_history_report"]
