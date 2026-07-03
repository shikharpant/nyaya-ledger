"""Compile deterministic legal amendment events from CBIC notification records."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from src.anchor_resolver import AnchorNotFoundError, resolve_anchor

from .identity_registry import load_registry
from .omlx_client import OmlxConfig, OmlxError, chat_json
from .renderer import canonical_form_id, canonical_rule_id, canonicalize_legacy_reference
from .source_archive import extract_source_text, read_metadata_yaml


COMPILER_VERSION = "amendment-events-v1"
DEFAULT_OBSERVED_AT = "2026-06-16T00:00:00Z"
SUPPORTED_MATERIALIZER_OPS = {"INSERT_SIBLING", "INSERT_CHILD", "SPLICE", "SUBSTITUTE", "OMIT"}


@dataclass(frozen=True)
class SourceRecord:
    record: dict[str, Any]
    json_path: Path
    text: str
    text_source: str
    source_file_sha256: str
    source_text_sha256: str
    source_url: str
    document_id: str
    publication_date: str
    commencement_date: str | None
    date_basis: str


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def category_slug(value: str) -> str:
    slug = value.lower().replace("&", "and")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def instrument_parts(record: dict[str, Any]) -> tuple[str, str, str]:
    instrument = str(record.get("no") or record.get("number") or "")
    match = re.search(r"(\d+)\s*/\s*(\d{4})", instrument)
    if match:
        return instrument, str(int(match.group(1))), match.group(2)
    issue_date = parse_record_date(record)
    year = issue_date[:4] if issue_date else "undated"
    number_match = re.search(r"\b(\d+)\b", instrument)
    number = str(int(number_match.group(1))) if number_match else str(record.get("id", "unknown"))
    return instrument, number, year


def notification_document_id(record: dict[str, Any]) -> str:
    instrument, number, year = instrument_parts(record)
    category = category_slug(str(record.get("category") or "unknown"))
    suffix = f"{number}-{year}" if number and year else str(record.get("id", "unknown"))
    if "corrigendum" in instrument.lower() or str(record.get("name", "")).lower().startswith("corrigendum"):
        suffix = f"corrigendum-{record.get('id', suffix)}"
    return f"/in/union/notifications/cbic/{category}/{year}/{suffix}"


def parse_record_date(record: dict[str, Any]) -> str:
    value = str(record.get("issueDt") or record.get("publication_date") or "")
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    return match.group(0) if match else ""


def _clean_date_text(value: str) -> str:
    value = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE)
    value = re.sub(r"\bday\s+of\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .")


def parse_legal_date(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    match = re.search(
        r"(?:come|comes|shall come)\s+into\s+force\s+"
        r"(?:(?:on|from)(?:\s+the)?|with\s+effect\s+from(?:\s+the)?)\s+(.+?)(?=[.;])",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = _clean_date_text(match.group(1))
    if not re.search(r"\b\d{4}\b", value) or not re.search(r"\b[A-Za-z]{3,}\b", value):
        return None
    try:
        from dateutil import parser as date_parser

        return date_parser.parse(value, dayfirst=True, fuzzy=True).date().isoformat()
    except Exception:
        return None


def _archive_path_for_record(record: dict[str, Any], archive_root: Path) -> Path | None:
    _instrument, number, year = instrument_parts(record)
    category = category_slug(str(record.get("category") or "unknown"))
    path = archive_root / "cbic" / category / year / f"{number}-{year}"
    return path if path.exists() else None


def _extract_pdf_base64_text(record: dict[str, Any]) -> str:
    payload = record.get("contentPdfBase64")
    if not payload:
        return ""
    try:
        import pdfplumber

        data = base64.b64decode(payload)
        pages = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text(x_tolerance=1, y_tolerance=3) or "")
        return "\n\n".join(page.strip() for page in pages if page.strip())
    except Exception:
        return ""


def load_source_record(
    json_path: Path,
    *,
    source_archive_root: Path = Path("sources"),
    extract_pdf_text: bool = False,
) -> SourceRecord:
    record = json.loads(json_path.read_text(encoding="utf-8"))
    content_text = str(record.get("contentText") or "").strip()
    source_url = str(record.get("source_url") or "")
    source_file_sha = sha256_file(json_path)
    text_source = "contentText"
    text = content_text

    archive_path = _archive_path_for_record(record, source_archive_root)
    if not text and archive_path:
        metadata = read_metadata_yaml(archive_path / "metadata.yaml")
        extracted = extract_source_text(archive_path)
        text = str(extracted.get("text") or "")
        source_file_sha = str(metadata.get("source_sha256") or source_file_sha)
        source_url = str(metadata.get("source_url") or source_url)
        text_source = "source_archive"

    if not text and extract_pdf_text:
        text = _extract_pdf_base64_text(record)
        text_source = "contentPdfBase64" if text else "metadata"

    if not text:
        text = str(record.get("name") or record.get("no") or "")
        text_source = "metadata"

    publication_date = parse_record_date(record)
    commencement_date = parse_legal_date(text)
    return SourceRecord(
        record=record,
        json_path=json_path,
        text=text,
        text_source=text_source,
        source_file_sha256=source_file_sha,
        source_text_sha256=sha256_text(text),
        source_url=source_url,
        document_id=notification_document_id(record),
        publication_date=publication_date,
        commencement_date=commencement_date or publication_date or None,
        date_basis="explicit_commencement_clause" if commencement_date else "publication_date_fallback",
    )


def iter_cbic_json(source_dir: Path, category: str | None = None) -> Iterable[Path]:
    for path in sorted(source_dir.glob("*.json")):
        if category:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if str(record.get("category") or "") != category:
                continue
        yield path


def numbered_blocks(text: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(r"(?m)^\s*(\d+)\.\s+", text))
    blocks: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        blocks.append({"label": match.group(1), "start": start, "end": end, "text": block})
    if not blocks and text.strip():
        blocks.append({"label": "1", "start": 0, "end": len(text), "text": text.strip()})
    return blocks


def _looks_like_amendment_instruction(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:in|for|after)\s+(?:the\s+)?(?:said\s+|principal\s+)?(?:rules?|rule|FORM|sub-rule|section|clause|proviso)\b"
            r"|\brule\s+\d+[A-Z]?\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _compound_clause_starts(block_text: str) -> list[int]:
    starts: list[int] = []
    for match in re.finditer(r"(?<!\w)\(([a-z]|[ivxlcdm]+)\)\s+", block_text, flags=re.IGNORECASE):
        suffix = block_text[match.end() : match.end() + 180]
        prefix = block_text[: match.start()]
        bare_word_edit = bool(
            re.search(
                r"^(?:\s*in\s+the\s+\w+\s+proviso,)?\s*(?:for|after|before)\s+the\s+(?:words?|figures?|letters?)\b",
                suffix,
                flags=re.IGNORECASE,
            )
        )
        has_parent_rule_context = bool(
            re.search(r"\bin\s+rule\s+\d+[A-Z]?", prefix, flags=re.IGNORECASE)
            and re.search(r"\bin\s+sub[\s-]+rule\s+\(", prefix, flags=re.IGNORECASE)
        )
        if _looks_like_amendment_instruction(suffix) or (bare_word_edit and has_parent_rule_context):
            starts.append(match.start())
    return starts


def _contextualized_segment_text(
    segment_text: str,
    context_rule: str | None,
    context_subrule: str | None = None,
) -> str:
    if not context_rule:
        return segment_text
    prefix = r"^(\(?[a-zivxlcdm]+\)?\s*,?\s*)"
    if re.search(prefix + r"in\s+sub-rule\b", segment_text, flags=re.IGNORECASE):
        return re.sub(
            prefix + r"in\s+sub-rule\b",
            rf"\1in rule {context_rule}, in sub-rule",
            segment_text,
            count=1,
            flags=re.IGNORECASE,
        )
    if context_subrule and re.search(
        prefix + r"(?:in\s+the\s+\w+\s+proviso,?\s+)?(?:for|after|before)\s+the\s+(?:words?|figures?|letters?)\b",
        segment_text,
        flags=re.IGNORECASE,
    ):
        return re.sub(
            prefix,
            rf"\1in rule {context_rule}, in sub-rule ({context_subrule}), ",
            segment_text,
            count=1,
            flags=re.IGNORECASE,
        )
    if re.search(prefix + r"(?:after|before|for)\s+sub-rule\b", segment_text, flags=re.IGNORECASE):
        return re.sub(
            prefix,
            rf"\1in rule {context_rule}, ",
            segment_text,
            count=1,
            flags=re.IGNORECASE,
        )
    if not re.search(r"\bin\s+rule\s+\d+[A-Z]?\b", segment_text, flags=re.IGNORECASE):
        amendment_fragment = re.search(
            r"\b(?:shall\s+be\s+(?:inserted|substituted|omitted)|for\s+(?:the\s+)?(?:words|letters|figures?)|"
            r"the\s+following\s+(?:clause|proviso|rule|sub-rule|statement|explanation|table)|"
            r"in\s+sub-rule\b|after\s+the\s+words)\b",
            segment_text,
            flags=re.IGNORECASE,
        )
        if amendment_fragment:
            context_prefix = f"in rule {context_rule}"
            if context_subrule:
                context_prefix += f", in sub-rule ({context_subrule})"
            return f"{context_prefix}, {segment_text}"
    return segment_text


def split_compound_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Split numbered CBIC amendment blocks into top-level instruction spans.

    The splitter is deliberately conservative. It only splits on enumerators
    like ``(i)`` or ``(a)`` when the following text starts like a fresh
    amendment instruction. If a block cannot be split safely, the original
    block is returned and the validation gate will keep compound materialization
    in review.
    """

    block_text = str(block["text"])
    if _span_amendment_instruction_count(block_text) <= 1:
        return [block]
    starts = _compound_clause_starts(block_text)
    if len(starts) < 2:
        return [block]

    segments: list[dict[str, Any]] = []
    block_start = int(block["start"])
    context_rule = block.get("context_rule")
    context_subrule = block.get("context_subrule")
    if not context_rule:
        context_match = re.search(r"\bin\s+rule\s+(\d+[A-Z]?)\b", block_text[: starts[0]], flags=re.IGNORECASE)
        if context_match:
            context_rule = context_match.group(1)
    if not context_subrule:
        context_subrule_match = re.search(
            r"\bin\s+rule\s+\d+[A-Z]?,?\s+in\s+sub[\s-]+rule\s+\((\d+[A-Z]?)\)",
            block_text[: starts[0]],
            flags=re.IGNORECASE,
        )
        if not context_subrule_match:
            context_subrule_match = re.search(
                r"\bin\s+sub[\s-]+rule\s+\((\d+[A-Z]?)\)\s+of\s+rule\s+\d+[A-Z]?",
                block_text[: starts[0]],
                flags=re.IGNORECASE,
            )
        if context_subrule_match:
            context_subrule = context_subrule_match.group(1)
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(block_text)
        segment_text = block_text[start:end].strip()
        if not segment_text:
            continue
        leading_ws = len(block_text[start:end]) - len(block_text[start:end].lstrip())
        trailing_ws = len(block_text[start:end]) - len(block_text[start:end].rstrip())
        segment_start = block_start + start + leading_ws
        segment_end = block_start + end - trailing_ws
        segment = {
            "label": f"{block.get('label', '')}.{index + 1}",
            "start": segment_start,
            "end": segment_end,
            "text": segment_text,
            "parent_start": block["start"],
            "parent_end": block["end"],
        }
        if context_rule:
            segment["context_rule"] = context_rule
            if context_subrule:
                segment["context_subrule"] = context_subrule
            contextualized = _contextualized_segment_text(
                segment_text,
                str(context_rule),
                str(context_subrule) if context_subrule else None,
            )
            if contextualized != segment_text:
                segment["parse_text"] = contextualized
        segments.append(segment)

    if len(segments) < 2:
        return [block]
    expanded: list[dict[str, Any]] = []
    for segment in segments:
        children = split_compound_block(segment)
        expanded.extend(children)
    return expanded


