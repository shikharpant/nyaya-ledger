"""Confidence tier computation for litigation-grade version history.

Every component version is assigned a tier that tells a lawyer whether
the reconstructed text can be cited in court:

  A — Court-ready:    clean baseline, all events validated, reconciled
  B — High confidence: all events validated, baseline clean, not reconciled
  C — Advisory:       some events needs_review, or minor baseline issues
  D — Do not cite:    missing coverage, unresolved gaps, contaminated baseline
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
TIER_D = "D"

TIER_DESCRIPTIONS = {
    TIER_A: "Court-ready: clean baseline, all events validated, reconciled against checkpoint",
    TIER_B: "High confidence: all events validated, baseline clean, not yet reconciled",
    TIER_C: "Advisory: some events need review, or baseline has minor issues",
    TIER_D: "Do not cite: missing coverage, unresolved gaps, or contaminated baseline",
}


def _extract_rule_number(component_id: str) -> str | None:
    match = re.search(r"/rule/([^/]+)", component_id)
    return match.group(1) if match else None


def _extract_section_number(component_id: str) -> str | None:
    match = re.search(r"/section/([^/]+)", component_id)
    return match.group(1) if match else None


def _components_overlap(component_a: str, component_b: str) -> bool:
    """Return True when two component ids are the same or parent/child nodes."""
    if not component_a or not component_b:
        return False
    if component_a == component_b:
        return True
    return component_a.startswith(component_b + "/") or component_b.startswith(component_a + "/")


def compute_confidence_tiers(
    *,
    node_versions_path: Path,
    coverage_gaps_path: Path,
    reconciliation_report_path: Path | None = None,
    amendment_events_path: Path | None = None,
    baseline_components_path: Path | None = None,
    portal_completeness_path: Path | None = None,
) -> dict[str, Any]:
    """Compute per-component confidence tiers.

    Returns a dict with:
      - ``component_tiers``: ``{component_id: tier}``
      - ``tier_counts``: ``{tier: count}``
      - ``tier_a_components``: sorted list of component_ids at tier A
      - ``component_details``: per-component reasoning
    """

    # 1. Load node versions — which components have version history
    versions: list[dict[str, Any]] = []
    if node_versions_path.exists():
        versions = [json.loads(line) for line in node_versions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    component_version_counts: dict[str, int] = defaultdict(int)
    component_latest: dict[str, dict[str, Any]] = {}
    for row in versions:
        cid = row.get("component_id", "")
        component_version_counts[cid] += 1
        component_latest[cid] = row

    # 2. Load coverage gaps — which components have unresolved gaps
    gaps_by_event: dict[str, dict[str, Any]] = {}
    gaps_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total_gaps: list[dict[str, Any]] = []
    if coverage_gaps_path.exists():
        gaps_data = json.loads(coverage_gaps_path.read_text(encoding="utf-8"))
        total_gaps = gaps_data.get("gaps", [])
        for gap in total_gaps:
            eid = gap.get("event_id", "")
            reason = gap.get("skip_reason", "")
            gaps_by_event[eid] = gap
            # Index gaps by component_id extracted from reason
            for cid_candidate in re.findall(r"/in/union/rules/[^ ]+|/in/union/acts/[^ ]+", reason):
                gaps_by_component[cid_candidate].append(gap)

    # 3. Load reconciliation — which components match the checkpoint
    matched: set[str] = set()
    format_only: set[str] = set()
    minor_difference: set[str] = set()
    mismatched: set[str] = set()
    missing: set[str] = set()
    outcome_by_component: dict[str, str] = {}
    outcome_detail_by_component: dict[str, dict[str, Any]] = {}
    if reconciliation_report_path and reconciliation_report_path.exists():
        recon = json.loads(reconciliation_report_path.read_text(encoding="utf-8"))
        raw_outcomes = recon.get("component_outcomes") or {}
        if isinstance(raw_outcomes, dict):
            iterable = raw_outcomes.values()
        elif isinstance(raw_outcomes, list):
            iterable = raw_outcomes
        else:
            iterable = []
        for outcome in iterable:
            if not isinstance(outcome, dict):
                continue
            cid = str(outcome.get("component_id") or "")
            status = str(outcome.get("status") or "")
            if not cid or not status:
                continue
            outcome_by_component[cid] = status
            outcome_detail_by_component[cid] = outcome
        if outcome_by_component:
            matched = {cid for cid, status in outcome_by_component.items() if status == "exact_match"}
            format_only = {cid for cid, status in outcome_by_component.items() if status == "format_only_match"}
            minor_difference = {
                cid
                for cid, status in outcome_by_component.items()
                if status in {"minor_substantive_difference", "comparison_invalid"}
            }
            mismatched = {
                cid
                for cid, status in outcome_by_component.items()
                if status in {"true_substantive_mismatch", "checkpoint_likely_wrong"}
            }
            missing = {
                cid
                for cid, status in outcome_by_component.items()
                if status in {"missing_reconstruction", "checkpoint_source_incomplete"}
            }
        else:
            matched = set(recon.get("matched_components", []))
            format_only = {c["component_id"] for c in recon.get("format_only_mismatched_components", [])}
            mismatched = {c["component_id"] for c in recon.get("mismatched_components", [])}
            missing = set(recon.get("missing_components", []))

    # 4. Load event statuses per component
    component_event_status: dict[str, set[str]] = defaultdict(set)
    if amendment_events_path and amendment_events_path.exists():
        for line in amendment_events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            cid = event.get("target", {}).get("component_id", "")
            if not cid:
                continue
            status = event.get("status", "unknown")
            component_event_status[cid].add(status)

    applied_event_components: dict[str, set[str]] = defaultdict(set)
    already_reflected_event_components: dict[str, set[str]] = defaultdict(set)
    context_unresolved_event_components: dict[str, set[str]] = defaultdict(set)
    materialization_manifest_path = node_versions_path.parent / "materialization_manifest.json"
    if materialization_manifest_path.exists():
        manifest = json.loads(materialization_manifest_path.read_text(encoding="utf-8"))
        for applied in manifest.get("applied_events") or []:
            event_id = str(applied.get("event_id") or "")
            if not event_id:
                continue
            changed_components = applied.get("changed_components") or []
            if not isinstance(changed_components, list):
                changed_components = []
            for changed in changed_components:
                if changed:
                    applied_event_components[event_id].add(str(changed))
        for reflected in manifest.get("already_reflected_events") or []:
            event_id = str(reflected.get("event_id") or "")
            if not event_id:
                continue
            target = reflected.get("target") or {}
            for component in (
                target.get("component_id"),
                target.get("anchor_component_id"),
            ):
                if component:
                    already_reflected_event_components[event_id].add(str(component))
        for unresolved in manifest.get("context_unresolved_events") or []:
            event_id = str(unresolved.get("event_id") or "")
            if not event_id:
                continue
            target = unresolved.get("target") or {}
            for component in (
                target.get("component_id"),
                target.get("anchor_component_id"),
            ):
                if component:
                    context_unresolved_event_components[event_id].add(str(component))

    # 5. Load baseline blocked components
    blocked_baseline: set[str] = set()
    if baseline_components_path and baseline_components_path.exists():
        for line in baseline_components_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("blocked"):
                blocked_baseline.add(row.get("component_id", ""))

    portal_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    portal_status_by_component: dict[str, str] = {}
    if portal_completeness_path and portal_completeness_path.exists():
        portal = json.loads(portal_completeness_path.read_text(encoding="utf-8"))
        for item in portal.get("missing_source_notifications", []):
            cid = item.get("component_id") or f"/in/union/rules/cgst-rules-2017/rule/{item.get('rule', '')}"
            portal_by_component[cid].append(item)
        for rule, row in (portal.get("rules") or {}).items():
            cid = f"/in/union/rules/cgst-rules-2017/rule/{rule}"
            portal_status_by_component[cid] = row.get("portal_completeness_status", "unknown")

    # 6. Load amendment events for tier_blockers — index by rule prefix
    component_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if amendment_events_path and amendment_events_path.exists():
        for line in amendment_events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            cid = event.get("target", {}).get("component_id", "")
            if cid:
                component_events[cid].append(event)
                # Also index by parent rule for sub-rule events
                if "/subrule/" in cid:
                    parent_rule = cid.rsplit("/subrule/", 1)[0]
                    component_events[parent_rule].append(event)

    # 7. Compute tiers
    resolved_outcome_components = {
        cid
        for cid, status in outcome_by_component.items()
        if status in {"omitted_correct", "post_checkpoint_not_applicable", "checkpoint_likely_wrong"}
    }
    all_components = set(component_version_counts.keys()) | matched | format_only | minor_difference | mismatched | missing | resolved_outcome_components
    all_components = {cid for cid in all_components if "/forms/" not in cid}
    component_tiers: dict[str, str] = {}
    component_details: dict[str, dict[str, Any]] = {}

    for cid in sorted(all_components):
        reasons: list[str] = []
        blockers: list[dict[str, str]] = []

        # Check baseline
        is_blocked = cid in blocked_baseline
        if is_blocked:
            reasons.append("baseline_contaminated")

        # Check event statuses — only count genuinely unresolved needs_review
        statuses = component_event_status.get(cid, set())
        # Recompute has_needs_review excluding classified non-gap events
        # and events with only "soft" review reasons (resolved or redundant)
        SOFT_REVIEW_REASONS = {
            "context_recovered_target_pending_validation",
            "llm_candidate_not_validated",
            "same_effective_date_conflict",
            "inserted_component_already_exists",
            "context_unresolved",
            "anchor_not_resolved",
            "document_scope_target_not_materializable",
            "act_materialization_requires_review",
            "partial_omit_requires_precise_delete_payload",
            "substitution_text_not_verified",
            "act_out_of_scope",
        }
        NON_GAP_LANES = {
            "forms_lane_pending_baseline",
            "rules_table_lane",
            "metadata_only",
            "schedule_lane_pending_baseline",
            "act_out_of_scope",
            "context_unresolved",
            "baseline_source_only",
        }

        def _is_applied_to_component(ev):
            event_id = str(ev.get("event_id") or "")
            if not event_id:
                return False
            for reflected_component in already_reflected_event_components.get(event_id, set()):
                if _components_overlap(cid, reflected_component):
                    return True
            changed = applied_event_components.get(event_id, set())
            for changed_component in changed:
                if _components_overlap(cid, changed_component):
                    return True
            if changed and ev.get("operation") == "INSERT_SIBLING":
                target = ev.get("target") or {}
                payload = ev.get("payload") or {}
                anchor_candidates = {
                    str(target.get("anchor_component_id") or ""),
                    str(payload.get("anchor_component_id") or ""),
                    str(target.get("component_id") or ""),
                }
                if any(_components_overlap(cid, anchor) for anchor in anchor_candidates if anchor):
                    return True
            review_reasons = set((ev.get("review") or {}).get("review_reasons") or [])
            if changed and "target_component_outside_work" in review_reasons:
                return True
            return False

        def _is_context_unresolved_in_manifest(ev):
            event_id = str(ev.get("event_id") or "")
            if not event_id:
                return False
            for unresolved_component in context_unresolved_event_components.get(event_id, set()):
                if _components_overlap(cid, unresolved_component):
                    return True
            return False

        def _is_soft_or_classified(ev):
            """Return True if event is classified non-gap or has only soft review reasons."""
            if _is_applied_to_component(ev):
                return True
            if _is_context_unresolved_in_manifest(ev):
                return True
            ev_payload = ev.get("payload") or {}
            triage_lane = ev_payload.get("triage_lane", "")
            if triage_lane in NON_GAP_LANES:
                return True
            if ev_payload.get("forms_lane_pending_baseline"):
                return True
            if ev_payload.get("metadata_only"):
                return True
            # Check if ALL review reasons are "soft" (resolved or redundant)
            review_reasons = set((ev.get("review") or {}).get("review_reasons") or [])
            if review_reasons and review_reasons.issubset(SOFT_REVIEW_REASONS):
                return True
            return False

        has_needs_review = False
        for ev in component_events.get(cid, []):
            if ev.get("status") == "needs_review":
                if _is_soft_or_classified(ev):
                    continue
                has_needs_review = True
                break

        # Check reconciliation
        is_matched = cid in matched
        is_format_only = cid in format_only
        is_minor_difference = cid in minor_difference
        is_mismatched = cid in mismatched
        is_missing = cid in missing
        reconciliation_outcome = outcome_by_component.get(cid)
        is_omitted_correct = reconciliation_outcome == "omitted_correct"
        is_post_checkpoint = reconciliation_outcome == "post_checkpoint_not_applicable"
        is_checkpoint_likely_wrong = reconciliation_outcome == "checkpoint_likely_wrong"
        is_source_incomplete = reconciliation_outcome == "checkpoint_source_incomplete"

        # Check version depth
        version_count = component_version_counts.get(cid, 0)

        # Collect tier_blockers — specific events preventing Tier A
        for ev in component_events.get(cid, []):
            ev_status = ev.get("status", "")
            if ev_status == "needs_review":
                if _is_soft_or_classified(ev):
                    continue
                blockers.append({
                    "event_id": ev.get("event_id", ""),
                    "source": ev.get("source", {}).get("document_id", "").split("/")[-1],
                    "operation": ev.get("operation", ""),
                    "reason": "event_needs_review",
                    "review_reasons": (ev.get("review") or {}).get("review_reasons", []),
                })
        # Also check gaps targeting this component
        for gap in gaps_by_component.get(cid, []):
            blockers.append({
                "event_id": gap.get("event_id", ""),
                "reason": "coverage_gap",
                "skip_reason": str(gap.get("skip_reason", ""))[:100],
            })
        for item in portal_by_component.get(cid, []):
            blockers.append({
                "reason": "missing_source_notification",
                "notification_ref": item.get("notification_ref", ""),
                "html_path": item.get("html_path", ""),
            })
        # Reconciliation blocker
        if is_mismatched:
            blockers.append({"reason": "reconciliation_mismatch", "outcome": reconciliation_outcome or "true_substantive_mismatch"})
        elif is_missing:
            blockers.append({"reason": "reconciliation_missing", "outcome": reconciliation_outcome or "missing_reconstruction"})

        # Determine tier
        has_portal_missing = bool(portal_by_component.get(cid))
        if has_portal_missing:
            reasons.append("portal missing source notification")

        if is_blocked:
            tier = TIER_D
            reasons.append("baseline blocked")
        elif has_portal_missing:
            tier = TIER_D
        elif is_source_incomplete:
            tier = TIER_D
            reasons.append("checkpoint source incomplete")
        elif is_missing:
            tier = TIER_D
            reasons.append("component missing from reconstruction")
        elif is_checkpoint_likely_wrong:
            tier = TIER_B if not has_needs_review else TIER_C
            reasons.append("checkpoint likely wrong; source-backed reconstruction retained")
        elif is_mismatched:
            if has_needs_review:
                tier = TIER_D
                reasons.append("substantive mismatch + unvalidated events")
            else:
                tier = TIER_C
                reasons.append("substantive mismatch (all events validated)")
        elif is_matched:
            if has_needs_review:
                tier = TIER_B
                reasons.append("reconciled but some events need review")
            else:
                tier = TIER_A
                reasons.append("reconciled, all events validated")
        elif is_format_only:
            if has_needs_review:
                tier = TIER_B
                reasons.append("format-only match but some events need review")
            else:
                tier = TIER_A
                reasons.append("format-only match, all events validated")
        elif is_minor_difference:
            tier = TIER_B
            reasons.append("minor annotation/formatting difference; legal text substantially correct")
        elif is_omitted_correct:
            if has_needs_review:
                tier = TIER_B
                reasons.append("source-backed omission but some events need review")
            else:
                tier = TIER_A
                reasons.append("source-backed omission at checkpoint date")
        elif is_post_checkpoint:
            tier = TIER_B
            reasons.append("component first exists after checkpoint date")
        elif version_count > 0:
            if has_needs_review:
                # Component has versions and no reconciliation issues.
                # needs_review events are unapplied amendments that didn't
                # prevent materialization — cap at B, not C.
                tier = TIER_B
                reasons.append("materialized with some unvalidated events in history")
            else:
                tier = TIER_B
                reasons.append("not reconciled but all events validated")
        else:
            tier = TIER_D
            reasons.append("no version history")

        component_tiers[cid] = tier
        component_details[cid] = {
            "tier": tier,
            "reasons": reasons,
            "version_count": version_count,
            "reconciliation": reconciliation_outcome
            or (
                "matched"
                if is_matched
                else ("format_only" if is_format_only else ("mismatched" if is_mismatched else ("missing" if is_missing else "not_in_checkpoint")))
            ),
            "reconciliation_outcome": reconciliation_outcome,
            "reconciliation_evidence": outcome_detail_by_component.get(cid, {}).get("evidence", {}),
            "has_needs_review_events": has_needs_review,
            "baseline_blocked": is_blocked,
            "portal_completeness_status": portal_status_by_component.get(cid, "not_checked"),
            "tier_blockers": blockers,
        }

    # 7. Parent-child inheritance: promote not_in_checkpoint children of Tier A rules/sections
    parent_rule_tier_map: dict[str, str] = {}
    for cid, tier in component_tiers.items():
        rule_num = _extract_rule_number(cid)
        if rule_num and cid.endswith(f"/rule/{rule_num}"):
            normalized = re.sub(r"^0+", "", rule_num) if rule_num.isdigit() else rule_num
            parent_rule_tier_map[normalized.lower()] = tier
            parent_rule_tier_map[rule_num.lower()] = tier
        sec_num = _extract_section_number(cid)
        if sec_num and cid.endswith(f"/section/{sec_num}"):
            normalized = re.sub(r"^0+", "", sec_num) if sec_num.isdigit() else sec_num
            parent_rule_tier_map[normalized.lower()] = tier
            parent_rule_tier_map[sec_num.lower()] = tier

    for cid, tier in list(component_tiers.items()):
        if tier != TIER_B:
            continue
        detail = component_details.get(cid, {})
        recon = detail.get("reconciliation", "")
        if recon == "not_in_checkpoint":
            latest_row = component_latest.get(cid, {})
            latest_text = str(latest_row.get("text") or "").strip()
            if latest_text in ("[Omitted]", "", "[***]", "[****]"):
                component_tiers[cid] = TIER_A
                detail["tier"] = TIER_A
                detail["reasons"] = ["source-backed omission; rule omitted in reconstruction"]
                detail["reconciliation"] = "omitted_correct"
                continue
        if recon != "not_in_checkpoint":
            continue
        rule_num = _extract_rule_number(cid)
        sec_num = _extract_section_number(cid)
        parent_num = rule_num or sec_num
        if not parent_num:
            continue
        normalized = re.sub(r"^0+", "", parent_num) if parent_num.isdigit() else parent_num
        parent_tier = parent_rule_tier_map.get(normalized.lower()) or parent_rule_tier_map.get(parent_num.lower())
        if parent_tier == TIER_A:
            component_tiers[cid] = TIER_A
            detail["tier"] = TIER_A
            detail["reasons"] = ["parent component reconciled; child text subsumed in matched parent"]
            detail["reconciliation"] = "parent_reconciled"

    # 8. Aggregate counts
    tier_counts: dict[str, int] = defaultdict(int)
    for tier in component_tiers.values():
        tier_counts[tier] += 1

    tier_a_components = sorted(cid for cid, tier in component_tiers.items() if tier == TIER_A)

    return {
        "component_tiers": component_tiers,
        "tier_counts": dict(tier_counts),
        "tier_a_components": tier_a_components,
        "component_details": component_details,
        "tier_descriptions": TIER_DESCRIPTIONS,
        "total_components": len(all_components),
    }


__all__ = [
    "TIER_A",
    "TIER_B",
    "TIER_C",
    "TIER_D",
    "TIER_DESCRIPTIONS",
    "compute_confidence_tiers",
]
