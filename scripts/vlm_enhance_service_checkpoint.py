#!/usr/bin/env python3
"""Enhance service rate checkpoints with VLM-extracted category headings.

Uses the chandra-ocr-2 VLM to read PDF table pages as HTML, then parses
the HTML to extract clean category headings and tariff items.
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


class TableParser(HTMLParser):
    """Parse HTML table from VLM output into structured rows."""

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

    def handle_data(self, data: str):
        if self._in_cell:
            self._current_cell.append(data)


def _parse_vlm_html(html_text: str) -> list[dict]:
    """Parse VLM HTML table output into entry dicts."""
    parser = TableParser()
    parser.feed(html_text)

    entries = []
    for row in parser.rows:
        if len(row) < 3:
            continue
        sno_text = re.sub(r"<[^>]+>", "", row[0]).strip()
        # Check if this is an S.No entry
        if not re.match(r"^\d{1,3}[A-Z]?$", sno_text):
            continue

        sno = sno_text
        tariff_cell = row[1] if len(row) > 1 else ""
        desc_cell = row[2] if len(row) > 2 else ""
        rate_cell = row[3] if len(row) > 3 else ""

        # Parse tariff cell: "Heading 9963\n(Accommodation, food and beverage services)"
        tariff_item = ""
        category = ""
        for line in tariff_cell.split("\n"):
            line = line.strip()
            if re.match(r"^(Chapter|Section|Heading)\s+\d", line):
                tariff_item = line
            elif line.startswith("(") and line.endswith(")"):
                category = line

        # Clean description cell (remove HTML tags, normalize whitespace)
        desc_clean = re.sub(r"<[^>]+>", "", desc_cell)
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()

        # Clean rate
        rate_clean = re.sub(r"<[^>]+>", "", rate_cell).strip()
        if rate_clean and not re.match(r"^(Nil|nil|\d{1,2}(\.\d+)?)$", rate_clean):
            rate_clean = ""

        entries.append({
            "sno": sno,
            "tariff_item": tariff_item,
            "category": category,
            "desc_start": desc_clean,
            "rate": rate_clean,
        })

    return entries


def _render_page_b64(pdf, pg_idx: int, dpi: int = 150) -> str:
    page = pdf.pages[pg_idx]
    img = page.to_image(resolution=dpi)
    buf = io.BytesIO()
    img.original.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _ask_vlm(image_b64: str) -> str:
    for attempt in range(3):
        try:
            resp = requests.post(VLM_URL,
                headers={"Authorization": f"Bearer {VLM_KEY}", "Content-Type": "application/json"},
                json={
                    "model": VLM_MODEL,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                        {"type": "text", "text": PROMPT},
                    ]}],
                    "max_tokens": 4000,
                    "temperature": 0.1,
                }, timeout=120)
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


def enhance_checkpoint(checkpoint_path: str, pdf_path: str, max_page: int = 50) -> None:
    """Enhance a checkpoint JSON with VLM-extracted data."""
    checkpoint = json.loads(open(checkpoint_path).read())
    instr_key = list(checkpoint["instruments"])[0]
    entries = checkpoint["instruments"][instr_key]["schedules"]["I"]["entries"]
    entry_map = {e["sno"].rstrip("."): e for e in entries}

    # Find entry-start pages
    entry_start_pages: dict[str, int] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pg_idx in range(min(max_page, len(pdf.pages))):
            page_text = pdf.pages[pg_idx].extract_text() or ""
            if "come into force" in page_text.lower() and pg_idx > 5:
                break
            words = pdf.pages[pg_idx].extract_words(keep_blank_chars=False, use_text_flow=False)
            for w in words:
                raw = w["text"].strip().lstrip("[").rstrip(".")
                if w["x0"] < 90 and re.match(r"^(\d{1,2}[A-Z]?)$", raw):
                    rest = " ".join(ww["text"] for ww in words if abs(ww["top"] - w["top"]) < 10 and ww["x0"] > w["x0"])
                    if not re.search(r"\b(Inserted|Substituted|Omitted|Commenced)\s+(vide|by)\b", rest[:60]):
                        if raw not in entry_start_pages:
                            entry_start_pages[raw] = pg_idx

    pages_to_process = sorted(set(entry_start_pages.values()))
    print(f"Processing {len(pages_to_process)} pages: {[p+1 for p in pages_to_process]}")

    vlm_entries: dict[str, dict] = {}

    with pdfplumber.open(pdf_path) as pdf:
        for pg_idx in pages_to_process:
            sno_list = [s for s, p in entry_start_pages.items() if p == pg_idx]
            print(f"\nPage {pg_idx+1} (expecting: {sno_list}):")
            time.sleep(5)
            img_b64 = _render_page_b64(pdf, pg_idx)
            html_response = _ask_vlm(img_b64)
            if not html_response:
                print("    No response")
                continue

            parsed = _parse_vlm_html(html_response)
            for entry in parsed:
                sno = entry["sno"]
                if sno in sno_list:
                    vlm_entries[sno] = entry
                    cat = entry["category"]
                    tariff = entry["tariff_item"]
                    print(f"    sno={sno}: tariff={tariff!r}, cat={cat!r}")

    # Merge VLM data into checkpoint
    enhanced = 0
    for sno, vlm_data in vlm_entries.items():
        if sno not in entry_map:
            continue

        e = entry_map[sno]
        cat = vlm_data.get("category", "")
        tariff = vlm_data.get("tariff_item", "")
        desc_start = vlm_data.get("desc_start", "")

        changed = False

        # Update tariff if VLM found a clean one
        if tariff and not e.get("tariff_item"):
            e["tariff_item"] = tariff
            changed = True
        elif tariff and e.get("tariff_item") and tariff != e["tariff_item"]:
            # Prefer VLM's cleaner tariff
            e["tariff_item"] = tariff
            changed = True

        # Prepend category heading if available
        if cat:
            old_desc = e["description"]
            if not old_desc.startswith(cat[:15]):
                # Replace description with VLM category + VLM desc_start + remaining text parser desc
                e["description"] = f"{cat} {desc_start}".strip()
                changed = True

        if changed:
            enhanced += 1

    # Save
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, ensure_ascii=False)
    print(f"\nEnhanced {enhanced}/{len(vlm_entries)} entries")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", help="Path to checkpoint JSON")
    parser.add_argument("pdf", help="Path to source PDF")
    parser.add_argument("--max-page", type=int, default=50)
    args = parser.parse_args()
    enhance_checkpoint(args.checkpoint, args.pdf, args.max_page)
