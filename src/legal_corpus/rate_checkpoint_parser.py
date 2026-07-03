"""Parse CBIC Ready Reckoner rate-schedule PDFs into checkpoint JSON.

Each PDF (a snapshot of the CGST rate schedule "as on" a given date) is
converted into a structured ``RateCheckpoint`` capturing every goods
rate-schedule and exemption instrument, split by schedule and serial number.

The parser shells out to the system ``pdftotext`` command with ``-layout`` so
that the tabular columns are preserved as horizontal whitespace.  Rows are then
delimited by their S.No token (the left-most column) and the remaining columns
(tariff item / description / rate) are reconstructed by splitting each line on
runs of two-or-more spaces -- the inter-column padding produced by the layout
extraction.  This is robust to the wrapped, multi-line cells, the ``Omitted``
rows and the page-break header repetitions that pervade these tables.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .rate_schedule_parser import RateSchedule, ScheduleEntry


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class RateInstrument:
    """A single notification (e.g. 1/2017, 2/2017, 9/2025) within a checkpoint."""

    notification_ref: str
    instrument_type: str = "goods_rate"   # "goods_rate" | "goods_exempt"
    schedules: dict[str, RateSchedule] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "notification_ref": self.notification_ref,
            "instrument_type": self.instrument_type,
            "schedules": {
                sid: {
                    "schedule_id": s.schedule_id,
                    "rate_pct": s.rate_pct,
                    "heading": s.heading,
                    "entries": [_entry_json(e) for e in s.entries],
                }
                for sid, s in self.schedules.items()
            },
        }


@dataclass
class RateCheckpoint:
    checkpoint_date: str          # "2022-05-01"
    source_pdf: str
    instruments: dict[str, RateInstrument] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "checkpoint_date": self.checkpoint_date,
            "source_pdf": self.source_pdf,
            "instruments": {
                ref: instr.to_json() for ref, instr in self.instruments.items()
            },
        }


def _entry_json(e: ScheduleEntry) -> dict:
    return {
        "sno": e.sno,
        "tariff_item": e.tariff_item,
        "description": e.description,
        "is_omitted": e.is_omitted,
    }


# ── regex toolkit ────────────────────────────────────────────────────────────

# any of the dash characters used between "Schedule" and its rate
_DASH = r"[\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212]"

# "1. CGST rates on goods ...", "2. Exempted Goods ...", "3. Effective Compensation Cess ..."
_SECTION_START_RE = re.compile(r"^\s*\d+\.\s+(CGST|Exempted|Effective)", re.I)

# capture the "as on DD.MM.YYYY" date
_DATE_RE = re.compile(r"as on\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)

# capture a notification number such as "No.1/2017" or "Notification No. 09/2025"
_NOTIF_RE = re.compile(r"No\.?\s*0*(\d{1,3})\s*/\s*(\d{4})", re.I)

# rate-bearing schedule heading: "Schedule I – 2.5%" / "[Schedule VII – 0.75%]P6"
_SCHEDULE_RATE_RE = re.compile(
    rf"^\s*\[?\s*Schedule\s+([IVX]+)\s*{_DASH}?\s*([\d.]+)\s*%",
    re.I,
)
# plain heading for exemption / single-table instruments: "Schedule", "SCHEDULE", "Table"
_SCHEDULE_PLAIN_RE = re.compile(r"^\s*(Schedule|SCHEDULE|Table)\s*$")

# A new table row: S.No (1-3 digits -- schedules never exceed ~700 entries, so
# any 4+ digit number is an HSN tariff continuation) with an optional letter
# suffix, optional trailing period and an optional leading "[" (inserted
# entries such as "[153").  The S.No column normally sits in the first ~8
# columns, but a page break makes ``pdftotext -layout`` shift the whole table
# right by ~11 columns, so the leading-whitespace band is widened to 16 and
# ``is_new_row`` re-checks the (now shifted) candidates.  The ``(?=\S)``
# lookahead stops the match before the first content character so the tariff
# cell is not eaten.  Named groups let the caller read the leading indent
# (``lead``), the S.No token (``sno``) and the inter-column gap (``gap``).
_ROW_START_RE = re.compile(
    r"^(?P<lead>\s{0,16})\[?(?P<sno>\d{1,3}[A-Z]*)\.?(?P<gap>\s+)(?=\S)"
)

# "List 1 [See S.No.180 of the Schedule I]" — embedded annexure, ends the last entry
_LIST_MARKER_RE = re.compile(r"^\s*List\s+\d+\s*\[")

# closing paragraph that terminates an instrument's table
_FORCE_END_RE = re.compile(r"^\s*\d+\.\s+This notification shall come into force")
# section-level explanatory note / annexure (left-margin) -- entry-level ones
# live in the description column (indent >= ~20) and must be preserved.
_NOTE_END_RE = re.compile(r"^\s{0,8}(Explanation|ANNEXURE)\b", re.I)

# repeated column-header fragments (only matched at small indent — see _is_header_line)
_HEADER_RE = re.compile(
    r"^(S\.|Sl\.|No\.|Chapter|Heading|Sub[\-\u2013\u2014\s]?heading|"
    r"Tariff\s*item|Description|Rate|Condition|/)",
    re.I,
)
_COLNUM_RE = re.compile(r"^\s*\(\d\)")

# vocabulary of wrapped column-header fragments (e.g. "heading / Tariff", "item")
# that can land at the description indent when a page break splits the header.
_HEADER_VOCAB = {
    "s", "sl", "no", "chapter", "heading", "sub", "subheading", "tariff",
    "item", "description", "rate", "condition", "of", "goods", "head", "ing",
}

# rate tokens living in the rate column
_RATE_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?\s*%$", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_page_number(line: str) -> bool:
    """A centred page-number line.

    Bare-number tariff continuations (e.g. a lone ``0210`` wrapping onto its
    own line) also match a digits-only pattern, but they sit in the tariff
    column (indent < 25); real page numbers are centred (indent >= 25).
    """
    m = re.match(r"^(\s*)\d{1,4}\s*$", line)
    return bool(m) and len(m.group(1)) >= 25


def _is_header_line(line: str) -> bool:
    m = re.match(r"^(\s*)\S", line)
    if not m:
        return False
    if len(m.group(1)) < 16:            # header labels live in the left margin
        if _COLNUM_RE.match(line):      # "(1)   (2)   (3)   (4)"
            return True
        if _HEADER_RE.match(line):
            return True
    # wrapped header fragments can land at the description indent when a page
    # break splits "Chapter / Heading / Sub-heading / Tariff item" across lines.
    # A page break can also place several column headers on one layout row
    # ("S.   Chapter /   Description of Goods   Rate"); such a line is long only
    # because of inter-column padding, so collapse internal whitespace before
    # applying the length guard.  Every word must still be header vocabulary,
    # which keeps real descriptions ("Chapter diagnostic test kits ...") safe.
    s = line.strip()
    if len(re.sub(r"\s+", " ", s)) <= 50 and re.fullmatch(r"[A-Za-z][A-Za-z/\-.\s]*", s):
        words = [w.rstrip(".").lower() for w in re.split(r"[\s/\-]+", s) if w]
        if words and all(w in _HEADER_VOCAB for w in words):
            return True
    return False


# ── pdftotext ────────────────────────────────────────────────────────────────

def _run_pdftotext(pdf_path: Path) -> str:
    """Extract text from a PDF preserving table layout."""
    res = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


# ── section slicing ──────────────────────────────────────────────────────────

def _classify_section(header: str) -> str:
    low = header.lower()
    if "compensation cess" in low:
        return "cess"
    if "exempted" in low:
        return "exempt"
    return "rate"


def _slice_sections(lines: list[str]) -> list[dict]:
    """Return [{num, type, notif_ref, date, body}] for each numbered section."""
    starts: list[tuple[int, str]] = []
    for idx, ln in enumerate(lines):
        m = _SECTION_START_RE.match(ln)
        if m:
            starts.append((idx, ln))
    sections: list[dict] = []
    for i, (idx, header) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        body = lines[idx + 1:end]
        sec_type = _classify_section(header)
        # date + notification number may spill onto the following line
        head_blob = header + "\n" + (lines[idx + 1] if idx + 1 < len(lines) else "")
        date = ""
        dm = _DATE_RE.search(head_blob)
        if dm:
            d, mo, y = dm.groups()
            date = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        notif = ""
        nm = _NOTIF_RE.search(head_blob)
        if nm:
            notif = f"{int(nm.group(1))}/{int(nm.group(2))}"
        sections.append({
            "type": sec_type,
            "notif_ref": notif,
            "date": date,
            "body": body,
        })
    return sections


# ── schedule / entry parsing ─────────────────────────────────────────────────

def _line_segments(line: str, min_col: int = 0) -> list[tuple[int, str]]:
    """Split a layout line into ``(start_col, text)`` column segments.

    Chunks separated by a single space stay in the same segment; a gap of two or
    more spaces (the inter-column padding produced by ``pdftotext -layout``)
    starts a new segment.  Text before *min_col* (e.g. the S.No token) is
    skipped.
    """
    segs: list[tuple[int, str]] = []
    cur_col: Optional[int] = None
    cur_end: int = 0
    cur_words: list[str] = []
    for m in re.finditer(r"\S+", line):
        s, e, t = m.start(), m.end(), m.group()
        if s < min_col:
            continue
        if cur_col is None or (s - cur_end) > 1:
            if cur_col is not None:
                segs.append((cur_col, " ".join(cur_words)))
            cur_col, cur_words, cur_end = s, [t], e
        else:
            cur_words.append(t)
            cur_end = e
    if cur_col is not None:
        segs.append((cur_col, " ".join(cur_words)))
    return segs


_TARIFF_TEXT_RE = re.compile(r"^[\d\s,\[\]/]+$")
# a tariff cell that has abutted the description (single-space gap) looks like
# "2301,2302,2308, Aquatic feed ..." -- a numeric/comma prefix followed by text
_MERGED_TARIFF_RE = re.compile(r"^([\d\s,]+(?:\[[^\]]*\]\s*)?)\s*([A-Za-z(].*)$")


def _split_merged_tariff(seg: str) -> Optional[tuple[str, str]]:
    """If a segment blends a tariff cell with the start of the description
    (because the cell was full and only one space separated the columns), split
    it into ``(tariff, description)``; otherwise return ``None``."""
    m = _MERGED_TARIFF_RE.match(seg)
    if m and m.group(1).strip() and m.group(2).strip():
        return m.group(1).strip(), m.group(2).strip()
    return None


def _is_tariff_text(seg: str) -> bool:
    """Heuristic: does this single segment live in the tariff-item column?"""
    s = seg.strip().strip(".,")
    if not s:
        return False
    low = s.lower()
    if low.startswith("any chapter") or low.startswith("except"):
        return True
    if s.startswith("["):
        return True
    return bool(_TARIFF_TEXT_RE.match(s))


def _is_rate_seg(seg: str) -> bool:
    low = seg.strip().lower()
    return bool(_RATE_TOKEN_RE.match(seg)) or low == "nil"


def _parse_entry(entry_lines: list[str]) -> tuple[ScheduleEntry, Optional[float]]:
    """Reconstruct one ScheduleEntry from its raw layout lines.

    Returns ``(entry, rate_pct_or_None)`` -- the rate is surfaced separately so
    table-style instruments (2/2022, 14/2025) can derive their schedule rate.
    """
    first = entry_lines[0]
    rm = _ROW_START_RE.match(first)
    sno = rm.group("sno") if rm else ""
    sno_end = rm.end() if rm else 0

    tariff_parts: list[str] = []
    desc_parts: list[str] = []
    rate_pct: Optional[float] = None

    for i, ln in enumerate(entry_lines):
        min_col = sno_end if i == 0 else 0
        segs = _line_segments(ln, min_col)
        # everything from the rate column onward (rate + any condition column)
        # is not tariff/description text -- drop it.
        rate_idx = next(
            (k for k, (_, t) in enumerate(segs) if _is_rate_seg(t)), None
        )
        if rate_pct is None and rate_idx is not None:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", segs[rate_idx][1])
            if m:
                rate_pct = float(m.group(1))
        content = segs if rate_idx is None else segs[:rate_idx]
        if not content:
            continue
        first_col, first_txt = content[0]
        # the tariff cell may have abutted the description (single-space gap)
        merged = (
            _split_merged_tariff(first_txt)
            if re.search(r"[A-Za-z]", first_txt)
            else None
        )
        if merged:
            tariff_parts.append(merged[0])
            if merged[1]:
                desc_parts.append(merged[1])
            for _, t in content[1:]:
                desc_parts.append(t)
        elif len(content) >= 2:
            # multi-column line: leftmost segment is the tariff cell
            tariff_parts.append(first_txt)
            for _, t in content[1:]:
                desc_parts.append(t)
        else:
            # single segment: decide by content (tariff continuation vs text)
            if _is_tariff_text(first_txt):
                tariff_parts.append(first_txt)
            else:
                desc_parts.append(first_txt)

    tariff = _clean(" ".join(tariff_parts))
    description = _clean(" ".join(desc_parts))

    joined = _clean(f"{tariff} {description}").lower().rstrip(".")
    if joined == "omitted":
        return ScheduleEntry(sno=sno, tariff_item="", description="", is_omitted=True), rate_pct

    return (
        ScheduleEntry(sno=sno, tariff_item=tariff, description=description, is_omitted=False),
        rate_pct,
    )


def _parse_instrument(body: list[str], notif_ref: str, sec_type: str) -> RateInstrument:
    instrument_type = "goods_exempt" if sec_type == "exempt" else "goods_rate"
    instr = RateInstrument(notification_ref=notif_ref, instrument_type=instrument_type)

    schedule: Optional[RateSchedule] = None
    entry_buf: Optional[list[str]] = None
    last_sno_num = 0                 # for the sequential row-start guard
    saw_rate_heading = False        # rate-bearing "Schedule I – 2.5%" seen?
    saw_plain_heading = False       # plain "Schedule"/"Table" heading seen?

    def flush_entry() -> None:
        nonlocal entry_buf
        if entry_buf and schedule is not None:
            entry, rate_pct = _parse_entry(entry_buf)
            schedule.entries.append(entry)
            if schedule.rate_pct == 0.0 and rate_pct is not None:
                schedule.rate_pct = rate_pct
        entry_buf = None

    def flush_schedule() -> None:
        nonlocal schedule, last_sno_num
        flush_entry()
        if schedule is not None:
            instr.schedules[schedule.schedule_id] = schedule
        schedule = None
        last_sno_num = 0

    def is_new_row(line: str) -> bool:
        """A real new row, gated by the sequential S.No guard.

        A bare numeric token (no period, no letter suffix, no leading "[") that
        does not advance past the last seen S.No is a tariff continuation (e.g.
        a chapter code) masquerading as a row start.

        Page breaks are a second hazard: ``pdftotext -layout`` re-emits the
        column headers and shifts every following data row ~11 columns to the
        right.  In that relaxed indent band (``lead`` > 8) an unmarked bare
        number followed by a column gap is almost always a wrapped tariff /
        description fragment (e.g. "99 added flavouring ...", "90 water ...",
        "71 government ..."), so only *marked* S.No tokens (period, letter
        suffix or leading "[") are accepted there.
        """
        nonlocal last_sno_num
        m = _ROW_START_RE.match(line)
        if not m:
            return False
        tok = m.group("sno")
        num = int(re.match(r"(\d+)", tok).group(1))
        prefix = line[: m.start("gap")]
        marked = "." in prefix or "[" in prefix or bool(re.search(r"[A-Z]", tok))
        if len(m.group("lead")) > 8 and not marked:
            return False
        if marked or num > last_sno_num:
            last_sno_num = num
            return True
        return False

    for line in body:
        if _FORCE_END_RE.match(line) or _NOTE_END_RE.match(line):
            flush_schedule()                          # closing paragraph / note
            break
        if _LIST_MARKER_RE.match(line):               # embedded annexure list
            flush_entry()
            continue
        m_rate = _SCHEDULE_RATE_RE.match(line)
        if m_rate:
            flush_schedule()
            saw_rate_heading = True
            sid, rate_str = m_rate.group(1), m_rate.group(2)
            schedule = RateSchedule(
                schedule_id=sid,
                rate_pct=float(rate_str),
                heading=_clean(line),
            )
            entry_buf = None
            continue
        # A plain "Schedule"/"Table" line is only a real heading for the
        # single-table instruments (exemption, 2/2022, 14/2025).  Inside a
        # multi-schedule rate section such a line is just a description word
        # that happened to wrap onto its own line (e.g. "...appended to this
        # Schedule"), so it must not start a new schedule.
        if _SCHEDULE_PLAIN_RE.match(line) and not saw_rate_heading and not saw_plain_heading:
            flush_schedule()
            saw_plain_heading = True
            schedule = RateSchedule(
                schedule_id="I",
                rate_pct=0.0,
                heading=_clean(line),
            )
            entry_buf = None
            continue
        if schedule is None:                          # preamble before first heading
            continue
        if _is_page_number(line):                     # centred page-number artefact
            continue
        if _is_header_line(line):                     # repeated column headers
            continue
        if is_new_row(line):                          # new row
            flush_entry()
            entry_buf = [line]
        elif entry_buf is not None:
            entry_buf.append(line)
        # else: stray line with no active entry (e.g. after a list marker) → drop

    flush_schedule()
    return instr


# ── public API ───────────────────────────────────────────────────────────────

def parse_checkpoint_pdf(pdf_path: str | Path) -> dict:
    """Parse a single CBIC ready-reckoner PDF into a checkpoint dict."""
    pdf_path = Path(pdf_path)
    text = _run_pdftotext(pdf_path)
    lines = text.splitlines()

    sections = _slice_sections(lines)

    checkpoint_date = ""
    # prefer a repository-relative path for portability of the output JSON
    try:
        cwd = Path.cwd()
        source_pdf = str(pdf_path.resolve().relative_to(cwd))
    except ValueError:
        source_pdf = str(pdf_path)

    instruments: dict[str, RateInstrument] = {}
    for sec in sections:
        if not checkpoint_date and sec["date"]:
            checkpoint_date = sec["date"]
        if not sec["notif_ref"]:
            continue
        # Compensation Cess sections map to the ``-cc-rate`` instrument key
        # (e.g. "1/2017" → "1/2017-cc-rate") so they line up with the
        # materializer / compiler target identifiers.
        notif_ref = sec["notif_ref"]
        if sec["type"] == "cess":
            notif_ref = f"{notif_ref}-cc-rate"
        elif sec["type"] in ("rate", "exempt"):
            notif_ref = f"{notif_ref}-ct-rate"
        instr = _parse_instrument(sec["body"], notif_ref, sec["type"])
        if instr.schedules:
            instruments[notif_ref] = instr

    cp = RateCheckpoint(
        checkpoint_date=checkpoint_date,
        source_pdf=source_pdf,
        instruments=instruments,
    )
    return cp.to_json()


def parse_all_checkpoints(checkpoint_dir: str | Path) -> list[dict]:
    """Parse every PDF in *checkpoint_dir* into a list of checkpoint dicts."""
    checkpoint_dir = Path(checkpoint_dir)
    pdfs = sorted(checkpoint_dir.glob("*.pdf"))
    return [parse_checkpoint_pdf(p) for p in pdfs]


def save_checkpoint(checkpoint: dict, output_dir: str | Path) -> Path:
    """Write a checkpoint dict to *output_dir/checkpoint_YYYY-MM-DD.json*."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date = checkpoint.get("checkpoint_date", "unknown")
    out = output_dir / f"checkpoint_{date}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, ensure_ascii=False)
    return out


# ── CLI entry ────────────────────────────────────────────────────────────────

_PDF_DIR = Path("docs/CBIC_ready_reckoner_rates")
_OUT_DIR = Path("derived/version_history/rate-schedules/checkpoints")


def _print_summary(checkpoint: dict) -> None:
    date = checkpoint["checkpoint_date"]
    print(f"\n=== Checkpoint {date} ({checkpoint['source_pdf']}) ===")
    for ref, instr in checkpoint["instruments"].items():
        total = 0
        print(f"  instrument {ref} ({instr['instrument_type']}):")
        for sid, sched in instr["schedules"].items():
            n = len(sched["entries"])
            total += n
            omitted = sum(1 for e in sched["entries"] if e["is_omitted"])
            print(
                f"    Schedule {sid} ({sched['rate_pct']}%): "
                f"{n} entries ({omitted} omitted)"
            )
        print(f"    -> {total} entries total")


if __name__ == "__main__":
    checkpoints = parse_all_checkpoints(_PDF_DIR)
    for cp in checkpoints:
        out = save_checkpoint(cp, _OUT_DIR)
        print(f"saved {out}")
    for cp in checkpoints:
        _print_summary(cp)
