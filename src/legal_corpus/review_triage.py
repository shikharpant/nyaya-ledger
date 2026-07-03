"""LLM-assisted review triage for amendment-event review queues."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any

from .omlx_client import OmlxConfig, OmlxError, chat_json


TRIAGE_VERSION = "review-triage-v1"
TRIAGE_CLASSES = {
    "likely_materializable",
    "needs_parser_support",
    "forms_lane",
    "notification_meta_only",
    "corrigendum_or_rescind",
    "commencement_only",
    "duplicate_or_superseded",
    "human_review",
    "auto_reject_candidate",
}
LLM_CANDIDATE_CLASSES = {"human_review", "likely_materializable"}
FORM_RE = re.compile(r"\b(form|gst\s+[a-z]{2,5}-?\d+|table|annexure|schedule)\b", re.IGNORECASE)
RULE_AMEND_RE = re.compile(
    r"\b(rule|sub-rule|clause|proviso|explanation|words?|figures?|inserted|substituted|omitted|after|before)\b",
    re.IGNORECASE,
)
META_RE = re.compile(r"\b(notifying|seeks to amend|notification no|notification number|rules,\s*2017)\b", re.IGNORECASE)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _event_text(event: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in [
            event.get("operation"),
            event.get("evidence", {}).get("excerpt"),
            event.get("target", {}).get("anchor_text"),
            json.dumps(event.get("payload") or {}, ensure_ascii=False),
            " ".join(event.get("review", {}).get("review_reasons") or []),
        ]
    )


def _event_key(event: dict[str, Any]) -> str:
    source = event.get("source") or {}
    span = (event.get("evidence") or {}).get("source_span") or {}
    seed = "|".join(
        [
            str(source.get("document_id") or ""),
            str(span.get("start") or ""),
            str(span.get("end") or ""),
            str(span.get("text_hash") or ""),
            str(event.get("operation") or ""),
            str((event.get("target") or {}).get("component_id") or ""),
            json.dumps(event.get("payload") or {}, ensure_ascii=False, sort_keys=True),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _group_key(event: dict[str, Any], triage_class: str) -> str:
    target = event.get("target") or {}
    source = event.get("source") or {}
    seed = "|".join(
        [
            triage_class,
            str(source.get("document_id") or ""),
            str(target.get("component_id") or ""),
            str(event.get("operation") or ""),
            ",".join(sorted(event.get("review", {}).get("review_reasons") or [])),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _deterministic_triage(event: dict[str, Any]) -> dict[str, Any]:
    operation = str(event.get("operation") or "UNKNOWN")
    review_reasons = list((event.get("review") or {}).get("review_reasons") or [])
    text = _event_text(event)
    lower_reasons = set(review_reasons)

    if operation in {"CORRIGENDUM", "RESCIND", "SUPERSEDE"} or "notification_level_status_change" in lower_reasons:
        triage_class = "corrigendum_or_rescind"
        action = "group_for_specialized_review"
        confidence = 0.9
    elif (
        "unsupported_form_or_table_mutation" in lower_reasons
        or "/forms/" in str((event.get("target") or {}).get("component_id") or "")
    ):
        triage_class = "forms_lane"
        action = "move_to_forms_lane"
        confidence = 0.88
    elif operation == "UNKNOWN" and FORM_RE.search(text) and not RULE_AMEND_RE.search(text):
        triage_class = "forms_lane"
        action = "move_to_forms_lane"
        confidence = 0.8
    elif "date_not_resolved" in lower_reasons and operation == "COMMENCE":
        triage_class = "commencement_only"
        action = "review_commencement_mapping"
        confidence = 0.82
    elif operation == "UNKNOWN" and META_RE.search(text) and not RULE_AMEND_RE.search(text):
        triage_class = "notification_meta_only"
        action = "auto_reject_candidate"
        confidence = 0.84
    elif "target_component_outside_work" in lower_reasons:
        triage_class = "auto_reject_candidate"
        action = "exclude_from_rules_lane"
        confidence = 0.86
    elif operation in {"SUBSTITUTE", "INSERT_CHILD", "INSERT_SIBLING", "SPLICE", "OMIT"}:
        triage_class = "needs_parser_support"
        action = "improve_resolver_or_materializer"
        confidence = 0.72
    elif operation == "UNKNOWN" and RULE_AMEND_RE.search(text):
        triage_class = "human_review"
        action = "llm_extract_or_human_review"
        confidence = 0.62
    else:
        triage_class = "auto_reject_candidate"
        action = "low_value_review"
        confidence = 0.68

    return {
        "triage_class": triage_class,
        "recommended_action": action,
        "confidence": confidence,
        "basis": "deterministic",
        "rationale": _rationale(event, triage_class, review_reasons),
        "materialization_hint": None,
    }


def _rationale(event: dict[str, Any], triage_class: str, reasons: list[str]) -> str:
    operation = str(event.get("operation") or "UNKNOWN")
    if triage_class == "forms_lane":
        return "Form, table, schedule, or annexure content should be reviewed in the separate forms lane."
    if triage_class == "notification_meta_only":
        return "The excerpt appears to describe a notification rather than a provision-level rules mutation."
    if triage_class == "auto_reject_candidate":
        return "The event is outside the materializable CGST Rules text lane or has low legal-history value for v1."
    if triage_class == "needs_parser_support":
        return f"{operation} was recognized but blocked by validation/materializer reasons: {', '.join(reasons[:4])}."
    if triage_class == "corrigendum_or_rescind":
        return "Corrigendum/rescind/supersede events need a specialized lane before they can affect component text."
    if triage_class == "commencement_only":
        return "Commencement metadata needs mapping to a source Act/rule amendment rather than direct text materialization."
    return "Potential rules amendment remains ambiguous and should be prioritized for LLM extraction or human review."


def _llm_prompt(event: dict[str, Any], deterministic: dict[str, Any]) -> str:
    compact = {
        "event_id": event.get("event_id"),
        "operation": event.get("operation"),
        "status": event.get("status"),
        "review_reasons": event.get("review", {}).get("review_reasons") or [],
        "source": {
            "document_id": event.get("source", {}).get("document_id"),
            "instrument_number": event.get("source", {}).get("instrument_number"),
            "publication_date": event.get("source", {}).get("publication_date"),
        },
        "target": event.get("target") or {},
        "payload": event.get("payload") or {},
        "excerpt": (event.get("evidence", {}).get("excerpt") or "")[:1800],
        "deterministic_triage": deterministic,
    }
    return (
        "Classify this CGST Rules amendment review item to reduce human review load.\n"
        "Return exactly one JSON object with keys: triage_class, recommended_action, confidence, rationale, materialization_hint.\n"
        "Allowed triage_class values: likely_materializable, needs_parser_support, forms_lane, notification_meta_only, "
        "corrigendum_or_rescind, commencement_only, duplicate_or_superseded, human_review, auto_reject_candidate.\n"
        "recommended_action should be short snake_case. confidence must be 0..1.\n"
        "materialization_hint may be an object with operation, target_component_id, anchor_text, payload_summary, blocker, or null.\n"
        "Do not claim legal validation; only triage priority.\n\n"
        + json.dumps(compact, ensure_ascii=False, sort_keys=True)
    )


def _normalize_llm_response(raw: dict[str, Any]) -> dict[str, Any]:
    triage_class = str(raw.get("triage_class") or "human_review")
    if triage_class not in TRIAGE_CLASSES:
        triage_class = "human_review"
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    hint = raw.get("materialization_hint")
    if hint is not None and not isinstance(hint, dict):
        hint = {"summary": str(hint)}
    return {
        "triage_class": triage_class,
        "recommended_action": str(raw.get("recommended_action") or "human_review"),
        "confidence": confidence,
        "basis": "omlx",
        "rationale": str(raw.get("rationale") or "")[:1000],
        "materialization_hint": hint,
    }


def _read_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not path or not path.exists():
        return cache
    for row in _read_jsonl(path):
        key = str(row.get("event_key") or "")
        if key:
            cache[key] = row
    return cache


def _append_cache(path: Path | None, row: dict[str, Any], lock: Lock) -> None:
    if not path:
        return
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _should_call_llm(event: dict[str, Any], deterministic: dict[str, Any]) -> bool:
    if deterministic["triage_class"] not in LLM_CANDIDATE_CLASSES:
        return False
    return deterministic["confidence"] < 0.78


def triage_review_items(
    *,
    events_path: Path = Path("derived/version_history/amendment_events.jsonl"),
    output: Path = Path("derived/version_history/review_triage.json"),
    use_llm: bool = False,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key_env: str = "OMLX_API_KEY",
    llm_cache: Path | None = Path("derived/version_history/review_triage_llm_cache.jsonl"),
    llm_limit: int | None = None,
    llm_concurrency: int = 1,
) -> dict[str, Any]:
    events = [event for event in _read_jsonl(events_path) if event.get("status") == "needs_review"]
    cache = _read_cache(llm_cache)
    cache_lock = Lock()
    config = OmlxConfig.from_env(base_url=llm_base_url, model=llm_model, api_key_env=llm_api_key_env, timeout=90)

    triage_rows: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    stats = Counter()
    seen_keys: dict[str, str] = {}

    for event in events:
        event_key = _event_key(event)
        deterministic = _deterministic_triage(event)
        if event_key in seen_keys:
            deterministic = {
                **deterministic,
                "triage_class": "duplicate_or_superseded",
                "recommended_action": "review_group_once",
                "confidence": max(float(deterministic["confidence"]), 0.9),
                "rationale": f"Duplicate event shape already represented by {seen_keys[event_key]}.",
            }
        else:
            seen_keys[event_key] = str(event.get("event_id") or "")

        cached = cache.get(event_key)
        if cached:
            triage = {k: cached.get(k) for k in ["triage_class", "recommended_action", "confidence", "basis", "rationale", "materialization_hint"]}
            triage["basis"] = "omlx_cache"
            stats["llm_cache_hits"] += 1
        elif use_llm and _should_call_llm(event, deterministic) and (llm_limit is None or stats["llm_scheduled"] < llm_limit):
            pending.append((event, deterministic, event_key))
            triage = deterministic
            stats["llm_scheduled"] += 1
        else:
            triage = deterministic
            if use_llm and _should_call_llm(event, deterministic):
                stats["llm_limit_not_attempted"] += 1

        triage_rows.append(_row(event, event_key, triage))

    row_by_event_id = {row["event_id"]: row for row in triage_rows}

    def call(item: tuple[dict[str, Any], dict[str, Any], str]) -> dict[str, Any]:
        event, deterministic, event_key = item
        raw = chat_json(_llm_prompt(event, deterministic), config=config, max_tokens=700)
        triage = _normalize_llm_response(raw)
        cache_row = {"event_key": event_key, "event_id": event.get("event_id"), **triage}
        _append_cache(llm_cache, cache_row, cache_lock)
        return _row(event, event_key, triage)

    if pending:
        workers = max(1, int(llm_concurrency or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(call, item): item for item in pending}
            for future in as_completed(futures):
                event, deterministic, event_key = futures[future]
                try:
                    row = future.result()
                    stats["llm_attempted"] += 1
                except OmlxError as exc:
                    stats[getattr(exc, "reason", "llm_unavailable")] += 1
                    row = _row(event, event_key, {**deterministic, "llm_error": getattr(exc, "reason", "llm_unavailable")})
                row_by_event_id[str(event.get("event_id") or "")] = row

    triage_rows = [row_by_event_id[str(event.get("event_id") or "")] for event in events]
    groups = _groups(triage_rows)
    summary = {
        "ok": True,
        "triage_version": TRIAGE_VERSION,
        "events_path": str(events_path),
        "event_count": len(triage_rows),
        "group_count": len(groups),
        "counts": {
            "by_triage_class": dict(Counter(row["triage_class"] for row in triage_rows)),
            "by_recommended_action": dict(Counter(row["recommended_action"] for row in triage_rows)),
            "by_basis": dict(Counter(row["basis"] for row in triage_rows)),
        },
        "llm": {
            "enabled": use_llm,
            "base_url": config.base_url,
            "model": config.model,
            "stats": dict(stats),
        },
        "groups": groups,
        "items": triage_rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _row(event: dict[str, Any], event_key: str, triage: dict[str, Any]) -> dict[str, Any]:
    triage_class = str(triage.get("triage_class") or "human_review")
    if triage_class not in TRIAGE_CLASSES:
        triage_class = "human_review"
    return {
        "event_id": str(event.get("event_id") or ""),
        "event_key": event_key,
        "group_key": _group_key(event, triage_class),
        "triage_class": triage_class,
        "recommended_action": str(triage.get("recommended_action") or "human_review"),
        "confidence": triage.get("confidence", 0.5),
        "basis": str(triage.get("basis") or "deterministic"),
        "rationale": str(triage.get("rationale") or ""),
        "materialization_hint": triage.get("materialization_hint"),
        "operation": event.get("operation"),
        "review_reasons": event.get("review", {}).get("review_reasons") or [],
        "target_component_id": (event.get("target") or {}).get("component_id"),
        "source_document_id": (event.get("source") or {}).get("document_id"),
        "record_id": (event.get("source") or {}).get("record_id"),
        "excerpt": event.get("evidence", {}).get("excerpt"),
    }


def _groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["group_key"]].append(row)
    result = []
    for group_key, items in grouped.items():
        first = items[0]
        result.append(
            {
                "group_key": group_key,
                "triage_class": first["triage_class"],
                "recommended_action": first["recommended_action"],
                "count": len(items),
                "event_ids": [item["event_id"] for item in items],
                "operation": first.get("operation"),
                "target_component_id": first.get("target_component_id"),
                "source_document_id": first.get("source_document_id"),
                "review_reasons": sorted({reason for item in items for reason in item.get("review_reasons", [])}),
                "sample_excerpt": first.get("excerpt"),
                "basis": ",".join(sorted({item.get("basis", "") for item in items})),
            }
        )
    return sorted(result, key=lambda row: (-int(row["count"]), row["triage_class"], row["group_key"]))


__all__ = ["TRIAGE_CLASSES", "TRIAGE_VERSION", "triage_review_items"]
