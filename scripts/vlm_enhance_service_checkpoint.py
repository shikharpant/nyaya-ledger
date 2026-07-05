#!/usr/bin/env python3
"""Enhance service rate checkpoints with VLM-extracted full descriptions.

Processes ALL table pages (not just entry-start pages) to reconstruct
complete entry descriptions from VLM HTML table output. Each page is sent
to the chandra-ocr-2 VLM which returns clean HTML; rows are classified as
new S.No entries or continuation rows, and description text is accumulated
across pages to build the full description for each entry.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import pdfplumber
import requests

VLM_URL = "http://100.79.90.123:8000/v1/chat/completions"
VLM_KEY = "omlx-your-secret-key"
VLM_MODEL = "jwindle47--chandra-ocr-2-8bit-mlx"

PROMPT = "Read and transcribe the table on this page. Output the complete table in HTML format."

CACHE_DIR = Path("derived/vlm_cache")

# Valid GST service rate tokens
_RATES = frozenset({
    "0", "0.1", "0.25", "0.75", "1", "1.5", "2.5", "3", "3.75",
    "5", "5.25", "6", "7", "7.5", "9", "12", "14", "18", "28",
    "Nil", "nil",
})


class TableParser(HTMLParser):
    """Parse HTML table from VLM output into structured rows.

    Each row is a list of cell strings. ``<br>`` and ``</p>`` insert
    newlines within cells so paragraph boundaries are preserved.
    """

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] = []
        self._in_cell = False
        self._in_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._in_cell = True
            self._current_cell = []
        elif tag == "br" and self._in_cell:
            self._current_cell.append("\n")

    def handle_endtag(self, tag: str):
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None
        elif tag in ("td", "th") and self._in_cell:
            cell_text = "".join(self._current_cell).strip()
            self._current_row.append(cell_text)
            self._in_cell = False
        elif tag == "p" and self._in_cell:
            self._current_cell.append("\n")

    def handle_data(self, data: str):
        if self._in_cell:
            self._current_cell.append(data)


# ── cell / row helpers ───────────────────────────────────────────────────────

def _is_sno(text: str) -> bool:
    """Check if text is a serial number like '7', '2A', '[31A'."""
    t = text.strip().lstrip("[").rstrip(".")
    return bool(re.match(r"^\d{1,3}[A-Z]?$", t))


def _is_rate(text: str) -> bool:
    """Check if text is a valid GST rate token."""
    return text.strip().rstrip(".") in _RATES


def _clean_cell(text: str) -> str:
    """Strip residual HTML tags and normalise whitespace to single spaces."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_tariff_cell(cell: str) -> tuple[str, str]:
    """Split a tariff cell into ``(tariff_item, category)``.

    The VLM puts the tariff item and category heading on separate lines,
    e.g. ``Heading 9954\\n(Construction services)``.
    """
    cell = re.sub(r"<[^>]+>", "", cell)
    tariff_item = ""
    category = ""
    for line in cell.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(Chapter|Section|Heading)\s+\d", line, re.I):
            tariff_item = line
        elif line.startswith("(") and line.endswith(")") and len(line) > 5:
            if not category:
                category = line
    return tariff_item, category


_HEADER_KW = (
    "sl no", "sl. no", "serial", "chapter, section or heading",
    "description of service", "rate (per cent", "condition",
)


def _is_header_row(row: list[str]) -> bool:
    """Detect repeated column-header rows."""
    joined = " ".join(row).lower()
    return any(kw in joined for kw in _HEADER_KW) and len(row) <= 6


def _classify_row(row: list[str]) -> dict | None:
    """Classify a parsed table row.

    Returns a dict with ``type`` of either ``"new"`` (S.No present) or
    ``"continuation"`` (empty / non-S.No first cell).  ``None`` for rows
    that carry no useful data.
    """
    if len(row) < 2:
        return None
    if _is_header_row(row):
        return None

    first = row[0].strip()

    # ── new S.No entry ──────────────────────────────────────────────────
    if _is_sno(first):
        sno = first.lstrip("[").rstrip(".")
        cells = row[1:]
        tariff_cell = ""
        desc_cell = ""
        rate_cell = ""
        cond_cell = ""
        if len(cells) >= 4:
            tariff_cell, desc_cell, rate_cell = cells[0], cells[1], cells[2]
            cond_cell = cells[3]
        elif len(cells) == 3:
            tariff_cell, desc_cell, rate_cell = cells[0], cells[1], cells[2]
        elif len(cells) == 2:
            tariff_cell, desc_cell = cells[0], cells[1]
        elif len(cells) == 1:
            desc_cell = cells[0]

        tariff_item, category = _parse_tariff_cell(tariff_cell)
        rate = rate_cell.strip().rstrip(".") if _is_rate(rate_cell) else ""
        return {
            "type": "new",
            "sno": sno,
            "tariff_item": tariff_item,
            "category": category,
            "desc": _clean_cell(desc_cell),
            "rate": rate,
            "condition": _clean_cell(cond_cell),
        }

    # ── continuation row ────────────────────────────────────────────────
    return _classify_continuation(row)


