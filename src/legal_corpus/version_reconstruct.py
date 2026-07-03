"""On-demand component reconstruction from node_versions.jsonl."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .version_compare import read_node_versions, resolve_version_dir
from .query import normalize_query_id


def _version_at(rows: list[dict[str, Any]], date_value: str) -> dict[str, Any] | None:
    candidates = []
    for row in rows:
        start = row.get("applicability_start") or row.get("valid_from")
        end = row.get("applicability_end") or row.get("valid_to")
        if start and start <= date_value and (not end or date_value < end):
            candidates.append(row)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.get("applicability_start") or item.get("valid_from") or "")[-1]


def reconstruct_component(
    component_id: str,
    *,
    date: str,
    target_work: str | None = None,
    registry_path: Path = Path("data/Law/statute_identity_registry.json"),
    version_dir: Path | None = None,
) -> dict[str, Any]:
    canonical = normalize_query_id(component_id)
    resolved_dir, resolved_work = resolve_version_dir(
        canonical,
        target_work=target_work,
        registry_path=registry_path,
        version_dir=version_dir,
    )
    rows = [
        row
        for row in read_node_versions(resolved_dir / "node_versions.jsonl")
        if row.get("component_id") == canonical
    ]
    version = _version_at(rows, date)
    if not version:
        return {
            "status": "not_found",
            "target_work": resolved_work,
            "component_id": canonical,
            "date": date,
            "text": "",
            "version": None,
        }
    return {
        "status": "ok",
        "target_work": resolved_work,
        "component_id": canonical,
        "date": date,
        "text": version.get("text", ""),
        "version": version,
    }


__all__ = ["reconstruct_component"]
