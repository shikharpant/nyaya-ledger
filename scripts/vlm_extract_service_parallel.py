#!/usr/bin/env python3
"""Parallel VLM extraction of service rate checkpoint PDFs.

Processes ALL table pages (1 to "come into force") concurrently with 8
workers, rotating between chandra-ocr-2 and VibeThinker-3B VLM models.
Caches HTML output, then reconstructs clean checkpoint descriptions.

Usage:
    python3 scripts/vlm_extract_service_parallel.py [--pdf PDF] [--checkpoint JSON]
        [--concurrency 8] [--dpi 150] [--force] [--reconstruct-only]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock

import pdfplumber
import requests

VLM_URL = "http://100.79.90.123:8000/v1/chat/completions"
VLM_KEY = "omlx-your-secret-key"
VLM_MODELS = [
    "jwindle47--chandra-ocr-2-8bit-mlx",
    "mlx-community--VibeThinker-3B-8bit",
]
PROMPT = (
    "Read and transcribe the table on this page. Output the complete table "
    "in HTML format (<table><tr><td>...</td></tr></table>). Preserve all "
    "text exactly as shown, including footnote superscripts. Do not add "
    "commentary."
)

CACHE_DIR = Path("derived/vlm_cache")
PRINT_LOCK = Lock()


def log(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


# ── Page rendering ────────────────────────────────────────────────────────────

def render_page_b64(pdf_path: str, pg_idx: int, dpi: int = 150) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[pg_idx]
        img = page.to_image(resolution=dpi)
        buf = io.BytesIO()
        img.original.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()


# ── VLM call ──────────────────────────────────────────────────────────────────

def ask_vlm(image_b64: str, model: str) -> str:
    for attempt in range(4):
        try:
            resp = requests.post(
                VLM_URL,
                headers={
                    "Authorization": f"Bearer {VLM_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{image_b64}"
                                    },
                                },
                                {"type": "text", "text": PROMPT},
                            ],
                        }
                    ],
                    "max_tokens": 8000,
                    "temperature": 0.1,
                },
                timeout=180,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                if content and len(content) > 20:
                    return content
                log(f"      {model}: empty response, retry")
                time.sleep(5)
            elif resp.status_code == 400 and "prefill_memory" in resp.text:
                log(f"      {model}: memory exceeded, retry in 30s (attempt {attempt+1})")
                time.sleep(30)
            elif resp.status_code == 503:
                log(f"      {model}: 503 overloaded, retry in 15s")
                time.sleep(15)
            else:
                log(f"      {model}: error {resp.status_code}: {resp.text[:150]}")
                return ""
        except Exception as ex:
            log(f"      {model}: exception: {ex}")
            time.sleep(10)
    return ""


# ── Cache ─────────────────────────────────────────────────────────────────────

def cache_key(pdf_path: str, pg_idx: int) -> str:
    stem = Path(pdf_path).stem.replace(" ", "_")
    return f"{stem}_p{pg_idx + 1}"


def get_cache(pdf_path: str, pg_idx: int) -> str | None:
    f = CACHE_DIR / f"{cache_key(pdf_path, pg_idx)}.html"
    if f.exists():
        content = f.read_text(encoding="utf-8")
        if len(content) > 20:
            return content
    return None


def set_cache(pdf_path: str, pg_idx: int, html: str, model: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / f"{cache_key(pdf_path, pg_idx)}.html"
    f.write_text(html, encoding="utf-8")
    meta = CACHE_DIR / f"{cache_key(pdf_path, pg_idx)}.meta"
    meta.write_text(
        json.dumps({"model": model, "ts": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())})
    )


# ── Page range detection ──────────────────────────────────────────────────────

def find_table_pages(pdf_path: str, max_page: int = 60) -> tuple[int, int]:
    """Return (start_idx, end_idx) inclusive 0-based page indices.

    The table for notification 11/2017 starts on page 1 (index 0) in both
    PDFs. End is the page containing "come into force".
    """
    with pdfplumber.open(pdf_path) as pdf:
        end = min(max_page, len(pdf.pages))
        force_idx = end - 1
        for pg_idx in range(end):
            text = (pdf.pages[pg_idx].extract_text() or "").lower()
            if "come into force" in text and pg_idx > 3:
                force_idx = pg_idx
                break
        return 0, force_idx


# ── Parallel fetch ────────────────────────────────────────────────────────────

def fetch_page_task(
    pdf_path: str, pg_idx: int, model: str, dpi: int, force: bool
) -> tuple[int, str, bool]:
    """Fetch one page. Returns (pg_idx, status, was_new)."""
    pg = pg_idx + 1
    if not force:
        cached = get_cache(pdf_path, pg_idx)
        if cached is not None:
            return pg_idx, "cached", False

    try:
        img_b64 = render_page_b64(pdf_path, pg_idx, dpi)
    except Exception as ex:
        log(f"  p{pg}: render FAILED: {ex}")
        return pg_idx, "render_failed", False

    html = ask_vlm(img_b64, model)
    if not html:
        log(f"  p{pg}: VLM FAILED ({model})")
        return pg_idx, "vlm_failed", False

    set_cache(pdf_path, pg_idx, html, model)
    log(f"  p{pg}: OK ({model}, {len(html)} bytes)")
    return pg_idx, "new", True


def parallel_fetch(
    pdf_path: str,
    start_idx: int,
    end_idx: int,
    concurrency: int = 8,
    dpi: int = 150,
    force: bool = False,
) -> dict:
    """Fetch all pages in [start_idx, end_idx] concurrently."""
    page_indices = list(range(start_idx, end_idx + 1))
    n = len(page_indices)
    log(f"\nFetching {n} pages (p{start_idx+1}–p{end_idx+1}) "
        f"with {concurrency} workers, model rotation")

    results = {}
    failed = []

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for i, pg_idx in enumerate(page_indices):
            model = VLM_MODELS[i % len(VLM_MODELS)]
            fut = pool.submit(fetch_page_task, pdf_path, pg_idx, model, dpi, force)
            futures[fut] = pg_idx

        done = 0
        for fut in as_completed(futures):
            pg_idx, status, was_new = fut.result()
            done += 1
            results[pg_idx] = status
            if status in ("vlm_failed", "render_failed"):
                failed.append(pg_idx + 1)
            if done % 10 == 0 or done == n:
                log(f"  Progress: {done}/{n} done, {len(failed)} failed")

    log(f"\nFetch complete: {n - len(failed)}/{n} succeeded, {len(failed)} failed")
    if failed:
        log(f"  Failed pages: {failed}")

    # Retry failed pages sequentially with the other model
    if failed:
        log(f"\nRetrying {len(failed)} failed pages sequentially...")
        for pg_num in failed:
            pg_idx = pg_num - 1
            for model in VLM_MODELS:
                try:
                    img_b64 = render_page_b64(pdf_path, pg_idx, dpi)
                    html = ask_vlm(img_b64, model)
                    if html:
                        set_cache(pdf_path, pg_idx, html, model)
                        log(f"  p{pg_num}: retry OK ({model})")
                        results[pg_idx] = "retried"
                        break
                except Exception as ex:
                    log(f"  p{pg_num}: retry failed with {model}: {ex}")
                    time.sleep(5)

    return {"total": n, "results": results, "failed": [p - 1 for p in failed]}


# ── HTML parsing (reused from existing script) ────────────────────────────────

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] = []
        self._in_cell = False
        self._in_table = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._in_cell = True
            self._current_cell = []
        elif tag == "br" and self._in_cell:
            self._current_cell.append("\n")

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None
        elif tag in ("td", "th") and self._in_cell:
            self._current_row.append("".join(self._current_cell).strip())
            self._in_cell = False
        elif tag == "p" and self._in_cell:
            self._current_cell.append("\n")

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell.append(data)


class TextBlockParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks: list[str] = []
        self._in_table = 0
        self._current: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table += 1
        elif self._in_table == 0 and tag in ("p", "li"):
            self._current = []
        elif tag == "br" and self._current is not None:
            self._current.append("\n")

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            self._in_table -= 1
        elif tag in ("p", "li") and self._current is not None:
            text = "".join(self._current).strip()
            if text:
                self.blocks.append(text)
            self._current = None

    def handle_data(self, data):
        if self._current is not None:
            self._current.append(data)


# ── Row classification helpers ────────────────────────────────────────────────

_RATES = frozenset({
    "0", "0.1", "0.25", "0.75", "1", "1.5", "2.5", "3", "3.75",
    "5", "5.25", "6", "7", "7.5", "9", "12", "14", "18", "28",
    "Nil", "nil",
})

_FOOTNOTE_RE = re.compile(
    r"\b(?:inserted|substituted|omitted|commenced)\s+(?:vide|by)\b", re.I
)


def _normalize_sno(text: str) -> str | None:
    t = str(text or "").strip()
    if not t.startswith("[") and not re.match(r"^\d", t):
        return None
    t = t.lstrip("[").strip()
    t = re.sub(r"[\].)\s]+$", "", t)
    t = re.sub(r"\s+", "", t).upper()
    m = re.match(r"^(\d{1,3})([A-Z]?)$", t)
    if not m:
        return None
    return f"{int(m.group(1))}{m.group(2)}"


def _is_sno(text: str) -> bool:
    return _normalize_sno(text) is not None


def _clean_cell(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _split_rate_cell(text: str) -> tuple[str, str]:
    raw = _clean_cell(str(text or ""))
    if not raw:
        return "", ""
    t = raw.strip().rstrip(".")
    simple = re.sub(r"^\[\s*([0-9]+(?:\.[0-9]+)?|nil|Nil)\s*\]$", r"\1", t)
    if simple in _RATES:
        return simple, ""
    m = re.match(
        r"^\[?\s*([0-9]+(?:\.[0-9]+)?|nil|Nil)\]?"
        r"(?:\s*\d{1,3})?(?:\s*[\].])?\s+(.+)$",
        raw, re.I,
    )
    if m and m.group(1) in _RATES:
        return m.group(1), m.group(2).strip()
    lower = raw.lower()
    if lower.startswith("same rate of central tax"):
        return raw, ""
    return "", ""


def _is_rate(text: str) -> bool:
    rate, remainder = _split_rate_cell(text)
    return bool(rate and not remainder)


def _is_marker_only(text: str) -> bool:
    cleaned = _clean_cell(text)
    if not cleaned or "*" not in cleaned:
        return False
    without_notes = re.sub(r"\d{1,3}", "", cleaned)
    return not re.search(r"[A-Za-z]", without_notes)


def _is_heading_or_section_code(text: str) -> bool:
    clean = _clean_cell(text)
    if not clean:
        return False
    if re.match(r"^(?:heading|section|chapter)\s+\d{2,6}$", clean, re.I):
        return True
    return bool(re.fullmatch(r"\d{4,6}", clean))


def _is_quoted_history_row(row: list[str]) -> bool:
    cells = [_clean_cell(c) for c in row if _clean_cell(c)]
    if not cells:
        return False
    first = cells[0].lstrip()
    return bool(re.match(r'^[“"]\s*(?:\(?[ivxlcdm]+\)|\[?\d{1,3}\b)', first, re.I))


def _parse_tariff_cell(cell: str) -> tuple[str, str]:
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


_HEADER_PATTERNS = (
    r"^(?:s|sl)\.?\s*no\.?$",
    r"^serial\s+(?:no|number)\.?",
    r"chapter\s*,?\s*section\s+or\s+heading",
    r"description\s+of\s+service",
    r"rate\s*\(per\s*cent",
    r"^condition$",
)


def _is_column_number_row(row: list[str]) -> bool:
    cells = [_clean_cell(c) for c in row if _clean_cell(c)]
    if not cells:
        return False
    joined = " ".join(cells)
    compact = re.sub(r"[\s().]+", "", joined)
    return compact in {"12345", "1234"} or (
        len(re.findall(r"\(?\d\)?", joined)) >= 4
        and not re.search(r"[A-Za-z]", joined)
    )


def _is_header_row(row: list[str]) -> bool:
    if row and _is_sno(row[0]):
        return False
    if _is_column_number_row(row):
        return True
    cells = [_clean_cell(c).lower() for c in row]
    if not cells:
        return False
    hits = sum(1 for c in cells if any(re.search(p, c, re.I) for p in _HEADER_PATTERNS))
    return hits >= 2 or (
        "description of service" in " ".join(cells)
        and "rate (per cent" in " ".join(cells)
        and len(row) <= 6
    )


def _classify_row(row: list[str]) -> dict | None:
    if len(row) < 1:
        return None
    if _is_header_row(row):
        return None
    if _is_quoted_history_row(row):
        return None
    first = row[0].strip()
    if _is_sno(first):
        sno = _normalize_sno(first)
        if not sno:
            return None
        cells = row[1:]
        tariff_cell = desc_cell = rate_cell = cond_cell = ""
        if len(cells) >= 4:
            tariff_cell, desc_cell, rate_cell = cells[0], cells[1], cells[2]
            cond_cell = cells[3]
        elif len(cells) == 3:
            tariff_cell, desc_cell, rate_cell = cells
        elif len(cells) == 2:
            tariff_cell, desc_cell = cells
        elif len(cells) == 1:
            desc_cell = cells[0]
        tariff_item, category = _parse_tariff_cell(tariff_cell)
        rate = rate_cell.strip().rstrip(".") if _is_rate(rate_cell) else ""
        return {
            "type": "new", "sno": sno,
            "tariff_item": tariff_item, "category": category,
            "desc": _clean_cell(desc_cell), "rate": rate,
            "condition": _clean_cell(cond_cell),
        }
    return _classify_continuation(row)


def _classify_continuation(row: list[str]) -> dict:
    cells = [c.strip() for c in row]
    if not any(cells):
        return {"type": "continuation", "desc": "", "rate": "", "condition": ""}
    non_empty = [i for i, c in enumerate(cells) if c]
    if len(non_empty) == 1 and non_empty[0] >= len(cells) - 2:
        return {"type": "continuation", "desc": "", "rate": "",
                "condition": _clean_cell(cells[non_empty[0]])}

    rate = ""
    rate_pos = -1
    rate_remainder = ""
    for i in range(len(cells) - 1, -1, -1):
        mr, mrem = _split_rate_cell(cells[i])
        if mr:
            rate = mr
            rate_pos = i
            rate_remainder = mrem
            break

    if rate_pos >= 0:
        desc_parts = [c for c in cells[:rate_pos] if c]
        if (len(desc_parts) >= 2 and _is_heading_or_section_code(desc_parts[0])
                and desc_parts[1].startswith("[")):
            desc_parts = desc_parts[1:]
        cond_parts = ([rate_remainder] if rate_remainder else [])
        cond_parts.extend(c for c in cells[rate_pos + 1:] if c)
    else:
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
        "desc": _clean_cell(" ".join(p for p in desc_parts if not _is_marker_only(p))),
        "rate": rate,
        "condition": _clean_cell(" ".join(p for p in cond_parts if not _is_marker_only(p))),
    }


def _extract_table_rows(html: str) -> list[list[str]]:
    p = TableParser()
    p.feed(html)
    return p.rows


def _extract_text_block_rows(html: str) -> list[list[str]]:
    p = TextBlockParser()
    p.feed(html)
    rows = []
    for block in p.blocks:
        text = _clean_cell(block)
        if not text or _FOOTNOTE_RE.search(text):
            continue
        m = re.match(r"^\[?\s*(\d{1,3})\s*([A-Z])\s*[\].]?\s+(.+)$", text, re.I)
        if m:
            rows.append([f"{int(m.group(1))}{m.group(2).upper()}", "", m.group(3).strip(), "", ""])
    return rows


def _extract_page_rows(html: str) -> list[list[str]]:
    return _extract_table_rows(html) + _extract_text_block_rows(html)


# ── Reconstruction ────────────────────────────────────────────────────────────

def _sno_sort_value(sno: str) -> float:
    normalized = _normalize_sno(sno) or str(sno or "").strip().upper()
    m = re.match(r"^(\d{1,3})([A-Z]?)$", normalized)
    if not m:
        return 99999.0
    v = float(int(m.group(1)))
    if m.group(2):
        v += (ord(m.group(2)) - ord("A") + 1) / 10.0
    return v


def _is_stale_backwards_serial(sno: str, current_sno: str | None) -> bool:
    if not current_sno:
        return False
    normalized = _normalize_sno(sno) or sno
    if normalized in {"2A"}:
        return False
    if re.search(r"[A-Z]$", normalized):
        return False
    return _sno_sort_value(normalized) < _sno_sort_value(current_sno)


def reconstruct_entries(pdf_path: str, start_idx: int, end_idx: int) -> dict:
    """Reconstruct entries from cached VLM HTML pages."""
    stem = Path(pdf_path).stem.replace(" ", "_")
    vlm_entries: dict[str, dict] = {}
    entry_order: list[str] = []
    current_sno: str | None = None
    previous_sno: str | None = None
    leading_conts: list[dict] = []
    failed_pages: list[int] = []

    for pg_idx in range(start_idx, end_idx + 1):
        pg = pg_idx + 1
        f = CACHE_DIR / f"{stem}_p{pg}.html"
        if not f.exists():
            failed_pages.append(pg)
            continue
        html = f.read_text(encoding="utf-8")
        if len(html) < 20:
            failed_pages.append(pg)
            continue

        rows = _extract_page_rows(html)
        page_new = page_cont = 0
        for row in rows:
            if any("come into force" in c.lower() for c in row):
                break
            classified = _classify_row(row)
            if classified is None:
                continue
            if classified["type"] == "new":
                sno = classified["sno"]
                if _is_stale_backwards_serial(sno, current_sno):
                    continue
                # Attach leading continuations to the sno just before this one
                if leading_conts and vlm_entries:
                    # attach to the most recent entry
                    if entry_order:
                        for pending in leading_conts:
                            _append_classified(vlm_entries[entry_order[-1]], pending)
                    leading_conts = []
                if sno not in vlm_entries:
                    vlm_entries[sno] = _new_vlm_entry()
                    entry_order.append(sno)
                _append_classified(vlm_entries[sno], classified)
                if current_sno != sno:
                    previous_sno = current_sno
                current_sno = sno
                page_new += 1
            elif classified["type"] == "continuation" and current_sno:
                _append_classified(vlm_entries[current_sno], classified)
                page_cont += 1
            elif classified["type"] == "continuation":
                leading_conts.append(classified)
                page_cont += 1

        if page_new or page_cont:
            log(f"  p{pg}: new={page_new} cont={page_cont} current={current_sno}")

    if leading_conts and entry_order:
        log(f"  Attaching {len(leading_conts)} trailing continuations to {entry_order[-1]}")
        for pending in leading_conts:
            _append_classified(vlm_entries[entry_order[-1]], pending)

    log(f"\nReconstruction: {len(vlm_entries)} entries, "
        f"order={entry_order[:8]}..., {len(failed_pages)} failed pages")
    return {"vlm_entries": vlm_entries, "entry_order": entry_order,
            "failed_pages": failed_pages}


def _new_vlm_entry() -> dict:
    return {"tariff_item": "", "category": "", "rate": "",
            "desc_parts": [], "cond_parts": []}


def _append_classified(vlm_entry: dict, classified: dict) -> None:
    if classified.get("tariff_item") and not vlm_entry["tariff_item"]:
        vlm_entry["tariff_item"] = classified["tariff_item"]
    if classified.get("category") and not vlm_entry["category"]:
        vlm_entry["category"] = classified["category"]
    if classified.get("rate") and not vlm_entry["rate"]:
        vlm_entry["rate"] = classified["rate"]
    if classified.get("desc") and not _is_marker_only(classified["desc"]):
        vlm_entry["desc_parts"].append(classified["desc"])
    if classified.get("condition") and not _is_marker_only(classified["condition"]):
        vlm_entry["cond_parts"].append(classified["condition"])


def _clean_joined(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _clean_condition(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if re.fullmatch(r"[-–—\]\[\s*0-9]+", text):
        return ""
    return text


# ── Merge into checkpoint ────────────────────────────────────────────────────

def merge_into_checkpoint(
    checkpoint_path: str, reconstruction: dict
) -> dict:
    checkpoint = json.loads(open(checkpoint_path, encoding="utf-8").read())
    instr_key = list(checkpoint["instruments"])[0]
    entries = checkpoint["instruments"][instr_key]["schedules"]["I"]["entries"]
    entry_map = {}
    for e in entries:
        sno = _normalize_sno(str(e["sno"])) or str(e["sno"]).rstrip(".").upper()
        entry_map[sno] = e

    vlm_entries = reconstruction["vlm_entries"]
    enhanced = 0
    replacements = []

    for sno, vlm in vlm_entries.items():
        if sno not in entry_map:
            continue
        e = entry_map[sno]
        desc = _clean_joined(vlm["desc_parts"])
        if vlm["category"]:
            cat = vlm["category"]
            if not desc.startswith(cat[:15]):
                desc = f"{cat} {desc}".strip()
        desc = re.sub(r"\s+", " ", desc).strip()

        changed = False
        old_desc = e.get("description", "")
        old_len = len(old_desc)
        if desc and desc != old_desc:
            e["description"] = desc
            changed = True
            replacements.append({
                "sno": sno, "old_len": old_len, "new_len": len(desc),
                "new_prefix": desc[:120],
            })
        if vlm["tariff_item"] and vlm["tariff_item"] != e.get("tariff_item", ""):
            e["tariff_item"] = vlm["tariff_item"]
            changed = True
        if vlm["rate"] and vlm["rate"] != e.get("rate", ""):
            e["rate"] = vlm["rate"]
            changed = True
        cond = _clean_condition(_clean_joined(vlm["cond_parts"]))
        if cond and cond != e.get("conditions", ""):
            e["conditions"] = cond
            changed = True
        if changed:
            enhanced += 1

    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, ensure_ascii=False)

    log(f"\nMerge: enhanced {enhanced}/{len(vlm_entries)} entries")
    log(f"  (matched {len(set(vlm_entries) & set(entry_map))}/"
        f"{len(entry_map)} checkpoint entries)")
    missing = sorted(set(entry_map) - set(vlm_entries), key=_sno_sort_value)
    if missing:
        log(f"  Missing from VLM: {missing}")

    for r in replacements[:10]:
        log(f"    sno={r['sno']}: {r['old_len']}→{r['new_len']} chars")
    if len(replacements) > 10:
        log(f"    ... and {len(replacements) - 10} more")

    return {"enhanced": enhanced, "replacements": replacements,
            "vlm_count": len(vlm_entries), "checkpoint_count": len(entry_map)}


# ── Main ──────────────────────────────────────────────────────────────────────

def process_pdf(
    pdf_path: str,
    checkpoint_path: str,
    concurrency: int = 8,
    dpi: int = 150,
    force: bool = False,
    reconstruct_only: bool = False,
) -> dict:
    log(f"\n{'='*70}")
    log(f"Processing: {Path(pdf_path).name}")
    log(f"Checkpoint: {Path(checkpoint_path).name}")
    log(f"{'='*70}")

    start_idx, end_idx = find_table_pages(pdf_path)
    log(f"Table pages: p{start_idx+1}–p{end_idx+1} ({end_idx-start_idx+1} pages)")

    if not reconstruct_only:
        fetch_result = parallel_fetch(
            pdf_path, start_idx, end_idx, concurrency, dpi, force
        )

    recon = reconstruct_entries(pdf_path, start_idx, end_idx)
    merge = merge_into_checkpoint(checkpoint_path, recon)

    return {"fetch": locals().get("fetch_result"), "reconstruct": recon,
            "merge": merge}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Parallel VLM extraction of service checkpoint PDFs")
    ap.add_argument("--pdf", help="Path to PDF (default: process both)")
    ap.add_argument("--checkpoint", help="Path to checkpoint JSON")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch all pages even if cached")
    ap.add_argument("--reconstruct-only", action="store_true",
                    help="Skip VLM fetch, only reconstruct from cache")
    args = ap.parse_args()

    if args.pdf and args.checkpoint:
        process_pdf(args.pdf, args.checkpoint, args.concurrency,
                    args.dpi, args.force, args.reconstruct_only)
    else:
        # Process both PDFs
        jobs = [
            ("docs/service_rate_checkpoints/Upto 54th Service Notification.pdf",
             "derived/version_history/rate-schedules/checkpoints/checkpoint_svc_2024-10-24.json"),
            ("docs/service_rate_checkpoints/1_Full booklet_till 55th Council.pdf",
             "derived/version_history/rate-schedules/checkpoints/checkpoint_svc_2025-03-31.json"),
        ]
        for pdf, cp in jobs:
            process_pdf(pdf, cp, args.concurrency, args.dpi,
                        args.force, args.reconstruct_only)
