"""One-step source archive ingestion into canonical XML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .renderer import render_source_document, write_xml
from .source_archive import (
    archive_source,
    extract_source_text,
    read_metadata_yaml,
    write_metadata_yaml,
    write_structure_json,
)
from .structure_parser import parse_structure, validate_structure_spans
from .validator import validate_source_archive, validate_xml_file, validate_xml_source_spans


def _notification_amendments(text: str, document_type: str) -> list[dict[str, str]]:
    if document_type != "notification":
        return []
    from src.mutation_parser import parse_notification_offline

    parsed = parse_notification_offline(text)
    return [{"operation": mutation.operation, "target": mutation.target_node_path} for mutation in parsed.mutations]


def ingest_source_file(
    input_path: Path,
    source_dir: Path,
    output_path: Path,
    metadata: dict[str, Any],
    *,
    mode: str = "deterministic",
    provider: str = "deepseek",
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Archive, extract, parse, render, and validate a source file."""
    archive_source(input_path, source_dir, metadata)
    extracted = extract_source_text(source_dir)
    archive_metadata = read_metadata_yaml(source_dir / "metadata.yaml")
    document_type = archive_metadata.get("document_type", metadata.get("document_type", "notification"))
    structure = parse_structure(
        extracted,
        document_type=document_type,
        mode=mode,
        provider=provider,
        model=model,
        base_url=base_url,
    )
    span_errors, span_warnings = validate_structure_spans(extracted, structure)
    if span_errors:
        raise ValueError("Structure span validation failed: " + "; ".join(span_errors))

    archive_metadata["parser_version"] = structure.get("parser", archive_metadata.get("parser_version", "unknown"))
    archive_metadata["review_status"] = "parsed"
    archive_metadata["source_sha256"] = extracted.get("source_sha256", archive_metadata.get("source_sha256", ""))
    archive_metadata["source_file"] = extracted.get("source_file", archive_metadata.get("source_file", ""))
    if archive_metadata.get("source_type") in {"", "archived-source"}:
        archive_metadata["source_type"] = "source-archive"
    amendments = _notification_amendments(extracted.get("text", ""), document_type)

    write_metadata_yaml(source_dir / "metadata.yaml", archive_metadata)
    structure_path = write_structure_json(source_dir, structure)
    render_metadata = dict(archive_metadata)
    if amendments:
        render_metadata["amendments"] = amendments
    tree = render_source_document(extracted.get("text", ""), render_metadata, structure)
    xml_path = write_xml(tree, output_path)

    source_validation = validate_source_archive(source_dir)
    xml_errors, xml_warnings, _canonical_id, _local_ids, _references = validate_xml_file(xml_path)
    source_span_errors, source_span_warnings = validate_xml_source_spans(xml_path, extracted)
    if source_validation.errors or xml_errors or source_span_errors:
        errors = source_validation.errors + xml_errors + source_span_errors
        raise ValueError("Ingest validation failed: " + "; ".join(errors))

    return {
        "source_dir": str(source_dir),
        "structure": str(structure_path),
        "xml": str(xml_path),
        "parser": structure.get("parser", ""),
        "nodes": len(structure.get("nodes", [])),
        "references": len(structure.get("references", [])),
        "warnings": span_warnings + source_validation.warnings + xml_warnings + source_span_warnings,
    }


def write_ingest_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
