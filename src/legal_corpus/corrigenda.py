"""Apply deterministic corrigendum corrections to amendment-event ledgers."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .version_snapshots import _corrigendum_effect_date, _parse_corrigendum


CORRIGENDUM_APPLICATION_VERSION = "corrigendum-application-v1"
_TEXT_FIELD_PATHS = [
    ("payload", "old_text"),
    ("payload", "new_text"),
    ("payload", "anchor_text"),
    ("payload", "insert_text"),
    ("payload", "content"),
    ("payload", "structural_text"),
    ("payload", "text"),
    ("target", "anchor_text"),
    ("evidence", "excerpt"),
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _date_key(event: dict[str, Any]) -> str:
    legal_time = event.get("legal_time") or {}
    source = event.get("source") or {}
    return legal_time.get("applicability_start") or legal_time.get("commencement_date") or source.get("publication_date") or ""


def _notification_ref(event: dict[str, Any]) -> str | None:
    source = event.get("source") or {}
    candidates = [
        str(source.get("instrument_number") or ""),
        str(source.get("document_id") or ""),
    ]
    for value in candidates:
        match = re.search(r"(?<!\d)(\d{1,3})[/-](\d{4})(?!\d)", value)
        if match:
            return f"{int(match.group(1)):02d}/{match.group(2)}"
    return None


def _normalize_ref(ref: str) -> str:
    match = re.search(r"(?<!\d)(\d{1,3})/(\d{4})(?!\d)", ref or "")
    if not match:
        return ref
    return f"{int(match.group(1)):02d}/{match.group(2)}"


def _source_span_hash(event: dict[str, Any]) -> str:
    span = ((event.get("evidence") or {}).get("source_span") or {})
    return str(span.get("text_hash") or "")


def _event_hash(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _corrigendum_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("operation") != "CORRIGENDUM":
            continue
        text = str((event.get("payload") or {}).get("text") or (event.get("evidence") or {}).get("excerpt") or "")
        parsed = _parse_corrigendum(text)
        corrected_refs = [_normalize_ref(ref) for ref in parsed.get("refers_to_notifications", [])]
        rows.append(
            {
                "corrigendum_event": event,
                "corrigendum_event_id": event.get("event_id"),
                "corrected_notification_refs": corrected_refs,
                "corrections": parsed.get("corrections", []),
                "corrigendum_publication_date": (event.get("source") or {}).get("publication_date"),
                "corrigendum_effect_date": _corrigendum_effect_date(event, parsed),
                "retrospective": bool(parsed.get("retrospective")),
                "date_basis": parsed.get("date_basis", "corrigendum_publication_date"),
            }
        )
    return rows


def _replace_field(event: dict[str, Any], path: tuple[str, str], old_text: str, new_text: str) -> dict[str, Any] | None:
    parent_key, child_key = path
    parent = event.get(parent_key)
    if not isinstance(parent, dict):
        return None
    value = parent.get(child_key)
    if not isinstance(value, str) or old_text not in value:
        return None
    before = value
    parent[child_key] = value.replace(old_text, new_text, 1)
    return {
        "path": ".".join(path),
        "old_value_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "new_value_sha256": hashlib.sha256(parent[child_key].encode("utf-8")).hexdigest(),
    }


def _apply_row_to_event(event: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    event_ref = _notification_ref(event)
    if not event_ref or event_ref not in set(row.get("corrected_notification_refs") or []):
        return []
    if not row.get("retrospective"):
        event_date = _date_key(event)
        effect_date = str(row.get("corrigendum_effect_date") or "")
        if event_date and effect_date and event_date > effect_date:
            pass
        # Source events from the corrected notification are patched as source-text corrections
        # even when the corrigendum publication is later; the provenance records that basis.
    patches: list[dict[str, Any]] = []
    for correction in row.get("corrections") or []:
        old_text = str(correction.get("old_text") or "")
        new_text = str(correction.get("new_text") or "")
        if not old_text or not new_text or old_text == new_text:
            continue
        for path in _TEXT_FIELD_PATHS:
            patch = _replace_field(event, path, old_text, new_text)
            if patch:
                patch.update({"old_text": old_text, "new_text": new_text})
                patches.append(patch)
    return patches


def apply_corrigenda(
    *,
    events_path: Path,
    output: Path,
    report_output: Path,
) -> dict[str, Any]:
    events = _read_jsonl(events_path)
    rows = _corrigendum_rows(events)
    output_rows: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    candidate_count = 0
    corrigenda_with_corrections = 0
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for original in events:
        event = copy.deepcopy(original)
        if event.get("operation") == "CORRIGENDUM":
            output_rows.append(event)
            continue
        event_patches: list[dict[str, Any]] = []
        for row in rows:
            if row.get("corrections"):
                corrigenda_with_corrections += 1
            patches = _apply_row_to_event(event, row)
            if not patches:
                continue
            candidate_count += 1
            event_patches.append(
                {
                    "corrigendum_event_id": row.get("corrigendum_event_id"),
                    "corrigendum_source_span": ((row.get("corrigendum_event") or {}).get("evidence") or {}).get("source_span", {}),
                    "corrigendum_source_span_hash": _source_span_hash(row.get("corrigendum_event") or {}),
                    "corrigendum_publication_date": row.get("corrigendum_publication_date"),
                    "corrigendum_effect_date": row.get("corrigendum_effect_date"),
                    "retrospective": row.get("retrospective"),
                    "date_basis": row.get("date_basis"),
                    "patches": patches,
                }
            )
        if event_patches:
            event.setdefault("payload", {})["corrigendum_applications"] = event_patches
            event.setdefault("system_time", {})["corrigendum_applied_at"] = now
            applied.append(
                {
                    "original_event_id": original.get("event_id"),
                    "original_event_sha256": _event_hash(original),
                    "corrected_event_sha256": _event_hash(event),
                    "original_source_span": ((original.get("evidence") or {}).get("source_span") or {}),
                    "original_source_span_hash": _source_span_hash(original),
                    "source_document_id": (original.get("source") or {}).get("document_id"),
                    "source_record_id": (original.get("source") or {}).get("record_id"),
                    "applications": event_patches,
                }
            )
        output_rows.append(event)

    _write_jsonl(output, output_rows)
    report = {
        "ok": True,
        "version": CORRIGENDUM_APPLICATION_VERSION,
        "events_path": str(events_path),
        "output": str(output),
        "corrigendum_count": len(rows),
        "corrigenda_with_corrections_count": len([row for row in rows if row.get("corrections")]),
        "candidate_patch_scan_count": candidate_count,
        "applied_event_count": len(applied),
        "applications": applied,
    }
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return report


__all__ = ["CORRIGENDUM_APPLICATION_VERSION", "apply_corrigenda"]