def _classify_continuation(row: list[str]) -> dict:
    """Extract desc / rate / condition from a continuation row.

    Uses the rate cell as an anchor when present.  Falls back to
    splitting on empty-cell gaps (the VLM drops the tariff column for
    continuation rows, leaving ``[empty, desc, rate, condition]``).
    """
    cells = [c.strip() for c in row]
    if not any(cells):
        return {"type": "continuation", "desc": "", "rate": "", "condition": ""}

    # Find rightmost rate cell
    rate = ""
    rate_pos = -1
    for i in range(len(cells) - 1, -1, -1):
        if _is_rate(cells[i]):
            rate = cells[i].rstrip(".")
            rate_pos = i
            break

    if rate_pos >= 0:
        desc_parts = [c for c in cells[:rate_pos] if c]
        cond_parts = [c for c in cells[rate_pos + 1:] if c]
    else:
        # No rate cell — split by empty-cell gaps
        groups: list[list[str]] = []
        cur: list[str] = []
        for c in cells:
            if c:
                cur.append(c)
            elif cur:
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)

        if len(groups) >= 2:
            desc_parts = list(groups[0])
            cond_parts = list(groups[-1])
            for g in groups[1:-1]:
                desc_parts.extend(g)
        elif len(groups) == 1:
            desc_parts = groups[0]
            cond_parts = []
        else:
            desc_parts = []
            cond_parts = []

    return {
        "type": "continuation",
        "desc": _clean_cell(" ".join(desc_parts)),
        "rate": rate,
        "condition": _clean_cell(" ".join(cond_parts)),
    }


def _extract_table_rows(html_text: str) -> list[list[str]]:
    """Parse all ``<table>`` rows from VLM HTML output."""
    parser = TableParser()
    parser.feed(html_text)
    return parser.rows


# ── VLM interaction ──────────────────────────────────────────────────────────

def _render_page_b64(pdf, pg_idx: int, dpi: int = 150) -> str:
    page = pdf.pages[pg_idx]
    img = page.to_image(resolution=dpi)
    buf = io.BytesIO()
    img.original.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _ask_vlm(image_b64: str) -> str:
    """Send an image to the VLM and return the HTML response text."""
    for attempt in range(3):
        try:
            resp = requests.post(VLM_URL,
                headers={"Authorization": f"Bearer {VLM_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": VLM_MODEL,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                        {"type": "text", "text": PROMPT},
                    ]}],
                    "max_tokens": 8000,
                    "temperature": 0.1,
                }, timeout=180)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            elif resp.status_code == 400 and "prefill_memory" in resp.text:
                print(f"    Memory exceeded, retry in 30s (attempt {attempt+1})")
                time.sleep(30)
            else:
                print(f"    Error {resp.status_code}: {resp.text[:200]}")
                return ""
        except Exception as ex:
            print(f"    Exception: {ex}")
            time.sleep(10)
    return ""


def _cache_key(pdf_path: str, pg_idx: int) -> str:
    stem = Path(pdf_path).stem.replace(" ", "_")
    return f"{stem}_p{pg_idx + 1}"


def _get_cache(pdf_path: str, pg_idx: int) -> str | None:
    f = CACHE_DIR / f"{_cache_key(pdf_path, pg_idx)}.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    return None


