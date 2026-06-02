"""Review-oriented diffs between canonical corpus directories."""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .query import build_corpus_lookup


@dataclass
class ProvisionSnapshot:
    canonical_id: str
    number: str
    title: str
    text: str

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, str]:
        return {
            "canonical_id": self.canonical_id,
            "number": self.number,
            "title": self.title,
            "text_sha256": self.text_sha256,
        }


@dataclass
class DocumentSnapshot:
    canonical_id: str
    document_type: str
    title: str
    path: str
    relative_path: str
    xml_sha256: str
    text: str
    provisions: dict[str, ProvisionSnapshot] = field(default_factory=dict)

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "document_type": self.document_type,
            "title": self.title,
            "path": self.path,
            "relative_path": self.relative_path,
            "xml_sha256": self.xml_sha256,
            "text_sha256": self.text_sha256,
            "provisions": len(self.provisions),
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_corpus(corpus_dir: Path) -> dict[str, DocumentSnapshot]:
    lookup = build_corpus_lookup(corpus_dir)
    documents: dict[str, DocumentSnapshot] = {}

    for entry in lookup.values():
        document = entry.get("document")
        if not document:
            continue
        path = Path(document["path"])
        documents[document["canonical_id"]] = DocumentSnapshot(
            canonical_id=document["canonical_id"],
            document_type=document.get("document_type", ""),
            title=document.get("title", ""),
            path=str(path),
            relative_path=str(path.relative_to(corpus_dir)),
            xml_sha256=_file_sha256(path),
            text=document.get("text", ""),
        )

    for entry in lookup.values():
        provision = entry.get("provision")
        if not provision:
            continue
        document_id = provision.get("document_id", "")
        document = documents.get(document_id)
        if not document:
            continue
        document.provisions[provision["canonical_id"]] = ProvisionSnapshot(
            canonical_id=provision["canonical_id"],
            number=provision.get("number", ""),
            title=provision.get("title", ""),
            text=provision.get("text", ""),
        )

    return documents


def _limited_unified_diff(
    base: DocumentSnapshot,
    review: DocumentSnapshot,
    context: int,
    max_lines: int,
) -> tuple[list[str], bool]:
    lines = list(
        difflib.unified_diff(
            base.text.splitlines(),
            review.text.splitlines(),
            fromfile=base.relative_path,
            tofile=review.relative_path,
            lineterm="",
            n=context,
        )
    )
    if max_lines and len(lines) > max_lines:
        return lines[:max_lines], True
    return lines, False


def _provision_changes(
    base: DocumentSnapshot,
    review: DocumentSnapshot,
) -> dict[str, list[dict[str, Any]]]:
    base_ids = set(base.provisions)
    review_ids = set(review.provisions)
    added = [review.provisions[canonical_id].summary() for canonical_id in sorted(review_ids - base_ids)]
    removed = [base.provisions[canonical_id].summary() for canonical_id in sorted(base_ids - review_ids)]
    modified = []

    for canonical_id in sorted(base_ids & review_ids):
        base_provision = base.provisions[canonical_id]
        review_provision = review.provisions[canonical_id]
        if base_provision.text_sha256 == review_provision.text_sha256:
            continue
        modified.append(
            {
                "canonical_id": canonical_id,
                "base": base_provision.summary(),
                "review": review_provision.summary(),
            }
        )

    return {"added": added, "removed": removed, "modified": modified}


def compare_corpora(
    base_corpus_dir: Path,
    review_corpus_dir: Path,
    context: int = 3,
    max_diff_lines: int = 200,
) -> dict[str, Any]:
    """Compare two canonical corpus directories by canonical document ID."""
    base_docs = _snapshot_corpus(base_corpus_dir)
    review_docs = _snapshot_corpus(review_corpus_dir)
    base_ids = set(base_docs)
    review_ids = set(review_docs)

    added = [review_docs[canonical_id].summary() for canonical_id in sorted(review_ids - base_ids)]
    removed = [base_docs[canonical_id].summary() for canonical_id in sorted(base_ids - review_ids)]
    modified: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []

    for canonical_id in sorted(base_ids & review_ids):
        base = base_docs[canonical_id]
        review = review_docs[canonical_id]
        xml_changed = base.xml_sha256 != review.xml_sha256
        text_changed = base.text_sha256 != review.text_sha256
        path_changed = base.relative_path != review.relative_path
        if not (xml_changed or text_changed or path_changed):
            unchanged.append(review.summary())
            continue

        diff_lines, truncated = _limited_unified_diff(base, review, context=context, max_lines=max_diff_lines)
        modified.append(
            {
                "canonical_id": canonical_id,
                "document_type": review.document_type or base.document_type,
                "title": review.title or base.title,
                "base_path": base.path,
                "review_path": review.path,
                "base_relative_path": base.relative_path,
                "review_relative_path": review.relative_path,
                "xml_changed": xml_changed,
                "text_changed": text_changed,
                "path_changed": path_changed,
                "base_xml_sha256": base.xml_sha256,
                "review_xml_sha256": review.xml_sha256,
                "base_text_sha256": base.text_sha256,
                "review_text_sha256": review.text_sha256,
                "provisions": _provision_changes(base, review),
                "unified_text_diff": diff_lines,
                "diff_truncated": truncated,
            }
        )

    return {
        "base_corpus_dir": str(base_corpus_dir),
        "review_corpus_dir": str(review_corpus_dir),
        "stats": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": len(unchanged),
        },
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": unchanged,
    }


def write_diff_report(
    base_corpus_dir: Path,
    review_corpus_dir: Path,
    output_path: Path,
    context: int = 3,
    max_diff_lines: int = 200,
) -> dict[str, Any]:
    report = compare_corpora(
        base_corpus_dir,
        review_corpus_dir,
        context=context,
        max_diff_lines=max_diff_lines,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
