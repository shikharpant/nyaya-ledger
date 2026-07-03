"""Compile CT(Rate) amending notifications into structured rate-amendment events.

Two-tier compiler (same architecture as Act/Rules):
  Tier 1 — deterministic regex patterns for the formulaic amendment language
  Tier 2 — (future) LLM fallback for complex compound clauses

The compiler reads each amending notification XML, identifies the target
notification and schedule being amended, parses each clause into a
RateAmendmentEvent, and writes all events as JSONL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET


# ── event model ──────────────────────────────────────────────────────────────

@dataclass
class RateAmendmentEvent:
    event_id: str
    operation: str          # RATE_OMIT_ENTRIES, RATE_INSERT_ENTRIES, etc.
    target_notification: str  # "1/2017-ct-rate", "9/2025-ct-rate", ...
    target_schedule: str    # "I", "II", ...
    payload: dict[str, Any]
    effective_date: str     # YYYY-MM-DD
    publication_date: str
    source_notification: str  # amending notification's canonical_id
    source_cbic_no: str     # "24/2018-Central Tax (Rate)"
    clause_ref: str = ""    # "(b)(i)", "(c)(ii)", etc.
    status: str = "validated"
    review_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── helpers ──────────────────────────────────────────────────────────────────

# Precise S.No-list capture: "72", "3, 4, 5 and 6", "26A to 26L"
# Avoids the old [\d,\sandA-Z]+? which truncated multi-digit S.Nos (72→7)
# Separators between consecutive numbers may be a comma OR bare whitespace to
# tolerate OCR/typo'd lists like "30, 31 32, 33" (Notification 41/2017).
# "\s+\d+" cannot swallow "and"/"to" (they begin with a letter, not a digit),
# so those still flow into the dedicated (and|to) group below.
_SNO_LIST_PAT = r'\d+[A-Z]*(?:\s*,\s*\d+[A-Z]*|\s+\d+[A-Z]*)*(?:\s+(?:and|to)\s+\d+[A-Z]*)*'

# Descriptive qualifier fragment matching any combination of
# "words / figures / brackets / letters / symbol(s)" joined by commas and/or
# "and" — the gazette phrasings that introduce a quoted value, e.g.
# "the brackets, words and figures", "the words and symbol",
# "the words, figure and brackets". Used by the word-level INSERT and
# SUBSTITUTE handlers so newly seen qualifiers (e.g. "symbol") are recognised
# without hand-listing every permutation.
_WORD_QUAL = (r"(?:words?|figures?|brackets?|letters?|symbol(?:s)?|numbers?)"
              r"(?:(?:\s*,?\s*|\s+and\s+)(?:words?|figures?|brackets?|letters?|symbol(?:s)?|numbers?))*")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _eid(source_id: str, clause_ref: str, text: str) -> str:
    return "evt_rate_" + hashlib.md5((source_id + "|" + clause_ref + "|" + text[:200]).encode()).hexdigest()[:12]


def _get_props(xml_path: Path) -> dict[str, str]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    props = {}
    for prop in root.iter():
        tag = prop.tag.split("}")[-1] if "}" in prop.tag else prop.tag
        if tag == "property":
            props[prop.get("name", "")] = prop.get("value", "")
    props["_tree"] = tree
    props["_root"] = root
    return props


def _collect_text(el: ET.Element, parts: list[str]) -> None:
    """Append text of *el* (and descendants) to *parts*, skipping <num> bodies.

    <num> elements hold footnote-reference digits that pollute the prose
    (e.g. "amendments in the 5 notification"); their text is dropped but the
    trailing text (tail) following them is retained.
    """
    tag = el.tag.split("}")[-1] if isinstance(el.tag, str) and "}" in el.tag else el.tag
    if tag != "num" and el.text:
        parts.append(el.text.strip())
    for child in el:
        _collect_text(child, parts)
    if el.tail:
        parts.append(el.tail.strip())


def _all_text(root: ET.Element) -> str:
    parts: list[str] = []
    _collect_text(root, parts)
    return " ".join(p for p in parts if p)


def _extract_sno_list(text: str) -> list[str]:
    """Parse 'S. Nos. 23 and 24' or 'S. Nos. 11, 13, 25, 45' → ['23', '24', ...].

    Also handles alpha-suffixed ranges like '26A to 26L'.
    """
    text = text.replace(" and ", ", ")
    snos: list[str] = []

    range_match = re.search(r"(\d+)([A-Z]?)\s*to\s+(\d+)([A-Z]?)", text)
    if range_match:
        start_num = int(range_match.group(1))
        start_letter = range_match.group(2)
        end_num = int(range_match.group(3))
        end_letter = range_match.group(4)
        if start_letter and end_letter and start_num == end_num:
            for c in range(ord(start_letter), ord(end_letter) + 1):
                snos.append(f"{start_num}{chr(c)}")
            text = text[:range_match.start()] + " " + text[range_match.end():]
        elif not start_letter and not end_letter and start_num < end_num and (end_num - start_num) < 100:
            for n in range(start_num, end_num + 1):
                snos.append(str(n))
            text = text[:range_match.start()] + " " + text[range_match.end():]

    for m in re.finditer(r"\b(\d+[A-Z]*)", text):
        sno = m.group(1)
        if len(sno) <= 4 or any(c.isalpha() for c in sno):
            snos.append(sno)
    return snos


def _collapse_nested_smart_quotes(text: str) -> str:
    """Collapse inner ``\u201c``/``\u201d`` pairs so they are not mistaken for
    entry-segment delimiters.

    Gazette entry blocks are wrapped in an outer quote pair that the corpus
    frequently mismatches (straight ``"..."``, smart ``\u201c...\u201d``, or a
    mixed ``"...\u201d``), and they often embed *inner* smart-quote pairs around
    product names, e.g. ``"24B. 2403 91 00 \u201cHomogenised\u201d or
    \u201creconstituted\u201d tobacco ..."``. After the blanket
    ``\u201c``/``\u201d`` → ``"`` normalization those inner quotes would be
    treated as segment delimiters and shatter the entry (24B losing its
    description, 24C losing its leading words).

    This rewrites every *inner* ``\u201c``/``\u201d`` to an ASCII single quote,
    which the segment extractor does not treat as a delimiter:

      * Straight/mixed wrapper: when a straight ``"`` is present, the entry
        block spans from the first quote char to the last (straight or smart)
        close — collapse every smart quote inside that region. Straight-quote
        segment boundaries between multiple entries (``"259A..." "259B..."``)
        are preserved.
      * Pure smart wrapper: top-level ``\u201c...\u201d`` spans are located via
        a depth walk (so multi-entry ``\u201c259A...\u201d \u201c259B...\u201d``
        keeps its boundaries) and inner pairs within each span are collapsed.
    """
    # Straight/mixed wrapper: a straight double-quote is present.
    first_dq = text.find('"')
    if first_dq != -1:
        last_dq = text.rfind('"')
        last_sq = text.rfind("\u201d")
        end = max(last_dq, last_sq)
        if end > first_dq:
            inner = text[first_dq + 1:end]
            if "\u201c" in inner or "\u201d" in inner:
                inner = inner.replace("\u201c", "'").replace("\u201d", "'")
                return text[:first_dq + 1] + inner + text[end:]
        return text

    # Pure smart wrapper: locate top-level \u201c...\u201d spans via a depth walk
    # so multi-entry blocks keep their boundaries, then collapse inner pairs.
    positions = [i for i, ch in enumerate(text) if ch in "\u201c\u201d"]
    if len(positions) < 4:  # need at least one outer + one inner pair
        return text
    spans: list[tuple[int, int]] = []
    depth = 0
    cur_start = -1
    for i in positions:
        ch = text[i]
        if ch == "\u201c":
            if depth == 0:
                cur_start = i
            depth += 1
        else:  # "\u201d"
            if depth > 0:
                depth -= 1
                if depth == 0 and cur_start != -1:
                    spans.append((cur_start, i))
                    cur_start = -1
    if not spans:
        return text
    has_inner = any(
        text[s + 1:e].count("\u201c") > 0 or text[s + 1:e].count("\u201d") > 0
        for s, e in spans
    )
    if not has_inner:
        return text
    out: list[str] = []
    prev = 0
    for s, e in spans:
        out.append(text[prev:s + 1])  # up to and including the opening \u201c
        inner = text[s + 1:e].replace("\u201c", "'").replace("\u201d", "'")
        out.append(inner)
        prev = e  # at the closing \u201d
    out.append(text[prev:])
    return "".join(out)


def _parse_quoted_entries(text: str) -> list[dict[str, str]]:
    """Parse quoted entry data like:
       '"123A  2515 11 00  Marble and travertine, crude or roughly trimmed"'
    Returns list of {sno, tariff_item, description}.
    """
    entries: list[dict[str, str]] = []
    # Collapse inner smart-quote pairs (product names like "Homogenised") inside
    # each top-level quoted span BEFORE the blanket normalization below, so they
    # are not mistaken for segment delimiters (cf. 3/2023-cess S.Nos 24B/24C).
    text = _collapse_nested_smart_quotes(text)
    # Normalize whitespace and smart quotes
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("namely: -", "").replace("namely:-", "").replace("namely:", "")

    # Fix malformed quotes in multi-entry insertions: the XML sometimes emits a
    # single stray " between two entries (e.g. "259A ... "259B ... 259C ..."),
    # yielding an odd quote count. A stray " immediately before an S.No + tariff
    # pattern is an entry boundary — replace it with a close+open pair (" ") so
    # the balanced-quote extraction below recovers every entry.
    if text.count('"') % 2 == 1:
        first_q = text.find('"')
        if first_q >= 0:
            rest = text[first_q + 1:]
            _entry_ahead = r'[1-9]\d{0,2}[A-Z]*(?:-[A-Z]+)?\.?\s+[1-9]\d{1,}'
            rest = re.sub(r'"(?=\s*' + _entry_ahead + r')', '" "', rest)
            # Complementary malformation (e.g. '"429A ..."; 429B ... 429H ...";'):
            # only the first entry is quoted and the rest run unquoted until a
            # dangling closing quote. Insert a missing opening quote before the
            # first unquoted S.No + tariff that follows a closing quote.
            rest = re.sub(r'("\s*;?\s*)(' + _entry_ahead + r')', r'\1"\2', rest)
            # Complementary malformation: the description is quoted separately
            # from its S.No/tariff (e.g. '"151 Any chapter "Parts for
            # manufacture of hearing aids"'), so a stray " sits before a
            # capitalized description word rather than before the next S.No.
            # Convert it to a close+open pair so the balanced-quote extraction
            # below captures the description as its own segment.
            rest = re.sub(r'"(?=\s*[A-Z][a-z]+ )', '" "', rest)
            text = text[:first_q + 1] + rest

    # Find content within quotes (handle unclosed quotes too)
    parts = re.findall(r'"([^"]+)"', text)
    if not parts:
        # Try smart quotes
        parts = re.findall(r'\u201c([^\u201d]+)\u201d', text)
    if not parts:
        m = re.search(r'["\u201c](.+)', text, re.DOTALL)
        if m:
            raw = m.group(1)
            end_idx = raw.rfind('"')
            if end_idx > 10:
                raw = raw[:end_idx]
            end_idx2 = raw.rfind('\u201d')
            if end_idx2 > 10:
                raw = raw[:end_idx2]
            parts = [raw]
        else:
            parts = [text]
    if not parts:
        parts = [text]

    for part in parts:
        part = _clean(part)
        if not part or len(part) < 3:
            continue

        multi = re.split(r'(?=\b[1-9]\d{0,2}[A-Z]*(?:-[A-Z]+)?\.?\s+(?:\d{2,}|Any\s|chapter))', part)
        if len(multi) > 1:
            sub_entries: list[dict[str, str]] = []
            for sub in multi:
                sub = _clean(sub)
                if not sub or len(sub) < 5:
                    continue
                _parse_single_entry(sub, sub_entries)
            if sub_entries:
                entries.extend(sub_entries)
                continue

        before = len(entries)
        _parse_single_entry(part, entries)
        # A description that the gazette quotes separately from its S.No/tariff
        # (e.g. '"151 Any chapter" "Parts for manufacture of hearing aids"')
        # lands in its own quoted segment with no leading S.No. Attach it to
        # the preceding entry when that entry is missing its description.
        if len(entries) == before and entries and not entries[-1].get("description", "").strip():
            entries[-1]["description"] = part

    return _dedup_consecutive_snos(_merge_rate_subrows(entries))


def _dedup_consecutive_snos(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge consecutive entries that share the same S.No.

    The gazette occasionally flattens a cross-reference into the same stream as
    a real entry, producing a phantom duplicate. The clearest instance is the
    Compensation Cess 5/2017 clause for S.No 52, whose serial numbers are
    flattened as ``... 52A 8703 ... specified against entry at S. No 52B 20%
    52B 8703 Motor vehicles ... 22%``: the parser splits at the first ``52B``
    (a cross-reference inside 52A's description) and emits a phantom ``52B``
    whose "tariff" is really 52A's rate ``20%`` and whose description is empty,
    immediately followed by the genuine ``52B`` entry. Left un-merged, a later
    RATE_SUBSTITUTE_COLUMN targeting 52B updates only the phantom row and the
    stale duplicate survives in the materialized schedule.

    This collapses each such duplicate: when two consecutive entries carry the
    same S.No, the row with a non-empty description supplies the description and
    a genuine (non rate-like) HSN tariff wins over a bare rate value.
    """
    if len(entries) <= 1:
        return entries

    def is_rate_like(val: str) -> bool:
        v = val.strip()
        return bool(re.match(r"^\d+(?:\.\d+)?%$", v) or re.match(r"^Nil$", v, re.I))

    result: list[dict[str, str]] = [dict(entries[0])]
    for entry in entries[1:]:
        prev = result[-1]
        if entry.get("sno", "") and entry.get("sno") == prev.get("sno", ""):
            if not prev.get("description", "").strip() and entry.get("description", "").strip():
                prev["description"] = entry["description"]
            prev_rate_like = is_rate_like(prev.get("tariff_item", ""))
            cur_tariff = entry.get("tariff_item", "").strip()
            if (prev_rate_like or not prev.get("tariff_item", "").strip()) \
                    and cur_tariff and not is_rate_like(cur_tariff):
                prev["tariff_item"] = cur_tariff
            continue
        result.append(dict(entry))
    return result


def _merge_rate_subrows(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge rate sub-rows produced by over-eager entry splitting back into
    their header row.

    A multi-group tariff item such as ``"4013 90 49"`` causes the split regex
    in :func:`_parse_quoted_entries` to break a single entry at the
    ``"90 49"`` number-number boundary. The header row ends up with an empty
    description and a truncated tariff, while the trailing tariff sub-digits
    land on a phantom sub-row whose S.No is a bare 1-2 digit number (``90``)
    and whose description is the real entry text.

    This collapses each such sub-row back into the preceding header: the
    sub-row's S.No and tariff are appended to the header's tariff item, and
    the sub-row's description is copied onto the header.
    """
    if len(entries) <= 1:
        return entries
    result: list[dict[str, str]] = [dict(entries[0])]
    for entry in entries[1:]:
        prev = result[-1]
        prev_sno = prev.get("sno", "")
        cur_sno = entry.get("sno", "")
        is_subrow = (
            re.match(r"^\d{1,2}$", cur_sno)
            and not prev.get("description", "").strip()
            and prev_sno != cur_sno
        )
        if is_subrow:
            extra = _clean((cur_sno + " " + entry.get("tariff_item", "")).strip())
            prev["tariff_item"] = _clean(
                (prev.get("tariff_item", "") + " " + extra).strip()
            )
            sub_desc = entry.get("description", "").strip()
            if sub_desc:
                prev["description"] = sub_desc
            continue
        result.append(dict(entry))
    return result


def _consume_tariff(tokens_rest: list[str]) -> tuple[str, int]:
    """Consume the tariff-item tokens from the start of *tokens_rest*.

    Returns ``(tariff_str, num_consumed)``. Returns ``("", 0)`` when no
    recognised tariff pattern begins at ``tokens_rest[0]``.

    Recognised tariff patterns (the bit between the S.No and the description):
      - "Any [other] chapter" (+ optional "except NN[, NN...]"
        or "other than NN[, NN...]")
      - Numeric tariffs, possibly multi-token: "0401", "2711 13 00"
      - Chapter / heading references joined by "or": "84 or 85",
        "6309 or 6310"
      - Tariff with a bracketed exclusion: "63 [other than 6309]",
        "63[other than 6309]"
      - Mixed numeric + chapter: "88 or Any other chapter"
    """
    n = len(tokens_rest)
    if n == 0:
        return "", 0

    def match_any_chapter(idx: int) -> int:
        """Token count if tokens_rest[idx:] begins with an
        'Any [other] chapter [exceptions]' expression, else 0."""
        if idx >= n or tokens_rest[idx].lower() != "any":
            return 0
        cnt = 1
        if idx + cnt < n and tokens_rest[idx + cnt].lower() == "other":
            cnt += 1
        if idx + cnt >= n or tokens_rest[idx + cnt].lower() != "chapter":
            return 0
        cnt += 1
        # optional "except NN[, NN...]"
        if idx + cnt < n and tokens_rest[idx + cnt].lower() == "except":
            cnt += 1
            while idx + cnt < n and re.match(r"^[\d,]+$", tokens_rest[idx + cnt]):
                cnt += 1
        # optional "other than NN[, NN...]"
        elif (idx + cnt + 1 < n
              and tokens_rest[idx + cnt].lower() == "other"
              and tokens_rest[idx + cnt + 1].lower() == "than"):
            cnt += 2
            while idx + cnt < n and re.match(r"^[\d,]+$", tokens_rest[idx + cnt]):
                cnt += 1
        return cnt

    # Leading "Any chapter" pattern
    cnt = match_any_chapter(0)
    if cnt > 0:
        return _clean(" ".join(tokens_rest[:cnt])), cnt

    # Otherwise expect a numeric / bracket tariff beginning with a digit
    if not re.match(r"^\d", tokens_rest[0]):
        return "", 0

    consumed: list[str] = []
    i = 0
    in_bracket = False
    while i < n:
        tok = tokens_rest[i]
        if in_bracket:
            consumed.append(tok)
            i += 1
            if "]" in tok:
                in_bracket = False
            continue
        if re.match(r"^\d", tok):
            consumed.append(tok)
            i += 1
            if "[" in tok and "]" not in tok:
                in_bracket = True
            continue
        if tok.startswith("["):
            consumed.append(tok)
            i += 1
            if "]" not in tok:
                in_bracket = True
            continue
        if re.match(r"^[\d,]+$", tok):
            consumed.append(tok)
            i += 1
            continue
        if tok.lower() == "or":
            ac = match_any_chapter(i + 1)
            if ac > 0:
                consumed.append(tok)
                consumed.extend(tokens_rest[i + 1:i + 1 + ac])
                i += 1 + ac
                continue
            if i + 1 < n and re.match(r"^\d", tokens_rest[i + 1]):
                consumed.append(tok)
                i += 1
                continue
        break

    return _clean(" ".join(consumed)), i


def _parse_single_entry(part: str, entries: list[dict[str, str]]) -> None:
    """Parse a single quoted entry into {sno, tariff_item, description}."""
    tokens = part.split()
    if not tokens or len(tokens) < 2:
        return

    # Handle sno rendered with an internal space in the XML: "102 A" → "102A",
    # "198 B" → "198B". Only combine when a tariff (digit) follows, so we don't
    # merge the article "A" in prose-like descriptions.
    if (len(tokens) >= 3
            and re.match(r"^[1-9]\d{0,2}$", tokens[0])
            and re.match(r"^[A-Z]$", tokens[1])
            and re.match(r"^\d", tokens[2])):
        tokens = [tokens[0] + tokens[1]] + tokens[2:]

    # Accept alpha-suffixed S.Nos (e.g. "102A", "26L") and the rare case where
    # an ingested footnote digit trails the letter portion (e.g. "171A1").
    # Also accept hyphenated suffixes (e.g. "411-I" — Notification 41/2017),
    # normalising the captured S.No by stripping the hyphen ("411-I" → "411I").
    sno_match = re.match(r"^([1-9]\d{0,2}[A-Z]*(?:-[A-Z]+)?\d?)\.?$", tokens[0])
    if not sno_match:
        return
    sno = sno_match.group(1).replace("-", "")

    # Extract the tariff item from the tokens following the S.No. This
    # recognises numeric tariffs, "Any chapter" references, "or"-joined
    # chapter/heading lists, and bracketed exclusions (e.g. "63 [other than
    # 6309]"). Everything after the tariff is the description.
    tariff, consumed = _consume_tariff(tokens[1:])
    desc_start = 1 + consumed
    description = _clean(" ".join(tokens[desc_start:]))

    if sno and (tariff or description):
        entries.append({
            "sno": sno,
            "tariff_item": tariff,
            "description": description,
        })


def _extract_schedule_id(text: str) -> Optional[str]:
    """Extract schedule id from text like 'Schedule I - 2.5%' or 'Schedule-IV-14%'."""
    m = re.search(r"Schedule[\s\-]+([IVX]+)", text, re.I)
    return m.group(1) if m else None


def _extract_target_notification(text: str) -> Optional[str]:
    """Extract the notification being amended.

    Looks for patterns like:
      'amendments in the notification ... No.1/2017-Central Tax (Rate)'
      'amendments in the notification ... No.1/2017-Compensation Cess (Rate)'
    Skips the amending notification's own number.
    """
    # First try: "amendments in the notification of ... No. X/YYYY-Central Tax (Rate)"
    m = re.search(
        r"amend\w*\s+in\s+the\s+notification\s+[^.]{0,200}?"
        r"No\.?\s*(\d+)/(\d{4})[-\s]*Central\s+Tax\s*\(Rate\)",
        text, re.I,
    )
    if m:
        return f"{int(m.group(1))}/{m.group(2)}-ct-rate"

    # Compensation Cess (Rate) target:
    # 'amendments in the notification ... No. 1/2017-Compensation Cess (Rate)'
    m = re.search(
        r"amend\w*\s+in\s+the\s+notification\s+[^.]{0,200}?"
        r"No\.?\s*(\d+)/(\d{4})[-\s]*Compensation\s+Cess\s*\(Rate\)",
        text, re.I,
    )
    if m:
        return f"{int(m.group(1))}/{m.group(2)}-cc-rate"

    # Second try: "amendments in the said notification" → use previously identified
    # (handled by caller)

    # Third try: doc name pattern
    return None


def _extract_effective_date(text: str) -> Optional[str]:
    """Extract 'come into force' date or 'with effect from' date."""
    # "shall come into force on the 1st day of January, 2019"
    m = re.search(
        r"come\s+into\s+force\s+(?:with\s+effect\s+from\s+)?"
        r"(?:on\s+)?the\s+(\d+)\w*\s+day\s+of\s+([A-Za-z]+),?\s*(\d{4})",
        text, re.I,
    )
    if m:
        day = int(m.group(1))
        month_name = m.group(2).lower()[:3]
        year = int(m.group(3))
        months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        mon = months.get(month_name)
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"

    # "with effect from the 22nd day of September, 2025"
    m = re.search(
        r"with\s+effect\s+from\s+the\s+(\d+)\w*\s+day\s+of\s+([A-Za-z]+),?\s*(\d{4})",
        text, re.I,
    )
    if m:
        day = int(m.group(1))
        month_name = m.group(2).lower()[:3]
        year = int(m.group(3))
        months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        mon = months.get(month_name)
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"

    # "with immediate effect"
    if "immediate effect" in text.lower():
        return None  # use publication date

    return None


# ── continuation text detection ──────────────────────────────────────────────

_CONTINUATION_MARKERS = [
    "the phrase",
    "shall mean",
    "shall file an affidavit",
    "set up by an act",
    "established by any government",
    "corrugated paper",
    "non-corrugated paper",
    "petrol, liquefied petroleum",
    "solar power",
    "wind mills",
    "waste to energy",
    "ocean waves",
    "all goods of marble",
    "statues, statuettes",
    "online money gaming",
    "project",
    "a brand registered as on",
]

_AMENDMENT_VERBS = [
    "shall be omitted", "shall be inserted", "shall be substituted",
    "shall be added", "shall respectively be substituted",
    "shall be re-numbered", "shall be renumbered",
    "re-numbered as", "renumbered as",
    "following serial number", "following s. no",
    "following entry", "following entries",
    "the entries relating thereto",
    "supersedes", "supersession",
    "opening paragraph",
    "portion beginning with",
]


def _is_continuation_or_definition(text_low: str) -> bool:
    """Check if clause is continuation data or definition, not an amendment."""
    has_verb = any(v in text_low for v in _AMENDMENT_VERBS)
    if has_verb and not re.match(r"^\([a-z]\)\s*(bearing|a brand|the phrase)", text_low):
        return False
    if any(m in text_low for m in _CONTINUATION_MARKERS):
        return True
    if re.match(r"^\([a-z]\)\s*(bearing|a brand|the phrase|set up|established)", text_low):
        return True
    if not re.search(r"s\.?\s*nos?\.?", text_low) and len(text_low) < 200:
        return True
    return False


def _extract_quoted_value(text: str, boundary: str) -> str:
    """Extract the last quoted value before a boundary string like 'shall be substituted'.

    Handles substitution values that themselves contain quoted phrases (e.g.
    notification 34/2017 SIII 63: ``...known as “dental wax” or as “dental
    impression compounds”...``) by balancing nested ``\u201c``/``\u201d`` pairs
    rather than stopping at the first inner closing quote.
    """
    idx = text.lower().find(boundary)
    if idx < 0:
        idx = len(text)
    before = text[:idx]
    # Prefer smart-quote balanced extraction: walk backwards from the last
    # closing \u201d, counting open/close pairs to find the matching opening
    # \u201c. This captures the full outer value even when it embeds inner
    # quoted phrases that a non-greedy match would truncate at.
    last_smart_close = before.rfind("\u201d")
    if last_smart_close >= 0:
        depth = 1
        open_idx = -1
        for i in range(last_smart_close - 1, -1, -1):
            ch = before[i]
            if ch == "\u201d":
                depth += 1
            elif ch == "\u201c":
                depth -= 1
                if depth == 0:
                    open_idx = i
                    break
        if open_idx >= 0:
            return _clean(before[open_idx + 1:last_smart_close])
    # Straight-quote fallback: return the last quoted segment.
    segments = re.findall(r'"([^"]*)"', before, re.DOTALL)
    if segments:
        return _clean(segments[-1])
    # Fall back to an unclosed quote (a recurring source-XML typo where the
    # closing quotation mark is dropped): capture from the last opening quote
    # to the end of the text preceding the boundary, stripping the trailing
    # sentence punctuation that leaked in (e.g. "...6309], shall be").
    open_idx = max(before.rfind('"'), before.rfind("\u201c"))
    if open_idx >= 0:
        return _clean(before[open_idx + 1:]).rstrip(",;:.")
    return ""


def _parse_clause(
    clause_text: str,
    target_notification: str,
    current_schedule: str,
    clause_ref: str,
    effective_date: str,
    publication_date: str,
    source_id: str,
    source_cbic_no: str,
) -> list[RateAmendmentEvent]:
    """Parse a single amendment clause into events."""
    events: list[RateAmendmentEvent] = []
    text = clause_text
    # Normalise space-separated serial-number suffixes that the gazette
    # occasionally inserts in the clause prose (e.g. Notification 41/2017
    # clause (xlix): "in S. No. 177 A, for the entry in columns (2) and
    # (3) …"). Without this the trailing alpha suffix is dropped and the
    # amendment is routed to the bare numeric S.No (177) instead of the
    # intended suffixed one (177A). Only merge a single capital letter
    # that immediately precedes the punctuation terminating the S.No
    # reference, so descriptions quoting bare letters are unaffected.
    # The lookahead also covers "...A and entries relating thereto"
    # (e.g. Notification 19/2018 clause (iii): "for S. No. 102 A and
    # entries relating thereto, …"), where the suffix is followed by the
    # "entries relating thereto" clause rather than punctuation. Without
    # this the SUBSTITUTE_ROW would target the bare S.No (102) and
    # destroy an unrelated entry instead of the suffixed one (102A).
    text = re.sub(
        r"(S\.?\s*Nos?\.?\s*\d+)\s+([A-Z])"
        r"(?=\s*[,;:]|\s+and\s+(?:the\s+)?entries|\s+entries\s+relat)",
        r"\1\2", text,
    )
    # Normalise the gazette's missing-space typo "inparagraph" → "in paragraph"
    # (Notification 31/2017 clause (ii): "inparagraph 2, for the words ...").
    # Without this the paragraph-scoped word substitution below is not seen.
    text = re.sub(r"\binparagraph\b", "in paragraph", text)
    text_low = text.lower()

    # ── filter continuation/definition text ──────────────────────────────
    if _is_continuation_or_definition(text_low):
        return events

    # ── Service schedule: sub-item level substitution ───────────────────
    # "against serial number 3, for item (iii) in column (3) and the entries
    #  relating thereto in columns (3), (4) and (5), the following shall be
    #  substituted, namely:- '(iii) New text...'"
    # Unlike RATE_SUBSTITUTE_COLUMN (which replaces the ENTIRE column-3
    # description), this operation replaces ONLY the targeted sub-item
    # (e.g. "(iii) ...") within the entry's multi-item description, leaving
    # sibling sub-items "(i)", "(ii)", "(iv)" ... intact. Checked BEFORE the
    # generic svc_item / col_sub patterns so the sub-item context is preserved.
    svc_item_sub = re.search(
        r"(?:against\s+)?serial\s+number\s+(\d+[A-Z]*)\s*,?\s*"
        r"for\s+(?:item|sub-?item)\s*(\([a-z0-9ivx]+\))"
        r".*?shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if svc_item_sub:
        sno = svc_item_sub.group(1)
        item_id = svc_item_sub.group(2)  # e.g. "(iii)"
        verb_end = svc_item_sub.end()
        after_verb = text[verb_end:]
        qm = re.search(r"[\u201c\"]", after_verb)
        new_text = ""
        if qm:
            raw_val = after_verb[qm.end():]
            for close_ch in ("\u201d", '"'):
                ci = raw_val.rfind(close_ch)
                if ci > 10:
                    raw_val = raw_val[:ci]
            new_text = _clean(raw_val)
        if new_text:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_ITEM",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "item_id": item_id, "new_text": new_text,
                         "raw_text": _clean(text)[:500]},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # ── Service schedule multi-column item operations ──────────────────
    # Patterns specific to the services rate schedule (11/2017) where
    # amendments target a specific item within a serial number's entry
    # across multiple table columns. These phrases have no analogue in the
    # goods schedules and are checked before the generic patterns so the
    # service-specific item/sub-item context is preserved.
    #   "against serial number 3, for item (iii) in column (3) and the
    #    entries relating thereto in columns (3), (4) and (5), the
    #    following shall be substituted"
    svc_item = re.search(
        r"(?:against\s+serial\s+number\s+(\d+[A-Z]*)\s*,?\s*)?"
        r"(for|after)\s+item\s+\(([ivx]+[a-z]?)\)"
        r".*?"
        r"columns?\s+\(3\),?\s*\(4\)\s+and\s+\(5\)"
        r".*?shall\s+be\s+(substituted|inserted)",
        text, re.I | re.DOTALL,
    )
    if svc_item:
        sno = svc_item.group(1) or ""
        action = svc_item.group(2)
        item_id = svc_item.group(3)
        verb = svc_item.group(4)
        # Extract the quoted replacement text that follows "shall be
        # substituted/inserted, namely:-". Service entries often lack a
        # closing quote in the source XML, so capture from the first
        # opening quote after the verb to the last closing quote (or end).
        verb_end = svc_item.end()
        after_verb = text[verb_end:]
        qm = re.search(r"[\u201c\"]", after_verb)
        new_val = ""
        if qm:
            raw_val = after_verb[qm.end():]
            for close_ch in ("\u201d", '"'):
                ci = raw_val.rfind(close_ch)
                if ci > 10:
                    raw_val = raw_val[:ci]
            new_val = _clean(raw_val)
        if verb == "substituted":
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_COLUMN",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "column": 3, "new_value": new_val,
                         "item": item_id, "raw_text": _clean(text)[:500]},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events
        else:  # inserted
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_INSERT_ENTRIES",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"after_sno": sno, "item": item_id,
                         "new_value": new_val,
                         "raw_text": _clean(text)[:500]},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # ── Service schedule: item/sub-item operations (broader) ───────────
    # Additional service-schedule patterns that target specific items or
    # sub-items within a serial number's entry. These cover rate changes,
    # omissions, word-level edits, and sub-item substitutions that have no
    # analogue in the goods schedules.

    # "against item (vii), for the entry in column (4), the entry "9" shall
    #  be substituted" — single-column rate/value substitution for an item.
    # Handles both single-quote ("9") and dual-quote ("6"→"9") variants.
    svc_item_col = re.search(
        r"against\s+items?\s+\(([ivx]+[a-z]?)\)"
        r".*?column\s+\((\d)\).*?"
        r"[\u201c\"]([^\u201d\"]{1,80})[\u201d\"]\s*,?\s*"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if svc_item_col:
        item_id = svc_item_col.group(1)
        col = int(svc_item_col.group(2))
        new_val = _clean(svc_item_col.group(3))
        sno_m = re.search(r"serial\s+number\s+(\d+[A-Z]*)", text[:svc_item_col.start()], re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "", "column": col,
                     "new_value": new_val, "item": item_id},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "for item (vii) and the corresponding entries relating thereto in
    #  columns (4) and (5), the following shall be substituted" — item
    #  substitution across columns 4 and 5 (or other column pairs).
    svc_item_any = re.search(
        r"(?:against\s+serial\s+number\s+(\d+[A-Z]*)\s*,?\s*)?"
        r"(for|after)\s+items?\s+\(([ivx]+[a-z]?)\)"
        r".*?columns?\s+\([3-5]\)"
        r".*?shall\s+be\s+(substituted|inserted)",
        text, re.I | re.DOTALL,
    )
    if svc_item_any:
        sno = svc_item_any.group(1) or ""
        action = svc_item_any.group(2)
        item_id = svc_item_any.group(3)
        verb = svc_item_any.group(4)
        verb_end = svc_item_any.end()
        after_verb = text[verb_end:]
        qm = re.search(r"[\u201c\"]", after_verb)
        new_val = ""
        if qm:
            raw_val = after_verb[qm.end():]
            for close_ch in ("\u201d", '"'):
                ci = raw_val.rfind(close_ch)
                if ci > 10:
                    raw_val = raw_val[:ci]
            new_val = _clean(raw_val)
        op = "RATE_SUBSTITUTE_COLUMN" if verb == "substituted" else "RATE_INSERT_ENTRIES"
        payload: dict[str, Any] = {"item": item_id, "new_value": new_val,
                                    "raw_text": _clean(text)[:500]}
        if op == "RATE_SUBSTITUTE_COLUMN":
            payload["sno"] = sno
            payload["column"] = 3
        else:
            payload["after_sno"] = sno
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation=op,
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload=payload,
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "items (iii), (iv) ... and the (corresponding) entries relating
    #  thereto in columns (4) and (5), shall be omitted" — item omission.
    svc_item_omit = re.search(
        r"items?\s+((?:\([ivx]+[a-z]?\)(?:\s*,?\s*|\s+and\s+))+)"
        r".*?(?:corresponding\s+)?entries\s+relat\w+\s+thereto"
        r".*?shall\s+be\s+omitted",
        text, re.I | re.DOTALL,
    )
    if svc_item_omit:
        items_raw = svc_item_omit.group(1)
        item_list = re.findall(r"\(([ivx]+[a-z]?)\)", items_raw)
        sno_m = re.search(r"serial\s+number\s+(\d+[A-Z]*)", text[:svc_item_omit.start()], re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_OMIT_ENTRIES",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "item_list": item_list, "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "the item (iv) in column (3) and the entries relating thereto ...
    #  shall be omitted" — single item omission with entries.
    svc_the_item_omit = re.search(
        r"(?:the\s+)?item\s+\(([ivx]+[a-z]?)\)\s+in\s+column\s+\(3\)"
        r".*?shall\s+be\s+omitted",
        text, re.I | re.DOTALL,
    )
    if svc_the_item_omit:
        item_id = svc_the_item_omit.group(1)
        sno_m = re.search(r"serial\s+number\s+(\d+[A-Z]*)", text[:svc_the_item_omit.start()], re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_OMIT_ENTRIES",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "item_list": [item_id], "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "for sub-item (b), the following sub-item shall be substituted" /
    # "after sub-item (d), the following sub-item shall be inserted"
    svc_subitem = re.search(
        r"(?:against\s+serial\s+number\s+(\d+[A-Z]*)\s*,?\s*)?"
        r"(in\s+item\s+\([ivx]+[a-z]?\)\s*,?\s*)?"
        r"(for|after)\s+sub-?items?\s+\(([a-z]+[a-z]?)\)"
        r".*?shall\s+be\s+(substituted|inserted)",
        text, re.I | re.DOTALL,
    )
    if svc_subitem:
        sno = svc_subitem.group(1) or ""
        item_ctx_m = svc_subitem.group(2)
        item_ctx = ""
        if item_ctx_m:
            im = re.search(r"item\s+\(([ivx]+[a-z]?)\)", item_ctx_m, re.I)
            if im:
                item_ctx = im.group(1)
        action = svc_subitem.group(3)
        sub_id = svc_subitem.group(4)
        verb = svc_subitem.group(5)
        verb_end = svc_subitem.end()
        after_verb = text[verb_end:]
        qm = re.search(r"[\u201c\"]", after_verb)
        new_val = ""
        if qm:
            raw_val = after_verb[qm.end():]
            for close_ch in ("\u201d", '"'):
                ci = raw_val.rfind(close_ch)
                if ci > 10:
                    raw_val = raw_val[:ci]
            new_val = _clean(raw_val)
        op = "RATE_SUBSTITUTE_COLUMN" if verb == "substituted" else "RATE_INSERT_ENTRIES"
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation=op,
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno, "column": 3, "item": item_ctx,
                     "sub_item": sub_id, "new_value": new_val,
                     "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "in item (iii), in column (3), for the words "X" the words "Y" shall
    #  be substituted" — word-level substitution within a specific item.
    svc_word_in_item = re.search(
        r"(?:against\s+serial\s+number\s+(\d+[A-Z]*)\s*,?\s*)?"
        r"in\s+items?\s+\(([ivx]+[a-z]?)\)"
        r".*?for\s+the\s+" + _WORD_QUAL + r".*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"(?:the\s+" + _WORD_QUAL + r").*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"shall\s+be\s+(substituted|inserted|omitted)",
        text, re.I | re.DOTALL,
    )
    if svc_word_in_item:
        sno = svc_word_in_item.group(1) or ""
        item_id = svc_word_in_item.group(2)
        old_val = _clean(svc_word_in_item.group(3))
        new_val = _clean(svc_word_in_item.group(4))
        verb = svc_word_in_item.group(5)
        if verb == "omitted":
            op = "RATE_OMIT_WORDS"
            payload_w: dict[str, Any] = {"sno": sno, "item": item_id, "words": old_val}
        elif verb == "inserted":
            op = "RATE_INSERT_WORDS"
            payload_w = {"sno": sno, "item": item_id, "after_words": old_val, "insert_words": new_val}
        else:
            op = "RATE_SUBSTITUTE_WORDS"
            payload_w = {"sno": sno, "item": item_id, "old_words": old_val, "new_words": new_val}
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation=op,
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload=payload_w,
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "in item (vi), after the brackets and figures "(iii)", the brackets
    #  and figures "(iiia)," shall be inserted" — bracket/figure insertion.
    svc_bracket_in_item = re.search(
        r"in\s+items?\s+\(([ivx]+[a-z]?)\)"
        r".*?after\s+the\s+" + _WORD_QUAL + r".*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"(?:the\s+" + _WORD_QUAL + r").*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"shall\s+be\s+inserted",
        text, re.I | re.DOTALL,
    )
    if svc_bracket_in_item:
        item_id = svc_bracket_in_item.group(1)
        after_val = _clean(svc_bracket_in_item.group(2))
        insert_val = _clean(svc_bracket_in_item.group(3))
        sno_m = re.search(r"serial\s+number\s+(\d+[A-Z]*)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_INSERT_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "", "item": item_id,
                     "after_words": after_val, "insert_words": insert_val},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "for the words "declared tariff" wherever they occur, the words "value
    #  of supply" shall be substituted" — global word substitution.
    svc_global_words = re.search(
        r"for\s+the\s+" + _WORD_QUAL + r".*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"wherever\s+they\s+occur.*?"
        r"(?:the\s+" + _WORD_QUAL + r").*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if svc_global_words:
        old_val = _clean(svc_global_words.group(1))
        new_val = _clean(svc_global_words.group(2))
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_BATCH_SUBSTITUTE_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno_list": [], "old_words": old_val, "new_words": new_val,
                     "scope": "global"},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "for paragraph 2, the following shall be substituted" — paragraph-
    # level substitution in the services schedule.
    svc_para = re.search(
        r"for\s+paragraph\s+(\d+[A-Z]?)\s*,?\s*"
        r"(?:the\s+following\s+)?shall\s+be\s+substituted",
        text, re.I,
    )
    if svc_para:
        para_num = svc_para.group(1)
        verb_end = svc_para.end()
        after_verb = text[verb_end:]
        qm = re.search(r"[\u201c\"]", after_verb)
        new_val = ""
        if qm:
            raw_val = after_verb[qm.end():]
            for close_ch in ("\u201d", '"'):
                ci = raw_val.rfind(close_ch)
                if ci > 10:
                    raw_val = raw_val[:ci]
            new_val = _clean(raw_val)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": "", "column": 3, "new_value": new_val,
                     "paragraph": para_num, "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "against serial number X, in column (Y), in item (Z), the words/
    #  figures "A" shall be omitted" or "...for the words "A", the words
    #  "B" shall be substituted" — word/figure-level operation within a
    #  specific item, referenced by serial number + column.
    svc_sno_item = re.search(
        r"against\s+serial\s+number\s+(\d+[A-Z]*)\s*,?\s*"
        r"in\s+column\s+\((\d)\)\s*,?\s*"
        r"(?:in\s+items?\s+\(([ivx]+[a-z]?)\)\s*,?\s*)?"
        r"(?:in\s+(?:the\s+)?Explanation\s*\d*\s*,?\s*)?"
        r"(?:in\s+clause\s*\([^)]*\)\s*,?\s*)?",
        text, re.I,
    )
    if svc_sno_item and re.search(r"shall\s+be\s+(substituted|omitted|inserted)", text_low):
        sno = svc_sno_item.group(1)
        col = int(svc_sno_item.group(2))
        item_id = svc_sno_item.group(3) or ""
        # Try two-quote word substitution first
        two_q = re.search(
            r"for\s+the\s+" + _WORD_QUAL + r".*?"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
            r"(?:the\s+" + _WORD_QUAL + r").*?"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if two_q:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "item": item_id,
                         "old_words": _clean(two_q.group(1)),
                         "new_words": _clean(two_q.group(2))},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events
        # Single-quote omission: the words/figures "X" shall be omitted
        one_q = re.search(
            r"(?:for\s+)?the\s+" + _WORD_QUAL + r".*?"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
            r"shall\s+be\s+omitted",
            text, re.I | re.DOTALL,
        )
        if one_q:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_OMIT_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "item": item_id,
                         "words": _clean(one_q.group(1))},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events
        # Single-quote insertion: after the figures "X", the ... "Y" shall be inserted
        ins_q = re.search(
            r"after\s+the\s+" + _WORD_QUAL + r".*?"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
            r"(?:the\s+" + _WORD_QUAL + r").*?"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
            r"shall\s+be\s+inserted",
            text, re.I | re.DOTALL,
        )
        if ins_q:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_INSERT_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "item": item_id,
                         "after_words": _clean(ins_q.group(1)),
                         "insert_words": _clean(ins_q.group(2))},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events
        # "for item (viii), the following shall be substituted" — item-level
        item_following = re.search(
            r"for\s+items?\s+\(([ivx]+[a-z]?)\)\s*,?\s*"
            r"(?:the\s+following.*?shall\s+be\s+substituted)",
            text, re.I | re.DOTALL,
        )
        if item_following:
            new_val = _extract_quoted_value(text, "shall be substituted")
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_COLUMN",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "column": col, "item": item_following.group(1),
                         "new_value": new_val, "raw_text": _clean(text)[:500]},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # "for the entry in column (4), the entry "2.5" shall be substituted" —
    # column entry substitution without an explicit item reference (the
    # item context is established by a parent clause).
    svc_col_entry = re.search(
        r"for\s+the\s+entry\s+in\s+column\s+\((\d)\)\s*,?\s*"
        r"the\s+entry\s+"
        r"[\u201c\"]([^\u201d\"]{1,80})[\u201d\"]\s*,?\s*"
        r"shall\s+be\s+substituted",
        text, re.I,
    )
    if svc_col_entry:
        col = int(svc_col_entry.group(1))
        new_val = _clean(svc_col_entry.group(2))
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)",
            text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "", "column": col,
                     "new_value": new_val},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "for item (ii), the following item shall be substituted:- "(ii) ..."
    #  — single item substitution without column reference.
    svc_item_following = re.search(
        r"for\s+items?\s+\(([ivx]+[a-z]?)\)\s*,?\s*"
        r"(?:the\s+following\s+items?\s+)?shall\s+be\s+substituted",
        text, re.I,
    )
    if svc_item_following:
        item_id = svc_item_following.group(1)
        new_val = _extract_quoted_value(text, "shall be substituted")
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": "", "column": 3, "item": item_id,
                     "new_value": new_val, "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # "in item (iiia), the words "X" shall be omitted" — direct word
    #  omission within an item (no "for the" prefix).
    svc_item_word_omit = re.search(
        r"in\s+items?\s+\(([ivx]+[a-z]?)\)\s*,?\s*"
        r"(?:the\s+)?" + _WORD_QUAL + r"\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"shall\s+be\s+omitted",
        text, re.I | re.DOTALL,
    )
    if svc_item_word_omit:
        item_id = svc_item_word_omit.group(1)
        words = _clean(svc_item_word_omit.group(2))
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_OMIT_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": "", "item": item_id, "words": words},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    has_omit = "shall be omitted" in text_low
    has_insert = "shall be inserted" in text_low or "shall be added" in text_low
    has_substitute = "shall be substituted" in text_low or "respectively be substituted" in text_low
    has_renumber = "re-numbered" in text_low or "renumbered" in text_low

    # ── OMIT operations ──────────────────────────────────────────────────
    if has_omit:
        serial_omit = re.search(
            r"serial\s+numbers?\s+(" + _SNO_LIST_PAT + r")"
            r"(?:\s*,?\s*(?:and\s+)?(?:the\s+)?entries\s+relat\w+\s+thereto)?"
            r"\s*,?\s*shall\s+be\s+omitted",
            text, re.I,
        )
        if serial_omit:
            snos = _extract_sno_list(serial_omit.group(1))
            if snos:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_OMIT_ENTRIES",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno_list": snos},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Entry-level: "S. Nos. X and Y ... shall be omitted"
        # Allows an optional trailing comma before the "and the entries relating
        # thereto" clause (e.g. "S. Nos. 2,3,4,...,10, and the entries ...").
        # "the" is optional to also match "and entries relating thereto" (e.g.
        # 6/2022: "S. Nos. 197A, 197B, 197C, 197D and 197E and entries relating
        # thereto shall be omitted;").
        omit_match = re.search(
            r"S\.?\s*Nos?\.?\s*(" + _SNO_LIST_PAT + r")"
            r"(?:\s*,?\s*(?:and\s+)?(?:the\s+)?entries\s+(?:relat\w+\s+thereto|thereof))?"
            r"\s*,?\s*shall\s+be\s+omitted",
            text, re.I,
        )
        if omit_match:
            snos = _extract_sno_list(omit_match.group(1))
            if snos:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_OMIT_ENTRIES",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno_list": snos},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Word-level: "in S. No. X, ... the (words|word|brackets and words|words and figures) 'Y' shall be omitted"
        words_omit = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*).*?"
            r"(?:in\s+(?:the\s+)?entry\s+in\s+column\s+\(3\))?\s*,?\s*"
            r"(?:the\s+(?:words?|brackets\s+and\s+words?|words\s+and\s+figures?|figures\s+and\s+(?:letters|word)|figures)).*?"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']*\s*,?\s*"
            r"shall\s+be\s+omitted",
            text, re.I | re.DOTALL,
        )
        if words_omit:
            sno = words_omit.group(1)
            words = _clean(words_omit.group(2))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_OMIT_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "words": words},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # ── INSERT operations ────────────────────────────────────────────────
    if has_insert:
        # Entry-level: "after S. No. X ... following ... shall be inserted"
        insert_match = re.search(
            r"after\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)"
            r"(?:\s+and\s+the\s+entries\s+relat\w+\s+thereto)?"
            r".*?(?:following|below).*?(?:shall\s+be\s+inserted|shall\s+be\s+added)",
            text, re.I | re.DOTALL,
        )
        if insert_match:
            after_sno = insert_match.group(1)
            entries = _parse_quoted_entries(text)
            if entries:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_INSERT_ENTRIES",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"after_sno": after_sno, "entries": entries},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Also try: "for S. No. X ... following ... shall be inserted"
        insert_for = re.search(
            r"for\s+S\.?\s*No\.?\s*(\d+[A-Z]*)"
            r"(?:\s+and\s+the\s+entries\s+relat\w+\s+thereto)?"
            r".*?(?:following|below).*?(?:shall\s+be\s+inserted|shall\s+be\s+added)",
            text, re.I | re.DOTALL,
        )
        if insert_for:
            after_sno = insert_for.group(1)
            entries = _parse_quoted_entries(text)
            if entries:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_INSERT_ENTRIES",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"after_sno": after_sno, "entries": entries},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Word-level: "after the words 'X' ... 'Y' shall be inserted/added"
        # Allow flexible whitespace/comma ordering around the closing quote
        # (gazette sometimes renders `"Y" , shall be inserted` with a space
        # before the comma). The S.No. reference may be written as
        # "serial number" (e.g. Notification 02/2024 clauses), and the
        # descriptive qualifier that introduces the inserted text accepts any
        # combination of words/figures/brackets/letters/symbol(s) — the
        # standard phrasings used when inserting a parenthetical qualifier such
        # as "[other than coir products]" or "; parts thereof".
        words_insert = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*).*?"
            r"after\s+the\s+" + _WORD_QUAL + r".*?"
            r"[\u201c\"]([^\u201d\"]+)[\u201d\"]\s*,?\s*.*?"
            r"(?:the\s+" + _WORD_QUAL + r"|entry).*?"
            r"[\u201c\"]([^\u201d\"]+)[\u201d\"]\s*,?\s*"
            r"shall\s+be\s+(?:inserted|added)",
            text, re.I | re.DOTALL,
        )
        if words_insert:
            sno = words_insert.group(1)
            after_words = _clean(words_insert.group(2))
            insert_words = _clean(words_insert.group(3))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_INSERT_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "after_words": after_words, "insert_words": insert_words},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # ── SUBSTITUTE operations ────────────────────────────────────────────
    if has_substitute:
        # Batch column substitution: a single clause bundles several
        # "against S. No. X, for the entries in column (N), the entry 'val'
        # shall be substituted" sub-clauses (plural "entries"). This shape is
        # common in Compensation Cess amendments that nil out column (4) for a
        # run of S.Nos (e.g. 2/2025-cess, 3/2025-cess). The singular patterns
        # below only catch the first sub-clause, so handle the whole batch here
        # and emit one RATE_SUBSTITUTE_COLUMN per S.No.
        col_batch = list(re.finditer(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"for\s+the\s+entries\s+in\s+column\s+\(?(\d)\)?\s*,?\s*"
            r"the\s+entry\s*[\u201c\"]([^\u201d\"]+?)[\u201d\"]\s*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I,
        ))
        if col_batch:
            for m in col_batch:
                sno = m.group(1)
                col = int(m.group(2))
                new_val = _clean(m.group(3))
                if new_val:
                    events.append(RateAmendmentEvent(
                        event_id=f"evt_rate_{_eid(source_id, clause_ref, text + '|' + sno + str(col))}",
                        operation="RATE_SUBSTITUTE_COLUMN",
                        target_notification=target_notification,
                        target_schedule=current_schedule,
                        payload={"sno": sno, "column": col, "new_value": new_val},
                        effective_date=effective_date,
                        publication_date=publication_date,
                        source_notification=source_id,
                        source_cbic_no=source_cbic_no,
                        clause_ref=clause_ref,
                    ))
            return events

        # Multi-row substitution: "for S. Nos. 26A to 26L ... the following shall be substituted"
        # Also accepts "for serial number(s) 47 and 48 ..." (5/2017-cess), which
        # the S.No-only variant below would miss.
        multi_row = re.search(
            r"for\s+(?:S\.?\s*Nos?\.?|serial\s+numbers?)\s+(" + _SNO_LIST_PAT + r")"
            r"(?:\s+and\s+the\s+(?:corresponding\s+)?entries\s+relat\w+\s+thereto)?"
            r".*?(?:following|below).*?shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if multi_row:
            sno_str = multi_row.group(1)
            snos = _extract_sno_list(sno_str)
            entries = _parse_quoted_entries(text)
            if entries:
                payload_sno = snos[0] if snos else entries[0].get("sno", "")
                for e in entries:
                    if not e.get("sno"):
                        e["sno"] = payload_sno
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_ROW",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": payload_sno, "new_entries": entries, "sno_list": snos},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        serial_row = re.search(
            r"for\s+serial\s+number\s+(\d+[A-Z]*)\s+and\s+the\s+entries\s+relat\w+\s+thereto,?\s*"
            r"(?:the\s+following\s+serial\s+number|the\s+following)\s.*?"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if serial_row:
            sno = serial_row.group(1)
            entries = _parse_quoted_entries(text)
            if entries:
                for e in entries:
                    if not e.get("sno"):
                        e["sno"] = sno
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_ROW",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "new_entries": entries},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Row substitution: "for S. No. X ... following ... shall be substituted"
        # "the corresponding entries relating thereto" variant (matching multi_row).
        row_sub = re.search(
            r"for\s+S\.?\s*No\.?\s*(\d+[A-Z]*)"
            r"(?:\s+and\s+the\s+(?:corresponding\s+)?entries\s+relat\w+\s+thereto)?"
            r".*?(?:following|below).*?shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if row_sub:
            sno = row_sub.group(1)
            entries = _parse_quoted_entries(text)
            if entries:
                for e in entries:
                    if not e.get("sno"):
                        e["sno"] = sno
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_ROW",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "new_entries": entries},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # "after S. No. X ... following S. No.(s) ... shall be substituted"
        # The "after S. No. X" phrasing names an *insertion anchor*, not the row
        # being replaced — the gazette sometimes writes "shall be substituted"
        # where it means "shall be inserted" (e.g. 6/2018 Sched II clauses (i)
        # and (ii): "after S. No. 32A ... '32AA 1704 Sugar boiled confectionery'
        # shall be substituted"). Treating this as RATE_SUBSTITUTE_ROW would
        # destroy the anchor entry (32A); emit RATE_INSERT_ENTRIES instead so
        # the new serial number is appended after the anchor.
        row_sub2 = re.search(
            r"after\s+S\.?\s*No\.?\s*(\d+[A-Z]*)"
            r"(?:\s+and\s+the\s+entries\s+relat\w+\s+thereto)?"
            r".*?following\s+(?:serial\s+number|S\.?\s*No\.?).*?shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if row_sub2:
            after_sno = row_sub2.group(1)
            entries = _parse_quoted_entries(text)
            if entries:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_INSERT_ENTRIES",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"after_sno": after_sno, "entries": entries},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Dual-column substitution: "in S. No. X, for the entry in columns (2) and (3), the following entries shall be substituted"
        dual_col = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"for\s+the\s+entry\s+in\s+columns\s+\(2\)\s+and\s+\(3\)\s*,?\s*"
            r".*?shall\s+be\s+substituted.*?"
            r"[\u201c\"](.+?)[\u201d\"]",
            text, re.I | re.DOTALL,
        )
        if dual_col:
            sno = dual_col.group(1)
            raw_val = _clean(dual_col.group(2))
            entries_parsed = _parse_quoted_entries(f'"{raw_val}"')
            if entries_parsed:
                for e in entries_parsed:
                    # The quoted value for a columns-(2)-and-(3) substitution
                    # is "tariff description" — the S.No comes from the clause,
                    # not the quote. When the leading tariff is a 4-digit
                    # heading (e.g. "6702 Artificial flowers…"), the generic
                    # entry parser mis-captures it as the sno; promote it back
                    # into tariff_item and stamp the clause's S.No instead.
                    if e.get("sno", "").isdigit() and len(e.get("sno", "")) >= 4 \
                            and not e.get("tariff_item"):
                        e["tariff_item"] = e["sno"]
                    e["sno"] = sno
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_ROW",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "new_entries": entries_parsed},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events
            tariff_tokens = []
            desc_tokens = []
            in_desc = False
            for tok in raw_val.split():
                if not in_desc and (re.match(r"^[\d,\s]+$", tok) or tok == "Any" or tok == "chapter"):
                    tariff_tokens.append(tok)
                else:
                    in_desc = True
                    desc_tokens.append(tok)
            tariff = _clean(" ".join(tariff_tokens))
            description = _clean(" ".join(desc_tokens))
            if tariff:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": 2, "new_value": tariff},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
            if description:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": 3, "new_value": description},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
            return events

        # "in column (2), for the figure/figures, 'X', 'Y' shall be substituted"
        fig_sub = re.search(
            r"in\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"in\s+column\s+\(?(2)\)?\s*,?\s*"
            r"for\s+the\s+(?:figure|figures|entry).*?"
            r"[\u201c\"](.+?)[\u201d\"].*?"
            r"(?:figures?|entry)\s*"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if fig_sub:
            sno = fig_sub.group(1)
            new_val = _clean(fig_sub.group(4))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_COLUMN",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "column": 2, "new_value": new_val},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        entry_sub = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"in\s+(?:the\s+)?entry\s+in\s+column\s+\(?(\d)\)?\s*,?\s*"
            r"for\s+the\s+entry,?\s*(?:the\s+entry|the\s+following\s+entry)\s*"
            r"(?:shall\s+be\s+substituted\s*,?\s*namely:?\s*-?\s*)?"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"(?:shall\s+be\s+substituted)?",
            text, re.I | re.DOTALL,
        )
        if entry_sub:
            sno = entry_sub.group(1)
            col = int(entry_sub.group(2))
            new_val = _clean(entry_sub.group(3))
            if new_val:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": col, "new_value": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Column substitution: "in S. No. X, for the entry in column (N), ..."
        col_sub = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"(?:for|in)\s+the\s+entry\s+in\s+column\s+(?:no\.?\s*)?\(?(\d)\)?\s*,?"
            r"(?:\s*the\s+(?:entry|following\s+entry)\s*,?)?"
            r"\s*(?:shall\s+be\s+substituted\s*,?\s*namely:?\s*-?\s*)?"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"(?:shall\s+be\s+substituted)?",
            text, re.I | re.DOTALL,
        )
        if col_sub:
            sno = col_sub.group(1)
            col = int(col_sub.group(2))
            new_val = _clean(col_sub.group(3))
            # When the captured value carries an unbalanced opening smart
            # quote ("\u201c" with no matching "\u201d"), the non-greedy match
            # truncated it at a nested inner closing quote — e.g. 34/2017
            # SIII 63 "...known as "dental wax" or as "dental impression
            # compounds"...". Re-extract the full value with balanced-quote
            # handling so the substitution carries the complete text.
            if new_val.count("\u201c") > new_val.count("\u201d"):
                fuller = _extract_quoted_value(text, "shall be substituted")
                if fuller:
                    new_val = fuller
            if new_val:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": col, "new_value": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        # Column substitution with reversed word order:
        # "against S. No. X, in column (N), for the entry, the following entry
        #  shall be substituted, namely:- "..."" (column named before "for the
        #  entry" and the quoted value follows "shall be substituted, namely:-").
        col_entry_rev = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"in\s+column\s+\(?(\d)\)?\s*,?\s*"
            r"for\s+the\s+entry,?\s*"
            r"(?:the\s+(?:following\s+)?entry\s*)?"
            r"(?:shall\s+be\s+substituted\s*,?\s*namely:?\s*-?\s*)?"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"(?:shall\s+be\s+substituted)?",
            text, re.I | re.DOTALL,
        )
        if col_entry_rev:
            sno = col_entry_rev.group(1)
            col = int(col_entry_rev.group(2))
            new_val = _clean(col_entry_rev.group(3))
            if new_val:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": col, "new_value": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

        col2_entries = re.search(
            r"in\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"for\s+the\s+entries\s+in\s+column\s+\(?(2)\)?\s*,?\s*"
            r"(?:the\s+entries\s*)?"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if col2_entries:
            sno = col2_entries.group(1)
            new_val = _clean(col2_entries.group(3))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_COLUMN",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "column": 2, "new_value": new_val},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        # Portion substitution: "for the portion beginning with... and ending with... the words..."
        # The "the words" qualifier after "beginning with" is optional: some
        # gazette clauses drop it (e.g. 7/2022 clause (v) for 2/2017 S.Nos
        # 65-75: "for the portion beginning with "[other than those" and
        # ending with the words ..."), where the opening bracket follows the
        # quote immediately.
        portion_match = re.search(
            r"(?:against|in)\s+(?:S\.?\s*Nos?\.?|serial\s+numbers?)\.?\s*(" + _SNO_LIST_PAT + r").*?"
            r"in\s+column\s+\(3\).*?"
            r"for\s+the\s+portion\s+beginning\s+with\s+(?:the\s+words\s*)?"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*.*?"
            r"ending\s+with\s+the\s+words\s+(?:and\s+bracket\s+)?"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*.*?"
            r"(?:the\s+words|figures)\s*"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if portion_match:
            sno_str = portion_match.group(1)
            snos = _extract_sno_list(sno_str)
            begin_words = _clean(portion_match.group(2))
            end_words = _clean(portion_match.group(3))
            new_words = _clean(portion_match.group(4))
            payload_sno = snos[0] if len(snos) == 1 else ""
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_PORTION",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={
                    "sno": payload_sno, "sno_list": snos,
                    "begin_words": begin_words, "end_words": end_words,
                    "new_words": new_words,
                },
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        brackets_sub = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"(?:in\s+(?:the\s+)?entry\s+in\s+column\s+\(3\)\s*,?\s*)?"
            r"(?:for\s+)?the\s+(?:brackets,?\s+words\s+and\s+figures|comma\s+and\s+words|words,?\s+figure\s+and\s+brackets|words\s+figure\s+and\s+brackets)\s*,?\s*"
            r"[\u201c\"\u2018']([^\u201d\"\u2019']+?)[\u201d\"\u2019']\s*,?\s*"
            r"(?:.*?)(?:the\s+(?:brackets,?\s+words\s+and\s+figures|comma\s+and\s+words|words|figures|entry))\s*"
            r"[\u201c\"\u2018']([^\u201d\"\u2019']+?)[\u201d\"\u2019']\s*,?\s*"
            r"(?:shall\s+be\s+(?:substituted|inserted|omitted))",
            text, re.I | re.DOTALL,
        )
        if brackets_sub:
            sno = brackets_sub.group(1)
            old_words = _clean(brackets_sub.group(2))
            new_words = _clean(brackets_sub.group(3))
            if "shall be omitted" in text_low:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_OMIT_WORDS",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "words": old_words},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
            elif "shall be inserted" in text_low:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_INSERT_WORDS",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "after_words": old_words, "insert_words": new_words},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
            else:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_WORDS",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "old_words": old_words, "new_words": new_words},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
            return events

        figure_sub = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"in\s+column\s+\((\d)\)\s*,?\s*"
            r"for\s+the\s+(?:words?\s+and\s+)?figures?\s*"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"(?:.*?)(?:figures?|entry)\s*"
            r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if figure_sub:
            sno = figure_sub.group(1)
            col = int(figure_sub.group(2))
            new_val = _clean(figure_sub.group(4))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_COLUMN",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "column": col, "new_value": new_val},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        # Word substitution: "for the words 'X' ... 'Y' shall be substituted".
        # The S.No. reference may be written as "serial number" (singular) — e.g.
        # Notification 28/2017 clauses (i)/(iv)/(v) — and the second quoted value
        # may be introduced by "the words, brackets and letters" (the same phrase
        # already accepted by the BATCH substitution pattern below).
        words_sub = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\.?\s*(\d+[A-Z]*).*?"
            r"in\s+column\s+\(3\)\s*,?\s*.*?"
            r"for\s+the\s+(?:words?|figures.*?|figures\s+and\s+(?:letters|word).*?|brackets\s+and\s+words?|words\s+and\s+(?:figures?|brackets?).*?)\s*,?\s*"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']+,?\s*.*?"
            r"(?:the\s+(?:words?(?:,?\s*brackets\s+and\s+letters)?|figures.*?|figures\s+and\s+(?:letters|word)|brackets\s+and\s+words?|words(?:,|\s+and\s+)?(?:figures?|brackets?|figure).*?)\s*,?)\s*"
            r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if words_sub:
            sno = words_sub.group(1)
            old_words = _clean(words_sub.group(2))
            new_words = _clean(words_sub.group(3))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "old_words": old_words, "new_words": new_words},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        # "respectively be substituted" — multiple parallel substitutions
        resp_match = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*).*?"
            r"for\s+the\s+(?:figures\s+and\s+(?:word|letters)|words).*?"
            r"[\u201c\"](.+?)[\u201d\"].*?and\s+the\s+figures\s+and\s+(?:letters|word).*?"
            r"[\u201c\"](.+?)[\u201d\"].*?"
            r"[\u201c\"](.+?)[\u201d\"].*?"
            r"respectively\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if resp_match:
            sno = resp_match.group(1)
            old1 = _clean(resp_match.group(2))
            old2 = _clean(resp_match.group(3))
            new1 = _clean(resp_match.group(4))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "old_words": old1, "new_words": new1,
                         "also_old": old2, "note": "respectively_substituted"},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        # General word/figure/bracket/letter qualifier substitution. Catches
        # the many gazette phrasings that the specific handlers above miss,
        # e.g. "for the words and figure 'X', the words, figure and brackets
        # 'Y' shall be substituted" (27/2017), "for the brackets, words and
        # figures 'X', the brackets, words, figures and letters 'Y' shall be
        # substituted" (18/2018), or a bare "for the words 'X', the words and
        # brackets 'Y' shall be substituted" (41/2017 Schedule V). The
        # qualifier fragment accepts any combination of words/figures/brackets/
        # letters/symbol(s) joined by commas and/or "and". Placed after the
        # specific word/figure handlers so they take precedence, and before the
        # column fallback so two-quote substitutions are not demoted to column
        # edits.
        _Q = r"(?:words?|figures?|brackets?|letters?|symbol(?:s)?)(?:(?:\s*,?\s*|\s+and\s+)(?:words?|figures?|brackets?|letters?|symbol(?:s)?))*"
        qual_sub = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)\s*,?\s*"
            r"(?:in\s+(?:the\s+)?entry\s+in\s+column\s+\(3\)\s*,?\s*)?"
            r"(?:in\s+column\s+\(3\)\s*,?\s*)?"
            r"for\s+the\s+" + _Q + r"\s*,?\s*"
            r"[\u201c\"\u2018']([^\u201d\"\u2019']+?)[\u201d\"\u2019']\s*,?\s*"
            r"(?:.*?)(?:the\s+" + _Q + r")\s*,?\s*"
            r"[\u201c\"\u2018']([^\u201d\"\u2019']+?)[\u201d\"\u2019']\s*,?\s*"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if qual_sub:
            sno = qual_sub.group(1)
            old_words = _clean(qual_sub.group(2))
            new_words = _clean(qual_sub.group(3))
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUBSTITUTE_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno": sno, "old_words": old_words, "new_words": new_words},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

        # Fallback column substitution: extract S.No.+column, then find quoted
        # value. This runs LAST among the substitute patterns so that the more
        # specific word/figure-substitution handlers (figure_sub / words_sub /
        # resp_match) take precedence: a clause with two quoted values such as
        # "for the figures and letters '68 cm', the figures and word '32 inches'
        # shall be substituted" is a RATE_SUBSTITUTE_WORDS, not a full-column
        # replacement (Notification 24/2018 clauses vi & vii).
        col_sno_fallback = re.search(
            r"(?:against|in)\s+S\.?\s*No\.?\s*(\d+[A-Z]*)\s*,?\s*"
            r"(?:for|in)\s+the\s+entry\s+in\s+column\s+(?:no\.?\s*)?\(?(\d)\)?",
            text, re.I,
        )
        if col_sno_fallback and "shall be substituted" in text_low:
            sno = col_sno_fallback.group(1)
            col = int(col_sno_fallback.group(2))
            new_val = _extract_quoted_value(text, "shall be substituted")
            if new_val:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": col, "new_value": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                ))
                return events

    # ── BATCH substitution (multiple S.Nos) ──────────────────────────────
    batch_match = re.search(
        r"(?:against|in)\s+(?:serial\s+numbers|S\.?\s*Nos?)\.?\s*(" + _SNO_LIST_PAT + r")\s*,?\s*"
        r"(?:in\s+column\s+\(3\)\s*,?\s*)?"
        r"for\s+the\s+(?:words|figures.*?|brackets\s+and\s+words).*?"
        r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*.*?"
        r"(?:the\s+(?:words(?:,?\s*brackets\s+and\s+letters)?|figures.*?|brackets\s+and\s+words|figures\s+and\s+(?:letters|word)))\s*"
        r"[\u201c\"](.+?)[\u201d\"]\s*,?\s*.*?"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if batch_match:
        snos = _extract_sno_list(batch_match.group(1))
        old_words = _clean(batch_match.group(2))
        new_words = _clean(batch_match.group(3))
        if snos:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_BATCH_SUBSTITUTE_WORDS",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno_list": snos, "old_words": old_words, "new_words": new_words},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # ── RENUMBER ─────────────────────────────────────────────────────────
    if has_renumber:
        renumber_match = re.search(
            r"S\.?\s*No\.?\s*(\d+[A-Z]*)\s+shall\s+be\s+re-?numbered\s+as\s+S\.?\s*No\.?\s*(\d+[A-Z]*)",
            text, re.I,
        )
        if renumber_match:
            old_sno = renumber_match.group(1)
            new_sno = renumber_match.group(2)
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_RENUMBER",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"old_sno": old_sno, "new_sno": new_sno},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            # Check if there's also an insert before the renumbered entry
            insert_match = re.search(
                r"before\s+S\.?\s*No\.?\s*\w+.*?(?:following|below).*?shall\s+be\s+inserted",
                text, re.I | re.DOTALL,
            )
            if insert_match:
                entries = _parse_quoted_entries(text)
                if entries:
                    events.append(RateAmendmentEvent(
                        event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                        operation="RATE_INSERT_ENTRIES",
                        target_notification=target_notification,
                        target_schedule=current_schedule,
                        payload={"after_sno": "", "entries": entries, "before_sno": new_sno},
                        effective_date=effective_date,
                        publication_date=publication_date,
                        source_notification=source_id,
                        source_cbic_no=source_cbic_no,
                        clause_ref=clause_ref,
                    ))
            return events

    # ── SUPERSESSION ─────────────────────────────────────────────────────
    if "supersedes" in text_low or "supersession" in text_low:
        sup_match = re.search(
            r"supersed?\w*\s+notification\s+No\.?\s*(\d+)/(\d{4})",
            text, re.I,
        )
        if sup_match:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_SUPERSEDE",
                target_notification=f"{sup_match.group(1)}/{sup_match.group(2)}-ct-rate",
                target_schedule="",
                payload={"superseding_notification": source_id},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
            ))
            return events

    # ── OPENING PARAGRAPH amendment ──────────────────────────────────────
    if "opening paragraph" in text_low:
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_AMEND_OPENING",
            target_notification=target_notification,
            target_schedule="",
            payload={"text": _clean(text)},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # ── Service-schedule fallbacks (no "S. No." anchor) ────────────────
    # These catch services-schedule (11/2017) amendments scoped to a
    # paragraph / item / sub-item / column / clause context that the generic
    # "S. No."-anchored patterns above do not recognise. Checked late so the
    # specific handlers take precedence; only genuinely unmatched clauses
    # reach this point.

    # Reverse-direction substitution: "the word 'X' ... shall be substituted
    # by the symbol/words 'Y'" (Notification 3/2019 clause (b)). The old
    # value precedes "shall be substituted by" rather than following "for the".
    rev_sub = re.search(
        r"the\s+" + _WORD_QUAL + r"\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*.*?"
        r"shall\s+be\s+substituted\s+by\s+(?:the\s+)?" + _WORD_QUAL + r"\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']",
        text, re.I | re.DOTALL,
    )
    if rev_sub:
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "old_words": _clean(rev_sub.group(1)),
                     "new_words": _clean(rev_sub.group(2))},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Item-scoped column entry substitution: "in items (iii) and (vi), in
    #  column (5), for the existing entry, the following entry shall be
    #  substituted" — column 5 is the Condition column in the services
    #  schedule (Notification 31/2017 clause (C)). Also catches single-item
    #  column (3) entry substitution (46/2017 (d), 3/2019 (f)).
    item_col_entry = re.search(
        r"in\s+items?\s+((?:\([ivx]+[a-z]?\)\s*(?:,\s*|\s+and\s+)*)+)"
        r"\s*,?\s*in\s+column\s+\(([3-5])\)\s*,?\s*"
        r"for\s+the\s+(?:existing\s+)?entry.*?shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if item_col_entry:
        items_raw = item_col_entry.group(1)
        item_list = re.findall(r"\(([ivx]+[a-z]?)\)", items_raw)
        col = int(item_col_entry.group(2))
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        new_val = _extract_quoted_value(text, "shall be substituted")
        payload_c: dict[str, Any] = {"sno": sno_m.group(1) if sno_m else "",
                                     "column": col, "new_value": new_val,
                                     "item_list": item_list,
                                     "raw_text": _clean(text)[:500]}
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload=payload_c,
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Serial-number-scoped explanation substitution: "against serial number
    #  X, in column (3), for the Explanation, the following explanation shall
    #  be substituted" (15/2025 clause (xiv)).
    sno_expl = re.search(
        r"against\s+serial\s+number\s+(\d+[A-Z]*)\s*,?\s*"
        r"in\s+column\s+\(3\)\s*,?\s*"
        r"for\s+the\s+Explanation.*?shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if sno_expl:
        sno = sno_expl.group(1)
        new_val = _extract_quoted_value(text, "shall be substituted")
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno, "column": 3, "new_value": new_val,
                     "scope": "explanation",
                     "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Item-scoped explanation insertion/substitution: "in item (ie), the
    #  following explanation shall be inserted" (6/2023 (i), 15/2025 (B)).
    item_expl = re.search(
        r"in\s+items?\s+\(([ivx]+[a-z]?)\)\s*,?\s*"
        r"(?:the\s+)?(?:following\s+)?explanation\s+shall\s+be\s+(inserted|substituted)",
        text, re.I | re.DOTALL,
    )
    if item_expl:
        item_id = item_expl.group(1)
        verb = item_expl.group(2)
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        new_val = _extract_quoted_value(text, "shall be " + verb)
        op = "RATE_SUBSTITUTE_COLUMN" if verb == "substituted" else "RATE_INSERT_WORDS"
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation=op,
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "", "item": item_id,
                     "column": 3, "new_value": new_val,
                     "scope": "explanation",
                     "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Condition insertion scoped to column (5) (the Condition column):
    #  "after the condition in column (5) ... the following condition shall
    #  be inserted" (12/2023), "in the conditions in column (5) ... the
    #  following clause shall be inserted" (2/2021), or a bare "in column
    #  (5), the following shall be inserted" (15/2025 (II)). The column-(5)
    #  reference and the "shall be inserted" verb may be separated by proviso /
    #  clause context in any order, so only the two anchors are required and
    #  the sno/item are recovered from the full clause text.
    cond5 = re.search(
        r"in\s+column\s+\(5\).*?shall\s+be\s+inserted",
        text, re.I | re.DOTALL,
    )
    if cond5:
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        im = re.search(r"in\s+items?\s+\(([ivx]+[a-z]?)\)", text, re.I)
        sno = sno_m.group(1) if sno_m else ""
        item_id = im.group(1) if im else ""
        new_val = _extract_quoted_value(text, "shall be inserted")
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_INSERT_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno, "item": item_id, "column": 5,
                     "insert_words": new_val, "scope": "condition",
                     "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Sub-item figure substitution: "against sub-item (b) of item (iii) in
    #  column (4), for the figure '6', the figure '9' shall be substituted"
    #  (15/2025 clause (A)).
    subitem_fig = re.search(
        r"against\s+sub-?items?\s+\(([a-z])\)\s+of\s+items?\s+\(([ivx]+[a-z]?)\)"
        r"\s+in\s+column\s+\((\d)\).*?"
        r"for\s+the\s+(?:figures?|entry|figures\s+and\s+(?:letters|word)).*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"(?:.*?)(?:figures?|entry|figures\s+and\s+(?:letters|word)).*?"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019'].*?"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if subitem_fig:
        sub_id = subitem_fig.group(1)
        item_id = subitem_fig.group(2)
        col = int(subitem_fig.group(3))
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_COLUMN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "", "column": col,
                     "item": item_id, "sub_item": sub_id,
                     "new_value": _clean(subitem_fig.group(5)),
                     "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Sub-item / sub-clause omission: "sub-items (e), (ea) and (h) shall be
    #  omitted" (3/2022 (VIII)) or "sub-clause(h) shall be omitted"
    #  (6/2023 (iii)). The whitespace between the keyword and the list is
    #  optional to tolerate the gazette's "sub-clause(h)" with no space.
    sub_omit = re.search(
        r"sub-?(?:items?|clauses?)\s*((?:\([a-z0-9]+\)\s*(?:,\s*|\s+and\s+)*)+)\s*,?\s*"
        r"shall\s+be\s+omitted",
        text, re.I,
    )
    if sub_omit:
        sub_list = re.findall(r"\(([a-z0-9]+)\)", sub_omit.group(1))
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        item_m = re.search(r"in\s+items?\s+\(([ivx]+[a-z]?)\)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_OMIT_ENTRIES",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "item": item_m.group(1) if item_m else "",
                     "sub_item_list": sub_list,
                     "raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Sub-item-scoped word substitution: "in sub-item (a), for the word
    #  'excluding', the word 'including' shall be substituted" (1/2018 (I)).
    subitem_word = re.search(
        r"in\s+sub-?items?\s+\(([a-z])\)\s*,?\s*"
        r"for\s+the\s+" + _WORD_QUAL + r"\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"(?:.*?)(?:the\s+" + _WORD_QUAL + r")\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if subitem_word:
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        item_m = re.search(r"in\s+items?\s+\(([ivx]+[a-z]?)\)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "item": item_m.group(1) if item_m else "",
                     "sub_item": subitem_word.group(1),
                     "old_words": _clean(subitem_word.group(2)),
                     "new_words": _clean(subitem_word.group(3))},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Paragraph-scoped word substitution: "in paragraph N, for the words
    #  'X', the words 'Y' shall be substituted" (31/2017 clause (ii), after
    #  the "inparagraph" → "in paragraph" fixup above).
    para_word = re.search(
        r"in\s+paragraph\s+(\d+[A-Z]?)\s*,?\s*"
        r"for\s+the\s+" + _WORD_QUAL + r"\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"(?:.*?)(?:the\s+" + _WORD_QUAL + r")\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if para_word:
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": "", "paragraph": para_word.group(1),
                     "old_words": _clean(para_word.group(2)),
                     "new_words": _clean(para_word.group(3))},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Bare word insertion (no "S. No." anchor): "after the word, brackets
    #  and figures 'X', the words and figure 'Y' shall be inserted". The
    #  second qualifier may or may not be preceded by "the" (e.g. 6/2021
    #  (a): "word, figures and letters ' or 12AB'").
    bare_insert = re.search(
        r"after\s+the\s+" + _WORD_QUAL + r"\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"(?:.*?)(?:(?:the\s+)?(?:words?|figures?|brackets?|letters?|symbol(?:s)?|numbers?)"
        r"(?:(?:\s*,?\s*|\s+and\s+)(?:words?|figures?|brackets?|letters?|symbol(?:s)?|numbers?))*)\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"shall\s+be\s+inserted",
        text, re.I | re.DOTALL,
    )
    if bare_insert:
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        item_m = re.search(r"in\s+items?\s+\(([ivx]+[a-z]?)\)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_INSERT_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "item": item_m.group(1) if item_m else "",
                     "after_words": _clean(bare_insert.group(1)),
                     "insert_words": _clean(bare_insert.group(2))},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # Bare word substitution (no "S. No." anchor): "for the words, figures
    #  and letters 'X', the words, figures and letters 'Y' shall be
    #  substituted" (3/2019 (a), 6/2023 (a)). Most general two-quote
    #  substitution; checked last so the scoped handlers above win.
    bare_word_sub = re.search(
        r"for\s+the\s+" + _WORD_QUAL + r"\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"(?:.*?)(?:the\s+" + _WORD_QUAL + r")\s*,?\s*"
        r"[\u201c\"\u2018']([^\u201d\"\u2019]+?)[\u201d\"\u2019']\s*,?\s*"
        r"shall\s+be\s+substituted",
        text, re.I | re.DOTALL,
    )
    if bare_word_sub:
        sno_m = re.search(
            r"(?:against|in)\s+(?:S\.?\s*No\.?|serial\s+number)\s*(\d+[A-Z]*)", text, re.I)
        item_m = re.search(r"in\s+items?\s+\(([ivx]+[a-z]?)\)", text, re.I)
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_SUBSTITUTE_WORDS",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"sno": sno_m.group(1) if sno_m else "",
                     "item": item_m.group(1) if item_m else "",
                     "old_words": _clean(bare_word_sub.group(1)),
                     "new_words": _clean(bare_word_sub.group(2))},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
        ))
        return events

    # ── AUTO-EXTRACT: try to extract entries from unrecognized clauses ───
    if "shall be" in text_low:
        entries = _parse_quoted_entries(text)
        if entries:
            if "substitut" in text_low:
                sno_match = re.search(r"S\.?\s*No\.?\s*(\d+[A-Z]*)", text, re.I)
                if sno_match:
                    target_sno = sno_match.group(1)
                    for e in entries:
                        e["sno"] = target_sno
                    events.append(RateAmendmentEvent(
                        event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                        operation="RATE_SUBSTITUTE_ROW",
                        target_notification=target_notification,
                        target_schedule=current_schedule,
                        payload={"sno": sno_match.group(1), "new_entries": entries},
                        effective_date=effective_date,
                        publication_date=publication_date,
                        source_notification=source_id,
                        source_cbic_no=source_cbic_no,
                        clause_ref=clause_ref,
                        status="auto_extracted",
                    ))
                    return events
            elif "insert" in text_low or "added" in text_low:
                after_match = re.search(r"after\s+S\.?\s*No\.?\s*(\d+[A-Z]*)", text, re.I)
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_INSERT_ENTRIES",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"after_sno": after_match.group(1) if after_match else "", "entries": entries},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                    status="auto_extracted",
                ))
                return events

        words_quote = re.search(
            r"in\s+S\.?\s*No\.?\s*(\d+[A-Z]*).*?"
            r"in\s+column\s+\((\d)\).*?"
            r"for\s+the\s+(?:words?.*?|entries|figures.*?)\s*"
            r"[\u201c\"]([^\u201d\"]+)[\u201d\"].*?"
            r"(?:the\s+(?:words?|entries|figures.*?))\s*"
            r"[\u201c\"]([^\u201d\"]+)[\u201d\"].*?"
            r"shall\s+be\s+substituted",
            text, re.I | re.DOTALL,
        )
        if words_quote:
            sno = words_quote.group(1)
            col = int(words_quote.group(2))
            old_val = _clean(words_quote.group(3))
            new_val = _clean(words_quote.group(4))
            if col == 2:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": 2, "new_value": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                    status="auto_extracted",
                ))
                return events
            else:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                    operation="RATE_SUBSTITUTE_WORDS",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "old_words": old_val, "new_words": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=source_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref=clause_ref,
                    status="auto_extracted",
                ))
                return events

        omit_simple = re.search(
            r"S\.?\s*No\.?\s*(\d+[A-Z]*)\s+(?:and\s+(?:the\s+)?entries\s+(?:thereof|relat\w+\s+thereto))\s*,?\s*shall\s+be\s+omitted",
            text, re.I,
        )
        if omit_simple:
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
                operation="RATE_OMIT_ENTRIES",
                target_notification=target_notification,
                target_schedule=current_schedule,
                payload={"sno_list": [omit_simple.group(1)]},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=source_id,
                source_cbic_no=source_cbic_no,
                clause_ref=clause_ref,
                status="auto_extracted",
            ))
            return events

    # ── Unrecognized clause — flag for review ────────────────────────────
    if text.strip() and len(text) > 20:
        events.append(RateAmendmentEvent(
            event_id=f"evt_rate_{_eid(source_id, clause_ref, text)}",
            operation="RATE_UNKNOWN",
            target_notification=target_notification,
            target_schedule=current_schedule,
            payload={"raw_text": _clean(text)[:500]},
            effective_date=effective_date,
            publication_date=publication_date,
            source_notification=source_id,
            source_cbic_no=source_cbic_no,
            clause_ref=clause_ref,
            status="needs_review",
            review_reasons=["unrecognized_clause_pattern"],
        ))

    return events


# ── corrigendum parser ───────────────────────────────────────────────────────

# Gazette page-to-schedule boundaries for notification 1/2017 (G.S.R. 673(E)).
# Each entry is (max_page_inclusive, schedule_id).  Derived from calibration
# points in the 30-Jun, 12-Jul and 27-Jul 2017 corrigenda cross-referenced
# against the schedule entries in the original notification XML:
#   page 241 → S.No 35 "Coffee"            → Schedule I
#   page 249 → S.No 234 "84 or 85"         → Schedule I
#   page 259 → insert 16A                  → Schedule II
#   page 260 → S.No 47 "2202 90 10"        → Schedule II
#   page 272 → omit "other than those"     → Schedule III (272 > 268)
#   page 290 → omit "goggles..."           → Schedule III (290 ≤ 292)
#   page 293 → S.No 11 "2202 90 90"        → Schedule IV
#   page 301 → insert 163A                 → Schedule IV
_PAGE_SCHEDULE_1_2017: list[tuple[int, str]] = [
    (253, "I"),    # pages ≤ 253  → Schedule I   (263 entries)
    (268, "II"),   # pages 254-268 → Schedule II  (242 entries)
    (292, "III"),  # pages 269-292 → Schedule III (453 entries)
    (315, "IV"),   # pages 293-315 → Schedule IV  (228 entries)
    (317, "V"),    # pages 316-317 → Schedule V   (18 entries)
    (9999, "VI"),  # pages 318+    → Schedule VI  (3 entries)
]


def _page_to_schedule(target_notification: str, page: int) -> str:
    """Map a gazette page number to a schedule id for the target notification."""
    page_maps: dict[str, list[tuple[int, str]]] = {
        "1/2017-ct-rate": _PAGE_SCHEDULE_1_2017,
    }
    boundaries = page_maps.get(target_notification)
    if not boundaries:
        return ""
    for max_page, sched in boundaries:
        if page <= max_page:
            return sched
    return boundaries[-1][1] if boundaries else ""


def _extract_corrigendum_target(text: str) -> str | None:
    """Extract target notification from corrigendum preamble.

    Corrigenda use 'In the notification of the Government of India ...
    No. X/YYYY-Central Tax (Rate)' rather than the standard
    'amendments in the notification' phrasing. Also handles Compensation
    Cess (Rate) corrigenda.
    """
    m = re.search(
        r"In\s+the\s+notification\s+of\s+the\s+Government[^.]{0,300}?"
        r"No\.?\s*(\d+)/(\d{4})[-\s]*Central\s+Tax\s*\(Rate\)",
        text, re.I,
    )
    if m:
        return f"{int(m.group(1))}/{m.group(2)}-ct-rate"
    m = re.search(
        r"In\s+the\s+(?:English\s+version\s+of\s+the\s+)?notification\s+of\s+the\s+Government[^.]{0,300}?"
        r"No\.?\s*(\d+)/(\d{4})[-\s]*Compensation\s+Cess\s*\(Rate\)",
        text, re.I,
    )
    if m:
        return f"{int(m.group(1))}/{m.group(2)}-cc-rate"
    return None

def _parse_corrigendum(
    full_text: str,
    *,
    notification_id: str,
    source_cbic_no: str,
    publication_date: str,
    effective_date: str,
    target_notification: str,
) -> list[RateAmendmentEvent]:
    """Parse a corrigendum that uses page-reference amendment language.

    Handles two clause styles:
      1. Page-reference (30-Jun, 12-Jul 2017):
         'at page 243 after line 44, insert- "103A 2302 Bran..."'
         'at page 241, in line 15, for "X", read "Y"'
      2. Schedule-reference (27-Jul 2017):
         'In Schedule I-2.5%,- (i) in S. No. 59, in column (3), for "X", read "Y"'

    All marker types (schedule headers + amendment clauses) are matched in a
    single sequential pass so that the schedule context is tracked correctly
    when a corrigendum spans multiple schedules (e.g. 27-Jul 2017).
    """
    events: list[RateAmendmentEvent] = []
    # Normalise smart quotes for uniform matching
    norm = full_text.replace("\u201c", '"').replace("\u201d", '"')
    norm = _clean(norm)

    # Combined pattern: matches schedule headers AND all amendment clause types,
    # processed in document order so schedule context is tracked correctly.
    combined_pat = re.compile(
        r"(?P<schedule>In\s+Schedule[\s\-]+(?P<sched_id>[IVX]+))"
        r"|"
        r"(?P<insert>at\s+page\s+(?P<ins_page>\d+)\s*,?\s*after\s+line\s+\d+\s*,?\s*"
        r"insert\s*[-\u2013\u2014]?\s*\"(?P<ins_text>.+?)\"\s*[;,.]?)"
        r"|"
        r"(?P<subcol>in\s+S\.?\s*No\.?\s*(?P<sc_sno>\d+[A-Z]*)\s*,?\s*"
        r"in\s+column\s+\((?P<sc_col>\d)\)\s*,?\s*"
        r'for\s*"(?P<sc_old>.+?)"\s*,?\s*read\s*"(?P<sc_new>.+?)")'
        r"|"
        r"(?P<subwords>at\s+page\s+(?P<sw_page>\d+)\s*,?\s*in\s+line\s+\d+\s*,?\s*"
        r'for\s*"(?P<sw_old>.+?)"\s*,?\s*read\s*"(?P<sw_new>.+?)")'
        r"|"
        r"(?P<omit>at\s+page\s+(?P<om_page>\d+)\s*,?\s*in\s+line\s+\d+\s*,?\s*"
        r'omit\s+(?:the\s+words?\s*)?"(?P<om_text>.+?)")',
        re.I | re.DOTALL,
    )

    current_schedule = ""
    for m in combined_pat.finditer(norm):
        # ── Schedule header ──
        if m.group("schedule"):
            current_schedule = m.group("sched_id")
            continue

        # ── INSERT entries ──
        if m.group("insert"):
            page_num = int(m.group("ins_page"))
            quoted = m.group("ins_text")
            entries = _parse_quoted_entries('"' + quoted + '"')
            if not entries:
                continue
            sched = _page_to_schedule(target_notification, page_num) or current_schedule
            first_sno = entries[0].get("sno", "")
            base_num = re.match(r"^(\d+)", first_sno)
            after_sno = base_num.group(1) if base_num else ""
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(notification_id, '', quoted)}",
                operation="RATE_INSERT_ENTRIES",
                target_notification=target_notification,
                target_schedule=sched,
                payload={"after_sno": after_sno, "entries": entries},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=notification_id,
                source_cbic_no=source_cbic_no,
                clause_ref="",
                status="validated",
            ))
            continue

        # ── SUBSTITUTE column ──
        if m.group("subcol"):
            sno = m.group("sc_sno")
            col = int(m.group("sc_col"))
            old_val = _clean(m.group("sc_old"))
            new_val = _clean(m.group("sc_new"))
            if col == 3:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(notification_id, '', sno + str(col) + new_val)}",
                    operation="RATE_SUBSTITUTE_WORDS",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "old_words": old_val, "new_words": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=notification_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref="",
                    status="validated",
                ))
            else:
                events.append(RateAmendmentEvent(
                    event_id=f"evt_rate_{_eid(notification_id, '', sno + str(col) + new_val)}",
                    operation="RATE_SUBSTITUTE_COLUMN",
                    target_notification=target_notification,
                    target_schedule=current_schedule,
                    payload={"sno": sno, "column": col, "new_value": new_val},
                    effective_date=effective_date,
                    publication_date=publication_date,
                    source_notification=notification_id,
                    source_cbic_no=source_cbic_no,
                    clause_ref="",
                    status="validated",
                ))
            continue

        # ── SUBSTITUTE words (page-reference style) ──
        if m.group("subwords"):
            page_num = int(m.group("sw_page"))
            old_val = _clean(m.group("sw_old"))
            new_val = _clean(m.group("sw_new"))
            sched = _page_to_schedule(target_notification, page_num) or current_schedule
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(notification_id, '', old_val + new_val)}",
                operation="RATE_SUBSTITUTE_WORDS",
                target_notification=target_notification,
                target_schedule=sched,
                payload={"sno": "", "old_words": old_val, "new_words": new_val},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=notification_id,
                source_cbic_no=source_cbic_no,
                clause_ref="",
                status="validated",
            ))
            continue

        # ── OMIT words ──
        if m.group("omit"):
            page_num = int(m.group("om_page"))
            words = _clean(m.group("om_text"))
            sched = _page_to_schedule(target_notification, page_num) or current_schedule
            events.append(RateAmendmentEvent(
                event_id=f"evt_rate_{_eid(notification_id, '', 'omit' + words)}",
                operation="RATE_OMIT_WORDS",
                target_notification=target_notification,
                target_schedule=sched,
                payload={"sno": "", "words": words},
                effective_date=effective_date,
                publication_date=publication_date,
                source_notification=notification_id,
                source_cbic_no=source_cbic_no,
                clause_ref="",
                status="needs_review",
                review_reasons=["corrigendum_omit_no_sno"],
            ))
            continue

    return events


# ── notification-level parser ────────────────────────────────────────────────

def compile_amendment_notification(
    xml_path: str | Path,
) -> list[RateAmendmentEvent]:
    """Parse a CT(Rate) amending notification XML into amendment events."""

    xml_path = Path(xml_path)
    props = _get_props(xml_path)
    root = props["_root"]

    notification_id = props.get("canonical_id", str(xml_path))
    cbic_no = props.get("cbic_no", "")
    pub_date = props.get("publication_date", "")
    eff_date = props.get("effective_from", "") or pub_date

    full_text = _all_text(root)

    # Detect corrigendum format and route to corrigendum parser
    is_corrigendum = (
        re.search(r"\bCorrigendum\b", full_text, re.I)
        and re.search(r"In\s+the\s+notification\s+of\s+the\s+Government", full_text, re.I)
    )
    if is_corrigendum:
        target_notif = _extract_corrigendum_target(full_text)
        if target_notif:
            source_cbic_no = cbic_no or f"Corrigendum to {target_notif}-Central Tax (Rate)"
            return _parse_corrigendum(
                full_text,
                notification_id=notification_id,
                source_cbic_no=source_cbic_no,
                publication_date=pub_date,
                effective_date=eff_date,
                target_notification=target_notif,
            )
        return []

    # Identify target notification (the principal being amended)
    target_notif = _extract_target_notification(full_text)
    if not target_notif:
        # Try from doc name
        doc_name = ""
        for doc in root:
            if "doc" in doc.tag:
                doc_name = doc.get("name", "")
                break
        for m in re.finditer(r"notification_no_(\d+)_(\d{4})", doc_name):
            target_notif = f"{m.group(1)}/{m.group(2)}-ct-rate"
            break

    if not target_notif:
        return []

    # Check for per-clause effective dates
    clause_eff_date = _extract_effective_date(full_text)
    if clause_eff_date:
        eff_date = clause_eff_date

    # Extract all <p> elements across all paragraphs into a flat list
    all_p_texts: list[str] = []
    for para in root.iter():
        tag = para.tag.split("}")[-1] if "}" in para.tag else para.tag
        if tag != "paragraph":
            continue
        content_el = para.find("content")
        if content_el is not None:
            for p in content_el:
                ptag = p.tag.split("}")[-1] if "}" in p.tag else p.tag
                if ptag == "p":
                    txt = " ".join(t.strip() for t in p.itertext()).strip()
                    if txt:
                        all_p_texts.append(txt)

    # Group <p> elements into clauses based on clause markers
    CLAUSE_START_RE = re.compile(r"^\s*(?:\([a-zA-Zivx]+\)|\d{1,3}\.(?:\s|$)|[A-Za-z]\.(?:\s|$))")
    # Any parenthesized letter/roman-numeral sub-item marker, e.g. (a), (ii),
    # (iii), (iv). Used to suppress these markers inside unclosed quotes so
    # continuation paragraphs of a quoted entry block are kept together.
    SUB_ITEM_RE = re.compile(r"^\s*\([a-c]\)|^\s*\([ivx]{2,}\)")
    # Broader parenthesized alpha/roman marker, e.g. (LED), (for ...) values
    # that live inside a multi-line quoted entry block. CLAUSE_START_RE's first
    # alternative matches these as fresh clause markers, so they must also be
    # suppressed when we are inside an unclosed quote (cf. 18/2021 clause (xv)
    # for SIII 392, whose quoted value embeds "(LED)").
    PAREN_ALPHA_RE = re.compile(r"^\s*\([a-zA-Zivx]+\)")
    COLUMN_HEADER_RE = re.compile(r"^\s*\([1-5]\)\s*$")
    AMENDMENT_VERB_RE = re.compile(r"shall\s+be\s+(?:inserted|substituted|omitted|added|renumbered)", re.I)
    # Trailing markers indicating the current clause is incomplete and the next
    # <p> is its continuation (e.g. "..., the following" / "namely: -"), even if
    # that next <p> begins with a clause-like token such as "S. Nos. ...".
    CONTINUATION_TAIL_RE = re.compile(
        r"(?:\bthe\s+following\b|\bnamely\b)\b\s*[:\-,]?\s*$", re.I
    )
    clauses: list[tuple[str, str]] = []
    current_clause_texts: list[str] = []
    current_ref = ""

    for p_text in all_p_texts:
        p_clean = _clean(p_text)
        if not p_clean:
            continue
        if COLUMN_HEADER_RE.match(p_clean):
            continue
        # Normalise the gazette abbreviation "Sl. No." → "S. No." so the
        # regex patterns (which match S\.?\s*Nos?\.?) recognise it. Some
        # CT(Rate) amending notifications (e.g. 10/2022) use "Sl. No."
        # instead of the more common "S. No." / "serial number".
        p_clean = re.sub(r"\bSl\.\s*No", "S. No", p_clean)

        current_joined = " ".join(current_clause_texts)
        has_unclosed_quote = current_joined.count("\u201c") > current_joined.count("\u201d")
        ends_with_continuation = bool(current_clause_texts) and bool(
            CONTINUATION_TAIL_RE.search(current_joined)
        )
        # A complete amendment clause terminates with an amendment verb + ";"
        # ("shall be inserted;", "shall be substituted;", ...). When such a
        # terminator ends the clause, any residual quote imbalance is a
        # source-XML typo (a dropped closing quotation mark) rather than a
        # genuine multi-line quoted entry block — so the next parenthesized
        # marker is a fresh clause, not a quoted sub-item continuation.
        # (Notification 14/2019 Schedule III clause (ii) drops its closing
        # quote, which previously mis-merged clauses (iii) and (iv).)
        # The terminating ";" must DIRECTLY follow the verb: a clause whose
        # joined text contains "shall be substituted, namely: -" followed by
        # many paragraphs of quoted table content ending in ";" (e.g.
        # 20/2017 services clauses) is NOT terminated — the ";" belongs to a
        # sub-item inside the quoted block, not to the clause terminator.
        # A clause is "terminated" if it contains an amendment verb AND ends
        # with ; or '; or "; — meaning the next parenthesized marker should
        # start a fresh clause even inside unclosed quotes.
        clause_terminated = bool(
            current_clause_texts
            and re.search(
                r"shall\s+be\s+(?:inserted|substituted|omitted|added|renumbered)",
                current_joined, re.I,
            )
            and re.search(r"['\u201d\"]?\s*;\s*$", current_joined)
        )

        m = CLAUSE_START_RE.match(p_clean)
        is_single_letter = bool(SUB_ITEM_RE.match(p_clean))
        is_paren_alpha = bool(PAREN_ALPHA_RE.match(p_clean))
        is_bare_number = bool(re.match(r"^\d{1,3}\.$", p_clean))
        # A numbered list item inside a quoted block (e.g. "1. The promoter
        # shall maintain project ...") — these are continuation paragraphs of
        # the quoted table content (Annexures, explanatory notes), not fresh
        # top-level operative clauses. Suppressed when inside an unclosed
        # quote by the continuation logic below.
        is_numbered_item = bool(re.match(r"^\d{1,3}\.\s", p_clean))

        # A paragraph that begins with a clause marker but also introduces a
        # genuine amendment (naming a serial number, item, or sub-item to
        # substitute/insert/omit) is a fresh clause, not a quoted sub-item
        # continuation — even when the current clause has an unclosed quote.
        # Without this guard, the unclosed-quote continuation logic below
        # would absorb a new amendment clause (e.g. "(ii) against serial
        # number 8, for item (vi)...") into the preceding quoted block.
        # The check is anchored to the START of the paragraph (right after
        # the clause marker) so that quoted references like "(c) of item
        # (vi), against serial number 3 of the Table above" — where the
        # serial-number mention is in the middle of descriptive text — are
        # NOT mistaken for fresh amendments.
        looks_like_fresh_amendment = bool(
            re.match(
                r"^\s*(?:\([a-zA-Zivx]+\)\s*|[A-Za-z]\.\s*)"
                r"(?:against\s+serial\s+number|"
                r"for\s+(?:item|sub-?item)\s*\(|"
                r"after\s+(?:item|sub-?item)\s*\(|"
                r"in\s+item\s*\([^)]*\)\s*,?\s*in\s+column|"
                r"in\s+sub-?item\s*\(|"
                r"for\s+paragraph\s+\d|"
                r"item\s*\([^)]*\)\s+and\s+the\s+entries)",
                p_clean, re.I,
            )
        )

        # Suppress parenthesized sub-item markers (e.g. (a), (ii), (iii), (iv))
        # and numbered list items (e.g. "1. The promoter ...") when inside
        # unclosed quotes, so continuation paragraphs of a quoted entry block
        # are kept together with the block instead of being ripped off as
        # fresh clause starts. Skip this when the current clause is already
        # terminated (the unclosed quote is a typo, not a real block) or when
        # the paragraph introduces a genuine fresh amendment.
        # The broader PAREN_ALPHA_RE also covers multi-letter markers that are
        # part of the quoted value itself (e.g. "(LED)" inside the SIII 392
        # substitution text of 18/2021 clause (xv)).
        if (has_unclosed_quote and not clause_terminated
                and (is_single_letter or is_paren_alpha or is_numbered_item)
                and not looks_like_fresh_amendment):
            current_clause_texts.append(p_clean)
            continue

        # When inside an unclosed quoted block, a paragraph containing an
        # amendment verb (e.g. 'blocks" shall be substituted.') is completing
        # the current clause — the verb follows the closing quote on the same
        # line — not starting a fresh "verb" clause. Append it so the full
        # clause text stays together (cf. 10/2022 amending 2/2022: "Fly ash /
        # bricks; ... blocks" shall be substituted.").
        if (has_unclosed_quote and not clause_terminated
                and not looks_like_fresh_amendment
                and bool(AMENDMENT_VERB_RE.search(p_clean))
                and not any(skip in p_clean.lower() for skip in [
                    "in exercise", "g.s.r", "government"])):
            current_clause_texts.append(p_clean)
            continue

        # Continuation of an incomplete clause (ends with "the following"/"namely")
        # — keep appending even if the <p> looks like a fresh clause marker.
        if ends_with_continuation:
            current_clause_texts.append(p_clean)
            continue

        # The current clause so far is a bare clause marker (e.g. "(viii)") with
        # no operative body yet — the next <p> is its body even when it begins
        # with a single letter + "." (e.g. "S. No. 243A shall be re-numbered ...",
        # where "S." would otherwise be misread as a fresh clause marker). Append
        # instead of starting a new clause so the clause_ref and text stay together.
        if current_clause_texts and re.match(r"^\s*\([a-zA-Zivx]+\)\s*$", current_joined):
            current_clause_texts.append(p_clean)
            continue

        is_verb_start = bool(AMENDMENT_VERB_RE.search(p_clean)) and not any(
            skip in p_clean.lower() for skip in ["in exercise", "g.s.r", "government"]
        )
        # An amendment intro that mentions "the following" but is split from its
        # verb by a line break ending in a hanging connector (e.g. "...the
        # following S. No. and" / "entries shall be inserted") is awaiting this
        # verb paragraph. Keep them in one clause so the after/before S.No.
        # anchor survives (Notification 18/2023 inserts 2/2017 S.No. 94A).
        intro_awaiting_verb = bool(current_clause_texts) and bool(
            re.search(r"\bthe\s+following\b", current_joined, re.I)
        ) and bool(re.search(r"\b(?:and|or|of)\s*$", current_joined, re.I))
        # A bare "N." is a legitimate top-level operative-clause marker (e.g.
        # "2. This notification shall come into force...") and must start a new
        # clause — UNLESS it occurs inside an unclosed quote, where it is a
        # quoted S.No value (e.g. "6.") that should not break the clause.
        if m and not (is_bare_number and has_unclosed_quote):
            if current_clause_texts:
                clauses.append((current_ref, _clean(" ".join(current_clause_texts))))
            current_ref = m.group(0).strip("(). ")
            current_clause_texts = [p_clean]
        elif is_verb_start and current_ref in ("", "0"):
            if intro_awaiting_verb:
                if current_ref in ("", "0"):
                    current_ref = "verb"
                current_clause_texts.append(p_clean)
            else:
                if current_clause_texts:
                    clauses.append((current_ref, _clean(" ".join(current_clause_texts))))
                current_ref = "verb"
                current_clause_texts = [p_clean]
        else:
            current_clause_texts.append(p_clean)

    if current_clause_texts:
        clauses.append((current_ref, _clean(" ".join(current_clause_texts))))

    # Pre-scan: detect which schedules this notification amends
    all_schedules_mentioned: list[str] = []
    for _, joined in clauses:
        for m in re.finditer(r"Schedule[\s\-]+([IVX]+)", joined, re.I):
            sid = m.group(1)
            if sid not in all_schedules_mentioned:
                all_schedules_mentioned.append(sid)
    # If only one schedule mentioned, use it as default
    default_schedule = all_schedules_mentioned[0] if len(all_schedules_mentioned) == 1 else ""

    # Track current schedule context across clauses
    current_schedule = default_schedule
    all_events: list[RateAmendmentEvent] = []

    # Detect all target notifications for dual-target support
    all_targets: list[str] = [target_notif]
    for m in re.finditer(
        r"No\.?\s*(\d+)/(\d{4})[-\s]*Central\s+Tax\s*\(Rate\)", full_text, re.I
    ):
        t = f"{m.group(1)}/{m.group(2)}-ct-rate"
        if t not in all_targets:
            all_targets.append(t)
    current_target = target_notif

    # Track the current serial-number context across clauses. The services
    # rate schedule (11/2017) groups amendments as a context marker clause
    # ("(i) against serial number 7, in column (3),-") followed by operative
    # sub-clauses ("(a) for item (i) ... shall be substituted") that do not
    # repeat the serial number. The sub-clause regexes therefore capture an
    # empty sno. We record the most recent serial context here and backfill
    # it onto those sub-item events below so they are not silently dropped
    # for lack of a target serial (cf. notification 13/2018 S.No 7 item (i)).
    current_serial = ""

    for clause_ref, joined in clauses:
        if not joined or len(joined) < 10:
            continue

        # Capture serial-number context from a context-marker clause of the
        # form "against serial number 7, in column (3),-". Only update when
        # the clause itself carries no amendment verb: an operative clause
        # that names a serial (e.g. "(ii) against serial number 9, for item
        # (vi) ... shall be substituted") is compiled with its own sno and
        # needs no backfill, and a mid-text cross-reference to another serial
        # must not corrupt the context.
        serial_ctx_m = re.search(
            r"against\s+serial\s+number\s+(\d+[A-Z]*)\s*,\s*in\s+column",
            joined, re.I,
        )
        if serial_ctx_m and not re.search(
            r"shall\s+be\s+(?:inserted|substituted|omitted|added|renumbered)",
            joined, re.I,
        ):
            current_serial = serial_ctx_m.group(1)

        # Skip preamble paragraphs
        # But never skip a genuine amendment clause — some entries quote
        # government references inside their description (e.g. 234A's e-waste
        # entry cites "published in the Gazette of India vide G.S.R. 338 (E)
        # dated the 23rd March, 2016"), which would otherwise trip the preamble
        # markers below and drop the INSERT/SUBSTITUTE event.
        is_amendment_clause = bool(
            re.search(r"shall\s+be\s+(?:inserted|substituted|omitted|added|renumbered)", joined, re.I)
        ) and bool(re.search(r"S\.?\s*Nos?\.?|serial\s+number", joined, re.I))
        if not is_amendment_clause and any(skip in joined.lower() for skip in [
            "[to be published", "government of india", "ministry of finance",
            "department of revenue", "notification no.", "new delhi",
            "come into force", "[f.no", "under secretary", "deputy secretary",
            "note:", "g.s.r",
            "in exercise of the powers",
            "published in the gazette", "dated the", "extraordinary",
            "section 3, sub-section", "part ii",
        ]):
            if "come into force" in joined.lower():
                force_date = _extract_effective_date(joined)
                if force_date:
                    eff_date = force_date
            # Extract schedule context even from preamble clauses, since
            # schedule-switch markers like "A. in Schedule I – 2.5%, -" are
            # often grouped into the same clause as the G.S.R. preamble text.
            sched_m = re.search(r"in\s+Schedule[\s\-]+([IVX]+)", joined, re.I)
            if sched_m:
                sid = sched_m.group(1)
                if sid:
                    current_schedule = sid
            elif not current_schedule and re.search(r"in\s+the\s+Schedule\b", joined, re.I):
                # 7/2018-style notifications group "(1) in the Schedule," into the
                # G.S.R. preamble clause and never name a schedule explicitly; the
                # amended rate notifications open with Schedule I, so default to it.
                current_schedule = "I"
            continue

        # Skip pure schedule-switch clauses ("in Schedule I - 2.5%, -" or "in Schedule-IV-14%,-")
        sched_only = re.match(
            r"^\s*(?:[\(\[]?\w+\.?[\)\]]?\s+)?in\s+Schedule[\s\-]+[IVX]+\s*[–\-—]?\s*[\d.]*\s*%?\s*[,–\-— ]*$",
            joined, re.I,
        )
        if sched_only:
            sid = _extract_schedule_id(joined)
            if sid:
                current_schedule = sid
            continue

        # Skip "(A) in the Schedule,-" context markers
        if re.match(r"^\s*\([A-Za-z]+\)\s*in\s+the\s+Schedule\s*,?\s*[-–]?\s*$", joined, re.I):
            # Some notifications (e.g. 7/2018 amending 1/2017/2/2017) use the
            # generic phrase "in the Schedule," without naming a schedule. The
            # amended rate notifications are organised as Schedule I..VI, and the
            # first set of amendments always lives in Schedule I — fall back to it
            # so the resulting events carry a non-empty target_schedule.
            if not current_schedule:
                current_schedule = "I"
            continue

        # Check for schedule switch at the START of the clause (context marker only).
        # Do NOT use re.search — body-text mentions like "except items in Schedule III"
        # would corrupt the schedule context for all subsequent clauses.
        sched_m = re.match(
            r"^\s*(?:[\(\[]?\w+\.?[\)\]]?\s+)?(?:in\s+)?Schedule[\s\-]+([IVX]+)",
            joined, re.I,
        )
        if sched_m:
            sid = sched_m.group(1)
            if sid:
                current_schedule = sid

        # Fallback: detect "List N" pattern (used in 9/2025 and some amendments)
        # List 1 → Schedule I, List 2 → II, List 3 → III, List 4 → IV, List 5 → V
        if not sched_m:
            list_m = re.search(r"in\s+List\s+([1-5])\b", joined, re.I)
            if list_m:
                list_to_sched = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V"}
                sid = list_to_sched.get(list_m.group(1), "")
                if sid:
                    current_schedule = sid

        # Fallback: if schedule still empty, try any mention
        if not current_schedule:
            sid = _extract_schedule_id(joined)
            if sid:
                current_schedule = sid

        # Fallback: a generic "in the Schedule" reference (no roman numeral)
        # inside an operative clause. The amended rate notifications are
        # organised as Schedule I..VI, and the first set of amendments always
        # lives in Schedule I (e.g. 18/2023 inserts 2/2017 S.No. 94A "after
        # S. No. 94" with only the generic phrase "in the Schedule").
        if not current_schedule and re.search(r"in\s+the\s+Schedule\b", joined, re.I):
            current_schedule = "I"

        # Dual-target: check if this clause switches to a different notification.
        # Only treat it as a switch when the notification reference is a genuine
        # context marker — not when it merely appears inside an operative
        # amendment clause (e.g. an Explanation quoting "notification No.
        # 11/2017-Central Tax (Rate)"). A clause that performs an S.No.
        # insertion/omission/substitution/renumber is amending the *current*
        # target, so any notification numbers it cites are incidental references
        # and must not clear the schedule context (cf. 24/2018 clause (vii),
        # whose Explanation references 11/2017 mid-text and would otherwise drop
        # the Schedule-I context needed by the following 243A/243B clause).
        clause_is_operative = bool(
            re.search(r"shall\s+be\s+(?:inserted|substituted|omitted|added|re-?numbered)", joined, re.I)
        ) and bool(re.search(r"S\.?\s*Nos?\.?|serial\s+number", joined, re.I))
        if not clause_is_operative:
            target_switch = re.search(
                r"No\.?\s*(\d+)/(\d{4})[-\s]*Central\s+Tax\s*\(Rate\)", joined, re.I
            )
            if target_switch:
                switched = f"{target_switch.group(1)}/{target_switch.group(2)}-ct-rate"
                if switched in all_targets and switched != current_target:
                    current_target = switched
                    current_schedule = ""

        # Parse the clause
        clause_events = _parse_clause(
            clause_text=joined,
            target_notification=current_target,
            current_schedule=current_schedule,
            clause_ref=clause_ref,
            effective_date=eff_date,
            publication_date=pub_date,
            source_id=notification_id,
            source_cbic_no=cbic_no,
        )
        # Backfill the tracked serial context onto service-schedule
        # sub-item substitution events whose own clause did not name a
        # serial number. Limited to RATE_SUBSTITUTE_COLUMN /
        # RATE_SUBSTITUTE_ITEM events that carry an item reference, so
        # document-scope or paragraph-scope substitutions (which have no
        # item) and batch/global operations are left untouched.
        if current_serial:
            for ev in clause_events:
                if ev.operation not in ("RATE_SUBSTITUTE_COLUMN", "RATE_SUBSTITUTE_ITEM"):
                    continue
                payload = ev.payload
                if not str(payload.get("sno", "")).strip() and (
                    payload.get("item") or payload.get("item_id")
                ):
                    payload["sno"] = current_serial
        all_events.extend(clause_events)

    return all_events


# ── batch compiler ───────────────────────────────────────────────────────────

# Notifications that amend the main goods rate/exemption schedules
CT_RATE_TARGETS = {
    "1/2017-ct-rate", "2/2017-ct-rate",   # base goods rate + exemption
    "9/2025-ct-rate", "10/2025-ct-rate",  # superseding goods rate + exemption
    "14/2025-ct-rate",                    # bricks rate
    "2/2022-ct-rate",                     # concessional brick rate
    "11/2017-ct-rate",                    # base services rate (notification 11/2017)
}

# Compensation Cess (Rate) targets — the base goods-cess schedule (1/2017)
# and the base services-cess schedule (2/2017).
CESS_RATE_TARGETS = {
    "1/2017-cc-rate", "2/2017-cc-rate",
}


def _detect_superseded_notifications(corpus_dir: Path) -> dict[str, str]:
    """Scan all notification XMLs for supersession language.
    
    Returns a mapping of superseded notification canonical_id → rescinding notification canonical_id.
    Example: {".../14-2021-central-tax-rate": ".../21-2021-central-tax-rate"}
    """
    superseded: dict[str, str] = {}
    for xml_path in sorted(corpus_dir.rglob("*-central-tax-rate.xml")):
        try:
            content = xml_path.read_text(encoding="utf-8")
        except Exception:
            continue
        # Match "in supersession of notification ... No. X/YYYY-Central Tax (Rate)"
        for m in re.finditer(
            r"supersession\s+of\s+notification.*?No\.?\s*(\d+)/(\d{4})[\s\-]*Central\s+Tax\s*\(Rate\)",
            content, re.I | re.DOTALL,
        ):
            sup_no = f"{m.group(1)}/{m.group(2)}"
            # Find the canonical_id of the superseded notification
            sup_year = m.group(2)
            for cand in (corpus_dir / sup_year).glob(f"{m.group(1)}-{sup_year}-central-tax-rate.xml"):
                sup_id = cand.stem
                if sup_id.startswith("/"):
                    sup_canonical = sup_id
                else:
                    # Construct canonical ID
                    parts = str(cand.relative_to(corpus_dir)).replace("\\", "/").replace(".xml", "")
                    sup_canonical = f"/in/union/notifications/cbic/central-tax-rate/{parts}"
                rescinding_parts = str(xml_path.relative_to(corpus_dir)).replace("\\", "/").replace(".xml", "")
                rescinding_canonical = f"/in/union/notifications/cbic/central-tax-rate/{rescinding_parts}"
                # Only supersede if the rescinding notification is LATER
                rescinding_date = ""
                try:
                    props = _get_props(xml_path)
                    rescinding_date = props.get("effective_from", props.get("publication_date", ""))
                except Exception:
                    pass
                try:
                    sup_props = _get_props(cand)
                    sup_date = sup_props.get("effective_from", sup_props.get("publication_date", ""))
                except Exception:
                    sup_date = ""
                if rescinding_date >= sup_date:
                    superseded[sup_canonical] = rescinding_canonical
                    print(f"  SUPERSEDED: {sup_canonical} → rescinded by {rescinding_canonical} (eff: {rescinding_date})")
    return superseded


def compile_all_amendments(
    corpus_dir: str | Path = "corpus/in/union/notifications/cbic/central-tax-rate",
    target_notifications: set[str] | None = None,
    output_path: str | Path = "derived/version_history/rate-schedules/rate_amendment_events.jsonl",
) -> list[RateAmendmentEvent]:
    """Compile all CT(Rate) amending notifications into events."""

    if target_notifications is None:
        target_notifications = CT_RATE_TARGETS

    corpus_dir = Path(corpus_dir)
    
    # Detect superseded (rescinded) notifications
    superseded = _detect_superseded_notifications(corpus_dir)
    
    all_events: list[RateAmendmentEvent] = []

    # Collect all amendment XML paths: top-level *-central-tax-rate.xml files
    # plus corrigenda in {notification}/corrigenda/*.xml subdirectories.
    amendment_xml_paths: list[Path] = []
    for year in sorted(os.listdir(corpus_dir)):
        year_dir = corpus_dir / year
        if not year_dir.is_dir():
            continue
        for fname in sorted(os.listdir(year_dir)):
            if fname.endswith("-central-tax-rate.xml"):
                amendment_xml_paths.append(year_dir / fname)
            elif (year_dir / fname).is_dir():
                # Scan corrigenda subdirectories (e.g. 1-2017/corrigenda/*.xml)
                amendment_xml_paths.extend(
                    sorted((year_dir / fname).rglob("corrigenda/*.xml"))
                )

    for xml_path in amendment_xml_paths:
        try:
            events = compile_amendment_notification(xml_path)
        except Exception as e:
            print(f"  WARNING: error parsing {xml_path.name}: {e}")
            continue

        # Filter to events targeting our notifications of interest
        for evt in events:
            if evt.target_notification in target_notifications:
                all_events.append(evt)

    # Filter out events from superseded (rescinded) notifications
    if superseded:
        before = len(all_events)
        all_events = [
            e for e in all_events
            if e.source_notification not in superseded
        ]
        removed = before - len(all_events)
        if removed:
            print(f"  Removed {removed} events from {len(superseded)} superseded notification(s)")

    # Sort by effective_date, then publication_date
    all_events.sort(key=lambda e: (e.effective_date, e.publication_date))

    # Write output
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for evt in all_events:
            f.write(json.dumps(evt.to_dict(), ensure_ascii=False) + "\n")

    return all_events


def compile_all_cess_amendments(
    corpus_dir: str | Path = "corpus/in/union/notifications/cbic/compensation-cess-rate",
    target_notifications: set[str] | None = None,
    output_path: str | Path = "derived/version_history/rate-schedules/cess_amendment_events.jsonl",
) -> list[RateAmendmentEvent]:
    """Compile all Compensation Cess (Rate) amending notifications into events.

    Mirrors :func:`compile_all_amendments` but scans the ``compensation-cess-rate``
    corpus directory and writes to a separate JSONL so cess events stay isolated
    from the CT(Rate) event stream.
    """

    if target_notifications is None:
        target_notifications = CESS_RATE_TARGETS

    corpus_dir = Path(corpus_dir)
    all_events: list[RateAmendmentEvent] = []

    # Collect all amendment XML paths: top-level *-compensation-cess-rate.xml
    # files plus corrigenda in {notification}/corrigenda/*.xml sub-directories
    # and stray corrigendum.xml files.
    amendment_xml_paths: list[Path] = []
    for year in sorted(os.listdir(corpus_dir)):
        year_dir = corpus_dir / year
        if not year_dir.is_dir():
            continue
        for fname in sorted(os.listdir(year_dir)):
            fpath = year_dir / fname
            if fname.endswith("-compensation-cess-rate.xml"):
                # Skip the two base notifications (1/2017, 2/2017) — they are
                # the schedules being amended, not amendING notifications.
                if fname in (
                    "1-2017-compensation-cess-rate.xml",
                    "2-2017-compensation-cess-rate.xml",
                ):
                    continue
                amendment_xml_paths.append(fpath)
            elif fpath.is_dir():
                amendment_xml_paths.extend(
                    sorted(fpath.rglob("corrigenda/*.xml"))
                )
            elif fname == "corrigendum.xml":
                amendment_xml_paths.append(fpath)

    for xml_path in amendment_xml_paths:
        try:
            events = compile_amendment_notification(xml_path)
        except Exception as e:
            print(f"  WARNING: error parsing {xml_path.name}: {e}")
            continue

        for evt in events:
            if evt.target_notification in target_notifications:
                all_events.append(evt)

    all_events.sort(key=lambda e: (e.effective_date, e.publication_date))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for evt in all_events:
            f.write(json.dumps(evt.to_dict(), ensure_ascii=False) + "\n")

    return all_events


# ── CLI entry ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "all":
        print("Compiling all rate amendment notifications...")
        events = compile_all_amendments()
        print(f"\nCompiled {len(events)} events")
        from collections import Counter
        ops = Counter(e.operation for e in events)
        for op, cnt in ops.most_common():
            print(f"  {op}: {cnt}")
        targets = Counter(e.target_notification for e in events)
        print(f"\nTarget distribution:")
        for t, cnt in targets.most_common():
            print(f"  {t}: {cnt}")
        print("\nCompiling all cess amendment notifications...")
        cess_events = compile_all_cess_amendments()
        print(f"\nCompiled {len(cess_events)} cess events")
        cess_ops = Counter(e.operation for e in cess_events)
        for op, cnt in cess_ops.most_common():
            print(f"  {op}: {cnt}")
        cess_targets = Counter(e.target_notification for e in cess_events)
        print(f"\nCess target distribution:")
        for t, cnt in cess_targets.most_common():
            print(f"  {t}: {cnt}")
    else:
        xml_path = sys.argv[1]
        events = compile_amendment_notification(xml_path)
        print(f"Notification: {xml_path}")
        print(f"Events: {len(events)}")
        for e in events:
            print(f"  {e.operation:30s} {e.target_notification:8s} sched={e.target_schedule:4s} "
                  f"clause={e.clause_ref:4s} {str(e.payload)[:80]}")
