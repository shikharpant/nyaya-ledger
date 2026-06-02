"""Review and promotion helpers for generated corpus batches."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _quality_flags(quality_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item.get("canonical_id", ""): item for item in quality_report.get("flagged_documents", [])}


def _corpus_relative_path(xml_path: Path, generated_corpus_dir: Path) -> Path:
    try:
        return xml_path.relative_to(generated_corpus_dir)
    except ValueError as exc:
        raise ValueError(f"{xml_path} is not inside generated corpus {generated_corpus_dir}") from exc


def _source_relative_path(source_dir: Path) -> Path:
    parts = source_dir.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "sources":
            return Path(*parts[index + 1 :])
    return Path(source_dir.name)


def plan_batch_promotion(
    ingest_report_path: Path,
    quality_report_path: Path,
    *,
    target_corpus_dir: Path,
    target_sources_dir: Path | None = None,
    include_flagged: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Select generated XML files that are safe candidates for canonical promotion."""
    ingest_report = _load_json(ingest_report_path)
    quality_report = _load_json(quality_report_path)
    generated_corpus_dir = Path(quality_report.get("corpus_dir", ""))
    flags = _quality_flags(quality_report)

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in ingest_report.get("items", []):
        canonical_id = item.get("canonical_id", "")
        if item.get("status") != "ingested":
            excluded.append(
                {
                    "canonical_id": canonical_id,
                    "reason": item.get("status", "not_ingested"),
                    "source_path": item.get("source_path", ""),
                    "generated_xml": item.get("output_path", ""),
                }
            )
            continue

        generated_xml = Path(item.get("output_path", ""))
        flag = flags.get(canonical_id)
        if flag and not include_flagged:
            excluded.append(
                {
                    "canonical_id": canonical_id,
                    "reason": "quality_flagged",
                    "quality": flag,
                    "source_path": item.get("source_path", ""),
                    "generated_xml": str(generated_xml),
                }
            )
            continue

        relative_path = _corpus_relative_path(generated_xml, generated_corpus_dir)
        target_xml = target_corpus_dir / relative_path
        generated_source_dir = Path(item.get("source_dir", ""))
        target_source_dir = None
        if target_sources_dir is not None and item.get("source_dir"):
            target_source_dir = target_sources_dir / _source_relative_path(generated_source_dir)

        if target_xml.exists() and not overwrite:
            excluded.append(
                {
                    "canonical_id": canonical_id,
                    "reason": "target_exists",
                    "source_path": item.get("source_path", ""),
                    "generated_xml": str(generated_xml),
                    "target_xml": str(target_xml),
                }
            )
            continue

        if target_source_dir is not None and target_source_dir.exists() and not overwrite:
            excluded.append(
                {
                    "canonical_id": canonical_id,
                    "reason": "target_source_exists",
                    "source_path": item.get("source_path", ""),
                    "generated_xml": str(generated_xml),
                    "target_xml": str(target_xml),
                    "source_dir": str(generated_source_dir),
                    "target_source_dir": str(target_source_dir),
                }
            )
            continue

        selected.append(
            {
                "canonical_id": canonical_id,
                "source_path": item.get("source_path", ""),
                "source_dir": str(generated_source_dir),
                "generated_xml": str(generated_xml),
                "target_xml": str(target_xml),
                "target_source_dir": str(target_source_dir) if target_source_dir is not None else "",
                "quality": flag or {},
            }
        )

    return {
        "profile": "git-for-law-batch-promotion-plan-v1",
        "ingest_report": str(ingest_report_path),
        "quality_report": str(quality_report_path),
        "generated_corpus_dir": str(generated_corpus_dir),
        "target_corpus_dir": str(target_corpus_dir),
        "target_sources_dir": str(target_sources_dir) if target_sources_dir is not None else "",
        "include_flagged": include_flagged,
        "overwrite": overwrite,
        "stats": {
            "ingested": sum(1 for item in ingest_report.get("items", []) if item.get("status") == "ingested"),
            "selected": len(selected),
            "excluded": len(excluded),
            "quality_flagged": sum(1 for item in excluded if item.get("reason") == "quality_flagged"),
            "target_exists": sum(1 for item in excluded if item.get("reason") == "target_exists"),
            "target_source_exists": sum(1 for item in excluded if item.get("reason") == "target_source_exists"),
        },
        "selected": selected,
        "excluded": excluded,
    }


def write_promotion_plan(plan: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def apply_batch_promotion(plan: dict[str, Any], *, approve: bool = False) -> dict[str, Any]:
    """Copy selected generated XML and optional source archives only when approved."""
    copied_xml: list[str] = []
    copied_sources: list[str] = []
    copied_source_targets: set[str] = set()
    if approve:
        for item in plan.get("selected", []):
            source = Path(item["generated_xml"])
            target = Path(item["target_xml"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied_xml.append(str(target))

            source_dir = item.get("source_dir")
            target_source_dir = item.get("target_source_dir")
            if source_dir and target_source_dir:
                source_archive = Path(source_dir)
                target_archive = Path(target_source_dir)
                target_key = str(target_archive)
                if target_key in copied_source_targets:
                    continue
                target_archive.parent.mkdir(parents=True, exist_ok=True)
                if target_archive.exists() and not plan.get("overwrite"):
                    copied_source_targets.add(target_key)
                    continue
                shutil.copytree(source_archive, target_archive, dirs_exist_ok=bool(plan.get("overwrite")))
                copied_source_targets.add(target_key)
                copied_sources.append(str(target_archive))

    return {
        "profile": "git-for-law-batch-promotion-result-v1",
        "approved": approve,
        "copied": copied_xml,
        "copied_xml": copied_xml,
        "copied_sources": copied_sources,
        "stats": {
            "selected": len(plan.get("selected", [])),
            "copied": len(copied_xml),
            "copied_xml": len(copied_xml),
            "copied_sources": len(copied_sources),
            "excluded": len(plan.get("excluded", [])),
        },
    }
