"""Structure parsers for source archives.

Deterministic parsing preserves exact source spans. Optional LLM parsing may
propose structure, but validation still checks that every node points back to
the extracted source text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .renderer import canonicalize_legacy_reference


STRUCTURE_PROMPT = """You are a legal document structure parser for Indian legal documents.

Return JSON only. Do not rewrite legal text. Every node must reference exact
character offsets in the provided source text.

Output schema:
{
  "document_type": "notification|act|rules|form|circular|order",
  "parser": "llm",
  "nodes": [
    {
      "type": "notification_title|publication_date|preamble|commencement|amendment|paragraph",
      "label": "string",
      "start": 0,
      "end": 10,
      "confidence": 0.0
    }
  ],
  "references": [
    {
      "source_node": "label or empty",
      "target": "canonical or literal reference",
      "kind": "rule|section|form|notification|unknown",
      "start": 0,
      "end": 10,
      "confidence": 0.0
    }
  ]
}
"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block_spans(text: str) -> list[tuple[int, int, str]]:
    spans = _double_newline_block_spans(text)
    line_spans = _line_aware_block_spans(text)
    if line_spans and any(end - start > 2000 for start, end, _block in spans):
        return line_spans
    if len(spans) > 4:
        return spans
    if len(spans) > 1 and len(text.splitlines()) <= len(spans) * 3:
        return spans
    return line_spans or spans


def _double_newline_block_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    cursor = 0
    for block in text.split("\n\n"):
        start = text.find(block, cursor)
        if start == -1:
            start = cursor
        end = start + len(block)
        if block.strip():
            stripped_start = start + len(block) - len(block.lstrip())
            stripped_end = start + len(block.rstrip())
            spans.append((stripped_start, stripped_end, text[stripped_start:stripped_end]))
        cursor = end + 2
    return spans


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        start = cursor
        cursor += len(raw_line)
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        stripped_start = start + len(line) - len(line.lstrip())
        stripped_end = start + len(line.rstrip())
        spans.append((stripped_start, stripped_end, text[stripped_start:stripped_end]))
    return spans


def _starts_new_block(line: str, current_lines: list[str]) -> bool:
    stripped = line.strip()
    lowered = stripped.lower()
    if not current_lines:
        return True
    if lowered.startswith(("notification", "corrigendum", "table", "explanation.", "provided")):
        return True
    if lowered.startswith(("annexure", "statement", "declaration", "undertaking", "self- declaration")):
        return True
    if re.match(r"[\[(]?\s*form\s+gst\b", lowered):
        return True
    if re.match(r"part\s+[a-z]\b", lowered):
        return True
    if lowered in {"verification", "notes"} or lowered.startswith(("verification:", "notes -", "notes:")):
        return True
    if lowered == "or" or lowered.startswith(("order of ", "this has reference", "the effective date")):
        return True
    if re.match(r"o\s+whereas\b", lowered):
        return True
    if re.match(r"new delhi,\s+the\s+", lowered):
        return True
    if lowered.startswith("g.s.r."):
        return True
    if re.match(r"(no\.|notification no\.)\s*\d+", lowered):
        return True
    if re.match(r"[\[(]?\s*\d+[A-Z]?(?:\.|\s+[A-Z])", stripped):
        return True
    if re.match(r"(?:\d+\s+\[\s*)?\*?\s*section\s+(?:\d+\s+\[\s*)?\d+[A-Z]?\b", stripped, flags=re.IGNORECASE):
        return True
    if re.match(r"\d+[A-Z](?:,|\s)", stripped):
        return True
    if re.match(r"[A-Z](?:\.\d+)+\s+", stripped):
        return True
    if re.match(r"\d{1,4}\s+[A-Z][A-Za-z0-9&.,()/ -]+", stripped):
        return True
    if re.match(r"\([0-9a-z]+\)\s+", stripped, flags=re.IGNORECASE):
        return True
    if re.match(r"\[F\.\s*No\.", stripped, flags=re.IGNORECASE):
        return True
    return False


def _line_aware_block_spans(text: str) -> list[tuple[int, int, str]]:
    lines = _line_spans(text)
    groups: list[tuple[int, int, list[str]]] = []
    current_start: int | None = None
    current_end: int | None = None
    current_lines: list[str] = []

    for start, end, line in lines:
        if _starts_new_block(line, current_lines):
            if current_start is not None and current_end is not None:
                groups.append((current_start, current_end, current_lines))
            current_start = start
            current_lines = [line]
        else:
            current_lines.append(line)
        current_end = end

    if current_start is not None and current_end is not None:
        groups.append((current_start, current_end, current_lines))

    return [(start, end, text[start:end]) for start, end, _lines in groups if text[start:end].strip()]


