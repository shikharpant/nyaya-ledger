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


class TextBlockParser(HTMLParser):
    """Collect non-table paragraph/list text from VLM HTML output."""

    def __init__(self):
        super().__init__()
        self.blocks: list[str] = []
        self._in_table = 0
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag == "table":
            self._in_table += 1
        elif self._in_table == 0 and tag in ("p", "li"):
            self._current = []
        elif tag == "br" and self._current is not None:
            self._current.append("\n")

    def handle_endtag(self, tag: str):
        if tag == "table" and self._in_table:
            self._in_table -= 1
        elif tag in ("p", "li") and self._current is not None:
            text = "".join(self._current).strip()
            if text:
                self.blocks.append(text)
            self._current = None

    def handle_data(self, data: str):
        if self._current is not None:
            self._current.append(data)


# ── cell / row helpers ───────────────────────────────────────────────────────

def _normalize_sno(text: str) -> str | None:
    """Return a normalized serial number like ``31A`` or ``None``."""
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
    """Check if text is a serial number like '7', '2A', '[31 A', '[38.'."""
    return _normalize_sno(text) is not None


def _is_rate(text: str) -> bool:
    """Check if text is a valid GST rate token."""
    rate, remainder = _split_rate_cell(text)
    return bool(rate and not remainder)


