"""Parse CT(Rate) notification XML into structured JSON rate schedules.

The parser converts flattened <p> elements in Akoma Ntoso XML into
structured Entry/Schedule/RateNotification objects.  The key challenge is
detecting row boundaries in the flattened token stream: S.No. tokens
(like "1.", "123A.") are the primary delimiter.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class ScheduleEntry:
    sno: str
    tariff_item: str = ""
    description: str = ""
    is_omitted: bool = False
    sub_items: list[dict] = field(default_factory=list)
    attached_explanation: str = ""
    rate: str = ""

    def key(self) -> str:
        return self.sno.rstrip(".").strip().upper()


@dataclass
class RateSchedule:
    schedule_id: str            # "I", "II", ...
    rate_pct: float             # CGST rate (e.g. 2.5)
    heading: str                # "Schedule I – 2.5%"
    entries: list[ScheduleEntry] = field(default_factory=list)


@dataclass
class RateNotification:
    notification_id: str
    title: str
    cbic_no: str
    base_date: str
    effective_from: str
    instrument_type: str        # "goods_rate" | "goods_exempt" | "concessional_rate"
    schedules: dict[str, RateSchedule] = field(default_factory=dict)
    opening_paragraph: str = ""
    explanations: list[str] = field(default_factory=list)
    supersedes: str = ""        # notification_id superseded by this one
    source_file: str = ""

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def entry_count(self) -> int:
        return sum(len(s.entries) for s in self.schedules.values())


# ── helpers ──────────────────────────────────────────────────────────────────

_SNO_RE = re.compile(r"^(\d{1,4}[A-Z]*)\.?$")
# A single tariff item: HSN chapter/heading/sub-heading/tariff item with
# optional space-separated sub-codes, e.g. "8703", "8703 40", "9619 00 30".
_TARIFF_ITEM_RE = re.compile(r"^\d{2,8}(?:\s\d{2}){0,3}$")
# Separators joining tariff items into a list: ", ", " or ", ", or ".
_TARIFF_LIST_SEP_RE = re.compile(r"\s*,\s*or\s+|\s+or\s+|\s*,\s*")
# A tariff code immediately followed by an exclusion bracket, e.g.
# "1404 [other than 1404 90 40," or "9401 [other than" (bracket may wrap
# across several flattened <p> lines until the closing "]").
_TARIFF_BRACKET_RE = re.compile(r"^\d{2,8}(?:\s\d{2}){0,3}\s+\[")
# An "Any [other] chapter" tariff reference (optionally preceded by a numeric
# chapter/heading list joined by "or"), e.g. "Any chapter", "Any other Chapter",
# "88 or Any other Chapter", "44 or any Chapter". Matches a leading prefix so a
# trailing description sharing the same flattened token is preserved separately.
_ANY_CHAPTER_TARIFF_RE = re.compile(
    r"^(?:\d+(?:\s+\d{2}){0,3}\s+or\s+)?"
    r"any(?:\s+other)?\s+chapter\b",
    re.I,
)
_SCHEDULE_HEADING_RE = re.compile(
    r"Schedule\s+([IVX]+)\s*[–\-—]?\s*([\d.]+)\s*%", re.I
)
# exemption schedules: "Schedule" alone or "Schedule I" without rate
_SCHEDULE_PLAIN_RE = re.compile(r"^Schedule\s*([IVX]+)?\s*$", re.I)
_RATE_FROM_HEADING_RE = re.compile(r"([\d.]+)\s*%")
_RATE_FROM_HEADING_RE = re.compile(r"([\d.]+)\s*%")
# tokens that are part of the column header block
_HEADER_TOKENS = {
    "s.", "no.", "chapter", "heading", "sub-heading", "subheading",
    "tariff", "item", "description", "of", "goods",
    "rate", "condition", "(1)", "(2)", "(3)", "(4)", "(5)",
    "/", "–", "-",
    "chapter/", "heading/", "sub-heading/", "subheading/", "tariffitem",
}
# sometimes the header bleeds across <p> splits
_HEADER_FRAGMENT_RE = re.compile(
    r"^(chapter|heading|sub[-–]?heading|tariff\s*item|description"
    r"|s\.|no\.|/\s*$|rate|condition|\(\d\))$",
    re.I,
)
OMITTED_TOKENS = {"omitted", "[omitted]", "omitted."}

# Compensation-Cess rate column: a token that begins a per-entry rate value
# (column 4), e.g. "60%", "21% or Rs. 4170", "Rs.4006", "NIL", "5% + Rs.2076".
# Once such a token is seen the remainder of the entry is captured as the rate.
_CESS_RATE_START_RE = re.compile(r"^\d+(?:\.\d+)?%|^Rs\.\s*\d|^NIL\b|^Nil\b")

# ── service-rate table (notification 11/2017-CT(Rate)) ───────────────────────
# Services are classified by "Chapter"/"Section"/"Heading" codes (e.g.
# "Heading 9954") rather than HSN tariff items, and each Sl.No carries its own
# rate (and optional sub-items (i)/(ii)/...). A row therefore begins with a
# bare serial number immediately followed by a classification token.
_SVC_CLASSIF_RE = re.compile(r"^(Chapter|Section|Heading)\b", re.I)
_SVC_ROW_SNO_RE = re.compile(r"^\d{1,3}$")
# A genuine rate sub-item opens with a roman numeral, e.g. "(i)", "(ii)".
# Letter-prefixed tokens ("(a)", "(b)") belong to inline Explanations and must
# NOT split a row.
_SVC_SUBITEM_RE = re.compile(r"^\([ivx]+\)")
# A standalone rate value token (column 4): "9", "2.5", "Nil".
_SVC_RATE_TOK_RE = re.compile(r"^\d+(?:\.\d+)?$")
# Post-table prose paragraph (e.g. "2. In case of supply of service ...").
_SVC_POST_TABLE_RE = re.compile(r"^\d+\.\s")
# The multi-token rate phrase used for goods-transfer-like services.
_SVC_SAME_RATE_PHRASE = (
    "Same rate of central tax as on supply of like goods "
    "involving transfer of title in goods"
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_sno_token(tok: str) -> bool:
    """Check whether *tok* looks like a serial-number token."""
    return bool(_SNO_RE.match(tok.strip()))


def _is_tariff_token(tok: str) -> bool:
    """Check whether *tok* looks like an HSN / tariff-item token.

    Accepts comma- and ``or``-joined lists such as ``"84 or 85"``,
    ``"8702 or 8703"``, ``"7310, 7323, 7612, or 7615"`` and
    space-separated sub-codes like ``"8703 40"``.  A leading/trailing
    standalone ``or`` (a line-wrap continuation marker) is tolerated.
    """
    t = tok.strip().rstrip(".").strip()
    if not t:
        return False
    # tolerate a leading/trailing standalone "or" (line-wrap artefact)
    t = re.sub(r"^or\s+", "", t, flags=re.I)
    t = re.sub(r"\s+or$", "", t, flags=re.I)
    parts = [p for p in _TARIFF_LIST_SEP_RE.split(t) if p]
    if not parts:
        return False
    return all(_TARIFF_ITEM_RE.match(p) for p in parts)


def _match_any_chapter_tariff(tokens: list[str], i: int) -> tuple[str, int, str]:
    """Match an ``Any [other] chapter`` tariff beginning at ``tokens[i]``.

    The expression may sit inside a single flattened ``<p>`` token (e.g.
    ``"Any chapter"``, ``"88 or Any other Chapter"``) or span a short run of
    tokens split across ``<p>`` boundaries (e.g. ``"90 or any other"`` +
    ``"Chapter"`` in notification 1/2017).  An optional numeric chapter list
    joined by ``or`` may precede it.

    Returns ``(tariff, consumed, remainder)`` where ``consumed`` is the number
    of tokens absorbed and ``remainder`` is any trailing description fragment
    that shared the final consumed token (empty in the common case where the
    match ends on a token boundary).  Returns ``("", 0, "")`` when the tokens
    at ``i`` do not begin with this pattern.
    """
    n = len(tokens)
    if i >= n:
        return "", 0, ""
    acc = ""
    for extra in range(min(4, n - i)):
        acc = (acc + " " + tokens[i + extra]).strip()
        m = _ANY_CHAPTER_TARIFF_RE.match(acc)
        if not m:
            continue
        return acc[:m.end()].strip(), extra + 1, acc[m.end():].strip()
    return "", 0, ""


def _extract_paragraphs(root: ET.Element) -> list[tuple[str, list[str], str]]:
    """Return [(num, [p_text, ...], heading_text), ...] for each <paragraph>."""
    ns = ""
    paras = []
    for para in root.iter():
        tag = para.tag.replace(ns, "")
        if tag != "paragraph":
            continue
        num_el = para.find(f"{ns}num") if ns else para.find("num")
        heading_el = para.find(f"{ns}heading") if ns else para.find("heading")
        content_el = para.find(f"{ns}content") if ns else para.find("content")
        num = num_el.text.strip() if num_el is not None and num_el.text else ""
        heading = ""
        if heading_el is not None:
            heading = " ".join(
                t.strip() for t in heading_el.itertext()
            ).strip()
        p_texts: list[str] = []
        if content_el is not None:
            for p in content_el:
                ptag = p.tag.replace(ns, "")
                if ptag != "p":
                    continue
                txt = " ".join(t.strip() for t in p.itertext()).strip()
                if txt:
                    p_texts.append(txt)
        paras.append((num, p_texts, heading))
    return paras


# ── schedule-table row parser ────────────────────────────────────────────────

class _RowParser:
    """State-machine that walks a flat token list and emits ScheduleEntry."""

    HEADER, EXPECT_SNO, EXPECT_TARIFF, DESCRIPTION = range(4)

    def __init__(self, has_rate_column: bool = False) -> None:
        self.entries: list[ScheduleEntry] = []
        self._state = self.HEADER
        self._cur_sno = ""
        self._cur_tariff = ""
        self._cur_desc: list[str] = []
        self._cur_explanation = ""
        self._saw_header_end = False
        # True while consuming the wrapped body of a bracketed tariff such as
        # "9401 [other than 9401 10 00" (tokens accumulated until "]").
        self._in_bracket_tariff = False
        # True once an inline "Explanation." has been seen for the current
        # entry; subsequent tokens are routed to ``_cur_explanation`` instead
        # of ``_cur_desc`` until the next serial number.
        self._in_explanation = False
        # Compensation-Cess (Rate) notifications carry a per-entry rate in an
        # extra column (4). When ``has_rate_column`` is set, trailing rate
        # tokens (e.g. "60%", "21% or Rs. 4170 per thousand", "NIL") are peeled
        # off the description into ``_cur_rate`` so the goods text stays clean.
        self._has_rate_column = has_rate_column
        self._in_rate = False
        self._cur_rate: list[str] = []

    # -- public --
    def feed_tokens(self, tokens: list[str]) -> None:
        # Pre-split tokens that combine S.No. + tariff ("20. 2619" → "20." "2619")
        expanded: list[str] = []
        for raw in tokens:
            t = raw.strip()
            if not t:
                continue
            m = re.match(r"^(\d{1,4}[A-Z]*\.)\s+(\S.*)$", t)
            if m:
                expanded.append(m.group(1))
                expanded.append(m.group(2))
            else:
                expanded.append(t)
        # Strip ALL repeated column-header preambles anywhere in the token
        # list (page breaks re-emit "S. No. Chapter / Heading / ... (1) (2)").
        # The preamble may be preceded by stray page-number digits.
        cleaned: list[str] = []
        idx = 0
        while idx < len(expanded):
            tok = expanded[idx]
            # Detect page-break header: bare number(s) followed by "S." "No."
            if re.match(r"^\d{1,3}$", tok):
                found_hdr = False
                for k in range(idx + 1, min(idx + 5, len(expanded))):
                    if expanded[k].lower() in ("s.", "s") and \
                            k + 1 < len(expanded) and \
                            expanded[k + 1].lower() in ("no.", "no"):
                        # Found header — find end (column markers like "(4)")
                        end = k + 1
                        for j in range(k, min(k + 20, len(expanded))):
                            if re.match(r"^\(\d+\)$", expanded[j]):
                                end = j
                        idx = end + 1
                        found_hdr = True
                        break
                if found_hdr:
                    if self._in_rate:
                        self._in_rate = False
                    continue
            cleaned.append(tok)
            idx += 1
        expanded = cleaned
        i = 0
        while i < len(expanded):
            tok = expanded[i].strip()
            if not tok:
                i += 1
                continue
            i = self._step(expanded, i)

    # -- internal --
    def _step(self, tokens: list[str], i: int) -> int:
        tok = tokens[i].strip()
        if self._state == self.HEADER:
            return self._step_header(tok, i)
        elif self._state == self.EXPECT_SNO:
            return self._step_expect_sno(tok, i)
        elif self._state == self.EXPECT_TARIFF:
            return self._step_expect_tariff(tok, tokens, i)
        else:
            return self._step_description(tok, tokens, i)

    def _step_header(self, tok: str, i: int) -> int:
        low = tok.lower().strip()
        if low in _HEADER_TOKENS or _HEADER_FRAGMENT_RE.match(low):
            return i + 1
        if tok == "(3)" or tok == "3)" or _is_sno_token(tok):
            self._state = self.EXPECT_SNO
            return i  # re-process in new state
        return i + 1

    def _step_expect_sno(self, tok: str, i: int) -> int:
        if _is_sno_token(tok):
            self._cur_sno = tok
            self._state = self.EXPECT_TARIFF
            return i + 1
        # skip stray tokens (page-break artefacts etc.)
        if tok.lower() in _HEADER_TOKENS or _HEADER_FRAGMENT_RE.match(tok.lower()):
            return i + 1
        # Could be "Omitted" as the entry text directly
        if tok.lower() in OMITTED_TOKENS:
            self._flush_entry(omitted=True)
            return i + 1
        return i + 1

    def _step_expect_tariff(self, tok: str, tokens: list[str], i: int) -> int:
        if tok.lower() in OMITTED_TOKENS:
            self._flush_entry(omitted=True)
            return i + 1
        # Continue accumulating a bracketed tariff whose "[" has not yet been
        # closed, e.g. "9603 [other than 9603 10 00" + "and 9603 21 00]".
        # These continuation tokens may contain letters ("and", "or") so they
        # must be consumed here regardless of the usual letter heuristic.
        if self._in_bracket_tariff:
            # Safety net: never let an unbalanced bracket swallow a serial
            # number (and the entries that follow it).
            if _is_sno_token(tok) \
                    and self._cur_tariff.count("[") > self._cur_tariff.count("]"):
                self._flush_entry()
                self._cur_sno = tok
                self._state = self.EXPECT_TARIFF
                return i + 1
            self._cur_tariff = (self._cur_tariff + " " + tok).strip()
            # The bracket may close mid-token (e.g. "2515 12 90] or 6802"),
            # so track balance rather than relying on a trailing "]".
            if self._cur_tariff.count("]") >= self._cur_tariff.count("["):
                self._in_bracket_tariff = False
                # Any post-"]" suffix already appended is part of the tariff
                # (e.g. "] or 6802"); leave state as EXPECT_TARIFF so the next
                # token is classified by the normal logic below.
            return i + 1
        # Recognise "Any [other] chapter" (optionally preceded by a numeric
        # chapter list joined by "or") as the tariff value rather than letting
        # it leak into the description — e.g. 9/2025 SII 638, SIII 13, SI 462.
        # The expression may share its final token with the start of the
        # description, in which case the trailing text is kept as description.
        ac_tariff, ac_consumed, ac_remainder = _match_any_chapter_tariff(tokens, i)
        if ac_tariff:
            self._cur_tariff = (
                (self._cur_tariff + " " + ac_tariff).strip()
                if self._cur_tariff else ac_tariff
            )
            if ac_remainder:
                self._cur_desc.append(ac_remainder)
            self._state = self.DESCRIPTION
            return i + ac_consumed
        # A tariff code immediately followed by an exclusion bracket, e.g.
        # "1404 [other than 1404 90 40," or "9401 [other than".  Only engage
        # bracket-accumulation when the bracket actually contains tariff codes
        # — otherwise the token is descriptive prose that merely starts with a
        # bracket (e.g. 1/2017 SI 138 "26 [other than" + "All ores ...").
        if _TARIFF_BRACKET_RE.match(tok):
            after_bracket = tok[tok.index("[") + 1:]
            next_tok = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
            bracket_has_code = bool(re.search(r"\d", after_bracket)) or (
                bool(next_tok) and (
                    _is_tariff_token(next_tok)
                    or re.match(r"^\d", next_tok)
                    or next_tok.endswith("]")
                )
            )
            if bracket_has_code:
                self._cur_tariff = tok
                if tok.count("]") >= tok.count("["):
                    self._state = self.DESCRIPTION
                else:
                    self._in_bracket_tariff = True
                return i + 1
            # not a real tariff bracket → fall through to description handling
        # Pure tariff token (possibly an "or"/comma list such as "84 or 85"
        # or "8703 40, 8703 60").
        if _is_tariff_token(tok):
            self._cur_tariff = (self._cur_tariff + " " + tok).strip() \
                if self._cur_tariff else tok
            if i + 1 < len(tokens):
                next_tok = tokens[i + 1].strip()
                if (
                    not next_tok
                    or next_tok.lower() in OMITTED_TOKENS
                    or (
                        re.search(r"[A-Za-z]", next_tok)
                        and next_tok.lower() != "or"
                        and not re.match(r"^or\d", next_tok, re.I)
                    )
                ):
                    self._state = self.DESCRIPTION
            return i + 1
        # Standalone "or" / "orNNN" continuation of an accumulated tariff.
        if tok.lower() == "or" or re.match(r"^or\d", tok, re.I):
            self._cur_tariff = (self._cur_tariff + " " + tok).strip() \
                if self._cur_tariff else tok
            if i + 1 < len(tokens):
                next_tok = tokens[i + 1].strip()
                if re.search(r"[A-Za-z]", next_tok) \
                        and not re.match(r"^or\d", next_tok, re.I):
                    self._state = self.DESCRIPTION
            return i + 1
        # Anything else (has letters, is descriptive prose) → description.
        self._cur_desc.append(tok)
        self._state = self.DESCRIPTION
        return i + 1

    def _step_description(self, tok: str, tokens: list[str], i: int) -> int:
        sno_strict = tok.rstrip().endswith(".") and bool(
            re.match(r"^\d{1,4}[A-Z]*\.$", tok.strip())
        )
        if sno_strict and i + 1 < len(tokens):
            next_tok = tokens[i + 1].strip()
            if (_is_tariff_token(next_tok)
                or next_tok.lower() in OMITTED_TOKENS
                or re.match(r"^\d", next_tok)
                or re.match(r"^Any\b", next_tok, re.I)
                or next_tok == "-"):
                self._flush_entry()
                self._cur_sno = tok
                self._state = self.EXPECT_TARIFF
                return i + 1
        # S.No. at end of token list (paragraph boundary): the tariff for this
        # entry arrives in the next feed_tokens call.  Flush the current entry
        # and transition to EXPECT_TARIFF so the parser recovers across the
        # paragraph split instead of swallowing every subsequent row.
        if sno_strict and i + 1 >= len(tokens) and self._cur_sno:
            self._flush_entry()
            self._cur_sno = tok
            self._state = self.EXPECT_TARIFF
            return i + 1
        # Check if this token is "Omitted" as a standalone entry (no tariff)
        if tok.lower() in OMITTED_TOKENS and not self._cur_desc:
            self._flush_entry(omitted=True)
            return i + 1
        # Detect embedded "Explanation:" blocks. Once seen, the wrapped body
        # (which may contain "(i) ... (ii) ..." sub-clauses and even further
        # serial numbers' explanations) is routed to ``_cur_explanation``
        # rather than polluting the goods description. This check runs BEFORE
        # the rate-column capture so an explanation that follows a rate token
        # is not swallowed into the rate value.
        if tok.lower().startswith("explanation"):
            self._cur_explanation = tok
            self._in_explanation = True
            return i + 1
        if self._in_explanation:
            self._cur_explanation = (self._cur_explanation + " " + tok).strip()
            return i + 1
        # Rate-column capture (Compensation Cess): once a rate-start token is
        # seen, every subsequent token until the next S.No. belongs to the
        # per-entry rate value rather than the goods description.
        if self._has_rate_column:
            if not self._in_rate and _CESS_RATE_START_RE.match(tok):
                self._in_rate = True
            if self._in_rate:
                # Detect the notification-level closing matter that follows the
                # last schedule entry — the Explanation sub-items
                # "(1) In this Schedule ...", the filing number "[F.No. ...]",
                # the signer "Under Secretary ...", or a stray bare page-number
                # digit. Without this, rate-column capture swallows the entire
                # trailing Explanation into the last entry's rate value (e.g.
                # S.No 56 of the Compensation Cess 1/2017 base notification,
                # whose rate became "Nil <page> (1) In this Schedule ...").
                next_tok = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
                low = tok.lower()
                rate_done = bool(self._cur_rate) and (
                    self._cur_rate[-1].lower() == "nil"
                    or self._cur_rate[-1].endswith("%")
                )
                if (
                    (tok == "(1)" and next_tok.lower() == "in")
                    or low.startswith("[f.no")
                    or low.startswith("under secre")
                    or (rate_done and re.match(r"^\d{1,3}$", tok))
                ):
                    self._flush_entry()
                    self._state = self.EXPECT_SNO
                    return i  # re-process token in EXPECT_SNO (skipped there)
                self._cur_rate.append(tok)
                return i + 1
        # Accumulate description
        self._cur_desc.append(tok)
        return i + 1

    def _flush_entry(self, omitted: bool = False) -> None:
        if not self._cur_sno:
            self._state = self.EXPECT_SNO
            return
        entry = ScheduleEntry(
            sno=self._cur_sno,
            tariff_item=self._cur_tariff,
            description=_clean(" ".join(self._cur_desc)),
            is_omitted=omitted,
            attached_explanation=_clean(self._cur_explanation),
            rate=_clean(" ".join(self._cur_rate)),
        )
        self.entries.append(entry)
        self._cur_sno = ""
        self._cur_tariff = ""
        self._cur_desc = []
        self._cur_explanation = ""
        self._cur_rate = []
        self._in_bracket_tariff = False
        self._in_explanation = False
        self._in_rate = False
        self._state = self.EXPECT_SNO


def _parse_schedule_tokens(
    tokens: list[str], has_rate_column: bool = False
) -> list[ScheduleEntry]:
    """Parse a flat list of <p> tokens into entries."""
    rp = _RowParser(has_rate_column=has_rate_column)
    rp.feed_tokens(tokens)
    rp._flush_entry()
    return rp.entries


def _parse_services_cess_table(
    paras: list[tuple[str, list[str], str]],
) -> "RateSchedule":
    """Parse the services-cess Table (2/2017-cc-rate) into a single schedule.

    The table has 4 columns — (1) Sl.No, (2) Description of Services,
    (3) Chapter/Section/Heading/Group, (4) Rate — flattened into ``<p>``
    tokens.  Sl.No values are bare numbers; the "tariff" is a Chapter
    reference (e.g. "Chapter 99"); the rate is free text (e.g.
    "Same rate of cess as applicable on supply of similar goods" or "Nil").
    """
    # Collect the flat token stream, skipping the column-header block that
    # runs from "Table" through the "(1) (2) (3) (4)" markers.
    all_tokens: list[str] = []
    for _, pts, _ in paras:
        all_tokens.extend(pts)
    # Find the start of the data rows: first bare S.No "1" after the "(4)"
    # column-number marker.
    start = 0
    for i, t in enumerate(all_tokens):
        if _clean(t) == "(4)":
            start = i + 1
            break
    body = all_tokens[start:]

    entries: list[ScheduleEntry] = []
    cur_sno = ""
    cur_desc: list[str] = []
    cur_tariff = ""
    cur_rate: list[str] = []
    # Once a "Chapter NN" tariff is seen, subsequent tokens until "Nil"/a rate
    # or the next S.No belong to the rate column.
    seen_chapter = False
    done = False
    for tok in body:
        t = _clean(tok)
        if not t:
            continue
        if done:
            break
        # Stop at a numbered paragraph marker ("2.", "3.") that follows the
        # table — these are the Explanation / come-into-force paragraphs.
        if re.match(r"^\d{1,2}\.$", t) and cur_sno and seen_chapter:
            done = True
            continue
        # Stop at an "Explanation." paragraph.
        if t.lower().startswith("explanation"):
            done = True
            continue
        # A bare 1-2 digit number begins a new service row (Sl.No).
        if re.match(r"^\d{1,2}$", t) and (not cur_sno or t != cur_sno.rstrip(".")):
            if cur_sno:
                entries.append(ScheduleEntry(
                    sno=cur_sno,
                    tariff_item=cur_tariff,
                    description=_clean(" ".join(cur_desc)),
                    rate=_clean(" ".join(cur_rate)),
                ))
            cur_sno = t + "."
            cur_desc = []
            cur_tariff = ""
            cur_rate = []
            seen_chapter = False
            continue
        if not cur_sno:
            continue
        # "Chapter NN" is the column-(3) tariff reference.
        if re.match(r"^Chapter\s+\d{2}$", t, re.I):
            cur_tariff = t
            seen_chapter = True
            continue
        if seen_chapter:
            cur_rate.append(t)
        else:
            cur_desc.append(t)
    if cur_sno:
        entries.append(ScheduleEntry(
            sno=cur_sno,
            tariff_item=cur_tariff,
            description=_clean(" ".join(cur_desc)),
            rate=_clean(" ".join(cur_rate)),
        ))
    return RateSchedule(
        schedule_id="I",
        rate_pct=0.0,
        heading="Table",
        entries=entries,
    )


# ── main parser ──────────────────────────────────────────────────────────────

def parse_rate_notification(
    xml_path: str | Path,
    instrument_type: str = "",
) -> RateNotification:
    """Parse a CT(Rate) notification XML file into a RateNotification."""

    xml_path = Path(xml_path)
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # extract metadata
    props: dict[str, str] = {}
    for prop in root.iter():
        tag = prop.tag.split("}")[-1] if "}" in prop.tag else prop.tag
        if tag == "property":
            props[prop.get("name", "")] = prop.get("value", "")

    notification_id = props.get("canonical_id", str(xml_path))
    title = props.get("title", "")
    cbic_no = props.get("cbic_no", "")
    base_date = props.get("publication_date", "")
    effective_from = props.get("effective_from", base_date)

    # determine instrument type if not provided
    if not instrument_type:
        doc_name = ""
        for doc in root:
            if "doc" in doc.tag:
                doc_name = doc.get("name", "").lower()
                break
        if "supersede" in doc_name:
            instrument_type = "goods_rate" if "1_2017" in doc_name or "9_2025" in doc_name else "goods_exempt"
        elif "exempt" in doc_name:
            instrument_type = "goods_exempt"
        elif "concessional" in doc_name:
            instrument_type = "concessional_rate"
        else:
            instrument_type = "goods_rate"

    rn = RateNotification(
        notification_id=notification_id,
        title=title,
        cbic_no=cbic_no,
        base_date=base_date,
        effective_from=effective_from,
        instrument_type=instrument_type,
        source_file=str(xml_path),
    )

    # extract all paragraphs
    paras = _extract_paragraphs(root)

    # Detect Compensation Cess (Rate) notifications. Goods cess (1/2017)
    # carries a per-entry rate in column (4) of its Schedule, so the row parser
    # must peel trailing rate tokens off the description. Services cess
    # (2/2017) uses a different services-Table layout handled separately.
    _is_cess = (
        "compensation-cess-rate" in str(xml_path)
        or props.get("cbic_category", "") == "Compensation Cess (Rate)"
        or "compensation cess" in title.lower()
    )
    _is_services_cess = _is_cess and any(
        "description of services" in _clean(t).lower()
        for _, pts, _ in paras for t in pts
    )
    _has_rate_column = _is_cess and not _is_services_cess

    # Services cess (2/2017-cc-rate): a small Table of services with columns
    # Sl.No / Description of Services / Chapter / Rate. Only 3 entries, never
    # amended via S.No language — parse directly into a single schedule.
    if _is_services_cess:
        rn.schedules["I"] = _parse_services_cess_table(paras)
        rn.instrument_type = "services_cess_rate"
        return rn

    # Concessional / special-rate notifications (e.g. 2/2022) lay their goods
    # out in a "Table" that carries an explicit per-row "Rate" column (and a
    # Condition column) instead of a rate-bearing "Schedule" heading. The row
    # parser above assumes the standard 3-column (S.No./tariff/description)
    # layout, so delegate these to the concessional table parser and adapt the
    # result into the base-schedule shape. Only engage when the table genuinely
    # uses a Rate column; standalone rate notifications (e.g. 14/2025) keep the
    # "Schedule" heading and are handled by the regular path below.
    _all_para_tokens = [pt for _, pts, _ in paras for pt in pts]
    _has_table_rate_col = (
        any(_clean(t).lower() == "table" for t in _all_para_tokens)
        and any(_clean(t).lower() == "rate" for t in _all_para_tokens)
        and not any(
            _SCHEDULE_HEADING_RE.search(_clean(t))
            for _, pts, hd in paras
            for t in [hd, *pts]
        )
    )
    if _has_table_rate_col:
        from legal_corpus.rate_concessional_parser import parse_concessional_notification
        conc = parse_concessional_notification(xml_path)
        rate = float(conc.get("rate_pct", 0.0) or 0.0)
        entries = [
            ScheduleEntry(
                sno=str(e.get("sno", "")).rstrip(".").strip(),
                tariff_item=str(e.get("tariff_item", "")),
                description=str(e.get("description", "")),
            )
            for e in conc.get("schedules", {}).get("I", {}).get("entries", [])
        ]
        rn.schedules["I"] = RateSchedule(
            schedule_id="I",
            rate_pct=rate,
            heading="Schedule I",
            entries=entries,
        )
        return rn

    # find schedule sections and their tokens
    current_schedule: Optional[RateSchedule] = None
    rp: Optional[_RowParser] = None

    for num, p_texts, heading in paras:
        heading_clean = _clean(heading)

        # Some notifications (e.g. 14/2025) bundle the schedule table into the
        # SAME paragraph as the opening "hereby notifies..." prose. Detect the
        # schedule start within the paragraph and split there, so the table rows
        # are parsed as a schedule while the prose is captured as the opening
        # paragraph. This must run BEFORE the schedule-heading checks below so
        # the truncated tokens (starting at "Schedule"/"S. No.") are recognised.
        joined_for_open = " ".join(p_texts)
        if (
            "hereby notifies the rate" in joined_for_open.lower()
            or "hereby exempts" in joined_for_open.lower()
        ):
            sched_start_idx = -1
            for i, pt in enumerate(p_texts):
                low = _clean(pt).lower()
                if low == "schedule" or low in ("s. no.", "s.no.", "s no", "s.no"):
                    sched_start_idx = i
                    break
            if sched_start_idx >= 0:
                rn.opening_paragraph = _clean(" ".join(p_texts[:sched_start_idx]))
                p_texts = list(p_texts[sched_start_idx:])

        # check if this paragraph is a schedule heading (rate-bearing)
        m = _SCHEDULE_HEADING_RE.search(heading_clean)
        heading_token_idx = -1  # position of heading within p_texts (-1 = heading attr)
        if not m:
            # also check inside p_texts for schedule heading
            for idx, pt in enumerate(p_texts[:3]):
                pt_clean = _clean(pt)
                m = _SCHEDULE_HEADING_RE.search(pt_clean)
                if m:
                    heading_clean = pt_clean
                    heading_token_idx = idx
                    break

        # also check for plain schedule heading (exemption: just "Schedule")
        m_plain = _SCHEDULE_PLAIN_RE.match(heading_clean)
        if not m_plain:
            for idx, pt in enumerate(p_texts[:1]):
                m_plain = _SCHEDULE_PLAIN_RE.match(_clean(pt))
                if m_plain:
                    heading_clean = _clean(pt)
                    heading_token_idx = idx
                    break

        # tokens following an inline schedule heading (same paragraph) that must
        # be fed to the row parser once the schedule is created below.
        tail_tokens: list[str] = []
        if heading_token_idx >= 0:
            tail_tokens = p_texts[heading_token_idx + 1:]

        if m:
            # flush previous schedule
            if rp and current_schedule:
                rp._flush_entry()
                current_schedule.entries = rp.entries
                rn.schedules[current_schedule.schedule_id] = current_schedule

            sched_id = m.group(1)
            rate_str = m.group(2)
            rate = float(rate_str)
            current_schedule = RateSchedule(
                schedule_id=sched_id,
                rate_pct=rate,
                heading=heading_clean,
            )
            rp = _RowParser(has_rate_column=_has_rate_column)
            if tail_tokens:
                rp.feed_tokens(tail_tokens)
            continue
        elif m_plain:
            # plain schedule (exemption — rate 0)
            if rp and current_schedule:
                rp._flush_entry()
                current_schedule.entries = rp.entries
                rn.schedules[current_schedule.schedule_id] = current_schedule

            sched_id = m_plain.group(1) or "I"
            # Default rate is 0 (exemption). But when a rate is declared in the
            # opening prose (e.g. 14/2025: "rate of the central tax of 6 per
            # cent"), capture it so the standalone rate schedule carries the
            # correct rate_pct.
            plain_rate = 0.0
            if rn.opening_paragraph:
                rm = re.search(
                    r"(\d+(?:\.\d+)?)\s*per\s*cent", rn.opening_paragraph, re.I
                )
                if rm:
                    plain_rate = float(rm.group(1))
            current_schedule = RateSchedule(
                schedule_id=sched_id,
                rate_pct=plain_rate,
                heading=heading_clean,
            )
            rp = _RowParser(has_rate_column=_has_rate_column)
            if tail_tokens:
                rp.feed_tokens(tail_tokens)
            continue

        # check if this is the opening paragraph (contains "hereby notifies" or "hereby exempts")
        joined = " ".join(p_texts)
        is_opening = (
            "hereby notifies the rate" in joined.lower()
            or "hereby exempts" in joined.lower()
        )
        if is_opening:
            rn.opening_paragraph = _clean(joined)
            continue

        # detect implicit schedule start: column header pattern without explicit heading
        # (for exemption notifications like 10/2025 that don't have "Schedule" heading)
        if not current_schedule and p_texts:
            first_low = _clean(p_texts[0]).lower()
            if first_low in ("s. no.", "s.no.", "s no", "s.no"):
                current_schedule = RateSchedule(
                    schedule_id="I",
                    rate_pct=0.0,
                    heading="Schedule (implicit)",
                )
                rp = _RowParser(has_rate_column=_has_rate_column)
                # feed THIS paragraph's tokens (header + first entries)
                rp.feed_tokens(p_texts)
                continue

        # check for closing explanations
        # A *notification-level* explanation carries its own <heading> (e.g.
        # "Explanation.- For the purposes of this notification,-") and must be
        # captured apart from any schedule.  An *entry-level* explanation has
        # no heading and appears inline inside a schedule paragraph, often
        # sharing the paragraph with later serial numbers (e.g. the motor
        # vehicle block of 9/2025).  Such paragraphs must be fed to the row
        # parser so their entries are not lost.
        heading_is_explanation = heading_clean.lower().startswith("explanation")
        p_starts_explanation = bool(p_texts) and p_texts[0].lower().startswith("explanation")
        if heading_is_explanation or (p_starts_explanation and not (rp and current_schedule)):
            rn.explanations.append(_clean(joined))
            continue

        # check for "come into force" paragraph
        if "come into force" in joined.lower():
            continue

        # if we're inside a schedule, feed tokens to the row parser
        if rp and current_schedule:
            rp.feed_tokens(p_texts)
        else:
            # could be preamble metadata
            pass

    # flush last schedule
    if rp and current_schedule:
        rp._flush_entry()
        current_schedule.entries = rp.entries
        rn.schedules[current_schedule.schedule_id] = current_schedule

    return rn


def parse_and_save(
    xml_path: str | Path,
    output_path: str | Path,
    instrument_type: str = "",
) -> RateNotification:
    """Parse a notification and save as JSON."""
    rn = parse_rate_notification(xml_path, instrument_type)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(rn.to_json(), f, indent=2, ensure_ascii=False)
    return rn


# ── CLI entry ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python rate_schedule_parser.py <xml_path> [output_json]")
        sys.exit(1)

    xml_path = sys.argv[1]
    if len(sys.argv) > 2:
        out = sys.argv[2]
    else:
        out = None

    rn = parse_rate_notification(xml_path)
    print(f"Notification: {rn.cbic_no}")
    print(f"Type: {rn.instrument_type}")
    print(f"Effective: {rn.effective_from}")
    print(f"Schedules: {len(rn.schedules)}")
    for sid, sched in sorted(rn.schedules.items()):
        print(f"  Schedule {sid} ({sched.rate_pct}%): {len(sched.entries)} entries")
    print(f"Total entries: {rn.entry_count()}")

    if out:
        parse_and_save(xml_path, out)
        print(f"Saved to {out}")
