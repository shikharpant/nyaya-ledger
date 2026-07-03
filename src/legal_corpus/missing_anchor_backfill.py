"""Source-backed backfill events for rule insertions missed by the broad compiler.

This module is deliberately narrow. It only emits validated INSERT_SIBLING
events for known missing anchor rules whose insertion text is present in the
canonical notification XML corpus.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .amendment_events import DEFAULT_OBSERVED_AT, sha256_text, write_jsonl
from .renderer import canonical_rule_id


TARGET_WORK = "/in/union/rules/cgst-rules-2017"
COMPILER_VERSION = "missing-anchor-backfill-v1"


@dataclass(frozen=True)
class BackfillSpec:
    label: str
    anchor_label: str
    xml_path: str
    paragraph_nums: tuple[str, ...]
    effective_date: str
    date_basis: str
    replacement_event_id: str | None = None
    end_pattern: str | None = None


@dataclass(frozen=True)
class StructuralSubstituteSpec:
    label: str
    xml_path: str
    paragraph_nums: tuple[str, ...]
    effective_date: str
    date_basis: str
    replacement_event_id: str
    end_pattern: str | None = None


@dataclass(frozen=True)
class SpliceSpec:
    component_label: str
    xml_path: str
    paragraph_nums: tuple[str, ...]
    anchor_text: str
    insert_text: str
    effective_date: str
    date_basis: str
    occurrence: int = 1
    position: str = "after"
    replacement_event_id: str | None = None


@dataclass(frozen=True)
class WholeRuleOmitSpec:
    labels: tuple[str, ...]
    xml_path: str
    paragraph_nums: tuple[str, ...]
    effective_date: str
    date_basis: str
    instruction_pattern: str


@dataclass(frozen=True)
class TextEditRepairSpec:
    event_id: str
    operation: str
    component_id: str
    xml_path: str
    paragraph_nums: tuple[str, ...]
    effective_date: str
    date_basis: str
    payload: dict[str, Any]
    anchor_text: str | None = None
    anchor_occurrence: int | None = None


BACKFILL_SPECS: tuple[BackfillSpec, ...] = (
    BackfillSpec(
        label="88A",
        anchor_label="88",
        xml_path="in/union/notifications/cbic/central-tax/2019/16-2019-central-tax.xml",
        paragraph_nums=("46", "47"),
        effective_date="2019-03-29",
        date_basis="publication_date_fallback",
    ),
    BackfillSpec(
        label="67A",
        anchor_label="67",
        xml_path="in/union/notifications/cbic/central-tax/2020/38-2020-central-tax.xml",
        paragraph_nums=("4", "5"),
        effective_date="2020-06-08",
        date_basis="commencement_notification_44_2020_rule_3",
        end_pattern=r"\[F\.\s*No\.",
    ),
    BackfillSpec(
        label="10B",
        anchor_label="10A",
        xml_path="in/union/notifications/cbic/central-tax/2021/35-2021-central-tax.xml",
        paragraph_nums=("3", "4", "5"),
        effective_date="2022-01-01",
        date_basis="commencement_notification_38_2021_rule_2_subrule_2",
        end_pattern=r"\(3\)\s+In\s+rule\s+23\b",
    ),
    BackfillSpec(
        label="37A",
        anchor_label="37",
        xml_path="in/union/notifications/cbic/central-tax/2022/26-2022-central-tax.xml",
        paragraph_nums=("10", "11"),
        effective_date="2022-12-26",
        date_basis="publication_date_general_commencement_clause",
    ),
    BackfillSpec(
        label="83B",
        anchor_label="83A",
        xml_path="in/union/notifications/cbic/central-tax/2019/33-2019-central-tax.xml",
        paragraph_nums=("7", "8", "9"),
        effective_date="2019-07-18",
        date_basis="deferred_commencement_text_present_in_current_checkpoint",
        end_pattern=r"\b6\.\s+In\s+the\s+said\s+rules\b",
    ),
    BackfillSpec(
        label="88B",
        anchor_label="88A",
        xml_path="in/union/notifications/cbic/central-tax/2022/14-2022-central-tax.xml",
        paragraph_nums=("12", "13", "14", "15", "16", "17", "18"),
        effective_date="2017-07-01",
        date_basis="explicit_retrospective_effective_clause",
        replacement_event_id="evt_cbic_11b43e13ef68e44e",
    ),
    BackfillSpec(
        label="88C",
        anchor_label="88B",
        xml_path="in/union/notifications/cbic/central-tax/2022/26-2022-central-tax.xml",
        paragraph_nums=("16", "17"),
        effective_date="2022-12-26",
        date_basis="publication_date_fallback",
    ),
    BackfillSpec(
        label="109C",
        anchor_label="109B",
        xml_path="in/union/notifications/cbic/central-tax/2022/26-2022-central-tax.xml",
        paragraph_nums=("29", "30", "31"),
        effective_date="2022-12-26",
        date_basis="publication_date_general_commencement_clause",
    ),
    BackfillSpec(
        label="31B",
        anchor_label="31A",
        xml_path="in/union/notifications/cbic/central-tax/2023/51-2023-central-tax.xml",
        paragraph_nums=("7", "8"),
        effective_date="2023-10-01",
        date_basis="explicit_commencement_clause",
        replacement_event_id="evt_cbic_787fb90c7719633b",
    ),
    BackfillSpec(
        label="31C",
        anchor_label="31B",
        xml_path="in/union/notifications/cbic/central-tax/2023/51-2023-central-tax.xml",
        paragraph_nums=("9", "10", "11", "12"),
        effective_date="2023-10-01",
        date_basis="explicit_commencement_clause",
        end_pattern=r"\b5\.\s+In\s+the\s+said\s+rules\b",
    ),
    BackfillSpec(
        label="95B",
        anchor_label="95",
        xml_path="in/union/notifications/cbic/central-tax/2024/12-2024-central-tax.xml",
        paragraph_nums=("51", "52", "53", "54"),
        effective_date="2024-07-10",
        date_basis="publication_date_general_commencement_clause",
    ),
    BackfillSpec(
        label="113A",
        anchor_label="113",
        xml_path="in/union/notifications/cbic/central-tax/2024/12-2024-central-tax.xml",
        paragraph_nums=("66", "67"),
        effective_date="2024-07-10",
        date_basis="publication_date_general_commencement_clause",
    ),
    BackfillSpec(
        label="88D",
        anchor_label="88C",
        xml_path="in/union/notifications/cbic/central-tax/2023/38-2023-central-tax.xml",
        paragraph_nums=("11", "12", "13"),
        effective_date="2023-08-04",
        date_basis="publication_date_general_commencement_clause",
        replacement_event_id="evt_cbic_291eb5ea9ade9727",
        end_pattern=r"\b13\.\s+In\s+the\s+said\s+rules\b",
    ),
    BackfillSpec(
        label="31D",
        anchor_label="31C",
        xml_path="in/union/notifications/cbic/central-tax/2025/20-2025-central-tax.xml",
        paragraph_nums=("2", "3", "4"),
        effective_date="2026-02-01",
        date_basis="explicit_commencement_clause",
        replacement_event_id="evt_cbic_d8f097a337c10b84",
        end_pattern=r"\b3\.\s+In\s+the\s+said\s+rules\b",
    ),
)


WHOLE_RULE_OMIT_SPECS: tuple[WholeRuleOmitSpec, ...] = (
    WholeRuleOmitSpec(
        labels=("69", "70", "71", "72", "73", "74", "75", "76", "77", "79"),
        xml_path="in/union/notifications/cbic/central-tax/2022/19-2022-central-tax.xml",
        paragraph_nums=("14",),
        effective_date="2022-10-01",
        date_basis="publication_date_general_commencement_clause",
        instruction_pattern=r"\brules\s+69,\s*70,\s*71,\s*72,\s*73,\s*74,\s*75,\s*76,\s*77\s+and\s+79\s+of\s+the\s+said\s+rules\s+shall\s+be\s+omitted\b",
    ),
)


STRUCTURAL_SUBSTITUTE_SPECS: tuple[StructuralSubstituteSpec, ...] = (
    StructuralSubstituteSpec(
        label="67A",
        xml_path="in/union/notifications/cbic/central-tax/2020/58-2020-central-tax.xml",
        paragraph_nums=("6", "7", "8", "9"),
        effective_date="2020-07-01",
        date_basis="explicit_commencement_clause",
        replacement_event_id="evt_cbic_f1ec09b93bd1ac51",
    ),
)


SPLICE_SPECS: tuple[SpliceSpec, ...] = (
    SpliceSpec(
        component_label="88C",
        xml_path="in/union/notifications/cbic/central-tax/2024/12-2024-central-tax.xml",
        paragraph_nums=("47",),
        anchor_text="FORM GSTR-1",
        insert_text=", as amended in FORM GSTR-1A if any,",
        effective_date="2024-07-10",
        date_basis="publication_date_general_commencement_clause",
    ),
)


TEXT_EDIT_REPAIR_SPECS: tuple[TextEditRepairSpec, ...] = (
    TextEditRepairSpec(
        event_id="evt_cbic_177fad0ed5c5d04f",
        operation="SUBSTITUTE",
        component_id=canonical_rule_id("89"),
        xml_path="in/union/notifications/cbic/central-tax/2017/17-2017-central-tax.xml",
        paragraph_nums=("7",),
        effective_date="2017-07-01",
        date_basis="explicit_effective_clause_in_instruction",
        anchor_text="sub-section",
        payload={
            "old_text": "sub-section",
            "new_text": "clause",
            "noop_if_already_reflected": True,
            "materializer_repair": True,
        },
    ),
    TextEditRepairSpec(
        event_id="evt_cbic_8739d092c4f28ae8",
        operation="SUBSTITUTE",
        component_id=f"{canonical_rule_id('54')}/subrule/2",
        xml_path="in/union/notifications/cbic/central-tax/2017/45-2017-central-tax.xml",
        paragraph_nums=("3",),
        effective_date="2017-10-13",
        date_basis="publication_date_fallback",
        anchor_text="tax invoice",
        anchor_occurrence=1,
        payload={
            "old_text": "tax invoice",
            "new_text": "consolidated tax invoice",
            "match_occurrence": 1,
            "materializer_repair": True,
        },
    ),
    TextEditRepairSpec(
        event_id="evt_cbic_e9f5c2ee072808ab",
        operation="SUBSTITUTE",
        component_id=canonical_rule_id("120"),
        xml_path="in/union/notifications/cbic/central-tax/2017/36-2017-central-tax.xml",
        paragraph_nums=("5",),
        effective_date="2017-09-29",
        date_basis="publication_date_fallback",
        anchor_text="ninety days of the appointed day",
        payload={
            "old_text": "ninety days of the appointed day",
            "new_text": "the period specified in rule 117 or such further period as extended by the Commissioner",
            "noop_if_already_reflected": True,
            "materializer_repair": True,
        },
    ),
    TextEditRepairSpec(
        event_id="evt_cbic_1b5dc6be014a997a",
        operation="OMIT",
        component_id=canonical_rule_id("42"),
        xml_path="in/union/notifications/cbic/central-tax/2022/19-2022-central-tax.xml",
        paragraph_nums=("14",),
        effective_date="2022-10-01",
        date_basis="explicit_commencement_clause",
        anchor_text="at the invoice level in FORM GSTR-2 and",
        payload={
            "omit_text": "at the invoice level in FORM GSTR-2 and",
            "whole_component": False,
            "noop_if_already_reflected": True,
            "materializer_repair": True,
        },
    ),
    TextEditRepairSpec(
        event_id="evt_cbic_030358a06f4f6e69",
        operation="OMIT",
        component_id=canonical_rule_id("43"),
        xml_path="in/union/notifications/cbic/central-tax/2022/19-2022-central-tax.xml",
        paragraph_nums=("14",),
        effective_date="2022-10-01",
        date_basis="explicit_commencement_clause",
        anchor_text="FORM GSTR-2 and",
        payload={
            "omit_text": "FORM GSTR-2 and",
            "whole_component": False,
            "noop_if_already_reflected": True,
            "materializer_repair": True,
        },
    ),
    TextEditRepairSpec(
        event_id="evt_cbic_8d1e9b596d074bea",
        operation="OMIT",
        component_id=canonical_rule_id("83"),
        xml_path="in/union/notifications/cbic/central-tax/2022/19-2022-central-tax.xml",
        paragraph_nums=("15",),
        effective_date="2022-10-01",
        date_basis="explicit_commencement_clause",
        anchor_text="and inward",
        anchor_occurrence=1,
        payload={
            "omit_text": "and inward",
            "whole_component": False,
            "match_occurrence": 1,
            "materializer_repair": True,
        },
    ),
    TextEditRepairSpec(
        event_id="evt_cbic_a8b48891c38dc61e",
        operation="OMIT",
        component_id=f"{canonical_rule_id('9')}/subrule/1",
        xml_path="in/union/notifications/cbic/central-tax/2023/38-2023-central-tax.xml",
        paragraph_nums=("3",),
        effective_date="2023-08-04",
        date_basis="publication_date_fallback",
        anchor_text="in the presence of the said person",
        anchor_occurrence=1,
        payload={
            "omit_text": "in the presence of the said person",
            "whole_component": False,
            "match_occurrence": 1,
            "materializer_repair": True,
        },
    ),
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _xml_properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) != "property":
            continue
        name = element.attrib.get("name")
        if name:
            props[name] = element.attrib.get("value", "")
    return props


def _paragraph_text(paragraph: ET.Element) -> str:
    content = paragraph.find("./content")
    if content is None:
        content = paragraph
    lines = [_clean_text("".join(node.itertext())) for node in content if _local_name(node.tag) in {"p", "heading"}]
    if not lines:
        lines = [_clean_text("".join(content.itertext()))]
    return "\n".join(line for line in lines if line)


def _paragraphs_by_num(root: ET.Element) -> dict[str, ET.Element]:
    paragraphs: dict[str, ET.Element] = {}
    for element in root.iter():
        if _local_name(element.tag) != "paragraph":
            continue
        num = _clean_text(element.findtext("./num") or "")
        if num:
            paragraphs[num] = element
    return paragraphs


def _document_text(root: ET.Element) -> str:
    paragraphs = []
    for element in root.iter():
        if _local_name(element.tag) == "paragraph":
            text = _paragraph_text(element)
            if text:
                paragraphs.append(text)
    return "\n".join(paragraphs)


def _span_bounds(paragraphs: list[ET.Element]) -> tuple[int, int]:
    starts = [int(p.attrib.get("sourceStart", "0")) for p in paragraphs if p.attrib.get("sourceStart")]
    ends = [int(p.attrib.get("sourceEnd", "0")) for p in paragraphs if p.attrib.get("sourceEnd")]
    return (min(starts) if starts else 0, max(ends) if ends else 0)


def _strip_source_quotes(value: str) -> str:
    return value.strip().strip(" \t\r\n\"'“”‘’‗―")


def _extract_rule_text(text: str, label: str, end_pattern: str | None = None) -> str:
    start_match = re.search(rf"(?:Rule\s+)?{re.escape(label)}(?:\s*\.|\s+)", text, flags=re.IGNORECASE)
    if not start_match:
        raise ValueError(f"Could not find inserted rule label {label}")
    end = len(text)
    if end_pattern:
        end_match = re.search(end_pattern, text[start_match.end() :], flags=re.IGNORECASE | re.DOTALL)
        if end_match:
            end = start_match.end() + end_match.start()
    rule_text = _strip_source_quotes(text[start_match.start() : end])
    rule_text = re.sub(r"[”\"'‘’‗―]+\s*[.;]?\s*$", "", rule_text).strip()
    return rule_text


def _rule_parts(rule_text: str, label: str) -> tuple[str, str, str]:
    text = _strip_source_quotes(rule_text)
    match = re.match(rf"(?:Rule\s+)?{re.escape(label)}\s*\.?\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError(f"Could not parse inserted rule text for {label}")
    body = match.group(1).strip()
    split = re.match(
        r"(?P<heading>.+?)(?:\.\s*[-–—]\s*|:\s*[-–—]\s*|\s+[-–—]\s+)(?P<content>.*)$",
        body,
        flags=re.DOTALL,
    )
    if not split:
        return label.upper(), "", body
    heading = _clean_text(split.group("heading"))
    content = split.group("content").strip()
    return label.upper(), heading, content


def _stable_backfill_event_id(document_id: str, component_id: str, start: int, end: int, rule_text: str) -> str:
    seed = "|".join([document_id, component_id, str(start), str(end), sha256_text(rule_text)])
    return f"evt_cbic_xml_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _stable_splice_event_id(document_id: str, component_id: str, start: int, end: int, spec: SpliceSpec) -> str:
    seed = "|".join(
        [
            document_id,
            component_id,
            str(start),
            str(end),
            spec.anchor_text,
            spec.insert_text,
            spec.position,
            str(spec.occurrence),
        ]
    )
    return f"evt_cbic_xml_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _stable_omit_event_id(document_id: str, component_id: str, start: int, end: int, instruction: str) -> str:
    seed = "|".join([document_id, component_id, str(start), str(end), sha256_text(instruction)])
    return f"evt_cbic_xml_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _event_for_spec(corpus_dir: Path, spec: BackfillSpec) -> dict[str, Any]:
    xml_path = corpus_dir / spec.xml_path
    root = ET.parse(xml_path).getroot()
    props = _xml_properties(root)
    paragraphs_by_num = _paragraphs_by_num(root)
    selected = [paragraphs_by_num[num] for num in spec.paragraph_nums]
    selected_text = "\n".join(_paragraph_text(paragraph) for paragraph in selected)
    rule_text = _extract_rule_text(selected_text, spec.label, spec.end_pattern)
    label, heading, content = _rule_parts(rule_text, spec.label)
    start, end = _span_bounds(selected)
    document_id = props.get("canonical_id") or f"/in/union/notifications/cbic/central-tax/{Path(spec.xml_path).stem}"
    component_id = canonical_rule_id(spec.label)
    anchor_component_id = canonical_rule_id(spec.anchor_label)
    event_id = spec.replacement_event_id or _stable_backfill_event_id(document_id, component_id, start, end, rule_text)
    publication_date = props.get("publication_date") or props.get("effective_from") or spec.effective_date
    document_text = _document_text(root)
    source_file_sha = props.get("source_sha256") or _sha256_file(xml_path)
    source_text_sha = sha256_text(document_text)
    excerpt = rule_text[:700]

    return {
        "event_id": event_id,
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "INSERT_SIBLING",
        "source": {
            "document_id": document_id,
            "record_id": props.get("cbic_id", ""),
            "instrument_number": props.get("cbic_no", ""),
            "issuing_authority": props.get("issuing_authority", "/in/authority/cbic"),
            "publication_date": publication_date,
            "source_url": props.get("source_url", ""),
            "source_file_sha256": source_file_sha,
            "source_text_sha256": source_text_sha,
            "text_source": "canonical_notification_xml",
        },
        "legal_time": {
            "commencement_date": spec.effective_date,
            "applicability_start": spec.effective_date,
            "applicability_end": None,
            "retrospective": bool(publication_date and spec.effective_date < publication_date),
            "date_basis": spec.date_basis,
        },
        "system_time": {
            "observed_at": DEFAULT_OBSERVED_AT,
            "compiled_at": DEFAULT_OBSERVED_AT,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": TARGET_WORK,
            "component_id": component_id,
            "anchor_component_id": anchor_component_id,
            "anchor_text": f"after rule {spec.anchor_label}",
            "anchor_hash": sha256_text(anchor_component_id),
            "anchor_occurrence": 1,
        },
        "payload": {
            "node_type": "rule",
            "label": label,
            "heading": heading,
            "content": content,
            "position": "after",
            "anchor_component_id": anchor_component_id,
            "source_text": rule_text,
        },
        "evidence": {
            "source_span": {"start": start, "end": end, "text_hash": sha256_text(rule_text)},
            "excerpt": excerpt,
            "parser_trace": {
                "pattern_id": "canonical_notification_xml_insert_rule_backfill_v1",
                "confidence": 1.0,
                "paragraph_nums": list(spec.paragraph_nums),
            },
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {
            "required": False,
            "review_reasons": [],
            "reviewed_by": "missing-anchor-backfill",
            "reviewed_at": DEFAULT_OBSERVED_AT,
        },
    }


def _events_for_whole_rule_omit_spec(corpus_dir: Path, spec: WholeRuleOmitSpec) -> list[dict[str, Any]]:
    xml_path = corpus_dir / spec.xml_path
    root = ET.parse(xml_path).getroot()
    props = _xml_properties(root)
    paragraphs_by_num = _paragraphs_by_num(root)
    selected = [paragraphs_by_num[num] for num in spec.paragraph_nums]
    selected_text = "\n".join(_paragraph_text(paragraph) for paragraph in selected)
    match = re.search(spec.instruction_pattern, selected_text, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not find whole-rule omission instruction in {spec.xml_path}: {spec.paragraph_nums}")
    instruction = _clean_text(match.group(0))
    start, end = _span_bounds(selected)
    document_id = props.get("canonical_id") or f"/in/union/notifications/cbic/central-tax/{Path(spec.xml_path).stem}"
    publication_date = props.get("publication_date") or props.get("effective_from") or spec.effective_date
    document_text = _document_text(root)
    source_file_sha = props.get("source_sha256") or _sha256_file(xml_path)
    source_text_sha = sha256_text(document_text)
    events = []
    for label in spec.labels:
        component_id = canonical_rule_id(label)
        events.append(
            {
                "event_id": _stable_omit_event_id(document_id, component_id, start, end, instruction),
                "event_type": "TEXTUAL_AMENDMENT",
                "operation": "OMIT",
                "source": {
                    "document_id": document_id,
                    "record_id": props.get("cbic_id", ""),
                    "instrument_number": props.get("cbic_no", ""),
                    "issuing_authority": props.get("issuing_authority", "/in/authority/cbic"),
                    "publication_date": publication_date,
                    "source_url": props.get("source_url", ""),
                    "source_file_sha256": source_file_sha,
                    "source_text_sha256": source_text_sha,
                    "text_source": "canonical_notification_xml",
                },
                "legal_time": {
                    "commencement_date": spec.effective_date,
                    "applicability_start": spec.effective_date,
                    "applicability_end": None,
                    "retrospective": bool(publication_date and spec.effective_date < publication_date),
                    "date_basis": spec.date_basis,
                },
                "system_time": {
                    "observed_at": DEFAULT_OBSERVED_AT,
                    "compiled_at": DEFAULT_OBSERVED_AT,
                    "compiler_version": COMPILER_VERSION,
                },
                "target": {
                    "work_id": TARGET_WORK,
                    "component_id": component_id,
                    "anchor_component_id": component_id,
                    "anchor_text": f"rule {label}",
                    "anchor_hash": sha256_text(component_id),
                    "anchor_occurrence": 1,
                },
                "payload": {
                    "whole_component": True,
                    "omitted_label": label,
                },
                "evidence": {
                    "source_span": {"start": start, "end": end, "text_hash": sha256_text(instruction)},
                    "excerpt": instruction,
                    "parser_trace": {
                        "pattern_id": "canonical_notification_xml_whole_rule_omit_backfill_v1",
                        "confidence": 1.0,
                        "paragraph_nums": list(spec.paragraph_nums),
                    },
                },
                "validation": {
                    "target_resolved": True,
                    "anchor_resolved": True,
                    "date_resolved": True,
                    "source_span_verified": True,
                    "materializable": True,
                },
                "status": "validated",
                "review": {
                    "required": False,
                    "review_reasons": [],
                    "reviewed_by": "missing-anchor-backfill",
                    "reviewed_at": DEFAULT_OBSERVED_AT,
                },
            }
        )
    return events


def _event_for_structural_substitute_spec(corpus_dir: Path, spec: StructuralSubstituteSpec) -> dict[str, Any]:
    xml_path = corpus_dir / spec.xml_path
    root = ET.parse(xml_path).getroot()
    props = _xml_properties(root)
    paragraphs_by_num = _paragraphs_by_num(root)
    selected = [paragraphs_by_num[num] for num in spec.paragraph_nums]
    selected_text = "\n".join(_paragraph_text(paragraph) for paragraph in selected)
    rule_text = _extract_rule_text(selected_text, spec.label, spec.end_pattern)
    label, heading, content = _rule_parts(rule_text, spec.label)
    start, end = _span_bounds(selected)
    document_id = props.get("canonical_id") or f"/in/union/notifications/cbic/central-tax/{Path(spec.xml_path).stem}"
    component_id = canonical_rule_id(spec.label)
    publication_date = props.get("publication_date") or props.get("effective_from") or spec.effective_date
    document_text = _document_text(root)
    source_file_sha = props.get("source_sha256") or _sha256_file(xml_path)
    source_text_sha = sha256_text(document_text)
    excerpt = selected_text[:700]

    return {
        "event_id": spec.replacement_event_id,
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SUBSTITUTE",
        "source": {
            "document_id": document_id,
            "record_id": props.get("cbic_id", ""),
            "instrument_number": props.get("cbic_no", ""),
            "issuing_authority": props.get("issuing_authority", "/in/authority/cbic"),
            "publication_date": publication_date,
            "source_url": props.get("source_url", ""),
            "source_file_sha256": source_file_sha,
            "source_text_sha256": source_text_sha,
            "text_source": "canonical_notification_xml",
        },
        "legal_time": {
            "commencement_date": spec.effective_date,
            "applicability_start": spec.effective_date,
            "applicability_end": None,
            "retrospective": bool(publication_date and spec.effective_date < publication_date),
            "date_basis": spec.date_basis,
        },
        "system_time": {
            "observed_at": DEFAULT_OBSERVED_AT,
            "compiled_at": DEFAULT_OBSERVED_AT,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": TARGET_WORK,
            "component_id": component_id,
            "anchor_component_id": component_id,
            "anchor_text": f"rule {spec.label}",
            "anchor_hash": sha256_text(component_id),
            "anchor_occurrence": 1,
        },
        "payload": {
            "node_type": "rule",
            "label": label,
            "heading": heading,
            "content": content,
            "structural_text": content,
            "source_text": rule_text,
        },
        "evidence": {
            "source_span": {"start": start, "end": end, "text_hash": sha256_text(rule_text)},
            "excerpt": excerpt,
            "parser_trace": {
                "pattern_id": "canonical_notification_xml_structural_substitute_rule_backfill_v1",
                "confidence": 1.0,
                "paragraph_nums": list(spec.paragraph_nums),
            },
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {
            "required": False,
            "review_reasons": [],
            "reviewed_by": "missing-anchor-backfill",
            "reviewed_at": DEFAULT_OBSERVED_AT,
        },
    }


def _event_for_splice_spec(corpus_dir: Path, spec: SpliceSpec) -> dict[str, Any]:
    xml_path = corpus_dir / spec.xml_path
    root = ET.parse(xml_path).getroot()
    props = _xml_properties(root)
    paragraphs_by_num = _paragraphs_by_num(root)
    selected = [paragraphs_by_num[num] for num in spec.paragraph_nums]
    selected_text = "\n".join(_paragraph_text(paragraph) for paragraph in selected)
    component_id = canonical_rule_id(spec.component_label)
    start, end = _span_bounds(selected)
    document_id = props.get("canonical_id") or f"/in/union/notifications/cbic/central-tax/{Path(spec.xml_path).stem}"
    event_id = spec.replacement_event_id or _stable_splice_event_id(document_id, component_id, start, end, spec)
    publication_date = props.get("publication_date") or props.get("effective_from") or spec.effective_date
    document_text = _document_text(root)
    source_file_sha = props.get("source_sha256") or _sha256_file(xml_path)
    source_text_sha = sha256_text(document_text)
    cleaned = _clean_text(selected_text)
    if not re.search(rf"\brule\s+{re.escape(spec.component_label)}\b", cleaned, flags=re.IGNORECASE):
        raise ValueError(f"Splice paragraph does not target rule {spec.component_label}: {spec.paragraph_nums}")
    if spec.anchor_text not in cleaned:
        raise ValueError(f"Splice paragraph does not contain anchor text {spec.anchor_text!r}: {spec.paragraph_nums}")
    if spec.insert_text.strip(" ,") not in cleaned:
        raise ValueError(f"Splice paragraph does not contain insert text {spec.insert_text!r}: {spec.paragraph_nums}")

    return {
        "event_id": event_id,
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": "SPLICE",
        "source": {
            "document_id": document_id,
            "record_id": props.get("cbic_id", ""),
            "instrument_number": props.get("cbic_no", ""),
            "issuing_authority": props.get("issuing_authority", "/in/authority/cbic"),
            "publication_date": publication_date,
            "source_url": props.get("source_url", ""),
            "source_file_sha256": source_file_sha,
            "source_text_sha256": source_text_sha,
            "text_source": "canonical_notification_xml",
        },
        "legal_time": {
            "commencement_date": spec.effective_date,
            "applicability_start": spec.effective_date,
            "applicability_end": None,
            "retrospective": bool(publication_date and spec.effective_date < publication_date),
            "date_basis": spec.date_basis,
        },
        "system_time": {
            "observed_at": DEFAULT_OBSERVED_AT,
            "compiled_at": DEFAULT_OBSERVED_AT,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": TARGET_WORK,
            "component_id": component_id,
            "anchor_text": spec.anchor_text,
            "anchor_hash": sha256_text(spec.anchor_text),
            "anchor_occurrence": spec.occurrence,
        },
        "payload": {
            "insert_text": spec.insert_text,
            "position": spec.position,
        },
        "evidence": {
            "source_span": {"start": start, "end": end, "text_hash": sha256_text(selected_text)},
            "excerpt": selected_text[:700],
            "parser_trace": {
                "pattern_id": "canonical_notification_xml_splice_backfill_v1",
                "confidence": 1.0,
                "paragraph_nums": list(spec.paragraph_nums),
            },
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {
            "required": False,
            "review_reasons": [],
            "reviewed_by": "missing-anchor-backfill",
            "reviewed_at": DEFAULT_OBSERVED_AT,
        },
    }


def _event_for_text_edit_repair_spec(corpus_dir: Path, spec: TextEditRepairSpec) -> dict[str, Any]:
    xml_path = corpus_dir / spec.xml_path
    root = ET.parse(xml_path).getroot()
    props = _xml_properties(root)
    paragraphs_by_num = _paragraphs_by_num(root)
    selected = [paragraphs_by_num[num] for num in spec.paragraph_nums]
    selected_text = "\n".join(_paragraph_text(paragraph) for paragraph in selected)
    start, end = _span_bounds(selected)
    document_id = props.get("canonical_id") or f"/in/union/notifications/cbic/central-tax/{Path(spec.xml_path).stem}"
    publication_date = props.get("publication_date") or props.get("effective_from") or spec.effective_date
    document_text = _document_text(root)
    source_file_sha = props.get("source_sha256") or _sha256_file(xml_path)
    source_text_sha = sha256_text(document_text)
    if spec.anchor_text and spec.anchor_text not in selected_text:
        normalized = _clean_text(selected_text)
        if spec.anchor_text not in normalized:
            raise ValueError(
                f"Repair paragraph does not contain anchor text {spec.anchor_text!r}: {spec.xml_path} {spec.paragraph_nums}"
            )

    payload = dict(spec.payload)
    return {
        "event_id": spec.event_id,
        "event_type": "TEXTUAL_AMENDMENT",
        "operation": spec.operation,
        "source": {
            "document_id": document_id,
            "record_id": props.get("cbic_id", ""),
            "instrument_number": props.get("cbic_no", ""),
            "issuing_authority": props.get("issuing_authority", "/in/authority/cbic"),
            "publication_date": publication_date,
            "source_url": props.get("source_url", ""),
            "source_file_sha256": source_file_sha,
            "source_text_sha256": source_text_sha,
            "text_source": "canonical_notification_xml",
        },
        "legal_time": {
            "commencement_date": spec.effective_date,
            "applicability_start": spec.effective_date,
            "applicability_end": None,
            "retrospective": bool(publication_date and spec.effective_date < publication_date),
            "date_basis": spec.date_basis,
        },
        "system_time": {
            "observed_at": DEFAULT_OBSERVED_AT,
            "compiled_at": DEFAULT_OBSERVED_AT,
            "compiler_version": COMPILER_VERSION,
        },
        "target": {
            "work_id": TARGET_WORK,
            "component_id": spec.component_id,
            "anchor_component_id": spec.component_id,
            "anchor_text": spec.anchor_text,
            "anchor_hash": sha256_text(spec.anchor_text or spec.component_id),
            "anchor_occurrence": spec.anchor_occurrence,
        },
        "payload": payload,
        "evidence": {
            "source_span": {"start": start, "end": end, "text_hash": sha256_text(selected_text)},
            "excerpt": selected_text[:700],
            "parser_trace": {
                "pattern_id": "canonical_notification_xml_materializer_repair_v1",
                "confidence": 1.0,
                "paragraph_nums": list(spec.paragraph_nums),
            },
        },
        "validation": {
            "target_resolved": True,
            "anchor_resolved": True,
            "date_resolved": True,
            "source_span_verified": True,
            "materializable": True,
        },
        "status": "validated",
        "review": {
            "required": False,
            "review_reasons": [],
            "reviewed_by": "missing-anchor-backfill",
            "reviewed_at": DEFAULT_OBSERVED_AT,
        },
    }


def build_missing_anchor_backfill_events(
    *,
    corpus_dir: Path = Path("corpus"),
    specs: tuple[BackfillSpec, ...] = BACKFILL_SPECS,
    substitute_specs: tuple[StructuralSubstituteSpec, ...] = STRUCTURAL_SUBSTITUTE_SPECS,
    splice_specs: tuple[SpliceSpec, ...] = SPLICE_SPECS,
    omit_specs: tuple[WholeRuleOmitSpec, ...] = WHOLE_RULE_OMIT_SPECS,
    text_edit_repair_specs: tuple[TextEditRepairSpec, ...] = TEXT_EDIT_REPAIR_SPECS,
) -> list[dict[str, Any]]:
    events = [_event_for_spec(corpus_dir, spec) for spec in specs]
    for spec in omit_specs:
        events.extend(_events_for_whole_rule_omit_spec(corpus_dir, spec))
    events.extend(_event_for_structural_substitute_spec(corpus_dir, spec) for spec in substitute_specs)
    events.extend(_event_for_splice_spec(corpus_dir, spec) for spec in splice_specs)
    events.extend(_event_for_text_edit_repair_spec(corpus_dir, spec) for spec in text_edit_repair_specs)
    return events


def write_missing_anchor_backfill_events(
    *,
    corpus_dir: Path,
    output: Path,
    report_output: Path | None = None,
) -> dict[str, Any]:
    events = build_missing_anchor_backfill_events(corpus_dir=corpus_dir)
    write_jsonl(events, output)
    report = {
        "ok": True,
        "compiler_version": COMPILER_VERSION,
        "corpus_dir": str(corpus_dir),
        "output": str(output),
        "event_count": len(events),
        "components": [event["target"]["component_id"] for event in events],
        "event_ids": [event["event_id"] for event in events],
        "source_documents": sorted({event["source"]["document_id"] for event in events}),
    }
    if report_output:
        report_output.parent.mkdir(parents=True, exist_ok=True)
        report_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        report["report_output"] = str(report_output)
    return report


__all__ = [
    "BACKFILL_SPECS",
    "BackfillSpec",
    "SPLICE_SPECS",
    "SpliceSpec",
    "STRUCTURAL_SUBSTITUTE_SPECS",
    "StructuralSubstituteSpec",
    "TEXT_EDIT_REPAIR_SPECS",
    "TextEditRepairSpec",
    "WHOLE_RULE_OMIT_SPECS",
    "WholeRuleOmitSpec",
    "build_missing_anchor_backfill_events",
    "write_missing_anchor_backfill_events",
]
