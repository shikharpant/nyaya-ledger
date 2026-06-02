"""One-command verification gate for the canonical legal corpus pipeline."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .api_payload import write_api_payload
from .graph_index import rebuild_graph_index, write_neo4j_payload
from .html_renderer import write_html_site
from .quality import audit_corpus_quality, write_quality_report
from .references import build_unresolved_reference_report, write_unresolved_reference_report
from .search_index import read_search_index, write_search_index
from .source_inventory import validate_source_inventory_file
from .validator import validate_corpus, validate_sources, validate_xml_source_spans
from .vector_index import read_vector_chunks, write_vector_chunks


@dataclass
class VerificationStep:
    name: str
    ok: bool
    counts: dict[str, int] = field(default_factory=dict)
    output: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "counts": self.counts,
            "output": self.output,
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class VerificationResult:
    ok: bool
    strict_warnings: bool
    steps: list[VerificationStep]

    @property
    def warnings(self) -> list[str]:
        return [warning for step in self.steps for warning in step.warnings]

    @property
    def errors(self) -> list[str]:
        return [error for step in self.steps for error in step.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "strict_warnings": self.strict_warnings,
            "warnings": self.warnings,
            "errors": self.errors,
            "steps": [step.to_dict() for step in self.steps],
        }


def _step_ok(ok: bool, warnings: list[str], strict_warnings: bool) -> bool:
    if not ok:
        return False
    if strict_warnings and warnings:
        return False
    return True


def _write_manifest(result: VerificationResult, manifest_path: Path | None) -> None:
    if not manifest_path:
        return
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _xml_properties(path: Path) -> dict[str, str]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    properties = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            properties[name] = prop.attrib.get("value", "")
    return properties


def _source_archives_by_sha(sources_dir: Path) -> dict[str, dict[str, Any]]:
    archives: dict[str, dict[str, Any]] = {}
    for extracted_path in sorted(sources_dir.rglob("extracted_text.json")):
        try:
            extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        source_sha = extracted.get("source_sha256")
        if source_sha:
            archives[source_sha] = extracted
    return archives


def run_verification(
    *,
    corpus_dir: Path,
    sources_dir: Path,
    derived_dir: Path,
    manifest_path: Path | None = None,
    inventory_path: Path | None = None,
    strict_warnings: bool = False,
    vector_max_chars: int = 900,
    vector_overlap: int = 120,
) -> VerificationResult:
    """Validate source/corpus state and rebuild deterministic derived artifacts."""
    steps: list[VerificationStep] = []

    source_result = validate_sources(sources_dir)
    source_ok = _step_ok(source_result.ok, source_result.warnings, strict_warnings)
    steps.append(
        VerificationStep(
            name="sources",
            ok=source_ok,
            counts={"archives": source_result.checked_archives},
            warnings=source_result.warnings,
            errors=source_result.errors,
        )
    )

    corpus_result = validate_corpus(corpus_dir)
    corpus_ok = _step_ok(corpus_result.ok, corpus_result.warnings, strict_warnings)
    steps.append(
        VerificationStep(
            name="corpus",
            ok=corpus_ok,
            counts={"xml_files": corpus_result.checked_files},
            warnings=corpus_result.warnings,
            errors=corpus_result.errors,
        )
    )

    extracted_by_sha = _source_archives_by_sha(sources_dir)
    span_errors: list[str] = []
    span_warnings: list[str] = []
    checked_span_files = 0
    for xml_path in sorted(corpus_dir.rglob("*.xml")):
        source_sha = _xml_properties(xml_path).get("source_sha256", "")
        extracted = extracted_by_sha.get(source_sha)
        if not extracted:
            continue
        checked_span_files += 1
        errors, warnings = validate_xml_source_spans(xml_path, extracted)
        span_errors.extend(errors)
        span_warnings.extend(warnings)
    steps.append(
        VerificationStep(
            name="xml_source_spans",
            ok=_step_ok(not span_errors, span_warnings, strict_warnings),
            counts={"xml_files": checked_span_files},
            warnings=span_warnings,
            errors=span_errors,
        )
    )

    selected_inventory = inventory_path or derived_dir / "sources/source_inventory.json"
    if selected_inventory.exists():
        inventory_result = validate_source_inventory_file(selected_inventory)
        inventory_ok = _step_ok(inventory_result.ok, inventory_result.warnings, strict_warnings)
        steps.append(
            VerificationStep(
                name="source_inventory",
                ok=inventory_ok,
                counts={"items": inventory_result.checked_items},
                output=str(selected_inventory),
                warnings=inventory_result.warnings,
                errors=inventory_result.errors,
            )
        )

    quality_path = derived_dir / "quality/corpus_quality.json"
    quality_report = audit_corpus_quality(corpus_dir)
    write_quality_report(quality_report, quality_path)
    quality_errors = []
    loaded_quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if loaded_quality != quality_report:
        quality_errors.append("quality report JSON round-trip mismatch")
    if quality_report.get("stats", {}).get("documents", 0) <= 0:
        quality_errors.append("quality report contains no documents")
    steps.append(
        VerificationStep(
            name="quality",
            ok=not quality_errors,
            counts=quality_report.get("stats", {}),
            output=str(quality_path),
            errors=quality_errors,
        )
    )

    references_path = derived_dir / "references/unresolved_references.json"
    reference_report = build_unresolved_reference_report(corpus_dir)
    write_unresolved_reference_report(reference_report, references_path)
    loaded_references = json.loads(references_path.read_text(encoding="utf-8"))
    reference_errors = []
    if loaded_references != reference_report:
        reference_errors.append("unresolved reference report JSON round-trip mismatch")
    steps.append(
        VerificationStep(
            name="unresolved_references",
            ok=not reference_errors,
            counts={
                "unresolved_occurrences": reference_report["stats"]["unresolved_occurrences"],
                "unresolved_targets": reference_report["stats"]["unresolved_targets"],
            },
            output=str(references_path),
            errors=reference_errors,
        )
    )

    graph_path = derived_dir / "graph/corpus_graph.json"
    graph = rebuild_graph_index(corpus_dir, graph_path)
    steps.append(
        VerificationStep(
            name="graph",
            ok=bool(graph.get("nodes")),
            counts={"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", []))},
            output=str(graph_path),
            errors=[] if graph.get("nodes") else ["graph index contains no nodes"],
        )
    )

    neo4j_path = derived_dir / "graph/corpus_neo4j_payload.json"
    payload = write_neo4j_payload(corpus_dir, neo4j_path)
    steps.append(
        VerificationStep(
            name="neo4j_payload",
            ok=bool(payload.get("statements")),
            counts={"statements": len(payload.get("statements", []))},
            output=str(neo4j_path),
            errors=[] if payload.get("statements") else ["neo4j payload contains no statements"],
        )
    )

    api_path = derived_dir / "api/corpus_api.json"
    api_payload = write_api_payload(corpus_dir, api_path)
    loaded_api = json.loads(api_path.read_text(encoding="utf-8"))
    api_errors = []
    if loaded_api != api_payload:
        api_errors.append("api payload JSON round-trip mismatch")
    if not api_payload.get("documents"):
        api_errors.append("api payload contains no documents")
    steps.append(
        VerificationStep(
            name="api_payload",
            ok=not api_errors,
            counts=api_payload.get("stats", {}),
            output=str(api_path),
            errors=api_errors,
        )
    )

    html_dir = derived_dir / "html"
    html_result = write_html_site(corpus_dir, html_dir)
    html_errors = []
    if html_result.get("files", 0) <= 0:
        html_errors.append("html export contains no files")
    if not (html_dir / "index.html").exists():
        html_errors.append("html export missing index.html")
    steps.append(
        VerificationStep(
            name="html",
            ok=not html_errors,
            counts=html_result,
            output=str(html_dir),
            errors=html_errors,
        )
    )

    search_path = derived_dir / "search/corpus_search.jsonl"
    records = write_search_index(corpus_dir, search_path)
    loaded_records = read_search_index(search_path)
    search_errors = []
    if loaded_records != records:
        search_errors.append("search index JSONL round-trip mismatch")
    if not records:
        search_errors.append("search index contains no records")
    steps.append(
        VerificationStep(
            name="search",
            ok=not search_errors,
            counts={"records": len(records)},
            output=str(search_path),
            errors=search_errors,
        )
    )

    vector_path = derived_dir / "vector/corpus_chunks.jsonl"
    chunks = write_vector_chunks(corpus_dir, vector_path, max_chars=vector_max_chars, overlap=vector_overlap)
    loaded_chunks = read_vector_chunks(vector_path)
    vector_errors = []
    if loaded_chunks != chunks:
        vector_errors.append("vector chunk JSONL round-trip mismatch")
    if not chunks:
        vector_errors.append("vector chunk export contains no chunks")
    steps.append(
        VerificationStep(
            name="vector_chunks",
            ok=not vector_errors,
            counts={"chunks": len(chunks)},
            output=str(vector_path),
            errors=vector_errors,
        )
    )

    result = VerificationResult(
        ok=all(step.ok for step in steps),
        strict_warnings=strict_warnings,
        steps=steps,
    )
    _write_manifest(result, manifest_path)
    return result