def amendment_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in numbered_blocks(text):
        blocks.extend(split_compound_block(block))
    return blocks


def _rule_text_parts(value: str) -> tuple[str, str]:
    value = re.sub(r"\s+", " ", value).strip().strip('"')
    match = re.match(r"([0-9A-Za-z]+)\.\s*(.+)", value)
    if match:
        value = match.group(2).strip()
    heading_match = re.match(r"(.+?)(?:\.-|-\.|-|\. )\s*(.+)", value)
    if heading_match:
        return heading_match.group(1).strip(), heading_match.group(2).strip()
    return "", value


def _component_for_rule(rule_label: str) -> str:
    return canonical_rule_id(rule_label.lower())


def _component_for_rule_subrule(rule_label: str, subrule_label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", subrule_label).lower()
    return f"{_component_for_rule(rule_label)}/subrule/{clean}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_xml_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _xml_element_text(element: ET.Element) -> str:
    lines: list[str] = []
    for child in element.iter():
        if _local_name(child.tag) not in {"num", "heading", "p"}:
            continue
        text = _clean_xml_text("".join(child.itertext()))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _xml_properties(root: ET.Element) -> dict[str, str]:
    props = {}
    for element in root.iter():
        if _local_name(element.tag) == "property" and element.attrib.get("name"):
            props[element.attrib["name"]] = element.attrib.get("value", "")
    return props


def _build_lookup_from_xml_paths(paths: list[Path], target_work: str, base_dir: Path) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    lookup[target_work] = {
        "canonical_id": target_work,
        "roles": ["document"],
        "document": {"canonical_id": target_work, "path": str(base_dir), "text": "", "document_type": "rules"},
    }

    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        props = _xml_properties(root)
        document_id = props.get("canonical_id")
        if document_id:
            lookup.setdefault(document_id, {"canonical_id": document_id, "roles": []})
            lookup[document_id]["document"] = {
                "canonical_id": document_id,
                "path": str(path),
                "text": _xml_element_text(root),
                "document_type": props.get("document_type", ""),
                "title": props.get("title", ""),
            }
        for element in root.iter():
            provision_id = element.attrib.get("refersTo")
            if not provision_id:
                continue
            lookup.setdefault(provision_id, {"canonical_id": provision_id, "roles": []})
            lookup[provision_id]["provision"] = {
                "canonical_id": provision_id,
                "document_id": document_id,
                "path": str(path),
                "element_tag": _local_name(element.tag),
                "text": _xml_element_text(element),
            }
    return lookup


def build_target_corpus_lookup(corpus_dir: Path, target_work: str) -> dict[str, dict[str, Any]]:
    """Build a fast lookup for v1 target work files.

    Discrete rule files are preferred over a bundled ``rules.xml`` wrapper so
    event materialization starts from the curated base components.
    """
    base_dir = corpus_dir / Path(target_work.strip("/"))
    paths = sorted(base_dir.rglob("*.xml")) if base_dir.exists() else []
    discrete_paths = [path for path in paths if path.name != "rules.xml"]
    if discrete_paths:
        paths = discrete_paths
    forms_dir = corpus_dir / "in/union/forms"
    if forms_dir.exists():
        paths.extend(sorted(forms_dir.rglob("*.xml")))
    return _build_lookup_from_xml_paths(paths, target_work, base_dir)


def build_target_baseline_lookup(baseline_dir: Path, target_work: str) -> dict[str, dict[str, Any]]:
    paths = []
    if (baseline_dir / "baseline.xml").exists():
        paths = [baseline_dir / "baseline.xml"]
    elif baseline_dir.exists():
        paths = sorted(baseline_dir.rglob("*.xml"))
    return _build_lookup_from_xml_paths(paths, target_work, baseline_dir)


def _is_target_work_amendment_document(source: SourceRecord) -> bool:
    combined = f"{source.record.get('name') or ''}\n{source.text}".lower()
    mentions_rules = "cgst rules" in combined or "central goods and services tax rules" in combined
    amendment_context = (
        "amend" in combined
        or "further to amend" in combined
        or "following rules" in combined
        or "said rules" in combined
    )
    return mentions_rules and amendment_context


def _is_principal_rules_notification(source: SourceRecord, target_work: str) -> bool:
    if target_work != "/in/union/rules/cgst-rules-2017":
        return False
    instrument = str(source.record.get("no") or source.record.get("number") or "")
    name = str(source.record.get("name") or "")
    combined = f"{instrument}\n{name}\n{source.text}".lower()
    _raw_instrument, number, year = instrument_parts(source.record)
    explicit_principal_id = number == "3" and year == "2017" and "central tax" in instrument.lower()
    principal_title = bool(
        re.search(
            r"\bnotif(?:y|ying)\b.*\b(?:cgst|central goods and services tax)\s+rules\b",
            name,
            flags=re.IGNORECASE,
        )
    )
    rule_making_body = (
        "central goods and services tax rules, 2017" in combined
        and "hereby makes the following rules" in combined
        and "short title, commencement and application" in combined
    )
    return explicit_principal_id or (principal_title and rule_making_body)


def _principal_rules_baseline_event(source: SourceRecord, target_work: str) -> dict[str, Any]:
    span = (0, len(source.text))
    span_text = source.text
    event_id = stable_event_id(source, span, "cbic")
    return {
        "event_id": event_id,
        "legacy_event_id": f"{_event_prefix(source)}_baseline",
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "COMMENCE",
        "source": _source_payload(source),
        "legal_time": _legal_time(source),
        "system_time": {
            "observed_at": DEFAULT_OBSERVED_AT,
            "compiled_at": DEFAULT_OBSERVED_AT,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": target_work,
            "component_id": target_work,
            "anchor_component_id": None,
            "anchor_text": None,
            "anchor_hash": None,
            "anchor_occurrence": None,
        },
        "payload": {
            "baseline_source_only": True,
            "description": "Principal notification that made the CGST Rules, 2017; represented by the 2017 baseline, not replayed as amendments.",
        },
        "evidence": {
            "source_span": {
                "start": span[0],
                "end": span[1],
                "text_hash": sha256_text(span_text),
            },
            "excerpt": re.sub(r"\s+", " ", span_text).strip()[:500],
            "parser_trace": {"pattern_id": "principal_rules_baseline_notification_v1", "confidence": 1.0},
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": bool(source.commencement_date),
            "source_span_verified": True,
            "materializable": False,
        },
        "status": "rejected",
        "review": {
            "required": False,
            "review_reasons": [],
            "reviewed_by": "compiler",
            "reviewed_at": DEFAULT_OBSERVED_AT,
        },
    }


def _is_commencement_block(block_text: str) -> bool:
    return bool(
        re.search(r"^\s*1\.\s*(?:\(\s*1\s*\))?\s*these\s+rules\b", block_text, flags=re.IGNORECASE | re.DOTALL)
    )


def _unparsed_candidate_component(block_text: str, target_work: str) -> str:
    rule_match = re.search(r"\b(?:in|after|before)\s+rule\s+(\d+[A-Z]?)\b", block_text, flags=re.IGNORECASE)
    if rule_match:
        return _component_for_rule(rule_match.group(1))
    rule_match = re.search(r"\brule\s+(\d+[A-Z]?)\b", block_text, flags=re.IGNORECASE)
    if rule_match:
        return _component_for_rule(rule_match.group(1))
    form_match = re.search(r"\bFORM\s+GST\s+([A-Z0-9-]+)", block_text, flags=re.IGNORECASE)
    if form_match:
        return canonical_form_id("GST " + form_match.group(1).upper())
    return target_work


def _looks_like_unparsed_target_block(block_text: str) -> bool:
    lowered = block_text.lower()
    signals = [
        "said rules",
        " in rule ",
        " after rule ",
        " before rule ",
        "form gst",
        "shall be inserted",
        "shall be substituted",
        "shall be omitted",
        "table",
        "serial number",
        "schedule",
        "annexure",
    ]
    return any(signal in lowered for signal in signals)


def _source_payload(source: SourceRecord) -> dict[str, Any]:
    instrument = str(source.record.get("no") or "")
    return {
        "document_id": source.document_id,
        "record_id": str(source.record.get("id") or ""),
        "instrument_number": instrument,
        "issuing_authority": "/in/authority/cbic",
        "publication_date": source.publication_date,
        "source_url": source.source_url,
        "source_file_sha256": source.source_file_sha256,
        "source_text_sha256": source.source_text_sha256,
        "text_source": source.text_source,
    }


def _legal_time(source: SourceRecord) -> dict[str, Any]:
    date_value = source.commencement_date
    return {
        "commencement_date": date_value,
        "applicability_start": date_value,
        "applicability_end": None,
        "retrospective": bool(source.publication_date and date_value and date_value < source.publication_date),
        "date_basis": source.date_basis,
    }


def _event_prefix(source: SourceRecord) -> str:
    _instrument, number, year = instrument_parts(source.record)
    category = category_slug(str(source.record.get("category") or "unknown")).replace("-", "_")
    return f"evt_cbic_{category}_{year}_{number}"


def stable_event_id(source: SourceRecord, source_span: tuple[int, int], source_family: str = "cbic") -> str:
    start, end = source_span
    span_text = source.text[start:end]
    text_hash = sha256_text(span_text)
    seed = "|".join([source.document_id, str(start), str(end), text_hash])
    return f"evt_{source_family}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _span_requires_future_commencement(span_text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:with\s+effect\s+from\s+)?(?:a\s+)?date\s+to\s+be\s+notified\b",
            span_text,
            flags=re.IGNORECASE,
        )
    )


