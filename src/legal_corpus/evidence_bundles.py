"""Evidence bundle generator for citation-grade legal version history.

Produces a JSON (and optionally HTML) evidence bundle for any component/date
range that includes:

  - baseline text and source metadata
  - ordered amendment events with source spans, hashes, excerpts
  - corrigendum provenance
  - resulting text versions
  - confidence tier and blockers
  - portal completeness status
  - coverage gaps

A Tier A bundle has zero unresolved blockers.
A Tier D bundle clearly explains why the component must not be cited.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _component_rule_number(component_id: str) -> str | None:
    m = re.search(r"/rule/([^/]+)", component_id)
    return m.group(1) if m else None


def generate_evidence_bundle(
    *,
    component_id: str,
    from_date: str,
    to_date: str,
    version_dir: Path,
    events_path: Path,
    baseline_dir: Path | None = None,
    coverage_gaps_path: Path | None = None,
    confidence_tiers_path: Path | None = None,
    portal_completeness_path: Path | None = None,
    corrigendum_ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Generate a citation-grade evidence bundle for a component/date range."""

    version_dir = Path(version_dir)

    # ------------------------------------------------------------------ #
    # 1. Baseline
    # ------------------------------------------------------------------ #
    baseline_text = ""
    baseline_source = {}
    baseline_decontaminated = False
    if baseline_dir:
        for bc in _load_jsonl(Path(baseline_dir) / "baseline_components.jsonl"):
            if bc.get("component_id") == component_id:
                baseline_text = bc.get("text", "")
                baseline_source = {
                    "source_basis": bc.get("source_basis", ""),
                    "text_sha256": bc.get("text_sha256", ""),
                    "component_type": bc.get("component_type", ""),
                    "blocked": bc.get("blocked", False),
                    "block_reasons": bc.get("block_reasons", []),
                }
                baseline_decontaminated = "decontaminate" in str(bc.get("source_basis", ""))
                break

    # ------------------------------------------------------------------ #
    # 2. Node versions in the requested interval
    # ------------------------------------------------------------------ #
    all_versions = _load_jsonl(version_dir / "node_versions.jsonl")
    component_versions = [
        v for v in all_versions
        if v.get("component_id") == component_id
        and _date_gte(v.get("valid_from", ""), from_date)
        and _date_lte(v.get("valid_from", ""), to_date)
    ]
    # Always include the baseline version if it precedes from_date
    baseline_versions = [
        v for v in all_versions
        if v.get("component_id") == component_id
        and not _date_gte(v.get("valid_from", ""), from_date)
    ]
    if baseline_versions and not component_versions:
        component_versions = [baseline_versions[-1]]  # latest pre-interval version
    elif baseline_versions:
        # Include the immediately preceding version as context
        preceding = sorted(baseline_versions, key=lambda v: v.get("valid_from", ""))[-1]
        if preceding not in component_versions:
            component_versions.insert(0, preceding)

    # Collect event IDs from version chains
    version_event_ids: set[str] = set()
    for v in component_versions:
        version_event_ids.update(v.get("event_chain") or [])

    # ------------------------------------------------------------------ #
    # 3. Amendment events
    # ------------------------------------------------------------------ #
    all_events = _load_jsonl(events_path)
    # Index by event_id and also by target component (including sub-rules)
    events_by_id: dict[str, dict[str, Any]] = {}
    events_for_component: list[dict[str, Any]] = []
    for e in all_events:
        eid = e.get("event_id", "")
        events_by_id[eid] = e
        tgt = e.get("target", {}).get("component_id", "")
        if tgt == component_id:
            events_for_component.append(e)
        # Also match sub-rule events if component_id is a parent rule
        elif "/subrule/" not in component_id and tgt.startswith(component_id + "/"):
            events_for_component.append(e)

    # Sort by effective date then source_span.start
    def _event_sort_key(e: dict[str, Any]) -> tuple:
        lt = e.get("legal_time", {})
        date = lt.get("commencement_date") or lt.get("applicability_start") or ""
        span = e.get("evidence", {}).get("source_span", {})
        return (str(date), span.get("start", 0))

    events_for_component.sort(key=_event_sort_key)

    # Build event chain entries
    event_chain: list[dict[str, Any]] = []
    for e in events_for_component:
        src = e.get("source", {})
        ev = e.get("evidence", {})
        span = ev.get("source_span", {})
        review = e.get("review", {})
        payload = e.get("payload", {})

        # Corrigendum provenance
        corr_apps = payload.get("corrigendum_applications") or []

        event_chain.append({
            "event_id": e.get("event_id", ""),
            "legacy_event_id": e.get("legacy_event_id", ""),
            "operation": e.get("operation", ""),
            "status": e.get("status", ""),
            "effective_date": _event_sort_key(e)[0],
            "source": {
                "document_id": src.get("document_id", ""),
                "instrument_number": src.get("instrument_number", ""),
                "publication_date": src.get("publication_date", ""),
                "source_url": src.get("source_url", ""),
                "source_file_sha256": src.get("source_file_sha256", ""),
                "source_text_sha256": src.get("source_text_sha256", ""),
            },
            "source_span": {
                "start": span.get("start"),
                "end": span.get("end"),
                "text_hash": span.get("text_hash") or span.get("sha256") or "",
                "sha256": span.get("sha256") or span.get("text_hash") or "",
            },
            "excerpt": ev.get("excerpt", ""),
            "payload_summary": _summarize_payload(e),
            "review_reasons": review.get("review_reasons", []),
            "corrigendum_applications": corr_apps,
            "in_version_chain": e.get("event_id", "") in version_event_ids,
        })

    # ------------------------------------------------------------------ #
    # 4. Coverage gaps for this component
    # ------------------------------------------------------------------ #
    component_gaps: list[dict[str, Any]] = []
    if coverage_gaps_path:
        gaps_data = _load_json(Path(coverage_gaps_path))
        for g in gaps_data.get("gaps", []):
            eid = g.get("event_id", "")
            ev = events_by_id.get(eid, {})
            tgt = ev.get("target", {}).get("component_id", "")
            gap_target = str(g.get("target", ""))
            if (tgt == component_id or
                (not tgt and component_id in gap_target) or
                ("/subrule/" not in component_id and tgt.startswith(component_id + "/"))):
                component_gaps.append({
                    "event_id": eid,
                    "skip_reason": g.get("skip_reason", ""),
                    "operation": g.get("operation", ""),
                })

    # ------------------------------------------------------------------ #
    # 5. Confidence tier
    # ------------------------------------------------------------------ #
    confidence: dict[str, Any] = {"tier": "unknown", "tier_blockers": []}
    if confidence_tiers_path:
        tiers_data = _load_json(Path(confidence_tiers_path))
        detail = tiers_data.get("component_details", {}).get(component_id, {})
        confidence = {
            "tier": detail.get("tier", "unknown"),
            "tier_blockers": detail.get("tier_blockers", []),
            "reasons": detail.get("reasons", []),
            "reconciliation": detail.get("reconciliation", ""),
            "version_count": detail.get("version_count", 0),
        }

    # ------------------------------------------------------------------ #
    # 6. Portal completeness for this component
    # ------------------------------------------------------------------ #
    portal: dict[str, Any] = {"status": "unknown"}
    if portal_completeness_path:
        portal_data = _load_json(Path(portal_completeness_path))
        rule_num = _component_rule_number(component_id)
        rules_map = portal_data.get("rules", {})
        if isinstance(rules_map, dict) and rule_num:
            rule_entry = rules_map.get(rule_num, {})
            if rule_entry:
                portal = {
                    "status": rule_entry.get("portal_completeness_status", "unknown"),
                    "missing_sources": [str(m) for m in rule_entry.get("missing_source_notifications", [])],
                    "unlinked_sources": [],
                    "external_references": [],
                }
                # Add unlinked from portal refs not in event refs
                portal_refs = set()
                for pref in rule_entry.get("portal_notification_refs", []):
                    if isinstance(pref, dict):
                        portal_refs.add(pref.get("notification_number", ""))
                    elif isinstance(pref, str):
                        portal_refs.add(pref)
                event_refs = set(rule_entry.get("event_notification_refs", []))
                unlinked = portal_refs - event_refs - {""}
                portal["unlinked_sources"] = sorted(unlinked)
                portal["external_references"] = [str(e) for e in range(rule_entry.get("external_reference_notification_count", 0))]

    # ------------------------------------------------------------------ #
    # 7. Corrigenda affecting this component
    # ------------------------------------------------------------------ #
    corrigenda: list[dict[str, Any]] = []
    if corrigendum_ledger_path:
        for c in _load_jsonl(Path(corrigendum_ledger_path)):
            rule_refs = c.get("rule_references", [])
            corr_rules = [_component_rule_number(r) for r in rule_refs if r]
            corr_rules = [r for r in corr_rules if r]
            rule_num = _component_rule_number(component_id)
            if rule_num and rule_num in corr_rules:
                corrigenda.append({
                    "corrigendum_event_id": c.get("corrigendum_event_id", ""),
                    "corrected_notification_refs": c.get("corrected_notification_refs", []),
                    "corrections": c.get("corrections", []),
                    "effect_date": c.get("corrigendum_effect_date", ""),
                    "retrospective": c.get("retrospective", False),
                })

    # ------------------------------------------------------------------ #
    # 8. Assemble bundle
    # ------------------------------------------------------------------ #
    bundle = {
        "component_id": component_id,
        "from_date": from_date,
        "to_date": to_date,
        "generated_at": _now_iso(),
        "baseline": {
            "text": baseline_text,
            **baseline_source,
            "decontamination_applied": baseline_decontaminated,
            "base_as_of": from_date if not baseline_text else None,
        },
        "event_chain": event_chain,
        "node_versions": [
            {
                "valid_from": v.get("valid_from", ""),
                "valid_to": v.get("valid_to"),
                "text_sha256": v.get("text_sha256", ""),
                "text_preview": (v.get("text", "") or "")[:500],
                "text_length": len(v.get("text", "") or ""),
                "event_chain_ids": v.get("event_chain", []),
                "source_basis": v.get("source_basis", ""),
            }
            for v in component_versions
        ],
        "coverage_gaps": component_gaps,
        "confidence": confidence,
        "portal": portal,
        "corrigenda": corrigenda,
    }

    # ------------------------------------------------------------------ #
    # 9. Citation readiness summary
    # ------------------------------------------------------------------ #
    unresolved_gaps = len(component_gaps)
    unresolved_events = sum(1 for e in event_chain if e["status"] != "validated" and e["status"] != "rejected")
    tier = confidence.get("tier", "unknown")
    bundle["citation_readiness"] = {
        "tier": tier,
        "unresolved_gaps": unresolved_gaps,
        "unresolved_events": unresolved_events,
        "can_cite": tier == "A" and unresolved_gaps == 0,
        "must_not_cite": tier in ("D",) or unresolved_gaps > 0,
        "warning": _citation_warning(tier, unresolved_gaps, unresolved_events),
    }

    # ------------------------------------------------------------------ #
    # 10. Deterministic validation block (for backward-compat tests)
    # ------------------------------------------------------------------ #
    def _span_hash(span: dict) -> str:
        return span.get("text_hash") or span.get("sha256") or ""

    missing_provenance: list[str] = []
    for e in event_chain:
        if not _span_hash(e.get("source_span", {})):
            missing_provenance.append(e.get("event_id", ""))
    bundle["deterministic_validation"] = {
        "bundle_citable": bundle["citation_readiness"]["can_cite"],
        "events_missing_required_provenance": missing_provenance,
        "has_unresolved_blockers": len(component_gaps) > 0 or unresolved_events > 0 or tier in ("C", "D"),
    }

    # Alias: amendment_events for backward-compat (with source_span_hash field)
    bundle["amendment_events"] = [
        {
            **e,
            "source_span_hash": _span_hash(e.get("source_span", {})),
        }
        for e in event_chain
    ]

    # ------------------------------------------------------------------ #
    # 11. Corrigendum provenance (events with corrigendum_applications)
    # ------------------------------------------------------------------ #
    payload_applications: list[dict[str, Any]] = []
    for e in events_for_component:
        apps = e.get("payload", {}).get("corrigendum_applications") or []
        if apps:
            payload_applications.append({
                "event_id": e.get("event_id", ""),
                "corrigendum_provenance": apps,
            })
    bundle["corrigendum_provenance"] = {
        "event_payload_applications": payload_applications,
    }

    return bundle


