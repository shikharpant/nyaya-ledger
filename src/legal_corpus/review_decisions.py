"""Apply curated review decisions to produce a promoted amendment-event ledger."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .component_spans import find_top_level_subrule_span, parent_component_for_subrule, subrule_label_from_component


DECISION_PROMOTER_VERSION = "review-decision-promoter-v1"
AUTO_REVIEW_VERSION = "auto-review-decisions-v1"
DEPENDENCY_REVIEW_VERSION = "dependency-review-decisions-v1"
CODEX_REVIEW_VERSION = "codex-review-decisions-v1"
RULES_WORK = "/in/union/rules/cgst-rules-2017"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _load_decisions(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    decisions = payload.get("decisions") if isinstance(payload, dict) else payload
    return {
        str(row.get("event_id")): row
        for row in decisions or []
        if row.get("decision") == "approved" and row.get("event_id")
    }


def _load_decision_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("decisions") if isinstance(payload, dict) else payload
    return [row for row in rows or [] if isinstance(row, dict) and row.get("decision") == "approved"]


def _load_decision_paths(paths: list[Path]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        merged.update(_load_decisions(path))
    return merged


def _load_triage(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("event_id")): row
        for row in payload.get("items", [])
        if row.get("event_id")
    }


def _clean_structural_text(text: str) -> str:
    clean = text.strip()
    clean = re.sub(r"^[\s:;\-.–—]*[“\"]?", "", clean)
    clean = re.sub(r"[”\"]?\s*[.;]?\s*$", "", clean)
    return clean.strip()


def _date_key(event: dict[str, Any]) -> str:
    legal_time = event.get("legal_time") or {}
    source = event.get("source") or {}
    return legal_time.get("applicability_start") or legal_time.get("commencement_date") or source.get("publication_date") or ""


def _event_slot(event: dict[str, Any]) -> tuple[str, str]:
    return (str((event.get("target") or {}).get("component_id") or ""), _date_key(event))


def _clean_materializable(event: dict[str, Any]) -> bool:
    validation = event.get("validation") or {}
    review = event.get("review") or {}
    return bool(
        event.get("status") == "validated"
        and validation.get("materializable")
        and not review.get("required")
    )


def _clean_materialized_slots(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {_event_slot(event) for event in events if _event_slot(event)[0] and _event_slot(event)[1] and _clean_materializable(event)}


def _clean_materialized_slot_event_ids(events: list[dict[str, Any]]) -> dict[tuple[str, str], set[str]]:
    slots: dict[tuple[str, str], set[str]] = {}
    for event in events:
        slot = _event_slot(event)
        event_id = str(event.get("event_id") or "")
        if slot[0] and slot[1] and event_id and _clean_materializable(event):
            slots.setdefault(slot, set()).add(event_id)
    return slots


def _already_reflected_strategy(decision: dict[str, Any]) -> bool:
    strategy = str((decision.get("promote") or {}).get("strategy") or "")
    return strategy.startswith("already_reflected_")


def _covered_by_other_clean_event(
    event: dict[str, Any],
    decision: dict[str, Any],
    clean_slot_event_ids: dict[tuple[str, str], set[str]],
) -> bool:
    if not _already_reflected_strategy(decision):
        return False
    event_id = str(event.get("event_id") or "")
    slot_ids = set(clean_slot_event_ids.get(_decision_slot(decision, event)) or set())
    slot_ids.discard(event_id)
    return bool(slot_ids)


def _decision_slot(decision: dict[str, Any], event: dict[str, Any]) -> tuple[str, str]:
    promote = decision.get("promote") or {}
    return (
        str(promote.get("component_id") or (event.get("target") or {}).get("component_id") or ""),
        str(promote.get("applicability_start") or _date_key(event) or ""),
    )


def _load_node_versions(path: Path) -> dict[str, list[dict[str, Any]]]:
    versions: dict[str, list[dict[str, Any]]] = {}
    if not path.exists():
        return versions
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        component_id = str(row.get("component_id") or "")
        if component_id:
            versions.setdefault(component_id, []).append(row)
    for rows in versions.values():
        rows.sort(key=lambda row: row.get("applicability_start") or row.get("valid_from") or "")
    return versions


def _version_text_at(versions: dict[str, list[dict[str, Any]]], component_id: str, date_value: str) -> str:
    rows = versions.get(component_id) or []
    candidates = []
    for row in rows:
        start = row.get("applicability_start") or row.get("valid_from") or ""
        end = row.get("applicability_end") or row.get("valid_to") or ""
        if start and start <= date_value and (not end or date_value < end):
            candidates.append(row)
    if candidates:
        return str(candidates[-1].get("text") or "")
    return str(rows[-1].get("text") or "") if rows else ""


def _source_archive_dir(event: dict[str, Any], source_archive_root: Path) -> Path | None:
    document_id = str((event.get("source") or {}).get("document_id") or "")
    parts = [part for part in document_id.strip("/").split("/") if part]
    try:
        idx = parts.index("cbic")
    except ValueError:
        return None
    suffix = parts[idx + 1 :]
    if len(suffix) < 3:
        return None
    leaf = suffix[-1]
    match = re.match(r"(\d+)-(\d{4})", leaf)
    if match:
        suffix[-1] = f"{int(match.group(1))}-{match.group(2)}"
    return source_archive_root.joinpath("cbic", *suffix)


def _event_source_span_text(event: dict[str, Any], source_archive_root: Path | None = None) -> str:
    if source_archive_root is None:
        return ""
    archive_dir = _source_archive_dir(event, source_archive_root)
    if not archive_dir:
        return ""
    extracted_path = archive_dir / "extracted_text.json"
    if not extracted_path.exists():
        return ""
    try:
        payload = json.loads(extracted_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    text = str(payload.get("text") or "")
    span = (event.get("evidence") or {}).get("source_span") or {}
    try:
        start = int(span.get("start"))
        end = int(span.get("end"))
    except (TypeError, ValueError):
        return ""
    if start < 0 or end <= start or start >= len(text):
        return ""
    span_text = text[start : min(end, len(text))]
    expected_hash = str(span.get("text_hash") or "")
    if expected_hash:
        actual_hash = hashlib.sha256(span_text.encode("utf-8")).hexdigest()
        if actual_hash != expected_hash:
            return ""
    return span_text.strip()


def _event_source_instruction_window(
    event: dict[str, Any],
    source_archive_root: Path | None = None,
    *,
    max_chars: int = 2500,
) -> str:
    if source_archive_root is None:
        return ""
    archive_dir = _source_archive_dir(event, source_archive_root)
    if not archive_dir:
        return ""
    extracted_path = archive_dir / "extracted_text.json"
    if not extracted_path.exists():
        return ""
    try:
        payload = json.loads(extracted_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    text = str(payload.get("text") or "")
    span = (event.get("evidence") or {}).get("source_span") or {}
    try:
        start = int(span.get("start"))
    except (TypeError, ValueError):
        return ""
    if start < 0 or start >= len(text):
        return ""
    window = text[start : min(len(text), start + max_chars)]
    next_amendment = re.search(r"\n\s*\(\d+\)\s+In\s+rule\b", window[80:], flags=re.IGNORECASE)
    if next_amendment:
        window = window[: 80 + next_amendment.start()]
    return window.strip()


def _normalized_exact_match(text: str, needle: str) -> str | None:
    needle = str(needle or "").strip()
    if not needle:
        return None
    if text.count(needle) == 1:
        return needle
    pattern = re.escape(re.sub(r"\s+", " ", needle).strip()).replace(r"\ ", r"\s+")
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
    if len(matches) != 1:
        return None
    return text[matches[0].start() : matches[0].end()]


def _normalized_match_spans(text: str, needle: str) -> list[tuple[int, int]]:
    needle = str(needle or "").strip()
    if not needle:
        return []
    pattern = re.escape(re.sub(r"\s+", " ", needle).strip()).replace(r"\ ", r"\s+")
    return [(match.start(), match.end()) for match in re.finditer(pattern, text, flags=re.IGNORECASE)]


def _hard_review_reasons(event: dict[str, Any]) -> set[str]:
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    return reasons & {
        "compound_block_contains_multiple_amendments",
        "compound_block_contains_unsupported_omission",
        "date_not_resolved",
        "document_scope_target_not_materializable",
        "incomplete_text_edit_payload",
        "inserted_component_already_exists",
        "notification_level_status_change",
        "omit_instruction_ambiguous",
        "partial_omit_requires_precise_delete_payload",

        "structural_substitution_label_not_verified",
        "target_component_outside_work",
        "target_not_resolved",
        "unsafe_generic_substitution_anchor",
        "unsupported_form_or_table_mutation",
        "unsupported_materializer_operation",
    }


def _parse_rule_from_instruction(text: str, fallback_label: str) -> tuple[str, str, str]:
    quoted = re.search(
        r"[“\"]\s*(?:Rule\s+)?([0-9A-Za-z]+)\.\s*([^.\n]+)\.\s*-?\s*(.*?)[”\"]?\.?\s*$",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if quoted:
        label = quoted.group(1).strip()
        heading = re.sub(r"\s+", " ", quoted.group(2)).strip()
        content = quoted.group(3).strip()
        return label, heading, content
    plain = re.search(
        r"\b(?:Rule\s+)?([0-9A-Za-z]+)\.\s*([^.\n]+)\.\s*-?\s*(.*)$",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if plain:
        label = plain.group(1).strip()
        heading = re.sub(r"\s+", " ", plain.group(2)).strip()
        content = plain.group(3).strip()
        return label, heading, content
    return fallback_label, "", text.strip()


def _rule_component(label: str) -> str:
    return f"/in/union/rules/cgst-rules-2017/rule/{label.lower()}"


def _component_label(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", str(label or "")).lower()


def _rule_child_component(rule_label: str, child_type: str, child_label: str) -> str:
    return f"{_rule_component(rule_label)}/{child_type}/{_component_label(child_label)}"


def _subrule_child_component(rule_label: str, subrule_label: str, child_type: str, child_label: str) -> str:
    return f"{_rule_child_component(rule_label, 'subrule', subrule_label)}/{child_type}/{_component_label(child_label)}"


def _child_component(parent_component_id: str, child_type: str, label: str, content: str = "") -> str:
    label_key = _component_label(label)
    if not label_key:
        label_key = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
    if child_type in {"proviso", "explanation"}:
        digest = hashlib.sha256((label + "\n" + content).encode("utf-8")).hexdigest()[:10]
        return f"{parent_component_id}/{child_type}/{label_key}-{digest}"
    return f"{parent_component_id}/{child_type}/{label_key}"


def _parse_insert_rule_instruction(text: str) -> dict[str, str] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    anchor = re.search(r"\bafter\s+rule\s+(\d+[A-Z]?)\b", normalized, flags=re.IGNORECASE)
    if not anchor:
        return None
    if not re.search(r"\bfollowing\s+rule\s*,?\s+shall\s+be\s+inserted\b", normalized, flags=re.IGNORECASE):
        return None
    after_rule = anchor.group(1)
    after_text = normalized[anchor.end() :]
    inserted = re.search(
        r"[“\"―]\s*(?:Rule\s+)?(\d+[A-Z]?)\.\s*([^.\n]+?)\.\s*-?\s*(.*?)[”\"]?\.?\s*$",
        after_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not inserted:
        return None
    label = inserted.group(1)
    heading = re.sub(r"\s+", " ", inserted.group(2)).strip()
    content = inserted.group(3).strip()
    if not content:
        return None
    return {
        "anchor_component_id": _rule_component(after_rule),
        "component_id": _rule_component(label),
        "label": label.upper(),
        "heading": heading,
        "content": content,
    }


def _quoted_inserted_body(text: str) -> str:
    markers = list(re.finditer(r"\bnamely\s*[:-]", text, flags=re.IGNORECASE))
    if markers:
        tail = text[markers[-1].end() :]
        quoted = re.match(r"\s*[“\"―](.*?)[”\"‖]\s*[.;]?\s*$", tail, flags=re.DOTALL)
        if quoted:
            return _clean_structural_text(quoted.group(1))
        return _clean_structural_text(tail)
    return ""


def _strip_leading_numbered_label(text: str, label: str) -> str:
    clean = _clean_structural_text(text)
    clean = re.sub(rf"^[“\"―]?\s*\({re.escape(label)}\)\s*", "", clean, flags=re.IGNORECASE)
    return _clean_structural_text(clean)


def _parse_insert_structural_child_instruction(text: str, *, fallback_rule_label: str = "") -> dict[str, str] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    rule_match = re.search(r"\bin\s+rule\s+(\d+[A-Z]?)\b", normalized, flags=re.IGNORECASE)
    if not rule_match and not fallback_rule_label:
        return None
    rule_label = rule_match.group(1) if rule_match else fallback_rule_label
    parent_rule = _rule_component(rule_label)
    source_body = _quoted_inserted_body(text)
    if not source_body:
        return None

    subrule_insert = re.search(
        r"\bafter\s+sub-?rule\s+\((\d+[A-Z]?)\).*?\bfollowing\s+sub-?rule\s+(?:shall|may)\s+be\s+inserted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if subrule_insert:
        label_match = re.match(r"^[“\"―]?\s*\((\d+[A-Z]?)\)\s*(.*)$", source_body, flags=re.IGNORECASE | re.DOTALL)
        if not label_match:
            return None
        label = label_match.group(1)
        content = _clean_structural_text(label_match.group(2))
        if len(content) < 80 or re.search(r"[-:]\s*$", content):
            return None
        return {
            "anchor_component_id": _rule_child_component(rule_label, "subrule", subrule_insert.group(1)).lower(),
            "component_id": _rule_child_component(rule_label, "subrule", label).lower(),
            "content": content,
            "label": label.upper(),
            "node_type": "subrule",
            "parent_component_id": parent_rule.lower(),
        }

    proviso_insert = re.search(
        r"(?:\bin\s+sub-?rule\s+\((\d+[A-Z]?)\).*?)?"
        r"\b(?:after\s+(?:the\s+)?(?:first|second|third|said)?\s*proviso,?\s+)?"
        r"(?:the\s+)?following\s+proviso\s+(?:shall|may)\s+be\s+inserted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if proviso_insert and re.match(r"^Provided\b", source_body, flags=re.IGNORECASE):
        subrule_label = proviso_insert.group(1)
        parent_component = (
            _rule_child_component(rule_label, "subrule", subrule_label).lower()
            if subrule_label
            else parent_rule.lower()
        )
        label_match = re.match(r"^(Provided(?:\s+(?:further|also))?\s+that)\b", source_body, flags=re.IGNORECASE)
        label = re.sub(r"\s+", " ", label_match.group(1)).strip() if label_match else "proviso"
        return {
            "anchor_component_id": parent_component,
            "component_id": _child_component(parent_component, "proviso", label, source_body),
            "content": source_body,
            "label": label,
            "node_type": "proviso",
            "parent_component_id": parent_component,
        }

    explanation_insert = re.search(
        r"(?:\bin\s+sub-?rule\s+\((\d+[A-Z]?)\).*?)?"
        r"(?:\bafter\s+sub-?rule\s+\((\d+[A-Z]?)\).*?)?"
        r"\b(?:the\s+)?following\s+explanation\s+(?:shall|may)\s+be\s+inserted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if explanation_insert and re.match(r"^Explanation\b", source_body, flags=re.IGNORECASE):
        subrule_label = explanation_insert.group(1)
        anchor_subrule = explanation_insert.group(2)
        parent_component = (
            _rule_child_component(rule_label, "subrule", subrule_label).lower()
            if subrule_label
            else parent_rule.lower()
        )
        anchor_component = (
            _rule_child_component(rule_label, "subrule", anchor_subrule).lower()
            if anchor_subrule
            else parent_component
        )
        label = "Explanation"
        content = re.sub(r"^Explanation\s*[-.:]*\s*", "", source_body, flags=re.IGNORECASE).strip()
        if len(content) < 80 or re.search(r"[-,;:]\s*$", content):
            return None
        return {
            "anchor_component_id": anchor_component,
            "component_id": _child_component(parent_component, "explanation", label, content),
            "content": content,
            "label": label,
            "node_type": "explanation",
            "parent_component_id": parent_component,
        }
    return None


def _parse_insert_clause_instruction(text: str, *, fallback_rule_label: str = "") -> dict[str, str] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"\bin\s+rule\s+(\d+[A-Z]?)\b.*?\bafter\s+clause\s+\(([A-Za-z]+)\).*?"
        r"\bfollowing\s+clause\s*,?\s+shall\s+be\s+inserted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    subrule_match = None
    if not match and fallback_rule_label:
        subrule_match = re.search(
            r"\bin\s+sub-?rule\s+\((\d+[A-Z]?)\).*?\bafter\s+clause\s+\(([A-Za-z]+)\).*?"
            r"\bfollowing\s+clause\s*,?\s+shall\s+be\s+inserted\b",
            normalized,
            flags=re.IGNORECASE,
        )
    if not match and not subrule_match:
        return None
    rule_label = match.group(1) if match else fallback_rule_label
    subrule_label = subrule_match.group(1) if subrule_match else ""
    anchor_label = match.group(2) if match else subrule_match.group(2)
    match_end = match.end() if match else subrule_match.end()
    inserted = re.search(
        r"[“\"―‗‘']\s*\(([A-Za-z]+)\)\s*(.*?)[”\"’']?\.?\s*$",
        normalized[match_end:],
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not inserted:
        return None
    label = inserted.group(1)
    content = inserted.group(2).strip()
    content = re.split(r"\s+\[F\.\s*No\.|\s+Note\s*[:-]", content, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    content = _clean_structural_text(content)
    content = re.sub(r"\s*([.?!:])?[”\"]\s*[.;]?\s*$", lambda match: match.group(1) or "", content).strip()
    if not content or len(content) < 20:
        return None
    if subrule_label:
        return {
            "anchor_component_id": _subrule_child_component(rule_label, subrule_label, "clause", anchor_label),
            "component_id": _subrule_child_component(rule_label, subrule_label, "clause", label),
            "label": f"({label.lower()})",
            "content": content,
            "node_type": "clause",
            "parent_component_id": _rule_child_component(rule_label, "subrule", subrule_label),
        }
    return {
        "anchor_component_id": _rule_child_component(rule_label, "clause", anchor_label),
        "component_id": _rule_child_component(rule_label, "clause", label),
        "label": f"({label.lower()})",
        "content": content,
        "node_type": "clause",
        "parent_component_id": _rule_component(rule_label),
    }


def _parse_structural_subrule_substitute_instruction(text: str) -> dict[str, str] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"\bin\s+rule\s+(\d+[A-Z]?)\b.*?\bfor\s+sub-?rule\s+\((\d+[A-Z]?)\).*?"
        r"\bfollowing\s+sub-?rule\s+shall\s+be\s+substituted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    rule_label = match.group(1)
    subrule_label = match.group(2)
    content_match = re.search(r"\bnamely\s*[:-]\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
    if not content_match:
        return None
    content = _clean_structural_text(content_match.group(1))
    content = re.split(r"\s+\[F\.\s*No\.|\s+Note\s*[:-]", content, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    content = re.sub(r"^\s*[-:;]\s*", "", content).strip()
    if not re.match(rf"^[“\"―]?\s*\({re.escape(subrule_label)}\)(?:\s|$)", content, flags=re.IGNORECASE):
        return None
    if len(content) < 120:
        return None
    if re.search(r"(?:unless|namely)\s*,?\s*[-:]?\s*$", content, flags=re.IGNORECASE):
        return None
    return {
        "component_id": _rule_child_component(rule_label, "subrule", subrule_label),
        "parent_component_id": _rule_component(rule_label),
        "label": subrule_label,
        "node_type": "subrule",
        "structural_text": content,
    }


def _parse_payload_structural_subrule_substitute(event: dict[str, Any]) -> dict[str, str] | None:
    payload = event.get("payload") or {}
    target = event.get("target") or {}
    component_id = str(target.get("component_id") or "").lower()
    label = subrule_label_from_component(component_id)
    parent_component = parent_component_for_subrule(component_id)
    structural_text = _clean_structural_text(str(payload.get("structural_text") or ""))
    instruction = _event_instruction_text(event)
    if not label or not parent_component or not structural_text:
        return None
    rule_match = re.search(r"/rule/([^/]+)$", parent_component, flags=re.IGNORECASE)
    if not rule_match:
        return None
    rule_label = rule_match.group(1)
    if not re.search(
        rf"\bin\s+rule\s+{re.escape(rule_label)}\b.*?\bfor\s+sub-?rule\s+\({re.escape(label)}\).*?"
        r"\bfollowing(?:\s+sub-?rule)?\s+shall\s+be\s+substituted\b",
        instruction,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        return None
    if not re.match(rf"^[“\"―]?\s*\({re.escape(label)}\)(?:\s|$)", structural_text, flags=re.IGNORECASE):
        return None
    if len(structural_text) < 120:
        return None
    return {
        "component_id": component_id,
        "parent_component_id": parent_component,
        "label": label,
        "node_type": "subrule",
        "structural_text": structural_text,
    }


def _parse_whole_component_omit_instruction(text: str, fallback_effective: str = "") -> dict[str, str] | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    subrule_match = re.search(
        r"\bin\s+rule\s+(\d+[A-Z]?)\b.*?\bsub-?rule\s+\((\d+[A-Z]?)\)\s+shall\s+"
        r"(?:be\s+)?(?:deemed\s+to\s+have\s+been\s+)?omitted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    effective = _parse_effective_date(normalized) or fallback_effective
    if subrule_match and effective:
        rule_label = subrule_match.group(1)
        subrule_label = subrule_match.group(2)
        return {
            "component_id": _rule_child_component(rule_label, "subrule", subrule_label),
            "component_type": "subrule",
            "label": subrule_label,
            "applicability_start": effective,
        }

    rule_match = re.search(
        r"\brule\s+(\d+[A-Z]?)\s+shall\s+(?:be\s+deemed\s+to\s+have\s+been\s+)?omitted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if not rule_match or not effective:
        return None
    label = rule_match.group(1).upper()
    return {
        "component_id": _rule_component(label),
        "component_type": "rule",
        "label": label,
        "applicability_start": effective,
    }


def _parse_whole_rule_omit_instruction(text: str) -> dict[str, str] | None:
    parsed = _parse_whole_component_omit_instruction(text)
    if not parsed or parsed.get("component_type") != "rule":
        return None
    return parsed


def _parse_effective_date(text: str) -> str | None:
    match = re.search(
        r"\bwith\s+effect\s+from\s+(?:the\s+)?(.+?)(?=,?\s+(?:the\s+following|namely|in\s+rule|for\s+rule|after\s+rule)|[.;])",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    value = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", match.group(1), flags=re.IGNORECASE)
    value = re.sub(r"\bday\s+of\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" ,-")
    try:
        from dateutil import parser as date_parser

        return date_parser.parse(value, dayfirst=True, fuzzy=True).date().isoformat()
    except Exception:
        return None


def _first_quoted_text(text: str) -> str:
    match = re.search(r"[“\"]([^”\"]+)[”\"]", text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _source_span_start(event: dict[str, Any]) -> int:
    value = ((event.get("evidence") or {}).get("source_span") or {}).get("start")
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _inherited_rule_context(events: list[dict[str, Any]]) -> dict[str, str]:
    context: dict[str, str] = {}
    current_by_doc: dict[str, str] = {}
    ordered = sorted(
        events,
        key=lambda event: (str((event.get("source") or {}).get("document_id") or ""), _source_span_start(event)),
    )
    for event in ordered:
        source_doc = str((event.get("source") or {}).get("document_id") or "")
        if not source_doc:
            continue
        text = _event_full_text(event) or str((event.get("evidence") or {}).get("excerpt") or "")
        rule_header = re.match(
            r"^\s*(?:\([a-zivxlcdm]+\)\s*)?"
            r"(?:(?:with\s+effect\s+from|with\s+retrospective\s+effect\s+from)\s+.*?,\s*)?"
            r"in\s+rule\s+(\d+[A-Z]?)\s*[,–-]*\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if rule_header:
            current_by_doc[source_doc] = rule_header.group(1)
            continue
        if source_doc in current_by_doc and (
            str(event.get("operation") or "") == "UNKNOWN"
            or "target_not_resolved" in set((event.get("review") or {}).get("review_reasons") or [])
            or "anchor_not_resolved" in set((event.get("review") or {}).get("review_reasons") or [])
        ):
            event_id = str(event.get("event_id") or "")
            if event_id:
                context[event_id] = current_by_doc[source_doc]
    return context


def _inherited_effective_context(events: list[dict[str, Any]]) -> dict[str, str]:
    context: dict[str, str] = {}
    current_by_doc: dict[str, str] = {}
    ordered = sorted(
        events,
        key=lambda event: (str((event.get("source") or {}).get("document_id") or ""), _source_span_start(event)),
    )
    for event in ordered:
        source_doc = str((event.get("source") or {}).get("document_id") or "")
        if not source_doc:
            continue
        text = _event_full_text(event) or str((event.get("evidence") or {}).get("excerpt") or "")
        if re.match(
            r"^\s*(?:\([a-zivxlcdm]+\)\s*)?"
            r"(?:with\s+effect\s+from|with\s+retrospective\s+effect\s+from)\s+.*?,\s*"
            r"in\s+rule\s+\d+[A-Z]?\s*[,–-]*\s*$",
            text,
            flags=re.IGNORECASE,
        ):
            effective = _parse_effective_date(text)
            if effective:
                current_by_doc[source_doc] = effective
            continue
        if source_doc in current_by_doc and (
            str(event.get("operation") or "") == "UNKNOWN"
            or "target_not_resolved" in set((event.get("review") or {}).get("review_reasons") or [])
            or "anchor_not_resolved" in set((event.get("review") or {}).get("review_reasons") or [])
        ):
            event_id = str(event.get("event_id") or "")
            if event_id:
                context[event_id] = current_by_doc[source_doc]
    return context


def _parse_unknown_text_edit_instruction(
    event: dict[str, Any],
    source_archive_root: Path | None = None,
    *,
    inherited_rule_label: str = "",
) -> dict[str, str] | None:
    text = _event_instruction_text(event, source_archive_root)
    normalized = re.sub(r"\s+", " ", text).strip()
    target = event.get("target") or {}
    component_id = str(target.get("component_id") or "")
    if inherited_rule_label and component_id == RULES_WORK:
        subrule_match = re.search(r"\bin\s+sub-?rule\s+\((\d+[A-Z]?)\)", normalized, flags=re.IGNORECASE)
        component_id = (
            _rule_child_component(inherited_rule_label, "subrule", subrule_match.group(1)).lower()
            if subrule_match
            else _rule_component(inherited_rule_label).lower()
        )
    if not component_id.startswith(f"{RULES_WORK}/rule/"):
        return None

    omit_match = re.search(
        r"\b(?:the\s+)?(?:word|words|letters|figures|brackets)(?:,\s*(?:words|letters|figures|brackets))*\s+"
        r"[“\"]([^”\"]+)[”\"]\s+shall\s+be\s+omitted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if omit_match:
        return {
            "operation": "OMIT",
            "component_id": component_id,
            "omit_text": re.sub(r"\s+", " ", omit_match.group(1)).strip(),
        }

    substitute_match = re.search(
        r"\bfor\s+the\s+(?:word|words|letters|figures|brackets)(?:,\s*(?:words|letters|figures|brackets))*\s+"
        r"[“\"]([^”\"]+)[”\"]\s*,?\s+the\s+(?:word|words|letters|figures|brackets)(?:,\s*(?:words|letters|figures|brackets))*\s+"
        r"[“\"]([^”\"]+)[”\"]\s+shall\s+be\s+substituted\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if substitute_match:
        return {
            "operation": "SUBSTITUTE",
            "component_id": component_id,
            "old_text": re.sub(r"\s+", " ", substitute_match.group(1)).strip(),
            "new_text": re.sub(r"\s+", " ", substitute_match.group(2)).strip(),
        }

    # Some compact heading instructions say "for the word X, the word Y" and
    # use the same quoted-text form as ordinary substitutions; keep this as a
    # fallback only when the mandatory substituted verb is present.
    if "shall be substituted" in normalized.lower():
        quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", normalized, flags=re.DOTALL)
        if len(quoted) >= 2 and re.search(r"\bfor\s+the\s+", normalized, flags=re.IGNORECASE):
            return {
                "operation": "SUBSTITUTE",
                "component_id": component_id,
                "old_text": re.sub(r"\s+", " ", quoted[0]).strip(),
                "new_text": re.sub(r"\s+", " ", quoted[1]).strip(),
            }
    if "shall be omitted" in normalized.lower():
        quoted_text = _first_quoted_text(normalized)
        if quoted_text:
            return {"operation": "OMIT", "component_id": component_id, "omit_text": quoted_text}
    return None


def _event_full_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    return str(payload.get("text") or payload.get("structural_text") or "")


def _event_instruction_text(event: dict[str, Any], source_archive_root: Path | None = None) -> str:
    source_span_text = _event_source_span_text(event, source_archive_root)
    if source_span_text:
        return source_span_text
    payload_text = _event_full_text(event)
    excerpt = str((event.get("evidence") or {}).get("excerpt") or "")
    parts = [part for part in (source_span_text, excerpt, payload_text) if part]
    return "\n".join(parts)


def _safe_structural_substitute(event: dict[str, Any], triage: dict[str, Any] | None = None) -> bool:
    payload = event.get("payload") or {}
    target = event.get("target") or {}
    validation = event.get("validation") or {}
    component_id = str(target.get("component_id") or "")
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    if event.get("operation") != "SUBSTITUTE":
        return False
    if not component_id.startswith("/in/union/rules/cgst-rules-2017/rule/"):
        return False
    if not str(payload.get("structural_text") or "").strip():
        return False
    if not validation.get("target_resolved") or not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return False
    hard_reasons = {
        "compound_block_contains_multiple_amendments",
        "document_scope_target_not_materializable",
        "incomplete_text_edit_payload",
        "structural_substitution_label_not_verified",
        "target_component_outside_work",
        "unsafe_generic_substitution_anchor",
    }
    if reasons & hard_reasons:
        return False
    full_text = _event_instruction_text(event)
    if "/subrule/" in component_id and re.search(r"\bsub-rules?\s+\([^)]+\)\s+and\s+sub-rules?\s+\([^)]+\)", full_text, flags=re.IGNORECASE):
        return False
    if triage and triage.get("triage_class") not in {"needs_parser_support", "likely_materializable"}:
        return False
    return True


def _safe_insert_rule(event: dict[str, Any], triage: dict[str, Any] | None = None) -> dict[str, str] | None:
    validation = event.get("validation") or {}
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    if not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return None
    if "inserted_component_already_exists" in reasons:
        return None
    if triage and triage.get("triage_class") not in {"human_review", "likely_materializable", "needs_parser_support"}:
        return None
    full_text = _event_instruction_text(event)
    if re.search(r"\bdate\s+as\s+may\s+be\s+notified\b", full_text, flags=re.IGNORECASE):
        return None
    parsed = _parse_insert_rule_instruction(full_text)
    if not parsed:
        parsed = _parse_insert_structural_child_instruction(_event_instruction_text(event))
    if not parsed:
        return None
    return parsed


def _coverage_gaps(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        gaps = payload.get("gaps") or payload.get("coverage_gaps") or []
        return [row for row in gaps if isinstance(row, dict)]
    return []


def _missing_anchor_components(coverage_gaps_path: Path) -> dict[str, list[dict[str, Any]]]:
    missing: dict[str, list[dict[str, Any]]] = {}
    for gap in _coverage_gaps(coverage_gaps_path):
        reason = str(gap.get("skip_reason") or "")
        match = re.search(r"Anchor component missing:\s+(\S+)", reason)
        if not match:
            continue
        component_id = match.group(1).strip()
        missing.setdefault(component_id, []).append(gap)
    return missing


def _find_insertions_for_component(events: list[dict[str, Any]], component_id: str) -> list[tuple[dict[str, Any], dict[str, str]]]:
    matches: list[tuple[dict[str, Any], dict[str, str]]] = []
    for event in events:
        parsed = _parse_insert_rule_instruction(_event_instruction_text(event))
        if parsed and parsed.get("component_id") == component_id:
            matches.append((event, parsed))
    return matches


def _decision(event: dict[str, Any], *, strategy: str, operation: str, promote: dict[str, Any], notes: str) -> dict[str, Any]:
    effective = _parse_effective_date(_event_instruction_text(event))
    if effective and "applicability_start" not in promote:
        promote = {**promote, "applicability_start": effective}
    return {
        "decision": "approved",
        "event_id": event.get("event_id"),
        "notes": notes,
        "promote": {"operation": operation, "strategy": strategy, **promote},
        "reviewed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "reviewed_by": AUTO_REVIEW_VERSION,
    }


def _codex_decision(
    event: dict[str, Any],
    *,
    strategy: str,
    operation: str,
    promote: dict[str, Any],
    notes: str,
) -> dict[str, Any]:
    return {
        "decision": "approved",
        "event_id": event.get("event_id"),
        "notes": notes,
        "promote": {"operation": operation, "strategy": strategy, **promote},
        "reviewed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "reviewed_by": CODEX_REVIEW_VERSION,
        "source_basis": {
            "source_document_id": (event.get("source") or {}).get("document_id"),
            "source_span": (event.get("evidence") or {}).get("source_span", {}),
            "excerpt": (event.get("evidence") or {}).get("excerpt", ""),
            "target": event.get("target", {}),
        },
    }


def _codex_exact_text_decision(
    event: dict[str, Any],
    *,
    component_text: str,
) -> dict[str, Any] | None:
    operation = str(event.get("operation") or "")
    target = event.get("target") or {}
    payload = event.get("payload") or {}
    component_id = str(target.get("component_id") or "")
    validation = event.get("validation") or {}
    if event.get("status") == "validated":
        return None
    if target.get("work_id") != RULES_WORK:
        return None
    if not component_id.startswith(f"{RULES_WORK}/rule/"):
        return None
    if not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return None
    hard_reasons = _hard_review_reasons(event)
    allowed_exact_reasons = {
        "anchor_not_resolved",
        "compound_block_contains_multiple_amendments",
        "llm_candidate_not_validated",
        "same_effective_date_conflict",
        "target_not_resolved",
        "unsafe_generic_substitution_anchor",
        "document_scope_target_not_materializable",
        "context_recovered_target_pending_validation",
    }
    if hard_reasons - allowed_exact_reasons:
        return None

    if operation == "SUBSTITUTE":
        old_text = str(payload.get("old_text") or "").strip()
        new_text = str(payload.get("new_text") or "").strip()
        if not old_text or not new_text:
            return None
        exact_old = _normalized_exact_match(component_text, old_text)
        exact_new = _normalized_exact_match(component_text, new_text)
        if exact_old and exact_new:
            old_spans = _normalized_match_spans(component_text, old_text)
            new_spans = _normalized_match_spans(component_text, new_text)
            if len(new_spans) == 1 and old_spans and all(
                new_spans[0][0] <= old_start and old_end <= new_spans[0][1]
                for old_start, old_end in old_spans
            ):
                return _codex_decision(
                    event,
                    strategy="already_reflected_substitute_payload",
                    operation="SUBSTITUTE",
                    promote={"old_text": old_text, "new_text": exact_new, "noop_if_already_reflected": True},
                    notes=(
                        "Codex-approved: source instruction is a direct SUBSTITUTE and the only "
                        "remaining old-text occurrence is contained inside the already-reflected "
                        "replacement phrase. The event is materialized as an already-reflected no-op."
                    ),
                )
        if not exact_old:
            if not exact_new:
                return None
            return _codex_decision(
                event,
                strategy="already_reflected_substitute_payload",
                operation="SUBSTITUTE",
                promote={"old_text": old_text, "new_text": exact_new, "noop_if_already_reflected": True},
                notes=(
                    "Codex-approved: source instruction is a direct SUBSTITUTE, the old text no longer "
                    "appears in the reconstructed target component, and the replacement text resolves "
                    "to exactly one occurrence. The event is materialized as an already-reflected no-op."
                ),
            )
        return _codex_decision(
            event,
            strategy="exact_substitute_payload",
            operation="SUBSTITUTE",
            promote={"old_text": exact_old, "new_text": new_text},
            notes=(
                "Codex-approved: source instruction is a direct SUBSTITUTE and the old text resolves "
                "to exactly one occurrence in the reconstructed target component."
            ),
        )

    if operation == "SPLICE":
        anchor_text = str(target.get("anchor_text") or payload.get("anchor_text") or "").strip()
        insert_text = str(payload.get("insert_text") or "").strip()
        position = str(payload.get("position") or "after").strip().lower()
        if not anchor_text or not insert_text or position not in {"after", "before"}:
            return None
        exact_anchor = _normalized_exact_match(component_text, anchor_text)
        if not exact_anchor:
            exact_insert = _normalized_exact_match(component_text, insert_text)
            if not exact_insert:
                return None
            return _codex_decision(
                event,
                strategy="already_reflected_splice_payload",
                operation="SPLICE",
                promote={
                    "anchor_text": anchor_text,
                    "insert_text": exact_insert,
                    "position": position,
                    "noop_if_already_reflected": True,
                },
                notes=(
                    "Codex-approved: source instruction is a direct SPLICE, the unresolved anchor "
                    "does not appear in the reconstructed component, and the inserted text already "
                    "resolves to exactly one occurrence. The event is materialized as an already-"
                    "reflected no-op."
                ),
            )
        return _codex_decision(
            event,
            strategy="exact_splice_payload",
            operation="SPLICE",
            promote={"anchor_text": exact_anchor, "insert_text": insert_text, "position": position},
            notes=(
                "Codex-approved: source instruction is a direct SPLICE and the anchor text resolves "
                "to exactly one occurrence in the reconstructed target component."
            ),
        )

    if operation == "OMIT":
        omit_text = str(payload.get("omit_text") or "").strip()
        if not omit_text:
            return None
        exact_omit = _normalized_exact_match(component_text, omit_text)
        if not exact_omit:
            return _codex_decision(
                event,
                strategy="already_reflected_omit_payload",
                operation="OMIT",
                promote={"omit_text": omit_text, "noop_if_already_reflected": True},
                notes=(
                    "Codex-approved: source instruction is a direct OMIT and the omitted text no "
                    "longer appears in the reconstructed target component. The event is materialized "
                    "as an already-reflected no-op."
                ),
            )
        return _codex_decision(
            event,
            strategy="exact_omit_payload",
            operation="OMIT",
            promote={"omit_text": exact_omit},
            notes=(
                "Codex-approved: source instruction is a direct OMIT and the omitted text resolves "
                "to exactly one occurrence in the reconstructed target component."
            ),
        )

    return None


def _codex_unknown_text_edit_decision(
    event: dict[str, Any],
    *,
    component_text: str,
    source_archive_root: Path | None = None,
    inherited_rule_label: str = "",
) -> dict[str, Any] | None:
    if event.get("status") == "validated" or event.get("operation") != "UNKNOWN":
        return None
    validation = event.get("validation") or {}
    target = event.get("target") or {}
    if target.get("work_id") != RULES_WORK:
        return None
    if not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return None
    parsed = _parse_unknown_text_edit_instruction(
        event,
        source_archive_root,
        inherited_rule_label=inherited_rule_label,
    )
    if not parsed:
        return None
    hard_reasons = _hard_review_reasons(event)
    allowed_reasons = {
        "compound_block_contains_unsupported_omission",
        "llm_limit_not_attempted",
        "same_effective_date_conflict",
        "unparsed_target_work_amendment",
        "unsupported_materializer_operation",
    }
    if hard_reasons - allowed_reasons:
        return None
    if parsed["operation"] == "OMIT":
        omit_text = parsed.get("omit_text", "")
        exact_omit = _normalized_exact_match(component_text, omit_text)
        if exact_omit:
            return _codex_decision(
                event,
                strategy="source_backed_unknown_omit_payload",
                operation="OMIT",
                promote={"component_id": parsed["component_id"], "omit_text": exact_omit},
                notes=(
                    "Codex-approved: UNKNOWN event text parses to a direct OMIT instruction and "
                    "the omitted text resolves to exactly one occurrence in the reconstructed target."
                ),
            )
        if omit_text and not _normalized_match_spans(component_text, omit_text):
            return _codex_decision(
                event,
                strategy="source_backed_unknown_already_reflected_omit_payload",
                operation="OMIT",
                promote={
                    "component_id": parsed["component_id"],
                    "omit_text": omit_text,
                    "noop_if_already_reflected": True,
                },
                notes=(
                    "Codex-approved: UNKNOWN event text parses to a direct OMIT instruction and "
                    "the omitted text no longer appears in the reconstructed target. The event is "
                    "materialized as an already-reflected no-op."
                ),
            )
        return None
    if parsed["operation"] == "SUBSTITUTE":
        old_text = parsed.get("old_text", "")
        new_text = parsed.get("new_text", "")
        if not old_text or not new_text:
            return None
        exact_old = _normalized_exact_match(component_text, old_text)
        if exact_old:
            return _codex_decision(
                event,
                strategy="source_backed_unknown_substitute_payload",
                operation="SUBSTITUTE",
                promote={"component_id": parsed["component_id"], "old_text": exact_old, "new_text": new_text},
                notes=(
                    "Codex-approved: UNKNOWN event text parses to a direct SUBSTITUTE instruction "
                    "and the old text resolves to exactly one occurrence in the reconstructed target."
                ),
            )
        exact_new = _normalized_exact_match(component_text, new_text)
        if exact_new:
            return _codex_decision(
                event,
                strategy="source_backed_unknown_already_reflected_substitute_payload",
                operation="SUBSTITUTE",
                promote={
                    "component_id": parsed["component_id"],
                    "old_text": old_text,
                    "new_text": exact_new,
                    "noop_if_already_reflected": True,
                },
                notes=(
                    "Codex-approved: UNKNOWN event text parses to a direct SUBSTITUTE instruction, "
                    "the old text no longer appears in the reconstructed target, and the replacement "
                    "text resolves to exactly one occurrence. The event is materialized as an "
                    "already-reflected no-op."
                ),
            )
    return None


def _codex_whole_rule_omit_decision(
    event: dict[str, Any],
    *,
    versions: dict[str, list[dict[str, Any]]],
    source_archive_root: Path | None = None,
) -> dict[str, Any] | None:
    if event.get("status") == "validated":
        return None
    if event.get("operation") != "OMIT":
        return None
    target = event.get("target") or {}
    validation = event.get("validation") or {}
    component_id = str(target.get("component_id") or "")
    if target.get("work_id") != RULES_WORK:
        return None
    if not component_id.startswith(f"{RULES_WORK}/rule/"):
        return None
    if not validation.get("source_span_verified"):
        return None
    parsed = _parse_whole_component_omit_instruction(
        _event_instruction_text(event, source_archive_root),
        fallback_effective=_date_key(event),
    )
    if not parsed:
        return None
    if parsed["component_id"].lower() != component_id.lower():
        return None
    target_exists = _component_has_version(versions, component_id.lower()) or _component_has_version(versions, component_id)
    parent_span = None
    if not target_exists:
        parent_span = _parent_subrule_span_available(versions, component_id.lower(), parsed["applicability_start"])
        if parent_span is None:
            return None
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    allowed_reasons = {
        "llm_candidate_not_validated",
        "omit_phrase_not_found",
        "same_effective_date_conflict",
        "target_not_resolved",
    }
    if reasons - allowed_reasons:
        return None
    component_type = str(parsed.get("component_type") or "rule")
    if parent_span:
        strategy = "whole_parent_span_subrule_omit_instruction"
    else:
        strategy = "whole_rule_omit_instruction" if component_type == "rule" else "whole_component_omit_instruction"
    promote = {
        "component_id": parsed["component_id"].lower(),
        "whole_component": True,
        "applicability_start": parsed["applicability_start"],
    }
    if parent_span:
        promote.update(
            {
                "apply_to_parent_subrule_span": True,
                "label": parent_span[1],
                "parent_component_id": parent_span[0],
            }
        )
    return _codex_decision(
        event,
        strategy=strategy,
        operation="OMIT",
        promote=promote,
        notes=(
            f"Codex-approved: source instruction expressly omits the whole named {component_type}; "
            "when the subrule is embedded in the parent rule, the parent has one unique top-level "
            "subrule span for that label."
        ),
    )


def _component_has_version(versions: dict[str, list[dict[str, Any]]], component_id: str) -> bool:
    return bool(versions.get(component_id))


def _parent_subrule_span_available(
    versions: dict[str, list[dict[str, Any]]],
    component_id: str,
    date_value: str,
    *,
    structural_text: str = "",
) -> tuple[str, str] | None:
    parent_component = parent_component_for_subrule(component_id)
    label = subrule_label_from_component(component_id)
    if not parent_component or not label:
        return None
    parent_text = _version_text_at(versions, parent_component, date_value)
    if not parent_text:
        return None
    if find_top_level_subrule_span(parent_text, label) is None:
        return None
    if structural_text:
        normalized = re.sub(r"\s+", " ", structural_text).strip()
        if not re.match(rf"^[“\"―]?\s*\({re.escape(label)}\)(?:\s|$)", normalized, flags=re.IGNORECASE):
            return None
    return parent_component, label


def _insert_rule_body_from_payload(event: dict[str, Any], parsed: dict[str, str]) -> dict[str, str]:
    payload = event.get("payload") or {}
    label = str(parsed.get("label") or payload.get("label") or "").strip()
    heading = str(parsed.get("heading") or payload.get("heading") or "").strip()
    content = str(parsed.get("content") or payload.get("content") or "").strip()
    payload_content = str(payload.get("content") or "").strip()
    split = re.match(r"([^.\n]+?)\.\s*-?\s*(.*)$", payload_content, flags=re.DOTALL)
    if split and len(split.group(2).strip()) > len(content):
        heading = re.sub(r"\s+", " ", split.group(1)).strip()
        content = split.group(2).strip()
    return {"label": label, "heading": heading, "content": content}


def _codex_insert_body_is_complete(body: dict[str, str]) -> bool:
    content = str(body.get("content") or "")
    if not body.get("label") or not body.get("heading") or not content:
        return False
    if len(content) < 300 or not re.search(r"\(\d+[A-Za-z]?\)", content):
        return False
    if re.search(r"\bIn\s+the\s+said\s+rules\b|\bfollowing\s+rule\s+shall\s+be\s+inserted\b", content, flags=re.I):
        return False
    return True


def _prior_codex_decision_valid(
    row: dict[str, Any],
    *,
    versions: dict[str, list[dict[str, Any]]] | None = None,
    event: dict[str, Any] | None = None,
) -> bool:
    promote = row.get("promote") or {}
    if promote.get("strategy") != "source_backed_insert_rule":
        if promote.get("strategy") in {
            "source_backed_parent_span_subrule_substitute",
            "source_backed_detached_subrule_substitute",
        }:
            if (
                versions is not None
                and event is not None
                and not _component_has_version(versions, str(promote.get("component_id") or "").lower())
            ):
                anchor_span = _parent_subrule_span_available(
                    versions,
                    str(promote.get("component_id") or "").lower(),
                    str(promote.get("applicability_start") or _date_key(event) or ""),
                )
                if anchor_span is None and not promote.get("allow_detached_component_version"):
                    return False
            return True
        if promote.get("strategy") in {
            "source_backed_insert_child_clause",
            "source_backed_insert_child",
            "source_backed_parent_span_insert_child",
        }:
            content = str(promote.get("content") or "").strip()
            if re.search(r"[”\"]\s*[.;]?\s*$", content):
                return False
            if re.search(
                r"\bfollowing\s+(?:sub-?rule|clause|proviso|explanation)\s+(?:shall|may)\s+be\s+inserted\b",
                content,
                flags=re.IGNORECASE,
            ):
                return False
            if (
                versions is not None
                and event is not None
                and promote.get("node_type") == "subrule"
                and not _component_has_version(versions, str(promote.get("anchor_component_id") or "").lower())
            ):
                anchor_span = _parent_subrule_span_available(
                    versions,
                    str(promote.get("anchor_component_id") or "").lower(),
                    _date_key(event),
                )
                if anchor_span is None:
                    return False
            return True
        return True
    return _codex_insert_body_is_complete(
        {
            "label": str(promote.get("label") or ""),
            "heading": str(promote.get("heading") or ""),
            "content": str(promote.get("content") or ""),
        }
    ) and not str(promote.get("content") or "").lstrip().startswith("- ")


def _codex_insert_rule_decision(
    event: dict[str, Any],
    *,
    versions: dict[str, list[dict[str, Any]]],
    source_archive_root: Path | None = None,
) -> dict[str, Any] | None:
    if event.get("status") == "validated":
        return None
    target = event.get("target") or {}
    validation = event.get("validation") or {}
    if target.get("work_id") != RULES_WORK:
        return None
    if not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return None
    text = _event_instruction_text(event, source_archive_root)
    if re.search(r"\b(?:date|day)\s+(?:as\s+may\s+be\s+)?(?:to\s+be\s+)?notified\b|\bto\s+be\s+notified\s+later\b", text, flags=re.I):
        return None
    parsed = _parse_insert_rule_instruction(text)
    if not parsed:
        return None
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    allowed_reasons = {
        "anchor_not_resolved",
        "inserted_component_already_exists",
        "inserted_rule_label_not_found",
        "llm_candidate_not_validated",
        "same_effective_date_conflict",
        "target_component_outside_work",
        "target_not_resolved",
        "unsupported_materializer_operation",
    }
    if reasons - allowed_reasons:
        return None
    anchor_component_id = parsed["anchor_component_id"].lower()
    component_id = parsed["component_id"].lower()
    if not _component_has_version(versions, anchor_component_id):
        return None
    if _component_has_version(versions, component_id):
        return None
    body = _insert_rule_body_from_payload(event, parsed)
    if not _codex_insert_body_is_complete(body):
        return None
    return _codex_decision(
        event,
        strategy="source_backed_insert_rule",
        operation="INSERT_SIBLING",
        promote={
            "anchor_component_id": anchor_component_id,
            "component_id": component_id,
            "label": body["label"].upper(),
            "heading": body["heading"],
            "content": body["content"],
        },
        notes=(
            "Codex-approved: source instruction deterministically inserts a complete rule after an "
            "existing anchor rule; the previous review failure was a parsed target/label issue."
        ),
    )


def _codex_insert_child_decision(
    event: dict[str, Any],
    *,
    versions: dict[str, list[dict[str, Any]]],
    source_archive_root: Path | None = None,
    inherited_rule_label: str = "",
    inherited_effective_date: str = "",
) -> dict[str, Any] | None:
    if event.get("status") == "validated":
        return None
    target = event.get("target") or {}
    validation = event.get("validation") or {}
    if target.get("work_id") != RULES_WORK:
        return None
    if not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return None
    fallback_rule_label = ""
    target_component = str(target.get("component_id") or "")
    match = re.search(r"/rule/([^/]+)$", target_component, flags=re.IGNORECASE)
    if match:
        fallback_rule_label = match.group(1)
    if not fallback_rule_label:
        parent_match = re.search(
            r"/rule/([^/]+)(?:/|$)",
            str((event.get("payload") or {}).get("parent_component_id") or ""),
            flags=re.IGNORECASE,
        )
        if parent_match:
            fallback_rule_label = parent_match.group(1)
    if not fallback_rule_label and inherited_rule_label:
        fallback_rule_label = inherited_rule_label
    instruction_text = _event_instruction_text(event, source_archive_root)
    parsed = _parse_insert_clause_instruction(
        instruction_text,
        fallback_rule_label=fallback_rule_label,
    )
    if not parsed:
        parsed = _parse_insert_structural_child_instruction(
            instruction_text,
            fallback_rule_label=fallback_rule_label,
        )
    source_window = _event_source_instruction_window(event, source_archive_root)
    window_parsed = None
    if source_window:
        window_parsed = _parse_insert_clause_instruction(source_window, fallback_rule_label=fallback_rule_label)
        if not window_parsed:
            window_parsed = _parse_insert_structural_child_instruction(source_window, fallback_rule_label=fallback_rule_label)
    if not parsed:
        parsed = window_parsed
    elif (
        window_parsed
        and window_parsed.get("node_type") == parsed.get("node_type")
        and str(window_parsed.get("label") or "").lower() == str(parsed.get("label") or "").lower()
        and len(str(window_parsed.get("content") or "")) > len(str(parsed.get("content") or ""))
    ):
        parsed = window_parsed
    if not parsed:
        return None
    payload = event.get("payload") or {}
    recovered_parent = str(payload.get("parent_component_id") or "").lower()
    target_component = str(target.get("component_id") or "").lower()
    if (
        recovered_parent.startswith(f"{RULES_WORK}/rule/")
        and str(parsed.get("node_type") or "") in {"clause", "proviso", "explanation"}
        and target_component.startswith(recovered_parent + f"/{parsed['node_type']}/")
    ):
        parsed = {**parsed}
        parsed["parent_component_id"] = recovered_parent
        parsed["anchor_component_id"] = recovered_parent
        parsed["component_id"] = target_component
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    allowed_reasons = {
        "anchor_not_resolved",
        "llm_limit_not_attempted",
        "llm_candidate_not_validated",
        "same_effective_date_conflict",
        "target_not_resolved",
        "unparsed_target_work_amendment",
        "unsupported_materializer_operation",
        "inserted_component_already_exists",
        "context_recovered_target_pending_validation",
    }
    if reasons - allowed_reasons:
        return None
    parent_component_id = parsed["parent_component_id"].lower()
    component_id = parsed["component_id"].lower()
    parent_span = None
    if not _component_has_version(versions, parent_component_id):
        if str(parsed.get("node_type") or "") not in {"proviso", "explanation"}:
            return None
        parent_span = _parent_subrule_span_available(versions, parent_component_id, _date_key(event))
        if parent_span is None:
            return None
    if str(parsed.get("node_type") or "") == "subrule" and not _component_has_version(
        versions, parsed["anchor_component_id"].lower()
    ):
        span_date = inherited_effective_date or _date_key(event)
        anchor_span = _parent_subrule_span_available(versions, parsed["anchor_component_id"].lower(), span_date)
        if anchor_span is None:
            return None
        parent_span = anchor_span
    if _component_has_version(versions, component_id):
        return None
    strategy = (
        "source_backed_insert_child_clause"
        if str(parsed.get("node_type") or "") == "clause"
        else "source_backed_parent_span_insert_child" if parent_span else "source_backed_insert_child"
    )
    promote = {
        "anchor_component_id": parsed["anchor_component_id"].lower(),
        "component_id": component_id,
        "content": parsed["content"],
        "label": parsed["label"],
        "node_type": parsed["node_type"],
        "parent_component_id": parent_component_id,
    }
    if parent_span:
        promote["apply_to_parent_subrule_span"] = True
        promote["parent_component_id"] = parent_span[0]
        promote["subrule_label"] = parent_span[1]
        if str(parsed.get("node_type") or "") == "subrule":
            label = str(parsed.get("label") or "").strip()
            content = str(parsed.get("content") or "").strip()
            if label and not re.match(rf"^\(?{re.escape(label)}\)?(?:\s|$)", content, flags=re.IGNORECASE):
                promote["content"] = f"({label}) {content}"
    if inherited_effective_date and "applicability_start" not in promote:
        promote["applicability_start"] = inherited_effective_date
    return _codex_decision(
        event,
        strategy=strategy,
        operation="INSERT_CHILD",
        promote=promote,
        notes=(
            "Codex-approved: source instruction deterministically inserts a complete child "
            "component into an existing provision; v1 records the child as a first-class component "
            "and appends it to the parent provision for reconstruction."
        ),
    )


def _codex_structural_subrule_substitute_decision(
    event: dict[str, Any],
    *,
    versions: dict[str, list[dict[str, Any]]],
    source_archive_root: Path | None = None,
) -> dict[str, Any] | None:
    if event.get("status") == "validated":
        return None
    if event.get("operation") != "SUBSTITUTE":
        return None
    target = event.get("target") or {}
    validation = event.get("validation") or {}
    component_id = str(target.get("component_id") or "")
    if target.get("work_id") != RULES_WORK:
        return None
    if not validation.get("date_resolved") or not validation.get("source_span_verified"):
        return None
    parsed = _parse_structural_subrule_substitute_instruction(_event_instruction_text(event, source_archive_root))
    if not parsed:
        parsed = _parse_payload_structural_subrule_substitute(event)
    if not parsed:
        return None
    if parsed["component_id"].lower() != component_id.lower():
        return None
    target_exists = _component_has_version(versions, component_id.lower())
    parent_span = None
    if not target_exists:
        parent_span = _parent_subrule_span_available(
            versions,
            component_id.lower(),
            _date_key(event),
            structural_text=parsed["structural_text"],
        )
        parent_component_text = _version_text_at(versions, parsed["parent_component_id"].lower(), _date_key(event))
        if parent_span is None and not parent_component_text:
            return None
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    allowed_reasons = {
        "anchor_not_resolved",
        "llm_candidate_not_validated",
        "same_effective_date_conflict",
        "target_not_resolved",
    }
    if reasons - allowed_reasons:
        return None
    strategy = (
        "source_backed_structural_subrule_substitute"
        if target_exists
        else "source_backed_parent_span_subrule_substitute" if parent_span else "source_backed_detached_subrule_substitute"
    )
    promote = {
        "component_id": parsed["component_id"].lower(),
        "label": parsed["label"],
        "node_type": parsed["node_type"],
        "parent_component_id": parsed["parent_component_id"].lower(),
        "structural_text": parsed["structural_text"],
    }
    if parent_span:
        promote["apply_to_parent_subrule_span"] = True
        promote["parent_component_id"] = parent_span[0]
    elif not target_exists:
        promote["allow_detached_component_version"] = True
    return _codex_decision(
        event,
        strategy=strategy,
        operation="SUBSTITUTE",
        promote=promote,
        notes=(
            "Codex-approved: source instruction substitutes one complete existing sub-rule and the "
            "quoted replacement body is complete. If the parent lacks a unique top-level subrule span, "
            "v1 creates a detached first-class component version and keeps parent coverage incomplete."
        ),
    )


def generate_codex_review_decisions(
    *,
    events_path: Path,
    node_versions_path: Path,
    output: Path,
    existing_decision_paths: list[Path] | None = None,
    source_archive_root: Path | None = Path("sources"),
    limit: int | None = None,
) -> dict[str, Any]:
    """Approve narrow exact text edits after deterministic source/component checks."""
    events = _read_jsonl(events_path)
    event_by_id = {str(event.get("event_id") or ""): event for event in events if event.get("event_id")}
    inherited_context = _inherited_rule_context(events)
    inherited_effective_dates = _inherited_effective_context(events)
    clean_slots = _clean_materialized_slots(events)
    clean_slot_event_ids = _clean_materialized_slot_event_ids(events)
    versions = _load_node_versions(node_versions_path)
    prior_codex_rows = []
    stale_prior_count = 0
    for row in _load_decision_rows(output):
        event = event_by_id.get(str(row.get("event_id") or ""))
        if not _prior_codex_decision_valid(row, versions=versions, event=event):
            stale_prior_count += 1
            continue
        if event and _covered_by_other_clean_event(event, row, clean_slot_event_ids):
            stale_prior_count += 1
            continue
        prior_codex_rows.append(row)
    prior_codex = {str(row.get("event_id")): row for row in prior_codex_rows if row.get("event_id")}
    existing = {**_load_decision_paths(existing_decision_paths or []), **prior_codex}
    decisions: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_hard_reason = 0
    skipped_no_component_text = 0
    skipped_no_exact_match = 0
    exact_text_allowed_hard_reasons = {
        "anchor_not_resolved",
        "compound_block_contains_multiple_amendments",
        "llm_candidate_not_validated",
        "same_effective_date_conflict",
        "target_not_resolved",
        "unsafe_generic_substitution_anchor",
        "document_scope_target_not_materializable",
        "context_recovered_target_pending_validation",
    }

    for event in events:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            continue
        if event_id in existing:
            skipped_existing += 1
            continue
        if event.get("operation") not in {"SUBSTITUTE", "OMIT", "SPLICE", "INSERT_SIBLING", "INSERT_CHILD", "UNKNOWN"}:
            continue
        decision: dict[str, Any] | None = None
        if event.get("operation") == "SUBSTITUTE":
            decision = _codex_structural_subrule_substitute_decision(
                event,
                versions=versions,
                source_archive_root=source_archive_root,
            )
            if decision:
                decisions.append(decision)
                if limit is not None and len(decisions) >= limit:
                    break
                continue
            if _hard_review_reasons(event) - exact_text_allowed_hard_reasons:
                skipped_hard_reason += 1
                continue
        if event.get("operation") == "OMIT":
            decision = _codex_whole_rule_omit_decision(
                event,
                versions=versions,
                source_archive_root=source_archive_root,
            )
            if decision:
                decisions.append(decision)
                if limit is not None and len(decisions) >= limit:
                    break
                continue
            if _hard_review_reasons(event) - exact_text_allowed_hard_reasons:
                skipped_hard_reason += 1
                continue
        if event.get("operation") in {"SUBSTITUTE", "OMIT", "SPLICE"}:
            component_id = str((event.get("target") or {}).get("component_id") or "")
            component_text = _version_text_at(versions, component_id, _date_key(event))
            if not component_text:
                skipped_no_component_text += 1
                continue
            decision = _codex_exact_text_decision(event, component_text=component_text)
        elif event.get("operation") == "INSERT_CHILD":
            decision = _codex_insert_child_decision(
                event,
                versions=versions,
                source_archive_root=source_archive_root,
                inherited_rule_label=inherited_context.get(event_id, ""),
                inherited_effective_date=inherited_effective_dates.get(event_id, ""),
            )
        else:
            component_id = str((event.get("target") or {}).get("component_id") or "")
            inherited_rule_label = inherited_context.get(event_id, "")
            if event.get("operation") == "UNKNOWN" and inherited_rule_label and component_id == RULES_WORK:
                text = _event_instruction_text(event, source_archive_root)
                subrule_match = re.search(r"\bin\s+sub-?rule\s+\((\d+[A-Z]?)\)", text, flags=re.IGNORECASE)
                component_id = (
                    _rule_child_component(inherited_rule_label, "subrule", subrule_match.group(1)).lower()
                    if subrule_match
                    else _rule_component(inherited_rule_label).lower()
                )
            component_text = _version_text_at(versions, component_id, _date_key(event))
            if component_text:
                decision = _codex_unknown_text_edit_decision(
                    event,
                    component_text=component_text,
                    source_archive_root=source_archive_root,
                    inherited_rule_label=inherited_rule_label,
                )
            else:
                skipped_no_component_text += 1
            if not decision:
                decision = _codex_insert_child_decision(
                    event,
                    versions=versions,
                    source_archive_root=source_archive_root,
                    inherited_rule_label=inherited_rule_label,
                    inherited_effective_date=inherited_effective_dates.get(event_id, ""),
                )
            if not decision:
                decision = _codex_insert_rule_decision(event, versions=versions, source_archive_root=source_archive_root)
        if not decision:
            skipped_no_exact_match += 1
            continue
        if _covered_by_other_clean_event(event, decision, clean_slot_event_ids):
            skipped_existing += 1
            continue
        decisions.append(decision)
        if limit is not None and len(decisions) >= limit:
            break

    payload = {
        "version": CODEX_REVIEW_VERSION,
        "events_path": str(events_path),
        "node_versions_path": str(node_versions_path),
        "existing_decision_paths": [str(path) for path in existing_decision_paths or []],
        "source_archive_root": str(source_archive_root) if source_archive_root else None,
        "decision_count": len(prior_codex_rows) + len(decisions),
        "skipped_existing_count": skipped_existing,
        "skipped_hard_reason_count": skipped_hard_reason,
        "skipped_no_component_text_count": skipped_no_component_text,
        "skipped_no_exact_match_count": skipped_no_exact_match,
        "stale_prior_count": stale_prior_count,
        "new_decision_count": len(decisions),
        "decisions": prior_codex_rows + decisions,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "version": CODEX_REVIEW_VERSION,
        "output": str(output),
        "decision_count": len(prior_codex_rows) + len(decisions),
        "new_decision_count": len(decisions),
        "event_ids": [row["event_id"] for row in prior_codex_rows + decisions],
        "new_event_ids": [row["event_id"] for row in decisions],
        "skipped_existing_count": skipped_existing,
        "skipped_hard_reason_count": skipped_hard_reason,
        "skipped_no_component_text_count": skipped_no_component_text,
        "skipped_no_exact_match_count": skipped_no_exact_match,
        "stale_prior_count": stale_prior_count,
    }


def generate_auto_review_decisions(
    *,
    events_path: Path,
    output: Path,
    triage_path: Path | None = None,
    existing_decisions_path: Path | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    events = _read_jsonl(events_path)
    triage = _load_triage(triage_path)
    existing = _load_decisions(existing_decisions_path) if existing_decisions_path else {}
    clean_slots = _clean_materialized_slots(events)
    decisions: list[dict[str, Any]] = []
    skipped_existing = 0
    for event in events:
        event_id = str(event.get("event_id") or "")
        if not event_id or event.get("status") == "validated":
            continue
        if event_id in existing:
            skipped_existing += 1
            continue
        if _event_slot(event) in clean_slots and not _clean_materializable(event):
            skipped_existing += 1
            continue
        triage_row = triage.get(event_id)
        decision: dict[str, Any] | None = None
        if _safe_structural_substitute(event, triage_row):
            decision = _decision(
                event,
                strategy="structural_substitute_existing_payload",
                operation="SUBSTITUTE",
                promote={},
                notes="Auto-approved: complete structural SUBSTITUTE payload with resolved target/date/source span; unresolved anchor is not required for whole-component replacement.",
            )
        else:
            parsed_insert = _safe_insert_rule(event, triage_row)
            if parsed_insert:
                decision = _decision(
                    event,
                    strategy="insert_rule_from_instruction_text",
                    operation="INSERT_SIBLING",
                    promote={
                        "anchor_component_id": parsed_insert["anchor_component_id"],
                        "component_id": parsed_insert["component_id"],
                    },
                    notes="Auto-approved: deterministic 'after rule X insert rule Y' instruction with parsed target rule and anchor rule.",
                )
        if decision:
            if _decision_slot(decision, event) in clean_slots and not _clean_materializable(event):
                skipped_existing += 1
                continue
            decisions.append(decision)
            if limit is not None and len(decisions) >= limit:
                break
    payload = {
        "version": AUTO_REVIEW_VERSION,
        "events_path": str(events_path),
        "triage_path": str(triage_path) if triage_path else None,
        "existing_decisions_path": str(existing_decisions_path) if existing_decisions_path else None,
        "decision_count": len(decisions),
        "skipped_existing_count": skipped_existing,
        "decisions": decisions,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "version": AUTO_REVIEW_VERSION,
        "output": str(output),
        "decision_count": len(decisions),
        "skipped_existing_count": skipped_existing,
        "event_ids": [row["event_id"] for row in decisions],
    }


def generate_dependency_review_decisions(
    *,
    events_path: Path,
    coverage_gaps_path: Path,
    output: Path,
    existing_decision_paths: list[Path] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Approve mechanically clear events that create missing anchor components."""
    events = _read_jsonl(events_path)
    existing = _load_decision_paths(existing_decision_paths or [])
    missing = _missing_anchor_components(coverage_gaps_path)
    decisions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    approved_components: list[str] = []

    for component_id, dependent_gaps in sorted(missing.items()):
        candidates = [
            (event, parsed)
            for event, parsed in _find_insertions_for_component(events, component_id)
            if str(event.get("event_id") or "") not in existing
        ]
        if len(candidates) == 1:
            event, parsed = candidates[0]
            decisions.append(
                _decision(
                    event,
                    strategy="insert_rule_from_instruction_text",
                    operation="INSERT_SIBLING",
                    promote={
                        "anchor_component_id": parsed["anchor_component_id"],
                        "component_id": parsed["component_id"],
                    },
                    notes=(
                        "Dependency auto-approved: this mechanically parsed insertion creates "
                        f"missing anchor {component_id} required by {len(dependent_gaps)} failed materialization event(s)."
                    ),
                )
            )
            approved_components.append(component_id)
            if limit is not None and len(decisions) >= limit:
                break
            continue
        unresolved.append(
            {
                "missing_component_id": component_id,
                "dependent_event_ids": [gap.get("event_id") for gap in dependent_gaps],
                "candidate_event_ids": [event.get("event_id") for event, _ in candidates],
                "reason": "no_candidate_found" if not candidates else "ambiguous_candidates",
            }
        )

    payload = {
        "version": DEPENDENCY_REVIEW_VERSION,
        "events_path": str(events_path),
        "coverage_gaps_path": str(coverage_gaps_path),
        "existing_decision_paths": [str(path) for path in existing_decision_paths or []],
        "missing_anchor_count": len(missing),
        "approved_component_ids": approved_components,
        "decision_count": len(decisions),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "decisions": decisions,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "version": DEPENDENCY_REVIEW_VERSION,
        "output": str(output),
        "missing_anchor_count": len(missing),
        "decision_count": len(decisions),
        "approved_component_ids": approved_components,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "event_ids": [row["event_id"] for row in decisions],
    }


