#!/usr/bin/env python3
"""Parse service rate checkpoint PDFs (11/2017-Central Tax (Rate)) into checkpoint JSONs.

Uses pdfplumber word-level extraction with precise x-coordinate column classification.
Handles inserted entries ([2A, [31A), omitted entries (quoted text), and footnotes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pdfplumber

# Valid GST service rate tokens
VALID_RATES = frozenset({
    "0", "0.1", "0.25", "0.75", "1", "1.5", "2.5", "3", "3.75",
    "5", "5.25", "6", "7", "7.5", "9", "12", "14", "18", "28",
    "Nil", "nil",
})

# Column boundaries (x0 in PDF points, from empirical analysis)
X_SNO_MAX = 80       # S.No: x0 < 80 (actual data at ~63)
X_TARIFF_MAX = 150   # Tariff: x0 80-150
X_DESC_MAX = 358     # Description: x0 150-358
X_RATE_MAX = 445     # Rate: x0 358-445
# Condition: x0 >= 445
# These are defaults; auto-detected per PDF via _detect_columns()

_DATE_RE = re.compile(r"amended\s+up\s*(?:to|onto)\s+(\d{1,2})[.](\d{1,2})[.](\d{4})", re.I)
_DATE_RE2 = re.compile(r"UPDATED\s+TILL\s+(\d{1,2})[.](\d{1,2})[.](\d{4})", re.I)
_NOTIF_RE = re.compile(r"Notification\s+No\.?\s*0*(\d{1,3})\s*/\s*(\d{4})", re.I)

# Footnote keyword pattern
_FOOTNOTE_KW = re.compile(
    r"\b(Inserted|Substituted|Omitted|Commenced|inserted|substituted|omitted|commenced)\s+(vide|by|Vide|By)\b"
)

# Category heading pattern (text in parentheses in tariff column)
_CATEGORY_RE = re.compile(r"^\(.*(?:services?|activities)\)$", re.I)


def _detect_columns(pdf) -> dict[str, float]:
    """Auto-detect column boundaries from the first data row (not header)."""
    defaults = {"sno": X_SNO_MAX, "tariff": X_TARIFF_MAX, "desc": X_DESC_MAX, "rate": X_RATE_MAX}
    try:
        # Scan first 5 pages for a data row starting with S.No "1"
        for pg_idx in range(min(5, len(pdf.pages))):
            page = pdf.pages[pg_idx]
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            # Find S.No "1" followed by "Chapter" or "Section" or "Heading"
            for w in words:
                if w["text"].strip().rstrip(".") == "1" and w["x0"] < 100 and w["top"] > 200:
                    # Check if there's a tariff keyword nearby
                    nearby = [ww for ww in words if abs(ww["top"] - w["top"]) < 10 and ww["x0"] > w["x0"]]
                    tariff_kw = [ww for ww in nearby if re.match(r"^(Chapter|Section|Heading)", ww["text"], re.I)]
                    if not tariff_kw:
                        continue
                    sno_x = w["x0"]
                    tariff_x = tariff_kw[0]["x0"]
                    # Description starts after the tariff value (e.g., "99", "5")
                    # Find first non-tariff word after tariff keyword
                    tariff_end = sno_x + 20
                    for ww in nearby:
                        if ww["x0"] > tariff_x and not re.match(r"^(Chapter|Section|Heading|\d{1,4})$", ww["text"], re.I):
                            tariff_end = ww["x0"]
                            break
                    # Find rate position: look for rate tokens in a wide x0 range
                    rate_x = defaults["rate"]
                    # Scan more rows for rate values
                    for w2 in words:
                        if w2["x0"] > tariff_end + 100 and w2["top"] > 200:
                            t = w2["text"].strip().strip(".,;:")
                            if t in VALID_RATES:
                                rate_x = w2["x0"]
                                break
                    return {
                        "sno": min(sno_x + 25, 85),
                        "tariff": tariff_end - 5,
                        "desc": rate_x - 10,
                        "rate": rate_x + 80,
                    }
        return defaults
    except Exception:
        return defaults


def _extract_date(text: str) -> str:
    for pat in [_DATE_RE, _DATE_RE2]:
        m = pat.search(text)
        if m:
            d, mo, y = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return ""


# Auto-detected column boundaries (set by parse_service_rate_pdf)
_cols: dict[str, float] = {"sno": X_SNO_MAX, "tariff": X_TARIFF_MAX, "desc": X_DESC_MAX, "rate": X_RATE_MAX}


def _classify(word: dict) -> str:
    x0 = word["x0"]
    # S.No: check both position AND content (handles [2A at x0=81)
    if x0 < _cols["sno"] + 5:
        if _clean_sno(word["text"]):
            return "sno"
        if x0 < _cols["sno"]:
            return "sno"  # positional fallback for plain numbers
    if x0 < _cols["tariff"]:
        return "tariff"
    if x0 < _cols["desc"]:
        return "desc"
    if x0 < _cols["rate"]:
        return "rate"
    return "cond"


def _is_valid_rate(text: str) -> bool:
    return text.strip().strip(".,;:") in VALID_RATES


def _clean_sno(raw: str) -> str | None:
    """Extract S.No from a raw token, handling [prefix and periods."""
    t = raw.strip().lstrip("[").rstrip(".")
    m = re.match(r"^(\d{1,2})([A-Z]?)$", t)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 39:
            return t
    return None


def _clean_tariff(parts: list[str]) -> str:
    """Keep only tariff-like text, filtering out footnote numbers and noise."""
    cleaned: list[str] = []
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        # "Chapter" + number
        if re.match(r"^Chapter$", p, re.I) and i + 1 < len(parts) and parts[i + 1].strip().isdigit():
            cleaned.append(f"Chapter {parts[i + 1].strip()}")
            i += 2
            continue
        # "Section" + number
        if re.match(r"^Section$", p, re.I) and i + 1 < len(parts) and parts[i + 1].strip().isdigit():
            cleaned.append(f"Section {parts[i + 1].strip()}")
            i += 2
            continue
        # "Heading" + 4-digit number
        if re.match(r"^Heading$", p, re.I) and i + 1 < len(parts) and re.match(r"^\d{4}$", parts[i + 1].strip()):
            cleaned.append(f"Heading {parts[i + 1].strip()}")
            i += 2
            continue
        # Standalone 4-digit heading number → add "Heading" prefix
        if re.match(r"^\d{4}$", p):
            if not cleaned or not cleaned[-1].startswith("Heading"):
                cleaned.append(f"Heading {p}")
            i += 1
            continue
        # "Chapter" + inline number (e.g., "Chapter 99" as one token)
        if re.match(r"^Chapter\s+\d+$", p, re.I):
            cleaned.append(p)
            i += 1
            continue
        # "Section" + inline number
        if re.match(r"^Section\s+\d+$", p, re.I):
            cleaned.append(p)
            i += 1
            continue
        # "Heading" + inline 4-digit
        if re.match(r"^Heading\s+\d{4}", p, re.I):
            cleaned.append(p)
            i += 1
            continue
        # Skip: "or", footnote numbers (1-3 digits), random text
        i += 1

    # Deduplicate consecutive identical entries
    deduped: list[str] = []
    for c in cleaned:
        if not deduped or deduped[-1] != c:
            deduped.append(c)

    # Join multiple headings with " or " (e.g., "9954 or 9983 or 9987")
    result = " ".join(deduped)
    # Normalize: if multiple "Heading XXXX" entries, join with " or "
    if len(deduped) > 1:
        # Extract just the numbers
        nums = []
        for d in deduped:
            m = re.match(r"^(?:Heading|Chapter|Section)\s+(.+)$", d)
            if m:
                nums.append(m.group(1))
            else:
                nums.append(d)
        prefix = "Heading" if deduped[0].startswith("Heading") else ("Chapter" if deduped[0].startswith("Chapter") else "Section")
        return f"{prefix} {' or '.join(nums)}"
    return result.strip()


def parse_service_rate_pdf(pdf_path: str | Path, max_page: int = 50) -> dict[str, Any]:
    """Parse a service rate checkpoint PDF into a checkpoint dict.

    Only processes the first `max_page` pages (main notification table).
    Annexure tables are excluded.
    """
    pdf_path = Path(pdf_path)

    entries: dict[str, dict[str, Any]] = {}
    entry_order: list[str] = []
    current_sno: str | None = None

    with pdfplumber.open(pdf_path) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""

        # Auto-detect column boundaries
        global _cols
        _cols = _detect_columns(pdf)

        for pg_idx in range(min(max_page, len(pdf.pages))):
            page = pdf.pages[pg_idx]
            page_text = page.extract_text() or ""

            # Stop at "This notification shall come into force" (end of notification)
            if "come into force" in page_text.lower() and pg_idx > 5:
                # Process words up to this point on this page, then stop
                pass  # We still process the page — the come-into-force line is a paragraph, not a table entry

            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            if not words:
                continue

            # Check for end-of-notification marker in word rows
            force_end = False

            # Group into rows by top coordinate
            words.sort(key=lambda w: (round(w["top"] / 4) * 4, w["x0"]))
            rows: list[list[dict]] = []
            cur_top: float | None = None
            cur_row: list[dict] = []
            for w in words:
                rk = round(w["top"] / 4) * 4
                if cur_top is None or rk == cur_top:
                    cur_row.append(w)
                    cur_top = rk
                else:
                    if cur_row:
                        cur_row.sort(key=lambda w: w["x0"])
                        rows.append(cur_row)
                    cur_row = [w]
                    cur_top = rk
            if cur_row:
                cur_row.sort(key=lambda w: w["x0"])
                rows.append(cur_row)

            for row in rows:
                row_text = " ".join(w["text"] for w in row)

                # Stop at end-of-notification
                if re.search(r"come\s+into\s+force", row_text, re.I):
                    force_end = True
                    break

                # Skip footnotes
                if _FOOTNOTE_KW.search(row_text):
                    continue
                # Skip page numbers
                if len(row) == 1 and row[0]["text"].strip().isdigit():
                    continue
                # Skip headers
                stripped = row_text.strip()
                if re.match(r"^(Sl\b|No\.\s*$|Chapter,\s|Heading\s*$|Description|Rate\b|Condition|cent\.\s*$|\(\d\)\s)", stripped, re.I):
                    continue

                # Classify words
                sno_words = [w for w in row if _classify(w) == "sno"]
                tariff_words = [w for w in row if _classify(w) == "tariff"]
                desc_words = [w for w in row if _classify(w) == "desc"]
                rate_words = [w for w in row if _classify(w) == "rate"]

                # Check for new S.No
                new_sno = None
                if sno_words:
                    sno_raw = sno_words[0]["text"]
                    sno = _clean_sno(sno_raw)
                    if sno:
                        # Make sure the rest of the line isn't footnote text
                        rest = " ".join(w["text"] for w in row[1:])
                        if not _FOOTNOTE_KW.search(rest[:60]):
                            # Skip if the line starts with a quote mark (omitted text)
                            if not sno_raw.startswith("\u201c") and not sno_raw.startswith('"'):
                                new_sno = sno

                if new_sno:
                    current_sno = new_sno
                    if current_sno not in entries:
                        entries[current_sno] = {
                            "sno": current_sno,
                            "tariff_item": [],
                            "description": [],
                            "category_prefix": [],
                            "rate": "",
                            "is_omitted": False,
                        }
                        entry_order.append(current_sno)
                    # Process current row columns
                    for w in tariff_words:
                        t = w["text"]
                        if _CATEGORY_RE.match(t):
                            entries[current_sno]["category_prefix"].append(t)
                        else:
                            entries[current_sno]["tariff_item"].append(t)
                    for w in desc_words:
                        entries[current_sno]["description"].append(w["text"])
                    for w in rate_words:
                        t = w["text"].strip().strip(".,;:")
                        if _is_valid_rate(t) and not entries[current_sno]["rate"]:
                            entries[current_sno]["rate"] = t

                elif current_sno and current_sno in entries:
                    # Continuation row
                    for w in tariff_words:
                        t = w["text"]
                        if _CATEGORY_RE.match(t):
                            entries[current_sno]["category_prefix"].append(t)
                        elif re.match(r"^(\d{2,4}|Chapter|Section|Heading|or\b)", t, re.I):
                            entries[current_sno]["tariff_item"].append(t)
                        else:
                            entries[current_sno]["description"].append(t)
                    for w in desc_words:
                        entries[current_sno]["description"].append(w["text"])
                    for w in rate_words:
                        t = w["text"].strip().strip(".,;:")
                        if _is_valid_rate(t) and not entries[current_sno]["rate"]:
                            entries[current_sno]["rate"] = t

            if force_end:
                break

    # Finalize entries
    final_entries: list[dict[str, Any]] = []
    for sno in entry_order:
        e = entries[sno]
        # Prepend category headings to description
        cat = " ".join(e.get("category_prefix", []))
        desc = re.sub(r"\s+", " ", " ".join(e["description"])).strip()
        if cat:
            desc = f"{cat} {desc}" if desc else cat
        final_entries.append({
            "sno": sno,
            "tariff_item": _clean_tariff(e["tariff_item"]),
            "description": desc,
            "rate": e["rate"],
            "is_omitted": False,
        })

    # Metadata
    checkpoint_date = _extract_date(first_page_text)
    nm = _NOTIF_RE.search(first_page_text)
    notif_num = f"{int(nm.group(1))}/{int(nm.group(2))}" if nm else "11/2017"
    notif_ref = f"{notif_num}-ct-rate"

    try:
        cwd = Path.cwd()
        source_pdf = str(pdf_path.resolve().relative_to(cwd))
    except ValueError:
        source_pdf = str(pdf_path)

    return {
        "checkpoint_date": checkpoint_date,
        "source_pdf": source_pdf,
        "instruments": {
            notif_ref: {
                "notification_ref": notif_ref,
                "instrument_type": "services_rate",
                "schedules": {
                    "I": {
                        "schedule_id": "I",
                        "rate_pct": 0.0,
                        "heading": "Schedule I – Service Rates",
                        "entries": final_entries,
                    }
                },
            }
        },
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parse service rate checkpoint PDF")
    parser.add_argument("pdf", help="Path to the service rate PDF")
    parser.add_argument("--output", "-o",
                        default="derived/version_history/rate-schedules/checkpoints",
                        help="Output directory for checkpoint JSON")
    parser.add_argument("--max-page", type=int, default=50,
                        help="Max pages to process (main table only)")
    args = parser.parse_args()

    checkpoint = parse_service_rate_pdf(args.pdf, args.max_page)
    date = checkpoint["checkpoint_date"]
    if not date:
        print("WARNING: Could not extract date", file=sys.stderr)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"checkpoint_svc_{date}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, ensure_ascii=False)

    instr_key = list(checkpoint["instruments"])[0]
    entries = checkpoint["instruments"][instr_key]["schedules"]["I"]["entries"]
    print(f"Saved {out_file}")
    print(f"  Date: {date}")
    print(f"  Entries: {len(entries)}")
    for e in entries:
        print(f"    sno={e['sno']:>4}, rate={e['rate']!r:>8}, tariff={e['tariff_item']!r:>30}, desc={e['description'][:70]}")


if __name__ == "__main__":
    main()
