"""API-ready payload export rebuilt from canonical corpus XML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .query import build_corpus_lookup


def _document_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_id": document["canonical_id"],
        "document_type": document.get("document_type", ""),
        "title": document.get("title", ""),
        "path": document.get("path", ""),
        "effective_from": document.get("effective_from", ""),
        "publication_date": document.get("publication_date", ""),
        "review_status": document.get("review_status", ""),
        "source_sha256": document.get("source_sha256", ""),
        "children": document.get("children", []),
        "references": document.get("references", []),
        "source_spans": document.get("source_spans", []),
        "text": document.get("text", ""),
        "metadata": document.get("properties", {}),
    }


def _provision_payload(provision: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_id": provision["canonical_id"],
        "document_id": provision.get("document_id", ""),
        "document_type": provision.get("document_type", ""),
        "document_title": provision.get("document_title", ""),
        "path": provision.get("path", ""),
        "element_tag": provision.get("element_tag", ""),
        "eId": provision.get("eId", ""),
        "number": provision.get("number", ""),
        "title": provision.get("title", ""),
        "children": provision.get("children", []),
        "references": provision.get("references", []),
        "source_span": provision.get("source_span", {}),
        "text": provision.get("text", ""),
    }


def build_api_payload(corpus_dir: Path) -> dict[str, Any]:
    lookup = build_corpus_lookup(corpus_dir)
    documents = []
    provisions = []
    for canonical_id, entry in sorted(lookup.items()):
        if entry.get("document"):
            documents.append(_document_payload(entry["document"]))
        if entry.get("provision"):
            provisions.append(_provision_payload(entry["provision"]))

    references = []
    for document in documents:
        for reference in document.get("references", []):
            references.append(
                {
                    "source": document["canonical_id"],
                    "source_role": "document",
                    **reference,
                }
            )
    for provision in provisions:
        for reference in provision.get("references", []):
            references.append(
                {
                    "source": provision["canonical_id"],
                    "source_role": "provision",
                    **reference,
                }
            )

    return {
        "profile": "git-for-law-india-v1",
        "stats": {
            "documents": len(documents),
            "provisions": len(provisions),
            "references": len(references),
        },
        "documents": documents,
        "provisions": provisions,
        "references": references,
    }


def write_api_payload(corpus_dir: Path, output_path: Path) -> dict[str, Any]:
    payload = build_api_payload(corpus_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload
