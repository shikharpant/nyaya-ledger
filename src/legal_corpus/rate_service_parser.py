"""Parse CT(Rate) *service*-rate notifications into structured JSON.

Notification 11/2017-CT(Rate) notifies the CGST rates for the intra-State
supply of **services**. Its table has a fundamentally different layout from
the goods rate schedules:

    (1) Sl. No.
    (2) Chapter / Section / Heading   (a services-classification code, e.g.
        ``Heading 9954`` — *not* an HSN tariff item)
    (3) Description of Service
    (4) Rate (per cent.)              — a per-row / per-sub-item rate
    (5) Condition

Because services carry no tariff items and rate every row individually, the
goods :class:`_RowParser` state machine cannot be reused. This module performs
a dedicated row-level parse: each row is detected by a bare serial number
immediately followed by a ``Chapter``/``Section``/``Heading`` token, which is
unambiguous and recovers all 37 rows of the base notification.

The output mirrors :meth:`RateNotification.to_json` so it drops into the same
materializer / reconciliation pipeline as the goods base schedules.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


# ── regexes ──────────────────────────────────────────────────────────────────

_SVC_CLASSIF_RE = re.compile(r"^(Chapter|Section|Heading)\b", re.I)
_SVC_ROW_SNO_RE = re.compile(r"^\d{1,3}$")
# A genuine rate sub-item opens with a roman numeral *followed by descriptive
# text*, e.g. "(i) Construction of a complex...". Letter-prefixed tokens
# ("(a)", "(b)") belong to inline Explanations, and bare references such as
# "(iv)]" (from "[Please refer to Explanation no. (iv)]") or wrapped prose like
# "(ii) above." must NOT split a row — hence the required space + letter/paren
# after the label and the rejection of a trailing "]".
_SVC_SUBITEM_RE = re.compile(r"^\([ivx]+\)\s+[A-Za-z(]")
# A standalone rate value token (column 4): "9", "2.5", "Nil".
_SVC_RATE_TOK_RE = re.compile(r"^\d+(?:\.\d+)?$")
# Post-table prose paragraph (e.g. "2. In case of supply of service ...").
_SVC_POST_TABLE_RE = re.compile(r"^\d+\.\s")
# The multi-token rate phrase used for goods-transfer-like services.
_SVC_SAME_RATE_PHRASE = (
    "Same rate of central tax as on supply of like goods "
    "involving transfer of title in goods"
)
_HEADER_TOKENS = {
    "s.", "no.", "chapter", "heading", "sub-heading", "subheading",
    "tariff", "item", "description", "of", "goods", "service", "services",
    "rate", "condition", "(1)", "(2)", "(3)", "(4)", "(5)",
    "/", "–", "-", "sl", "or", "(per", "cent.)",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_paragraphs(root: ET.Element) -> list[tuple[str, list[str], str]]:
    """Return [(num, [p_text, ...], heading_text), ...] for each <paragraph>."""
    paras: list[tuple[str, list[str], str]] = []
    for para in root.iter():
        tag = para.tag.split("}")[-1] if "}" in para.tag else para.tag
        if tag != "paragraph":
            continue
        num_el = para.find("num")
        heading_el = para.find("heading")
        content_el = para.find("content")
        num = num_el.text.strip() if num_el is not None and num_el.text else ""
        heading = ""
        if heading_el is not None:
            heading = " ".join(t.strip() for t in heading_el.itertext()).strip()
        p_texts: list[str] = []
        if content_el is not None:
            for p in content_el:
                ptag = p.tag.split("}")[-1] if "}" in p.tag else p.tag
                if ptag != "p":
                    continue
                txt = " ".join(t.strip() for t in p.itertext()).strip()
                if txt:
                    p_texts.append(txt)
        paras.append((num, p_texts, heading))
    return paras


def _is_service_rate_token(t: str) -> bool:
    """A standalone column-(4) rate value: ``9``, ``2.5`` or ``Nil``."""
    return bool(_SVC_RATE_TOK_RE.match(t)) or t == "Nil"


def _extract_service_rate(
    tokens: list[str],
) -> tuple[list[str], str, list[str]]:
    """Split a sub-item token slice into ``(prose, rate, condition)``.

    The rate is the rightmost standalone rate token that is followed by a
    condition marker (``"-"``, ``"Provided ..."`` or a numbered condition) or
    by the end of the slice. The multi-token ``"Same rate ..."`` phrase is
    recognised as a single rate value.
    """
    n = len(tokens)
    if n == 0:
        return [], "", []
    # Multi-token "Same rate of central tax ..." phrase
    for i, t in enumerate(tokens):
        if t != "Same rate":
            continue
        words = _SVC_SAME_RATE_PHRASE.split()
        j = i
        wi = 0
        while j < n and wi < len(words) and tokens[j].lower() == words[wi].lower():
            j += 1
            wi += 1
        if wi >= len(words):
            return tokens[:i], _SVC_SAME_RATE_PHRASE, tokens[j:]
    # Numeric / Nil rate
    for i in range(n - 1, -1, -1):
        t = tokens[i]
        if not _is_service_rate_token(t):
            continue
        after = tokens[i + 1] if i + 1 < n else ""
        is_condition = (
            after == ""
            or after == "-"
            or after.lower().startswith("provided")
            or bool(_SVC_POST_TABLE_RE.match(after))
        )
        if is_condition:
            return tokens[:i], t, tokens[i + 1:]
    return tokens, "", []


def _split_service_body(
    body: list[str],
) -> tuple[str, str, list[dict]]:
    """Structure a row's body tokens into ``(description, rate, sub_items)``.

    *description* is always the full faithful prose (the canonical amendable
    text). *rate* is the top-level rate for single-rate rows (empty otherwise).
    *sub_items* is a best-effort structured breakdown; messy PDF-column rows
    that defeat clean extraction yield an empty list.
    """
    description = _clean(" ".join(body))
    label_positions = [i for i, t in enumerate(body) if _SVC_SUBITEM_RE.match(t)]
    if not label_positions:
        prose, rate, cond = _extract_service_rate(body)
        cond_str = _clean(" ".join(cond))
        sub_items: list[dict] = []
        if rate:
            sub_items.append(
                {"label": "", "text": "", "rate": rate, "condition": cond_str}
            )
        clean_desc = _clean(" ".join(prose))
        return (clean_desc or description, rate, sub_items)

    # Rows with explicit (i)/(ii)/... rate sub-items.
    bounds = label_positions + [len(body)]
    sub_items = []
    prefix = body[: label_positions[0]]
    for k in range(len(label_positions)):
        seg = body[bounds[k]: bounds[k + 1]]
        if not seg:
            continue
        lm = re.match(r"^(\([ivx]+\))\s*(.*)$", seg[0], re.I)
        label = lm.group(1) if lm else ""
        first = lm.group(2) if lm else seg[0]
        rest = ([first] if first else []) + list(seg[1:])
        prose, rate, cond = _extract_service_rate(rest)
        text = _clean(" ".join(prose))
        cond_str = _clean(" ".join(cond))
        sub_items.append(
            {"label": label, "text": text, "rate": rate, "condition": cond_str}
        )
    # Conservative clean-gate: only emit structured sub-items when EVERY
    # sub-item carries a real rate and substantive text. Otherwise the
    # PDF-column interleaving / wrapped-prose false positives would leak
    # unreliable rate data, so fall back to the faithful full description.
    if sub_items and all(
        si.get("rate") and len(si.get("text", "")) >= 15 for si in sub_items
    ):
        return description, "", sub_items
    return description, "", []


def _find_table_bounds(paras: list[tuple[str, list[str], str]]) -> tuple[int, int]:
    """Return the (start, end) paragraph indices that bound the service table."""
    start = -1
    for pi, (_, pts, _) in enumerate(paras):
        for t in pts:
            low = _clean(t).lower()
            if low == "(1)" and start < 0:
                start = pi
        if start >= 0:
            break
    if start < 0:
        start = 0
    # Table ends at the first paragraph that opens post-table prose, e.g.
    # "2. In case of supply of service ...".
    end = len(paras)
    for pi in range(start + 1, len(paras)):
        joined = _clean(" ".join(paras[pi][1]))
        if re.match(r"^\d+\.\s", joined) and ("case of supply" in joined.lower()
                or "value of supply" in joined.lower()
                or "this notification shall come" in joined.lower()):
            end = pi
            break
    return start, end


def parse_service_rate_notification(xml_path: str | Path) -> dict[str, Any]:
    """Parse a service-rate CT(Rate) notification XML into a JSON dict.

    The shape matches :meth:`RateNotification.to_json`:
    ``{notification_id, title, cbic_no, base_date, effective_from,
    instrument_type, schedules, opening_paragraph, explanations, ...}``.
    """
    xml_path = Path(xml_path)
    tree = ET.parse(xml_path)
    root = tree.getroot()

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

    paras = _extract_paragraphs(root)

    # Opening paragraph: the G.S.R. preamble that ends with "...of the said
    # Table:-". It usually sits (flattened) inside a single <paragraph>.
    opening_paragraph = ""
    for _, pts, _ in paras:
        joined = _clean(" ".join(pts))
        if "hereby notifies that the central tax" in joined.lower():
            cut = joined.lower().find("said table")
            if cut >= 0:
                opening_paragraph = _clean(joined[: cut + len("said Table:-")])
            else:
                opening_paragraph = joined
            break

    # Flatten only the table paragraphs (skip the preamble + post-table prose).
    t_start, t_end = _find_table_bounds(paras)
    table_paras = paras[t_start: t_end]
    toks = [t for _, pts, _ in table_paras for t in pts]

    # Strip a leading column-header preamble ("Condition (1) (2) ... (5)").
    cut = 0
    for k in range(min(len(toks), 20)):
        low = toks[k].lower().strip()
        if low in _HEADER_TOKENS or low in ("sl", "no.", "no", "condition"):
            cut = k + 1
            continue
        if re.match(r"^\(\d\)$", low):
            cut = k + 1
            continue
        break
    toks = toks[cut:]

    row_starts = [
        i
        for i, t in enumerate(toks)
        if _SVC_ROW_SNO_RE.match(t)
        and i + 1 < len(toks)
        and _SVC_CLASSIF_RE.match(toks[i + 1])
    ]

    entries: list[dict[str, Any]] = []
    for idx, s in enumerate(row_starts):
        sno = toks[s].rstrip(".") + "."
        tariff_item = _clean(toks[s + 1])
        body_start = s + 2
        if idx + 1 < len(row_starts):
            body_end = row_starts[idx + 1]
        else:
            body_end = len(toks)
            for k in range(body_start, len(toks)):
                if _SVC_POST_TABLE_RE.match(toks[k]):
                    body_end = k
                    break
        body = toks[body_start:body_end]
        description, rate, sub_items = _split_service_body(body)
        entries.append({
            "sno": sno,
            "tariff_item": tariff_item,
            "description": description,
            "is_omitted": False,
            "sub_items": sub_items,
            "attached_explanation": "",
            "rate": rate,
        })

    schedule = {
        "schedule_id": "I",
        "rate_pct": 0.0,
        "heading": "Schedule I – Service Rates",
        "entries": entries,
    }

    # Post-table explanations (paragraphs after the table).
    explanations: list[str] = []
    for pi in range(t_end, len(paras)):
        joined = _clean(" ".join(paras[pi][1]))
        if joined.lower().startswith("explanation") or "for the purposes of this notification" in joined.lower():
            explanations.append(joined)

    return {
        "notification_id": notification_id,
        "title": title,
        "cbic_no": cbic_no,
        "base_date": base_date,
        "effective_from": effective_from,
        "instrument_type": "services_rate",
        "schedules": {"I": schedule},
        "opening_paragraph": opening_paragraph,
        "explanations": explanations,
        "supersedes": "",
        "source_file": str(xml_path),
    }


def parse_and_save(
    xml_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Parse a service-rate notification and save as JSON."""
    data = parse_service_rate_notification(xml_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python rate_service_parser.py <xml_path> [output_json]")
        sys.exit(1)

    xml_arg = sys.argv[1]
    out_arg = sys.argv[2] if len(sys.argv) > 2 else None
    data = parse_service_rate_notification(xml_arg)
    n_entries = len(data["schedules"]["I"]["entries"])
    print(f"Notification: {data['cbic_no']}")
    print(f"Type: {data['instrument_type']}")
    print(f"Effective: {data['effective_from']}")
    print(f"Schedule I: {n_entries} entries")
    with_rates = sum(
        1 for e in data["schedules"]["I"]["entries"]
        if e["rate"] or e["sub_items"]
    )
    print(f"Entries with extracted rate/sub-items: {with_rates}")
    if out_arg:
        parse_and_save(xml_arg, out_arg)
        print(f"Saved to {out_arg}")
