"""Compile CGST Act amendment candidates from Finance Act style JSON sources."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .amendment_events import (
    DEFAULT_OBSERVED_AT,
    COMPILER_VERSION,
    sha256_file,
    sha256_text,
    split_compound_block,
    write_jsonl,
)
from .baselines import ACT_WORK
from .identity_registry import load_registry


SUPPORTED_OPS = {"SPLICE", "SUBSTITUTE", "OMIT", "INSERT_CHILD", "INSERT_SIBLING", "COMMENCE"}

_NOISE_LINE_PATTERNS = [
    re.compile(r"^[A-Z][a-z]+\s*\|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
    re.compile(r"^\d+;#"),
    re.compile(r"^\d{12,}$"),
    re.compile(r"^\d+\.\d{6,}$"),
    re.compile(r"^(?:INDIRECT|DIRECT)\s+TAXES$", re.I),
    re.compile(r"^CHAPTER\s+[IVXLC]+$", re.I),
    re.compile(r"^\*+$"),
    re.compile(r"^0+$"),
    re.compile(r"^Finance\s+Acts?\s*\|"),
    re.compile(r"^\d{4}\|"),
]


def _clean_noise(raw: str) -> str:
    """Strip SharePoint/structured-data noise tokens from Finance Act section text."""
    return "\n".join(
        line.strip()
        for line in raw.split("\n")
        if line.strip() and not any(p.search(line.strip()) for p in _NOISE_LINE_PATTERNS)
    )


def _section_component(label: str) -> str:
    return f"{ACT_WORK}/section/{re.sub(r'[^0-9A-Za-z]+', '', label).lower()}"


def _iter_source_files(source_dir: Path, source_family: str) -> Iterable[Path]:
    if source_family == "finance-acts":
        for pattern in ("finance_act_20*.json", "finance_no_1_act_20*.json", "finance_no_2_act_20*.json"):
            yield from sorted(source_dir.glob(pattern))
        unsplit = source_dir / "finance_acts_unsplit.json"
        if unsplit.exists():
            yield unsplit
    elif source_family == "cbic-acts":
        yield from sorted(source_dir.glob("*.json"))
    elif source_family == "taxation-acts":
        yield from sorted(source_dir.glob("taxation*act*.json"))
    else:
        raise ValueError(f"Unsupported Act source family: {source_family}")


def _sections_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    sections = list(data.get("sections", []) or [])
    if not sections:
        for chapter in data.get("chapters", []) or []:
            sections.extend(chapter.get("sections", []) or [])
    for section in sections:
        raw_text = str(section.get("full_text") or section.get("contentText") or "")
        text = _clean_noise(raw_text) if "act|" in raw_text.lower() or "|" in raw_text and re.search(r"\d{12,}", raw_text) else raw_text
        number = str(section.get("section_number") or section.get("sectionNo") or "")
        number_match = re.search(r"(\d+[A-Z]?)", number)
        normalized.append(
            {
                "section_number": number_match.group(1) if number_match else number,
                "description": section.get("description") or section.get("sectionName") or "",
                "full_text": text,
            }
        )
    return normalized


def _clause_span(text: str, section_number: str) -> tuple[int, int, str]:
    """Locate the substantive amendment clause inside noisy split JSON text."""

    candidates: list[int] = []
    if section_number:
        number = re.escape(section_number)
        for match in re.finditer(
            rf"\b{number}\.\s+(?=(?:In|After|Before|For|The|Notwithstanding|Section)\b)",
            text,
            flags=re.I,
        ):
            candidates.append(match.start())
    for pattern in [
        r"\bIn\s+section\s+\d+[A-Z]?\b",
        r"\bAfter\s+section\s+\d+[A-Z]?\b",
        r"\bBefore\s+section\s+\d+[A-Z]?\b",
        r"\bSection\s+\d+[A-Z]?\s+shall\s+be\s+omitted\b",
        r"\b(?:After|Before)\s+the\s+existing\s+section\s+\d+[A-Z]?\b",
        r"\bIn\s+the\s+Central\s+Goods\s+and\s+Services\s+Tax\s+Act\b",
    ]:
        match = re.search(pattern, text, flags=re.I)
        if match:
            candidates.append(match.start())
    if not candidates:
        stripped = text.strip()
        start = text.find(stripped) if stripped else 0
        return start, start + len(stripped), stripped
    start = min(candidates)
    clause = text[start:].strip()
    return start, start + len(clause), clause


def _clause_segments(source_text: str, section_number: str) -> list[dict[str, Any]]:
    start, end, text = _clause_span(source_text, section_number)
    block = {"label": section_number or "1", "start": start, "end": end, "text": text}
    return split_compound_block(block)


def _source_document_id(data: dict[str, Any], path: Path, source_family: str) -> str:
    if path.name == "finance_acts_unsplit.json":
        year = "2024"
        slug = "finance-no-2-act-2024"
    else:
        year_match = re.search(r"(20\d{2})", path.name)
        year = str(data.get("year") or (year_match.group(1) if year_match else "unknown"))
        slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("act") or path.stem).lower()).strip("-")
    return f"/in/union/acts/source/{source_family}/{year}/{slug}"


def _operation(text: str) -> str:
    lowered = text.lower()
    if "shall be omitted" in lowered:
        return "OMIT"
    if "shall be subsituted" in lowered or "shall be substituted" in lowered:
        return "SUBSTITUTE"
    if "shall be inserted" in lowered:
        return "SPLICE" if "after" in lowered or "before" in lowered else "INSERT_CHILD"
    if "shall be renumbered" in lowered:
        return "SUBSTITUTE"
    if "shall come into force" in lowered or "appointed" in lowered:
        return "COMMENCE"
    return "UNKNOWN"


def _parse_record_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    return match.group(0) if match else ""


def _publication_date(data: dict[str, Any]) -> str:
    return _parse_record_date(str(data.get("issueDt") or data.get("publication_date") or ""))


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_named_date(text: str) -> str:
    named = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
        text, flags=re.I,
    )
    if named:
        day, month_name, year = named.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text)
    if match:
        day, month, year = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if match:
        return match.group(0)
    return ""


_HARDCODED_COMMENCEMENT: dict[tuple[str, str], dict[str, str]] = {
    ("2018", "221"): {"date": "2018-02-01", "basis": "hardcoded_commencement", "notification": "2/2018-Central Tax"},
    ("2019", "92"): {"date": "2019-08-27", "basis": "hardcoded_commencement", "notification": "14/2019-Central Tax"},
    ("2019", "104"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "105"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "106"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "107"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "108"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "109"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "110"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2019", "113"): {"date": "2020-01-01", "basis": "hardcoded_commencement", "notification": "01/2020-Central Tax"},
    ("2020", "130"): {"date": "2020-06-30", "basis": "hardcoded_commencement", "notification": "49/2020-Central Tax"},
    ("2021", "111"): {"date": "2021-01-01", "basis": "hardcoded_commencement", "notification": "13/2021-Central Tax"},
    ("2021", "96"): {"date": "2021-01-01", "basis": "hardcoded_commencement", "notification": "13/2021-Central Tax"},
    ("2022", "115"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "116"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "117"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "118"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "119"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "121"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "122"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2022", "124"): {"date": "2022-07-05", "basis": "hardcoded_commencement", "notification": "09/2022-Central Tax"},
    ("2023", "149"): {"date": "2023-07-31", "basis": "hardcoded_commencement", "notification": "28/2023-Central Tax"},
    ("2023", "151"): {"date": "2023-07-31", "basis": "hardcoded_commencement", "notification": "28/2023-Central Tax"},
    ("2023", "152"): {"date": "2023-07-31", "basis": "hardcoded_commencement", "notification": "28/2023-Central Tax"},
    ("2023", "154"): {"date": "2023-07-31", "basis": "hardcoded_commencement", "notification": "28/2023-Central Tax"},
    ("2025", "125"): {"date": "2025-04-01", "basis": "hardcoded_commencement", "notification": "05/2025-Central Tax"},
    ("2026", "153"): {"date": "2026-04-01", "basis": "hardcoded_commencement", "notification": "04/2026-Central Tax"},
    ("2026", "154"): {"date": "2026-04-01", "basis": "hardcoded_commencement", "notification": "04/2026-Central Tax"},
    ("2026", "155"): {"date": "2026-04-01", "basis": "hardcoded_commencement", "notification": "04/2026-Central Tax"},
}


def _effective_date_from_act(data: dict[str, Any], section_number: str) -> dict[str, str]:
    """Resolve explicit commencement ranges from the Finance Act itself."""

    if not section_number.isdigit():
        return {}
    act_title = str(data.get("act") or "")
    if "unsplit" in act_title.lower():
        return {}
    section_one = next((item for item in _sections_from_data(data) if str(item.get("section_number") or "") == "1"), None)
    text = str(section_one.get("full_text") or "") if section_one else ""
    if not text:
        return {}
    target = int(section_number)
    for match in re.finditer(
        r"sections?\s+(\d+)(?:\s+to\s+(\d+))?\s+shall\s+come\s+into\s+force\s+on\s+the\s+(.+?)(?:;|\.|\))",
        text,
        flags=re.I,
    ):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not (start <= target <= end):
            continue
        date_match = re.search(
            r"(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
            match.group(3),
            flags=re.I,
        )
        if not date_match:
            continue
        day, month_name, year = date_match.groups()
        month = _MONTHS.get(month_name.lower())
        if not month:
            continue
        return {
            "date": f"{int(year):04d}-{month:02d}-{int(day):02d}",
            "basis": "finance_act_commencement_clause",
        }
    default_match = re.search(
        r"save\s+as\s+otherwise\s+provided,\s+it\s+shall\s+be\s+deemed\s+to\s+have\s+come\s+into\s+force\s+on\s+the\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
        text,
        flags=re.I,
    )
    if default_match:
        day, month_name, year = default_match.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            return {
                "date": f"{int(year):04d}-{month:02d}-{int(day):02d}",
                "basis": "act_default_commencement_clause",
            }
    return {}


def _expand_section_tokens(value: str) -> set[str]:
    sections: set[str] = set()
    for start, end in re.findall(r"\b(\d+)\s+to\s+(\d+)\b", value, flags=re.I):
        sections.update(str(item) for item in range(int(start), int(end) + 1))
    for item in re.findall(r"\bsection(?:s)?\s+([0-9A-Za-z,\s]+?)(?=\s+of\b|\s+and\b|[.;)]|$)", value, flags=re.I):
        for number in re.findall(r"\d+[A-Za-z]?", item):
            sections.add(number.upper())
    return sections


def _commencement_map(notification_dir: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    mapping: dict[tuple[str, str], dict[str, str]] = {}
    if not notification_dir or not notification_dir.exists():
        return mapping
    for path in sorted(notification_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(record.get("category") or "") != "Central Tax":
            continue
        name = str(record.get("name") or "")
        content_text = str(record.get("contentText") or "")
        text = f"{name}\n{content_text}"
        lowered = text.lower()
        if not re.search(r"finance\s+(?:\([^)]*\)\s*)?act", lowered) or not any(word in lowered for word in ["notify", "appoint", "come into force", "bring", "force"]):
            continue
        if not content_text.strip():
            pdf_b64 = str(record.get("contentPdfBase64") or "")
            if pdf_b64:
                try:
                    import base64
                    import pdfplumber
                    import io
                    pdf_bytes = base64.b64decode(pdf_b64)
                    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                        for page in pdf.pages:
                            page_text = page.extract_text() or ""
                            text += "\n" + page_text
                except Exception:
                    pass
        year_match = re.search(r"Finance\s+(?:\([^)]*\)\s*)?Act,?\s*(\d{4})", text, flags=re.I)
        if not year_match:
            continue
        year = year_match.group(1)
        effective = _parse_named_date(text) or _parse_record_date(str(record.get("issueDt") or ""))
        sections = _expand_section_tokens(text)
        except_sections = _expand_section_tokens(" ".join(re.findall(r"except\s+([^.;]+)", text, flags=re.I)))
        sections -= except_sections
        for section in sections:
            mapping[(year, section.upper())] = {
                "date": effective,
                "basis": "central_tax_commencement_notification",
                "notification": str(record.get("no") or path.stem),
                "notification_record_id": str(record.get("id") or ""),
            }
    return mapping


def _source_year(data: dict[str, Any], path: Path) -> str:
    if data.get("year"):
        return str(data["year"])
    for value in [data.get("act"), path.name]:
        match = re.search(r"(20\d{2})", str(value))
        if match:
            return match.group(1)
    if path.name == "finance_acts_unsplit.json":
        return "2024"
    return ""


def _target_section_number(text: str) -> str | None:
    inserted = _inserted_section_number(text)
    if inserted:
        return inserted
    patterns = [
        r"\bInsertion\s+of\s+new\s+section\s+(\d+[A-Z]?)\b",
        r"\bSubstitution\s+of\s+(?:new\s+section\s+for\s+)?section\s+(\d+[A-Z]?)\b",
        r"\bAmendment\s+of\s+section\s+(\d+[A-Z]?)\b",
        r"\bOmission\s+of\s+sections?\s+(\d+[A-Z]?)\b",
        r"\bFor\s+section\s+(\d+[A-Z]?)\s+of\s+the\s+Central\s+Goods\b",
        r"\bin\s+section\s+(\d+[A-Z]?)\s+of\s+the\s+Central\s+Goods\s+and\s+Services\s+Tax\s+Act",
        r"\bin\s+section\s+(\d+[A-Z]?)\s+of\s+the\s+principal\s+Act",
        r"\bSection\s+(\d+[A-Z]?)\s+of\s+the\s+Central\s+Goods\s+and\s+Services\s+Tax\s+Act\s+shall\s+be\b",
        r"\bSections?\s+(\d+[A-Z]?)\s+(?:and\s+\d+[A-Z]?\s+)?of\s+the\s+Central\s+Goods\s+and\s+Services\s+Tax\s+Act\s+shall\s+be\s+omitted\b",
        r"\bin\s+section\s+(\d+[A-Z]?)\b",
        r"\bafter\s+section\s+(\d+[A-Z]?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return None


def _inserted_section_number(text: str) -> str | None:
    """Return the new section label for whole-section insertion clauses."""
    if not re.search(r"\bafter\s+section\s+\d+[A-Z]?\b", text, flags=re.I):
        return None
    if not re.search(r"\bfollowing\s+section\s+shall\s+be\s+inserted\b", text, flags=re.I):
        return None
    extracted = _extract_quoted_after_namely(text)
    match = re.match(r"\s*(\d+[A-Z]?)\s*\.\s+", extracted, flags=re.I)
    return match.group(1) if match else None


def _compound_inserted_sections(text: str) -> list[dict[str, Any]]:
    """Extract sections from clauses inserting multiple whole sections."""
    if not re.search(r"\bafter\s+section\s+\d+[A-Z]?\b", text, flags=re.I):
        return []
    if not re.search(r"\bfollowing\s+sections\s+shall\s+be\s+inserted\b", text, flags=re.I):
        return []
    insert = re.search(r"\bfollowing\s+sections\s+shall\s+be\s+inserted,?\s+namely\s*:?\s*[—–\-]*\s*(.+)", text, flags=re.I | re.S)
    if not insert:
        return []
    body = insert.group(1)
    label_matches = [
        match
        for match in re.finditer(r'["“”]?\s*(\d+[A-Z])\s*\.\s*\(', body, flags=re.I)
        if not re.search(r"\bsection\s*$", body[max(0, match.start() - 16) : match.start()], flags=re.I)
    ]
    if len(label_matches) < 2:
        return []
    sections: list[dict[str, Any]] = []
    previous_anchor = _anchor_section_number(text)
    for index, match in enumerate(label_matches):
        next_heading_start = label_matches[index + 1].start() if index + 1 < len(label_matches) else len(body)
        heading_start = label_matches[index - 1].end() if index > 0 else 0
        heading_text = body[heading_start:match.start()]
        heading = re.sub(r"\s+", " ", heading_text).strip().strip(' "\'“”.-')
        label = match.group(1).upper()
        section_text = body[match.start():next_heading_start].strip().strip(' "“”')
        _ignored_heading, content = _section_heading_and_content(section_text, label)
        sections.append(
            {
                "label": label,
                "heading": heading,
                "content": content,
                "text": section_text,
                "body_start": insert.start(1) + match.start(),
                "body_end": insert.start(1) + next_heading_start,
                "anchor_section": previous_anchor,
            }
        )
        previous_anchor = label
    return sections


def _anchor_section_number(text: str) -> str | None:
    match = re.search(r"\bafter\s+section\s+(\d+[A-Z]?)\b", text, flags=re.I)
    return match.group(1) if match else None


def _section_heading_and_content(section_text: str, label: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", section_text).strip().strip('"').strip("'")
    cleaned = re.sub(rf"^\s*{re.escape(label)}\s*\.\s*", "", cleaned, flags=re.I)
    heading = ""
    content = cleaned
    split = re.split(r"\.\s*(?=[A-Z(])", cleaned, maxsplit=1)
    if len(split) == 2 and len(split[0]) <= 160:
        heading = split[0].strip()
        content = split[1].strip()
    return heading, content


def _is_target_cbic_amendment_act(data: dict[str, Any]) -> bool:
    title = str(data.get("act") or data.get("title") or "")
    lowered = title.lower()
    return (
        "central goods and services tax" in lowered
        and "amendment" in lowered
        and "integrated goods and services tax" not in lowered
    )


def _is_cbic_amendment_section(section: dict[str, Any]) -> bool:
    text = str(section.get("full_text") or "")
    description = str(section.get("description") or "")
    combined = f"{description}\n{text}".lower()
    if "central goods and services tax act" in combined or "cgst act" in combined:
        return True
    amendment_markers = [
        "amendment of section",
        "insertion of new section",
        "substitution of section",
        "substitution of new section",
        "amendment of schedule",
        "substitution of schedule",
    ]
    if any(marker in combined for marker in amendment_markers):
        return True
    return "principal act" in combined and any(word in combined for word in ["inserted", "substituted", "omitted"])


def _extract_quoted_after_namely(text: str) -> str:
    """Extract quoted replacement text after 'namely:' separator."""
    dash = r"[—–\-]"
    q = r'["\u201c\u201d\']'
    for pattern in [
        rf"namely\s*:?\s*{dash}*\s*{q}\s*(.+?){q}\s*(?:\.|$)",
        rf"namely\s*:?\s*{dash}*\s*{q}\s*(.+)",
        rf"namely\s*:?\s*{dash}+\s*(.+)",
    ]:
        m = re.search(pattern, text, flags=re.I | re.S)
        if m:
            extracted = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".").strip()
            if len(extracted) > 10:
                return extracted
    return ""


def _payload(operation: str, text: str) -> dict[str, Any]:
    if operation == "SUBSTITUTE":
        quoted = r"[\"'\u201c\u201d]"
        match = re.search(
            rf"for\s+the\s+(?:words?|figures?|letters?|words\s+and\s+figures)\s+{quoted}\s*(.+?)\s*{quoted}"
            rf".*?(?:the\s+(?:words?|figures?|letters?|words\s+and\s+figures)\s+)?{quoted}\s*(.+?)\s*{quoted}"
            r"\s*(?:shall\s+be\s+substituted|,)",
            text,
            flags=re.I | re.S,
        )
        if match:
            return {
                "old_text": re.sub(r"\s+", " ", match.group(1)).strip(),
                "new_text": re.sub(r"\s+", " ", match.group(2)).strip(),
            }
        if re.search(r"for\s+(?:section\s+\d+[A-Z]?|sub-section\s*\(\d+\)|clause\s*\([a-z]\)|the\s+proviso|marginal\s+heading).*?shall\s+be\s+subst?ituted", text, flags=re.I | re.S):
            replacement = _extract_quoted_after_namely(text)
            if replacement:
                if re.search(r"for\s+the\s+marginal\s+heading", text, flags=re.I):
                    return {"structural_heading": replacement, "structural_text": replacement}
                return {"structural_text": replacement}
    if operation == "SPLICE":
        insert = re.search(r"(?:the\s+following\s+[^:]+|namely)\s*:?\s*[—–\-]+\s*(.+)", text, flags=re.I | re.S)
        if not insert:
            insert = re.search(r"(?:the\s+following\s+[^:]+|namely)\s*[:\\-]\s*(.+)", text, flags=re.I | re.S)
        anchor = re.search(r"(after|before)\s+([^,;.]+)", text, flags=re.I)
        payload = {
            "insert_text": re.sub(r"\s+", " ", insert.group(1)).strip() if insert else "",
            "position": anchor.group(1).lower() if anchor else "after",
        }
        inserted_section = _inserted_section_number(text)
        if inserted_section:
            section_text = _extract_quoted_after_namely(text) or payload["insert_text"]
            heading, content = _section_heading_and_content(section_text, inserted_section)
            payload.update(
                {
                    "label": inserted_section,
                    "heading": heading,
                    "content": content,
                    "node_type": "section",
                    "whole_section_insert": True,
                }
            )
        return payload
    if operation == "OMIT":
        quoted = r"[\"'\u201c\u201d]"
        match = re.search(
            rf"the\s+(?:words?|figures?|letters?|words\s+and\s+figures)\s+{quoted}\s*(.+?)\s*{quoted}.*?shall\s+be\s+omitted",
            text,
            flags=re.I | re.S,
        )
        if match:
            return {"omit_text": re.sub(r"\s+", " ", match.group(1)).strip(), "whole_component": False}
        whole = bool(re.search(r"^\s*(?:section\s+\d+[A-Z]?\s+)?(?:shall\s+be\s+)?omitted\b", text, flags=re.I))
        return {"omitted": whole, "whole_component": whole}
    return {"text": re.sub(r"\s+", " ", text).strip()}


def _is_metadata_only_act_effect(text: str) -> bool:
    """Detect notification-level effects that do not amend Act section text."""
    lowered = text.lower()
    if "schedule iii" in lowered:
        return False
    if re.search(r"\bshall\s+be\s+(?:inserted|substituted|omitted)\b", lowered):
        return False
    if not any(marker in lowered for marker in ["notification of the government", "notification issued", "g.s.r."]):
        return False
    return bool(
        re.search(r"\bretrospective(?:ly)?\b", lowered)
        or re.search(r"\bretrospective\s+(?:effect|exemption)\b", lowered)
        or re.search(r"\bexemption\s+from,\s+or\s+levy\s+or\s+collection\b", lowered)
        or re.search(r"\bshall\s+be\s+deemed\s+to\s+have\b.*\bcome\s+into\s+force\b", lowered)
    )


def _is_schedule_act_effect(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(r"\bin\s+schedule\s+[ivxlcdm]+\s+to\s+(?:the\s+principal\s+act|the\s+central\s+goods\s+and\s+services\s+tax\s+act)", lowered)
        or re.search(r"\bamendment\s+of\s+schedule\s+[ivxlcdm]+\b", lowered)
    )


def _is_non_target_act_effect(text: str, target_work: str) -> bool:
    if target_work != "/in/union/acts/cgst-act-2017":
        return False
    lowered = text.lower()
    non_target_acts = [
        "integrated goods and services tax act",
        "union territory goods and services tax act",
        "central excise act",
        "customs act",
        "customs tariff act",
    ]
    if not any(act in lowered for act in non_target_acts):
        return False
    non_target_pattern = (
        r"(?:integrated\s+goods\s+and\s+services\s+tax\s+act|union\s+territory\s+goods\s+and\s+services\s+tax\s+act|"
        r"central\s+excise\s+act|customs\s+act|customs\s+tariff\s+act)"
    )
    leading_target_is_non_target = bool(
        re.search(rf"\bin\s+(?:the\s+)?{non_target_pattern}\b", lowered[:300])
        or re.search(rf"\bin\s+(?:sub-section\s+\([^)]+\)\s+of\s+)?section\s+\d+[a-z]?\s+of\s+the\s+{non_target_pattern}\b", lowered)
        or re.search(
            rf"\b(?:amendment|insertion|substitution|omission)\s+of\s+(?:new\s+)?section\s+\d+[a-z]?\s+"
            rf"(?:of|in)\s+the\s+{non_target_pattern}\b",
            lowered,
        )
    )
    cross_statute_relaxation = bool(
        re.search(rf"\bnotwithstanding\s+anything\s+contained\s+in\s+{non_target_pattern}\b", lowered[:400])
        and "time limit specified in, or prescribed or notified under, the said acts" in lowered
    )
    if leading_target_is_non_target or cross_statute_relaxation:
        return True
    if "central goods and services tax act" in lowered or re.search(r"\bprincipal\s+act\b", lowered):
        return False
    return False


def _load_baseline_texts(registry_path: Path, target_work: str) -> dict[str, str]:
    registry = load_registry(registry_path)
    baseline_path = registry.baseline_path(target_work)
    if not baseline_path:
        return {}
    components_path = Path(baseline_path) / "baseline_components.jsonl"
    if not components_path.exists():
        return {}
    texts: dict[str, str] = {}
    for line in components_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        component_id = row.get("component_id")
        if component_id:
            texts[str(component_id)] = str(row.get("text") or "")
    return texts


def _event(
    *,
    path: Path,
    data: dict[str, Any],
    section: dict[str, Any],
    target_work: str,
    source_family: str,
    commencement: dict[tuple[str, str], dict[str, str]],
    baseline_texts: dict[str, str],
    clause_segment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_text = str(section.get("full_text") or "")
    source_hash = sha256_text(source_text)
    section_number = str(section.get("section_number") or "")
    if clause_segment:
        source_start = int(clause_segment["start"])
        source_end = int(clause_segment["end"])
        text = str(clause_segment.get("parse_text") or clause_segment["text"])
        excerpt_text = str(clause_segment["text"])
    else:
        source_start, source_end, text = _clause_span(source_text, section_number)
        excerpt_text = text
    target_section = _target_section_number(text)
    component_id = _section_component(target_section) if target_section else target_work
    operation = _operation(text)
    document_id = _source_document_id(data, path, source_family)
    event_seed = "|".join([document_id, section_number, str(source_start), str(source_end), sha256_text(excerpt_text)])
    source_year = _source_year(data, path)
    commencement_info = commencement.get((source_year, section_number.upper()), {})
    if not commencement_info:
        commencement_info = _effective_date_from_act(data, section_number)
    if not commencement_info:
        commencement_info = _HARDCODED_COMMENCEMENT.get((source_year, section_number.upper()), {})
    effective_date = commencement_info.get("date")
    if not effective_date and source_family == "cbic-acts" and "come into force on such date" not in text.lower():
        effective_date = _publication_date(data)
        commencement_info = {
            "date": effective_date,
            "basis": "act_publication_date_no_deferred_commencement_clause",
        }
    payload = _payload(operation, text)
    if operation == "SPLICE" and payload.get("whole_section_insert"):
        operation = "INSERT_SIBLING"
    anchor_component_id = None
    if operation == "INSERT_SIBLING" and payload.get("whole_section_insert"):
        anchor_section = _anchor_section_number(text)
        if anchor_section:
            anchor_component_id = _section_component(anchor_section)
    baseline_text = baseline_texts.get(component_id, "")
    direct_substitute_verified = (
        operation == "SUBSTITUTE"
        and bool(payload.get("old_text"))
        and bool(payload.get("new_text"))
        and bool(baseline_text)
        and str(payload.get("old_text")) in baseline_text
    )
    structural_substitute_verified = (
        operation == "SUBSTITUTE"
        and bool(payload.get("structural_text"))
        and component_id != target_work
    )
    whole_section_omit = (
        operation == "OMIT"
        and bool(payload.get("whole_component"))
        and bool(re.search(r"^\s*section\s+\d+[A-Z]?\s+shall\s+be\s+omitted\b", text, flags=re.I))
    )
    whole_section_insert = (
        operation == "INSERT_SIBLING"
        and bool(payload.get("whole_section_insert"))
        and component_id != target_work
        and bool(anchor_component_id)
        and bool(payload.get("label"))
        and bool(payload.get("content"))
    )
    reasons = []
    if component_id == target_work:
        reasons.append("target_not_resolved")
    if operation not in SUPPORTED_OPS:
        reasons.append("unsupported_or_unknown_act_operation")
    if not effective_date:
        reasons.append("date_not_resolved")
    if operation == "SUBSTITUTE" and not direct_substitute_verified and not structural_substitute_verified:
        reasons.append("substitution_text_not_verified")
    if operation == "OMIT" and not whole_section_omit:
        reasons.append("partial_omit_requires_precise_delete_payload")
    metadata_only = operation == "UNKNOWN" and component_id == target_work and _is_metadata_only_act_effect(text)
    if metadata_only:
        payload = {
            **payload,
            "metadata_only": True,
            "triage_lane": "metadata_only",
            "metadata_only_reason": "act_notification_level_retrospective_effect",
        }
    schedule_lane = (
        not metadata_only
        and component_id == target_work
        and _is_schedule_act_effect(text)
    )
    if schedule_lane:
        payload = {
            **payload,
            "schedule_lane_pending_baseline": True,
            "triage_lane": "schedule_lane_pending_baseline",
            "schedule_lane_reason": "act_schedule_component_not_in_baseline",
        }
    out_of_scope_lane = (
        not metadata_only
        and not schedule_lane
        and _is_non_target_act_effect(text, target_work)
    )
    if out_of_scope_lane:
        payload = {
            **payload,
            "act_out_of_scope": True,
            "triage_lane": "act_out_of_scope",
            "act_out_of_scope_reason": "non_target_act_reference",
        }
    materializable = (
        not out_of_scope_lane
        and
        operation in {"OMIT", "SUBSTITUTE", "INSERT_SIBLING"}
        and component_id != target_work
        and bool(effective_date)
        and operation in SUPPORTED_OPS
        and (operation != "SUBSTITUTE" or direct_substitute_verified or structural_substitute_verified)
        and (operation != "OMIT" or whole_section_omit)
        and (operation != "INSERT_SIBLING" or whole_section_insert)
    )
    if not materializable:
        reasons.append("act_materialization_requires_review")
    if metadata_only:
        reasons = [reason for reason in reasons if reason != "act_materialization_requires_review"]
        reasons.append("metadata_only")
    if schedule_lane:
        reasons = [reason for reason in reasons if reason != "act_materialization_requires_review"]
        reasons.append("schedule_lane_pending_baseline")
    if out_of_scope_lane:
        reasons = [reason for reason in reasons if reason != "act_materialization_requires_review"]
        reasons.append("act_out_of_scope")
    status = "validated" if materializable else "needs_review"
    return {
        "event_id": f"evt_{source_family.replace('-', '_')}_{sha256_text(event_seed)[:16]}",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": operation,
        "source": {
            "document_id": document_id,
            "record_id": section_number,
            "instrument_number": str(data.get("act") or path.stem),
            "issuing_authority": "/in/authority/parliament-of-india",
            "publication_date": _publication_date(data),
            "source_url": str(data.get("source") or ""),
            "source_file_sha256": sha256_file(path),
            "source_text_sha256": source_hash,
            "text_source": source_family,
        },
        "legal_time": {
            "commencement_date": effective_date,
            "applicability_start": effective_date,
            "applicability_end": None,
            "retrospective": False,
            "date_basis": commencement_info.get("basis", "unresolved_finance_act_commencement"),
        },
        "system_time": {
            "observed_at": DEFAULT_OBSERVED_AT,
            "compiled_at": DEFAULT_OBSERVED_AT,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": target_work,
            "component_id": component_id,
            "anchor_component_id": anchor_component_id,
            "anchor_text": None,
            "anchor_hash": None,
            "anchor_occurrence": None,
        },
        "payload": payload,
        "evidence": {
            "source_span": {"start": source_start, "end": source_end, "text_hash": sha256_text(excerpt_text)},
            "excerpt": re.sub(r"\s+", " ", excerpt_text).strip()[:500],
            "parser_trace": {"pattern_id": "act_amendment_section_v1", "confidence": 0.45},
        },
        "validation": {
            "target_resolved": component_id != target_work,
            "anchor_resolved": operation == "SUBSTITUTE" and (direct_substitute_verified or structural_substitute_verified),
            "date_resolved": bool(effective_date),
            "source_span_verified": True,
            "materializable": materializable,
        },
        "status": status,
        "review": {
            "required": status != "validated",
            "review_reasons": sorted(set(reasons)),
            "reviewed_by": None,
            "reviewed_at": None,
        },
    }


def _derived_compound_section_events(event: dict[str, Any], text: str, source_start: int) -> list[dict[str, Any]]:
    sections = _compound_inserted_sections(text)
    if not sections:
        return []
    derived: list[dict[str, Any]] = []
    for section in sections:
        label = str(section["label"])
        component_id = _section_component(label)
        anchor_section = str(section.get("anchor_section") or "")
        source_span = {
            "start": source_start + int(section["body_start"]),
            "end": source_start + int(section["body_end"]),
            "text_hash": sha256_text(str(section["text"])),
        }
        derived_event = json.loads(json.dumps(event))
        derived_event["event_id"] = f"{event['event_id']}_section_{label.lower()}"
        derived_event["operation"] = "INSERT_SIBLING"
        derived_event["target"] = {
            **derived_event.get("target", {}),
            "component_id": component_id,
            "anchor_component_id": _section_component(anchor_section) if anchor_section else None,
        }
        derived_event["payload"] = {
            "label": label,
            "heading": section.get("heading", ""),
            "content": section.get("content", ""),
            "insert_text": section.get("text", ""),
            "node_type": "section",
            "position": "after",
            "whole_section_insert": True,
            "compound_parent_event_id": event["event_id"],
        }
        derived_event["evidence"] = {
            **(derived_event.get("evidence") or {}),
            "source_span": source_span,
            "excerpt": re.sub(r"\s+", " ", str(section["text"])).strip()[:500],
            "parser_trace": {
                **((derived_event.get("evidence") or {}).get("parser_trace") or {}),
                "compound_parent_event_id": event["event_id"],
                "compound_section_split": True,
            },
        }
        derived_event["validation"] = {
            **(derived_event.get("validation") or {}),
            "target_resolved": True,
            "anchor_resolved": bool(anchor_section),
            "materializable": bool(anchor_section and section.get("content")),
        }
        derived_event["status"] = "validated" if derived_event["validation"]["materializable"] else "needs_review"
        derived_event["review"] = {
            "required": derived_event["status"] != "validated",
            "review_reasons": [] if derived_event["status"] == "validated" else ["act_materialization_requires_review"],
            "reviewed_by": None,
            "reviewed_at": None,
        }
        derived.append(derived_event)
    return derived


def compile_act_events(
    *,
    registry_path: Path,
    source_dir: Path,
    source_family: str,
    target_work: str,
    output: Path,
    review_output: Path,
    commencement_dir: Path | None = Path("data/Law/cbic_tax_portal/notifications"),
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    resolved = registry.resolve_corpus_id(target_work) or target_work
    if resolved != ACT_WORK:
        raise ValueError(f"Act compiler target must be {ACT_WORK}")
    events: list[dict[str, Any]] = []
    commencement = _commencement_map(commencement_dir)
    baseline_texts = _load_baseline_texts(registry_path, resolved)
    for path in _iter_source_files(source_dir, source_family):
        data = json.loads(path.read_text(encoding="utf-8"))
        if source_family == "cbic-acts" and not _is_target_cbic_amendment_act(data):
            continue
        for section in _sections_from_data(data):
            text = str(section.get("full_text") or "")
            if source_family == "cbic-acts":
                if not _is_cbic_amendment_section(section):
                    continue
            elif source_family == "taxation-acts":
                combined = (text + " " + str(section.get("description",""))).lower()
                if "central goods and services tax act" not in combined and "act 12 of 2017" not in combined:
                    continue
            elif "central goods and services tax act" not in text.lower() and "cgst act" not in text.lower():
                continue
            if "customs act" in text.lower() and "central goods and services tax" not in text.lower():
                continue
            if "economic offences" in text.lower():
                continue
            for clause_segment in _clause_segments(text, str(section.get("section_number") or "")):
                event = _event(
                    path=path,
                    data=data,
                    section=section,
                    target_work=resolved,
                    source_family=source_family,
                    commencement=commencement,
                    baseline_texts=baseline_texts,
                    clause_segment=clause_segment,
                )
                parse_text = str(clause_segment.get("parse_text") or clause_segment["text"])
                derived = _derived_compound_section_events(event, parse_text, int(clause_segment["start"]))
                events.extend(derived or [event])
    _flag_same_date_conflicts(events)
    events.sort(key=lambda event: event["event_id"])
    write_jsonl(events, output)
    counts = Counter(event["status"] for event in events)
    reasons = Counter(reason for event in events for reason in event.get("review", {}).get("review_reasons", []))
    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "event_count": len(events),
        "counts": {"by_status": dict(counts), "by_review_reason": dict(reasons)},
        "non_validated_events": [
            {
                "event_id": event["event_id"],
                "operation": event["operation"],
                "target": event["target"],
                "source_document_id": event["source"]["document_id"],
                "source_span": event["evidence"]["source_span"],
                "excerpt": event["evidence"]["excerpt"],
                "parser_pattern": event["evidence"]["parser_trace"]["pattern_id"],
                "review_reasons": event["review"]["review_reasons"],
            }
            for event in events
            if event["status"] != "validated"
        ],
    }
    review_output.parent.mkdir(parents=True, exist_ok=True)
    review_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"ok": True, "events": len(events), "output": str(output), "review_output": str(review_output)}


def _flag_same_date_conflicts(events: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        date_value = event.get("legal_time", {}).get("applicability_start") or ""
        component_id = event.get("target", {}).get("component_id") or ""
        if not date_value or not component_id:
            continue
        grouped.setdefault((component_id, date_value), []).append(event)
    for (_component_id, _date), slot in grouped.items():
        payload_hashes = {
            sha256_text(json.dumps(event.get("payload", {}), sort_keys=True))
            for event in slot
        }
        if len(slot) <= 1 or len(payload_hashes) <= 1:
            continue
        for event in slot:
            reasons = set(event.get("review", {}).get("review_reasons", []))
            reasons.add("same_effective_date_conflict")
            event["review"]["review_reasons"] = sorted(reasons)
            event["review"]["required"] = True
            event["status"] = "needs_review"
            event.setdefault("validation", {})["materializable"] = False


__all__ = ["compile_act_events"]
