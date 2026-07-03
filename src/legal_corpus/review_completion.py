"""Classify amendment-event review items into terminal audit states."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REVIEW_COMPLETION_VERSION = "review-completion-v1"

TERMINAL_STATES = {
    "materialized",
    "forms_materialized",
    "forms_lane_resolved",
    "commencement_blocked",
    "corrigendum_rescind_deferred",
    "notification_rescinded",
    "covered_by_source_backed_event",
    "baseline_blocked",
    "llm_extraction_required",
    "parser_support_required",
    "rejected_non_rules_text",
    "requires_legal_review",
}

RULES_WORK = "/in/union/rules/cgst-rules-2017"
INITIAL_RULES_PUBLICATION = "/in/union/notifications/cbic/central-tax/2017/10-2017"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date_key(event: dict[str, Any]) -> str:
    legal_time = event.get("legal_time") or {}
    source = event.get("source") or {}
    return legal_time.get("applicability_start") or legal_time.get("commencement_date") or source.get("publication_date") or ""


def _norm(value: str) -> str:
    return re.sub(r"\W+", " ", value or "").strip().lower()


def _source_span_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    source_document = str(row.get("source_document_id") or (row.get("source") or {}).get("document_id") or "")
    span = row.get("source_span") or (row.get("evidence") or {}).get("source_span") or {}
    text_hash = str(span.get("text_hash") or "")
    if not source_document or not text_hash:
        return None
    return source_document, str(span.get("start") or ""), text_hash


def _span_overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_span = left.get("source_span") or (left.get("evidence") or {}).get("source_span") or {}
    right_span = right.get("source_span") or (right.get("evidence") or {}).get("source_span") or {}
    try:
        left_start = int(left_span.get("start"))
        left_end = int(left_span.get("end"))
        right_start = int(right_span.get("start"))
        right_end = int(right_span.get("end"))
    except (TypeError, ValueError):
        return 0.0
    if left_end <= left_start or right_end <= right_start:
        return 0.0
    overlap = max(0, min(left_end, right_end) - max(left_start, right_start))
    return overlap / max(1, min(left_end - left_start, right_end - right_start))


def _event_digest(event: dict[str, Any]) -> str:
    source = event.get("source") or {}
    span = (event.get("evidence") or {}).get("source_span") or {}
    seed = "|".join(
        [
            str(event.get("event_id") or ""),
            str(source.get("document_id") or ""),
            str(span.get("start") or ""),
            str(span.get("end") or ""),
            str(span.get("text_hash") or ""),
            str(event.get("operation") or ""),
            str((event.get("target") or {}).get("component_id") or ""),
            _date_key(event),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _decision_ids(*payloads: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for payload in payloads:
        for row in payload.get("decisions") or []:
            if row.get("decision") == "approved" and row.get("event_id"):
                ids.add(str(row["event_id"]))
    return ids


def _triage_by_event(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("event_id")): row
        for row in payload.get("items") or []
        if row.get("event_id")
    }


def _applied_event_ids(manifest: dict[str, Any]) -> set[str]:
    return {
        str(row.get("event_id"))
        for row in manifest.get("applied_events") or []
        if row.get("event_id")
    }


def _coverage_event_ids(*payloads: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for payload in payloads:
        for row in payload.get("gaps") or []:
            if row.get("event_id"):
                ids.add(str(row["event_id"]))
    return ids


def _coverage_reason_index(*payloads: dict[str, Any]) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = defaultdict(list)
    for payload in payloads:
        for row in payload.get("gaps") or []:
            event_id = str(row.get("event_id") or "")
            if event_id:
                reasons[event_id].append(str(row.get("skip_reason") or ""))
    return reasons


def _reconciliation_event_index(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload.get("priority_review_queue") or []:
        summary = {
            "component_id": row.get("component_id"),
            "reason": row.get("reason"),
            "blocker": row.get("blocker"),
            "recommended_action": row.get("recommended_action"),
        }
        for event_id in row.get("related_event_ids") or []:
            index[str(event_id)].append(summary)
    return index


def _text_for_event(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    return " ".join(
        str(value or "")
        for value in [
            event.get("operation"),
            (event.get("evidence") or {}).get("excerpt"),
            (event.get("target") or {}).get("anchor_text"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )


def _is_form_or_table(event: dict[str, Any], triage: dict[str, Any] | None) -> bool:
    target = event.get("target") or {}
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    text = _text_for_event(event)
    if triage and triage.get("triage_class") == "forms_lane":
        return True
    return (
        str(target.get("component_id") or "").startswith("/in/union/forms/")
        or "unsupported_form_or_table_mutation" in reasons
        or bool(re.search(r"\b(form|table|annexure|schedule)\b", text, flags=re.IGNORECASE))
        or bool(re.search(r"\bStatement\s*-?\s*\d+[A-Z]?\b", text))
        or bool(re.search(r"\bDECLARATION\s*\[\s*rule\b", text))
        or bool(re.search(r"\bserial\s+number\s+\d+\b", text, flags=re.IGNORECASE))
        or bool(re.search(r"\be-?commerce\s+operators?\b", text, flags=re.IGNORECASE))
    )


def _is_non_rules_text(event: dict[str, Any], triage: dict[str, Any] | None) -> bool:
    target = event.get("target") or {}
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    source_doc = str((event.get("source") or {}).get("document_id") or "")
    text = re.sub(r"\s+", " ", _text_for_event(event)).strip()
    excerpt = re.sub(r"\s+", " ", str((event.get("evidence") or {}).get("excerpt") or "")).strip()
    if triage and triage.get("triage_class") in {"notification_meta_only", "auto_reject_candidate"}:
        return True
    if source_doc == INITIAL_RULES_PUBLICATION:
        return True
    if re.search(r"\bTo be published in the Gazette of India\b.*\bGovernment of India\b", text, flags=re.IGNORECASE):
        return True
    if re.search(
        r"^\s*(?:\([a-zivxlcdm]+\)\s*)?in\s+rule\s+\d+[A-Z]?"
        r"(?:\s*,?\s*with\s+effect\s+from\s+.*?)?\s*[,–-]*\s*$",
        excerpt,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"^\s*(?:\([a-zivxlcdm]+\)\s*)?with\s+effect\s+from\s+.*?,\s*in\s+rule\s+\d+[A-Z]?\s*[,–-]*\s*$",
        excerpt,
        flags=re.IGNORECASE,
    ):
        return True
    return (
        "target_component_outside_work" in reasons
        or "document_scope_target_not_materializable" in reasons
        or target.get("work_id") not in {None, "", RULES_WORK}
    )


def _is_corrigendum_or_rescind(event: dict[str, Any], triage: dict[str, Any] | None) -> bool:
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    if triage and triage.get("triage_class") == "corrigendum_or_rescind":
        return True
    return event.get("operation") in {"CORRIGENDUM", "RESCIND", "SUPERSEDE"} or "notification_level_status_change" in reasons


def _is_commencement_blocked(
    event: dict[str, Any],
    triage: dict[str, Any] | None,
    reconciliation_rows: list[dict[str, Any]],
) -> bool:
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    if triage and triage.get("triage_class") == "commencement_only":
        return True
    if event.get("operation") == "COMMENCE" or "date_not_resolved" in reasons:
        return True
    return any(row.get("blocker") == "unresolved_commencement" for row in reconciliation_rows)


def _operation_family(operation: str) -> str:
    if operation in {"INSERT_CHILD", "INSERT_SIBLING"}:
        return "INSERT"
    if operation in {"SPLICE", "SUBSTITUTE"}:
        return "TEXT_EDIT"
    return operation or ""


def _find_covering_event(event: dict[str, Any], applied_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    source_doc = str((event.get("source") or {}).get("document_id") or "")
    target_id = str((event.get("target") or {}).get("component_id") or "")
    date_value = _date_key(event)
    event_excerpt = _norm((event.get("evidence") or {}).get("excerpt") or "")
    event_family = _operation_family(str(event.get("operation") or ""))
    for applied in applied_events:
        if str((applied.get("source") or {}).get("document_id") or "") != source_doc:
            continue
        if str((applied.get("target") or {}).get("component_id") or "") != target_id:
            continue
        if _date_key(applied) != date_value:
            continue
        applied_family = _operation_family(str(applied.get("operation") or ""))
        if event_family and applied_family and event_family not in {applied_family, "UNKNOWN"}:
            continue
        overlap = _span_overlap_ratio(event, applied)
        applied_excerpt = _norm((applied.get("evidence") or {}).get("excerpt") or "")
        excerpt_match = bool(
            event_excerpt
            and applied_excerpt
            and min(len(event_excerpt), len(applied_excerpt)) >= 80
            and (event_excerpt in applied_excerpt or applied_excerpt in event_excerpt)
        )
        if overlap >= 0.8 or excerpt_match or _source_span_key(event) == _source_span_key(applied):
            return applied
    return None


def _coverage_impact(terminal_state: str) -> str:
    if terminal_state == "materialized":
        return "complete"
    if terminal_state in {"forms_lane_resolved", "rejected_non_rules_text", "covered_by_source_backed_event", "notification_rescinded", "forms_materialized"}:
        return "not_rules_text_gap"
    if terminal_state in {
        "baseline_blocked",
        "commencement_blocked",
        "corrigendum_rescind_deferred",
        "notification_rescinded",
        "llm_extraction_required",
        "partial_materialized",
        "parser_support_required",
        "requires_legal_review",
    }:
        return "incomplete"
    return "incomplete"


def _classify_event(
    event: dict[str, Any],
    *,
    triage: dict[str, Any] | None,
    applied_ids: set[str],
    applied_events: list[dict[str, Any]],
    approved_ids: set[str],
    rules_gap_ids: set[str],
    rules_gap_reasons: dict[str, list[str]],
    form_gap_ids: set[str],
    forms_applied_ids: set[str] | None = None,
    reconciliation_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    event_id = str(event.get("event_id") or "")
    covered_by = None
    basis = "terminal_state_rules_v1"
    if event_id in applied_ids and any(reason.startswith("partial_apply:") for reason in rules_gap_reasons.get(event_id, [])):
        terminal_state = "partial_materialized"
        rationale = "Event was applied as a first-class component version, but parent snapshot coverage remains incomplete."
    elif event_id in applied_ids:
        terminal_state = "materialized"
        rationale = "Event was applied by the rules materializer."
    elif event.get("status") == "validated" and event_id not in rules_gap_ids:
        terminal_state = "materialized"
        rationale = "Event is validated and does not appear in current rules coverage gaps."
    elif forms_applied_ids and event_id in forms_applied_ids:
        terminal_state = "forms_materialized"
        rationale = "Event was applied by the forms materializer (form version boundary recorded)."
    else:
        covering_event = _find_covering_event(event, applied_events)
        if covering_event:
            covered_by = covering_event.get("event_id")
            terminal_state = "covered_by_source_backed_event"
            rationale = "A source-backed applied event already covers the same source span, date, and target component."
        elif any(reason.startswith("baseline_component_blocked") for reason in rules_gap_reasons.get(event_id, [])):
            terminal_state = "baseline_blocked"
            rationale = "Event targets a component whose 2017 baseline text is flagged as contaminated or unsafe."
        elif any(reason == "same_effective_date_conflict" for reason in rules_gap_reasons.get(event_id, [])):
            terminal_state = "parser_support_required"
            rationale = "Event conflicts with another event on the same effective date; deterministic conflict resolution needed."
        elif any(
            reason.startswith("apply_failed: Anchor component missing")
            or reason.startswith("apply_failed: Parent component missing")
            or reason.startswith("apply_failed: Target component missing")
            or reason.startswith("apply_failed: Partial omission text not uniquely found")
            or reason.startswith("apply_failed: Anchor text not uniquely found")
            or reason.startswith("apply_failed: Anchor not found")
            or reason.startswith("apply_failed: Substitute text not uniquely found")
            or reason.startswith("apply_failed: Substitution text not found")
            or reason.startswith("apply_failed: Subrule span not uniquely found")
            for reason in rules_gap_reasons.get(event_id, [])
        ):
            terminal_state = "parser_support_required"
            rationale = "Event is validated but current parser/resolver/materializer support is insufficient for safe replay."
        elif _is_form_or_table(event, triage):
            terminal_state = "forms_lane_resolved"
            if event_id in form_gap_ids:
                rationale = "Event is routed to the forms lane and remains visible in forms coverage gaps."
            else:
                rationale = "Event is form/table/schedule content and is outside the Rules text lane."
        elif _is_commencement_blocked(event, triage, reconciliation_rows):
            terminal_state = "commencement_blocked"
            rationale = "Event depends on unresolved commencement/effective-date mapping."
        elif any(reason == "notification_rescinded" for reason in rules_gap_reasons.get(event_id, [])):
            terminal_state = "notification_rescinded"
            rationale = "Event originates from a notification that has been rescinded/superseded."
        elif any(reason == "corrigendum_targets_notification" for reason in rules_gap_reasons.get(event_id, [])):
            terminal_state = "corrigendum_rescind_deferred"
            rationale = "Corrigendum corrects a notification (not rule text directly); deferred to notification-correction lane."
        elif any(reason == "rescind_notification_processed" for reason in rules_gap_reasons.get(event_id, [])):
            terminal_state = "corrigendum_rescind_deferred"
            rationale = "Rescission/supersession notification has been processed; affected events flagged."
        elif any(reason == "commence_no_text_change" for reason in rules_gap_reasons.get(event_id, [])):
            terminal_state = "commencement_blocked"
            rationale = "Commencement notification processed; no direct rule text change."
        elif _is_corrigendum_or_rescind(event, triage):
            terminal_state = "corrigendum_rescind_deferred"
            rationale = "Event needs the specialized corrigendum/rescind lane before affecting text."
        elif _is_non_rules_text(event, triage):
            terminal_state = "rejected_non_rules_text"
            rationale = "Event is outside the materializable CGST Rules text lane for v1."
        elif triage and (
            triage.get("triage_class") == "needs_parser_support"
            or (
                triage.get("triage_class") == "likely_materializable"
                and event_id in rules_gap_ids
            )
        ):
            terminal_state = "parser_support_required"
            rationale = "Event is a real Rules text amendment, but current parser/resolver/materializer support is insufficient."
        elif (
            triage
            and triage.get("recommended_action") == "llm_extract_or_human_review"
            or (
                event.get("operation") == "UNKNOWN"
                and ((event.get("target") or {}).get("component_id") in {None, "", RULES_WORK})
                and "unsupported_materializer_operation" in ((event.get("review") or {}).get("review_reasons") or [])
            )
        ):
            terminal_state = "llm_extraction_required"
            rationale = "Event still needs structured extraction before a legal-review decision can be made."
        elif event_id in approved_ids:
            terminal_state = "requires_legal_review"
            rationale = "Event has an approval decision but is not materialized in the current rules output."
        else:
            terminal_state = "requires_legal_review"
            rationale = "No deterministic terminal materialization or non-rules classification resolved this event."

    return {
        "decision": "terminal_review_state",
        "event_id": event_id,
        "event_digest": _event_digest(event),
        "terminal_state": terminal_state,
        "coverage_impact": _coverage_impact(terminal_state),
        "review_closed": terminal_state != "requires_legal_review",
        "requires_human_legal_review": terminal_state == "requires_legal_review",
        "covered_by_event_id": covered_by,
        "basis": basis,
        "rationale": rationale,
        "status": event.get("status"),
        "operation": event.get("operation"),
        "date": _date_key(event),
        "target": event.get("target") or {},
        "source_document_id": (event.get("source") or {}).get("document_id"),
        "source_record_id": (event.get("source") or {}).get("record_id"),
        "source_span": (event.get("evidence") or {}).get("source_span") or {},
        "excerpt": (event.get("evidence") or {}).get("excerpt") or "",
        "review_reasons": (event.get("review") or {}).get("review_reasons") or [],
        "triage_class": (triage or {}).get("triage_class"),
        "triage_action": (triage or {}).get("recommended_action"),
        "reconciliation": reconciliation_rows,
    }


def complete_review(
    *,
    events_path: Path = Path("derived/version_history/amendment_events_reviewed.jsonl"),
    rules_manifest_path: Path = Path("derived/version_history/cgst-rules-2017/materialization_manifest.json"),
    rules_coverage_path: Path = Path("derived/version_history/cgst-rules-2017/coverage_gaps.json"),
    forms_manifest_path: Path = Path("derived/version_history/forms/materialization_manifest.json"),
    forms_coverage_path: Path = Path("derived/version_history/forms/coverage_gaps.json"),
    reconciliation_report_path: Path = Path("derived/version_history/cgst-rules-2017/reconciliation_report.json"),
    review_triage_path: Path = Path("derived/version_history/review_triage.json"),
    decision_paths: list[Path] | None = None,
    report_output: Path = Path("derived/version_history/review_completion_report.json"),
    decisions_output: Path = Path("derived/version_history/review_completion_decisions.json"),
) -> dict[str, Any]:
    events = _read_jsonl(events_path)
    rules_manifest = _read_json(rules_manifest_path)
    rules_coverage = _read_json(rules_coverage_path)
    forms_coverage = _read_json(forms_coverage_path)
    forms_manifest = _read_json(forms_manifest_path)
    reconciliation = _read_json(reconciliation_report_path)
    triage = _read_json(review_triage_path)
    decision_payloads = [_read_json(path) for path in decision_paths or []]

    triage_index = _triage_by_event(triage)
    applied_ids = _applied_event_ids(rules_manifest)
    forms_applied_ids: set[str] = set(forms_manifest.get("applied_event_ids") or [])
    event_by_id = {str(event.get("event_id") or ""): event for event in events}
    applied_events = [event_by_id[event_id] for event_id in applied_ids if event_id in event_by_id]
    approved_ids = _decision_ids(*decision_payloads)
    rules_gap_ids = _coverage_event_ids(rules_coverage)
    rules_gap_reasons = _coverage_reason_index(rules_coverage)
    form_gap_ids = _coverage_event_ids(forms_coverage)
    reconciliation_index = _reconciliation_event_index(reconciliation)

    decisions = [
        _classify_event(
            event,
            triage=triage_index.get(str(event.get("event_id") or "")),
            applied_ids=applied_ids,
            applied_events=applied_events,
            approved_ids=approved_ids,
            rules_gap_ids=rules_gap_ids,
            rules_gap_reasons=rules_gap_reasons,
            form_gap_ids=form_gap_ids,
            forms_applied_ids=forms_applied_ids,
            reconciliation_rows=reconciliation_index.get(str(event.get("event_id") or ""), []),
        )
        for event in events
    ]
    pending_decisions = decisions
    counts_by_state = Counter(row["terminal_state"] for row in pending_decisions)
    counts_by_impact = Counter(row["coverage_impact"] for row in pending_decisions)
    counts_by_triage = Counter(row.get("triage_class") or "untriaged" for row in pending_decisions)
    open_items = [row for row in pending_decisions if row["requires_human_legal_review"]]
    generated_at = _now()

    compact_decisions = {
        "version": REVIEW_COMPLETION_VERSION,
        "generated_at": generated_at,
        "events_path": str(events_path),
        "decision_count": len(pending_decisions),
        "open_count": len(open_items),
        "counts_by_terminal_state": dict(counts_by_state),
        "decisions": [
            {
                "event_id": row["event_id"],
                "terminal_state": row["terminal_state"],
                "coverage_impact": row["coverage_impact"],
                "review_closed": row["review_closed"],
                "covered_by_event_id": row["covered_by_event_id"],
                "rationale": row["rationale"],
            }
            for row in pending_decisions
        ],
    }
    report = {
        "ok": True,
        "version": REVIEW_COMPLETION_VERSION,
        "generated_at": generated_at,
        "inputs": {
            "events": str(events_path),
            "rules_manifest": str(rules_manifest_path),
            "rules_coverage": str(rules_coverage_path),
            "forms_coverage": str(forms_coverage_path),
            "reconciliation_report": str(reconciliation_report_path),
            "review_triage": str(review_triage_path),
            "decisions": [str(path) for path in decision_paths or []],
        },
        "event_count": len(events),
        "pending_event_count": len(pending_decisions),
        "closed_count": len(pending_decisions) - len(open_items),
        "open_count": len(open_items),
        "counts": {
            "by_terminal_state": dict(counts_by_state),
            "by_coverage_impact": dict(counts_by_impact),
            "by_triage_class": dict(counts_by_triage),
        },
        "open_items": open_items,
        "items": pending_decisions,
    }
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    decisions_output.parent.mkdir(parents=True, exist_ok=True)
    decisions_output.write_text(
        json.dumps(compact_decisions, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "version": REVIEW_COMPLETION_VERSION,
        "report_output": str(report_output),
        "decisions_output": str(decisions_output),
        "event_count": len(events),
        "pending_event_count": len(pending_decisions),
        "closed_count": len(pending_decisions) - len(open_items),
        "open_count": len(open_items),
        "counts_by_terminal_state": dict(counts_by_state),
        "counts_by_coverage_impact": dict(counts_by_impact),
    }


__all__ = ["REVIEW_COMPLETION_VERSION", "TERMINAL_STATES", "complete_review"]
