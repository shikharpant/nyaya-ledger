"""Derived search index rebuilt from canonical corpus XML."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .query import build_corpus_lookup


TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(value: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(value)]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _snippet(text: str, query_tokens: list[str], phrase: str = "", size: int = 260) -> str:
    clean = _clean_text(text)
    if not clean:
        return ""
    lowered = clean.lower()
    positions = []
    if phrase and lowered.find(phrase) >= 0:
        positions.append(lowered.find(phrase))
    else:
        positions = [lowered.find(token) for token in query_tokens if token and lowered.find(token) >= 0]
    if not positions:
        return clean[:size]
    start = max(0, min(positions) - size // 3)
    end = min(len(clean), start + size)
    snippet = clean[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(clean):
        snippet += "..."
    return snippet


def _searchable_record(record_id: str, role: str, data: dict[str, Any]) -> dict[str, Any]:
    title = data.get("title") or data.get("document_title", "")
    text = data.get("text", "")
    weighted_text = " ".join(
        [
            title,
            title,
            data.get("canonical_id", ""),
            data.get("document_id", ""),
            text,
        ]
    )
    token_counts = Counter(_tokens(weighted_text))
    return {
        "id": record_id,
        "canonical_id": data.get("canonical_id", ""),
        "role": role,
        "document_id": data.get("document_id", data.get("canonical_id", "")),
        "document_type": data.get("document_type", ""),
        "title": title,
        "number": data.get("number", ""),
        "element_tag": data.get("element_tag", ""),
        "path": data.get("path", ""),
        "effective_from": data.get("effective_from", ""),
        "publication_date": data.get("publication_date", ""),
        "review_status": data.get("review_status", ""),
        "source_span": data.get("source_span", {}),
        "source_spans": data.get("source_spans", []),
        "text": text,
        "token_count": sum(token_counts.values()),
        "term_counts": dict(sorted(token_counts.items())),
    }


def build_search_records(corpus_dir: Path) -> list[dict[str, Any]]:
    """Build document and provision records for search and downstream indexing."""
    lookup = build_corpus_lookup(corpus_dir)
    records: list[dict[str, Any]] = []
    for canonical_id, entry in sorted(lookup.items()):
        document = entry.get("document")
        if document:
            records.append(_searchable_record(f"{canonical_id}#document", "document", document))
        provision = entry.get("provision")
        if provision:
            records.append(_searchable_record(f"{canonical_id}#provision", "provision", provision))
    return records


def write_search_index(corpus_dir: Path, output_path: Path) -> list[dict[str, Any]]:
    records = build_search_records(corpus_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    return records


def read_search_index(index_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not index_path.exists():
        raise FileNotFoundError(f"Search index not found: {index_path}")
    for line_number, line in enumerate(index_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{index_path}:{line_number}: invalid JSONL record: {exc}") from exc
    return records


def search_records(
    records: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 10,
    document_type: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    phrase = _clean_text(query).lower()
    results: list[dict[str, Any]] = []

    for record in records:
        if document_type and record.get("document_type") != document_type:
            continue
        if role and record.get("role") != role:
            continue

        term_counts = record.get("term_counts", {})
        matched_terms = {token: int(term_counts.get(token, 0)) for token in query_tokens}
        score = sum(matched_terms.values())
        title = (record.get("title") or "").lower()
        canonical_id = (record.get("canonical_id") or "").lower()
        text = record.get("text") or ""
        text_lower = text.lower()

        for token in query_tokens:
            if token in title:
                score += 4
            if token in canonical_id:
                score += 2
        if phrase and phrase in text_lower:
            score += max(6, len(query_tokens) * 3)
        if phrase and phrase in title:
            score += 8
        if score <= 0:
            continue

        results.append(
            {
                "score": score,
                "id": record.get("id", ""),
                "canonical_id": record.get("canonical_id", ""),
                "role": record.get("role", ""),
                "document_id": record.get("document_id", ""),
                "document_type": record.get("document_type", ""),
                "title": record.get("title", ""),
                "number": record.get("number", ""),
                "path": record.get("path", ""),
                "matched_terms": {term: count for term, count in matched_terms.items() if count},
                "snippet": _snippet(text, query_tokens, phrase),
            }
        )

    return sorted(
        results,
        key=lambda item: (-item["score"], -len(item["canonical_id"]), item["canonical_id"], item["role"]),
    )[:limit]


def search_index(
    index_path: Path,
    query: str,
    *,
    limit: int = 10,
    document_type: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]]:
    records = read_search_index(index_path)
    return search_records(records, query, limit=limit, document_type=document_type, role=role)
