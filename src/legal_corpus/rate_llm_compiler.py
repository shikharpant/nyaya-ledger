"""LLM fallback compiler for unrecognized GST rate-amendment clauses.

The deterministic rate_schedule_compiler leaves some clauses as RATE_UNKNOWN.
This module sends each such clause to a local OpenAI-compatible LLM, asks it to
classify the amendment operation, and converts the response into
RateAmendmentEvent dicts that merge back into the main event stream.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


LLM_BASE_URL = "http://100.79.90.123:8000/v1"
LLM_MODEL = "mlx-community--VibeThinker-3B-8bit"
LLM_API_KEY = "omlx-your-secret-key"
LLM_TIMEOUT = 10
CACHE_PATH = "derived/version_history/rate-schedules/llm_events_cache.json"

_VALID_OPS = {
    "INSERT_ENTRIES",
    "OMIT_ENTRIES",
    "SUBSTITUTE_COLUMN",
    "SUBSTITUTE_WORDS",
    "OMIT_WORDS",
    "INSERT_WORDS",
    "SUBSTITUTE_ROW",
    "SKIP",
}

_PROMPT_TEMPLATE = """You are a legal text parser for Indian GST rate notifications. Classify the following amendment clause into one of these operations:
- INSERT_ENTRIES: Insert new entries after a S.No. (fields: after_sno, entries as list of {sno, tariff_item, description})
- OMIT_ENTRIES: Omit entries by S.No. (fields: sno_list)
- SUBSTITUTE_COLUMN: Replace column value for a S.No. (fields: sno, column, new_value)
- SUBSTITUTE_WORDS: Replace words in description (fields: sno, old_words, new_words)
- OMIT_WORDS: Remove words from description (fields: sno, words)
- INSERT_WORDS: Insert words in description (fields: sno, after_words, insert_words)
- SUBSTITUTE_ROW: Replace entire row (fields: sno, new_entries)
- SKIP: Not an amendment operation (definitions, explanations)

Return JSON only. Example:
{"operation": "OMIT_ENTRIES", "sno_list": ["219"]}