def build_evidence_bundle(
    component_id: str,
    *,
    from_date: str,
    to_date: str,
    version_dir: Path,
    amendment_events_path: Path,
    baseline_components_path: Path | None = None,
    confidence_tiers_path: Path | None = None,
    portal_completeness_path: Path | None = None,
    coverage_gaps_path: Path | None = None,
    corrigendum_ledger_path: Path | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Backward-compat wrapper around generate_evidence_bundle.

    Accepts the alternate parameter names used in earlier tests and
    delegates to generate_evidence_bundle.
    """
    # baseline_components_path is a file, not a directory — derive the dir
    baseline_dir = None
    if baseline_components_path:
        bcp = Path(baseline_components_path)
        if bcp.is_file():
            baseline_dir = bcp.parent
        else:
            baseline_dir = bcp

    if coverage_gaps_path is None:
        coverage_gaps_path = Path(version_dir) / "coverage_gaps.json"
    if corrigendum_ledger_path is None:
        clp = Path(version_dir) / "corrigendum_ledger.jsonl"
        corrigendum_ledger_path = clp if clp.exists() else None

    return generate_evidence_bundle(
        component_id=component_id,
        from_date=from_date,
        to_date=to_date,
        version_dir=Path(version_dir),
        events_path=Path(amendment_events_path),
        baseline_dir=baseline_dir,
        coverage_gaps_path=Path(coverage_gaps_path) if coverage_gaps_path else None,
        confidence_tiers_path=Path(confidence_tiers_path) if confidence_tiers_path else None,
        portal_completeness_path=Path(portal_completeness_path) if portal_completeness_path else None,
        corrigendum_ledger_path=Path(corrigendum_ledger_path) if corrigendum_ledger_path else None,
    )


def _summarize_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Extract a concise payload summary without dumping full text."""
    p = event.get("payload", {})
    summary: dict[str, Any] = {}
    for key in ("old_text", "new_text", "structural_text", "insert_text", "omit_text", "content"):
        val = str(p.get(key, "") or "").strip()
        if val:
            summary[key] = val[:200] + ("..." if len(val) > 200 else "")
    for key in ("label", "heading", "node_type", "position", "anchor_rule"):
        val = p.get(key)
        if val:
            summary[key] = val
    return summary


def _citation_warning(tier: str, gaps: int, events: int) -> str:
    if tier == "A" and gaps == 0:
        return "Court-ready: reconciled, validated, clean baseline."
    if tier == "B":
        return "High confidence but not reconciled against checkpoint. Verify externally before citing."
    if tier == "C":
        return f"Advisory only: {events} unvalidated events. Do not cite without independent verification."
    if tier == "D":
        return f"Do not cite: {gaps} unresolved gaps, {events} unvalidated events. Text may be incomplete or incorrect."
    return f"Unknown tier ({tier}). Treat as advisory."


def _date_gte(d1: str, d2: str) -> bool:
    return str(d1) >= str(d2)


def _date_lte(d1: str, d2: str) -> bool:
    return str(d1) <= str(d2)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------- #
# HTML rendering
# ---------------------------------------------------------------------- #

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Evidence Bundle — {component_id}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem auto; max-width: 900px; color: #222; }}
h1 {{ font-size: 1.4rem; border-bottom: 2px solid #333; padding-bottom: .3rem; }}
h2 {{ font-size: 1.1rem; margin-top: 2rem; color: #555; }}
.tier-A {{ color: #1a7d1a; font-weight: bold; }}
.tier-B {{ color: #0050a0; font-weight: bold; }}
.tier-C {{ color: #b86000; font-weight: bold; }}
.tier-D {{ color: #c00; font-weight: bold; }}
.event {{ border: 1px solid #ddd; border-radius: 4px; padding: .6rem; margin: .4rem 0; }}
.event.validated {{ border-left: 4px solid #1a7d1a; }}
.event.needs_review {{ border-left: 4px solid #b86000; }}
.event.rejected {{ border-left: 4px solid #c00; opacity: .6; }}
.event-id {{ font-family: monospace; font-size: .8rem; color: #666; }}
.excerpt {{ background: #f5f5f5; padding: .3rem; font-size: .85rem; margin: .3rem 0; white-space: pre-wrap; }}
.gap {{ background: #fff3cd; padding: .3rem; border-radius: 3px; margin: .2rem 0; }}
.warning-A {{ background: #d4edda; padding: .5rem; border-radius: 4px; }}
.warning-D {{ background: #f8d7da; padding: .5rem; border-radius: 4px; }}
.warning-C {{ background: #fff3cd; padding: .5rem; border-radius: 4px; }}
table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
th, td {{ border: 1px solid #ddd; padding: .3rem; text-align: left; }}
th {{ background: #f0f0f0; }}
.muted {{ color: #888; font-size: .8rem; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def render_evidence_bundle_html(bundle: dict[str, Any]) -> str:
    """Render an evidence bundle as a self-contained HTML document."""

    cid = bundle.get("component_id", "?")
    tier = bundle.get("citation_readiness", {}).get("tier", "?")
    warning = bundle.get("citation_readiness", {}).get("warning", "")

    parts: list[str] = []
    parts.append("<h1>Evidence Bundle</h1>")
    parts.append(
        "<p><strong>Component:</strong> <code>" + escape(cid) + "</code><br>"
        "<strong>Date range:</strong> " + escape(bundle.get("from_date", "")) + " → " + escape(bundle.get("to_date", "")) + "<br>"
        '<strong>Citation tier:</strong> <span class="tier-' + str(tier) + '">' + str(tier) + "</span></p>"
    )

    # Citation warning banner
    parts.append(f'<div class="warning-{tier}">{escape(warning)}</div>')

    # Baseline
    baseline = bundle.get("baseline", {})
    parts.append("<h2>Baseline Source</h2>")
    if baseline.get("text"):
        parts.append(f"<p><strong>Source:</strong> <code>{escape(baseline.get('source_basis',''))}</code><br>"
                     f"<strong>SHA-256:</strong> <code>{escape(baseline.get('text_sha256',''))}</code><br>"
                     f"<strong>Decontaminated:</strong> {baseline.get('decontamination_applied', False)}<br>"
                     f"<strong>Blocked:</strong> {baseline.get('blocked', False)}</p>")
    else:
        parts.append('<p class="muted">No baseline found for this component.</p>')

    # Event chain
    chain = bundle.get("event_chain", [])
    parts.append(f"<h2>Amendment Events ({len(chain)})</h2>")
    if chain:
        for e in chain:
            status = e.get("status", "")
            eid = e.get("event_id", "")
            op = e.get("operation", "")
            date = e.get("effective_date", "")
            src = e.get("source", {})
            span = e.get("source_span", {})
            excerpt = e.get("excerpt", "")
            reasons = e.get("review_reasons", [])
            corr = e.get("corrigendum_applications", [])

            parts.append(f'<div class="event {status}">')
            parts.append(f'<div><strong>{escape(op)}</strong> — {escape(date)} '
                         f'<span class="event-id">({escape(eid)})</span></div>')
            parts.append(f'<div class="muted">Source: {escape(src.get("document_id",""))} '
                         f'| Span: [{span.get("start","?")}–{span.get("end","?")}] '
                         f'| Hash: <code>{escape(span.get("text_hash","")[:16])}…</code></div>')
            if excerpt:
                parts.append(f'<div class="excerpt">{escape(excerpt[:300])}{"…" if len(excerpt)>300 else ""}</div>')
            if reasons:
                parts.append(f'<div class="muted">Review reasons: {escape(", ".join(reasons[:4]))}</div>')
            if corr:
                parts.append(f'<div class="muted">⚠️ Corrigendum applied: {len(corr)} correction(s)</div>')
            parts.append('</div>')
    else:
        parts.append('<p class="muted">No amendment events for this component.</p>')

    # Node versions
    versions = bundle.get("node_versions", [])
    parts.append(f"<h2>Text Versions ({len(versions)})</h2>")
    if versions:
        parts.append('<table><tr><th>Valid from</th><th>Valid to</th><th>SHA-256</th><th>Length</th><th>Events</th></tr>')
        for v in versions:
            chain_ids = v.get("event_chain_ids", [])
            parts.append(f'<tr><td>{escape(v.get("valid_from",""))}</td>'
                         f'<td>{escape(str(v.get("valid_to","")))}</td>'
                         f'<td><code>{escape(v.get("text_sha256","")[:16])}…</code></td>'
                         f'<td>{v.get("text_length",0)}</td>'
                         f'<td>{len(chain_ids)}</td></tr>')
        parts.append('</table>')
    else:
        parts.append('<p class="muted">No materialized versions in this interval.</p>')

    # Coverage gaps
    gaps = bundle.get("coverage_gaps", [])
    if gaps:
        parts.append(f"<h2>Unresolved Coverage Gaps ({len(gaps)})</h2>")
        for g in gaps:
            parts.append(f'<div class="gap"><strong>{escape(g.get("operation",""))}</strong> '
                         f'<span class="event-id">({escape(g.get("event_id",""))})</span>: '
                         f'{escape(g.get("skip_reason","")[:120])}</div>')

    # Confidence blockers
    confidence = bundle.get("confidence", {})
    blockers = confidence.get("tier_blockers", [])
    if blockers:
        parts.append(f"<h2>Confidence Blockers ({len(blockers)})</h2>")
        parts.append('<table><tr><th>Event ID</th><th>Reason</th><th>Source</th></tr>')
        for b in blockers[:20]:
            parts.append(f'<tr><td><code>{escape(str(b.get("event_id",""))[:20])}</code></td>'
                         f'<td>{escape(b.get("reason",""))}</td>'
                         f'<td>{escape(str(b.get("source","")))}</td></tr>')
        parts.append('</table>')
        if len(blockers) > 20:
            parts.append(f'<p class="muted">…and {len(blockers)-20} more.</p>')

    # Portal
    portal = bundle.get("portal", {})
    if portal.get("status") != "unknown":
        parts.append(f"<h2>Portal Completeness</h2>")
        parts.append(f'<p><strong>Status:</strong> {escape(portal.get("status",""))}')
        if portal.get("missing_sources"):
            parts.append(f'<br><strong>Missing sources:</strong> {escape(", ".join(portal["missing_sources"][:5]))}')
        if portal.get("unlinked_sources"):
            parts.append(f'<br><strong>Unlinked sources:</strong> {escape(", ".join(portal["unlinked_sources"][:5]))}')
        parts.append('</p>')

    # Corrigenda
    corrigenda = bundle.get("corrigenda", [])
    if corrigenda:
        parts.append(f"<h2>Corrigenda ({len(corrigenda)})</h2>")
        for c in corrigenda:
            parts.append(f'<div class="event needs_review">'
                         f'<strong>{escape(c.get("corrigendum_event_id",""))}</strong> '
                         f'corrects {escape(", ".join(c.get("corrected_notification_refs",[])[:3]))}'
                         f'</div>')

    html = _HTML_TEMPLATE.format(component_id=escape(cid), body="\n".join(parts))
    return html


__all__ = [
    "generate_evidence_bundle",
    "build_evidence_bundle",
    "render_evidence_bundle_html",
]
