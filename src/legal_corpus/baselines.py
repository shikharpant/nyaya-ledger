"""Build 2017 baseline component XML for event-sourced version history."""

from __future__ import annotations

import json
import base64
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .amendment_events import sha256_text
from .identity_registry import load_registry
from .omlx_client import OmlxConfig, OmlxError, chat_json
from .renderer import write_xml


RULES_WORK = "/in/union/rules/cgst-rules-2017"
ACT_WORK = "/in/union/acts/cgst-act-2017"

DEFAULT_RULES_REPAIR_SOURCES: tuple[tuple[Path, int, int], ...] = (
    (
        Path("data/Law/cbic_tax_portal/notifications/3-2017-central-tax-notifying-the-cgst-rules-2017-on-registration-and-composition_1000872.json"),
        1,
        26,
    ),
    (
        Path("data/Law/cbic_tax_portal/notifications/10-2017-central-tax-seeks-to-amend-cgst-rules-notification-no-3-2017-central-tax_1000865.json"),
        27,
        138,
    ),
    (
        Path("data/Law/cbic_tax_portal/notifications/15-2017-central-tax-amending-cgst-rules-notification-10-2017-ct-dt-28-06-2017_1000860.json"),
        139,
        162,
    ),
)


@dataclass(frozen=True)
class BaselineComponent:
    component_id: str
    label: str
    heading: str
    text: str
    component_type: str
    source_start: int | None = None
    source_end: int | None = None
    blocked: bool = False
    block_reasons: tuple[str, ...] = ()
    source_basis: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "label": self.label,
            "heading": self.heading,
            "text": self.text,
            "text_sha256": sha256_text(self.text),
            "component_type": self.component_type,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "blocked": self.blocked,
            "block_reasons": list(self.block_reasons),
            "source_basis": self.source_basis,
        }


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _extract_pdf_text(path: Path) -> str:
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            if text.strip():
                pages.append(text)
    return "\n\n".join(pages)


def _extract_pdf_bytes_text(raw: bytes) -> str:
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            if text.strip():
                pages.append(text)
    return "\n\n".join(pages)


def _rule_id(label: str) -> str:
    return f"{RULES_WORK}/rule/{label.lower()}"


