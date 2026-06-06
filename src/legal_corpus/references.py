"""Reference-resolution reports for canonical corpus planning."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            props[name] = prop.attrib.get("value", "")
    return props


def _document_slug(canonical_id: str) -> str:
    return canonical_id.rstrip("/").split("/")[-1]


def _strip_the(value: str) -> str:
    return re.sub(r"^the-", "", value)


def _normalize_form_slug(value: str) -> str:
    parts = value.lower().replace("_", "-").split("-")
    normalized = []
    for part in parts:
        match = re.fullmatch(r"([0-9]+)([a-z]?)", part)
        if match:
            normalized.append(f"{int(match.group(1))}{match.group(2)}")
        else:
            normalized.append(part)
    return "-".join(normalized)


def _target_parts(target: str) -> list[str]:
    return [part for part in target.split("/") if part]


def _known_ids(root: ET.Element, document_id: str) -> set[str]:
    ids = {document_id}
    for node in root.findall(".//*[@refersTo]"):
        refers_to = node.attrib.get("refersTo")
        if refers_to:
            ids.add(refers_to)
    return ids


def _references(path: Path, root: ET.Element, document_id: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for node in root.findall(".//ref") + root.findall(".//textualMod"):
        href = node.attrib.get("href", "")
        if not href.startswith("/in/"):
            continue
        refs.append(
            {
                "target": href,
                "source_document": document_id,
                "source_path": path.as_posix(),
                "source_eid": node.attrib.get("eId", ""),
                "type": node.attrib.get("type", "REFERS_TO"),
                "showAs": node.attrib.get("showAs", href),
            }
        )
    return refs


def _target_kind(target: str) -> str:
    parts = _target_parts(target)
    if len(parts) >= 4 and parts[2] == "acts":
        return "act_section" if "section" in parts else "act"
    if len(parts) >= 4 and parts[2] == "rules":
        return "rule"
    if len(parts) >= 3 and parts[2] == "forms":
        return "form"
    if len(parts) >= 3 and parts[2] == "notifications":
        return "notification"
    return "other"


def _target_document(target: str) -> str:
    parts = _target_parts(target)
    if len(parts) >= 4 and parts[2] == "acts":
        return "/" + "/".join(parts[:4])
    if len(parts) >= 4 and parts[2] == "rules":
        return "/" + "/".join(parts[:4])
    if len(parts) >= 3 and parts[2] == "forms":
        return "/" + "/".join(parts[:4]) if len(parts) >= 4 else target
    if len(parts) >= 3 and parts[2] == "notifications":
        return "/" + "/".join(parts[: min(len(parts), 7)])
    return target


class CorpusReferenceResolver:
    """Resolve high-confidence aliases against IDs actually present in corpus XML."""

    def __init__(self, known_ids: set[str], document_ids: set[str]) -> None:
        self.known_ids = known_ids
        self.document_ids = document_ids
        self._act_aliases = self._build_act_aliases(document_ids)
        self._form_aliases = self._build_form_aliases(document_ids)

    @staticmethod
    def _build_act_aliases(document_ids: set[str]) -> dict[str, str]:
        by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
        for document_id in document_ids:
            parts = _target_parts(document_id)
            if len(parts) == 4 and parts[2] == "acts":
                by_key[("/".join(parts[:3]), _strip_the(parts[3]))].append(document_id)
        aliases: dict[str, str] = {}
        for (namespace, slug), candidates in by_key.items():
            unique = sorted(set(candidates))
            if len(unique) != 1:
                continue
            canonical = unique[0]
            canonical_slug = _document_slug(canonical)
            for alias_slug in {slug, f"the-{slug}"}:
                alias = f"/{namespace}/{alias_slug}"
                if alias != canonical:
                    aliases[alias] = canonical
        return aliases

    @staticmethod
    def _build_form_aliases(document_ids: set[str]) -> dict[str, str]:
        by_key: dict[str, list[str]] = defaultdict(list)
        for document_id in document_ids:
            parts = _target_parts(document_id)
            if len(parts) == 4 and parts[2] == "forms":
                by_key[_normalize_form_slug(parts[3])].append(document_id)
        aliases: dict[str, str] = {}
        for normalized_slug, candidates in by_key.items():
            unique = sorted(set(candidates))
            if len(unique) != 1:
                continue
            canonical = unique[0]
            candidate_slug = _document_slug(canonical)
            alias = f"/in/union/forms/{normalized_slug}"
            if alias != canonical and _normalize_form_slug(candidate_slug) == normalized_slug:
                aliases[alias] = canonical
        return aliases

    def resolve(self, target: str) -> tuple[str, str, str]:
        """Return normalized target, status, and action for a reference target."""
        if not target.startswith("/in/"):
            return target, "external_or_literal", "ignore"
        if target in self.known_ids:
            return target, "resolved", "none"

        target_document = _target_document(target)
        suffix = target[len(target_document) :] if target.startswith(target_document) else ""
        alias_document = self._act_aliases.get(target_document) or self._form_aliases.get(target_document)
        if alias_document:
            normalized = f"{alias_document}{suffix}"
            if normalized in self.known_ids:
                return normalized, "resolved_by_alias", "apply_alias"
            return normalized, "alias_document_exists_child_missing", "refresh_source_or_parser"

        if target_document in self.document_ids:
            return target, "document_exists_missing_child", "refresh_source_or_parser"
        if target.startswith("/in/union/forms/"):
            return target, "form_missing", "ingest_missing_form"
        if target_document != target:
            return target, "document_missing", "manual_review"
        return target, "document_missing", "manual_review"


def build_reference_resolver(corpus_dir: Path) -> CorpusReferenceResolver:
    known_ids: set[str] = set()
    document_ids: set[str] = set()
    for path in sorted(corpus_dir.rglob("*.xml")):
        root = ET.parse(path).getroot()
        props = _properties(root)
        document_id = props.get("canonical_id", "")
        if not document_id:
            continue
        document_ids.add(document_id)
        known_ids.update(_known_ids(root, document_id))
    return CorpusReferenceResolver(known_ids=known_ids, document_ids=document_ids)


def normalize_reference_target(target: str, resolver: CorpusReferenceResolver | None = None) -> str:
    if resolver is None:
        return target
    normalized, _status, _action = resolver.resolve(target)
    return normalized


def build_unresolved_reference_report(corpus_dir: Path, *, sample_limit: int = 5) -> dict[str, Any]:
    """Summarize unresolved canonical references and their source documents."""
    known_ids: set[str] = set()
    document_ids: set[str] = set()
    references: list[dict[str, str]] = []
    documents = 0

    for path in sorted(corpus_dir.rglob("*.xml")):
        root = ET.parse(path).getroot()
        props = _properties(root)
        document_id = props.get("canonical_id", "")
        if not document_id:
            continue
        documents += 1
        document_ids.add(document_id)
        known_ids.update(_known_ids(root, document_id))
        references.extend(_references(path, root, document_id))

    resolver = CorpusReferenceResolver(known_ids=known_ids, document_ids=document_ids)
    normalized_references: list[dict[str, str]] = []
    alias_resolved = 0
    for ref in references:
        normalized_target, status, action = resolver.resolve(ref["target"])
        if status == "resolved_by_alias":
            alias_resolved += 1
        normalized_ref = dict(ref)
        normalized_ref["target"] = normalized_target
        normalized_ref["original_target"] = ref["target"]
        normalized_ref["resolution_status"] = status
        normalized_ref["suggested_action"] = action
        normalized_references.append(normalized_ref)

    unresolved = [ref for ref in normalized_references if ref["target"] not in known_ids]
    occurrence_counts = Counter(ref["target"] for ref in unresolved)
    target_sources: dict[str, Counter[str]] = defaultdict(Counter)
    target_samples: dict[str, list[dict[str, str]]] = defaultdict(list)
    target_statuses: dict[str, Counter[str]] = defaultdict(Counter)
    target_actions: dict[str, Counter[str]] = defaultdict(Counter)
    target_originals: dict[str, Counter[str]] = defaultdict(Counter)
    for ref in unresolved:
        target = ref["target"]
        target_sources[target][ref["source_document"]] += 1
        target_statuses[target][ref["resolution_status"]] += 1
        target_actions[target][ref["suggested_action"]] += 1
        target_originals[target][ref.get("original_target", target)] += 1
        if len(target_samples[target]) < sample_limit:
            target_samples[target].append(ref)

    targets: list[dict[str, Any]] = []
    for target, occurrences in occurrence_counts.most_common():
        source_counts = target_sources[target]
        targets.append(
            {
                "target": target,
                "original_targets": [
                    {"target": original, "occurrences": count}
                    for original, count in target_originals[target].most_common(sample_limit)
                ],
                "kind": _target_kind(target),
                "target_document": _target_document(target),
                "occurrences": occurrences,
                "classification": target_statuses[target].most_common(1)[0][0],
                "suggested_action": target_actions[target].most_common(1)[0][0],
                "source_documents": len(source_counts),
                "top_sources": [
                    {"source_document": source, "occurrences": count}
                    for source, count in source_counts.most_common(sample_limit)
                ],
                "samples": target_samples[target],
            }
        )

    kind_counts = Counter(item["kind"] for item in targets)
    class_counts = Counter(item["classification"] for item in targets)
    action_counts = Counter(item["suggested_action"] for item in targets)
    document_counts = Counter(item["target_document"] for item in targets)
    return {
        "profile": "git-for-law-unresolved-reference-report-v2",
        "corpus_dir": str(corpus_dir),
        "stats": {
            "documents": documents,
            "references": len(references),
            "alias_resolved_occurrences": alias_resolved,
            "unresolved_occurrences": len(unresolved),
            "unresolved_targets": len(targets),
            "kinds": dict(sorted(kind_counts.items())),
            "classifications": dict(sorted(class_counts.items())),
            "suggested_actions": dict(sorted(action_counts.items())),
        },
        "top_target_documents": [
            {"target_document": target_document, "unresolved_targets": count}
            for target_document, count in document_counts.most_common(25)
        ],
        "targets": targets,
    }


def write_unresolved_reference_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_unresolved_reference_summary(report: dict[str, Any], output_path: Path) -> Path:
    stats = report.get("stats", {})
    lines = [
        "# Unresolved Reference Triage",
        "",
        f"- Documents: {stats.get('documents', 0)}",
        f"- References: {stats.get('references', 0)}",
        f"- Alias-resolved occurrences: {stats.get('alias_resolved_occurrences', 0)}",
        f"- Unresolved occurrences: {stats.get('unresolved_occurrences', 0)}",
        f"- Unresolved targets: {stats.get('unresolved_targets', 0)}",
        "",
        "## Classifications",
        "",
    ]
    for key, value in sorted(stats.get("classifications", {}).items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Suggested Actions", ""])
    for key, value in sorted(stats.get("suggested_actions", {}).items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Top Target Documents", ""])
    for item in report.get("top_target_documents", [])[:25]:
        lines.append(f"- {item['target_document']}: {item['unresolved_targets']}")
    lines.extend(["", "## Top Targets", ""])
    for item in report.get("targets", [])[:50]:
        lines.append(
            f"- {item['occurrences']}x `{item['target']}` "
            f"({item['classification']}, {item['suggested_action']})"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path