def _looks_like_act_section(block: str) -> bool:
    stripped = block.strip()
    match = re.match(
        r"(?:\d+\s+\[\s*)?(?:\*+\s*)?(?:section\s+(?:\d+\s+\[\s*)?)?\d+[A-Z]?(?:\.\s*|\s+-\s+)(.+)",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return False
    heading_text = re.sub(r"\s+", " ", match.group(1)).strip()
    lowered = heading_text.lower()
    if re.match(r"^[0-9]{2,}\b", heading_text):
        return False
    if lowered.startswith(
        (
            "from rs.",
            "from rs ",
            "tariff item ",
            "for the purposes of tariff",
            "for the purposes of the tariff",
            "in the case of ",
            "above rs.",
            "upto rs.",
            "up to rs.",
        )
    ):
        return False
    if " scheme, " in lowered and "section" not in lowered and "act" not in lowered:
        return False
    return True


def _looks_like_rule(block: str) -> bool:
    stripped = block.strip()
    match = re.match(r"[\[(]?\s*(\d+[A-Z]?)(?:\.\s*|\s+-\s+|\s+)(.+)", stripped, flags=re.DOTALL)
    if not match:
        return False
    heading_text = re.sub(r"\s+", " ", match.group(2)).strip()
    lowered = heading_text.lower()
    if not heading_text:
        return False
    if lowered.startswith(
        (
            "chapter ",
            "form gst ",
            "table ",
            "annexure",
            "notification ",
            "central goods and services tax rules",
        )
    ):
        return False
    if re.match(r"^[0-9]{2,}\b", heading_text):
        return False
    return True


def _node_type(block: str, document_type: str = "") -> str:
    stripped = block.strip()
    lowered = stripped.lower()
    if document_type == "act" and _looks_like_act_section(stripped):
        return "section"
    if document_type == "rules" and _looks_like_rule(stripped):
        return "rule"
    if re.match(r"[\[(]?\s*form\s+gst\b", lowered):
        return "form"
    if lowered.startswith("table"):
        return "table"
    if lowered.startswith("annexure"):
        return "annexure"
    if lowered.startswith("statement"):
        return "statement"
    if lowered.startswith(("declaration", "undertaking", "self- declaration")):
        return "declaration"
    if re.match(r"part\s+[a-z]\b", lowered):
        return "form_part"
    if lowered.startswith("verification"):
        return "verification"
    if lowered.startswith("notification"):
        return "notification_title"
    if re.match(r"new delhi,\s+the\s+", lowered):
        return "publication_date"
    if lowered.startswith("g.s.r."):
        return "preamble"
    if re.match(r"1\.\s*\(1\)", stripped) and "come into force" in lowered:
        return "commencement"
    if re.match(r"\d+\.", stripped) and any(
        phrase in lowered
        for phrase in (
            "shall be inserted",
            "shall be substituted",
            "shall be omitted",
            "shall be deleted",
            "after rule",
            "in form",
        )
    ):
        return "amendment"
    return "paragraph"


def _label_for_block(index: int, block: str) -> str:
    number = re.match(
        r"[\[(]?\s*(?:\d+\s+\[\s*)?(?:\*+\s*)?(?:section\s+(?:\d+\s+\[\s*)?)?(\d+[A-Z]?)(?:\.|\s+-|\s+)",
        block.strip(),
        flags=re.IGNORECASE,
    )
    if number:
        return number.group(1).lower()
    return str(index)


def _line_containing(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end]


def _nearby_text(text: str, start: int, end: int, radius: int = 120) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _is_gazette_publication_header(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", text.lower().replace("-\n", "-"))
    lowered = re.sub(r"sub-\s+section", "sub-section", lowered)
    return (
        "gazette of india" in lowered
        and "part" in lowered
        and "section" in lowered
        and "sub-section" in lowered
    )


def _section_reference_target(text: str, start: int, end: int, document_type: str) -> str:
    section = re.search(r"([0-9]+[A-Z]?)", text[start:end], flags=re.IGNORECASE)
    section_label = section.group(1).lower() if section else ""
    nearby = re.sub(r"\s+", " ", text[end : min(len(text), end + 220)].lower())
    act_targets = [
        ("integrated goods and services tax act", "/in/union/acts/igst-act-2017"),
        ("central goods and services tax act", "/in/union/acts/cgst-act-2017"),
        ("income-tax act", "/in/union/acts/income-tax-act-1961"),
        ("income tax act", "/in/union/acts/income-tax-act-1961"),
        ("customs tariff act", "/in/union/acts/customs-tariff-act-1975"),
        ("customs act", "/in/union/acts/customs-act-1962"),
        ("central excise act", "/in/union/acts/central-excise-act-1944"),
    ]
    matches = [(nearby.find(phrase), act_id) for phrase, act_id in act_targets if phrase in nearby]
    if matches:
        _position, act_id = min(matches, key=lambda item: item[0])
        return f"{act_id}/section/{section_label}"
    if document_type != "act":
        return canonicalize_legacy_reference(f"CGST_Act_2017/Section_{section_label.upper()}")
    return ""


def _find_references(text: str, document_type: str = "notification") -> list[dict[str, Any]]:
    patterns = [
        ("rule", re.compile(r"\brule\s+([0-9]+[A-Z]?)\b", re.IGNORECASE)),
        ("section", re.compile(r"\bsection\s+([0-9]+[A-Z]?)\b", re.IGNORECASE)),
        ("form", re.compile(r"\bFORM\s+GST\s+([A-Z]{2,5}\s*-?\s*[0-9]{1,3}[A-Z]?)\b", re.IGNORECASE)),
    ]
    references: list[dict[str, Any]] = []
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            if kind == "section" and (
                _is_gazette_publication_header(_line_containing(text, match.start(), match.end()))
                or _is_gazette_publication_header(_nearby_text(text, match.start(), match.end()))
            ):
                continue
            literal = match.group(0)
            if kind == "rule":
                target = canonicalize_legacy_reference(f"CGST_Rules/Rule_{match.group(1).upper()}")
            elif kind == "section":
                target = _section_reference_target(text, match.start(), match.end(), document_type)
                if not target:
                    continue
            else:
                form_ref = match.group(1).replace("-", "_").replace(" ", "_").upper()
                target = canonicalize_legacy_reference(f"FORM_GST_{form_ref}")
            references.append(
                {
                    "source_node": "",
                    "target": target,
                    "kind": kind,
                    "start": match.start(),
                    "end": match.end(),
                    "text_hash": text_hash(literal),
                    "confidence": 1.0,
                }
            )
    references.sort(key=lambda ref: (ref["start"], ref["end"], ref["kind"]))
    return references


def _merge_continuation_nodes(text: str, nodes: list[dict[str, Any]], document_type: str) -> list[dict[str, Any]]:
    if document_type not in {"act", "rules"}:
        return nodes
    owner_type = "section" if document_type == "act" else "rule"
    merged: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    continuation_types = {"paragraph", "table", "annexure", "statement", "declaration", "form_part", "verification"}
    for node in nodes:
        node_type = node.get("type")
        if node_type == owner_type:
            if current:
                current["text_hash"] = text_hash(text[current["start"] : current["end"]])
                merged.append(current)
            current = dict(node)
            continue
        if current and node_type in continuation_types:
            end = node.get("end")
            if isinstance(end, int) and end >= current["end"]:
                current["end"] = end
                current["confidence"] = min(float(current.get("confidence", 1.0)), float(node.get("confidence", 1.0)))
                continue
        if current:
            current["text_hash"] = text_hash(text[current["start"] : current["end"]])
            merged.append(current)
            current = None
        merged.append(node)
    if current:
        current["text_hash"] = text_hash(text[current["start"] : current["end"]])
        merged.append(current)
    return merged


def parse_structure_deterministic(extracted: dict[str, Any], document_type: str = "notification") -> dict[str, Any]:
    """Parse extracted text into typed nodes with exact character spans."""
    text = extracted.get("text", "")
    nodes = []
    for index, (start, end, block) in enumerate(_block_spans(text), start=1):
        nodes.append(
            {
            "type": _node_type(block, document_type=document_type),
                "label": _label_for_block(index, block),
                "start": start,
                "end": end,
                "text_hash": text_hash(text[start:end]),
                "confidence": 1.0,
            }
        )
    nodes = _merge_continuation_nodes(text, nodes, document_type)

    return {
        "document_type": document_type,
        "parser": "deterministic-india-profile-v1",
        "nodes": nodes,
        "references": _find_references(text, document_type=document_type),
    }


def parse_structure_paragraphs(extracted: dict[str, Any], document_type: str = "notification") -> dict[str, Any]:
    """Fallback parser that only emits paragraph spans."""
    text = extracted.get("text", "")
    nodes = []
    for index, (start, end, _block) in enumerate(_block_spans(text), start=1):
        nodes.append(
            {
                "type": "paragraph",
                "label": str(index),
                "start": start,
                "end": end,
                "text_hash": text_hash(text[start:end]),
                "confidence": 1.0,
            }
        )
    return {
        "document_type": document_type,
        "parser": "deterministic-paragraph-v1",
        "nodes": nodes,
        "references": [],
    }


def validate_structure_spans(extracted: dict[str, Any], structure: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate that nodes and references resolve to exact source slices."""
    text = extracted.get("text", "")
    errors: list[str] = []
    warnings: list[str] = []
    previous_end = -1
    low_confidence_threshold = 0.8

    for index, node in enumerate(structure.get("nodes", []), start=1):
        start = node.get("start")
        end = node.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            errors.append(f"node {index}: start/end must be integers")
            continue
        if start < 0 or end < start or end > len(text):
            errors.append(f"node {index}: invalid span {start}:{end}")
            continue
        if start < previous_end:
            warnings.append(f"node {index}: span overlaps previous node")
        previous_end = max(previous_end, end)

        expected_hash = node.get("text_hash")
        if expected_hash and expected_hash != text_hash(text[start:end]):
            errors.append(f"node {index}: text_hash does not match extracted text")
        if not text[start:end].strip():
            warnings.append(f"node {index}: empty text span")
        confidence = node.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < low_confidence_threshold:
            warnings.append(f"node {index}: low confidence {confidence}")

    for index, ref in enumerate(structure.get("references", []), start=1):
        start = ref.get("start")
        end = ref.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            errors.append(f"reference {index}: start/end must be integers")
            continue
        if start < 0 or end < start or end > len(text):
            errors.append(f"reference {index}: invalid span {start}:{end}")
            continue
        expected_hash = ref.get("text_hash")
        if expected_hash and expected_hash != text_hash(text[start:end]):
            errors.append(f"reference {index}: text_hash does not match extracted text")
        confidence = ref.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < low_confidence_threshold:
            warnings.append(f"reference {index}: low confidence {confidence}")

    if not structure.get("nodes"):
        errors.append("structure has no nodes")

    return errors, warnings


def parse_structure_with_llm(
    extracted: dict[str, Any],
    api_key: str,
    model: str,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Ask an OpenAI-compatible model for structure JSON, then validate spans."""
    from openai import OpenAI

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": STRUCTURE_PROMPT},
            {"role": "user", "content": extracted.get("text", "")},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    structure = json.loads(response.choices[0].message.content)
    structure.setdefault("parser", f"llm:{model}")

    errors, _warnings = validate_structure_spans(extracted, structure)
    if errors:
        raise ValueError("LLM structure failed span validation: " + "; ".join(errors))
    return structure


def parse_structure(
    extracted: dict[str, Any],
    document_type: str = "notification",
    mode: str = "deterministic",
    model: str | None = None,
    provider: str = "deepseek",
    base_url: str | None = None,
) -> dict[str, Any]:
    """Parse source structure using deterministic, paragraph, or LLM mode."""
    if mode == "paragraph":
        return parse_structure_paragraphs(extracted, document_type=document_type)
    if mode == "deterministic":
        return parse_structure_deterministic(extracted, document_type=document_type)
    if mode != "llm":
        raise ValueError(f"Unknown parser mode: {mode}")

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        selected_base_url = base_url or "https://api.deepseek.com"
        selected_model = model or "deepseek-chat"
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        selected_base_url = base_url
        selected_model = model or "gpt-4o"
    elif provider == "local":
        api_key = os.getenv("LOCAL_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "local"
        selected_base_url = base_url or os.getenv("LOCAL_LLM_BASE_URL") or "http://100.79.90.123:8000/v1"
        selected_model = model or os.getenv("LOCAL_LLM_MODEL") or "local-model"
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")

    if not api_key:
        raise RuntimeError(f"Missing API key for provider: {provider}")
    return parse_structure_with_llm(extracted, api_key=api_key, model=selected_model, base_url=selected_base_url)


def load_extracted(source_dir: Path) -> dict[str, Any]:
    extracted_path = source_dir / "extracted_text.json"
    return json.loads(extracted_path.read_text(encoding="utf-8"))
