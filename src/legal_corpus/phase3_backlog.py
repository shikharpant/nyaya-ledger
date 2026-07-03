"""Build a compact Phase 3 backlog from current version-history artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _sample(values: list[Any], limit: int) -> list[Any]:
    return values[: max(limit, 0)]


def _rules_items(rules_coverage: dict[str, Any], portal: dict[str, Any], sample_limit: int) -> list[dict[str, Any]]:
    gaps = rules_coverage.get("gaps") or rules_coverage.get("coverage_gaps") or []
    reason_counts: dict[str, int] = {}
    sample_by_reason: dict[str, list[str]] = {}
    for gap in gaps:
        reason = str(gap.get("skip_reason") or gap.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        sample_by_reason.setdefault(reason, [])
        if len(sample_by_reason[reason]) < sample_limit:
            sample_by_reason[reason].append(str(gap.get("event_id") or ""))

    items: list[dict[str, Any]] = []
    if gaps:
        items.append(
            {
                "id": "rules-explicit-gaps",
                "lane": "rules",
                "priority": "P1",
                "title": "Resolve remaining Rules coverage gaps",
                "count": len(gaps),
                "reason_counts": reason_counts,
                "sample_event_ids_by_reason": sample_by_reason,
                "next_action": "Promote validated rows or leave explicit gaps with deterministic diagnostics.",
            }
        )

    source_present = int(portal.get("source_present_unlinked_notification_count") or 0)
    linkage = portal.get("source_present_unlinked_linkage") or {}
    new_unlinked = int(linkage.get("new_unlinked_count") or 0)
    if new_unlinked:
        items.append(
            {
                "id": "portal-source-present-unlinked",
                "lane": "rules",
                "priority": "P1",
                "title": "Link portal-listed source notifications already present in the ledger",
                "count": new_unlinked,
                "next_action": "For each portal-listed notification, attach the matching event chain or classify it as non-material text.",
            }
        )
    elif source_present:
        items.append(
            {
                "id": "portal-source-present-unlinked",
                "lane": "rules",
                "priority": "P1",
                "title": "Portal unlinked notifications (all classified, non-gap)",
                "count": 0,
                "classified_count": source_present,
                "classification_counts": linkage.get("gap_classification_counts") or {},
                "next_action": "All portal unlinked rows are classified as contextual_reference or cross_rule_reference. No action needed.",
            }
        )
    return items


def _forms_items(forms_manifest: dict[str, Any], sample_limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    overroute_count = int(forms_manifest.get("forms_lane_overrouted_count") or 0)
    top_priority = forms_manifest.get("forms_lane_pending_baseline_top_priority") or {}
    for slug, row in top_priority.items():
        items.append(
            {
                "id": f"forms-baseline-{slug}",
                "lane": "forms",
                "priority": f"P{row.get('priority') or 5}",
                "title": f"Create structured baseline for {slug}",
                "count": row.get("count", 0),
                "baseline_status": row.get("baseline_status"),
                "sample_event_ids": _sample(row.get("sample_event_ids") or [], sample_limit),
                "next_action": "Add structured baseline components, then rerun forms materialization before promoting form amendments.",
            }
        )

    if overroute_count:
        true_pending = int(forms_manifest.get("forms_lane_true_pending_baseline_count") or 0)
        unclassified = int(forms_manifest.get("forms_lane_pending_baseline_unclassified_count") or 0)
        if true_pending == 0 and unclassified == 0:
            items.append(
                {
                    "id": "forms-lane-overrouted-rules",
                    "lane": "forms",
                    "priority": "P1",
                    "title": "Over-routed forms rows (all classified, non-gap)",
                    "count": 0,
                    "classified_count": overroute_count,
                    "next_action": "All over-routed rows are classified as table/formula references, baseline fragments, or rule over-routes. No action needed.",
                }
            )
        else:
            buckets = forms_manifest.get("forms_lane_pending_baseline_unclassified_by_bucket") or {}
            items.append(
                {
                    "id": "forms-lane-overrouted-rules",
                    "lane": "forms",
                    "priority": "P1",
                    "title": "Remove suspected Rules/table rows from the forms pending-baseline lane",
                    "count": overroute_count,
                    "bucket_counts": {
                        key: value.get("count", 0)
                        for key, value in buckets.items()
                        if key in {"suspected_rule_text_overrouted", "suspected_table_or_formula_overrouted"}
                        and isinstance(value, dict)
                    },
                    "sample_event_ids": _sample(forms_manifest.get("forms_lane_overrouted_event_ids") or [], sample_limit),
                    "next_action": "Reclassify these rows into Rules materialization or explicit table/formula lanes before adding more form baselines.",
                }
            )

    unclassified = int(forms_manifest.get("forms_lane_pending_baseline_unclassified_count") or 0)
    if unclassified:
        buckets = forms_manifest.get("forms_lane_pending_baseline_unclassified_by_bucket") or {}
        non_overroute_unclassified = (
            unclassified
            - int((buckets.get("suspected_rule_text_overrouted") or {}).get("count") or 0)
            - int((buckets.get("suspected_table_or_formula_overrouted") or {}).get("count") or 0)
        )
        items.append(
            {
                "id": "forms-unclassified-pending-baseline",
                "lane": "forms",
                "priority": "P2",
                "title": "Classify pending-baseline form rows with no detected form slug",
                "count": non_overroute_unclassified,
                "bucket_counts": {
                    key: value.get("count", 0)
                    for key, value in buckets.items()
                    if key not in {"suspected_rule_text_overrouted", "suspected_table_or_formula_overrouted"}
                    if isinstance(value, dict)
                },
                "sample_event_ids": _sample(
                    forms_manifest.get("forms_lane_pending_baseline_non_overroute_unclassified_event_ids")
                    or forms_manifest.get("forms_lane_pending_baseline_unclassified_event_ids")
                    or [],
                    sample_limit,
                ),
                "next_action": "Separate true form-slug misses from suspected over-routed Rules/table rows, then add baselines only for proven forms.",
            }
        )
    return items


def _act_items(act_audit: dict[str, Any]) -> list[dict[str, Any]]:
    summary = act_audit.get("summary") or act_audit.get("stats") or {}
    if not act_audit or not summary:
        return [
            {
                "id": "act-audit-refresh",
                "lane": "act",
                "priority": "P2",
                "title": "Refresh non-blocking CGST Act audit",
                "count": None,
                "next_action": "Run `python3 main.py version act-audit` and fold the report into this backlog.",
            }
        ]
    return [
        {
            "id": "act-pipeline-backlog",
            "lane": "act",
            "priority": "P2",
            "title": "Continue non-blocking CGST Act pipeline remediation",
            "count": summary.get("coverage_gap_count"),
            "summary": summary,
            "next_action": "Address baseline contamination, metadata-only rows, and source-proven missing section creation independently from Rules recovery.",
        }
    ]


def build_phase3_backlog(
    *,
    rules_coverage_path: Path,
    forms_manifest_path: Path,
    portal_completeness_path: Path,
    confidence_tiers_path: Path,
    act_audit_path: Path,
    output_path: Path,
    reconciliation_report_path: Path = Path("derived/version_history/cgst-rules-2017/reconciliation_report.json"),
    sample_limit: int = 10,
) -> dict[str, Any]:
    rules_coverage = _read_json(rules_coverage_path)
    forms_manifest = _read_json(forms_manifest_path)
    portal = _read_json(portal_completeness_path)
    confidence = _read_json(confidence_tiers_path)
    act_audit = _read_json(act_audit_path)
    reconciliation = _read_json(reconciliation_report_path)
    audit_rows = (reconciliation.get("unresolved_reconciliation_audit") or {}).get("rows") or []
    audit_by_component = {row.get("component_id"): row for row in audit_rows}
    unresolved_reconciliation = [
        {
            "component_id": cid,
            "tier": detail.get("tier"),
            "reconciliation_outcome": detail.get("reconciliation_outcome") or detail.get("reconciliation"),
            "audit_class": (audit_by_component.get(cid) or {}).get("audit_class"),
            "candidate_event_count": (audit_by_component.get(cid) or {}).get("candidate_event_count"),
            "candidate_event_ids": (audit_by_component.get(cid) or {}).get("candidate_event_ids") or [],
            "reasons": detail.get("reasons") or [],
        }
        for cid, detail in (confidence.get("component_details") or {}).items()
        if (detail.get("reconciliation_outcome") or detail.get("reconciliation"))
        in {"true_substantive_mismatch", "missing_reconstruction", "checkpoint_source_incomplete", "mismatched", "missing"}
    ]

    items = [
        *_rules_items(rules_coverage, portal, sample_limit),
        *_forms_items(forms_manifest, sample_limit),
        *_act_items(act_audit),
        {
            "id": "rules-unresolved-reconciliation",
            "lane": "rules",
            "priority": "P0",
            "title": "Resolve externally visible reconciliation blockers",
            "count": len(unresolved_reconciliation),
            "samples": unresolved_reconciliation[:sample_limit],
            "next_action": "Audit only true substantive mismatches, missing reconstructions, and checkpoint source incompleteness; resolved outcome classes are not actionable backlog.",
            "audit_class_counts": (reconciliation.get("unresolved_reconciliation_audit") or {}).get("audit_class_counts") or {},
        },
        {
            "id": "future-compiler-context-inheritance",
            "lane": "compiler",
            "priority": "P4",
            "title": "Monitor compiler context inheritance on future recompilations",
            "count": None,
            "next_action": "Regression coverage now proves child fragments inherit parent rule/subrule context; keep this item as a future recompilation guard.",
        },
    ]

    backlog = {
        "version": "phase3-backlog-v1",
        "inputs": {
            "rules_coverage": str(rules_coverage_path),
            "forms_manifest": str(forms_manifest_path),
            "portal_completeness": str(portal_completeness_path),
            "confidence_tiers": str(confidence_tiers_path),
            "reconciliation_report": str(reconciliation_report_path),
            "act_audit": str(act_audit_path),
        },
        "summary": {
            "rules_coverage_gap_count": rules_coverage.get("gap_count") or rules_coverage.get("coverage_gap_count"),
            "forms_pending_baseline_count": forms_manifest.get("forms_lane_pending_baseline_count"),
            "forms_true_pending_baseline_count": forms_manifest.get("forms_lane_true_pending_baseline_count"),
            "forms_lane_overrouted_count": forms_manifest.get("forms_lane_overrouted_count"),
            "forms_pending_baseline_unclassified_count": forms_manifest.get("forms_lane_pending_baseline_unclassified_count"),
            "forms_pending_baseline_unclassified_bucket_counts": {
                key: value.get("count", 0)
                for key, value in (forms_manifest.get("forms_lane_pending_baseline_unclassified_by_bucket") or {}).items()
                if isinstance(value, dict)
            },
            "portal_missing_source_notification_count": portal.get("missing_source_notification_count"),
            "portal_source_present_unlinked_notification_count": portal.get("source_present_unlinked_notification_count"),
            "confidence_tier_counts": confidence.get("tier_counts"),
            "unresolved_reconciliation_count": len(unresolved_reconciliation),
            "act_coverage_gap_count": (act_audit.get("summary") or act_audit.get("stats") or {}).get("coverage_gap_count"),
            "act_confidence_tier_counts": (act_audit.get("summary") or act_audit.get("stats") or {}).get("confidence_tier_counts"),
            "item_count": len(items),
        },
        "items": items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(backlog, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return backlog


__all__ = ["build_phase3_backlog"]
