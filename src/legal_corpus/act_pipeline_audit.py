"""Diagnostics for the CGST Act version-history parallel track."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ACT_WORK = "/in/union/acts/cgst-act-2017"

_BASELINE_NOISE = [
    re.compile(r"[A-Z][a-z]+\|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    re.compile(r"\b\d+;#"),
    re.compile(r"\b\d{12,}\b"),
    re.compile(r"^(?:INDIRECT|DIRECT)\s+TAXES$", re.I | re.M),
    re.compile(r"\bFinance\s+Acts?\|", re.I),
]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _component_id(value: dict[str, Any]) -> str:
    return str((value.get("target") or {}).get("component_id") or value.get("component_id") or "")


def _short_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "operation": event.get("operation"),
        "status": event.get("status"),
        "component_id": _component_id(event),
        "review_reasons": (event.get("review") or {}).get("review_reasons", []),
        "skip_reason": event.get("skip_reason"),
        "excerpt": str((event.get("evidence") or {}).get("excerpt") or event.get("excerpt") or "")[:300],
    }


def _looks_metadata_only(event: dict[str, Any]) -> bool:
    excerpt = str((event.get("evidence") or {}).get("excerpt") or event.get("excerpt") or "")
    lowered = excerpt.lower()
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    if "missing_source_text" in reasons:
        return True
    if event.get("operation") != "UNKNOWN":
        return False
    if re.search(r"\bshall\s+be\s+(?:inserted|substituted|omitted)\b", lowered):
        return False
    if re.search(r"\b(?:in|after|before)\s+section\s+\d+[a-z]?\b", lowered):
        return False
    return any(
        marker in lowered
        for marker in [
            "short title",
            "commencement",
            "appointed",
            "come into force",
            "notification",
            "retrospective exemption",
        ]
    )


def _is_classified_metadata_only(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    return payload.get("metadata_only") is True or "metadata_only" in reasons


def _is_schedule_lane(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    return payload.get("schedule_lane_pending_baseline") is True or "schedule_lane_pending_baseline" in reasons


def _is_act_out_of_scope(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    return payload.get("act_out_of_scope") is True or "act_out_of_scope" in reasons


def _missing_component_candidate(event: dict[str, Any], known_components: set[str]) -> bool:
    component_id = _component_id(event)
    if not component_id.startswith(ACT_WORK + "/section/"):
        return False
    if component_id in known_components:
        return False
    excerpt = str((event.get("evidence") or {}).get("excerpt") or event.get("excerpt") or "")
    return bool(
        re.search(r"\b(?:after|before)\s+section\s+\d+[A-Z]?\b", excerpt, flags=re.I)
        or re.search(r"\binsertion\s+of\s+new\s+section\b", excerpt, flags=re.I)
        or re.search(r"\bthe\s+following\s+section\s+shall\s+be\s+inserted\b", excerpt, flags=re.I)
    )


def audit_act_pipeline(
    *,
    events_path: Path = Path("derived/version_history/cgst-act-2017/merged_amendment_events.jsonl"),
    coverage_gaps_path: Path = Path("derived/version_history/cgst-act-2017/coverage_gaps.json"),
    materialization_manifest_path: Path = Path("derived/version_history/cgst-act-2017/materialization_manifest.json"),
    baseline_components_path: Path = Path("derived/version_history/baselines/cgst-act-2017/2017-04-12/baseline_components.jsonl"),
    baseline_reconciliation_path: Path = Path("derived/version_history/baselines/cgst-act-2017/2017-04-12/baseline_reconciliation.json"),
    confidence_tiers_path: Path = Path("derived/version_history/cgst-act-2017/confidence_tiers.json"),
    output_path: Path | None = Path("derived/version_history/cgst-act-2017/act_pipeline_audit.json"),
    sample_limit: int = 20,
) -> dict[str, Any]:
    events = _read_jsonl(events_path)
    gaps = list((_read_json(coverage_gaps_path).get("gaps") or []))
    manifest = _read_json(materialization_manifest_path)
    baseline_rows = _read_jsonl(baseline_components_path)
    baseline_recon = _read_json(baseline_reconciliation_path)
    confidence = _read_json(confidence_tiers_path)

    known_components = {str(row.get("component_id") or "") for row in baseline_rows}
    node_versions_path = str(manifest.get("node_versions") or "")
    if node_versions_path:
        known_components.update(str(row.get("component_id") or "") for row in _read_jsonl(Path(node_versions_path)))

    blocked_baseline_rows = [row for row in baseline_rows if row.get("blocked")]
    noisy_baseline_rows = [
        row
        for row in baseline_rows
        if any(pattern.search(str(row.get("text") or "")) for pattern in _BASELINE_NOISE)
    ]
    metadata_only_classified = [event for event in events if _is_classified_metadata_only(event)]
    schedule_lane_events = [event for event in events if _is_schedule_lane(event)]
    act_out_of_scope_events = [event for event in events if _is_act_out_of_scope(event)]
    metadata_only_candidates = [
        event
        for event in events
        if not _is_classified_metadata_only(event)
        and not _is_schedule_lane(event)
        and not _is_act_out_of_scope(event)
        and _looks_metadata_only(event)
    ]
    missing_component = [event for event in events if _missing_component_candidate(event, known_components)]
    gap_missing_component = [
        gap
        for gap in gaps
        if "Target component missing" in str(gap.get("skip_reason") or "")
        or _missing_component_candidate(gap, known_components)
    ]
    act_details = {
        key: value
        for key, value in (confidence.get("component_details") or {}).items()
        if str(key).startswith(ACT_WORK + "/")
    }

    recommended_next_actions = [
        "Generate or refresh Act-specific confidence tiers if act_component_count is zero.",
        "Use missing_section_creation_audit candidates to add source-proven section/component creation repairs.",
        "Address baseline noise candidates before treating Act sections as citation-grade.",
    ]
    if metadata_only_candidates:
        recommended_next_actions.append("Review metadata_only_audit candidates for deterministic metadata_only classification.")
    if schedule_lane_events:
        recommended_next_actions.append("Create Act schedule baselines before materializing schedule_lane_audit pending events.")
    if act_out_of_scope_events:
        recommended_next_actions.append("Keep act_out_of_scope_audit rows outside the CGST Act text materialization lane.")

    summary = {
        "event_count": manifest.get("event_count", len(events)),
        "applied_count": manifest.get("applied_count"),
        "coverage_gap_count": manifest.get("coverage_gap_count", len(gaps)),
        "baseline_blocked_count": int(baseline_recon.get("blocked_count") or len(blocked_baseline_rows)),
        "baseline_noise_candidate_count": len(noisy_baseline_rows),
        "metadata_only_classified_count": len(metadata_only_classified),
        "metadata_only_candidate_count": len(metadata_only_candidates),
        "schedule_lane_pending_baseline_count": len(schedule_lane_events),
        "act_out_of_scope_count": len(act_out_of_scope_events),
        "missing_section_event_candidate_count": len(missing_component),
        "missing_section_gap_candidate_count": len(gap_missing_component),
        "confidence_act_component_count": len(act_details),
        "confidence_components_with_blockers": sum(1 for value in act_details.values() if value.get("tier_blockers")),
        "confidence_tier_counts": dict(Counter(str(value.get("tier") or "") for value in act_details.values())),
    }

    report = {
        "target_work": ACT_WORK,
        "summary": summary,
        "inputs": {
            "events": str(events_path),
            "coverage_gaps": str(coverage_gaps_path),
            "materialization_manifest": str(materialization_manifest_path),
            "baseline_components": str(baseline_components_path),
            "baseline_reconciliation": str(baseline_reconciliation_path),
            "confidence_tiers": str(confidence_tiers_path),
        },
        "materialization": {
            "event_count": manifest.get("event_count", len(events)),
            "applied_count": manifest.get("applied_count"),
            "coverage_gap_count": manifest.get("coverage_gap_count", len(gaps)),
            "gap_skip_reasons": dict(Counter(str(gap.get("skip_reason") or "") for gap in gaps)),
        },
        "baseline_contamination_audit": {
            "baseline_component_count": len(baseline_rows),
            "blocked_count": int(baseline_recon.get("blocked_count") or len(blocked_baseline_rows)),
            "blocked_components": baseline_recon.get("blocked_components") or [row.get("component_id") for row in blocked_baseline_rows],
            "noise_pattern_candidate_count": len(noisy_baseline_rows),
            "noise_pattern_candidates": [
                {"component_id": row.get("component_id"), "label": row.get("label"), "heading": row.get("heading")}
                for row in noisy_baseline_rows[:sample_limit]
            ],
        },
        "metadata_only_audit": {
            "classified_count": len(metadata_only_classified),
            "classified_events": [_short_event(event) for event in metadata_only_classified[:sample_limit]],
            "candidate_count": len(metadata_only_candidates),
            "candidates": [_short_event(event) for event in metadata_only_candidates[:sample_limit]],
        },
        "schedule_lane_audit": {
            "pending_baseline_count": len(schedule_lane_events),
            "pending_baseline_events": [_short_event(event) for event in schedule_lane_events[:sample_limit]],
        },
        "act_out_of_scope_audit": {
            "event_count": len(act_out_of_scope_events),
            "events": [_short_event(event) for event in act_out_of_scope_events[:sample_limit]],
        },
        "missing_section_creation_audit": {
            "event_candidate_count": len(missing_component),
            "gap_candidate_count": len(gap_missing_component),
            "event_candidates": [_short_event(event) for event in missing_component[:sample_limit]],
            "gap_candidates": [_short_event(gap) for gap in gap_missing_component[:sample_limit]],
        },
        "confidence_tier_integration": {
            "confidence_file_present": bool(confidence),
            "act_component_count": len(act_details),
            "tier_counts": dict(Counter(str(value.get("tier") or "") for value in act_details.values())),
            "components_with_blockers": sum(1 for value in act_details.values() if value.get("tier_blockers")),
        },
        "recommended_next_actions": recommended_next_actions,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


__all__ = ["audit_act_pipeline"]
