"""Batch ingestion from a reviewed source inventory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .ingest import ingest_source_file


def load_inventory(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_id": item.get("canonical_id", ""),
        "document_type": item.get("document_type", "unknown"),
        "title": item.get("title") or item.get("canonical_id", ""),
        "jurisdiction": item.get("jurisdiction", "IN-UNION"),
        "language": item.get("language", "eng"),
        "publication_date": item.get("publication_date", ""),
        "effective_from": item.get("effective_from", ""),
        "issuing_authority": item.get("issuing_authority", ""),
        "review_status": "raw",
        "parser_version": "unparsed",
        "source_url": item.get("source_url") or item.get("source_path", ""),
        "source_type": "archived-source",
        "inventory_kind": item.get("kind", ""),
        "inventory_record_id": item.get("record_id", ""),
        "inventory_content_id": item.get("content_id", ""),
        "description": item.get("description", ""),
    }


def _eligible(item: dict[str, Any], status: str, document_type: str | None, category: str | None) -> bool:
    if status != "any" and item.get("status") != status:
        return False
    if document_type and item.get("document_type") != document_type:
        return False
    if category and item.get("category_slug") != category:
        return False
    return True


def ingest_inventory(
    inventory_path: Path,
    *,
    execute: bool = False,
    limit: int | None = None,
    status: str = "ready",
    document_type: str | None = None,
    category: str | None = None,
    mode: str = "deterministic",
    provider: str = "deepseek",
    model: str | None = None,
    base_url: str | None = None,
    skip_existing: bool = True,
    continue_on_error: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Preview or execute ingestion for selected inventory items."""
    inventory = load_inventory(inventory_path)
    results: list[dict[str, Any]] = []
    selected_items = [
        item
        for item in inventory.get("items", [])
        if _eligible(item, status=status, document_type=document_type, category=category)
    ]
    if limit is not None:
        selected_items = selected_items[:limit]
    total = len(selected_items)

    for index, item in enumerate(selected_items, start=1):
        result = {
            "canonical_id": item.get("canonical_id", ""),
            "source_path": item.get("source_path", ""),
            "source_dir": item.get("source_dir", ""),
            "output_path": item.get("output_path", ""),
            "status": "planned",
            "error": "",
        }

        missing_required = [
            key
            for key in ["source_path", "source_dir", "output_path", "canonical_id", "document_type"]
            if not item.get(key)
        ]
        if missing_required:
            result["status"] = "not_ingestible"
            result["error"] = "missing required fields: " + ", ".join(missing_required)
            results.append(result)
            if progress:
                progress({"index": index, "total": total, **result})
            if not continue_on_error:
                break
            continue

        output_path = Path(item["output_path"])
        if skip_existing and output_path.exists():
            result["status"] = "skipped_existing"
            results.append(result)
            if progress:
                progress({"index": index, "total": total, **result})
            continue

        if not execute:
            results.append(result)
            if progress:
                progress({"index": index, "total": total, **result})
            continue

        try:
            report = ingest_source_file(
                Path(item["source_path"]),
                Path(item["source_dir"]),
                output_path,
                _item_metadata(item),
                mode=mode,
                provider=provider,
                model=model,
                base_url=base_url,
            )
            result["status"] = "ingested"
            result["report"] = report
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            results.append(result)
            if progress:
                progress({"index": index, "total": total, **result})
            if not continue_on_error:
                break
            continue

        results.append(result)
        if progress:
            progress({"index": index, "total": total, **result})

    stats = {
        "planned": sum(1 for item in results if item["status"] == "planned"),
        "ingested": sum(1 for item in results if item["status"] == "ingested"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "skipped_existing": sum(1 for item in results if item["status"] == "skipped_existing"),
        "not_ingestible": sum(1 for item in results if item["status"] == "not_ingestible"),
    }
    return {
        "inventory_path": str(inventory_path),
        "execute": execute,
        "mode": mode,
        "provider": provider,
        "model": model or "",
        "base_url": base_url or "",
        "stats": stats,
        "items": results,
    }


def write_batch_ingest_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
