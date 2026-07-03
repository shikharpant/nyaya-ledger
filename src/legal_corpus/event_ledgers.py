"""Utilities for combining amendment event ledgers without hiding conflicts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .amendment_events import read_events, write_jsonl


def _date_key(event: dict[str, Any]) -> str:
    legal_time = event.get("legal_time", {})
    return legal_time.get("applicability_start") or legal_time.get("commencement_date") or ""


def _payload_hash(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event.get("payload", {}), sort_keys=True).encode("utf-8")).hexdigest()


def _event_quality_rank(event: dict[str, Any]) -> tuple[int, int, int, int]:
    status_rank = {"materialized": 3, "validated": 2, "candidate": 1, "needs_review": 0, "rejected": -1}
    validation = event.get("validation") or {}
    review = event.get("review") or {}
    source = event.get("source") or {}
    payload = event.get("payload") or {}
    return (
        status_rank.get(str(event.get("status") or ""), 0),
        1 if validation.get("materializable") else 0,
        1 if not review.get("required") else 0,
        1 if source.get("text_source") == "canonical_notification_xml" or payload.get("materializer_repair") else 0,
    )


def _clean_materializable(event: dict[str, Any]) -> bool:
    validation = event.get("validation") or {}
    review = event.get("review") or {}
    return bool(
        event.get("status") == "validated"
        and validation.get("materializable")
        and not review.get("required")
    )


def _normalize_doc_id(doc_id: str) -> str:
    return re.sub(r"-(central|union|integrated)-tax$", "", doc_id)


def flag_cross_source_conflicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        component_id = event.get("target", {}).get("component_id") or ""
        date_value = _date_key(event)
        if component_id and date_value:
            grouped.setdefault((component_id, date_value), []).append(event)

    conflicts: list[dict[str, Any]] = []
    for (component_id, date_value), slot in grouped.items():
        if len(slot) <= 1:
            continue
        clean = [event for event in slot if _clean_materializable(event)]
        if len(clean) == 1 and all(
            event is clean[0] or not (event.get("validation") or {}).get("materializable")
            for event in slot
        ):
            continue
        payload_hashes = {_payload_hash(event) for event in slot}
        source_docs = {_normalize_doc_id(event.get("source", {}).get("document_id", "")) for event in slot}
        if len(payload_hashes) <= 1 and len(source_docs) <= 1:
            continue
        conflicts.append(
            {
                "component_id": component_id,
                "date": date_value,
                "event_ids": sorted(str(event.get("event_id")) for event in slot),
                "source_document_ids": sorted(source_docs),
                "reason": "same_effective_date_conflict",
            }
        )
        for event in slot:
            review = event.setdefault("review", {})
            reasons = set(review.get("review_reasons", []))
            reasons.add("same_effective_date_conflict")
            review["review_reasons"] = sorted(reasons)
            review["required"] = True
            event["status"] = "needs_review"
            event.setdefault("validation", {})["materializable"] = False
    return conflicts


def merge_event_ledgers(
    *,
    inputs: list[Path],
    output: Path,
    review_output: Path | None = None,
) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    replaced_duplicate_ids: list[str] = []
    for path in inputs:
        for event in read_events(path):
            event_id = str(event.get("event_id") or "")
            if not event_id:
                continue
            if event_id in by_id:
                duplicate_ids.append(event_id)
                if _event_quality_rank(event) > _event_quality_rank(by_id[event_id]):
                    by_id[event_id] = event
                    replaced_duplicate_ids.append(event_id)
                continue
            by_id[event_id] = event

    events = list(by_id.values())
    conflicts = flag_cross_source_conflicts(events)
    events.sort(
        key=lambda event: (
            _date_key(event),
            event.get("source", {}).get("publication_date") or "",
            event.get("source", {}).get("document_id") or "",
            event.get("source", {}).get("record_id") or "",
            event.get("event_id") or "",
        )
    )
    write_jsonl(events, output)

    counts = Counter(event.get("status", "") for event in events)
    reasons = Counter(reason for event in events for reason in event.get("review", {}).get("review_reasons", []))
    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_files": [str(path) for path in inputs],
        "event_count": len(events),
        "duplicate_event_ids": sorted(set(duplicate_ids)),
        "replaced_duplicate_event_ids": sorted(set(replaced_duplicate_ids)),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "counts": {"by_status": dict(counts), "by_review_reason": dict(reasons)},
        "non_validated_events": [
            {
                "event_id": event.get("event_id"),
                "operation": event.get("operation"),
                "target": event.get("target", {}),
                "source_document_id": event.get("source", {}).get("document_id"),
                "source_span": event.get("evidence", {}).get("source_span", {}),
                "excerpt": event.get("evidence", {}).get("excerpt", ""),
                "parser_pattern": event.get("evidence", {}).get("parser_trace", {}).get("pattern_id"),
                "review_reasons": event.get("review", {}).get("review_reasons", []),
            }
            for event in events
            if event.get("status") != "validated"
        ],
    }
    if review_output:
        review_output.parent.mkdir(parents=True, exist_ok=True)
        review_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "events": len(events),
        "output": str(output),
        "review_output": str(review_output) if review_output else None,
        "duplicate_event_ids": sorted(set(duplicate_ids)),
        "replaced_duplicate_event_ids": sorted(set(replaced_duplicate_ids)),
        "conflict_count": len(conflicts),
    }


__all__ = ["flag_cross_source_conflicts", "merge_event_ledgers"]
