"""Reference-resolution reports for canonical corpus planning."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            props[name] = prop.attrib.get("value", "")
    return props


def _known_ids(root: ET.Element, document_id: str) -> set[str]:
    ids = {document_id}
    for node in root.findall(".//*[@refersTo]"):
        refers_to = node.attrib.get("refersTo")
        if refers_to:
            ids.add(refers_to)
    return ids


def _references(path: Path, root: ET.Element, document_id: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for node in root.findall(".//ref") + root.findall(".//textualMod"):
        href = node.attrib.get("href", "")
        if not href.startswith("/in/"):
            continue
        refs.append(
            {
                "target": href,
                "source_document": document_id,
                "source_path": path.as_posix(),
                "source_eid": node.attrib.get("eId", ""),
                "type": node.attrib.get("type", "REFERS_TO"),
                "showAs": node.attrib.get("showAs", href),
            }
        )
    return refs


def _target_kind(target: str) -> str:
    parts = [part for part in target.split("/") if part]
    if len(parts) >= 4 and parts[2] == "acts":
        return "act_section" if "section" in parts else "act"
    if len(parts) >= 4 and parts[2] == "rules":
        return "rule"
    if len(parts) >= 3 and parts[2] == "forms":
        return "form"
    if len(parts) >= 3 and parts[2] == "notifications":
        return "notification"
    return "other"


def _target_document(target: str) -> str:
    parts = [part for part in target.split("/") if part]
    if len(parts) >= 4 and parts[2] == "acts":
        return "/" + "/".join(parts[:4])
    if len(parts) >= 4 and parts[2] == "rules":
        return "/" + "/".join(parts[:4])
    if len(parts) >= 3 and parts[2] == "forms":
        return "/" + "/".join(parts[:4]) if len(parts) >= 4 else target
    if len(parts) >= 3 and parts[2] == "notifications":
        return "/" + "/".join(parts[: min(len(parts), 7)])
    return target


def build_unresolved_reference_report(corpus_dir: Path, *, sample_limit: int = 5) -> dict[str, Any]:
    """Summarize unresolved canonical references and their source documents."""
    known_ids: set[str] = set()
    references: list[dict[str, str]] = []
    documents = 0

    for path in sorted(corpus_dir.rglob("*.xml")):
        root = ET.parse(path).getroot()
        props = _properties(root)
        document_id = props.get("canonical_id", "")
        if not document_id:
            continue
        documents += 1
        known_ids.update(_known_ids(root, document_id))
        references.extend(_references(path, root, document_id))

    unresolved = [ref for ref in references if ref["target"] not in known_ids]
    occurrence_counts = Counter(ref["target"] for ref in unresolved)
    target_sources: dict[str, Counter[str]] = defaultdict(Counter)
    target_samples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for ref in unresolved:
        target = ref["target"]
        target_sources[target][ref["source_document"]] += 1
        if len(target_samples[target]) < sample_limit:
            target_samples[target].append(ref)

    targets: list[dict[str, Any]] = []
    for target, occurrences in occurrence_counts.most_common():
        source_counts = target_sources[target]
        targets.append(
            {
                "target": target,
                "kind": _target_kind(target),
                "target_document": _target_document(target),
                "occurrences": occurrences,
                "source_documents": len(source_counts),
                "top_sources": [
                    {"source_document": source, "occurrences": count}
                    for source, count in source_counts.most_common(sample_limit)
                ],
                "samples": target_samples[target],
            }
        )

    kind_counts = Counter(item["kind"] for item in targets)
    document_counts = Counter(item["target_document"] for item in targets)
    return {
        "profile": "git-for-law-unresolved-reference-report-v1",
        "corpus_dir": str(corpus_dir),
        "stats": {
            "documents": documents,
            "references": len(references),
            "unresolved_occurrences": len(unresolved),
            "unresolved_targets": len(targets),
            "kinds": dict(sorted(kind_counts.items())),
        },
        "top_target_documents": [
            {"target_document": target_document, "unresolved_targets": count}
            for target_document, count in document_counts.most_common(25)
        ],
        "targets": targets,
    }


def write_unresolved_reference_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
