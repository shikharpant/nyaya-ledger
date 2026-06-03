#!/usr/bin/env python3
"""Build a LanceDB vector table from corpus chunks and embedding JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _iter_jsonl(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _iter_joined_records(chunks_path: Path, embeddings_path: Path, max_records: int = 0) -> Any:
    count = 0
    for chunk, embedding in zip(_iter_jsonl(chunks_path), _iter_jsonl(embeddings_path), strict=True):
        if max_records and count >= max_records:
            return
        chunk_id = embedding.get("chunk_id")
        if chunk_id != chunk.get("chunk_id"):
            raise RuntimeError(f"Chunk/embedding order mismatch: {chunk.get('chunk_id')} != {chunk_id}")
        if isinstance(chunk_id, str):
            count += 1
            yield {
                "chunk_id": chunk_id,
                "canonical_id": embedding.get("canonical_id") or chunk.get("canonical_id", ""),
                "document_id": embedding.get("document_id") or chunk.get("document_id", ""),
                "document_type": embedding.get("document_type") or chunk.get("document_type", ""),
                "role": embedding.get("role") or chunk.get("role", ""),
                "chunk_index": int(embedding.get("chunk_index") or chunk.get("chunk_index") or 0),
                "title": chunk.get("title", ""),
                "path": embedding.get("path") or chunk.get("path", ""),
                "token_estimate": int(embedding.get("token_estimate") or chunk.get("token_estimate") or 0),
                "model": embedding.get("model", ""),
                "text": chunk.get("text", ""),
                "vector": embedding["embedding"],
            }


def _chunks(iterator: Any, size: int) -> Any:
    batch = []
    for item in iterator:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", default="derived/vector/corpus_chunks.jsonl")
    parser.add_argument("--embeddings", default="derived/vector/embeddings.nomic-v1.5.jsonl")
    parser.add_argument("--db", default="derived/vector/lancedb")
    parser.add_argument("--table", default="nyaya_ledger_nomic_v1_5")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-records", type=int, default=0, help="Optional cap for smoke tests")
    args = parser.parse_args()

    try:
        import lancedb
        import pyarrow as pa
    except ImportError as exc:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt") from exc

    db = lancedb.connect(args.db)
    schema = pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("canonical_id", pa.string()),
            pa.field("document_id", pa.string()),
            pa.field("document_type", pa.string()),
            pa.field("role", pa.string()),
            pa.field("chunk_index", pa.int64()),
            pa.field("title", pa.string()),
            pa.field("path", pa.string()),
            pa.field("token_estimate", pa.int64()),
            pa.field("model", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 768)),
        ]
    )
    mode = "overwrite" if args.overwrite else "create"
    total = 0
    table = None
    records = _iter_joined_records(Path(args.chunks), Path(args.embeddings), args.max_records)
    for batch in _chunks(records, args.batch_size):
        if table is None:
            table = db.create_table(args.table, data=batch, schema=schema, mode=mode)
        else:
            table.add(batch)
        total += len(batch)
        print(f"indexed={total}", flush=True)

    if table is None:
        raise SystemExit("No embedding records found")

    print(f"lancedb={args.db} table={args.table} records={total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
