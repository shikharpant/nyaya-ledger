"""Rate ledger — generate queryable HSN-to-rate views from materialized schedules.

Produces two views:
  1. HSN-centric: tariff item → rate history with date ranges
  2. Date-centric: date → all tariff items and their rates

Stitches across supersession boundaries (1/2017 → 9/2025).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class RatePeriod:
    from_date: str
    to_date: Optional[str]
    cgst_rate_pct: float
    gst_rate_pct: float
    schedule: str
    sno: str
    notification: str
    description: str = ""
    condition: str = ""


@dataclass
class HsnRateEntry:
    tariff_item: str
    description: str
    rate_periods: list[RatePeriod] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tariff_item": self.tariff_item,
            "description": self.description,
            "rate_periods": [asdict(rp) for rp in self.rate_periods],
        }


def _normalize_tariff(tariff: str) -> str:
    """Normalize HSN code: remove spaces, lowercase."""
    return re.sub(r"\s+", "", tariff).strip().lower()


def _expand_tariff_list(tariff: str) -> list[str]:
    """Split comma-separated tariff items into individual codes."""
    parts = re.split(r"[,\s]+", tariff.strip())
    return [p for p in parts if p and re.match(r"\d{2,8}", p)]


def _extract_individual_hsns(tariff_item: str) -> list[str]:
    """Extract individual HSN codes from a tariff cell.

    Handles patterns like:
      "0303" → ["0303"]
      "0202, 0203, 0204" → ["0202", "0203", "0204"]
      "63 [other than 6305 32 00, 6309]" → ["63"]  (chapter-level with exclusions)
      "0101 21 00, 0101 29" → ["01012100", "010129"]
    """
    # Remove bracketed exclusions for now (they're handled separately)
    cleaned = re.sub(r"\[.*?\]", "", tariff_item).strip()
    parts = [p.strip() for p in cleaned.split(",")]
    result = []
    for part in parts:
        # Remove spaces within HSN code
        code = re.sub(r"\s+", "", part)
        if re.match(r"^\d{2,8}$", code):
            result.append(code)
    return result


def build_rate_ledger(
    materialized_snapshots: list[dict],
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the rate ledger from materialized schedule snapshots.

    Args:
        materialized_snapshots: list of snapshot dicts from materializer
        output_path: if provided, save as JSON

    Returns:
        {hsn_rates: dict, date_index: dict}
    """
    # Collect all entries across all snapshots
    # Key: normalized_hsn → list of (date_range, rate, schedule, sno, notification, description)
    hsn_history: dict[str, list[dict]] = defaultdict(list)

    for snap in materialized_snapshots:
        notif = snap.get("target_notification", snap.get("notification_id", ""))
        checkpoint_date = snap.get("checkpoint_date", "")

        for sid, sched in snap.get("schedules", {}).items():
            rate_pct = sched.get("rate_pct", 0.0)
            for entry in sched.get("entries", []):
                if entry.get("is_omitted"):
                    continue
                tariff_raw = entry.get("tariff_item", "")
                hsn_codes = _extract_individual_hsns(tariff_raw)
                sno = entry.get("sno", "").rstrip(".")
                desc = entry.get("description", "")

                for hsn in hsn_codes:
                    hsn_history[hsn].append({
                        "date": checkpoint_date,
                        "cgst_rate_pct": rate_pct,
                        "gst_rate_pct": rate_pct * 2,
                        "schedule": sid,
                        "sno": sno,
                        "notification": notif,
                        "description": desc,
                        "tariff_raw": tariff_raw,
                    })

    # Build HSN-centric view
    hsn_rates: dict[str, dict] = {}
    for hsn, records in sorted(hsn_history.items()):
        # Sort by date
        records.sort(key=lambda r: r["date"])
        # Deduplicate by keeping the latest record for each date
        latest = records[-1] if records else {}
        hsn_rates[hsn] = {
            "tariff_item": hsn,
            "description": latest.get("description", ""),
            "current_rate": {
                "cgst_rate_pct": latest.get("cgst_rate_pct", 0),
                "gst_rate_pct": latest.get("gst_rate_pct", 0),
                "schedule": latest.get("schedule", ""),
                "sno": latest.get("sno", ""),
                "notification": latest.get("notification", ""),
            },
            "rate_history": [
                {
                    "date": r["date"],
                    "cgst_rate_pct": r["cgst_rate_pct"],
                    "gst_rate_pct": r["gst_rate_pct"],
                    "schedule": r["schedule"],
                    "sno": r["sno"],
                    "notification": r["notification"],
                }
                for r in records
            ],
        }

    # Build date-centric view
    date_index: dict[str, dict[str, dict]] = defaultdict(dict)
    for hsn, records in hsn_history.items():
        for r in records:
            date = r["date"]
            date_index[date][hsn] = {
                "cgst_rate_pct": r["cgst_rate_pct"],
                "gst_rate_pct": r["gst_rate_pct"],
                "schedule": r["schedule"],
                "sno": r["sno"],
                "notification": r["notification"],
                "description": r["description"],
            }

    result = {
        "hsn_rates": hsn_rates,
        "date_index": dict(date_index),
        "total_hsns": len(hsn_rates),
        "total_dates": len(date_index),
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    return result


def query_rate(
    ledger: dict,
    hsn: str,
    date: str | None = None,
) -> dict | None:
    """Query the rate for a specific HSN at a specific date.

    Args:
        ledger: rate ledger dict
        hsn: HSN code (will be normalized)
        date: YYYY-MM-DD, or None for latest

    Returns:
        {cgst_rate_pct, gst_rate_pct, schedule, sno, notification} or None
    """
    hsn_norm = _normalize_tariff(hsn)
    entry = ledger.get("hsn_rates", {}).get(hsn_norm)
    if not entry:
        return None

    if date is None:
        return entry.get("current_rate")

    # Find the rate at the given date
    history = entry.get("rate_history", [])
    applicable = [r for r in history if r["date"] <= date]
    if applicable:
        latest = applicable[-1]
        return {
            "cgst_rate_pct": latest["cgst_rate_pct"],
            "gst_rate_pct": latest["gst_rate_pct"],
            "schedule": latest["schedule"],
            "sno": latest["sno"],
            "notification": latest["notification"],
        }
    return None


def query_rate_multi_instrument(
    ledger: dict,
    concessional_data: dict[str, dict],
    hsn: str,
    date: str,
) -> dict:
    """Query all applicable rates for a HSN at a date across instruments.

    Returns:
        {
            "hsn": str,
            "primary_rate": {...} | None,
            "concessional_rates": [...],
            "exemption": {...} | None,
        }
    """
    hsn_norm = _normalize_tariff(hsn)
    result = {
        "hsn": hsn_norm,
        "query_date": date,
        "primary_rate": query_rate(ledger, hsn, date),
        "concessional_rates": [],
        "exemption": None,
    }

    for notif_id, data in concessional_data.items():
        eff_date = data.get("effective_date", "")
        if eff_date and date < eff_date:
            continue
        rate = data.get("rate_pct", 0)
        conditions = data.get("conditions", [])

        for entry in data.get("schedules", {}).get("I", {}).get("entries", []):
            entry_hsns = _extract_individual_hsns(entry.get("tariff_item", ""))
            if hsn_norm in [_normalize_tariff(h) for h in entry_hsns]:
                is_exempt = rate == 0
                rate_info = {
                    "notification": notif_id,
                    "cgst_rate_pct": rate,
                    "gst_rate_pct": rate * 2,
                    "description": entry.get("description", ""),
                    "condition": conditions[0]["text"][:200] if conditions else "",
                    "effective_date": eff_date,
                }
                if is_exempt:
                    result["exemption"] = rate_info
                else:
                    result["concessional_rates"].append(rate_info)

    return result


def _short_notif_id(notification_id: str) -> str:
    """Shorten a canonical notification path to an instrument-tagged id.

    The CT(Rate) goods base (``1/2017``) and the Compensation Cess (Rate)
    goods base (also ``1/2017``) would collapse to the same bare id without a
    disambiguating suffix. The suffix is therefore derived from the corpus
    path segment so cess and central-tax instruments stay distinct:

        .../central-tax-rate/2017/1-2017-central-tax-rate      → 1/2017-ct-rate
        .../compensation-cess-rate/2017/1-2017-compensation-…  → 1/2017-cc-rate
    """
    nid = str(notification_id or "")
    m = re.search(r"/(\d{4})/(\d+)-\d+-", nid)
    if not m:
        return nid
    short = f"{m.group(2)}/{m.group(1)}"
    if "compensation-cess-rate" in nid:
        return f"{short}-cc-rate"
    if "central-tax-rate" in nid:
        return f"{short}-ct-rate"
    return short


def _find_tariff_in_base(
    target: str, base: dict
) -> tuple[str, str, dict, dict] | None:
    for sid, sched in base.get("schedules", {}).items():
        for e in sched.get("entries", []):
            codes = [c.lower() for c in _extract_individual_hsns(e.get("tariff_item", ""))]
            if target in codes:
                return sid, e.get("sno", "").rstrip("."), e, sched
    return None


def query_rate_history(
    tariff_item: str,
    events_jsonl_path: str = "derived/version_history/rate-schedules/rate_amendment_events.jsonl",
    base_dir: str = "derived/version_history/rate-schedules",
    instrument: str = "ct-rate",
) -> list[dict]:
    """Get continuous rate history for a tariff item from 2017 to present.

    Handles the Sep 2025 schedule restructuring by using the concordance
    to trace entries across the boundary.

    ``instrument`` selects the instrument family:

        * ``"ct-rate"`` (default) — Central Tax (Rate) goods schedule
          (base ``1/2017-ct-rate`` → ``9/2025-ct-rate``), replayed from
          ``rate_amendment_events.jsonl``.
        * ``"cc-rate"`` — Compensation Cess (Rate) goods schedule
          (base ``1/2017-cc-rate``), replayed from
          ``cess_amendment_events.jsonl``. Cess was not part of the Sep
          2025 restructuring, so there is no supersession boundary and no
          concordance step.

    Returns a list of:
        {
            "date": "2017-07-01",
            "rate_pct": 6.0,
            "schedule": "II",
            "sno": "45",
            "notification": "1/2017-ct-rate",
            "description": "..."
        }
    """
    from legal_corpus.rate_concordance import (
        SUPERSESSION_DATE,
        build_concordance,
        lookup_post_entry,
    )

    base_dir = Path(base_dir)
    is_cess = instrument == "cc-rate"

    if is_cess:
        events_jsonl_path = "derived/version_history/rate-schedules/cess_amendment_events.jsonl"
        pre_base_path = base_dir / "base_cess_1-2017.json"
        post_base_path = None
        default_pre = "1/2017-cc-rate"
        default_post = "1/2017-cc-rate"
    else:
        pre_base_path = base_dir / "base_1-2017.json"
        post_base_path = base_dir / "base_9-2025.json"
        default_pre = "1/2017-ct-rate"
        default_post = "9/2025-ct-rate"

    with open(pre_base_path) as f:
        pre_base = json.load(f)
    post_base: dict = {}
    if post_base_path and post_base_path.exists():
        with open(post_base_path) as f:
            post_base = json.load(f)

    target = _normalize_tariff(tariff_item)
    if not target:
        return []

    pre_notif = _short_notif_id(pre_base.get("notification_id", default_pre)) or default_pre
    post_notif = _short_notif_id(post_base.get("notification_id", default_post)) or default_post
    boundary = SUPERSESSION_DATE
    gst_start = "2017-07-01"

    history: list[dict] = []
    current_sched: str | None = None
    current_sno: str | None = None

    pre_hit = _find_tariff_in_base(target, pre_base)
    if not pre_hit:
        return []
    sid, sno, e, sched = pre_hit
    current_sched, current_sno = sid, sno
    history.append({
        "date": gst_start,
        "rate_pct": sched.get("rate_pct", 0.0),
        "schedule": sid,
        "sno": sno,
        "notification": pre_notif,
        "description": e.get("description", ""),
    })

    with open(events_jsonl_path) as f:
        events = [json.loads(line) for line in f if line.strip()]

    col2_events = sorted(
        [
            e for e in events
            if e.get("operation") == "RATE_SUBSTITUTE_COLUMN"
            and e.get("payload", {}).get("column") == 2
        ],
        key=lambda e: (e.get("effective_date", ""), e.get("target_notification", "")),
    )

    def _apply_event(evt: dict, base: dict, default_notif: str) -> None:
        nonlocal current_sched, current_sno
        new_val = evt.get("payload", {}).get("new_value", "")
        esid = evt.get("target_schedule", "")
        esno = str(evt.get("payload", {}).get("sno", "")).rstrip(".")
        sched_ref = base.get("schedules", {}).get(esid)
        rate = sched_ref.get("rate_pct", 0.0) if sched_ref else 0.0
        codes = [c.lower() for c in _extract_individual_hsns(new_val)]
        target_here = target in codes
        if esid == current_sched and esno == current_sno:
            if not target_here:
                current_sched = None
                current_sno = None
            return
        if target_here and (esid, esno) != (current_sched, current_sno):
            history.append({
                "date": evt.get("effective_date", ""),
                "rate_pct": rate,
                "schedule": esid,
                "sno": esno,
                "notification": evt.get("target_notification", "") or default_notif,
                "description": new_val,
            })
            current_sched, current_sno = esid, esno

    for evt in col2_events:
        if is_cess:
            _apply_event(evt, pre_base, pre_notif)
        elif evt.get("effective_date", "") < boundary:
            _apply_event(evt, pre_base, pre_notif)
        else:
            break

    if is_cess:
        return history

    mapped = None
    if current_sched is not None:
        concordance = build_concordance(
            pre_base_path=pre_base_path, post_base_path=post_base_path
        )
        mapped = lookup_post_entry(concordance, current_sched, current_sno or "")
        if mapped:
            mt = _normalize_tariff(mapped.get("post_tariff", ""))
            if target[:4] not in mt and target not in mt:
                mapped = None

    if mapped and mapped.get("post_schedule") and mapped.get("post_sno"):
        post_sid = mapped["post_schedule"]
        post_sno = str(mapped["post_sno"]).rstrip(".")
        post_rate = mapped.get("post_rate", 0.0)
        post_desc = mapped.get("post_desc", "")
    else:
        post_hit = _find_tariff_in_base(target, post_base)
        if post_hit:
            post_sid, post_sno, pe, psched = post_hit
            post_rate = psched.get("rate_pct", 0.0)
            post_desc = pe.get("description", "")
        else:
            post_sid = post_sno = None

    if post_sid is not None:
        history.append({
            "date": boundary,
            "rate_pct": post_rate,
            "schedule": post_sid,
            "sno": post_sno or "",
            "notification": post_notif,
            "description": post_desc,
        })
        current_sched, current_sno = post_sid, post_sno

    for evt in col2_events:
        if evt.get("effective_date", "") >= boundary:
            _apply_event(evt, post_base, post_notif)

    return history


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "src")
    from legal_corpus.rate_schedule_materializer import materialize_schedule

    # Build ledger from 1/2017 at 2022 checkpoint
    snap = materialize_schedule(
        "derived/version_history/rate-schedules/base_1-2017.json",
        "derived/version_history/rate-schedules/rate_amendment_events.jsonl",
        "1/2017-ct-rate",
        checkpoint_date="2022-05-01",
    )

    ledger = build_rate_ledger([snap], "derived/version_history/rate-schedules/rate_ledger.json")

    print(f"Rate ledger: {ledger['total_hsns']} HSNs, {ledger['total_dates']} dates")

    # Sample queries
    for hsn in ["0303", "0402", "7102", "1703", "8708"]:
        rate = query_rate(ledger, hsn, "2022-05-01")
        if rate:
            print(f"  HSN {hsn}: CGST={rate['cgst_rate_pct']}% GST={rate['gst_rate_pct']}% "
                  f"Schedule={rate['schedule']} S.No={rate['sno']} ({rate['notification']})")
        else:
            print(f"  HSN {hsn}: not found")
