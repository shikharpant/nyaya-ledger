"""Canonical ID to repository path mapping for the India corpus profile."""

from __future__ import annotations

import re
from pathlib import Path


def _rule_file_label(label: str) -> str:
    clean = label.lower()
    if re.fullmatch(r"\d+", clean):
        return f"{int(clean):03}"
    if re.fullmatch(r"\d+[a-z]+", clean):
        number = re.match(r"\d+", clean)
        assert number is not None
        return f"{int(number.group(0)):02}{clean[number.end() :]}"
    return clean


def expected_corpus_relative_path(canonical_id: str, document_type: str = "") -> Path:
    """Return the expected corpus-relative XML path for a canonical document ID."""
    parts = [part for part in canonical_id.strip("/").split("/") if part]
    if len(parts) < 3 or parts[0] != "in":
        return Path(*parts).with_suffix(".xml")

    if document_type == "form" or (len(parts) >= 4 and parts[2] == "forms"):
        return Path(*parts, "form.xml")

    if document_type == "rule" and "rule" in parts:
        rule_index = len(parts) - 2
        if parts[rule_index] == "rule":
            base = parts[:rule_index]
            label = parts[rule_index + 1]
            return Path(*base, f"rule-{_rule_file_label(label)}.xml")

    if document_type == "appendix" or (
        len(parts) >= 5 and parts[2] == "rules" and parts[-1].startswith("appendix-")
    ):
        base = parts[:-1]
        last = parts[-1]
        return Path(*base, f"{last}.xml")

    if document_type == "rules" or (
        len(parts) >= 4 and parts[2] == "rules" and "rule" not in parts
    ):
        return Path(*parts, "rules.xml")

    if document_type == "act" or (
        len(parts) >= 4 and parts[2] == "acts" and "section" not in parts
    ):
        return Path(*parts, "act.xml")

    if document_type == "schedule" or (len(parts) >= 4 and parts[2] == "schedules"):
        return Path(*parts, "schedule.xml")

    return Path(*parts).with_suffix(".xml")
