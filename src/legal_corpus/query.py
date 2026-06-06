"""Corpus-native lookup and text export for canonical legal XML."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .references import CorpusReferenceResolver, build_reference_resolver, normalize_reference_target
from .renderer import canonicalize_legacy_reference


TEXT_TAGS = {"num", "heading", "p"}
REFERENCE_TAGS = {"ref", "textualMod"}


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _properties(root: ET.Element) -> dict[str, str]:
    props = {}
    for element in root.iter():
        if _local_name(element.tag) != "property":
            continue
        name = element.attrib.get("name")
        if name:
            props[name] = element.attrib.get("value", "")
    return props


def _first_child_text(element: ET.Element, tag_name: str) -> str:
    for child in element:
        if _local_name(child.tag) == tag_name:
            return _clean_text("".join(child.itertext()))
    return ""


def _body(root: ET.Element) -> ET.Element:
    for element in root.iter():
        if _local_name(element.tag) in {"body", "mainBody"}:
            return element
    return root


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _element_text(element: ET.Element) -> str:
    lines: list[str] = []
    for child in element.iter():
        if _local_name(child.tag) not in TEXT_TAGS:
            continue
        text = _clean_text("".join(child.itertext()))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _references(element: ET.Element, resolver: CorpusReferenceResolver | None = None) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for child in element.iter():
        if _local_name(child.tag) not in REFERENCE_TAGS:
            continue
        target = child.attrib.get("href")
        if not target:
            continue
        normalized_target = normalize_reference_target(target, resolver)
        item = {
            "target": normalized_target,
            "type": child.attrib.get("type", "REFERS_TO"),
            "showAs": child.attrib.get("showAs", target),
            "eId": child.attrib.get("eId", ""),
        }
        if normalized_target != target:
            item["originalTarget"] = target
        key = (item["target"], item["type"], item["eId"])
        if key not in seen:
            references.append(item)
            seen.add(key)
    return references


def _source_span(element: ET.Element) -> dict[str, str]:
    keys = ["sourceStart", "sourceEnd", "sourceHash", "sourceNodeType", "sourceConfidence"]
    return {key: element.attrib[key] for key in keys if key in element.attrib}


def _source_spans(element: ET.Element) -> list[dict[str, str]]:
    spans: list[dict[str, str]] = []
    for child in element.iter():
        span = _source_span(child)
        if span:
            span["eId"] = child.attrib.get("eId", "")
            span["element_tag"] = _local_name(child.tag)
            spans.append(span)
    return spans


def _child_canonical_ids(element: ET.Element, own_id: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for child in element.iter():
        child_id = child.attrib.get("refersTo")
        if not child_id or child_id == own_id or child_id in seen:
            continue
        ids.append(child_id)
        seen.add(child_id)
    return ids


def _provision_score(provision: dict[str, Any]) -> tuple[int, int]:
    text = provision.get("text", "")
    number = re.escape(str(provision.get("number", "")).strip())
    has_explicit_heading = 0
    if number and re.search(rf"\bsection\s+{number}\b", text, flags=re.IGNORECASE):
        has_explicit_heading = 2
    elif number and re.search(rf"\brule\s+{number}\b", text, flags=re.IGNORECASE):
        has_explicit_heading = 2
    elif "substituted" not in text.lower() and "inserted by" not in text.lower() and "omitted" not in text.lower():
        has_explicit_heading = 1
    return (has_explicit_heading, len(text))


def _add_role(entry: dict[str, Any], role: str) -> None:
    roles = entry.setdefault("roles", [])
    if role not in roles:
        roles.append(role)


def _entry(index: dict[str, dict[str, Any]], canonical_id: str) -> dict[str, Any]:
    return index.setdefault(canonical_id, {"canonical_id": canonical_id, "roles": []})


def normalize_query_id(canonical_id: str) -> str:
    """Accept canonical IDs and known legacy prototype IDs."""
    value = canonical_id.strip()
    if value.startswith("/"):
        return value
    return canonicalize_legacy_reference(value)


def build_corpus_lookup(corpus_dir: Path) -> dict[str, dict[str, Any]]:
    """Build an in-memory lookup keyed by canonical document/provision ID."""
    index: dict[str, dict[str, Any]] = {}
    resolver = build_reference_resolver(corpus_dir)

    for path in sorted(corpus_dir.rglob("*.xml")):
        tree = ET.parse(path)
        root = tree.getroot()
        props = _properties(root)
        document_id = props.get("canonical_id")
        if not document_id:
            continue

        body = _body(root)
        document_entry = _entry(index, document_id)
        _add_role(document_entry, "document")
        document_entry["document"] = {
            "canonical_id": document_id,
            "document_type": props.get("document_type", ""),
            "title": props.get("title", ""),
            "path": str(path),
            "effective_from": props.get("effective_from", ""),
            "publication_date": props.get("publication_date", ""),
            "review_status": props.get("review_status", ""),
            "source_sha256": props.get("source_sha256", ""),
            "properties": props,
            "text": _element_text(body),
            "children": _child_canonical_ids(body, document_id),
            "references": _references(body, resolver),
            "source_spans": _source_spans(body),
        }

        for element in root.iter():
            provision_id = element.attrib.get("refersTo")
            if not provision_id:
                continue
            provision_entry = _entry(index, provision_id)
            _add_role(provision_entry, "provision")
            provision = {
                "canonical_id": provision_id,
                "document_id": document_id,
                "document_type": props.get("document_type", ""),
                "document_title": props.get("title", ""),
                "path": str(path),
                "element_tag": _local_name(element.tag),
                "eId": element.attrib.get("eId", ""),
                "number": _first_child_text(element, "num"),
                "title": _first_child_text(element, "heading"),
                "text": _element_text(element),
                "children": _child_canonical_ids(element, provision_id),
                "references": _references(element, resolver),
                "source_span": _source_span(element),
            }
            existing = provision_entry.get("provision")
            if not existing or _provision_score(provision) > _provision_score(existing):
                provision_entry["provision"] = provision

    return index


def list_documents(corpus_dir: Path, document_type: str | None = None) -> list[dict[str, Any]]:
    index = build_corpus_lookup(corpus_dir)
    documents = [entry["document"] for entry in index.values() if "document" in entry]
    if document_type:
        documents = [document for document in documents if document.get("document_type") == document_type]
    return sorted(documents, key=lambda document: document["canonical_id"])


def lookup_canonical_id(corpus_dir: Path, canonical_id: str) -> dict[str, Any] | None:
    index = build_corpus_lookup(corpus_dir)
    return index.get(normalize_query_id(canonical_id))


def entry_text(entry: dict[str, Any], role: str = "auto") -> str:
    if role not in {"auto", "document", "provision"}:
        raise ValueError(f"Unsupported role: {role}")
    if role == "document":
        document = entry.get("document")
        return document.get("text", "") if document else ""
    if role == "provision":
        provision = entry.get("provision")
        return provision.get("text", "") if provision else ""
    if entry.get("document"):
        return entry["document"].get("text", "")
    if entry.get("provision"):
        return entry["provision"].get("text", "")
    return ""


def export_text(corpus_dir: Path, canonical_id: str, role: str = "auto") -> str | None:
    entry = lookup_canonical_id(corpus_dir, canonical_id)
    if not entry:
        return None
    if role != "auto" and role not in entry:
        return None
    return entry_text(entry, role=role)