def _subrule_id(rule_label: str, sub_label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", sub_label).lower()
    return f"{_rule_id(rule_label)}/subrule/{clean}"


_RE_ANNOTATION_MARKER = re.compile(
    r"(?:Inserted|Substituted|Omitted|Amended|Notified)\s+vide\s+(?:Notf|Notification)\b",
    re.IGNORECASE,
)
_RE_BRACKETED_FOOTNOTE = re.compile(r"\[[^\]]{1,2000}\]\d{1,3}")
_RE_VIDE_NOTF_TAIL = re.compile(
    r"\s*vide\s+Notf\s+no\.\s*\d+/\d{4}\s*[-\u2013]\s*CT\b.*$",
    re.IGNORECASE | re.DOTALL,
)
_RE_WEF = re.compile(r"\s*wef\s+\d{2}\.\d{2}\.\d{4}", re.IGNORECASE)
_RE_TILL_THEN = re.compile(
    r"Till then,?\s+the rule read as follows.*$",
    re.IGNORECASE | re.DOTALL,
)
_RE_STRAY_BRACKET_NUM = re.compile(r"\]\d{1,3}(?=[\s.,;)]|$)")
_RE_STRAY_PRE_ANNOTATION_NUM = re.compile(
    r"\d{1,3}(?=(?:Inserted|Substituted|Omitted|Amended)\s+vide)",
    re.IGNORECASE,
)
_RE_PAGE_HEADER = re.compile(r"Page\s+\d+\s+of\s+\d+", re.IGNORECASE)
_RE_TRAILING_CHAPTER_HEADING = re.compile(
    r"\s+Chapter\s*[-\u2013]\s*[IVXLCDM]+\s+[A-Z][A-Za-z ,&()/-]{2,120}$",
    re.IGNORECASE,
)


def decontaminate_baseline_text(text: str) -> str:
    """Strip editorial annotations from a consolidated-PDF baseline component.

    The CGST Rules PDF at ``data/Law/base_laws/`` is a 2021 consolidated edition.
    CBIC bakes amendment history into the text as:
      - Bracketed insertions with footnote numbers: ``[text]33``
      - Trailing annotation paragraphs: ``58 Inserted vide Notf no. 31/2019-CT...``
      - Stray footnote numbers after ``]``: ``]59``
      - "wef DD.MM.YYYY" markers
      - "Till then, the rule read as follows" historical context blocks
    This function strips all of these to recover the original legal text.
    """
    original = text

    # 1. Cut everything from the first annotation marker to end.
    #    Annotation markers are editorial paragraphs, never legal text.
    match = _RE_ANNOTATION_MARKER.search(text)
    if match:
        cut = match.start()
        # Backtrack over preceding whitespace, digits, and ]/\u2016 artefacts
        while cut > 0 and text[cut - 1] in " \t\r\n":
            cut -= 1
        while cut > 0 and text[cut - 1].isdigit():
            cut -= 1
        while cut > 0 and text[cut - 1] in " \t\r\n":
            cut -= 1
        if cut > 0 and text[cut - 1] in "]\u2016\u2016":
            cut -= 1
        text = text[:cut]

    # 2. Remove bracketed insertions followed by footnote numbers
    text = _RE_BRACKETED_FOOTNOTE.sub("", text)

    # 2b. Remove unclosed bracketed post-2017 sub-rule insertions.
    #     Pattern: [(2A) or [(3A) etc. — starts a new sub-rule that didn't
    #     exist in 2017.  The closing ] is typically on a later PDF page.
    unclosed = re.search(r"\s*\[\([0-9]+[A-Z]\)", text)
    if unclosed:
        text = text[: unclosed.start()]

    # 3. Remove standalone "vide Notf no. ..." tails
    text = _RE_VIDE_NOTF_TAIL.sub("", text)

    # 4. Remove "wef" annotations
    text = _RE_WEF.sub("", text)

    # 5. Remove "Till then, the rule read as follows" blocks
    text = _RE_TILL_THEN.sub("", text)

    # 6. Clean stray footnote numbers
    text = _RE_STRAY_BRACKET_NUM.sub("]", text)
    text = _RE_STRAY_PRE_ANNOTATION_NUM.sub("", text)

    # 7. Remove page headers
    text = _RE_PAGE_HEADER.sub("", text)

    # 7a. Remove next-chapter headings glued to the preceding rule body by PDF extraction.
    text = _RE_TRAILING_CHAPTER_HEADING.sub("", text)

    # 7b. Remove stray page/footnote numbers between list items: "; 19 (d)" → "; (d)"
    text = re.sub(r"(?<=[;.])\s*\d{1,3}\s+(?=\()", " ", text)

    # 8. Clean unicode artefacts
    text = text.replace("\u2016", "")  # broken bar
    text = text.replace("\u2014", "-")  # em-dash -> hyphen
    text = text.replace("\u2013", "-")  # en-dash -> hyphen

    # 9. Normalise whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


_POST_2017_RULE_PREFIXES: tuple[str, ...] = (
    "96a", "96b",
    "138a", "138b", "138c", "138d", "138e",
    "109b",
    "9b",
    "31a",
    "120a",
    "122a", "122b", "122c",
    "164a", "164b",
)


def _is_post_2017_rule_insertion(component_id: str) -> bool:
    """Return True if this component is a rule/sub-rule that did not exist in June 2017."""
    match = re.search(r"/rule/([^/]+)", component_id)
    if not match:
        return False
    rule_label = match.group(1).lower()
    return rule_label in _POST_2017_RULE_PREFIXES


def _is_post_2017_subrule_insertion(component_id: str, text: str) -> bool:
    """Detect sub-rules inserted after 2017 by checking for annotation evidence."""
    # If the text immediately starts with an annotation marker, it's a post-2017 insertion
    if _RE_ANNOTATION_MARKER.search(text[:200]):
        return True
    # If the text starts with ] or broken bar + annotation
    if re.match(r"^\s*[\]\u2016]?\s*\d{0,3}(?:Inserted|Substituted)", text[:200], re.IGNORECASE):
        return True
    return False


def decontaminate_baseline(
    components: list[BaselineComponent],
) -> tuple[list[BaselineComponent], list[dict[str, Any]]]:
    """Decontaminate text and drop post-2017 insertions from baseline components.

    Returns (cleaned_components, log) where log records every action taken.
    """
    cleaned: list[BaselineComponent] = []
    log: list[dict[str, Any]] = []

    for component in components:
        # Check if this is a post-2017 rule insertion
        is_post_2017_rule = _is_post_2017_rule_insertion(component.component_id)
        is_post_2017_subrule = (
            component.component_type == "subrule"
            and _is_post_2017_subrule_insertion(component.component_id, component.text)
        )

        if is_post_2017_rule or is_post_2017_subrule:
            log.append({
                "component_id": component.component_id,
                "action": "dropped_post_2017_insertion",
                "reason": "post_2017_rule" if is_post_2017_rule else "post_2017_subrule",
            })
            continue

        # Decontaminate the text
        original_text = component.text
        original_heading = component.heading
        decon_text = decontaminate_baseline_text(original_text)
        decon_heading = decontaminate_baseline_text(original_heading)

        if decon_text != original_text or decon_heading != original_heading:
            log.append({
                "component_id": component.component_id,
                "action": "decontaminated",
                "original_length": len(original_text),
                "cleaned_length": len(decon_text),
            })

        cleaned.append(
            BaselineComponent(
                component_id=component.component_id,
                label=component.label,
                heading=decon_heading,
                text=decon_text,
                component_type=component.component_type,
                source_start=component.source_start,
                source_end=component.source_end,
                blocked=component.blocked,
                block_reasons=component.block_reasons,
                source_basis=component.source_basis,
            )
        )

    return cleaned, log


def baseline_component_quality_flags(component: BaselineComponent) -> list[str]:
    """Return deterministic reasons a baseline component is unsafe as original text."""
    text = _clean(component.text)
    heading = _clean(component.heading)
    flags: list[str] = []
    if component.component_type == "rule" and re.match(r"^\d{4}\s+\S+", heading):
        flags.append("table_or_tariff_row_misparsed_as_rule")
    if re.search(
        r"(?:^|\b|\d+)(?:Inserted|Substituted|Omitted|Amended)\s+vide\s+(?:Notf|Notification)\b",
        text,
        flags=re.IGNORECASE,
    ):
        flags.append("post_2017_amendment_annotation_in_baseline")
    if re.search(r"(?:^|\b|\d+)vide\s+Notf\s+no\.\s*\d+/\d{4}\s*[-–]\s*CT\b", text, flags=re.IGNORECASE):
        flags.append("notification_footnote_in_baseline")
    if component.component_type == "subrule":
        marker = re.search(r"/subrule/([0-9a-z]+)$", component.component_id, flags=re.IGNORECASE)
        own_label = marker.group(1) if marker else ""
        embedded = re.findall(r"(?:^|[\[.;:]\s*)\(([0-9]+[A-Z]+)\)\s+", text, flags=re.IGNORECASE)
        if any(label.lower() != own_label.lower() for label in embedded):
            flags.append("embedded_later_subrule_marker_in_baseline")
    return sorted(set(flags))


def apply_baseline_quality_flags(components: list[BaselineComponent]) -> tuple[list[BaselineComponent], list[dict[str, Any]]]:
    flagged: list[dict[str, Any]] = []
    output: list[BaselineComponent] = []
    for component in components:
        flags = baseline_component_quality_flags(component)
        if not flags:
            output.append(component)
            continue
        flagged.append(
            {
                "component_id": component.component_id,
                "reasons": flags,
                "heading": component.heading,
                "text_excerpt": component.text[:500],
            }
        )
        output.append(
            BaselineComponent(
                component_id=component.component_id,
                label=component.label,
                heading=component.heading,
                text=component.text,
                component_type=component.component_type,
                source_start=component.source_start,
                source_end=component.source_end,
                blocked=True,
                block_reasons=tuple(flags),
                source_basis=component.source_basis,
            )
        )
    return output, flagged


def _is_rules_label_at_most(label: str, maximum: int) -> bool:
    match = re.fullmatch(r"0*(\d+)([A-Za-z]?)", str(label or "").strip())
    if not match or match.group(2):
        return False
    return int(match.group(1)) <= maximum


def repair_rules_baseline_from_source(
    deterministic: list[BaselineComponent],
    source_components: list[BaselineComponent],
    *,
    min_rule_label: int = 1,
    max_rule_label: int = 26,
) -> tuple[list[BaselineComponent], list[dict[str, Any]]]:
    """Replace contaminated deterministic components with clean source-covered components.

    The principal 3/2017 notification is partial. It can repair rules 1-26,
    but must not be treated as a full baseline source. Existing quality flags
    remain the safety gate for both candidate and replacement text.
    """

    source_by_id = {component.component_id: component for component in source_components}
    repaired: list[BaselineComponent] = []
    repair_log: list[dict[str, Any]] = []
    for component in deterministic:
        rule_match = re.search(r"/rule/([^/]+)(?:/|$)", component.component_id)
        if not rule_match or not _is_rules_label_between(rule_match.group(1), min_rule_label, max_rule_label):
            repaired.append(component)
            continue
        replacement = source_by_id.get(component.component_id)
        if not replacement:
            repaired.append(component)
            continue
        original_flags = baseline_component_quality_flags(component)
        replacement_flags = baseline_component_quality_flags(replacement)
        if replacement_flags:
            repaired.append(component)
            repair_log.append(
                {
                    "component_id": component.component_id,
                    "status": "replacement_rejected",
                    "original_reasons": original_flags,
                    "replacement_reasons": replacement_flags,
                    "replacement_source": replacement.source_basis,
                }
            )
            continue
        if not original_flags and sha256_text(_clean(component.text)) == sha256_text(_clean(replacement.text)):
            repaired.append(component)
            continue
        repaired.append(replacement)
        repair_log.append(
            {
                "component_id": component.component_id,
                "status": "repaired",
                "original_reasons": original_flags,
                "replacement_source": replacement.source_basis,
                "replacement_text_sha256": sha256_text(replacement.text),
            }
        )
    return repaired, repair_log


def _is_rules_label_between(label: str, minimum: int, maximum: int) -> bool:
    match = re.fullmatch(r"0*(\d+)([A-Za-z]?)", str(label or "").strip())
    if not match or match.group(2):
        return False
    value = int(match.group(1))
    return minimum <= value <= maximum


def parse_rules_text_deterministic(
    text: str,
    *,
    source_basis: str = "",
) -> tuple[str, list[BaselineComponent]]:
    """Parse rule-level and simple sub-rule components from Rules text."""

    # Start at the first substantive rule to avoid cover-page amendment history.
    first_rule = re.search(r"(?m)^\s*1\.\s+Short\s+title", text, flags=re.IGNORECASE)
    search_text = text[first_rule.start() :] if first_rule else text
    matches = list(
        re.finditer(
            r"(?m)^\s*(\d+[A-Z]?)(?:\.\s*|\[\s*)([^\n]+)",
            search_text,
        )
    )
    components: list[BaselineComponent] = []
    seen_rules: set[str] = set()
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(search_text)
        block = search_text[start:end].strip()
        label = match.group(1)
        if label.lower() in seen_rules:
            continue
        seen_rules.add(label.lower())
        first_line = _clean(match.group(2))
        heading = first_line.split(".--", 1)[0].split(".-", 1)[0].split("--", 1)[0].strip(" .-")
        components.append(
            BaselineComponent(
                component_id=_rule_id(label),
                label=label,
                heading=heading,
                text=block,
                component_type="rule",
                source_start=(first_rule.start() if first_rule else 0) + start,
                source_end=(first_rule.start() if first_rule else 0) + end,
                source_basis=source_basis,
            )
        )
        sub_matches = list(re.finditer(r"(?:^|[.\n-])\s*(\((\d+[A-Z]?)\)\s*)", block))
        seen_subrules: set[str] = set()
        for sub_index, sub_match in enumerate(sub_matches):
            sub_label = sub_match.group(2)
            if sub_label.lower() in seen_subrules:
                continue
            seen_subrules.add(sub_label.lower())
            sub_start = sub_match.start(1)
            sub_end = sub_matches[sub_index + 1].start() if sub_index + 1 < len(sub_matches) else len(block)
            sub_text = _clean(block[sub_start:sub_end])
            if len(sub_text) < 20:
                continue
            components.append(
                BaselineComponent(
                    component_id=_subrule_id(label, sub_label),
                    label=sub_label,
                    heading="",
                    text=sub_text,
                    component_type="subrule",
                    source_start=(first_rule.start() if first_rule else 0) + start + sub_start,
                    source_end=(first_rule.start() if first_rule else 0) + start + sub_end,
                    source_basis=source_basis,
                )
            )
    return text, components


def parse_rules_pdf_deterministic(pdf_path: Path) -> tuple[str, list[BaselineComponent]]:
    """Parse rule-level and simple sub-rule components from the 2017 Rules PDF text."""

    text = _extract_pdf_text(pdf_path)
    return parse_rules_text_deterministic(text, source_basis=str(pdf_path))


def parse_rules_notification_json(notification_json: Path) -> tuple[str, list[BaselineComponent]]:
    """Parse a CBIC notification JSON PDF payload as a Rules source track."""

    data = json.loads(notification_json.read_text(encoding="utf-8"))
    text = str(data.get("contentText") or "").strip()
    if not text and data.get("contentPdfBase64"):
        text = _extract_pdf_bytes_text(base64.b64decode(str(data.get("contentPdfBase64") or "")))
    return parse_rules_text_deterministic(text, source_basis=str(notification_json))


def parse_rules_with_omlx(text: str, *, config: OmlxConfig, max_chars: int = 12000) -> list[BaselineComponent]:
    """Ask OMLX for a structural sample of the Rules baseline.

    The full PDF is too large for a single request, so v1 uses this as a
    reconciliation track over the initial extract window. Deterministic parsing
    remains the source of baseline XML unless this track agrees component-wise.
    """

    prompt = (
        "Extract CGST Rules components from this PDF text sample. Return JSON with key "
        "rules, an array of objects with label, heading, text. Do not include forms or tables. "
        "Text sample:\n"
        + text[:max_chars]
    )
    response = chat_json(prompt, config=config, max_tokens=4096)
    components: list[BaselineComponent] = []
    for item in response.get("rules", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        components.append(
            BaselineComponent(
                component_id=_rule_id(label),
                label=label,
                heading=_clean(str(item.get("heading") or "")),
                text=str(item.get("text") or "").strip(),
                component_type="rule",
            )
        )
    return components


def reconcile_baseline_tracks(
    deterministic: list[BaselineComponent],
    llm_components: list[BaselineComponent],
    *,
    llm_error: str | None = None,
    llm_attempted: bool = False,
    source_repairs: list[dict[str, Any]] | None = None,
) -> tuple[list[BaselineComponent], dict[str, Any]]:
    det_by_id = {component.component_id: component for component in deterministic}
    llm_by_id = {component.component_id: component for component in llm_components}
    blocked: list[dict[str, Any]] = []
    agreed = []
    for component_id, component in det_by_id.items():
        other = llm_by_id.get(component_id)
        if not other:
            agreed.append(component)
            continue
        heading_ok = not other.heading or _clean(other.heading).lower() == _clean(component.heading).lower()
        text_ok = not other.text or sha256_text(_clean(other.text)) == sha256_text(_clean(component.text))
        if heading_ok or text_ok:
            agreed.append(component)
        else:
            blocked.append(
                {
                    "component_id": component_id,
                    "reason": "baseline_track_disagreement",
                    "deterministic_heading": component.heading,
                    "llm_heading": other.heading,
                }
            )
            agreed.append(
                BaselineComponent(
                    component_id=component.component_id,
                    label=component.label,
                    heading=component.heading,
                    text=component.text,
                    component_type=component.component_type,
                    source_start=component.source_start,
                    source_end=component.source_end,
                    blocked=True,
                )
            )
    warnings = []
    strategy = "deterministic_only"
    if llm_components:
        strategy = "deterministic_plus_omlx_reconciliation"
        if len(llm_components) < len(deterministic):
            warnings.append("omlx_reconciliation_partial_coverage")
    elif llm_attempted:
        strategy = "deterministic_with_omlx_reconciliation_empty"
        warnings.append("omlx_returned_no_components")
    if llm_error:
        warnings.append(llm_error)
        strategy = "deterministic_with_omlx_reconciliation_unavailable"
    agreed, quality_blocked = apply_baseline_quality_flags(agreed)
    blocked.extend(
        {
            "component_id": row["component_id"],
            "reason": "baseline_quality_flag",
            "reasons": row["reasons"],
            "heading": row["heading"],
            "text_excerpt": row["text_excerpt"],
        }
        for row in quality_blocked
    )
    if quality_blocked:
        warnings.append("baseline_quality_flags_present")
    if source_repairs:
        warnings.append("source_priority_repairs_applied")
    return agreed, {
        "strategy": strategy,
        "component_count": len(deterministic),
        "llm_attempted": llm_attempted,
        "llm_component_count": len(llm_components),
        "llm_coverage": "partial" if llm_components and len(llm_components) < len(deterministic) else ("complete" if llm_components else "none"),
        "blocked_count": len(blocked),
        "blocked_components": blocked,
        "quality_blocked_count": len(quality_blocked),
        "source_repair_count": len([row for row in source_repairs or [] if row.get("status") == "repaired"]),
        "source_repairs": source_repairs or [],
        "warnings": warnings,
    }


def parse_act_json(json_path: Path) -> list[BaselineComponent]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    components = []
    for section in data.get("sections", []):
        label = str(section.get("section_number") or "").strip()
        if not label:
            continue
        text = str(section.get("full_text") or "").strip()
        description = _clean(str(section.get("description") or ""))
        components.append(
            BaselineComponent(
                component_id=f"{ACT_WORK}/section/{label.lower()}",
                label=label,
                heading=description,
                text=text,
                component_type="section",
            )
        )
    return components


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_text(element: ET.Element) -> str:
    return _clean(" ".join(part for part in element.itertext() if part and part.strip()))


def _section_marker_score(label: str, text: str) -> int:
    score = 0
    if re.search(rf"\bSection\s+{re.escape(label)}\b", text, flags=re.IGNORECASE):
        score += 100
    if re.search(rf"^\*?\s*Section\s+{re.escape(label)}\b", text, flags=re.IGNORECASE):
        score += 100
    if re.search(r"\b(inserted|substituted|omitted|vide notification|finance act)\b", text, flags=re.IGNORECASE):
        score -= 25
    if len(text) > 250:
        score += 25
    return score


def _heading_from_section_text(label: str, text: str) -> str:
    match = re.search(
        rf"\bSection\s+{re.escape(label)}\.\s*([^-.\n]+(?:\s+[A-Za-z][^-.\n]+)?)\s*(?:[.-]|-\s)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean(match.group(1).strip(" .-"))
    return ""


def _is_original_act_section(label: str) -> bool:
    return label.isdigit() and 1 <= int(label) <= 174


def parse_act_corpus_xml(xml_path: Path, *, original_2017_only: bool = True) -> list[BaselineComponent]:
    """Parse the canonical Act corpus XML into section baseline components.

    The India Code JSON currently carries shifted section_number metadata after
    later insertions/omissions. The corpus XML has stable section refersTo IDs,
    but also contains footnote-like duplicate section nodes. Prefer substantive
    section nodes by matching the textual "* Section N." marker and longer text.
    """

    tree = ET.parse(xml_path)
    candidates: dict[str, list[BaselineComponent]] = {}
    for element in tree.iter():
        if _local_name(element.tag) != "section":
            continue
        refers_to = str(element.attrib.get("refersTo") or "")
        match = re.search(r"/section/([^/]+)$", refers_to)
        if not match:
            continue
        label = match.group(1)
        if original_2017_only and not _is_original_act_section(label.lower()):
            continue
        text = _element_text(element)
        if not text:
            continue
        heading = ""
        heading_element = next((child for child in element.iter() if _local_name(child.tag) == "heading"), None)
        if heading_element is not None:
            heading = _element_text(heading_element)
        if not heading:
            heading = _heading_from_section_text(label, text)
        candidates.setdefault(label.lower(), []).append(
            BaselineComponent(
                component_id=f"{ACT_WORK}/section/{label.lower()}",
                label=label,
                heading=heading,
                text=text,
                component_type="section",
                source_start=int(element.attrib["sourceStart"]) if str(element.attrib.get("sourceStart") or "").isdigit() else None,
                source_end=int(element.attrib["sourceEnd"]) if str(element.attrib.get("sourceEnd") or "").isdigit() else None,
            )
        )

    components = []
    for label in sorted(candidates, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)):
        component = max(
            candidates[label],
            key=lambda item: (_section_marker_score(item.label, item.text), len(item.text)),
        )
        components.append(component)
    return components


def _add_metadata(root: ET.Element, *, work_id: str, title: str, document_type: str, base_as_of: str) -> None:
    meta = ET.SubElement(root, "meta")
    proprietary = ET.SubElement(meta, "proprietary", {"source": "#git-for-law"})
    for key, value in {
        "canonical_id": work_id,
        "title": title,
        "document_type": document_type,
        "effective_from": base_as_of,
        "parser_version": "baseline-builder-v1",
    }.items():
        ET.SubElement(proprietary, "property", {"name": key, "value": value})


def _render_components_xml(
    components: list[BaselineComponent],
    *,
    work_id: str,
    title: str,
    document_type: str,
    base_as_of: str,
) -> ET.ElementTree:
    root = ET.Element("akomaNtoso")
    doc = ET.SubElement(root, "doc", {"refersTo": work_id})
    _add_metadata(doc, work_id=work_id, title=title, document_type=document_type, base_as_of=base_as_of)
    body = ET.SubElement(doc, "body")
    for component in components:
        attrs = {
            "eId": re.sub(r"[^0-9A-Za-z_]+", "_", component.component_id.strip("/")),
            "refersTo": component.component_id,
        }
        if component.source_start is not None and component.source_end is not None:
            attrs["sourceStart"] = str(component.source_start)
            attrs["sourceEnd"] = str(component.source_end)
        if component.source_basis:
            attrs["sourceBasis"] = component.source_basis
        if component.blocked:
            attrs["data-baseline-blocked"] = "true"
        if component.block_reasons:
            attrs["data-baseline-block-reasons"] = ",".join(component.block_reasons)
        element = ET.SubElement(body, component.component_type, attrs)
        ET.SubElement(element, "num").text = component.label
        if component.heading:
            ET.SubElement(element, "heading").text = component.heading
        content = ET.SubElement(element, "content")
        ET.SubElement(content, "p").text = component.text
    return ET.ElementTree(root)


def _write_baseline(
    output_dir: Path,
    components: list[BaselineComponent],
    *,
    work_id: str,
    title: str,
    document_type: str,
    base_as_of: str,
    reconciliation: dict[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_xml(
        _render_components_xml(
            components,
            work_id=work_id,
            title=title,
            document_type=document_type,
            base_as_of=base_as_of,
        ),
        output_dir / "baseline.xml",
    )
    (output_dir / "baseline_components.jsonl").write_text(
        "\n".join(json.dumps(component.to_json(), ensure_ascii=False, sort_keys=True) for component in components) + "\n",
        encoding="utf-8",
    )
    (output_dir / "baseline_reconciliation.json").write_text(
        json.dumps(reconciliation, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "ok": reconciliation.get("blocked_count", 0) == 0,
        "work_id": work_id,
        "output_dir": str(output_dir),
        "component_count": len(components),
        "blocked_count": reconciliation.get("blocked_count", 0),
    }


def build_baseline(
    *,
    target_work: str,
    registry_path: Path,
    output_dir: Path | None = None,
    rules_pdf: Path = Path("data/Law/base_laws/cgst-rules-2017-part-a-rules.pdf"),
    rules_repair_notification: Path | None = Path(
        "data/Law/cbic_tax_portal/notifications/3-2017-central-tax-notifying-the-cgst-rules-2017-on-registration-and-composition_1000872.json"
    ),
    rules_repair_sources: tuple[tuple[Path, int, int], ...] | None = DEFAULT_RULES_REPAIR_SOURCES,
    act_json: Path = Path("data/Law/base_laws/central_goods_and_services_tax_act_2017.json"),
    act_corpus_xml: Path = Path("corpus/in/union/acts/cgst-act-2017/act.xml"),
    use_llm: bool = False,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key_env: str = "OMLX_API_KEY",
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    resolved = registry.resolve_corpus_id(target_work) or target_work
    work = registry.work(resolved) or {}
    base_as_of = str(work.get("base_as_of") or "1970-01-01")
    baseline_dir = output_dir or Path(str(work.get("baseline_path") or f"derived/version_history/baselines/{Path(resolved).name}/{base_as_of}"))
    if resolved == RULES_WORK:
        text, components = parse_rules_pdf_deterministic(rules_pdf)
        repair_log: list[dict[str, Any]] = []
        repair_sources = rules_repair_sources
        if repair_sources is None and rules_repair_notification:
            repair_sources = ((rules_repair_notification, 1, 26),)
        for repair_path, min_rule, max_rule in repair_sources or ():
            if not repair_path.exists():
                repair_log.append(
                    {
                        "source": str(repair_path),
                        "status": "source_missing",
                        "min_rule_label": min_rule,
                        "max_rule_label": max_rule,
                    }
                )
                continue
            _, repair_components = parse_rules_notification_json(repair_path)
            components, current_log = repair_rules_baseline_from_source(
                components,
                repair_components,
                min_rule_label=min_rule,
                max_rule_label=max_rule,
            )
            for row in current_log:
                row.setdefault("source", str(repair_path))
                row.setdefault("min_rule_label", min_rule)
                row.setdefault("max_rule_label", max_rule)
            repair_log.extend(current_log)
        # Decontaminate: strip editorial annotations and drop post-2017 insertions
        components, decon_log = decontaminate_baseline(components)
        llm_components: list[BaselineComponent] = []
        llm_error = None
        if use_llm:
            try:
                llm_components = parse_rules_with_omlx(
                    text,
                    config=OmlxConfig.from_env(
                        base_url=llm_base_url,
                        model=llm_model,
                        api_key_env=llm_api_key_env,
                    ),
                )
            except OmlxError as exc:
                llm_error = getattr(exc, "reason", "llm_unavailable")
        components, reconciliation = reconcile_baseline_tracks(
            components,
            llm_components,
            llm_error=llm_error,
            llm_attempted=use_llm,
            source_repairs=repair_log,
        )
        if decon_log:
            reconciliation["decontamination_log"] = decon_log
            reconciliation["decontamination_dropped"] = sum(1 for e in decon_log if e["action"] == "dropped_post_2017_insertion")
            reconciliation["decontamination_cleaned"] = sum(1 for e in decon_log if e["action"] == "decontaminated")
        reconciliation["work_id"] = resolved
        return _write_baseline(
            baseline_dir,
            components,
            work_id=resolved,
            title=str(work.get("title") or "CGST Rules, 2017"),
            document_type="rules",
            base_as_of=base_as_of,
            reconciliation=reconciliation,
        )
    if resolved == ACT_WORK:
        source_path = act_corpus_xml if act_corpus_xml.exists() else act_json
        if source_path == act_corpus_xml:
            components = parse_act_corpus_xml(act_corpus_xml)
            strategy = "canonical_corpus_xml_section_ids"
        else:
            components = parse_act_json(act_json)
            strategy = "source_json_section_count"
        expected = 174
        blocked = [] if len(components) == expected else [resolved]
        reconciliation = {
            "work_id": resolved,
            "strategy": strategy,
            "source_path": str(source_path),
            "component_count": len(components),
            "expected_section_count": expected,
            "blocked_count": len(blocked),
            "blocked_components": blocked,
        }
        return _write_baseline(
            baseline_dir,
            components,
            work_id=resolved,
            title=str(work.get("title") or "CGST Act, 2017"),
            document_type="act",
            base_as_of=base_as_of,
            reconciliation=reconciliation,
        )
    raise ValueError(f"Unsupported baseline target work: {target_work}")


__all__ = [
    "ACT_WORK",
    "RULES_WORK",
    "BaselineComponent",
    "apply_baseline_quality_flags",
    "baseline_component_quality_flags",
    "build_baseline",
    "decontaminate_baseline",
    "decontaminate_baseline_text",
    "parse_act_corpus_xml",
    "parse_act_json",
    "parse_rules_with_omlx",
    "parse_rules_pdf_deterministic",
    "parse_rules_notification_json",
    "parse_rules_text_deterministic",
    "repair_rules_baseline_from_source",
    "reconcile_baseline_tracks",
]