def _clean_cell(text: str) -> str:
    """Strip residual HTML tags and normalise whitespace to single spaces."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_footnote_suffix(text: str) -> str:
    """Remove compact numeric footnote suffixes from a short cell."""
    return re.sub(r"(?<=\])\s*\d{1,3}$", "", text).strip()


def _split_rate_cell(text: str) -> tuple[str, str]:
    """Return ``(rate, remainder)`` for a rate cell.

    VLM often merges the rate and condition columns into one cell, e.g.
    ``"2.5 Provided that credit..."``.  The parser needs to keep the rate out
    of the description while preserving the trailing condition text.
    """
    raw = _clean_cell(str(text or ""))
    if not raw:
        return "", ""

    t = _strip_footnote_suffix(raw).strip().rstrip(".")
    simple = re.sub(r"^\[\s*([0-9]+(?:\.[0-9]+)?|nil|Nil)\s*\]$", r"\1", t)
    if simple in _RATES:
        return simple, ""

    m = re.match(
        r"^\[?\s*([0-9]+(?:\.[0-9]+)?|nil|Nil)\]?"
        r"(?:\s*\d{1,3})?(?:\s*[\].])?\s+(.+)$",
        raw,
        re.I,
    )
    if m and m.group(1) in _RATES:
        return m.group(1), m.group(2).strip()

    lower = raw.lower()
    if lower.startswith("same rate of central tax"):
        return raw, ""
    if re.match(r"^65\s+per\s+cent\b", lower):
        return raw, ""
    return "", ""


def _is_marker_only(text: str) -> bool:
    """True for omission-marker cells such as ``[***]`` or ``***]66``."""
    cleaned = _clean_cell(text)
    if not cleaned or "*" not in cleaned:
        return False
    without_notes = re.sub(r"\d{1,3}", "", cleaned)
    return not re.search(r"[A-Za-z]", without_notes)


def _is_heading_or_section_code(text: str) -> bool:
    """Detect tariff-code cells that sometimes drift into continuation desc."""
    clean = _clean_cell(text)
    if not clean:
        return False
    if re.match(r"^(?:heading|section|chapter)\s+\d{2,6}$", clean, re.I):
        return True
    return bool(re.fullmatch(r"\d{4,6}", clean))


def _is_quoted_history_row(row: list[str]) -> bool:
    """Detect quoted prior-law rows embedded in VLM table output.

    The CBIC booklet prints some historical substituted/omitted rows in
    footnotes.  VLM sometimes emits those footnotes as ordinary table rows,
    typically beginning with an opening quote before an item marker or old
    serial number.  They should not be merged into the current checkpoint row.
    """
    cells = [_clean_cell(c) for c in row if _clean_cell(c)]
    if not cells:
        return False
    first = cells[0].lstrip()
    return bool(re.match(r'^[“"]\s*(?:\(?[ivxlcdm]+\)|\[?\d{1,3}\b)', first, re.I))


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


_HEADER_PATTERNS = (
    r"^(?:s|sl)\.?\s*no\.?$",
    r"^serial\s+(?:no|number)\.?",
    r"chapter\s*,?\s*section\s+or\s+heading",
    r"description\s+of\s+service",
    r"rate\s*\(per\s*cent",
    r"^condition$",
)


def _is_column_number_row(row: list[str]) -> bool:
    """Detect OCR rows that only repeat printed column numbers."""
    cells = [_clean_cell(c) for c in row if _clean_cell(c)]
    if not cells:
        return False
    joined = " ".join(cells)
    tokens = re.findall(r"\(?\d\)?", joined)
    compact = re.sub(r"[\s().]+", "", joined)
    return compact in {"12345", "1234"} or (
        len(tokens) >= 4 and not re.search(r"[A-Za-z]", joined)
    )


def _is_header_row(row: list[str]) -> bool:
    """Detect repeated column-header rows."""
    if row and _is_sno(row[0]):
        return False
    if _is_column_number_row(row):
        return True
    cells = [_clean_cell(c).lower() for c in row]
    if not cells:
        return False
    hits = 0
    for cell in cells:
        if any(re.search(pat, cell, re.I) for pat in _HEADER_PATTERNS):
            hits += 1
    joined = " ".join(cells)
    if re.match(r"^(?:s|sl)\.?\s*no\.?", cells[0], re.I):
        return True
    return hits >= 2 or (
        "description of service" in joined
        and "rate (per cent" in joined
        and len(row) <= 6
    )


def _classify_row(row: list[str]) -> dict | None:
    """Classify a parsed table row.

    Returns a dict with ``type`` of either ``"new"`` (S.No present) or
    ``"continuation"`` (empty / non-S.No first cell).  ``None`` for rows
    that carry no useful data.
    """
    if len(row) < 1:
        return None
    if _is_header_row(row):
        return None
    if _is_quoted_history_row(row):
        return None

    first = row[0].strip()

    # ── new S.No entry ──────────────────────────────────────────────────
    if _is_sno(first):
        sno = _normalize_sno(first)
        if not sno:
            return None
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

    non_empty_positions = [idx for idx, cell in enumerate(cells) if cell]
    if len(non_empty_positions) == 1 and non_empty_positions[0] >= len(cells) - 2:
        return {
            "type": "continuation",
            "desc": "",
            "rate": "",
            "condition": _clean_cell(cells[non_empty_positions[0]]),
        }

    # Find rightmost rate-like cell.  The cell may include condition text after
    # a leading rate token, so retain the trailing text as condition material.
    rate = ""
    rate_pos = -1
    rate_remainder = ""
    for i in range(len(cells) - 1, -1, -1):
        maybe_rate, maybe_remainder = _split_rate_cell(cells[i])
        if maybe_rate:
            rate = maybe_rate
            rate_pos = i
            rate_remainder = maybe_remainder
            break

    if rate_pos >= 0:
        desc_parts = [c for c in cells[:rate_pos] if c]

        if (
            len(desc_parts) >= 2
            and _is_heading_or_section_code(desc_parts[0])
            and desc_parts[1].startswith("[")
        ):
            desc_parts = desc_parts[1:]

        cond_parts = [rate_remainder] if rate_remainder else []
        cond_parts.extend(c for c in cells[rate_pos + 1:] if c)
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
        "desc": _clean_cell(" ".join(p for p in desc_parts if not _is_marker_only(p))),
        "rate": rate,
        "condition": _clean_cell(" ".join(p for p in cond_parts if not _is_marker_only(p))),
    }


def _extract_table_rows(html_text: str) -> list[list[str]]:
    """Parse all ``<table>`` rows from VLM HTML output."""
    parser = TableParser()
    parser.feed(html_text)
    return parser.rows


def _extract_text_block_rows(html_text: str) -> list[list[str]]:
    """Parse standalone alpha-suffixed serial paragraphs outside tables.

    Some VLM pages return ordinary positioned text instead of a table.  The
    service checkpoint uses this shape for paragraph-style insertions such as
    ``[2A. Where ...]``.  Numeric prose paragraphs are deliberately ignored to
    avoid treating notification paragraphs like ``5. This notification...`` as
    table rows.
    """
    parser = TextBlockParser()
    parser.feed(html_text)
    rows: list[list[str]] = []
    for block in parser.blocks:
        text = _clean_cell(block)
        if not text or _FOOTNOTE_RE.search(text):
            continue
        match = re.match(r"^\[?\s*(\d{1,3})\s*([A-Z])\s*[\].]?\s+(.+)$", text, re.I)
        if not match:
            continue
        sno = f"{int(match.group(1))}{match.group(2).upper()}"
        rows.append([sno, "", match.group(3).strip(), "", ""])
    return rows


def _extract_page_rows(html_text: str) -> list[list[str]]:
    """Parse table rows plus supported standalone serial text blocks."""
    return _extract_table_rows(html_text) + _extract_text_block_rows(html_text)


_FOOTNOTE_RE = re.compile(
    r"\b(?:inserted|substituted|omitted|commenced)\s+(?:vide|by)\b",
    re.I,
)


def _sno_sort_value(sno: str) -> float:
    normalized = _normalize_sno(sno) or str(sno or "").strip().upper()
    m = re.match(r"^(\d{1,3})([A-Z]?)$", normalized)
    if not m:
        return 99999.0
    value = float(int(m.group(1)))
    if m.group(2):
        value += (ord(m.group(2)) - ord("A") + 1) / 10.0
    return value


def _previous_checkpoint_sno(sno: str, checkpoint_snos: list[str]) -> str | None:
    target = _sno_sort_value(sno)
    prior = [s for s in checkpoint_snos if _sno_sort_value(s) < target]
    if not prior:
        return None
    return max(prior, key=_sno_sort_value)


def _is_stale_backwards_serial(sno: str, current_sno: str | None) -> bool:
    if not current_sno:
        return False
    normalized = _normalize_sno(sno) or sno
    if normalized in {"2A"}:
        return False
    if re.search(r"[A-Z]$", normalized):
        return False
    return _sno_sort_value(normalized) < _sno_sort_value(current_sno)


def _new_vlm_entry() -> dict:
    return {
        "tariff_item": "",
        "category": "",
        "rate": "",
        "desc_parts": [],
        "cond_parts": [],
    }


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


def _insert_entry_sorted(entries: list[dict], entry: dict) -> None:
    target = _sno_sort_value(entry.get("sno", ""))
    for idx, existing in enumerate(entries):
        if _sno_sort_value(existing.get("sno", "")) > target:
            entries.insert(idx, entry)
            return
    entries.append(entry)


_SERVICE_HINT_TERMS = {
    "accommodation", "beverage", "business", "construction", "courier",
    "domestic", "education", "electricity", "extraterritorial", "financial",
    "goods", "health", "hotel", "leasing", "manufacturing", "membership",
    "passenger", "postal", "production", "rental", "social", "support",
    "technical", "telecommunications", "transport", "water",
}


def _extract_service_hints(text: str) -> set[str]:
    normalized_text = re.sub(r"[^a-z0-9()]+", " ", text.lower())
    hints: set[str] = set()

    for phrase in re.findall(r"\(([^()]*(?:service|services)[^()]*)\)", text, re.I):
        phrase = re.sub(r"^\s*[ivxlcdm]+\s*$", "", phrase, flags=re.I).strip()
        normalized = re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip()
        tokens = normalized.split()
        if len(tokens) >= 3 and "services" in tokens:
            hints.add(normalized)

    for match in re.finditer(
        r"\b((?:[a-z][a-z0-9]*\s+){0,4}[a-z][a-z0-9]*\s+services?)\b",
        normalized_text,
    ):
        normalized = re.sub(r"\s+", " ", match.group(1)).strip()
        tokens = normalized.split()
        if len(tokens) >= 2 and any(t in _SERVICE_HINT_TERMS for t in tokens):
            hints.add(normalized)
    return hints


def _service_category_hints(vlm_entry: dict | None) -> set[str]:
    """Extract normalized service category labels from an accumulated entry."""
    if not vlm_entry:
        return set()
    candidates = [vlm_entry.get("category", "")]
    candidates.extend(vlm_entry.get("desc_parts", [])[:16])
    hints: set[str] = set()
    for candidate in candidates:
        hints.update(_extract_service_hints(candidate))
    return hints


def _matches_category_hint(text: str, hint: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return bool(hint and hint in normalized)


def _should_reroute_to_previous(
    classified: dict,
    previous_entry: dict | None,
    current_entry: dict | None,
) -> bool:
    """Return true for continuation rows emitted after the next entry starts.

    Some VLM table pages preserve visual reading order rather than row order,
    leaving the final continuation of entry N immediately after the first row
    of entry N+1.  If the row names the previous service category and not the
    current one, attach it back to the previous entry.
    """
    desc = classified.get("desc", "")
    if not desc:
        return False
    if not current_entry:
        return False
    if not current_entry.get("desc_parts") and not current_entry.get("category"):
        return False
    if re.match(r"^\d{4}\b", desc):
        return False
    previous_hints = _service_category_hints(previous_entry)
    current_hints = _service_category_hints(current_entry)
    if not previous_hints:
        return False
    previous_match = any(_matches_category_hint(desc, hint) for hint in previous_hints)
    current_match = any(_matches_category_hint(desc, hint) for hint in current_hints)
    return previous_match and not current_match


def _write_manifest(manifest_path: str | Path, extraction: dict) -> None:
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    else:
        manifest = {}
    extractions = manifest.get("extractions", [])
    key = extraction["checkpoint_json_path"]
    extractions = [e for e in extractions if e.get("checkpoint_json_path") != key]
    extractions.append(extraction)
    manifest.update({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vlm_model": VLM_MODEL,
        "vlm_url": VLM_URL,
        "cache_dir": str(CACHE_DIR),
        "extractions": sorted(extractions, key=lambda e: e.get("checkpoint_json_path", "")),
    })
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


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

def enhance_checkpoint(
    checkpoint_path: str,
    pdf_path: str,
    max_page: int = 50,
    manifest_path: str | Path | None = None,
) -> dict:
    """Enhance a checkpoint JSON with VLM-reconstructed descriptions.

    Processes every table page in the PDF, accumulates description and
    condition text across continuation rows, and merges the result back
    into the checkpoint keyed by S.No.
    """
    checkpoint = json.loads(open(checkpoint_path, encoding="utf-8").read())
    instr_key = list(checkpoint["instruments"])[0]
    entries = checkpoint["instruments"][instr_key]["schedules"]["I"]["entries"]
    entry_map = {(_normalize_sno(e["sno"]) or e["sno"].rstrip(".").upper()): e for e in entries}
    checkpoint_snos = list(entry_map)

    with pdfplumber.open(pdf_path) as pdf:
        start_pg, end_pg = _find_table_pages(pdf, max_page)
        n_pages = end_pg - start_pg + 1
        print(f"Table pages: {start_pg + 1}–{end_pg + 1} ({n_pages} pages)")

        # Accumulator:  sno -> dict with accumulated fields
        vlm_entries: dict[str, dict] = {}
        entry_order: list[str] = []
        current_sno: str | None = None
        previous_sno: str | None = None
        failed_pages: list[int] = []
        leading_continuations: list[dict] = []
        leading_continuation_target: str | None = None
        page_reports: list[dict] = []
        stale_serials: list[dict] = []
        rerouted_continuations: list[dict] = []

        for pg_idx in range(start_pg, end_pg + 1):
            page_text = pdf.pages[pg_idx].extract_text() or ""
            print(f"\nPage {pg_idx + 1}/{end_pg + 1}:", end=" ", flush=True)
            page_report = {
                "page": pg_idx + 1,
                "cache_key": _cache_key(pdf_path, pg_idx),
                "cache_file": str(CACHE_DIR / f"{_cache_key(pdf_path, pg_idx)}.html"),
                "cache_status": "",
                "failed": False,
                "raw_row_count": 0,
                "parsed_new_count": 0,
                "parsed_continuation_count": 0,
                "skipped_row_count": 0,
                "serials": [],
                "stale_serials": [],
                "rerouted_continuations": [],
                "hit_stop_marker": False,
            }

            # Try cache first
            html_response = _get_cache(pdf_path, pg_idx)
            if html_response is not None:
                print("(cached)", end=" ", flush=True)
                page_report["cache_status"] = "cached"
            else:
                time.sleep(5)
                img_b64 = _render_page_b64(pdf, pg_idx)
                html_response = _ask_vlm(img_b64)
                if not html_response:
                    print("FAILED")
                    failed_pages.append(pg_idx + 1)
                    page_report["cache_status"] = "failed"
                    page_report["failed"] = True
                    page_reports.append(page_report)
                    continue
                _set_cache(pdf_path, pg_idx, html_response)
                print("(new)", end=" ", flush=True)
                page_report["cache_status"] = "new"

            rows = _extract_page_rows(html_response)
            page_report["raw_row_count"] = len(rows)
            page_new = 0
            page_cont = 0
            for row in rows:
                # Stop at end-of-notification marker inside table
                if any("come into force" in c.lower() for c in row):
                    page_report["hit_stop_marker"] = True
                    break
                classified = _classify_row(row)
                if classified is None:
                    page_report["skipped_row_count"] += 1
                    continue

                if classified["type"] == "new":
                    sno = classified["sno"]
                    if _is_stale_backwards_serial(sno, current_sno):
                        stale = {"page": pg_idx + 1, "sno": sno, "row": row}
                        stale_serials.append(stale)
                        page_report["stale_serials"].append(sno)
                        page_report["skipped_row_count"] += 1
                        continue

                    if leading_continuations:
                        target = _previous_checkpoint_sno(sno, checkpoint_snos)
                        if target:
                            if target not in vlm_entries:
                                vlm_entries[target] = _new_vlm_entry()
                                entry_order.append(target)
                            for pending in leading_continuations:
                                _append_classified(vlm_entries[target], pending)
                            leading_continuation_target = target
                        leading_continuations = []

                    if sno not in vlm_entries:
                        vlm_entries[sno] = _new_vlm_entry()
                        entry_order.append(sno)
                    _append_classified(vlm_entries[sno], classified)
                    if current_sno != sno:
                        previous_sno = current_sno
                    current_sno = sno
                    page_report["serials"].append(sno)
                    page_new += 1

                elif classified["type"] == "continuation" and current_sno:
                    target_sno = current_sno
                    if (
                        previous_sno
                        and _should_reroute_to_previous(
                            classified,
                            vlm_entries.get(previous_sno),
                            vlm_entries.get(current_sno),
                        )
                    ):
                        target_sno = previous_sno
                        reroute = {
                            "page": pg_idx + 1,
                            "from_sno": current_sno,
                            "to_sno": previous_sno,
                            "desc_prefix": classified.get("desc", "")[:160],
                        }
                        rerouted_continuations.append(reroute)
                        page_report["rerouted_continuations"].append(reroute)
                    v = vlm_entries[target_sno]
                    _append_classified(v, classified)
                    page_cont += 1
                elif classified["type"] == "continuation":
                    leading_continuations.append(classified)
                    page_cont += 1

            print(f"new={page_new} cont={page_cont}")
            page_report["parsed_new_count"] = page_new
            page_report["parsed_continuation_count"] = page_cont
            page_reports.append(page_report)

    if failed_pages:
        print(f"\nFailed pages: {failed_pages}")

    # ── merge into checkpoint ───────────────────────────────────────────
    enhanced = 0
    inserted = 0
    replacements: list[dict] = []
    for sno, vlm in vlm_entries.items():
        if sno not in entry_map:
            if sno not in {"2A", "31A"}:
                continue
            new_entry = {
                "sno": sno,
                "tariff_item": vlm.get("tariff_item", ""),
                "description": "",
                "rate": vlm.get("rate", ""),
                "is_omitted": False,
            }
            _insert_entry_sorted(entries, new_entry)
            entry_map[sno] = new_entry
            inserted += 1
        e = entry_map[sno]

        desc = _clean_joined(vlm["desc_parts"])
        if vlm["category"]:
            cat = vlm["category"]
            if not desc.startswith(cat[:15]):
                desc = f"{cat} {desc}".strip()
        desc = re.sub(r"\s+", " ", desc).strip()

        changed = False
        old_desc = e.get("description", "")
        if desc and desc != old_desc:
            e["description"] = desc
            changed = True
            replacements.append({
                "sno": sno,
                "old_description_length": len(old_desc),
                "new_description_length": len(desc),
                "new_description_prefix": desc[:160],
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

    # Save
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, ensure_ascii=False)
    print(f"\nEnhanced {enhanced}/{len(vlm_entries)} VLM entries "
          f"(matched {len([s for s in vlm_entries if s in entry_map])}/"
          f"{len(entry_map)} checkpoint entries, inserted {inserted})")

    cache_files = [
        str(CACHE_DIR / f"{_cache_key(pdf_path, pg_idx)}.html")
        for pg_idx in range(start_pg, end_pg + 1)
    ]
    manifest = {
        "source_pdf_path": pdf_path,
        "checkpoint_json_path": checkpoint_path,
        "vlm_model": VLM_MODEL,
        "vlm_url": VLM_URL,
        "cache_family": Path(pdf_path).stem.replace(" ", "_"),
        "cache_dir": str(CACHE_DIR),
        "detected_page_list": list(range(start_pg + 1, end_pg + 2)),
        "detected_start_page": start_pg + 1,
        "detected_end_page": end_pg + 1,
        "stop_boundary_page": end_pg + 1,
        "cache_files": cache_files,
        "failed_pages": failed_pages,
        "failed_page_status": [
            {"page": p, "status": "failed_after_vlm_retries"} for p in failed_pages
        ],
        "page_reports": page_reports,
        "leading_continuation_target": leading_continuation_target,
        "stale_serials": stale_serials,
        "rerouted_continuations": rerouted_continuations,
        "description_replacements": replacements,
        "serial_coverage": {
            "checkpoint_serials": sorted(entry_map, key=_sno_sort_value),
            "vlm_serials": sorted(vlm_entries, key=_sno_sort_value),
            "checkpoint_entry_count": len(entry_map),
            "vlm_entry_count": len(vlm_entries),
            "matched_serials": sorted(set(entry_map) & set(vlm_entries), key=_sno_sort_value),
            "missing_from_vlm": sorted(set(entry_map) - set(vlm_entries), key=_sno_sort_value),
            "extra_from_vlm": sorted(set(vlm_entries) - set(entry_map), key=_sno_sort_value),
        },
    }
    if manifest_path:
        _write_manifest(manifest_path, manifest)
        print(f"Manifest updated: {manifest_path}")

    # Summary
    for e in entries:
        print(f"  sno={e['sno']:>4} len={len(e['description']):>5}  "
              f"desc={e['description'][:80]}")
    return manifest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Enhance service checkpoint with VLM full-page descriptions")
    parser.add_argument("checkpoint", help="Path to checkpoint JSON")
    parser.add_argument("pdf", help="Path to source PDF")
    parser.add_argument("--max-page", type=int, default=50)
    parser.add_argument("--manifest", help="Path to aggregate extraction manifest JSON")
    args = parser.parse_args()
    enhance_checkpoint(args.checkpoint, args.pdf, args.max_page, args.manifest)
