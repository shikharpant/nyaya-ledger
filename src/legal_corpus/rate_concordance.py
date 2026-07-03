"""Pre/post Sep-2025 schedule concordance for GST rate rationalization.

The Sep 2025 rate rationalization (notification 9/2025) restructured all
rate schedules.  This module builds a concordance table mapping entries
across the pre-2025 (1/2017) and post-2025 (9/2025) schedule structures.

Schedule mapping (by rate):
    PRE 1/2017          POST 9/2025
    ─────────────────   ─────────────────
    Sched I   2.5%  →   Sched I    2.5%   (expanded — absorbed old 6%)
    Sched II  6.0%  →   Sched I    2.5%   (rate reduced, merged into I)
    Sched III 9.0%  →   Sched II   9.0%   (same rate, schedule ID changed)
    Sched IV  14.0%  →   Sched VII 14.0%   (greatly reduced; most items lowered)
    Sched V   1.5%  →   Sched IV   1.5%   (same rate, schedule ID changed)
    Sched VI  0.125%→   Sched V    0.125% (same rate, schedule ID changed)
    (new)              Sched III  20.0%   (NEW — sin/luxury goods)
    (new)              Sched VI   0.75%   (NEW)

List attachments:
    PRE:  List 1 [See S.No 180 of Sched I], List 2 [S.No 181], List 3 [S.No 257]
    POST: List 1 [See S.No 478 of Sched I]
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


SUPERSESSION_DATE = "2025-09-22"

SCHEDULE_RATE_MAP: dict[str, dict[str, str]] = {
    "I":   {"post_schedule": "I",   "pre_rate": 2.5,   "post_rate": 2.5},
    "II":  {"post_schedule": "I",   "pre_rate": 6.0,   "post_rate": 2.5},
    "III": {"post_schedule": "II",  "pre_rate": 9.0,   "post_rate": 9.0},
    "IV":  {"post_schedule": "VII", "pre_rate": 14.0,  "post_rate": 14.0},
    "V":   {"post_schedule": "IV",  "pre_rate": 1.5,   "post_rate": 1.5},
    "VI":  {"post_schedule": "V",   "pre_rate": 0.125, "post_rate": 0.125},
}

POST_TO_PRE_MAP: dict[str, list[str]] = defaultdict(list)
for pre_sid, info in SCHEDULE_RATE_MAP.items():
    POST_TO_PRE_MAP[info["post_schedule"]].append(pre_sid)

LIST_ATTACHMENTS_PRE: dict[str, str] = {
    "1": "180",
    "2": "181",
    "3": "257",
}

LIST_ATTACHMENTS_POST: dict[str, str] = {
    "1": "478",
}


def _normalize_tariff(t: str) -> str:
    t = re.sub(r"\s+", "", str(t or "")).strip().lower()
    t = re.sub(r"\[.*?\]|\(.*?\)", "", t).strip()
    return t


def _normalize_desc(d: str) -> str:
    d = str(d or "").lower()
    for src, dst in {"\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}.items():
        d = d.replace(src, dst)
    return re.sub(r"\s+", " ", d).strip().rstrip(".").strip()


def build_concordance(
    pre_base_path: str | Path = "derived/version_history/rate-schedules/base_1-2017.json",
    post_base_path: str | Path = "derived/version_history/rate-schedules/base_9-2025.json",
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a concordance table between pre- and post-Sep-2025 schedules.

    Returns a dict with:
        - 'schedule_map': {pre_sched: {post_schedule, pre_rate, post_rate}}
        - 'list_map': {era: {list_num: sno}}
        - 'entries': list of {pre_sched, pre_sno, pre_tariff, pre_desc,
                              post_sched, post_sno, post_tariff, post_desc, confidence}
        - 'unmatched_pre': list of entries with no post counterpart
        - 'summary': {matched, unmatched, by_schedule}
    """
    with open(pre_base_path) as f:
        pre = json.load(f)
    with open(post_base_path) as f:
        post = json.load(f)

    post_index: dict[tuple[str, str], dict] = {}
    for sid, sched in post.get("schedules", {}).items():
        for e in sched.get("entries", []):
            t = _normalize_tariff(e.get("tariff_item", ""))
            if t and len(t) >= 4:
                key = (sid, t)
                if key not in post_index:
                    post_index[key] = e

    post_desc_index: dict[tuple[str, str], dict] = {}
    for sid, sched in post.get("schedules", {}).items():
        for e in sched.get("entries", []):
            d = _normalize_desc(e.get("description", ""))
            if d and len(d) >= 20:
                key = (sid, d[:30])
                if key not in post_desc_index:
                    post_desc_index[key] = e

    entries: list[dict] = []
    unmatched: list[dict] = []
    by_schedule: dict[str, dict[str, int]] = defaultdict(lambda: {"matched": 0, "unmatched": 0})

    for sid, sched in sorted(pre.get("schedules", {}).items()):
        mapping = SCHEDULE_RATE_MAP.get(sid)
        if not mapping:
            continue
        post_sid = mapping["post_schedule"]
        pre_rate = mapping["pre_rate"]
        post_rate = mapping["post_rate"]

        for e in sched.get("entries", []):
            pre_sno = e["sno"].rstrip(".")
            tariff = _normalize_tariff(e.get("tariff_item", ""))
            desc = e.get("description", "")

            post_entry = None
            confidence = 0.0

            if tariff and len(tariff) >= 4:
                for t_try in [tariff, tariff[:4], tariff[:6]]:
                    key = (post_sid, t_try)
                    if key in post_index:
                        post_entry = post_index[key]
                        confidence = 0.9
                        break

            if not post_entry and tariff:
                for psid in post.get("schedules", {}):
                    for t_try in [tariff, tariff[:4]]:
                        key = (psid, t_try)
                        if key in post_index:
                            post_entry = post_index[key]
                            confidence = 0.7
                            break
                    if post_entry:
                        break

            if not post_entry and desc:
                d_norm = _normalize_desc(desc)
                if len(d_norm) >= 20:
                    key = (post_sid, d_norm[:30])
                    if key in post_desc_index:
                        post_entry = post_desc_index[key]
                        confidence = 0.6

            if post_entry:
                entries.append({
                    "pre_schedule": sid,
                    "pre_sno": pre_sno,
                    "pre_rate": pre_rate,
                    "pre_tariff": e.get("tariff_item", ""),
                    "pre_desc": desc,
                    "post_schedule": post_entry.get("_schedule_id", post_sid),
                    "post_sno": post_entry["sno"].rstrip("."),
                    "post_rate": post_rate,
                    "post_tariff": post_entry.get("tariff_item", ""),
                    "post_desc": post_entry.get("description", ""),
                    "confidence": confidence,
                })
                by_schedule[sid]["matched"] += 1
            else:
                unmatched.append({
                    "pre_schedule": sid,
                    "pre_sno": pre_sno,
                    "pre_rate": pre_rate,
                    "pre_tariff": e.get("tariff_item", ""),
                    "pre_desc": desc,
                })
                by_schedule[sid]["unmatched"] += 1

    result = {
        "supersession_date": SUPERSESSION_DATE,
        "schedule_map": SCHEDULE_RATE_MAP,
        "post_to_pre_map": dict(POST_TO_PRE_MAP),
        "list_map": {
            "pre": LIST_ATTACHMENTS_PRE,
            "post": LIST_ATTACHMENTS_POST,
        },
        "entries": entries,
        "unmatched_pre": unmatched,
        "summary": {
            "matched": len(entries),
            "unmatched": len(unmatched),
            "by_schedule": dict(by_schedule),
        },
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    return result


def lookup_post_entry(
    concordance: dict,
    pre_schedule: str,
    pre_sno: str,
) -> dict | None:
    """Look up the post-Sep-2025 counterpart of a pre-Sep-2025 entry."""
    for entry in concordance.get("entries", []):
        if entry["pre_schedule"] == pre_schedule and entry["pre_sno"] == pre_sno.rstrip("."):
            return entry
    return None


def map_schedule_id(
    schedule_id: str,
    era: str,
) -> str | list[str]:
    """Map a schedule ID between eras.

    Args:
        schedule_id: Schedule ID (e.g., "II", "III")
        era: "pre_to_post" or "post_to_pre"

    Returns:
        Mapped schedule ID (or list of IDs for post_to_pre)
    """
    if era == "pre_to_post":
        mapping = SCHEDULE_RATE_MAP.get(schedule_id)
        return mapping["post_schedule"] if mapping else schedule_id
    elif era == "post_to_pre":
        return POST_TO_PRE_MAP.get(schedule_id, [schedule_id])
    return schedule_id


def map_sno_by_tariff(
    source_schedule: str,
    source_sno: str,
    source_tariff: str,
    target_base: dict,
    target_era: str,
) -> tuple[str, str] | None:
    """Find a S.No in the target era by matching tariff item.

    Args:
        source_schedule: Schedule in source era
        source_sno: S.No in source era
        source_tariff: Tariff item to match
        target_base: Base notification JSON of target era
        target_era: "pre" or "post"

    Returns:
        (target_schedule, target_sno) or None
    """
    tariff = _normalize_tariff(source_tariff)
    if not tariff or len(tariff) < 4:
        return None

    if target_era == "post":
        candidate_schedules = [SCHEDULE_RATE_MAP.get(source_schedule, {}).get("post_schedule", source_schedule)]
    else:
        candidate_schedules = POST_TO_PRE_MAP.get(source_schedule, [source_schedule])

    for sid in candidate_schedules:
        sched = target_base.get("schedules", {}).get(sid, {})
        for e in sched.get("entries", []):
            e_tariff = _normalize_tariff(e.get("tariff_item", ""))
            if e_tariff == tariff or e_tariff.startswith(tariff[:4]):
                return (sid, e["sno"].rstrip("."))

    for sid, sched in target_base.get("schedules", {}).items():
        for e in sched.get("entries", []):
            e_tariff = _normalize_tariff(e.get("tariff_item", ""))
            if e_tariff == tariff:
                return (sid, e["sno"].rstrip("."))

    return None


__all__ = [
    "SUPERSESSION_DATE",
    "SCHEDULE_RATE_MAP",
    "POST_TO_PRE_MAP",
    "LIST_ATTACHMENTS_PRE",
    "LIST_ATTACHMENTS_POST",
    "build_concordance",
    "lookup_post_entry",
    "map_schedule_id",
    "map_sno_by_tariff",
]
