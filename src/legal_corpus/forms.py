"""Split aggregate GST forms source archives into canonical form documents."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .paths import expected_corpus_relative_path
from .renderer import canonicalize_legacy_reference, render_source_document, write_xml
from .source_archive import read_metadata_yaml
from .structure_parser import text_hash


FORM_HEADING_RE = re.compile(r"\bFORM\s+GST\s+([A-Z]{2,5})\s*-?\s*([0-9]{1,3}[A-Z]?)\b", re.IGNORECASE)


def _form_id(text: str) -> str | None:
    match = FORM_HEADING_RE.search(text)
    if not match:
        return None
    form_ref = f"{match.group(1)}_{match.group(2)}".upper()
    return canonicalize_legacy_reference(f"FORM_GST_{form_ref}")


def _form_title(text: str, canonical_id: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line or canonical_id.rsplit("/", 1)[-1].upper()


def _form_score(text: str) -> tuple[int, int]:
    dot_leader_penalty = 1 if re.search(r"\.{8,}", text) else 0
    has_body = 1 if len(text.splitlines()) > 2 else 0
    return (has_body - dot_leader_penalty, len(text))


def split_forms_archive(source_dir: Path, corpus_dir: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Render each detected FORM GST block as its own canonical form XML file."""
    extracted = json.loads((source_dir / "extracted_text.json").read_text(encoding="utf-8"))
    structure = json.loads((source_dir / "structure.json").read_text(encoding="utf-8"))
    archive_metadata = read_metadata_yaml(source_dir / "metadata.yaml")
    text = extracted.get("text", "")

    candidates: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    for node in structure.get("nodes", []):
        if node.get("type") != "form":
            continue
        start = node.get("start")
        end = node.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        form_text = text[start:end].strip()
        canonical_id = _form_id(form_text)
        if not canonical_id:
            skipped.append({"reason": "missing form heading", "label": str(node.get("label", ""))})
            continue
        candidate = {"node": node, "text": form_text, "start": start, "end": end, "score": _form_score(form_text)}
        existing = candidates.get(canonical_id)
        if existing and existing["score"] >= candidate["score"]:
            skipped.append({"reason": "weaker duplicate form heading", "canonical_id": canonical_id})
            continue
        if existing:
            skipped.append({"reason": "replaced weaker duplicate form heading", "canonical_id": canonical_id})
        candidates[canonical_id] = candidate

    generated: list[dict[str, str]] = []
    for canonical_id, candidate in sorted(candidates.items()):
        node = candidate["node"]
        start = candidate["start"]
        end = candidate["end"]
        form_text = candidate["text"]
        output_path = corpus_dir / expected_corpus_relative_path(canonical_id, "form")
        if output_path.exists() and not overwrite:
            skipped.append({"reason": "output exists", "canonical_id": canonical_id, "path": str(output_path)})
            continue

        form_node = {
            "type": "form",
            "label": canonical_id.rsplit("/", 1)[-1].upper(),
            "start": start,
            "end": end,
            "text_hash": text_hash(text[start:end]),
            "confidence": node.get("confidence", 1.0),
        }
        metadata = {
            **archive_metadata,
            "canonical_id": canonical_id,
            "document_type": "form",
            "title": _form_title(form_text, canonical_id),
            "source_sha256": extracted.get("source_sha256", archive_metadata.get("source_sha256", "")),
            "source_type": archive_metadata.get("source_type", "source-text"),
            "review_status": archive_metadata.get("review_status", "extracted"),
        }
        tree = render_source_document(
            text,
            metadata,
            {"parser": "form-splitter-v1", "nodes": [form_node], "references": []},
        )
        write_xml(tree, output_path)
        generated.append({"canonical_id": canonical_id, "path": str(output_path)})

    return {
        "profile": "git-for-law-form-split-report-v1",
        "source_dir": str(source_dir),
        "corpus_dir": str(corpus_dir),
        "generated": generated,
        "skipped": skipped,
        "stats": {"generated": len(generated), "skipped": len(skipped)},
    }


def write_form_split_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
