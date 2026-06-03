#!/usr/bin/env python3
"""Embed derived vector chunks through an OpenAI-compatible embeddings API."""

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


def _write_batch(output: Path, model: str, endpoint: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            record = {
                "model": model,
                "endpoint": endpoint.rstrip("/"),
                "embedding_dimension": len(embedding),
                "chunk_id": chunk["chunk_id"],
                "canonical_id": chunk.get("canonical_id"),
                "document_id": chunk.get("document_id"),
                "document_type": chunk.get("document_type"),
                "role": chunk.get("role"),
                "chunk_index": chunk.get("chunk_index"),
                "source_record_id": chunk.get("source_record_id"),
                "path": chunk.get("path"),
                "token_estimate": chunk.get("token_estimate"),
                "embedding": embedding,
            }
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="derived/vector/corpus_chunks.jsonl", help="Input chunk JSONL")
    parser.add_argument("--output", default="derived/vector/embeddings.nomic-v1.5.jsonl", help="Output embedding JSONL")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OpenAI-compatible API base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Embedding model name")
    parser.add_argument("--batch-size", type=int, default=16, help="Chunks per embeddings request")
    parser.add_argument("--delay", type=float, default=0.05, help="Seconds to sleep between successful batches")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout per request")
    parser.add_argument("--max-chunks", type=int, default=0, help="Optional cap for smoke tests")
    parser.add_argument("--retries", type=int, default=4, help="Retries per batch")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    done = _read_done(output_path)
    total_written = 0
    batch: list[dict[str, Any]] = []

    for chunk in _iter_chunks(input_path, done):
        batch.append(chunk)
        if len(batch) < args.batch_size:
            continue
        embeddings = _embed_with_retries(args, batch)
        _write_batch(output_path, args.model, args.endpoint, batch, embeddings)
        total_written += len(batch)
        print(f"embedded={total_written} skipped_existing={len(done)} last={batch[-1]['chunk_id']}", flush=True)
        batch = []
        if args.max_chunks and total_written >= args.max_chunks:
            return 0
        time.sleep(args.delay)

    if batch and not (args.max_chunks and total_written >= args.max_chunks):
        embeddings = _embed_with_retries(args, batch)
        _write_batch(output_path, args.model, args.endpoint, batch, embeddings)
        total_written += len(batch)
        print(f"embedded={total_written} skipped_existing={len(done)} last={batch[-1]['chunk_id']}", flush=True)

    print(f"done embedded={total_written} skipped_existing={len(done)} output={output_path}", flush=True)
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
