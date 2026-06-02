"""Validation for the canonical corpus profile."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import expected_corpus_relative_path
from .source_archive import find_source_file, read_metadata_yaml, sha256_file
from .structure_parser import text_hash, validate_structure_spans


REQUIRED_METADATA = {
    "canonical_id",
    "document_type",
    "title",
    "jurisdiction",
    "language",
    "source_type",
    "source_url",
    "source_sha256",
    "publication_date",
    "effective_from",
    "issuing_authority",
    "review_status",
    "parser_version",
}


@dataclass
class CorpusValidationResult:
    checked_files: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class SourceValidationResult:
    checked_archives: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _properties(root: ET.Element) -> dict[str, str]:
    props = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            props[name] = prop.attrib.get("value", "")
    return props


def _validate_source_attrs(path: Path, node: ET.Element) -> list[str]:
    errors: list[str] = []
    source_start = node.attrib.get("sourceStart")
    source_end = node.attrib.get("sourceEnd")
    source_hash = node.attrib.get("sourceHash")

    if source_start is None and source_end is None and source_hash is None:
        return errors
    if source_start is None or source_end is None:
        errors.append(f"{path}: sourceStart/sourceEnd must be provided together")
        return errors
    try:
        start = int(source_start)
        end = int(source_end)
    except ValueError:
        errors.append(f"{path}: sourceStart/sourceEnd must be integers")
        return errors
    if start < 0 or end < start:
        errors.append(f"{path}: invalid source span {start}:{end}")
    if source_hash and not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        errors.append(f"{path}: sourceHash must be a SHA-256 hex digest")
    source_confidence = node.attrib.get("sourceConfidence")
    if source_confidence is not None:
        try:
            confidence = float(source_confidence)
        except ValueError:
            errors.append(f"{path}: sourceConfidence must be numeric")
        else:
            if confidence < 0 or confidence > 1:
                errors.append(f"{path}: sourceConfidence must be between 0 and 1")
    return errors


def validate_xml_file(path: Path) -> tuple[list[str], list[str], str | None, set[str], list[tuple[str, str]]]:
    errors: list[str] = []
    warnings: list[str] = []
    canonical_id: str | None = None
    local_ids: set[str] = set()
    references: list[tuple[str, str]] = []

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return [f"{path}: XML parse error: {exc}"], warnings, canonical_id, local_ids, references

    root = tree.getroot()
    if root.tag != "akomaNtoso":
        errors.append(f"{path}: root element must be akomaNtoso")

    props = _properties(root)
    missing = sorted(REQUIRED_METADATA - set(props))
    if missing:
        errors.append(f"{path}: missing metadata fields: {', '.join(missing)}")

    canonical_id = props.get("canonical_id")
    if canonical_id:
        local_ids.add(canonical_id)
    if canonical_id and not canonical_id.startswith("/in/"):
        errors.append(f"{path}: canonical_id must start with /in/")
    if canonical_id and not re.fullmatch(r"/[a-z0-9][a-z0-9/_-]*", canonical_id):
        errors.append(f"{path}: canonical_id contains unsupported characters: {canonical_id}")

    document_type = props.get("document_type")
    if document_type not in {"act", "rules", "rule", "notification", "circular", "order", "form", "schedule"}:
        errors.append(f"{path}: unsupported document_type: {document_type}")

    text_nodes = [node.text.strip() for node in root.findall(".//p") if node.text and node.text.strip()]
    if not text_nodes:
        warnings.append(f"{path}: no textual p nodes found")

    seen_eids: dict[str, int] = {}
    for node in root.findall(".//*[@eId]"):
        eid = node.attrib["eId"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", eid):
            errors.append(f"{path}: invalid eId: {eid}")
        seen_eids[eid] = seen_eids.get(eid, 0) + 1
        errors.extend(_validate_source_attrs(path, node))
    for eid, count in sorted(seen_eids.items()):
        if count > 1:
            errors.append(f"{path}: duplicate eId in file: {eid}")

    for node in root.findall(".//*[@refersTo]"):
        refers_to = node.attrib["refersTo"]
        local_ids.add(refers_to)
        if canonical_id and not (refers_to == canonical_id or refers_to.startswith(f"{canonical_id}/")):
            errors.append(f"{path}: local provision ID is outside document hierarchy: {refers_to}")

    for node in root.findall(".//ref") + root.findall(".//textualMod"):
        href = node.attrib.get("href")
        if href:
            references.append((path.as_posix(), href))

    return errors, warnings, canonical_id, local_ids, references


def validate_xml_source_spans(path: Path, extracted: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate XML source span attributes against extracted source text."""
    errors: list[str] = []
    warnings: list[str] = []
    text = extracted.get("text", "")
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return [f"{path}: XML parse error: {exc}"], warnings

    checked = 0
    for node in tree.getroot().findall(".//*[@sourceStart]"):
        eid = node.attrib.get("eId", node.tag)
        try:
            start = int(node.attrib["sourceStart"])
            end = int(node.attrib["sourceEnd"])
        except (KeyError, ValueError):
            errors.append(f"{path}: {eid}: invalid source span attributes")
            continue
        if start < 0 or end < start or end > len(text):
            errors.append(f"{path}: {eid}: source span {start}:{end} is outside extracted text")
            continue
        expected_hash = node.attrib.get("sourceHash")
        if expected_hash and expected_hash != text_hash(text[start:end]):
            errors.append(f"{path}: {eid}: sourceHash does not match extracted text")
        checked += 1

    if checked == 0:
        warnings.append(f"{path}: no XML source spans found")
    return errors, warnings


