#!/usr/bin/env python3
"""LLM fallback compiler pass for the 62 service-rate UNKNOWN events.

Targets ``target_notification == "11/2017-ct-rate"`` clauses left as
``RATE_UNKNOWN`` by the deterministic compiler, and asks a local
OpenAI-compatible LLM (the Grug-35B model) to classify each clause into a
structured amendment event.

Reuses the prompt/parsing/validation helpers from
``src.legal_corpus.rate_llm_compiler`` so the output schema is identical to
that of the main pipeline, but overrides the model name, generation params
(temperature 0, max_tokens 500) and uses a dedicated cache/output pair so the
main ``rate_amendment_events.jsonl`` and ``llm_events.*`` artefacts are never
touched.
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.legal_corpus.rate_llm_compiler import (  # noqa: E402
    _build_payload,
    _build_prompt,
    _extract_json,
    _normalize_quotes,
    _to_event_dict,
    _VALID_OPS,
    _validate_llm_payload,
)

EVENTS_JSONL = PROJECT_ROOT / "derived/version_history/rate-schedules/rate_amendment_events.jsonl"
OUTPUT_JSONL = PROJECT_ROOT / "derived/version_history/rate-schedules/llm_svc_events.jsonl"
CACHE_PATH = PROJECT_ROOT / "derived/version_history/rate-schedules/llm_svc_cache.json"

TARGET_NOTIFICATION = "11/2017-ct-rate"

LLM_BASE_URL = "http://100.79.90.123:8000/v1"
LLM_MODEL = "kai-os--Grug-35B-A3B-6bit-mlx"
LLM_API_KEY = "omlx-your-secret-key"
LLM_TIMEOUT = 120
MAX_TOKENS = 500
TEMPERATURE = 0.0

SYSTEM_MSG = (
    "You are a strict JSON API. Output exactly one compact JSON object and no prose."
)


def _call_llm(prompt: str) -> dict:
    """Call the LLM endpoint and return the parsed JSON classification."""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": "/no_think " + prompt.lstrip()},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    resp = requests.post(
        LLM_BASE_URL.rstrip("/") + "/chat/completions",
        json=payload,
        headers={
            "Authorization": "Bearer " + LLM_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    content = str((choices[0].get("message") or {}).get("content") or "")
    if not content.strip():
        raise RuntimeError("LLM returned empty content")
    return _extract_json(content)


def _classify(evt: dict) -> dict:
    """Classify one clause, retrying once on payload-validation failure."""
    clause_text = _normalize_quotes(evt.get("payload", {}).get("raw_text", ""))
    classification = _call_llm(
        _build_prompt(
            clause_text,
            evt.get("target_notification", ""),
            evt.get("target_schedule", ""),
        )
    )
    op = str(classification.get("operation") or "").upper().strip()
    if op in _VALID_OPS and op != "SKIP":
        if not _validate_llm_payload(op, _build_payload(op, classification)):
            classification = _call_llm(
                _build_prompt(
                    "RETRY: The previous response had invalid fields. " + clause_text,
                    evt.get("target_notification", ""),
                    evt.get("target_schedule", ""),
                )
            )
    return classification


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


def main() -> int:
    unknown_events = []
    with open(EVENTS_JSONL, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            if (
                evt.get("operation") == "RATE_UNKNOWN"
                and evt.get("target_notification") == TARGET_NOTIFICATION
            ):
                unknown_events.append(evt)

    print(f"Loaded {len(unknown_events)} UNKNOWN events for {TARGET_NOTIFICATION}")
    cache = _load_cache()

    resolved_events = []
    failed = 0
    for idx, evt in enumerate(unknown_events, 1):
        event_id = evt.get("event_id", "")
        if event_id in cache:
            classification = cache[event_id]
            source = "cache"
        else:
            try:
                classification = _classify(evt)
                source = "llm"
            except Exception as exc:  # timeout / http / json errors
                classification = {"operation": "ERROR", "error": str(exc)}
                source = "error"
            cache[event_id] = classification
            _save_cache(cache)

        new_event = _to_event_dict(evt, classification)
        resolved_events.append(new_event)
        is_ok = new_event.get("operation") != "RATE_UNKNOWN"
        if not is_ok:
            failed += 1
        print(
            f"[{idx}/{len(unknown_events)}] {event_id} "
            f"op={new_event.get('operation')} status={new_event.get('status')} "
            f"({source})"
        )
        # Gentle pacing so we never fire more than one in-flight request.
        if source == "llm":
            time.sleep(0.2)

    OUTPUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSONL, "w", encoding="utf-8") as fh:
        for evt in resolved_events:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")

    resolved = sum(1 for e in resolved_events if e.get("operation") != "RATE_UNKNOWN")
    print("\n=== SUMMARY ===")
    print(f"total:   {len(resolved_events)}")
    print(f"resolved:{resolved}")
    print(f"failed:  {failed}")
    print("operations:")
    for op, cnt in Counter(e["operation"] for e in resolved_events).most_common():
        print(f"  {op}: {cnt}")
    print("statuses:")
    for st, cnt in Counter(e.get("status") for e in resolved_events).most_common():
        print(f"  {st}: {cnt}")
    print(f"\nwrote {OUTPUT_JSONL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
