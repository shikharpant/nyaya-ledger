"""Quality reporting for generated corpus XML."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


JOINED_TOKEN_RE = re.compile(
    r"\b(?:of|and|or|to|for|from|in|on|by|the)[A-Z][A-Za-z0-9]*\b|\bNo[A-Z]{2,}\b|\bFORMGST[A-Z0-9]*\b"
)


def _properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            props[name] = prop.attrib.get("value", "")
    return props


def _node_text(node: ET.Element) -> str:
    return " ".join(part.strip() for part in node.itertext() if part and part.strip())


def audit_corpus_quality(corpus_dir: Path, *, max_paragraph_chars: int = 2000) -> dict[str, Any]:
    """Build review metrics for XML generated from source documents."""
    documents: list[dict[str, Any]] = []
    totals = {
        "documents": 0,
        "paragraphs": 0,
        "long_paragraphs": 0,
        "joined_token_hits": 0,
        "references": 0,
    }

    for path in sorted(corpus_dir.rglob("*.xml")):
        tree = ET.parse(path)
        root = tree.getroot()
        props = _properties(root)
        paragraphs = root.findall(".//p")
        paragraph_lengths = [len(_node_text(node)) for node in paragraphs if _node_text(node)]
        long_paragraphs = sum(1 for length in paragraph_lengths if length > max_paragraph_chars)
        text = _node_text(root)
        joined_hits = len(JOINED_TOKEN_RE.findall(text))
        references = len(root.findall(".//ref"))

        totals["documents"] += 1
        totals["paragraphs"] += len(paragraph_lengths)
        totals["long_paragraphs"] += long_paragraphs
        totals["joined_token_hits"] += joined_hits
        totals["references"] += references

        documents.append(
            {
                "path": str(path),
                "canonical_id": props.get("canonical_id", ""),
                "document_type": props.get("document_type", ""),
                "paragraphs": len(paragraph_lengths),
                "max_paragraph_chars": max(paragraph_lengths, default=0),
                "long_paragraphs": long_paragraphs,
                "joined_token_hits": joined_hits,
                "references": references,
            }
        )

    flagged = [
        doc
        for doc in documents
        if doc["long_paragraphs"] > 0 or doc["joined_token_hits"] > 0 or doc["paragraphs"] == 0
    ]
    flagged.sort(key=lambda doc: (doc["long_paragraphs"], doc["joined_token_hits"], doc["max_paragraph_chars"]), reverse=True)

    return {
        "profile": "git-for-law-corpus-quality-v1",
        "corpus_dir": str(corpus_dir),
        "max_paragraph_chars": max_paragraph_chars,
        "stats": totals,
        "flagged_documents": flagged,
        "documents": documents,
    }


def write_quality_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