def validate_corpus(corpus_dir: Path) -> CorpusValidationResult:
    result = CorpusValidationResult()
    seen_ids: dict[str, Path] = {}
    known_ids: set[str] = set()
    references: list[tuple[str, str]] = []

    for path in sorted(corpus_dir.rglob("*.xml")):
        result.checked_files += 1
        errors, warnings, canonical_id, local_ids, local_references = validate_xml_file(path)
        result.errors.extend(errors)
        result.warnings.extend(warnings)
        known_ids.update(local_ids)
        references.extend(local_references)
        if canonical_id:
            previous = seen_ids.get(canonical_id)
            if previous:
                result.errors.append(f"{path}: duplicate canonical_id also used by {previous}: {canonical_id}")
            seen_ids[canonical_id] = path
            try:
                relative_path = path.relative_to(corpus_dir)
            except ValueError:
                relative_path = path
            expected_path = expected_corpus_relative_path(canonical_id, _properties(ET.parse(path).getroot()).get("document_type", ""))
            if relative_path != expected_path:
                result.errors.append(f"{path}: canonical_id path mismatch; expected {corpus_dir / expected_path}")

    for path, href in references:
        if href.startswith("/in/") and href not in known_ids:
            result.warnings.append(f"{path}: unresolved canonical reference: {href}")

    if result.checked_files == 0:
        result.errors.append(f"{corpus_dir}: no XML files found")

    return result


def validate_source_archive(source_dir: Path) -> SourceValidationResult:
    """Validate extracted text and structure spans for one source archive."""
    result = SourceValidationResult(checked_archives=1)
    extracted_path = source_dir / "extracted_text.json"
    structure_path = source_dir / "structure.json"
    metadata_path = source_dir / "metadata.yaml"
    metadata: dict[str, str] = {}

    if not metadata_path.exists():
        result.warnings.append(f"{source_dir}: metadata.yaml missing")
    else:
        metadata = read_metadata_yaml(metadata_path)
    if not extracted_path.exists():
        result.errors.append(f"{source_dir}: extracted_text.json missing")
        return result
    if not structure_path.exists():
        result.errors.append(f"{source_dir}: structure.json missing")
        return result

    try:
        extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
        structure = json.loads(structure_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.errors.append(f"{source_dir}: invalid JSON: {exc}")
        return result

    if not extracted.get("source_sha256"):
        result.warnings.append(f"{source_dir}: source_sha256 missing from extracted_text.json")
    if not extracted.get("text"):
        result.errors.append(f"{source_dir}: extracted text is empty")

    source_file_name = metadata.get("source_file") or extracted.get("source_file", "")
    source_path = source_dir / source_file_name if source_file_name else None
    if source_path is None or not source_path.exists():
        try:
            source_path = find_source_file(source_dir)
        except FileNotFoundError:
            result.errors.append(f"{source_dir}: source file missing")
            source_path = None

    if source_path:
        actual_sha = sha256_file(source_path)
        if metadata.get("source_file") and metadata["source_file"] != source_path.name:
            result.errors.append(
                f"{source_dir}: metadata source_file does not match archive source file: {metadata['source_file']}"
            )
        if extracted.get("source_file") and extracted["source_file"] != source_path.name:
            result.errors.append(
                f"{source_dir}: extracted_text source_file does not match archive source file: {extracted['source_file']}"
            )
        if metadata.get("source_sha256") and metadata["source_sha256"] != actual_sha:
            result.errors.append(f"{source_dir}: metadata source_sha256 does not match source file")
        if extracted.get("source_sha256") and extracted["source_sha256"] != actual_sha:
            result.errors.append(f"{source_dir}: extracted_text source_sha256 does not match source file")

    pages = extracted.get("pages", [])
    if isinstance(pages, list) and pages:
        reconstructed = "\n\n".join(str(page.get("text", "")) for page in pages if isinstance(page, dict))
        if reconstructed != extracted.get("text", ""):
            result.errors.append(f"{source_dir}: extracted text does not match page text round-trip")
        for index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                result.errors.append(f"{source_dir}: page {index}: page record must be an object")
                continue
            start = page.get("start")
            end = page.get("end")
            page_text = str(page.get("text", ""))
            if not isinstance(start, int) or not isinstance(end, int):
                result.errors.append(f"{source_dir}: page {index}: start/end must be integers")
                continue
            if start < 0 or end < start or end > len(extracted.get("text", "")):
                result.errors.append(f"{source_dir}: page {index}: invalid span {start}:{end}")
                continue
            if extracted.get("text", "")[start:end] != page_text:
                result.errors.append(f"{source_dir}: page {index}: span does not match extracted text")
    else:
        result.warnings.append(f"{source_dir}: extracted_text.json has no page records")

    errors, warnings = validate_structure_spans(extracted, structure)
    result.errors.extend(f"{source_dir}: {error}" for error in errors)
    result.warnings.extend(f"{source_dir}: {warning}" for warning in warnings)
    return result


def validate_sources(sources_dir: Path) -> SourceValidationResult:
    """Validate every source archive containing extracted_text.json."""
    result = SourceValidationResult()
    archives = sorted(path.parent for path in sources_dir.rglob("extracted_text.json"))
    for archive_dir in archives:
        archive_result = validate_source_archive(archive_dir)
        result.checked_archives += archive_result.checked_archives
        result.errors.extend(archive_result.errors)
        result.warnings.extend(archive_result.warnings)
    if result.checked_archives == 0:
        result.errors.append(f"{sources_dir}: no source archives found")
    return result
