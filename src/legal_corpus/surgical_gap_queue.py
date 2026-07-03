"""Action queue for remaining surgical Rules materializer gaps."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_FOCUS_RULES = ("36", "45", "46", "138")
SURGICAL_REASON_PREFIXES = ("apply_failed", "partial_apply", "same_effective_date_conflict")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _target_component(gap: dict[str, Any]) -> str:
    target = gap.get("target")
    if isinstance(target, dict):
        return str(target.get("component_id") or target.get("anchor_component_id") or "")
    return str(target or gap.get("component_id") or "")


def _rule_label(component_id: str) -> str:
    match = re.search(r"/rule/([^/]+)", component_id)
    return match.group(1).lower() if match else ""


def _reason_prefix(gap: dict[str, Any]) -> str:
    reason = str(gap.get("skip_reason") or gap.get("reason") or "")
    return reason.split(":", 1)[0]


def _lane(gap: dict[str, Any]) -> str:
    reason = str(gap.get("skip_reason") or gap.get("reason") or "").lower()
    review_reasons = {str(reason).lower() for reason in gap.get("review_reasons") or []}
    operation = str(gap.get("operation") or "").upper()
    if _reason_prefix(gap) == "event_status_not_validated":
        return "event_resolution"
    if "form" in reason or "statement" in reason:
        return "forms_lane"
    if "metadata_only" in review_reasons:
        return "metadata_only"
    if "same_effective_date_conflict" in reason:
        return "sequencing_chain"
    if "anchor" in reason or "not found" in reason:
        return "anchor_or_target_resolution"
    if _reason_prefix(gap) == "partial_apply":
        return "partial_materialization"
    if operation in {"SPLICE", "SUBSTITUTE", "OMIT", "INSERT_CHILD", "INSERT_SIBLING"}:
        return "materializer_support"
    return "other"


def _is_surgical(gap: dict[str, Any]) -> bool:
    prefix = _reason_prefix(gap)
    if prefix not in SURGICAL_REASON_PREFIXES:
        return False
    lane = _lane(gap)
    return lane not in {"event_resolution", "forms_lane", "metadata_only"}


def _priority(gap: dict[str, Any], focus_rules: set[str]) -> int:
    rule = _rule_label(_target_component(gap))
    lane = _lane(gap)
    score = 0
    if rule in focus_rules:
        score -= 100
    if lane == "anchor_or_target_resolution":
        score -= 20
    elif lane == "sequencing_chain":
        score -= 15
    elif lane == "partial_materialization":
        score -= 10
    return score


def _slim_gap(gap: dict[str, Any], *, focus_rules: set[str]) -> dict[str, Any]:
    component_id = _target_component(gap)
    source_span = gap.get("source_span") if isinstance(gap.get("source_span"), dict) else {}
    return {
        "event_id": gap.get("event_id", ""),
        "date": gap.get("date", ""),
        "operation": gap.get("operation", ""),
        "status": gap.get("status", ""),
        "target": component_id,
        "rule": _rule_label(component_id),
        "lane": _lane(gap),
        "skip_reason": gap.get("skip_reason") or gap.get("reason") or "",
        "review_reasons": gap.get("review_reasons", []),
        "source_document_id": gap.get("source_document_id", ""),
        "source_record_id": gap.get("source_record_id", ""),
        "source_span": source_span,
        "source_span_hash": source_span.get("text_hash") or source_span.get("sha256") or source_span.get("hash") or "",
        "excerpt": gap.get("excerpt", ""),
        "focus_rule": _rule_label(component_id) in focus_rules,
        "priority": _priority(gap, focus_rules),
    }


def build_surgical_gap_queue(
    *,
    coverage_gaps_path: Path = Path("derived/version_history/cgst-rules-2017/coverage_gaps.json"),
    output: Path | None = None,
    focus_rules: list[str] | tuple[str, ...] = DEFAULT_FOCUS_RULES,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build an actionable queue from materializer-facing Rules coverage gaps."""

    payload = _read_json(coverage_gaps_path)
    gaps = payload.get("gaps", payload if isinstance(payload, list) else [])
    if not isinstance(gaps, list):
        gaps = []

    focus = {str(rule).lower() for rule in focus_rules}
    surgical = [_slim_gap(gap, focus_rules=focus) for gap in gaps if isinstance(gap, dict) and _is_surgical(gap)]
    surgical.sort(key=lambda gap: (gap["priority"], gap.get("date") or "", gap.get("rule") or "", gap.get("event_id") or ""))
    if limit is not None:
        surgical = surgical[:limit]

    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lane_counts: Counter[str] = Counter()
    for item in surgical:
        by_rule[item.get("rule") or "unknown"].append(item)
        lane_counts[item["lane"]] += 1

    rules = {
        rule: {
            "gap_count": len(items),
            "lane_counts": dict(sorted(Counter(item["lane"] for item in items).items())),
            "focus_rule": rule in focus,
            "items": items,
        }
        for rule, items in sorted(by_rule.items(), key=lambda row: (-len(row[1]), row[0]))
    }
    skipped_counts = Counter(_reason_prefix(gap) for gap in gaps if isinstance(gap, dict) and not _is_surgical(gap))
    report = {
        "summary": {
            "coverage_gaps_path": str(coverage_gaps_path),
            "total_gap_count": len(gaps),
            "surgical_gap_count": len(surgical),
            "reported_rule_count": len(rules),
            "focus_rules": sorted(focus),
            "lane_counts": dict(sorted(lane_counts.items())),
            "non_surgical_reason_counts": dict(sorted(skipped_counts.items())),
        },
        "rules": rules,
        "queue": surgical,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return report


__all__ = ["DEFAULT_FOCUS_RULES", "build_surgical_gap_queue"]