def _span_amendment_instruction_count(span_text: str) -> int:
    return len(
        re.findall(
            r"\bshall\s+be\s+(?:inserted|substituted|omitted)\b",
            span_text,
            flags=re.IGNORECASE,
        )
    )


def _canonical_anchor_text(provision_text: str, anchor_text: str) -> str:
    """Return an exact provision substring for whitespace-normalized anchors."""

    cleaned_anchor = re.sub(r"\s+", " ", anchor_text or "").strip()
    if not cleaned_anchor:
        return anchor_text
    if cleaned_anchor in provision_text:
        return cleaned_anchor
    pattern = re.escape(cleaned_anchor).replace(r"\ ", r"\s+")
    match = re.search(pattern, provision_text)
    if match:
        return provision_text[match.start() : match.end()]
    return cleaned_anchor


def _quoted_chunks(value: str) -> list[str]:
    chunks = re.findall(r"['\"\u201c\u201d\u2015\u2016]\s*(.+?)\s*['\"\u201c\u201d\u2015\u2016]", value, flags=re.DOTALL)
    return [re.sub(r"\s+", " ", chunk).strip() for chunk in chunks if re.sub(r"\s+", " ", chunk).strip()]


def _text_after_namely(value: str) -> str:
    match = re.search(r"(?:namely|namely:-|namely:|namely-)\s*[:\-\u2013\u2014]?\s*(.+)", value, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = match.group(1).strip()
    text = re.sub(r"\[[^\]]*\]\s*$", "", text).strip()
    return text.strip(" \t\r\n'\"\u201c\u201d")


def _structural_substitute_payload(span_text: str, component_id: str) -> tuple[str, dict[str, Any]]:
    text = re.sub(r"\s+", " ", span_text).strip()
    form_match = re.search(r"\bfor\s+FORM\s+((?:GST\s+)?[A-Z0-9-]+)\b", text, flags=re.IGNORECASE)
    if form_match:
        structural_text = _text_after_namely(text)
        if not structural_text:
            chunks = _quoted_chunks(text)
            structural_text = chunks[-1] if chunks else ""
        return canonical_form_id("FORM " + form_match.group(1).upper()), {
            "node_type": "form",
            "label": "FORM " + form_match.group(1).upper(),
            "structural_text": structural_text,
        }
    rule_match = re.search(r"\bfor\s+rule\s+(\d+[A-Z]?)\b", text, flags=re.IGNORECASE)
    subrule_match = re.search(r"\bfor\s+sub-rule\s+\(([^)]+)\)", text, flags=re.IGNORECASE)
    explanation_match = re.search(r"\bfor\s+(Explanation\s+\d*|Explanation)\b", text, flags=re.IGNORECASE)
    structural_text = _text_after_namely(text)
    if not structural_text:
        chunks = _quoted_chunks(text)
        structural_text = chunks[-1] if chunks else ""
    payload: dict[str, Any] = {"structural_text": structural_text}
    if rule_match:
        label = rule_match.group(1)
        payload.update({"node_type": "rule", "label": label})
        return _component_for_rule(label), payload
    if subrule_match:
        label = re.sub(r"[^0-9A-Za-z]+", "", subrule_match.group(1)).lower()
        parent = component_id.split("/subrule/", 1)[0]
        if "/rule/" in parent:
            payload.update({"node_type": "subrule", "label": label, "parent_component_id": parent})
            return f"{parent}/subrule/{label}", payload
    if explanation_match:
        payload.update({"node_type": "explanation", "label": explanation_match.group(1)})
    return component_id, payload


def _partial_omit_payload(span_text: str) -> dict[str, Any]:
    match = re.search(
        r"\b(?:the\s+)?(?:words?|letters?|figures?|word,?\s+figures?\s+and\s+letters?|figures?,?\s+letters?\s+and\s+word)[^'\"\u201c\u201d\u2015\u2016]*['\"\u201c\u201d\u2015\u2016]\s*(.+?)\s*['\"\u201c\u201d\u2015\u2016][^.;]*?\bshall\s+be\s+omitted",
        span_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return {"omit_text": re.sub(r"\s+", " ", match.group(1)).strip(), "whole_component": False}
    return {"whole_component": False}


def _insert_child_payload_from_span(span_text: str, component_id: str) -> tuple[str, str | None, dict[str, Any]]:
    text = re.sub(r"\s+", " ", span_text).strip()
    parent = component_id.split("/subrule/", 1)[0] if "/subrule/" in component_id else component_id
    rule_match = re.search(r"\bin\s+rule\s+(\d+[A-Z]?)\b", text, flags=re.IGNORECASE)
    if rule_match:
        parent = _component_for_rule(rule_match.group(1))
    anchor_match = re.search(r"\bafter\s+sub-rule\s+\(([^)]+)\)", text, flags=re.IGNORECASE)
    anchor_component = None
    if anchor_match and "/rule/" in parent:
        anchor_label = re.sub(r"[^0-9A-Za-z]+", "", anchor_match.group(1)).lower()
        anchor_component = f"{parent}/subrule/{anchor_label}"
    inserted = _text_after_namely(text)
    label = ""
    content = inserted
    child_match = re.match(r"\(?([0-9A-Za-z]+)\)?\s*(.+)", inserted, flags=re.DOTALL)
    if child_match:
        label = re.sub(r"[^0-9A-Za-z]+", "", child_match.group(1)).lower()
        content = child_match.group(2).strip()
    target_component = f"{parent}/subrule/{label}" if label and "/rule/" in parent else component_id
    return target_component, anchor_component, {
        "node_type": "subrule",
        "label": label,
        "content": content,
        "position": "after" if anchor_component else "append",
        "anchor_component_id": anchor_component,
        "parent_component_id": parent,
    }


def _make_event(
    *,
    source: SourceRecord,
    sequence: int,
    operation: str,
    target_work: str,
    component_id: str,
    source_span: tuple[int, int],
    excerpt: str,
    pattern_id: str,
    payload: dict[str, Any],
    corpus_lookup: dict[str, dict[str, Any]],
    anchor_text: str | None = None,
    anchor_component_id: str | None = None,
    confidence: float = 0.92,
    initial_reasons: list[str] | None = None,
    parser_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons = list(initial_reasons or [])
    start, end = source_span
    span_text = source.text[start:end]
    source_span_verified = span_text.strip() in source.text
    date_resolved = bool(source.commencement_date) and not _span_requires_future_commencement(span_text)

    target_id_for_resolution = anchor_component_id if operation in {"INSERT_SIBLING", "INSERT_CHILD"} else component_id
    target_resolved = bool(corpus_lookup.get(target_id_for_resolution))
    target_inside_work = component_id == target_work or component_id.startswith(target_work + "/")
    document_scope_target = component_id == target_work and operation in {"SPLICE", "SUBSTITUTE", "OMIT"}
    structural_substitution_payload = operation == "SUBSTITUTE" and bool(
        re.search(
            r"\bfor\s+(?:FORM|rule|sub-rule|Explanation)\b|for\s+the\s+following\s+FORM\b",
            span_text,
            flags=re.IGNORECASE,
        )
    )
    incomplete_text_payload = (
        operation == "SPLICE"
        and not str(payload.get("insert_text") or "").strip()
        or operation == "SUBSTITUTE"
        and not structural_substitution_payload
        and (
            not str(payload.get("old_text") or "").strip()
            or not str(payload.get("new_text") or "").strip()
        )
    )
    structural_substitution_missing = structural_substitution_payload and not str(payload.get("structural_text") or "").strip()
    structural_text = str(payload.get("structural_text") or "").strip()
    structural_label = str(payload.get("label") or "").strip()
    structural_label_missing = structural_substitution_payload and bool(
        structural_label
        and str(payload.get("node_type") or "") in {"rule", "subrule"}
        and structural_label.lower() not in structural_text.lower()[:80]
    )
    compound_multiple_amendments = _span_amendment_instruction_count(span_text) > 1
    compound_unsupported_omission = operation != "OMIT" and bool(
        re.search(r"\b(?:proviso|word|words|letters?|figures?|clause)\b.*?\bshall\s+be\s+omitted", span_text, flags=re.IGNORECASE | re.DOTALL)
    )
    partial_omit_payload = operation == "OMIT" and bool(
        re.search(r"\b(?:proviso|word|words|letters?|figures?|clause)\b.*?\bshall\s+be\s+omitted", span_text, flags=re.IGNORECASE | re.DOTALL)
    )
    ambiguous_omit_instruction = operation == "OMIT" and bool(
        re.search(r"\b(?:after|before)\s+the\s+words?\b.*?\bshall\s+be\s+omitted", span_text, flags=re.IGNORECASE | re.DOTALL)
    )
    omit_phrase_present = operation != "OMIT" or bool(re.search(r"\bshall\s+be\s+omitted\b", span_text, flags=re.IGNORECASE))
    partial_omit_missing = partial_omit_payload and not str(payload.get("omit_text") or "").strip()
    inserted_component_exists = operation in {"INSERT_SIBLING", "INSERT_CHILD"} and component_id in corpus_lookup
    if inserted_component_exists:
        reasons.append("inserted_component_already_exists")
    if not target_inside_work:
        reasons.append("target_component_outside_work")
    if incomplete_text_payload:
        reasons.append("incomplete_text_edit_payload")
    if structural_substitution_missing:
        reasons.append("structural_substitution_requires_component_payload")
    if structural_label_missing:
        reasons.append("structural_substitution_label_not_verified")
    if partial_omit_missing:
        reasons.append("partial_omit_requires_precise_delete_payload")
    if ambiguous_omit_instruction:
        reasons.append("omit_instruction_ambiguous")
    if not omit_phrase_present:
        reasons.append("omit_phrase_not_found")
    unsafe_substitute_anchor = operation == "SUBSTITUTE" and bool(
        re.fullmatch(r"\(?[ivxlcdm0-9a-z]+\)?", str(payload.get("old_text") or "").strip(), flags=re.IGNORECASE)
    )
    if unsafe_substitute_anchor:
        reasons.append("unsafe_generic_substitution_anchor")
    if document_scope_target:
        reasons.append("document_scope_target_not_materializable")
    if compound_multiple_amendments:
        reasons.append("compound_block_contains_multiple_amendments")
    if compound_unsupported_omission:
        reasons.append("compound_block_contains_unsupported_omission")

    anchor_resolved = True
    anchor_occurrence: int | None = None
    if anchor_text:
        anchor_resolved = False
        entry = corpus_lookup.get(component_id) or {}
        provision_text = (entry.get("provision") or entry.get("document") or {}).get("text", "")
        anchor_text = _canonical_anchor_text(provision_text, anchor_text)
        if provision_text:
            try:
                match = resolve_anchor(provision_text, anchor_text, component_id)
                anchor_resolved = True
                anchor_occurrence = provision_text[: match.position].count(anchor_text) + 1
            except AnchorNotFoundError:
                anchor_resolved = False
    if not target_resolved:
        reasons.append("target_not_resolved")
    if not anchor_resolved:
        reasons.append("anchor_not_resolved")
    if not date_resolved:
        reasons.append("date_not_resolved")
    if not source_span_verified:
        reasons.append("source_span_not_verified")
    if operation not in SUPPORTED_MATERIALIZER_OPS:
        reasons.append("unsupported_materializer_operation")

    materializable = (
        operation in SUPPORTED_MATERIALIZER_OPS
        and target_resolved
        and target_inside_work
        and anchor_resolved
        and date_resolved
        and source_span_verified
        and not inserted_component_exists
        and not incomplete_text_payload
        and not structural_substitution_missing
        and not structural_label_missing
        and not partial_omit_missing
        and not ambiguous_omit_instruction
        and omit_phrase_present
        and not unsafe_substitute_anchor
        and not document_scope_target
        and not compound_multiple_amendments
        and not compound_unsupported_omission
    )
    status = "validated" if materializable else "needs_review"
    event_id = stable_event_id(source, source_span, "cbic")
    legacy_event_id = f"{_event_prefix(source)}_{sequence:04d}"
    now = DEFAULT_OBSERVED_AT
    parser_trace = {"pattern_id": pattern_id, "confidence": confidence}
    if parser_context:
        parser_trace.update(parser_context)
    return {
        "event_id": event_id,
        "legacy_event_id": legacy_event_id,
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": operation,
        "source": _source_payload(source),
        "legal_time": _legal_time(source),
        "system_time": {
            "observed_at": now,
            "compiled_at": now,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": target_work,
            "component_id": component_id,
            "anchor_component_id": anchor_component_id,
            "anchor_text": anchor_text,
            "anchor_hash": sha256_text(anchor_text) if anchor_text else None,
            "anchor_occurrence": anchor_occurrence,
        },
        "payload": payload,
        "evidence": {
            "source_span": {
                "start": start,
                "end": end,
                "text_hash": sha256_text(span_text),
            },
            "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:500],
            "parser_trace": parser_trace,
        },
        "validation": {
            "target_resolved": target_resolved,
            "anchor_resolved": anchor_resolved,
            "date_resolved": date_resolved,
            "source_span_verified": source_span_verified,
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


def _quoted_rule_text(block: str) -> str:
    match = re.search(r"['\"\u201c]\s*(\d+[A-Z]?\.\s*.+?)\s*['\"\u201d]", block, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r"namely\s*[:-]\s*(.+)", block, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def compile_events_from_text(
    source: SourceRecord,
    *,
    target_work: str,
    corpus_lookup: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    sequence = 1
    text_lower = source.text.lower()
    target_work_amendment = _is_target_work_amendment_document(source)
    if _is_principal_rules_notification(source, target_work):
        return [_principal_rules_baseline_event(source, target_work)]
    if source.text_source == "metadata" and any(term in text_lower for term in ["cgst rule", "goods and services tax rule"]):
        events.append(
            _make_event(
                source=source,
                sequence=sequence,
                operation="UNKNOWN",
                target_work=target_work,
                component_id=target_work,
                source_span=(0, len(source.text)),
                excerpt=source.text,
                pattern_id="metadata_only_rule_reference_v1",
                payload={"description": source.text},
                corpus_lookup=corpus_lookup,
                confidence=0.2,
                initial_reasons=["missing_source_text"],
            )
        )
        return events

    for block in amendment_blocks(source.text):
        block_text = block["text"]
        parse_text = block.get("parse_text", block_text)
        block_lower = parse_text.lower()
        span = (int(block["start"]), int(block["end"]))
        parser_context = {}
        if block.get("context_rule"):
            parser_context["context_rule"] = str(block["context_rule"])
            parser_context["context_inherited"] = True
        if block.get("context_subrule"):
            parser_context["context_subrule"] = str(block["context_subrule"])
        if parse_text != block_text:
            parser_context["context_parse_text"] = re.sub(r"\s+", " ", str(parse_text)).strip()[:500]
        if target_work_amendment and _is_commencement_block(block_text):
            continue

        if "corrigendum" in block_lower or str(source.record.get("name", "")).lower().startswith("corrigendum"):
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="CORRIGENDUM",
                    target_work=target_work,
                    component_id=target_work,
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="corrigendum_v1",
                    payload={"text": block_text},
                    corpus_lookup=corpus_lookup,
                    confidence=0.5,
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        insert_match = re.search(
            r"after\s+rule\s+(\d+[A-Z]?)(?:\s+of\s+the\s+said\s+rules)?,?\s+"
            r"the\s+following\s+rule\s+shall\s+be\s+inserted",
            parse_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if insert_match:
            after_rule = insert_match.group(1)
            quoted = _quoted_rule_text(parse_text)
            label_match = re.match(r"\s*(\d+[A-Z]?)\.", quoted)
            new_label = label_match.group(1) if label_match else ""
            heading, content = _rule_text_parts(quoted)
            component_id = _component_for_rule(new_label or after_rule)
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="INSERT_SIBLING",
                    target_work=target_work,
                    component_id=component_id,
                    anchor_component_id=_component_for_rule(after_rule),
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="insert_sibling_rule_v1",
                    payload={
                        "node_type": "rule",
                        "label": new_label,
                        "heading": heading,
                        "content": content,
                        "position": "after",
                        "anchor_rule": after_rule,
                    },
                    corpus_lookup=corpus_lookup,
                    confidence=0.95,
                    initial_reasons=[] if new_label else ["inserted_rule_label_not_found"],
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        whole_rule_sub_match = re.search(
            r"for\s+rule\s+(\d+[A-Z]?),?\s+"
            r"the\s+following\s+(?:rule\s+)?shall\s+be\s+substituted\s*,?\s*"
            r"namely\s*:\s*[-\u2013\u2014\u2018\u201c\u201e\"'\s]*",
            parse_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if whole_rule_sub_match:
            rule_label = whole_rule_sub_match.group(1)
            after_namely = parse_text[whole_rule_sub_match.end():]
            quoted = _quoted_rule_text(after_namely)
            if not quoted or len(quoted) < 10:
                quoted = re.split(r'["\u201d]\s*[;)]', after_namely)[0].strip().rstrip('"\u201d')
            if quoted and len(quoted) >= 10:
                heading, content = _rule_text_parts(quoted)
                events.append(
                    _make_event(
                        source=source,
                        sequence=sequence,
                        operation="SUBSTITUTE",
                        target_work=target_work,
                        component_id=_component_for_rule(rule_label),
                        source_span=span,
                        excerpt=block_text,
                        pattern_id="whole_rule_substitution_v1",
                        payload={"structural_text": quoted, "structural_heading": heading},
                        corpus_lookup=corpus_lookup,
                        confidence=0.95,
                        parser_context=parser_context,
                    )
                )
                sequence += 1
                continue

        splice_match = re.search(
            r"in\s+rule\s+(\d+[A-Z]?),?\s+in\s+sub-rule\s+\((\d+[A-Z]?)\),?\s+"
            r"(after|before)\s+the\s+words(?:,?\s+letters)?(?:\s+and\s+\w+)*\s+['\"\u201c]([^'\"\u201d]+)['\"\u201d],?\s+"
            r"(?:the\s+words(?:,?\s+letters)?(?:\s+and\s+\w+)*\s*)?['\"\u201c]([^'\"\u201d]+)['\"\u201d].*?shall\s+be\s+inserted",
            parse_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not splice_match:
            reverse_splice_match = re.search(
                r"in\s+sub-rule\s+\((\d+[A-Z]?)\)\s+of\s+rule\s+(\d+[A-Z]?),?\s+"
                r"(after|before)\s+the\s+words(?:,?\s+letters)?(?:\s+and\s+\w+)*\s+['\"\u201c]([^'\"\u201d]+)['\"\u201d],?\s+"
                r"(?:the\s+words(?:,?\s+letters)?(?:\s+and\s+\w+)*\s*)?['\"\u201c]([^'\"\u201d]+)['\"\u201d].*?shall\s+be\s+inserted",
                parse_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if reverse_splice_match:
                subrule_label, rule_label, position, anchor, insert_text = reverse_splice_match.groups()
                splice_match = type("_MatchAdapter", (), {"groups": lambda self: (rule_label, subrule_label, position, anchor, insert_text)})()
        if splice_match:
            rule_label, subrule_label, position, anchor, insert_text = splice_match.groups()
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="SPLICE",
                    target_work=target_work,
                    component_id=_component_for_rule_subrule(rule_label, subrule_label),
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="splice_after_words_v1" if position.lower() == "after" else "splice_before_words_v1",
                    payload={"insert_text": insert_text, "position": position.lower()},
                    corpus_lookup=corpus_lookup,
                    anchor_text=anchor,
                    confidence=0.94,
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        contains_rule_reference = bool(re.search(r"\bin\s+rule\s+\d+[A-Z]?\b", parse_text, flags=re.IGNORECASE))
        form_match = re.search(r"\bin\s+FORM\s+GST\s+([A-Z0-9-]+)", parse_text, flags=re.IGNORECASE)
        if form_match and not contains_rule_reference:
            form_id = canonical_form_id("GST " + form_match.group(1).upper())
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="UNKNOWN",
                    target_work=target_work,
                    component_id=form_id,
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="form_or_table_mutation_v1",
                    payload={"text": block_text},
                    corpus_lookup=corpus_lookup,
                    confidence=0.55,
                    initial_reasons=["unsupported_form_or_table_mutation"],
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        rule_level_splice_match = re.search(
            r"in\s+rule\s+(\d+[A-Z]?).*?"
            r"(after|before)\s+the\s+[\w\s,()-]+?\s+['\"\u201c\u2018\u2015]([^'\"\u201d\u2019\u2016]+)['\"\u201d\u2019\u2016],?\s+"
            r"(?:the\s+[\w\s,()-]+?\s*)?['\"\u201c\u2018\u2015]([^'\"\u201d\u2019\u2016]+)['\"\u201d\u2019\u2016].*?shall\s+be\s+inserted",
            parse_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if rule_level_splice_match:
            rule_label, position, anchor, insert_text = rule_level_splice_match.groups()
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="SPLICE",
                    target_work=target_work,
                    component_id=_component_for_rule(rule_label),
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="rule_level_splice_after_words_v1"
                    if position.lower() == "after"
                    else "rule_level_splice_before_words_v1",
                    payload={"insert_text": insert_text, "position": position.lower()},
                    corpus_lookup=corpus_lookup,
                    anchor_text=anchor,
                    confidence=0.9,
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        substitute_match = re.search(
            r"in\s+rule\s+(\d+[A-Z]?)(?:,?\s+in\s+sub-rule\s+\((\d+[A-Z]?)\))?.*?"
            r"for\s+the\s+[\w\s,()-]+?\s+['\"\u201c\u2018\u2015]([^'\"\u201d\u2019\u2016]+)['\"\u201d\u2019\u2016].*?"
            r"['\"\u201c\u2018\u2015]([^'\"\u201d\u2019\u2016]+)['\"\u201d\u2019\u2016].*?shall\s+be\s+substituted",
            parse_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if substitute_match:
            rule_label, subrule_label, old_text, new_text = substitute_match.groups()
            component_id = (
                _component_for_rule_subrule(rule_label, subrule_label)
                if subrule_label
                else _component_for_rule(rule_label)
            )
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="SUBSTITUTE",
                    target_work=target_work,
                    component_id=component_id,
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="substitute_words_v1",
                    payload={"old_text": old_text, "new_text": new_text},
                    corpus_lookup=corpus_lookup,
                    anchor_text=old_text,
                    confidence=0.9,
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        omit_match = re.search(
            r"(?:in\s+rule\s+(\d+[A-Z]?),?\s+)?sub-rule\s+\((\d+[A-Z]?)\)\s+shall\s+be\s+omitted"
            r"|(?:^|\b)rule\s+(\d+[A-Z]?)\s+shall\s+be\s+omitted",
            parse_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if omit_match:
            parent_rule, subrule_label, direct_rule = omit_match.groups()
            if parent_rule and subrule_label:
                component_id = _component_for_rule_subrule(parent_rule, subrule_label)
            else:
                component_id = _component_for_rule(direct_rule or parent_rule or "")
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="OMIT",
                    target_work=target_work,
                    component_id=component_id,
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="omit_rule_or_subrule_v1",
                    payload={"omitted": True},
                    corpus_lookup=corpus_lookup,
                    confidence=0.88,
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        rescind_match = re.search(r"\b(rescind(?:s|ed)?|supersed(?:es|ed)?)\b", block_text, flags=re.IGNORECASE)
        if rescind_match:
            operation = "RESCIND" if rescind_match.group(1).lower().startswith("rescind") else "SUPERSEDE"
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation=operation,
                    target_work=target_work,
                    component_id=target_work,
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="notification_rescind_or_supersede_v1",
                    payload={"text": block_text},
                    corpus_lookup=corpus_lookup,
                    confidence=0.6,
                    initial_reasons=["notification_level_status_change"],
                    parser_context=parser_context,
                )
            )
            sequence += 1
            continue

        if _looks_like_unparsed_target_block(parse_text) and (
            target_work_amendment
            or block.get("context_rule")
            or re.search(r"\bin\s+rule\s+\d+[A-Z]?\b", parse_text, flags=re.IGNORECASE)
        ):
            events.append(
                _make_event(
                    source=source,
                    sequence=sequence,
                    operation="UNKNOWN",
                    target_work=target_work,
                    component_id=_unparsed_candidate_component(parse_text, target_work),
                    source_span=span,
                    excerpt=block_text,
                    pattern_id="unparsed_target_work_amendment_v1",
                    payload={"text": block_text},
                    corpus_lookup=corpus_lookup,
                    confidence=0.35,
                    initial_reasons=["unparsed_target_work_amendment"],
                    parser_context=parser_context,
                )
            )
            sequence += 1

    return events


def _should_try_llm(event: dict[str, Any]) -> bool:
    reasons = set(event.get("review", {}).get("review_reasons", []))
    return bool(reasons & {"unparsed_target_work_amendment", "target_not_resolved", "missing_source_text"})


def _llm_cache_key(event: dict[str, Any]) -> str:
    span = event.get("evidence", {}).get("source_span", {})
    return "|".join(
        [
            str(event.get("event_id") or ""),
            str(span.get("start") or ""),
            str(span.get("end") or ""),
            str(span.get("text_hash") or ""),
        ]
    )


def read_llm_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = str(row.get("cache_key") or "")
        if key:
            cache[key] = row
    return cache


def append_llm_cache(path: Path | None, row: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _append_review_reason(event: dict[str, Any], reason: str) -> dict[str, Any]:
    updated = json.loads(json.dumps(event))
    review = updated.setdefault("review", {})
    reasons = set(review.get("review_reasons", []))
    reasons.add(reason)
    review["review_reasons"] = sorted(reasons)
    review["required"] = True
    updated["status"] = "needs_review"
    return updated


def _llm_prompt(event: dict[str, Any]) -> str:
    target = event.get("target", {})
    return (
        "Extract one CGST Rules textual amendment candidate from this source excerpt. "
        "Return JSON with keys operation, component_id, rule, subrule, anchor_text, payload, confidence. "
        "operation must be one of INSERT_SIBLING, INSERT_CHILD, SPLICE, SUBSTITUTE, OMIT, UNKNOWN. "
        "Use INSERT_CHILD when the excerpt inserts a new sub-rule, clause, proviso, or explanation inside an existing rule. "
        "Use INSERT_SIBLING only when it inserts a new rule after/before another rule. "
        "Use UNKNOWN for form/table/schedule/rate mutations unless the excerpt directly amends rule text. "
        "payload for INSERT_CHILD must include label, node_type, content, position, and anchor_component_id when known. "
        "payload for SPLICE must include insert_text and position. "
        "payload for SUBSTITUTE must include old_text and new_text. "
        "For SUBSTITUTE, anchor_text must be the exact old_text, not the full legal instruction. "
        "Use null for unknown optional fields. Return only one JSON object. Excerpt:\n"
        + json.dumps(
            {
                "current_component_id": target.get("component_id"),
                "excerpt": event.get("evidence", {}).get("excerpt", ""),
            },
            ensure_ascii=False,
        )
    )


def _event_from_llm_candidate(
    *,
    source: SourceRecord,
    original_event: dict[str, Any],
    candidate: dict[str, Any],
    target_work: str,
    corpus_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    span = original_event.get("evidence", {}).get("source_span", {})
    source_span = (int(span.get("start", 0)), int(span.get("end", 0)))
    span_text = source.text[source_span[0] : source_span[1]]
    operation = str(candidate.get("operation") or "UNKNOWN").upper()
    operation_aliases = {
        "INSERT": "UNKNOWN",
        "INSERT_RULE": "INSERT_SIBLING",
        "INSERT_SUBRULE": "INSERT_CHILD",
        "DELETE": "OMIT",
        "REMOVE": "OMIT",
        "REPLACE": "SUBSTITUTE",
    }
    operation = operation_aliases.get(operation, operation)
    component_id = str(candidate.get("component_id") or "")
    if not component_id:
        rule_label = str(candidate.get("rule") or candidate.get("rule_label") or "").strip()
        subrule_label = str(candidate.get("subrule") or candidate.get("subrule_label") or "").strip()
        if rule_label and subrule_label:
            component_id = _component_for_rule_subrule(rule_label, subrule_label)
        elif rule_label:
            component_id = _component_for_rule(rule_label)
    component_id = component_id or str(original_event.get("target", {}).get("component_id") or target_work)
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    if operation == "INSERT_SIBLING" and (candidate.get("subrule") or candidate.get("subrule_label")):
        operation = "INSERT_CHILD"
    if operation == "INSERT_CHILD":
        rule_label = str(candidate.get("rule") or candidate.get("rule_label") or "").strip()
        child_label = str(candidate.get("subrule") or candidate.get("subrule_label") or payload.get("label") or "").strip()
        parent_component_id = str(
            candidate.get("parent_component_id")
            or payload.get("parent_component_id")
            or (_component_for_rule(rule_label) if rule_label else "")
        )
        child_anchor_component_id = str(
            candidate.get("anchor_component_id")
            or payload.get("anchor_component_id")
            or parent_component_id
            or ""
        )
        if parent_component_id and child_label:
            component_id = f"{parent_component_id}/subrule/{child_label.lower()}"
        if not payload:
            payload = {
                "node_type": "subrule",
                "label": child_label,
                "content": str(candidate.get("insert_text") or candidate.get("content") or ""),
                "position": str(candidate.get("position") or "after").lower(),
                "anchor_component_id": child_anchor_component_id or None,
                "parent_component_id": parent_component_id or None,
            }
        else:
            payload.setdefault("node_type", "subrule")
            payload.setdefault("label", child_label)
            payload.setdefault("content", str(candidate.get("insert_text") or candidate.get("content") or ""))
            payload.setdefault("position", str(candidate.get("position") or "after").lower())
            payload.setdefault("anchor_component_id", child_anchor_component_id or None)
            payload.setdefault("parent_component_id", parent_component_id or None)
        if not payload.get("content") or not payload.get("label"):
            component_id, inferred_anchor, inferred_payload = _insert_child_payload_from_span(span_text, component_id)
            payload = {**inferred_payload, **{key: value for key, value in payload.items() if value}}
            if inferred_anchor and not payload.get("anchor_component_id"):
                payload["anchor_component_id"] = inferred_anchor
    if not payload and operation == "SPLICE":
        payload = {
            "insert_text": str(candidate.get("insert_text") or ""),
            "position": str(candidate.get("position") or "after").lower(),
        }
    if not payload and operation == "SUBSTITUTE":
        payload = {
            "old_text": str(candidate.get("old_text") or ""),
            "new_text": str(candidate.get("new_text") or ""),
        }
    if operation == "SUBSTITUTE" and re.search(
        r"\bfor\s+(?:FORM|rule|sub-rule|Explanation)\b|for\s+the\s+following\s+FORM\b",
        span_text,
        flags=re.IGNORECASE,
    ):
        component_id, structural_payload = _structural_substitute_payload(span_text, component_id)
        payload = structural_payload
    if operation == "OMIT" and re.search(
        r"\b(?:proviso|word|words|letters?|figures?|clause)\b.*?\bshall\s+be\s+omitted",
        span_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        payload = _partial_omit_payload(span_text)
    anchor_text = candidate.get("anchor_text") or candidate.get("anchor") or None
    insert_text = str(payload.get("insert_text") or candidate.get("insert_text") or payload.get("content") or "")
    if operation == "INSERT_SIBLING":
        rule_insert = re.match(r"\s*(?:Rule\s+)?(\d+[A-Z]?)\.\s*(.+)", insert_text, flags=re.IGNORECASE | re.DOTALL)
        anchor_rule = re.search(r"\bafter\s+rule\s+(\d+[A-Z]?)\b", str(anchor_text or ""), flags=re.IGNORECASE)
        if rule_insert and anchor_rule and not re.search(r"\bFORM\s+GST\b", insert_text, flags=re.IGNORECASE):
            new_label = rule_insert.group(1)
            heading, content = _rule_text_parts(insert_text)
            component_id = _component_for_rule(new_label)
            payload = {
                "node_type": "rule",
                "label": new_label,
                "heading": heading,
                "content": content,
                "position": "after",
                "anchor_rule": anchor_rule.group(1),
            }
            anchor_text = None
            candidate["anchor_component_id"] = _component_for_rule(anchor_rule.group(1))
        else:
            child_insert = re.match(r"\s*\(([^)]+)\)\s*(.+)", insert_text, flags=re.IGNORECASE | re.DOTALL)
            anchor_subrule = re.search(
                r"\bafter\s+sub-?rule\s+\(([^)]+)\)", str(anchor_text or ""), flags=re.IGNORECASE
            )
            parent_rule_match = re.search(r"/rule/([^/]+)$", component_id)
            if child_insert and anchor_subrule and parent_rule_match:
                operation = "INSERT_CHILD"
                child_label = child_insert.group(1)
                parent_component_id = component_id
                component_id = f"{parent_component_id}/subrule/{re.sub(r'[^0-9A-Za-z]+', '', child_label).lower()}"
                payload = {
                    "node_type": "subrule",
                    "label": child_label,
                    "content": insert_text,
                    "position": "after",
                    "anchor_component_id": (
                        f"{parent_component_id}/subrule/"
                        f"{re.sub(r'[^0-9A-Za-z]+', '', anchor_subrule.group(1)).lower()}"
                    ),
                    "parent_component_id": parent_component_id,
                }
                anchor_text = None
    anchor_component_id = None
    if operation == "SUBSTITUTE" and payload.get("old_text"):
        anchor_text = payload.get("old_text")
    if operation == "INSERT_CHILD":
        anchor_component_id = str(payload.get("anchor_component_id") or "") or None
        parent_component_id = str(payload.get("parent_component_id") or "") or None
        if parent_component_id and payload.get("label"):
            component_id = f"{parent_component_id}/subrule/{str(payload.get('label') or '').lower()}"
    promoted = _make_event(
        source=source,
        sequence=0,
        operation=operation,
        target_work=target_work,
        component_id=component_id,
        source_span=source_span,
        excerpt=source.text[source_span[0] : source_span[1]],
        pattern_id="omlx_candidate_v1",
        payload=payload,
        corpus_lookup=corpus_lookup,
        anchor_text=str(anchor_text) if anchor_text else None,
        anchor_component_id=anchor_component_id or str(candidate.get("anchor_component_id") or "") or None,
        confidence=float(candidate.get("confidence") or 0.0),
        initial_reasons=[],
    )
    promoted["event_id"] = original_event["event_id"]
    promoted["legacy_event_id"] = original_event.get("legacy_event_id")
    promoted["evidence"]["parser_trace"]["llm_candidate"] = candidate
    if promoted["status"] != "validated":
        reasons = set(promoted.get("review", {}).get("review_reasons", []))
        reasons.add("llm_candidate_not_validated")
        promoted["review"]["review_reasons"] = sorted(reasons)
    return promoted


def enhance_events_with_omlx(
    *,
    source: SourceRecord,
    events: list[dict[str, Any]],
    target_work: str,
    corpus_lookup: dict[str, dict[str, Any]],
    config: OmlxConfig,
    cache_path: Path | None = None,
    max_attempts: int | None = None,
    concurrency: int = 1,
    stats: Counter[str] | None = None,
) -> list[dict[str, Any]]:
    enhanced: list[dict[str, Any] | None] = [None] * len(events)
    cache = read_llm_cache(cache_path)
    attempts = 0
    pending: list[tuple[int, dict[str, Any], str]] = []
    for index, event in enumerate(events):
        if not _should_try_llm(event):
            enhanced[index] = event
            continue
        key = _llm_cache_key(event)
        cached = cache.get(key)
        if cached and isinstance(cached.get("candidate"), dict):
            if stats is not None:
                stats["llm_cache_hits"] += 1
            enhanced[index] = (
                _event_from_llm_candidate(
                    source=source,
                    original_event=event,
                    candidate=cached["candidate"],
                    target_work=target_work,
                    corpus_lookup=corpus_lookup,
                )
            )
            continue
        if max_attempts is not None and attempts >= max_attempts:
            enhanced[index] = _append_review_reason(event, "llm_limit_not_attempted")
            if stats is not None:
                stats["llm_limit_not_attempted"] += 1
            continue
        attempts += 1
        if stats is not None:
            stats["llm_scheduled"] += 1
        pending.append((index, event, key))

    def call_llm(item: tuple[int, dict[str, Any], str]) -> tuple[int, dict[str, Any], str, dict[str, Any] | None, str | None]:
        index, event, key = item
        try:
            return index, event, key, chat_json(_llm_prompt(event), config=config), None
        except OmlxError as exc:
            return index, event, key, None, getattr(exc, "reason", "llm_unavailable")

    cache_lock = Lock()
    if pending:
        workers = max(1, min(int(concurrency or 1), len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(call_llm, item) for item in pending]
            for future in as_completed(futures):
                index, event, key, candidate, reason = future.result()
                if reason:
                    with cache_lock:
                        append_llm_cache(
                            cache_path,
                            {
                                "cache_key": key,
                                "event_id": event.get("event_id"),
                                "status": "error",
                                "reason": reason,
                                "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                            },
                        )
                    if stats is not None:
                        stats[reason] += 1
                    enhanced[index] = _append_review_reason(event, reason)
                    continue
                assert candidate is not None
                with cache_lock:
                    append_llm_cache(
                        cache_path,
                        {
                            "cache_key": key,
                            "event_id": event.get("event_id"),
                            "status": "ok",
                            "candidate": candidate,
                            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                        },
                    )
                if stats is not None:
                    stats["llm_attempted"] += 1
                enhanced[index] = _event_from_llm_candidate(
                    source=source,
                    original_event=event,
                    candidate=candidate,
                    target_work=target_work,
                    corpus_lookup=corpus_lookup,
                )
    return [event for event in enhanced if event is not None]


def write_jsonl(events: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events) + "\n", encoding="utf-8")


def read_events(path: Path | str) -> list[dict[str, Any]]:
    event_path = Path(path)
    if not event_path.exists():
        return []
    return [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def build_review_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts_by_status = Counter(event.get("status", "") for event in events)
    counts_by_operation = Counter(event.get("operation", "") for event in events)
    counts_by_year = Counter((event.get("source", {}).get("publication_date", "") or "unknown")[:4] for event in events)
    counts_by_target = Counter(event.get("target", {}).get("work_id", "") for event in events)
    reason_counts: Counter[str] = Counter()
    non_validated = []
    for event in events:
        reasons = event.get("review", {}).get("review_reasons", [])
        reason_counts.update(reasons)
        if event.get("status") == "validated":
            continue
        non_validated.append(
            {
                "event_id": event.get("event_id"),
                "status": event.get("status"),
                "operation": event.get("operation"),
                "source_document_id": event.get("source", {}).get("document_id"),
                "record_id": event.get("source", {}).get("record_id"),
                "target": event.get("target", {}),
                "source_span": event.get("evidence", {}).get("source_span", {}),
                "excerpt": event.get("evidence", {}).get("excerpt", ""),
                "parser_pattern": event.get("evidence", {}).get("parser_trace", {}).get("pattern_id", ""),
                "review_reasons": reasons,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "event_count": len(events),
        "counts": {
            "by_status": dict(sorted(counts_by_status.items())),
            "by_operation": dict(sorted(counts_by_operation.items())),
            "by_year": dict(sorted(counts_by_year.items())),
            "by_target": dict(sorted(counts_by_target.items())),
            "by_review_reason": dict(sorted(reason_counts.items())),
        },
        "non_validated_events": non_validated,
    }


def compile_events(
    *,
    registry_path: Path,
    source_dir: Path,
    category: str,
    target_work: str,
    output: Path,
    review_output: Path,
    corpus_dir: Path = Path("corpus"),
    source_archive_root: Path = Path("sources"),
    extract_pdf_text: bool = False,
    use_llm: bool = False,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key_env: str = "OMLX_API_KEY",
    llm_cache: Path | None = None,
    llm_limit: int | None = None,
    llm_concurrency: int = 1,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    resolved_work = registry.resolve_corpus_id(target_work) or target_work
    baseline_path = registry.baseline_path(resolved_work)
    if baseline_path:
        from .baselines import build_baseline

        baseline_dir = Path(baseline_path)
        if not (baseline_dir / "baseline.xml").exists():
            build_baseline(target_work=resolved_work, registry_path=registry_path, output_dir=baseline_dir)
        corpus_lookup = build_target_baseline_lookup(baseline_dir, resolved_work)
        # Forms are not part of the Rules baseline; keep current form lookup for review surfacing.
        corpus_lookup.update(
            {
                key: value
                for key, value in build_target_corpus_lookup(corpus_dir, resolved_work).items()
                if key.startswith("/in/union/forms/")
            }
        )
    else:
        corpus_lookup = build_target_corpus_lookup(corpus_dir, resolved_work)

    events: list[dict[str, Any]] = []
    llm_config = (
        OmlxConfig.from_env(base_url=llm_base_url, model=llm_model, api_key_env=llm_api_key_env)
        if use_llm
        else None
    )
    llm_stats: Counter[str] = Counter()
    remaining_llm_attempts = llm_limit
    for json_path in iter_cbic_json(source_dir, category=category):
        source = load_source_record(
            json_path,
            source_archive_root=source_archive_root,
            extract_pdf_text=extract_pdf_text,
        )
        source_events = compile_events_from_text(source, target_work=resolved_work, corpus_lookup=corpus_lookup)
        if llm_config:
            scheduled_before = llm_stats.get("llm_scheduled", 0)
            source_events = enhance_events_with_omlx(
                source=source,
                events=source_events,
                target_work=resolved_work,
                corpus_lookup=corpus_lookup,
                config=llm_config,
                cache_path=llm_cache,
                max_attempts=remaining_llm_attempts,
                concurrency=llm_concurrency,
                stats=llm_stats,
            )
            if remaining_llm_attempts is not None:
                remaining_llm_attempts = max(0, remaining_llm_attempts - (llm_stats.get("llm_scheduled", 0) - scheduled_before))
        events.extend(source_events)

    events.sort(
        key=lambda event: (
            event.get("source", {}).get("publication_date", ""),
            event.get("source", {}).get("record_id", ""),
            event.get("evidence", {}).get("source_span", {}).get("start", 0),
            event.get("legacy_event_id") or event.get("event_id", ""),
        )
    )
    write_jsonl(events, output)
    report = build_review_report(events)
    review_output.parent.mkdir(parents=True, exist_ok=True)
    review_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "events": len(events),
        "output": str(output),
        "review_output": str(review_output),
        "review_report": report,
        "llm": dict(sorted(llm_stats.items())),
    }


__all__ = [
    "compile_events",
    "compile_events_from_text",
    "build_target_corpus_lookup",
    "build_target_baseline_lookup",
    "load_source_record",
    "enhance_events_with_omlx",
    "read_llm_cache",
    "read_events",
    "build_review_report",
    "stable_event_id",
    "sha256_text",
]