def _set_cache(pdf_path: str, pg_idx: int, html: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / f"{_cache_key(pdf_path, pg_idx)}.html"
    f.write_text(html, encoding="utf-8")


# ── page-range detection ─────────────────────────────────────────────────────

def _find_table_pages(pdf, max_page: int) -> tuple[int, int]:
    """Return ``(start_idx, end_idx)`` inclusive page indices for the table.

    Starts at the first page mentioning the 11/2017 notification and ends
    at (and includes) the first page with "come into force".
    """
    start = 0
    end = min(max_page, len(pdf.pages))
    for pg_idx in range(end):
        text = (pdf.pages[pg_idx].extract_text() or "").lower()
        if start == 0 and ("central tax (rate)" in text or "11/2017" in text):
            start = pg_idx
        if "come into force" in text and pg_idx > start + 3:
            end = pg_idx
            break
    return start, end


# ── main enhancement ─────────────────────────────────────────────────────────

def enhance_checkpoint(checkpoint_path: str, pdf_path: str, max_page: int = 50) -> None:
    """Enhance a checkpoint JSON with VLM-reconstructed descriptions.

    Processes every table page in the PDF, accumulates description and
    condition text across continuation rows, and merges the result back
    into the checkpoint keyed by S.No.
    """
    checkpoint = json.loads(open(checkpoint_path).read())
    instr_key = list(checkpoint["instruments"])[0]
    entries = checkpoint["instruments"][instr_key]["schedules"]["I"]["entries"]
    entry_map = {e["sno"].rstrip("."): e for e in entries}

    with pdfplumber.open(pdf_path) as pdf:
        start_pg, end_pg = _find_table_pages(pdf, max_page)
        n_pages = end_pg - start_pg + 1
        print(f"Table pages: {start_pg + 1}–{end_pg + 1} ({n_pages} pages)")

        # Accumulator:  sno -> dict with accumulated fields
        vlm_entries: dict[str, dict] = {}
        entry_order: list[str] = []
        current_sno: str | None = None
        failed_pages: list[int] = []

        for pg_idx in range(start_pg, end_pg + 1):
            page_text = pdf.pages[pg_idx].extract_text() or ""
            print(f"\nPage {pg_idx + 1}/{end_pg + 1}:", end=" ", flush=True)

            # Try cache first
            html_response = _get_cache(pdf_path, pg_idx)
            if html_response is not None:
                print("(cached)", end=" ", flush=True)
            else:
                time.sleep(5)
                img_b64 = _render_page_b64(pdf, pg_idx)
                html_response = _ask_vlm(img_b64)
                if not html_response:
                    print("FAILED")
                    failed_pages.append(pg_idx + 1)
                    continue
                _set_cache(pdf_path, pg_idx, html_response)
                print("(new)", end=" ", flush=True)

            rows = _extract_table_rows(html_response)
            page_new = 0
            page_cont = 0
            for row in rows:
                # Stop at end-of-notification marker inside table
                if any("come into force" in c.lower() for c in row):
                    break
                classified = _classify_row(row)
                if classified is None:
                    continue

                if classified["type"] == "new":
                    sno = classified["sno"]
                    if sno not in vlm_entries:
                        vlm_entries[sno] = {
                            "tariff_item": classified["tariff_item"],
                            "category": classified["category"],
                            "rate": classified["rate"],
                            "desc_parts": [],
                            "cond_parts": [],
                        }
                        entry_order.append(sno)
                    else:
                        v = vlm_entries[sno]
                        if classified["tariff_item"] and not v["tariff_item"]:
                            v["tariff_item"] = classified["tariff_item"]
                        if classified["category"] and not v["category"]:
                            v["category"] = classified["category"]
                        if classified["rate"] and not v["rate"]:
                            v["rate"] = classified["rate"]
                    current_sno = sno
                    if classified["desc"]:
                        vlm_entries[sno]["desc_parts"].append(classified["desc"])
                    if classified["condition"]:
                        vlm_entries[sno]["cond_parts"].append(classified["condition"])
                    page_new += 1

                elif classified["type"] == "continuation" and current_sno:
                    v = vlm_entries[current_sno]
                    if classified["desc"]:
                        v["desc_parts"].append(classified["desc"])
                    if classified["condition"]:
                        v["cond_parts"].append(classified["condition"])
                    if classified["rate"] and not v["rate"]:
                        v["rate"] = classified["rate"]
                    page_cont += 1

            print(f"new={page_new} cont={page_cont}")

    if failed_pages:
        print(f"\nFailed pages: {failed_pages}")

    # ── merge into checkpoint ───────────────────────────────────────────
    enhanced = 0
    for sno, vlm in vlm_entries.items():
        if sno not in entry_map:
            continue
        e = entry_map[sno]

        desc = " ".join(vlm["desc_parts"])
        desc = re.sub(r"\s+", " ", desc).strip()
        if vlm["category"]:
            cat = vlm["category"]
            if not desc.startswith(cat[:15]):
                desc = f"{cat} {desc}".strip()
        desc = re.sub(r"\s+", " ", desc).strip()

        changed = False
        if desc and len(desc) > len(e.get("description", "")):
            e["description"] = desc
            changed = True
        if vlm["tariff_item"] and vlm["tariff_item"] != e.get("tariff_item", ""):
            e["tariff_item"] = vlm["tariff_item"]
            changed = True
        if vlm["rate"] and vlm["rate"] != e.get("rate", ""):
            e["rate"] = vlm["rate"]
            changed = True
        cond = re.sub(r"\s+", " ", " ".join(vlm["cond_parts"])).strip()
        if cond:
            e["conditions"] = cond
        if changed:
            enhanced += 1

    # Save
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, ensure_ascii=False)
    print(f"\nEnhanced {enhanced}/{len(vlm_entries)} VLM entries "
          f"(matched {len([s for s in vlm_entries if s in entry_map])}/"
          f"{len(entry_map)} checkpoint entries)")

    # Summary
    for e in entries:
        print(f"  sno={e['sno']:>4} len={len(e['description']):>5}  "
              f"desc={e['description'][:80]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Enhance service checkpoint with VLM full-page descriptions")
    parser.add_argument("checkpoint", help="Path to checkpoint JSON")
    parser.add_argument("pdf", help="Path to source PDF")
    parser.add_argument("--max-page", type=int, default=50)
    args = parser.parse_args()
    enhance_checkpoint(args.checkpoint, args.pdf, args.max_page)