Clause text: {clause_text}
Target: notification {target_notification}, schedule {target_schedule}"""


def _normalize_quotes(text: str) -> str:
    return (
        (text or "")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _build_prompt(
    clause_text: str, target_notification: str, target_schedule: str
) -> str:
    return (
        _PROMPT_TEMPLATE
        .replace("{clause_text}", clause_text)
        .replace("{target_notification}", target_notification)
        .replace("{target_schedule}", target_schedule)
    )


def _first_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
        start = text.find("{", start + 1)
    return None


def _extract_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    embedded = _first_json_object(text)
    if embedded is not None:
        return embedded
    raise ValueError("No JSON object found in LLM response")


def _call_llm(prompt: str) -> str:
    payload = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a strict JSON API. Output exactly one compact JSON object and no prose.",
                },
                {"role": "user", "content": "/no_think " + prompt.lstrip()},
            ],
            "temperature": 0,
            "max_tokens": 2048,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        LLM_BASE_URL.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Authorization": "Bearer " + LLM_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"LLM HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    content = str((choices[0].get("message") or {}).get("content") or "")
    if not content.strip():
        raise RuntimeError("LLM returned empty content")
    return content


def classify_clause_with_llm(
    clause_text: str,
    target_notification: str,
    target_schedule: str,
    effective_date: str,
    source_notification: str,
) -> dict[str, Any]:
    """Classify a single amendment clause via the LLM endpoint.

    Returns a dict guaranteed to contain the keys: operation, sno,
    tariff_item, description, after_sno, column, old_words, new_words.
    Any additional operation-specific fields (entries, sno_list, new_value,
    words, after_words, insert_words, new_entries) are preserved verbatim.
    """
    clean_clause = _normalize_quotes(clause_text)
    prompt = _build_prompt(clean_clause, target_notification, target_schedule)
    content = _call_llm(prompt)
    resp = _extract_json(content)

    op = str(resp.get("operation") or "").upper().strip()
    normalized: dict[str, Any] = {
        "operation": op if op in _VALID_OPS else "UNKNOWN",
        "sno": resp.get("sno"),
        "tariff_item": resp.get("tariff_item"),
        "description": resp.get("description"),
        "after_sno": resp.get("after_sno"),
        "column": resp.get("column"),
        "old_words": resp.get("old_words"),
        "new_words": resp.get("new_words"),
    }
    for key, value in resp.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def _build_payload(op: str, resp: dict[str, Any]) -> dict[str, Any]:
    if op == "INSERT_ENTRIES":
        return {
            "after_sno": str(resp.get("after_sno") or ""),
            "entries": resp.get("entries") or [],
        }
    if op == "OMIT_ENTRIES":
        return {"sno_list": resp.get("sno_list") or []}
    if op == "SUBSTITUTE_COLUMN":
        try:
            col = int(resp.get("column"))
        except (TypeError, ValueError):
            col = 0
        return {
            "sno": str(resp.get("sno") or ""),
            "column": col,
            "new_value": str(resp.get("new_value") or ""),
        }
    if op == "SUBSTITUTE_WORDS":
        return {
            "sno": str(resp.get("sno") or ""),
            "old_words": str(resp.get("old_words") or ""),
            "new_words": str(resp.get("new_words") or ""),
        }
    if op == "OMIT_WORDS":
        return {
            "sno": str(resp.get("sno") or ""),
            "words": str(resp.get("words") or ""),
        }
    if op == "INSERT_WORDS":
        return {
            "sno": str(resp.get("sno") or ""),
            "after_words": str(resp.get("after_words") or ""),
            "insert_words": str(resp.get("insert_words") or ""),
        }
    if op == "SUBSTITUTE_ROW":
        return {
            "sno": str(resp.get("sno") or ""),
            "new_entries": resp.get("new_entries") or resp.get("entries") or [],
        }
    return {"raw": resp}


_INVALID_SNO_PATTERNS = re.compile(r'^[\(\)\s]+$|^[IVX]+$|^\(.*\)$')

_GENERIC_AFTER_WORDS = {
    "the comma", "after the words", "the words", "the following",
    "the figures", "the brackets", "", "the entry", "the letter",
}


def _validate_llm_payload(op: str, payload: dict[str, Any]) -> bool:
    """Return True if the payload fields are plausible, False if rejected."""
    if op == "SKIP":
        return True

    sno = str(payload.get("sno", "") or "").strip()
    if op in ("SUBSTITUTE_WORDS", "OMIT_WORDS", "INSERT_WORDS", "SUBSTITUTE_COLUMN", "SUBSTITUTE_ROW"):
        if not sno or _INVALID_SNO_PATTERNS.match(sno):
            return False

    if op == "SUBSTITUTE_COLUMN":
        col = payload.get("column", 0)
        try:
            col_int = int(col)
        except (TypeError, ValueError):
            col_int = 0
        if col_int not in (2, 3):
            return False
        new_val = str(payload.get("new_value", "") or "").strip().lower()
        if new_val in ("unknown", "none", "") or len(new_val) < 3:
            return False

    if op in ("SUBSTITUTE_WORDS",):
        old_words = str(payload.get("old_words", "") or "").strip()
        new_words = str(payload.get("new_words", "") or "").strip()
        if old_words.lower() in ("unknown", "none", "") or len(old_words) < 2:
            return False
        if new_words.lower() in ("unknown", "none", "") or len(new_words) < 2:
            return False

    if op == "INSERT_WORDS":
        after_words = str(payload.get("after_words", "") or "").strip().lower()
        insert_words = str(payload.get("insert_words", "") or "").strip().lower()
        if after_words in _GENERIC_AFTER_WORDS:
            return False
        if insert_words in ("unknown", "none", "") or len(insert_words) < 3:
            return False

    if op == "OMIT_WORDS":
        words = str(payload.get("words", "") or "").strip().lower()
        if words in ("unknown", "none", "") or len(words) < 2:
            return False

    return True


def _to_event_dict(
    orig: dict[str, Any], classification: dict[str, Any]
) -> dict[str, Any]:
    op = str(classification.get("operation") or "").upper().strip()
    raw_text = orig.get("payload", {}).get("raw_text", "")
    base = {
        "event_id": orig.get("event_id", ""),
        "target_notification": orig.get("target_notification", ""),
        "target_schedule": orig.get("target_schedule", ""),
        "effective_date": orig.get("effective_date", ""),
        "publication_date": orig.get("publication_date", ""),
        "source_notification": orig.get("source_notification", ""),
        "source_cbic_no": orig.get("source_cbic_no", ""),
        "clause_ref": orig.get("clause_ref", ""),
    }
    if op == "SKIP":
        return {
            **base,
            "operation": "RATE_SKIP",
            "payload": {"raw_text": raw_text},
            "status": "llm_skipped",
            "review_reasons": ["llm_skip"],
        }
    if op not in _VALID_OPS:
        return {
            **base,
            "operation": "RATE_UNKNOWN",
            "payload": {
                "raw_text": raw_text,
                "llm_error": classification.get("error", "unparseable_operation"),
            },
            "status": "llm_failed",
            "review_reasons": ["llm_classification_failed"],
        }
    payload = _build_payload(op, classification)
    if not _validate_llm_payload(op, payload):
        return {
            **base,
            "operation": "RATE_UNKNOWN",
            "payload": {"raw_text": raw_text, "llm_error": "payload_validation_failed"},
            "status": "llm_failed",
            "review_reasons": ["llm_payload_validation_failed"],
        }
    return {
        **base,
        "operation": "RATE_" + op,
        "payload": payload,
        "status": "llm_classified",
        "review_reasons": [],
    }


def _load_cache(path: str | Path) -> dict[str, Any]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(path: str | Path, cache: dict[str, Any]) -> None:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


def compile_unknown_events(
    events_jsonl_path: str | Path,
    output_path: str | Path,
) -> list[dict[str, Any]]:
    """Compile RATE_UNKNOWN events into LLM-classified event dicts.

    Caches every classification to CACHE_PATH so subsequent runs do not call
    the LLM again. Network/JSON failures are recorded as RATE_UNKNOWN events
    with status ``llm_failed`` rather than aborting the run.
    """
    cache = _load_cache(CACHE_PATH)
    events_jsonl_path = Path(events_jsonl_path)
    output_path = Path(output_path)

    unknown_events: list[dict[str, Any]] = []
    with open(events_jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            if evt.get("operation") == "RATE_UNKNOWN":
                unknown_events.append(evt)

    new_events: list[dict[str, Any]] = []
    for evt in unknown_events:
        event_id = evt.get("event_id", "")
        clause_text = evt.get("payload", {}).get("raw_text", "")

        if event_id in cache:
            classification = cache[event_id]
        else:
            try:
                classification = classify_clause_with_llm(
                    clause_text=clause_text,
                    target_notification=evt.get("target_notification", ""),
                    target_schedule=evt.get("target_schedule", ""),
                    effective_date=evt.get("effective_date", ""),
                    source_notification=evt.get("source_notification", ""),
                )
                op_check = str(classification.get("operation") or "").upper().strip()
                if op_check in _VALID_OPS and op_check != "SKIP":
                    payload_check = _build_payload(op_check, classification)
                    if not _validate_llm_payload(op_check, payload_check):
                        classification = classify_clause_with_llm(
                            clause_text="RETRY: The previous response had invalid fields. "
                            + clause_text,
                            target_notification=evt.get("target_notification", ""),
                            target_schedule=evt.get("target_schedule", ""),
                            effective_date=evt.get("effective_date", ""),
                            source_notification=evt.get("source_notification", ""),
                        )
            except Exception as exc:
                classification = {"operation": "ERROR", "error": str(exc)}
            cache[event_id] = classification
            _save_cache(CACHE_PATH, cache)

        new_event = _to_event_dict(evt, classification)
        if new_event is not None:
            new_events.append(new_event)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        for evt in new_events:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")

    return new_events


def merge_llm_events(
    main_events_path: str | Path,
    llm_events: list[dict[str, Any]],
) -> dict[str, int]:
    """Replace RATE_UNKNOWN events in the main file with LLM-classified ones.

    Matching is by ``event_id``. The merged result is written back to
    ``main_events_path``. Returns counts of total merged and replaced rows.
    """
    main_events_path = Path(main_events_path)
    llm_by_id = {evt["event_id"]: evt for evt in llm_events if "event_id" in evt}

    merged: list[dict[str, Any]] = []
    replaced = 0
    with open(main_events_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            eid = evt.get("event_id")
            if eid in llm_by_id:
                merged.append(llm_by_id[eid])
                replaced += 1
            else:
                merged.append(evt)

    with open(main_events_path, "w", encoding="utf-8") as fh:
        for evt in merged:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")

    return {"merged": len(merged), "replaced": replaced}


__all__ = [
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT",
    "CACHE_PATH",
    "classify_clause_with_llm",
    "compile_unknown_events",
    "merge_llm_events",
]


if __name__ == "__main__":
    import sys
    from collections import Counter

    events_jsonl = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "derived/version_history/rate-schedules/rate_amendment_events.jsonl"
    )
    out_jsonl = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "derived/version_history/rate-schedules/llm_events.jsonl"
    )

    results = compile_unknown_events(events_jsonl, out_jsonl)
    print(f"Classified {len(results)} UNKNOWN events -> {out_jsonl}")
    ops = Counter(e["operation"] for e in results)
    for op, cnt in ops.most_common():
        print(f"  {op}: {cnt}")
    statuses = Counter(e["status"] for e in results)
    print("Statuses:")
    for st, cnt in statuses.most_common():
        print(f"  {st}: {cnt}")