def _promote_event(event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    promoted = json.loads(json.dumps(event))
    promote = decision.get("promote") or {}
    operation = str(promote.get("operation") or promoted.get("operation") or "")
    strategy = str(promote.get("strategy") or "")
    promoted["operation"] = operation

    if operation == "SUBSTITUTE" and strategy == "structural_substitute_existing_payload":
        payload = dict(promoted.get("payload") or {})
        payload["structural_text"] = _clean_structural_text(str(payload.get("structural_text") or ""))
        promoted["payload"] = payload
        promoted.setdefault("target", {})["anchor_resolved_by_review"] = True

    if operation == "SUBSTITUTE" and strategy in {
        "exact_substitute_payload",
        "already_reflected_substitute_payload",
        "source_backed_unknown_substitute_payload",
        "source_backed_unknown_already_reflected_substitute_payload",
    }:
        payload = dict(promoted.get("payload") or {})
        payload["old_text"] = str(promote.get("old_text") or payload.get("old_text") or "")
        payload["new_text"] = str(promote.get("new_text") or payload.get("new_text") or "")
        if promote.get("noop_if_already_reflected"):
            payload["noop_if_already_reflected"] = True
        promoted["payload"] = payload
        target = promoted.setdefault("target", {})
        target["anchor_resolved_by_codex"] = True
        if promote.get("component_id"):
            target["component_id"] = promote.get("component_id")

    if operation == "OMIT" and strategy in {
        "exact_omit_payload",
        "already_reflected_omit_payload",
        "source_backed_unknown_omit_payload",
        "source_backed_unknown_already_reflected_omit_payload",
    }:
        payload = dict(promoted.get("payload") or {})
        payload["omit_text"] = str(promote.get("omit_text") or payload.get("omit_text") or "")
        payload["whole_component"] = False
        if promote.get("noop_if_already_reflected"):
            payload["noop_if_already_reflected"] = True
        promoted["payload"] = payload
        target = promoted.setdefault("target", {})
        target["anchor_resolved_by_codex"] = True
        if promote.get("component_id"):
            target["component_id"] = promote.get("component_id")

    if operation == "SPLICE" and strategy in {"exact_splice_payload", "already_reflected_splice_payload"}:
        payload = dict(promoted.get("payload") or {})
        payload["insert_text"] = str(promote.get("insert_text") or payload.get("insert_text") or "")
        payload["position"] = str(promote.get("position") or payload.get("position") or "after")
        if promote.get("noop_if_already_reflected"):
            payload["noop_if_already_reflected"] = True
        promoted["payload"] = payload
        promoted.setdefault("target", {})["anchor_text"] = str(
            promote.get("anchor_text") or (promoted.get("target") or {}).get("anchor_text") or ""
        )
        promoted.setdefault("target", {})["anchor_occurrence"] = 1
        promoted.setdefault("target", {})["anchor_resolved_by_codex"] = True
        if promote.get("component_id"):
            promoted.setdefault("target", {})["component_id"] = promote.get("component_id")

    if operation == "OMIT" and strategy in {
        "whole_rule_omit_instruction",
        "whole_component_omit_instruction",
        "whole_parent_span_subrule_omit_instruction",
    }:
        payload = dict(promoted.get("payload") or {})
        payload.pop("omit_text", None)
        payload["whole_component"] = True
        if promote.get("apply_to_parent_subrule_span"):
            payload["apply_to_parent_subrule_span"] = True
            payload["label"] = str(promote.get("label") or payload.get("label") or "")
            payload["parent_component_id"] = str(
                promote.get("parent_component_id") or payload.get("parent_component_id") or ""
            )
        promoted["payload"] = payload
        promoted["target"] = {
            **(promoted.get("target") or {}),
            "anchor_resolved_by_codex": True,
            "component_id": promote.get("component_id") or (promoted.get("target") or {}).get("component_id"),
        }

    if operation == "SUBSTITUTE" and strategy in {
        "source_backed_structural_subrule_substitute",
        "source_backed_parent_span_subrule_substitute",
        "source_backed_detached_subrule_substitute",
    }:
        payload = dict(promoted.get("payload") or {})
        payload["label"] = str(promote.get("label") or payload.get("label") or "")
        payload["node_type"] = str(promote.get("node_type") or payload.get("node_type") or "subrule")
        payload["parent_component_id"] = str(promote.get("parent_component_id") or payload.get("parent_component_id") or "")
        if promote.get("apply_to_parent_subrule_span"):
            payload["apply_to_parent_subrule_span"] = True
        if promote.get("allow_detached_component_version"):
            payload["allow_detached_component_version"] = True
        payload["structural_text"] = _clean_structural_text(
            str(promote.get("structural_text") or payload.get("structural_text") or "")
        )
        promoted["payload"] = payload
        promoted["target"] = {
            **(promoted.get("target") or {}),
            "anchor_resolved_by_codex": True,
            "component_id": promote.get("component_id") or (promoted.get("target") or {}).get("component_id"),
        }

    if operation == "INSERT_CHILD" and strategy in {
        "source_backed_insert_child_clause",
        "source_backed_insert_child",
        "source_backed_parent_span_insert_child",
    }:
        promoted["payload"] = {
            "anchor_component_id": promote.get("anchor_component_id"),
            "content": str(promote.get("content") or ""),
            "label": str(promote.get("label") or ""),
            "node_type": str(promote.get("node_type") or "clause"),
            "parent_component_id": promote.get("parent_component_id"),
            "position": "after",
        }
        if promote.get("apply_to_parent_subrule_span"):
            promoted["payload"]["apply_to_parent_subrule_span"] = True
            promoted["payload"]["subrule_label"] = str(promote.get("subrule_label") or "")
        promoted["target"] = {
            **(promoted.get("target") or {}),
            "anchor_component_id": promote.get("anchor_component_id"),
            "anchor_occurrence": 1,
            "anchor_resolved_by_codex": True,
            "anchor_text": promote.get("anchor_component_id"),
            "component_id": promote.get("component_id") or (promoted.get("target") or {}).get("component_id"),
        }

    if operation == "INSERT_SIBLING" and strategy in {"insert_rule_from_instruction_text", "source_backed_insert_rule"}:
        full_text = str((promoted.get("payload") or {}).get("text") or (promoted.get("evidence") or {}).get("excerpt") or "")
        component_id = str(promote.get("component_id") or "")
        label = component_id.rsplit("/rule/", 1)[-1].upper() if "/rule/" in component_id else ""
        parsed_label, heading, content = _parse_rule_from_instruction(full_text, label)
        parsed_label = str(promote.get("label") or parsed_label).strip()
        heading = str(promote.get("heading") or heading).strip()
        content = str(promote.get("content") or content).strip()
        promoted["payload"] = {
            "content": content,
            "heading": heading,
            "label": parsed_label,
            "node_type": "rule",
        }
        promoted["target"] = {
            **(promoted.get("target") or {}),
            "anchor_component_id": promote.get("anchor_component_id"),
            "anchor_occurrence": 1,
            "anchor_resolved_by_codex" if strategy == "source_backed_insert_rule" else "anchor_resolved_by_review": True,
            "anchor_text": promote.get("anchor_component_id"),
            "component_id": component_id,
        }
    applicability_start = promote.get("applicability_start")
    if applicability_start:
        legal_time = dict(promoted.get("legal_time") or {})
        legal_time["applicability_start"] = applicability_start
        legal_time["commencement_date"] = applicability_start
        legal_time["date_basis"] = "approved_review_instruction_effective_date"
        promoted["legal_time"] = legal_time

    validation = dict(promoted.get("validation") or {})
    validation.update(
        {
            "anchor_resolved": True,
            "date_resolved": True,
            "materializable": True,
            "source_span_verified": bool(validation.get("source_span_verified", True)),
            "target_resolved": True,
        }
    )
    promoted["validation"] = validation
    promoted["status"] = "validated"
    promoted["review"] = {
        "required": False,
        "review_reasons": [],
        "reviewed_at": decision.get("reviewed_at"),
        "reviewed_by": decision.get("reviewed_by", "user"),
        "decision_notes": decision.get("notes"),
        "promoted_by": DECISION_PROMOTER_VERSION,
    }
    return promoted


def apply_review_decisions(
    *,
    events_path: Path,
    decisions_path: Path | list[Path],
    output: Path,
) -> dict[str, Any]:
    events = _read_jsonl(events_path)
    decision_paths = decisions_path if isinstance(decisions_path, list) else [decisions_path]
    decisions = _load_decision_paths(decision_paths)
    promoted_ids: list[str] = []
    output_rows: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "")
        if event_id in decisions:
            output_rows.append(_promote_event(event, decisions[event_id]))
            promoted_ids.append(event_id)
        else:
            output_rows.append(event)
    _write_jsonl(output, output_rows)
    return {
        "ok": True,
        "promoter_version": DECISION_PROMOTER_VERSION,
        "events_path": str(events_path),
        "decisions_path": [str(path) for path in decision_paths],
        "output": str(output),
        "decision_count": len(decisions),
        "promoted_count": len(promoted_ids),
        "promoted_event_ids": promoted_ids,
    }


__all__ = [
    "AUTO_REVIEW_VERSION",
    "DECISION_PROMOTER_VERSION",
    "DEPENDENCY_REVIEW_VERSION",
    "CODEX_REVIEW_VERSION",
    "apply_review_decisions",
    "generate_auto_review_decisions",
    "generate_codex_review_decisions",
    "generate_dependency_review_decisions",
]
