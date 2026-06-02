"""Render canonical Akoma Ntoso-compatible XML documents."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def canonical_rule_id(rule_label: str) -> str:
    return f"/in/union/rules/cgst-rules-2017/rule/{rule_label.lower()}"


def canonical_subrule_id(rule_label: str, subrule_label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", subrule_label).lower()
    return f"{canonical_rule_id(rule_label)}/subrule/{clean}"


def canonical_form_id(form_number: str) -> str:
    slug = form_number.lower().replace(" ", "-").replace("_", "-")
    return f"/in/union/forms/{slug}"


def canonicalize_legacy_reference(reference: str) -> str:
    """Convert prototype IDs into India profile canonical IDs when possible."""
    rule_match = re.fullmatch(r"CGST_Rules/Rule_([0-9A-Za-z]+)(?:/SubRule_([0-9A-Za-z]+))?", reference)
    if rule_match:
        rule_label, subrule_label = rule_match.groups()
        canonical = canonical_rule_id(rule_label)
        if subrule_label:
            canonical = f"{canonical}/subrule/{subrule_label.lower()}"
        return canonical

    form_match = re.fullmatch(r"FORM_GST_([0-9A-Za-z_]+)", reference)
    if form_match:
        form_number = "GST " + form_match.group(1).replace("_", "-")
        return canonical_form_id(form_number)

    act_match = re.fullmatch(r"([A-Z]+)_Act_2017/Section_([0-9A-Za-z]+)", reference)
    if act_match:
        act_prefix, section = act_match.groups()
        return f"/in/union/acts/{act_prefix.lower()}-act-2017/section/{section.lower()}"

    return reference


def safe_eid(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^0-9a-z]+", "_", value)
    return value.strip("_") or "node"


def _metadata(parent: ET.Element, metadata: dict[str, Any]) -> None:
    meta = ET.SubElement(parent, "meta")
    identification = ET.SubElement(meta, "identification", {"source": "#git-for-law"})
    work = ET.SubElement(identification, "FRBRWork")
    ET.SubElement(work, "FRBRthis", {"value": metadata.get("canonical_id", "")})
    ET.SubElement(work, "FRBRuri", {"value": metadata.get("work_uri", metadata.get("canonical_id", ""))})
    if metadata.get("effective_from"):
        ET.SubElement(work, "FRBRdate", {"date": metadata["effective_from"], "name": "effective"})
    if metadata.get("publication_date"):
        ET.SubElement(work, "FRBRdate", {"date": metadata["publication_date"], "name": "publication"})
    ET.SubElement(work, "FRBRauthor", {"href": metadata.get("issuing_authority", "/in/authority/unknown")})
    ET.SubElement(work, "FRBRcountry", {"value": "in"})

    proprietary = ET.SubElement(meta, "proprietary", {"source": "#git-for-law"})
    for key, value in sorted(metadata.items()):
        if isinstance(value, (str, int, float)) or value is None:
            ET.SubElement(proprietary, "property", {"name": key, "value": "" if value is None else str(value)})


def _content_text(parent: ET.Element, text: str) -> None:
    content = ET.SubElement(parent, "content")
    paragraph = ET.SubElement(content, "p")
    paragraph.text = text or ""


def _add_references(parent: ET.Element, edges: list[dict[str, Any]] | None) -> None:
    if not edges:
        return
    refs = ET.SubElement(parent, "references")
    parent_eid = parent.attrib.get("eId", "node")
    for index, edge in enumerate(edges, start=1):
        target = edge.get("target", "")
        href = canonicalize_legacy_reference(target)
        ET.SubElement(
            refs,
            "ref",
            {
                "eId": f"{parent_eid}__ref_{index}",
                "href": href,
                "showAs": edge.get("showAs", target),
                "type": edge.get("type", "REFERS_TO"),
            },
        )


def _references_for_node(node: dict[str, Any], references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start = node.get("start")
    end = node.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return []
    edges = []
    for reference in references:
        ref_start = reference.get("start")
        ref_end = reference.get("end")
        if not isinstance(ref_start, int) or not isinstance(ref_end, int):
            continue
        if start <= ref_start and ref_end <= end:
            target = reference.get("target", "")
            edges.append(
                {
                    "target": target,
                    "type": reference.get("type", "REFERS_TO"),
                    "showAs": reference.get("showAs", target),
                }
            )
    return edges


def _source_attrs(node: dict[str, Any]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    start = node.get("start")
    end = node.get("end")
    if isinstance(start, int) and isinstance(end, int):
        attrs["sourceStart"] = str(start)
        attrs["sourceEnd"] = str(end)
    if node.get("text_hash"):
        attrs["sourceHash"] = str(node["text_hash"])
    if node.get("type"):
        attrs["sourceNodeType"] = str(node["type"])
    confidence = node.get("confidence")
    if isinstance(confidence, (int, float)):
        attrs["sourceConfidence"] = str(confidence)
    return attrs


def _structure_blocks(
    text: str,
    structure: dict[str, Any] | None,
) -> list[tuple[str, str, list[dict[str, Any]], dict[str, str], str]]:
    nodes = structure.get("nodes", []) if structure else []
    references = structure.get("references", []) if structure else []
    blocks: list[tuple[str, str, list[dict[str, Any]], dict[str, str], str]] = []
    if nodes:
        for index, node in enumerate(nodes, start=1):
            start = node.get("start")
            end = node.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            label = node.get("label") or str(index)
            blocks.append(
                (
                    str(label),
                    text[start:end].strip(),
                    _references_for_node(node, references),
                    _source_attrs(node),
                    str(node.get("type", "")),
                )
            )
        return blocks
    for index, block in enumerate([part.strip() for part in text.split("\n\n") if part.strip()], start=1):
        blocks.append((str(index), block, [], {}, "paragraph"))
    return blocks


def _section_id(document_id: str, label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", label).lower()
    return f"{document_id}/section/{clean}"


def _rule_id(document_id: str, label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", label).lower()
    return f"{document_id}/rule/{clean}"


def render_source_document(
    text: str,
    metadata: dict[str, Any],
    structure: dict[str, Any] | None = None,
) -> ET.ElementTree:
    """Render a generic source archive into the India profile XML subset."""
    document_type = metadata.get("document_type", "document") or "document"
    metadata = {
        "document_type": document_type,
        "canonical_id": metadata.get("canonical_id", "/in/union/documents/unknown"),
        "title": metadata.get("title", "Untitled document"),
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "source_type": "source-text",
        "review_status": "extracted",
        "parser_version": structure.get("parser", "unstructured-v1") if structure else "unstructured-v1",
        **metadata,
    }

    root = ET.Element("akomaNtoso")
    if document_type in {"act", "rules"}:
        doc = ET.SubElement(root, "act", {"name": safe_eid(metadata.get("title", document_type))})
        body = ET.SubElement(doc, "body")
    else:
        doc = ET.SubElement(root, "doc", {"name": safe_eid(document_type)})
        body = ET.SubElement(doc, "mainBody")
    _metadata(doc, metadata)

    document_id = metadata.get("canonical_id", "")
    section_eid_counts: dict[str, int] = {}
    rule_eid_counts: dict[str, int] = {}
    for index, (label, block_text, references, source_attrs, source_node_type) in enumerate(
        _structure_blocks(text, structure), start=1
    ):
        if document_type == "act" and source_node_type == "section":
            base_eid = f"section_{safe_eid(label)}"
            section_eid_counts[base_eid] = section_eid_counts.get(base_eid, 0) + 1
            section_eid = base_eid if section_eid_counts[base_eid] == 1 else f"{base_eid}_{section_eid_counts[base_eid]}"
            section_attrs = {
                "eId": section_eid,
                "refersTo": _section_id(document_id, label),
                **source_attrs,
            }
            section = ET.SubElement(body, "section", section_attrs)
            ET.SubElement(section, "num").text = label
            _content_text(section, block_text)
            _add_references(section, references)
            continue

        if document_type == "rules" and source_node_type == "rule":
            base_eid = f"rule_{safe_eid(label)}"
            rule_eid_counts[base_eid] = rule_eid_counts.get(base_eid, 0) + 1
            rule_eid = base_eid if rule_eid_counts[base_eid] == 1 else f"{base_eid}_{rule_eid_counts[base_eid]}"
            rule_attrs = {
                "eId": rule_eid,
                "refersTo": _rule_id(document_id, label),
                **source_attrs,
            }
            article = ET.SubElement(body, "article", rule_attrs)
            ET.SubElement(article, "num").text = label
            _content_text(article, block_text)
            _add_references(article, references)
            continue

        paragraph_attrs = {"eId": f"{safe_eid(document_type)}__para_{index}", **source_attrs}
        paragraph = ET.SubElement(body, "paragraph", paragraph_attrs)
        ET.SubElement(paragraph, "num").text = label
        _content_text(paragraph, block_text)
        _add_references(paragraph, references)

    modifications = None
    for index, amendment in enumerate(metadata.get("amendments", []) or [], start=1):
        if modifications is None:
            modifications = ET.SubElement(body, "modifications")
        ET.SubElement(
            modifications,
            "textualMod",
            {
                "eId": f"mod_{index}",
                "type": amendment.get("operation", ""),
                "href": canonicalize_legacy_reference(amendment.get("target", "")),
            },
        )

    return ET.ElementTree(root)


def render_rule(rule: dict[str, Any], chapter: dict[str, Any], metadata: dict[str, Any]) -> ET.ElementTree:
    rule_label = rule["label"]
    metadata = {
        "document_type": "rule",
        "canonical_id": canonical_rule_id(rule_label),
        "title": rule.get("heading", ""),
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "source_type": "seed-json",
        "review_status": "seeded",
        "parser_version": "seed-json-v1",
        **metadata,
    }

    root = ET.Element("akomaNtoso")
    doc = ET.SubElement(root, "act", {"name": "cgst-rules-2017"})
    _metadata(doc, metadata)
    body = ET.SubElement(doc, "body")
    chapter_el = ET.SubElement(body, "chapter", {"eId": f"chp_{safe_eid(chapter.get('number', ''))}"})
    ET.SubElement(chapter_el, "num").text = chapter.get("number", "")
    ET.SubElement(chapter_el, "heading").text = chapter.get("title", "")

    article = ET.SubElement(
        chapter_el,
        "article",
        {"eId": f"rule_{safe_eid(rule_label)}", "refersTo": metadata["canonical_id"]},
    )
    ET.SubElement(article, "num").text = f"{rule_label}."
    ET.SubElement(article, "heading").text = rule.get("heading", "")
    if rule.get("content"):
        intro = ET.SubElement(article, "intro")
        _content_text(intro, rule["content"])

    for subrule in rule.get("subrules", []):
        subrule_id = canonical_subrule_id(rule_label, subrule["label"])
        paragraph = ET.SubElement(
            article,
            "paragraph",
            {"eId": f"rule_{safe_eid(rule_label)}__subrule_{safe_eid(subrule['label'])}", "refersTo": subrule_id},
        )
        ET.SubElement(paragraph, "num").text = subrule["label"]
        _content_text(paragraph, subrule.get("content", ""))

        for proviso in subrule.get("provisos", []):
            proviso_el = ET.SubElement(
                paragraph,
                "proviso",
                {"eId": f"{paragraph.attrib['eId']}__proviso_{proviso.get('ordinal', 1)}"},
            )
            if proviso.get("label"):
                ET.SubElement(proviso_el, "heading").text = proviso["label"]
            _content_text(proviso_el, proviso.get("content", ""))

        for clause in subrule.get("clauses", []):
            clause_el = ET.SubElement(
                paragraph,
                "subparagraph",
                {"eId": f"{paragraph.attrib['eId']}__clause_{safe_eid(clause.get('label', ''))}"},
            )
            ET.SubElement(clause_el, "num").text = clause.get("label", "")
            _content_text(clause_el, clause.get("content", ""))

        _add_references(paragraph, subrule.get("edges", []))

    return ET.ElementTree(root)


def render_form(form: dict[str, Any], metadata: dict[str, Any]) -> ET.ElementTree:
    metadata = {
        "document_type": "form",
        "canonical_id": canonical_form_id(form["form_number"]),
        "title": form.get("title", ""),
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "source_type": "seed-json",
        "review_status": "seeded",
        "parser_version": "seed-json-v1",
        **metadata,
    }

    root = ET.Element("akomaNtoso")
    doc = ET.SubElement(root, "doc", {"name": "form"})
    _metadata(doc, metadata)
    body = ET.SubElement(doc, "mainBody")
    form_el = ET.SubElement(body, "block", {"name": "form", "eId": safe_eid(form["form_number"])})
    ET.SubElement(form_el, "heading").text = form.get("title", "")
    ET.SubElement(form_el, "num").text = form["form_number"]

    for section in form.get("sections", []):
        section_el = ET.SubElement(
            form_el,
            "block",
            {"name": "form-section", "eId": safe_eid(section["section_id"])},
        )
        ET.SubElement(section_el, "num").text = section.get("section_label", "")
        ET.SubElement(section_el, "heading").text = section.get("heading", "")
        if section.get("description"):
            _content_text(section_el, section["description"])
        schema = ET.SubElement(section_el, "embeddedStructure", {"name": "json-schema"})
        schema.text = section.get("schema_payload_json", "")

    return ET.ElementTree(root)


def render_notification(text: str, metadata: dict[str, Any], structure: dict[str, Any] | None = None) -> ET.ElementTree:
    metadata = {
        "document_type": "notification",
        "canonical_id": "/in/union/notifications/cbic/central-tax/unknown",
        "title": "Notification",
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "source_type": "source-text",
        "review_status": "extracted",
        "parser_version": "deterministic-paragraph-v1",
        **metadata,
    }

    root = ET.Element("akomaNtoso")
    doc = ET.SubElement(root, "doc", {"name": "notification"})
    _metadata(doc, metadata)
    body = ET.SubElement(doc, "mainBody")

    nodes = structure.get("nodes", []) if structure else []
    if nodes:
        for index, node in enumerate(nodes, start=1):
            block_text = text[node["start"] : node["end"]].strip()
            paragraph = ET.SubElement(body, "paragraph", {"eId": f"para_{index}"})
            ET.SubElement(paragraph, "num").text = node.get("label", str(index))
            _content_text(paragraph, block_text)
    else:
        for index, block in enumerate([part.strip() for part in text.split("\n\n") if part.strip()], start=1):
            paragraph = ET.SubElement(body, "paragraph", {"eId": f"para_{index}"})
            ET.SubElement(paragraph, "num").text = str(index)
            _content_text(paragraph, block)

    for index, amendment in enumerate(metadata.get("amendments", []) or [], start=1):
        modifications = body.find("modifications")
        if modifications is None:
            modifications = ET.SubElement(body, "modifications")
        ET.SubElement(
            modifications,
            "textualMod",
            {
                "eId": f"mod_{index}",
                "type": amendment.get("operation", ""),
                "href": canonicalize_legacy_reference(amendment.get("target", "")),
            },
        )

    return ET.ElementTree(root)


def write_xml(tree: ET.ElementTree, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path
