#!/usr/bin/env python3
"""Embed provision-level chunks and build a LanceDB vector table.

Reads ``derived/vector/provision_chunks.jsonl`` (produced by
``build_provision_chunks.py``), calls an OpenAI-compatible embeddings
endpoint, writes ``provision_embeddings.jsonl``, and builds the LanceDB
table ``nyaya_provisions_v1``.

Follows the pattern of ``embed_vector_chunks.py`` with three additions:
  * Provision-specific record metadata (provision_type, number).
  * Resumable embedding (skips already-embedded chunk_ids).
  * LanceDB index build (separate from the existing ``nyaya_ledger_nomic_v1_5``
    table — the flat-chunk table is never touched).

If the embedding endpoint is unreachable, the script exits non-zero and
reports that only the chunks JSONL is available.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "text-embedding-nomic-embed-text-v1.5"
DEFAULT_TABLE = "nyaya_provisions_v1"
EMBEDDING_DIM = 768


# ---------------------------------------------------------------------------
# Resumable embedding
# ---------------------------------------------------------------------------

def _read_done(output: Path) -> set[str]:
    if not output.exists():
        return set()
    done: set[str] = set()
    with output.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunk_id = record.get("chunk_id")
            if isinstance(chunk_id, str):
                done.add(chunk_id)
    return done


def _iter_chunks(path: Path, done: set[str]) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            chunk = json.loads(line)
            chunk_id = chunk.get("chunk_id")
            text = chunk.get("text")
            if not isinstance(chunk_id, str) or chunk_id in done:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            yield chunk


def _post_embeddings(endpoint: str, model: str, inputs: list[str], timeout: float) -> list[list[float]]:
    url = endpoint.rstrip("/") + "/embeddings"
    body = json.dumps({"model": model, "input": inputs}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != len(inputs):
        raise RuntimeError(f"Unexpected embeddings response shape: {payload!r}")
    data.sort(key=lambda item: item.get("index", 0))
    embeddings: list[list[float]] = []
    for item in data:
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError(f"Missing embedding in response item: {item!r}")
        embeddings.append(embedding)
    return embeddings


def _write_batch(
    output: Path,
    model: str,
    endpoint: str,
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            record = {
                "model": model,
                "endpoint": endpoint.rstrip("/"),
                "embedding_dimension": len(embedding),
                "chunk_id": chunk["chunk_id"],
                "canonical_id": chunk.get("canonical_id"),
                "provision_type": chunk.get("provision_type"),
                "number": chunk.get("number"),
                "document_id": chunk.get("document_id"),
                "document_type": chunk.get("document_type"),
                "document_title": chunk.get("document_title"),
                "title": chunk.get("title"),
                "chunk_index": chunk.get("chunk_index"),
                "token_estimate": chunk.get("token_estimate"),
                "eId": chunk.get("eId"),
                "path": chunk.get("path"),
                "embedding": embedding,
            }
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()


def _embed_with_retries(args: argparse.Namespace, batch: list[dict[str, Any]]) -> list[list[float]]:
    inputs = [chunk["text"] for chunk in batch]
    delay = max(args.delay, 0.2)
    last_error: Exception | None = None
    for attempt in range(1, args.retries + 1):
        try:
            return _post_embeddings(args.endpoint, args.model, inputs, args.timeout)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt == args.retries:
                break
            print(f"retrying batch attempt={attempt} error={exc}", flush=True)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Embedding batch failed after {args.retries} attempts: {last_error}") from last_error


# ---------------------------------------------------------------------------
# LanceDB index build
# ---------------------------------------------------------------------------

def build_lancedb_table(
    embeddings_path: Path,
    db_path: str,
    table_name: str,
    *,
    overwrite: bool = False,
    batch_size: int = 500,
) -> int:
    """Build a LanceDB table from provision embeddings JSONL.

    Returns the number of records indexed.
    """
    import lancedb
    import pyarrow as pa

    schema = pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("canonical_id", pa.string()),
            pa.field("provision_type", pa.string()),
            pa.field("number", pa.string()),
            pa.field("document_id", pa.string()),
            pa.field("document_type", pa.string()),
            pa.field("document_title", pa.string()),
            pa.field("title", pa.string()),
            pa.field("chunk_index", pa.int64()),
            pa.field("token_estimate", pa.int64()),
            pa.field("eId", pa.string()),
            pa.field("path", pa.string()),
            pa.field("text", pa.string()),  # populated from source chunk at join time
            pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        ]
    )

    db = lancedb.connect(db_path)
    # Join embeddings with chunk text from the source JSONL for a self-contained table.
    chunks_path = embeddings_path.parent / "provision_chunks.jsonl"
    chunk_text: dict[str, str] = {}
    if chunks_path.exists():
        with chunks_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                rec = json.loads(line)
                cid = rec.get("chunk_id")
                if isinstance(cid, str):
                    chunk_text[cid] = rec.get("text", "")

    mode = "overwrite" if overwrite else "create"
    table = None
    total = 0

    def _record_batches(iterator: Any) -> Any:
        batch: list[dict[str, Any]] = []
        for record in iterator:
            chunk_id = record.get("chunk_id", "")
            batch.append(
                {
                    "chunk_id": chunk_id,
                    "canonical_id": record.get("canonical_id") or "",
                    "provision_type": record.get("provision_type") or "",
                    "number": record.get("number") or "",
                    "document_id": record.get("document_id") or "",
                    "document_type": record.get("document_type") or "",
                    "document_title": record.get("document_title") or "",
                    "title": record.get("title") or "",
                    "chunk_index": int(record.get("chunk_index") or 0),
                    "token_estimate": int(record.get("token_estimate") or 0),
                    "eId": record.get("eId") or "",
                    "path": record.get("path") or "",
                    "text": chunk_text.get(chunk_id, ""),
                    "vector": record["embedding"],
                }
            )
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    with embeddings_path.open("r", encoding="utf-8") as handle:
        records = (json.loads(line) for line in handle if line.strip())
        for batch in _record_batches(records):
            if table is None:
                table = db.create_table(table_name, data=batch, schema=schema, mode=mode)
            else:
                table.add(batch)
            total += len(batch)
            print(f"indexed={total}", flush=True)

    if table is None:
        raise SystemExit("No embedding records found to index")

    print(f"lancedb={db_path} table={table_name} records={total}", flush=True)
    return total


# ---------------------------------------------------------------------------
# Endpoint reachability
# ---------------------------------------------------------------------------

def _endpoint_available(endpoint: str, timeout: float = 5.0) -> bool:
    url = endpoint.rstrip("/") + "/models"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="derived/vector/provision_chunks.jsonl",
        help="Input provision chunk JSONL",
    )
    parser.add_argument(
        "--output",
        default="derived/vector/provision_embeddings.jsonl",
        help="Output embedding JSONL",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OpenAI-compatible API base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Embedding model name")
    parser.add_argument("--batch-size", type=int, default=16, help="Chunks per embeddings request")
    parser.add_argument("--delay", type=float, default=0.05, help="Seconds to sleep between successful batches")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout per request")
    parser.add_argument("--retries", type=int, default=4, help="Retries per batch")
    parser.add_argument("--skip-embed", action="store_true", help="Skip embedding, only rebuild LanceDB")
    parser.add_argument("--skip-lancedb", action="store_true", help="Embed only, do not build LanceDB table")
    parser.add_argument("--db", default="derived/vector/lancedb", help="LanceDB directory")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="LanceDB table name")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing LanceDB table")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise SystemExit(f"Input chunks not found: {input_path}. Run build_provision_chunks.py first.")

    # ---- Embedding phase ----
    total_embedded = 0
    if not args.skip_embed:
        if not _endpoint_available(args.endpoint):
            print(
                f"WARNING: embedding endpoint {args.endpoint} is not reachable. "
                "Generated chunks JSONL only. Re-run with the endpoint up to embed.",
                flush=True,
            )
            # Count remaining chunks for reporting.
            done = _read_done(output_path)
            remaining = sum(1 for _ in _iter_chunks(input_path, done))
            print(f"chunks_total_remaining={remaining} already_embedded={len(done)}", flush=True)
            print(f"lancedb_table=skipped (no embeddings)", flush=True)
            return 1

        done = _read_done(output_path)
        batch: list[dict[str, Any]] = []

        for chunk in _iter_chunks(input_path, done):
            batch.append(chunk)
            if len(batch) < args.batch_size:
                continue
            embeddings = _embed_with_retries(args, batch)
            _write_batch(output_path, args.model, args.endpoint, batch, embeddings)
            total_embedded += len(batch)
            print(f"embedded={total_embedded} skipped_existing={len(done)} last={batch[-1]['chunk_id']}", flush=True)
            batch = []
            time.sleep(args.delay)

        if batch:
            embeddings = _embed_with_retries(args, batch)
            _write_batch(output_path, args.model, args.endpoint, batch, embeddings)
            total_embedded += len(batch)
            print(f"embedded={total_embedded} skipped_existing={len(done)} last={batch[-1]['chunk_id']}", flush=True)

        print(f"embedding_done embedded={total_embedded} skipped_existing={len(done)} output={output_path}", flush=True)
    else:
        print(f"embedding_skipped (using existing {output_path})", flush=True)

    # ---- LanceDB phase ----
    if not args.skip_lancedb:
        if not output_path.exists():
            print(f"WARNING: embeddings file {output_path} not found — cannot build LanceDB table.", flush=True)
            return 1
        build_lancedb_table(output_path, args.db, args.table, overwrite=args.overwrite)
    else:
        print("lancedb_skipped", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
