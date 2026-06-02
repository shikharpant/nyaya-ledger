"""Seed the canonical corpus from the existing prototype data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.mutation_parser import parse_notification_offline

from .paths import expected_corpus_relative_path
from .renderer import (
    canonical_form_id,
    canonical_rule_id,
    render_form,
    render_rule,
    render_source_document,
    write_xml,
)
from .source_archive import (
    archive_source,
    extract_source_text,
    sha256_file,
    write_structure_json,
)
from .structure_parser import parse_structure_deterministic


def _sha_metadata(source_path: Path, source_type: str) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_url": str(source_path),
        "source_sha256": sha256_file(source_path),
        "publication_date": "",
        "effective_from": "",
        "issuing_authority": "/in/authority/cbic",
    }


def seed_rules(root_dir: Path, corpus_dir: Path) -> list[Path]:
    rules_path = root_dir / "data/genesis/rules/cgst_rules_chapter3.json"
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    metadata_base = {
        **_sha_metadata(rules_path, "seed-json"),
        "effective_from": data["_metadata"].get("effective_date", ""),
        "source_url": str(rules_path),
    }

    outputs = []
    for rule in data.get("rules", []):
        path = corpus_dir / expected_corpus_relative_path(canonical_rule_id(rule["label"]), "rule")
        tree = render_rule(rule, data["chapter"], metadata_base)
        outputs.append(write_xml(tree, path))
    return outputs


def seed_form(root_dir: Path, corpus_dir: Path) -> list[Path]:
    form_path = root_dir / "data/genesis/forms/form_gst_reg_01.json"
    form = json.loads(form_path.read_text(encoding="utf-8"))
    for section in form.get("sections", []):
        section["schema_payload_json"] = json.dumps(section.get("schema_payload", {}), ensure_ascii=False, sort_keys=True)

    metadata = {
        **_sha_metadata(form_path, "seed-json"),
        "effective_from": form.get("valid_from", "")[:10],
        "source_url": str(form_path),
        "canonical_id": canonical_form_id(form["form_number"]),
    }
    tree = render_form(form, metadata)
    output = corpus_dir / expected_corpus_relative_path(canonical_form_id(form["form_number"]), "form")
    return [write_xml(tree, output)]


def seed_notification(root_dir: Path, corpus_dir: Path, sources_dir: Path) -> list[Path]:
    notification_path = root_dir / "data/notifications/18_2025_CT.txt"
    source_dir = sources_dir / "cbic/central-tax/2025/18-2025"
    archived_source = archive_source(
        notification_path,
        source_dir,
        {
            "canonical_id": "/in/union/notifications/cbic/central-tax/2025/18-2025",
            "document_type": "notification",
            "title": "Notification No. 18/2025 - Central Tax",
            "jurisdiction": "IN-UNION",
            "language": "eng",
            "publication_date": "2025-10-31",
            "effective_from": "2025-11-01",
            "issuing_authority": "/in/authority/cbic",
            "review_status": "seeded",
            "parser_version": "deterministic-paragraph-v1",
            "source_url": str(notification_path),
            "source_type": "seed-text",
        },
    )
    extracted = extract_source_text(source_dir)
    structure = parse_structure_deterministic(extracted)
    write_structure_json(source_dir, structure)

    parsed = parse_notification_offline(extracted["text"])
    amendments = [
        {
            "operation": mutation.operation,
            "target": mutation.target_node_path,
        }
        for mutation in parsed.mutations
    ]
    metadata = {
        "canonical_id": "/in/union/notifications/cbic/central-tax/2025/18-2025",
        "document_type": "notification",
        "title": "Notification No. 18/2025 - Central Tax",
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "source_type": "seed-text",
        "source_url": str(notification_path),
        "source_sha256": extracted["source_sha256"],
        "publication_date": "2025-10-31",
        "effective_from": "2025-11-01",
        "issuing_authority": "/in/authority/cbic",
        "review_status": "seeded",
        "parser_version": "deterministic-paragraph-v1",
        "amendments": amendments,
    }
    tree = render_source_document(extracted["text"], metadata, structure)
    output = corpus_dir / expected_corpus_relative_path(metadata["canonical_id"], "notification")
    return [
        write_xml(tree, output),
        archived_source,
        source_dir / "metadata.yaml",
        source_dir / "extracted_text.json",
        source_dir / "structure.json",
    ]


def seed_from_existing_data(
    root_dir: Path,
    corpus_dir: Path | None = None,
    sources_dir: Path | None = None,
) -> list[Path]:
    """Convert current prototype JSON/text data into canonical corpus artifacts."""
    corpus_dir = corpus_dir or root_dir / "corpus"
    sources_dir = sources_dir or root_dir / "sources"

    outputs = []
    outputs.extend(seed_rules(root_dir, corpus_dir))
    outputs.extend(seed_form(root_dir, corpus_dir))
    outputs.extend(seed_notification(root_dir, corpus_dir, sources_dir))
    return outputs
