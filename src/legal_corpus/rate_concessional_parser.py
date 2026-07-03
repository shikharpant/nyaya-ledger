"""Parse concessional/special rate CT(Rate) notifications.

These are standalone rate notifications with simpler table structures
than the main 1/2017 rate schedule. Examples: 2/2022 (bricks 3%),
14/2025 (bricks 6%), 3/2017 (exploration 2.5%).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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


def _all_p_texts(root: ET.Element) -> list[str]:
    texts: list[str] = []
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
                        texts.append(txt)
    return texts


HEADER_TOKENS = {
    "sl", "no", "tariff", "item", "sub-heading", "subheading",
    "heading", "chapter", "description", "rate", "condition",
    "(1)", "(2)", "(3)", "(4)", "(5)",
}

SKIP_TOKENS = {
    "table", "explanation", "annexure", "condition", "schedule",
    "s.no", "sl.", "sl",
}


def _extract_rate_from_text(text: str) -> Optional[float]:
    m = re.search(r"rate\s+of\s+the\s+central\s+tax\s+of\s+([\d.]+)\s*per\s*cent", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"at\s+the\s+rate\s+.*?([\d.]+)\s*%", text, re.I | re.DOTALL)
    if m:
        return float(m.group(1))
    m = re.search(r"central\s+tax\s+leviable.*?([\d.]+)\s*%", text, re.I | re.DOTALL)
    if m:
        return float(m.group(1))
    return None


def _detect_num_columns(p_texts: list[str], table_start: int) -> int:
    """Count table columns from header markers like (1) (2) (3) (4) (5)."""
    for i in range(table_start, min(table_start + 20, len(p_texts))):
        line = _clean(p_texts[i])
        if re.match(r"^\(\d+\)$", line):
            count = 1
            for j in range(i + 1, min(i + 10, len(p_texts))):
                if re.match(r"^\(\d+\)$", _clean(p_texts[j])):
                    count += 1
                else:
                    break
            if count >= 2:
                return count
    return 3


def _parse_table_entries(p_texts: list[str], has_rate_col: bool = False) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    in_table = False
    table_start_idx = 0
    num_cols = 3

    for idx, line in enumerate(p_texts):
        line_low = _clean(line).lower()
        if line_low in ("table", "schedule") and not in_table:
            in_table = True
            table_start_idx = idx
            num_cols = _detect_num_columns(p_texts, idx)
            continue
        if re.match(r"^schedule\s*$", line_low) and not in_table:
            in_table = True
            table_start_idx = idx
            num_cols = _detect_num_columns(p_texts, idx)
            continue

    if not in_table:
        return entries

    collecting = False
    current_sno = ""
    row_cells: list[str] = []

    for line in p_texts[table_start_idx + 1:]:
        line_clean = _clean(line)
        if not line_clean:
            continue
        line_low = line_clean.lower()

        if re.match(r"^\(\d+\)$", line_low):
            collecting = True
            continue
        if not collecting:
            continue

        if line_low in ("sl.", "sl", "no.", "no", "tariff", "item", "sub-heading",
                         "subheading", "heading", "chapter", "description", "rate",
                         "condition", "s.no.", "s.no"):
            continue
        if re.match(r"^sl\.?\s*$", line_low) or re.match(r"^no\.?\s*$", line_low):
            continue

        if line_low.startswith("explanation") or line_low.startswith("annexure"):
            break

        sno_match = re.match(r"^(\d{1,3})\.$", line_clean)
        if sno_match and collecting:
            if current_sno and row_cells:
                entry = _build_entry(current_sno, row_cells, num_cols)
                if entry:
                    entries.append(entry)
            current_sno = sno_match.group(1)
            row_cells = []
        else:
            row_cells.append(line_clean)

    if current_sno and row_cells:
        entry = _build_entry(current_sno, row_cells, num_cols)
        if entry:
            entries.append(entry)

    return entries


def _build_entry(sno: str, cells: list[str], num_cols: int) -> dict[str, str] | None:
    """Build an entry dict from collected cell texts."""
    if not cells:
        return None

    if num_cols >= 5:
        tariff = cells[0] if len(cells) > 0 else ""
        rate = ""
        condition = ""
        desc_parts = []

        for cell in cells[1:]:
            if re.match(r"^[\d.]+\s*%$", cell) and not rate:
                rate = cell.replace("%", "").strip()
            elif re.match(r"^\d+$", cell) and not condition and rate:
                condition = cell
            else:
                desc_parts.append(cell)

        entry = {"sno": sno, "tariff_item": tariff, "description": _clean(" ".join(desc_parts))}
        if rate:
            entry["rate_pct"] = rate
        return entry

    elif num_cols == 4:
        tariff = cells[0] if cells else ""
        rate = ""
        desc_parts = []
        for cell in cells[1:]:
            if re.match(r"^[\d.]+\s*%$", cell) and not rate:
                rate = cell.replace("%", "").strip()
            else:
                desc_parts.append(cell)
        entry = {"sno": sno, "tariff_item": tariff, "description": _clean(" ".join(desc_parts))}
        if rate:
            entry["rate_pct"] = rate
        return entry

    elif num_cols == 3:
        tariff = cells[0] if cells else ""
        description = _clean(" ".join(cells[1:]))
        return {"sno": sno, "tariff_item": tariff, "description": description}

    else:
        return {"sno": sno, "tariff_item": "", "description": _clean(" ".join(cells))}


def _parse_annexure_conditions(p_texts: list[str]) -> list[dict[str, str]]:
    conditions: list[dict[str, str]] = []
    in_annexure = False
    current_no = ""
    current_text = ""

    for line in p_texts:
        line_clean = _clean(line)
        if not line_clean:
            continue

        if line_clean.lower().startswith("annexure"):
            in_annexure = True
            continue

        if not in_annexure:
            continue

        if line_clean.lower() in ("condition no.", "condition"):
            continue

        sno_match = re.match(r"^(\d+)\.?\s*(.*)", line_clean)
        if sno_match:
            if current_no:
                conditions.append({"condition_no": current_no, "text": _clean(current_text)})
            current_no = sno_match.group(1)
            current_text = sno_match.group(2)
        else:
            if current_no:
                current_text += " " + line_clean

    if current_no:
        conditions.append({"condition_no": current_no, "text": _clean(current_text)})

    return conditions


def parse_concessional_notification(xml_path: str | Path) -> dict[str, Any]:
    xml_path = Path(xml_path)
    props = _get_props(xml_path)
    root = props["_root"]

    notification_id = props.get("canonical_id", str(xml_path))
    pub_date = props.get("publication_date", "")
    eff_date = props.get("effective_from", pub_date)

    p_texts = _all_p_texts(root)
    full_text = " ".join(p_texts)

    rate = _extract_rate_from_text(full_text)
    has_rate_col = rate is None
    if has_rate_col:
        m = re.search(r"([\d.]+)\s*%", " ".join(p_texts))
        rate = float(m.group(1)) if m else 0.0

    entries = _parse_table_entries(p_texts, has_rate_col=has_rate_col)

    if not has_rate_col and rate is None:
        for e in entries:
            if e.get("rate_pct"):
                rate = float(e["rate_pct"])
                break
    if rate is None:
        rate = 0.0

    conditions = _parse_annexure_conditions(p_texts)

    short_id = ""
    m = re.search(r"(\d+)/(\d{4})", props.get("cbic_no", ""))
    if m:
        short_id = f"{m.group(1)}/{m.group(2)}"
    if not short_id:
        m = re.search(r"central-tax-rate/(\d+)/(\d+)-(\d{4})", str(xml_path))
        if m:
            short_id = f"{m.group(2)}/{m.group(3)}"

    return {
        "notification_id": notification_id,
        "short_id": short_id,
        "rate_pct": rate,
        "effective_date": eff_date,
        "conditions": conditions,
        "schedules": {
            "I": {
                "rate_pct": rate,
                "entries": entries,
            }
        },
        "instrument_type": "concessional_rate",
    }


KNOWN_CONCESSIONAL = {
    "2/2022": "2022/2-2022-central-tax-rate.xml",
    "14/2025": "2025/14-2025-central-tax-rate.xml",
    "3/2017": "2017/3-2017-central-tax-rate.xml",
    "8/2018": "2018/8-2018-central-tax-rate.xml",
    "40/2017": "2017/40-2017-central-tax-rate.xml",
    "45/2017": "2017/45-2017-central-tax-rate.xml",
    # Phase 1D — remaining goods CT(Rate) instruments
    "5/2017": "2017/5-2017-central-tax-rate.xml",
    "6/2017": "2017/6-2017-central-tax-rate.xml",
    "7/2017": "2017/7-2017-central-tax-rate.xml",
    "8/2017": "2017/8-2017-central-tax-rate.xml",
    "9/2017": "2017/9-2017-central-tax-rate.xml",
    "10/2017": "2017/10-2017-central-tax-rate.xml",
    "11/2017": "2017/11-2017-central-tax-rate.xml",
    "12/2017": "2017/12-2017-central-tax-rate.xml",
    "13/2017": "2017/13-2017-central-tax-rate.xml",
    "14/2017": "2017/14-2017-central-tax-rate.xml",
    "15/2017": "2017/15-2017-central-tax-rate.xml",
    "16/2017": "2017/16-2017-central-tax-rate.xml",
    "17/2017": "2017/17-2017-central-tax-rate.xml",
    "18/2017": "2017/18-2017-central-tax-rate.xml",
    "19/2017": "2017/19-2017-central-tax-rate.xml",
    "20/2017": "2017/20-2017-central-tax-rate.xml",
    "21/2017": "2017/21-2017-central-tax-rate.xml",
    "22/2017": "2017/22-2017-central-tax-rate.xml",
    "23/2017": "2017/23-2017-central-tax-rate.xml",
    "24/2017": "2017/24-2017-central-tax-rate.xml",
    "25/2017": "2017/25-2017-central-tax-rate.xml",
    "26/2017": "2017/26-2017-central-tax-rate.xml",
    "27/2017": "2017/27-2017-central-tax-rate.xml",
    "28/2017": "2017/28-2017-central-tax-rate.xml",
    "29/2017": "2017/29-2017-central-tax-rate.xml",
    "30/2017": "2017/30-2017-central-tax-rate.xml",
    "31/2017": "2017/31-2017-central-tax-rate.xml",
    "32/2017": "2017/32-2017-central-tax-rate.xml",
    "33/2017": "2017/33-2017-central-tax-rate.xml",
    "34/2017": "2017/34-2017-central-tax-rate.xml",
    "35/2017": "2017/35-2017-central-tax-rate.xml",
    "36/2017": "2017/36-2017-central-tax-rate.xml",
    "37/2017": "2017/37-2017-central-tax-rate.xml",
    "38/2017": "2017/38-2017-central-tax-rate.xml",
    "39/2017": "2017/39-2017-central-tax-rate.xml",
    "41/2017": "2017/41-2017-central-tax-rate.xml",
    "42/2017": "2017/42-2017-central-tax-rate.xml",
    "43/2017": "2017/43-2017-central-tax-rate.xml",
    "44/2017": "2017/44-2017-central-tax-rate.xml",
    "46/2017": "2017/46-2017-central-tax-rate.xml",
    "47/2017": "2017/47-2017-central-tax-rate.xml",
    "5/2018": "2018/5-2018-central-tax-rate.xml",
    "6/2018": "2018/6-2018-central-tax-rate.xml",
    "7/2018": "2018/7-2018-central-tax-rate.xml",
    "9/2018": "2018/9-2018-central-tax-rate.xml",
    "10/2018": "2018/10-2018-central-tax-rate.xml",
    "11/2018": "2018/11-2018-central-tax-rate.xml",
    "12/2018": "2018/12-2018-central-tax-rate.xml",
    "13/2018": "2018/13-2018-central-tax-rate.xml",
    "14/2018": "2018/14-2018-central-tax-rate.xml",
    "15/2018": "2018/15-2018-central-tax-rate.xml",
    "16/2018": "2018/16-2018-central-tax-rate.xml",
    "17/2018": "2018/17-2018-central-tax-rate.xml",
    "18/2018": "2018/18-2018-central-tax-rate.xml",
    "19/2018": "2018/19-2018-central-tax-rate.xml",
    "20/2018": "2018/20-2018-central-tax-rate.xml",
    "21/2018": "2018/21-2018-central-tax-rate.xml",
    "22/2018": "2018/22-2018-central-tax-rate.xml",
    "23/2018": "2018/23-2018-central-tax-rate.xml",
    "24/2018": "2018/24-2018-central-tax-rate.xml",
    "25/2018": "2018/25-2018-central-tax-rate.xml",
    "26/2018": "2018/26-2018-central-tax-rate.xml",
    "27/2018": "2018/27-2018-central-tax-rate.xml",
    "28/2018": "2018/28-2018-central-tax-rate.xml",
    "29/2018": "2018/29-2018-central-tax-rate.xml",
    "30/2018": "2018/30-2018-central-tax-rate.xml",
}


def parse_and_save_concessional(
    xml_path: str | Path,
    output_dir: str | Path = "derived/version_history/rate-schedules",
) -> dict[str, Any]:
    result = parse_concessional_notification(xml_path)
    short_id = result.get("short_id", "unknown")
    output_path = Path(output_dir) / f"concessional_{short_id.replace('/', '-')}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    result["_output_path"] = str(output_path)
    return result


def parse_all_concessional(
    corpus_dir: str | Path = "corpus/in/union/notifications/cbic/central-tax-rate",
    output_dir: str | Path = "derived/version_history/rate-schedules",
) -> dict[str, dict[str, Any]]:
    corpus_dir = Path(corpus_dir)
    results: dict[str, dict[str, Any]] = {}

    for short_id, rel_path in KNOWN_CONCESSIONAL.items():
        xml_path = corpus_dir / rel_path
        if not xml_path.exists():
            continue
        try:
            result = parse_and_save_concessional(xml_path, output_dir)
            results[short_id] = result
        except Exception as e:
            print(f"  WARNING: error parsing {short_id}: {e}")

    return results
