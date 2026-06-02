"""Vector/RAG-ready chunk export rebuilt from canonical corpus XML."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .search_index import build_search_records


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    clean = _clean_text(text)
    if not clean:
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    if overlap < 0:
        raise ValueError("overlap must be zero or greater")
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")

    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + max_chars)
        if end < len(clean):
            boundary = max(clean.rfind(". ", start, end), clean.rfind("; ", start, end), clean.rfind(" ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(clean):
            break
        start = max(0, end - overlap)
    return chunks


def _chunk_record(record: dict[str, Any], chunk_text: str, chunk_index: int) -> dict[str, Any]:
    return {
        "chunk_id": f"{record['id']}#chunk-{chunk_index:04d}",
        "source_record_id": record["id"],
        "canonical_id": record["canonical_id"],
        "role": record["role"],
        "document_id": record["document_id"],
        "document_type": record["document_type"],
        "title": record.get("title", ""),
        "number": record.get("number", ""),
        "path": record.get("path", ""),
        "source_span": record.get("source_span", {}),
        "source_spans": record.get("source_spans", []),
        "chunk_index": chunk_index,
        "text": chunk_text,
        "token_estimate": len(re.findall(r"[A-Za-z0-9]+", chunk_text)),
    }


def build_vector_chunks(
    corpus_dir: Path,
    *,
    max_chars: int = 900,
    overlap: int = 120,
    include_documents: bool = True,
    include_provisions: bool = True,
) -> list[dict[str, Any]]:
    """Create stable text chunks suitable for embedding or RAG ingestion."""
    chunks: list[dict[str, Any]] = []
    for record in build_search_records(corpus_dir):
        if record["role"] == "document" and not include_documents:
            continue
        if record["role"] == "provision" and not include_provisions:
            continue
        for index, chunk_text in enumerate(_split_text(record.get("text", ""), max_chars, overlap), start=1):
            chunks.append(_chunk_record(record, chunk_text, index))
    return chunks


def write_vector_chunks(
    corpus_dir: Path,
    output_path: Path,
    *,
    max_chars: int = 900,
    overlap: int = 120,
    include_documents: bool = True,
    include_provisions: bool = True,
) -> list[dict[str, Any]]:
    chunks = build_vector_chunks(
        corpus_dir,
        max_chars=max_chars,
        overlap=overlap,
        include_documents=include_documents,
        include_provisions=include_provisions,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n" for chunk in chunks),
        encoding="utf-8",
    )
    return chunks


def read_vector_chunks(path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"Vector chunk export not found: {path}")
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            chunks.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL chunk: {exc}") from exc
    return chunks
