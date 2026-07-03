"""Text span helpers for component fragments embedded in parent rule text."""

from __future__ import annotations

import re


RULES_WORK = "/in/union/rules/cgst-rules-2017"


def parent_component_for_subrule(component_id: str) -> str | None:
    if "/subrule/" not in component_id:
        return None
    parent = component_id.split("/subrule/", 1)[0]
    if not parent.startswith(f"{RULES_WORK}/rule/"):
        return None
    return parent


def subrule_label_from_component(component_id: str) -> str | None:
    if "/subrule/" not in component_id:
        return None
    label = component_id.rsplit("/subrule/", 1)[-1].strip()
    if not re.fullmatch(r"[0-9a-z]+", label, flags=re.IGNORECASE):
        return None
    return label.upper()


def _looks_like_reference(before: str) -> bool:
    return bool(
        re.search(
            r"(?:sub-?\s*(?:rule|section)|section|clause|proviso|rule)\s*$",
            before,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_quoted_history(before: str) -> bool:
    return bool(
        re.search(
            r"\b(?:Inserted|Substituted|Omitted|Amended)\b.{0,160}\bfor\s*:\s*[\"'`“‘―-]?\s*$",
            before,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )


def _looks_like_component_start(before: str, after: str) -> bool:
    if _looks_like_reference(before):
        return False
    if _looks_like_quoted_history(before):
        return False
    stripped_before = before.rstrip()
    if stripped_before and not re.search(r"[\s.\]\):;,\-–—\"'“‘\[]$", stripped_before):
        return False
    return bool(
        re.match(
            r"\s*(?:[A-Z\[]|No\b|Where\b|The\b|Any\b|Every\b|Notwithstanding\b|For\b|Upon\b|In\b|A\b|An\b)",
            after,
            flags=re.IGNORECASE,
        )
    )


def top_level_subrule_marker_spans(text: str) -> list[tuple[str, int, int]]:
    markers: list[tuple[str, int, int]] = []
    for match in re.finditer(r"\((\d+[A-Za-z]?)\)", text):
        before = text[max(0, match.start() - 220) : match.start()]
        after = text[match.end() : match.end() + 120]
        if not _looks_like_component_start(before, after):
            continue
        markers.append((match.group(1).upper(), match.start(), match.end()))
    return markers


def find_top_level_subrule_span(text: str, label: str) -> tuple[int, int] | None:
    wanted = str(label or "").upper()
    if not wanted:
        return None
    markers = top_level_subrule_marker_spans(text)
    matches = [index for index, marker in enumerate(markers) if marker[0] == wanted]
    if len(matches) != 1:
        return None
    index = matches[0]
    start = markers[index][1]
    end = markers[index + 1][1] if index + 1 < len(markers) else len(text)
    span_text = text[start:end]
    history = re.search(
        r"\s+\d+\s*(?:Inserted|Substituted|Omitted|Amended)\b.{0,220}\bfor\s*:",
        span_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if history:
        end = start + history.start()
    if end <= start:
        return None
    return start, end


__all__ = [
    "find_top_level_subrule_span",
    "parent_component_for_subrule",
    "subrule_label_from_component",
    "top_level_subrule_marker_spans",
]
