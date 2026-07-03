from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def generate_amendment_ledger(
    events_path: Path,
    output_path: Path,
    target_work: str = "",
) -> dict[str, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    with open(events_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    ledger: dict[str, list[dict[str, Any]]] = defaultdict(list)

    op_labels = {
        "SUBSTITUTE": "Substituted",
        "INSERT_SIBLING": "Inserted",
        "INSERT_CHILD": "Inserted",
        "OMIT": "Omitted",
        "SPLICE": "Amended",
    }

    for ev in events:
        if ev.get("status") != "validated":
            continue
        target = ev.get("target") or {}
        cid = str(target.get("component_id") or "")
        if not cid or "/section/" not in cid:
            continue
        if target_work and target_work not in cid:
            continue
        op = str(ev.get("operation") or "")
        label = op_labels.get(op, op)
        lt = ev.get("legal_time") or {}
        effective = lt.get("applicability_start") or lt.get("commencement_date") or ""
        source = ev.get("source") or {}
        doc_id = str(source.get("document_id") or "")
        notif_match = re.search(r"(\d+-\d{4})", doc_id)
        notification_ref = notif_match.group(1) if notif_match else doc_id.rsplit("/", 1)[-1]
        evidence = ev.get("evidence") or {}
        excerpt = str(evidence.get("excerpt") or "")[:200]
        anchor = str(target.get("anchor_text") or "")

        entry = {
            "event_id": ev.get("event_id", ""),
            "operation": label,
            "effective_date": effective,
            "notification": notification_ref,
            "source_document": doc_id,
            "anchor": anchor,
            "excerpt": excerpt,
        }
        ledger[cid].append(entry)

    for entries in ledger.values():
        entries.sort(key=lambda e: e.get("effective_date") or "")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(ledger.items())), f, indent=2, ensure_ascii=False)

    return dict(ledger)
