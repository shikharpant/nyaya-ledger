"""Reconcile reconstructed component history against consolidated checkpoints."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .amendment_events import sha256_text
from .version_compare import normalize_version_component_id, read_node_versions, resolve_version_dir


DEFAULT_EVENTS_PATH = Path("derived/version_history/amendment_events_reviewed.jsonl")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _element_text(element: ET.Element) -> str:
    lines = []
    for child in element.iter():
        if _local_name(child.tag) not in {"num", "heading", "p"}:
            continue
        text = _clean("".join(child.itertext()))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def checkpoint_component_candidates(path: Path) -> dict[str, list[str]]:
    components: dict[str, list[str]] = {}
    structural_tags = {"article", "rule", "subrule", "section", "clause", "paragraph"}
    paths = [path] if path.is_file() else sorted(path.rglob("*.xml"))
    for xml_path in paths:
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        for element in root.iter():
            if _local_name(element.tag) not in structural_tags:
                continue
            component_id = element.attrib.get("refersTo")
            if component_id:
                text = _element_text(element)
                if text:
                    components.setdefault(normalize_version_component_id(component_id), []).append(text)
    return components


def _component_label(component_id: str) -> str:
    for marker in ("/rule/", "/section/", "/subrule/"):
        if marker in component_id:
            return component_id.rsplit(marker, 1)[-1].split("/", 1)[0]
    return ""


def _component_id_for_manifest_label(target_work: str, label: str) -> str:
    clean_label = re.sub(r"^(?:rule|section)\s+", "", str(label or "").strip(), flags=re.IGNORECASE).strip()
    if "/acts/" in target_work:
        component_type = "/section/"
    elif "/rules/" in target_work:
        component_type = "/rule/"
    else:
        component_type = "/rule/"
    return f"{target_work.rstrip('/')}{component_type}{clean_label.lower()}"


def _component_label_pattern(label: str) -> str:
    parts = [re.escape(char) for char in label if char.isalnum()]
    if not parts:
        return ""
    return r"\s*".join(parts)


def _line_is_component_label(line: str, label: str) -> bool:
    pattern = _component_label_pattern(label)
    if not pattern:
        return False
    return bool(re.fullmatch(rf"\[?{pattern}\]?\.?", _clean(line), flags=re.IGNORECASE))


def _heading_pattern(heading: str) -> str:
    return re.escape(_clean(heading)).replace(r"\ ", r"\s+")


def _strip_content_intro(line: str, label: str, heading: str) -> str | None:
    label_pattern = _component_label_pattern(label)
    heading_pattern = _heading_pattern(heading)
    if not label_pattern or not heading_pattern:
        return None
    value = _clean(line)
    value = re.sub(
        rf"^(?:Rule|Section)\s+{label_pattern}\s+(?=(?:\d+\s*\[|\[|(?:Rule|Section)\s+{label_pattern}|{label_pattern}\s*\.))",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(
        rf"^{label_pattern}\s+\*?\s*(?=(?:Rule|Section)\s+{label_pattern}\b)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(r"^\d+\s*\[\s*", "", value).strip()
    value = re.sub(r"^\[\s*", "", value).strip()
    match = re.match(
        rf"^(?:(?:Rule|Section)\s+)?{label_pattern}\s*\.?\s*{heading_pattern}"
        rf"\s*(?:\.?\s*[-–—]\s*|[-–—]\s*|\.\s*)?",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return value[match.end() :].strip()


def _split_content_intro(line: str, label: str) -> tuple[str, str] | None:
    label_pattern = _component_label_pattern(label)
    if not label_pattern:
        return None
    value = _clean(line)
    value = re.sub(
        rf"^{label_pattern}\s+\*?\s*(?=(?:Rule|Section)\s+{label_pattern}\b)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(r"^\d+\s*\[\s*", "", value).strip()
    value = re.sub(r"^\[\s*", "", value).strip()
    match = re.match(
        rf"^(?:(?:Rule|Section)\s+)?{label_pattern}\s*\.?\s*(?P<heading>.+?)"
        rf"(?:\.?\s*[-–—]\s*|[-–—]\s*|\.\s+)(?P<body>.+)$",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    heading = _clean(match.group("heading"))
    body = _clean(match.group("body"))
    return (heading, body) if heading and body else None


def _reconciliation_text(value: str, component_id: str = "") -> str:
    """Normalize parser-shape differences without erasing substantive text."""
    label = _component_label(component_id)
    lines = [_clean(line) for line in str(value or "").splitlines() if _clean(line)]
    if label and len(lines) == 2 and _line_is_component_label(lines[0], label):
        split_intro = _split_content_intro(lines[1], label)
        if split_intro:
            lines = [lines[0], split_intro[0], split_intro[1]]
    if label and len(lines) >= 3 and _line_is_component_label(lines[0], label):
        intro_stripped = _strip_content_intro(lines[2], label, lines[1])
        if intro_stripped is not None:
            lines = lines[:2] + ([intro_stripped] if intro_stripped else []) + lines[3:]
        if len(lines) < 3:
            return _clean(" ".join(lines) if lines else value)
        heading = re.escape(lines[1])
        label_pattern = _component_label_pattern(label)
        if re.match(rf"^\[?{label_pattern}\]?\.\s*{heading}\b", lines[2], flags=re.I) or re.match(
            rf"^(?:Section|Rule)\s+{label_pattern}\.?\s*{heading}\b",
            lines[2],
            flags=re.I,
        ) or re.match(
            rf"^\[?{label_pattern}\]?\s+\*?\s*(?:Section|Rule)\s+{label_pattern}\.\s*{heading}\b",
            lines[2],
            flags=re.I,
        ):
            lines.pop(1)
    text = _clean(" ".join(lines) if lines else value)
    text = text.replace("–", "-").replace("—", "-").replace("―", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_ANNOTATION_WORDS = (
    "Inserted",
    "Substituted",
    "Omitted",
    "Amended",
    "Inserted",
    "Renumbered",
)


def _strip_annotation_noise(value: str) -> str:
    text = value
    text = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(section|sections|sub-section|sub-sections)\s+(\d{1,2})(\d)\[\s*\*+\s*\]",
        r"\1 \2 ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<![A-Za-z0-9-])\d+\[\s*\*+\s*\]", " ", text)
    text = re.sub(r"(?<=[A-Za-z])\d+\[", "[", text)
    text = re.sub(r"\[\s*(?:\*+|Omitted)\s*\]", " ", text, flags=re.IGNORECASE)
    annotation = "|".join(_ANNOTATION_WORDS)
    text = re.sub(
        rf"\d+\s*\.?\s*(?:{annotation})(?:\s*\([^)]*\)\s*\)?)?\s*vide\s*(?:Notification|Notf)\b.*?"
        rf"(?=(?:\s+\d+\s*\.?\s*(?:{annotation})(?:\s*\([^)]*\)\s*\)?)?\s*vide\s*(?:Notification|Notf)\b)|$)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\d+\s*\.?\s*(?:{annotation})\b.*?\bvide\s*(?:Notification|Notf)\b.*?"
        rf"(?=(?:\s+\d+\s*\.?\s*(?:{annotation})\b.*?\bvide\s*(?:Notification|Notf)\b)|$)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b((?:FORM\s+)?GSTR-\d[A-Z]?)(\d+)\[", r"\1[", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<![A-Za-z0-9-])\d+\[", "[", text)
    text = re.sub(r"\](\d+)\b", "]", text)
    text = text.replace("[", " ").replace("]", " ")
    return text


def _substantive_reconciliation_text(value: str, component_id: str = "") -> str:
    text = _strip_annotation_noise(_reconciliation_text(value, component_id))
    label = _component_label(component_id)
    if label:
        label_pattern = _component_label_pattern(label)
        if label_pattern:
            # Consolidated checkpoints frequently inline a display heading as
            # "Rule N. Heading.- body" after already emitting <num>/<heading>.
            # Some reconstructed baselines store only the operative body.  For
            # substantive comparison, strip that duplicated intro while leaving
            # the strict normalized hash unchanged for audit visibility.
            text = re.sub(
                rf"^\s*{label_pattern}\.?\s+.{{0,260}}?(?:Rule|Section)\s+{label_pattern}\.?\s+"
                rf".{{0,260}}?(?:\.\s*[-–—]\s*|\s+[-–—]\s+)",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                rf"^\s*(?:{label_pattern}\.?\s+)?(?:(?:Rule|Section)\s+)?{label_pattern}\.?\s+"
                rf".{{0,260}}?(?:\.\s*[-–—]\s*|\s+[-–—]\s+)",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                rf"^\s*{label_pattern}\.?\s+(?:Rule|Section)\s+{label_pattern}\.?\s+",
                f"{label} ",
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                rf"^\s*{label_pattern}\.?\s+{label_pattern}\.?\s+",
                f"{label} ",
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                rf"^\s*{label_pattern}\.?\s+[A-Za-z][^.;:]{{2,180}}\s+(?=\(\d+[A-Za-z]?\)\s)",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
    text = text.lower()
    text = text.replace("–", "-").replace("—", "-").replace("―", "-")
    text = re.sub(r"\brule\s+(\d+)\s+([a-z])\b", r"\1\2", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(in|of|under|through|by|and|or)(form|section|sub-section|rule)\b", r"\1 \2", text)
    text = re.sub(r"\b(form\s+gst(?:r)?-[a-z0-9]+)([a-z])\b", r"\1 \2", text)
    text = re.sub(r"\b(vouchers)(and)\b", r"\1 \2", text)
    text = re.sub(r"\b(provided)\s+(provided\s+that)\b", r"\2", text)
    text = re.sub(r"\b(provided\s+further)\s+(provided\s+further\s+that)\b", r"\2", text)
    text = re.sub(r"\bchapter\s+[ivxlcdm]+\s+[a-z][a-z\s-]{2,80}\s*$", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bthereafterthecorrect\b", "thereafter the correct", text)
    text = re.sub(r"\bscoredout\b", "scored out", text)
    text = re.sub(r"[^0-9a-z]+", "", text)
    return text.strip()


def _reconciliation_hash(value: str, component_id: str = "") -> str:
    return sha256_text(_normalize_for_comparison(value, component_id))


def _substantive_reconciliation_hash(value: str, component_id: str = "") -> str:
    return sha256_text(_substantive_reconciliation_text(value, component_id))


def _substantive_texts_equivalent(value_a: str, value_b: str, component_id: str = "") -> bool:
    text_a = _substantive_reconciliation_text(value_a, component_id)
    text_b = _substantive_reconciliation_text(value_b, component_id)
    if text_a == text_b:
        return True
    label = _component_label(component_id).lower()
    if not label:
        return False
    compact_label = re.sub(r"[^0-9a-z]+", "", label)
    if text_a.endswith(text_b):
        prefix = text_a[: -len(text_b)]
        return compact_label in prefix and len(prefix) <= 80
    if text_b.endswith(text_a):
        prefix = text_b[: -len(text_a)]
        return compact_label in prefix and len(prefix) <= 80
    return False


# Similarity at or above which a near-match is treated as format-only even when
# the substantive-normalized hashes disagree. 0.95 captures components whose
# remaining diff is annotation noise, footnote bleed, or stale parent baseline
# text that substantive normalization does not fully canonicalize.
FORMAT_ONLY_SIMILARITY_THRESHOLD = 0.95

# Components in the [MINOR_SUBSTANTIVE_SIMILARITY_LOWER, FORMAT_ONLY_SIMILARITY_THRESHOLD)
# similarity band are re-checked with an aggressive deep normalization that
# strips duplicated display headings, footnote markers, and annotation noise.
# If the deep-normalized similarity is at or above
# MINOR_SUBSTANTIVE_NORMALIZED_SIMILARITY and the normalized lengths are close
# (so the difference is annotation/formatting, not missing or changed legal
# text), the component is classified as ``minor_substantive_difference``
# (Tier B) rather than ``true_substantive_mismatch`` (Tier C).
MINOR_SUBSTANTIVE_SIMILARITY_LOWER = 0.80
MINOR_SUBSTANTIVE_NORMALIZED_SIMILARITY = 0.95
MINOR_SUBSTANTIVE_LENGTH_RATIO_MIN = 0.75
MINOR_SUBSTANTIVE_LENGTH_RATIO_MAX = 1.30


def _normalize_for_comparison(value: str, component_id: str = "") -> str:
    """Aggressive normalization used only for similarity scoring.

    Strips editorial annotation clauses (``Substituted vide Notification ...``),
    footnote markers (``[1]``), trailing PDF page-number artifacts, and
    normalizes Unicode quotes/dashes so that format-only differences do not
    suppress the similarity ratio used to separate ``format_only_match`` from
    ``true_substantive_mismatch``.

    NOT used for hash computation — hashing continues to use
    ``_reconciliation_text`` / ``_substantive_reconciliation_text`` so the
    audit trail in the report reflects the stored text.
    """
    text = _reconciliation_text(value, component_id)
    text = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", " ", text, flags=re.IGNORECASE)
    while re.search(r"\d+\[([^\[\]]*)\]", text):
        text = re.sub(r"\d+\[([^\[\]]*)\]", r"\1", text)
    text = re.sub(r"\d+\[", " ", text)
    text = re.sub(r"\b(Rule\s+\d+[A-Za-z]?)\s+\1\b", r"\1", text, count=1, flags=re.IGNORECASE)
    annotation = "|".join(_ANNOTATION_WORDS)
    # Strip editorial annotation clauses. The CBIC checkpoint frequently runs
    # footnote numbers into the annotation word (e.g. "114Inserted vide ...",
    # "324Substituted vide ..."), so we permit a leading digit run and do not
    # require a word boundary before the annotation verb.
    text = re.sub(
        rf"\d*\s*\.?\s*(?:{annotation})(?:\s*\([^)]*\)\s*\)?)?\s*vide\s*(?:Notification|Notf|Not\.?|N\.?)"
        rf"\s*No\.?\s*\d+/[-\dA-Za-z]+(?:\s+(?:dt\.?|dated)\s*\d[\d.\-]+\s*)?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\d*\s*\.?\s*(?:{annotation})(?:\s*\([^)]*\)\s*\)?)?\s*vide\s*(?:Notification|Notf|Not\.?|N\.?)\b[^.]*?\.\s*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\*?\s*Enforced\s+w\.?e\.?f\.?\s*[^.]*\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(
        rf"\s+\d+\s*\.\s*(?:{annotation}|Provided|Brought|Notified|Inserted|Substituted|Omitted)[\s\S]*\Z",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s+\d+\s*\.\s*(?:Inserted|Substituted|Omitted|Provided)\s*\([^)]*\)\s*(?:vide\s*)?(?:Notification|Notf|Not\.?)\s*[^.]*?by\s+s\.?\s*\d+\s*of\s*[^.]*\.",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+\]\s*$", "", text)
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201e", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u00a0", " ")
    )
    # Strip PDF page-number bleed-through (e.g. trailing ". 24" at end of text).
    text = re.sub(r"\.\s+\d{1,3}\s*\Z", ".", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+([,;:.!])", r"\1", text)
    label = _component_label(component_id)
    if label:
        text = re.sub(
            rf"\b{re.escape(label)}\s*\.\s*[A-Z][^.]{{3,80}}?\.\s*-\s*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    text = re.sub(r"\[\*+\]", "", text)
    text = re.sub(r"\*+", "", text)
    # Fix PDF dash doubling: .-- → .-
    text = re.sub(r"\.\s*--\s*", ".- ", text)
    # Rejoin hyphenated line breaks: "sub- section" → "sub-section"
    text = re.sub(r"(\w)-\s+(\w)", r"\1-\2", text)
    # Strip parenthetical Act reference numbers: "1872 (1 of 1872)" → "1872"
    text = re.sub(r"\(\d+\s+of\s+\d+\)", "", text)
    # Normalize Unicode quote artifacts from PDF
    text = text.replace("\u2016", '"').replace("\u2015", '"')
    # Strip trailing enforcement date markers
    text = re.sub(
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:July|January|February|March|April|May|June|August|September|October|November|December),?\s*\d{4}\.?\s*$",
        "",
        text,
    )
    # Collapse double dashes in non-heading context
    text = re.sub(r"(?<=\w)--(?=\w)", "-", text)
    text = re.sub(r"(?<=\w)--(?=\s)", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _deep_normalized_text(value: str, component_id: str = "") -> str:
    """Aggressive normalization for the minor-difference similarity band.

    Builds on :func:`_normalize_for_comparison` (which already strips annotation
    clauses, footnote markers, and Unicode artifacts) and additionally removes
    inline footnote bleed (``N[`` / ``[N]``) and collapses to a lowercase
    alphanumeric string. This canonicalizes duplicated display headings — a
    common PDF extraction artifact in the CBIC checkpoint where the rule heading
    appears both as a standalone display line and inline before the body — so
    that annotation/formatting differences do not suppress the deep similarity.
    """
    text = _normalize_for_comparison(value, component_id)
    text = re.sub(r"\d+\[", " ", text)
    text = re.sub(r"\[\d+\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[^0-9A-Za-z]+", "", text).lower()
    return text


def _is_minor_substantive_difference(
    reconstructed_text: str,
    checkpoint_text: str,
    component_id: str,
    similarity: float,
) -> bool:
    """Return True when a band mismatch is annotation/formatting, not legal text.

    Only components whose normalized similarity falls in the
    ``[MINOR_SUBSTANTIVE_SIMILARITY_LOWER, FORMAT_ONLY_SIMILARITY_THRESHOLD)``
    band are considered. Within that band, the reconstructed and checkpoint
    texts are re-compared after deep normalization. If the deep-normalized
    similarity is high AND the normalized lengths are close (ruling out missing
    or extra legal content), the remaining difference is annotation, footnote
    bleed, or duplicated display headings — not a substantive legal change.
    """
    if not (MINOR_SUBSTANTIVE_SIMILARITY_LOWER <= similarity < FORMAT_ONLY_SIMILARITY_THRESHOLD):
        return False
    recon_deep = _deep_normalized_text(reconstructed_text, component_id)
    checkpoint_deep = _deep_normalized_text(checkpoint_text, component_id)
    if not recon_deep or not checkpoint_deep:
        return False
    length_ratio = len(recon_deep) / len(checkpoint_deep)
    if not (MINOR_SUBSTANTIVE_LENGTH_RATIO_MIN <= length_ratio <= MINOR_SUBSTANTIVE_LENGTH_RATIO_MAX):
        return False
    deep_similarity = SequenceMatcher(None, recon_deep, checkpoint_deep).ratio()
    return deep_similarity >= MINOR_SUBSTANTIVE_NORMALIZED_SIMILARITY


_LEGAL_DIFFERENCE_PATTERNS = (
    re.compile(r"(?:section|rule|clause|sub-section|sub-rule|article)\s+\d+[a-z]?", re.IGNORECASE),
    re.compile(r"\b(?:19|20)\d{2}\b"),
    re.compile(r"\b\d{1,3}(?:,\d{2,3})+\b"),
    re.compile(r"\b\d+(?:\.\d+)?\s*per\s*cent\b", re.IGNORECASE),
    re.compile(
        r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|fifty|hundred)\s+per\s+cent\b",
        re.IGNORECASE,
    ),
)


def _has_potential_legal_difference(text_a: str, text_b: str) -> bool:
    for pattern in _LEGAL_DIFFERENCE_PATTERNS:
        if set(pattern.findall(text_a or "")) != set(pattern.findall(text_b or "")):
            return True
    return False


def _candidate_quality(component_id: str, text: str) -> tuple[int, int, int]:
    label = re.escape(_component_label(component_id))
    clean = _clean(text)
    starts_with_label = 1 if label and re.match(rf"^\[?{label}\b[\].\s-]*", clean, flags=re.I) else 0
    amendment_note = 1 if re.search(r"\b(inserted|omitted|substituted)\s+vide\b|\bnotf\b|\bpage\s+\d+\s+of\b", clean, flags=re.I) else 0
    table_fragment = 1 if re.search(r"\bregistered persons whose principal place\b|\bfor every \d+\s*km\b|\bchapter\s+\d+\b", clean, flags=re.I) else 0
    return (starts_with_label, -amendment_note - table_fragment, len(clean))


def _best_static_candidate(component_id: str, candidates: list[str]) -> str:
    if not candidates:
        return ""
    return sorted(candidates, key=lambda text: _candidate_quality(component_id, text))[-1]


_CHECKPOINT_PLACEHOLDER_PATTERN = re.compile(
    r"\bRule\s+[0-9]+[A-Z]?\s*\(\s*inserted\s+by\s+amendment\s+notification\s*\)"
    r"|\[\s*Content\s+to\s+be\s+extracted\s+from\s+amendment\s+notification\s*\.?\s*\]",
    flags=re.IGNORECASE,
)


def _is_checkpoint_placeholder_candidate(text: str) -> bool:
    return bool(_CHECKPOINT_PLACEHOLDER_PATTERN.search(_clean(text)))


def _is_ignorable_checkpoint_component(component_id: str, checkpoint_candidates: list[str]) -> bool:
    # The checkpoint builder can emit synthetic top-level rule components for
    # amendment placeholders such as "7A. Rule 7A (inserted by amendment
    # notification)". Those are not consolidated legal text and should not be
    # treated as missing reconstructions.
    if "/rule/" not in component_id or not checkpoint_candidates:
        return False
    return all(_is_checkpoint_placeholder_candidate(candidate) for candidate in checkpoint_candidates)


def checkpoint_components(path: Path) -> dict[str, str]:
    return {
        component_id: _best_static_candidate(component_id, candidates)
        for component_id, candidates in checkpoint_component_candidates(path).items()
    }


def _version_at(rows: list[dict[str, Any]], date_value: str) -> dict[str, Any] | None:
    candidates = []
    for row in rows:
        start = row.get("applicability_start") or row.get("valid_from")
        end = row.get("applicability_end") or row.get("valid_to")
        if start and start <= date_value and (not end or date_value < end):
            candidates.append(row)
    return sorted(candidates, key=lambda item: item.get("applicability_start") or item.get("valid_from") or "")[-1] if candidates else None


def _row_start(row: dict[str, Any]) -> str:
    return str(row.get("applicability_start") or row.get("valid_from") or "")


def _row_end(row: dict[str, Any]) -> str:
    return str(row.get("applicability_end") or row.get("valid_to") or "")


def _first_version_after(rows: list[dict[str, Any]], date_value: str) -> dict[str, Any] | None:
    future = [row for row in rows if _row_start(row) and _row_start(row) > date_value]
    return sorted(future, key=_row_start)[0] if future else None


def _last_version_before(rows: list[dict[str, Any]], date_value: str) -> dict[str, Any] | None:
    past = [row for row in rows if _row_start(row) and _row_start(row) <= date_value]
    return sorted(past, key=_row_start)[-1] if past else None


def _row_is_omitted(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    if row.get("omitted") is True or row.get("is_omitted") is True:
        return True
    source_basis = row.get("source_basis") if isinstance(row.get("source_basis"), dict) else {}
    if str(source_basis.get("operation") or row.get("operation") or "").upper() == "OMIT":
        return True
    text = _clean(str(row.get("text") or ""))
    return not text and bool(row.get("created_by_event_id") or row.get("event_chain"))


def _row_evidence(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "version_id": row.get("version_id"),
        "created_by_event_id": row.get("created_by_event_id"),
        "event_chain": row.get("event_chain") or [],
        "source_basis": row.get("source_basis") or {},
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "applicability_start": row.get("applicability_start"),
        "applicability_end": row.get("applicability_end"),
    }


def _strip_child_prefix(child_text: str) -> str:
    """Strip a leading ``N\\n`` marker line from a stored sub-rule text."""
    text = str(child_text or "").lstrip()
    match = re.match(r"^\d+[a-z]?\s*\n\s*", text, flags=re.IGNORECASE)
    if match:
        return text[match.end():].lstrip()
    return text


def _merge_child_versions(
    component_id: str,
    parent_text: str,
    by_component: dict[str, list[dict[str, Any]]],
    checkpoint_date: str,
) -> str:
    """Merge updated child (sub-rule) bodies into the parent's text for comparison.

    Parent rule baselines inline the original pre-amendment sub-rule text.
    When a sub-rule is later amended, only the child's version row is updated;
    the parent's stored text becomes stale. The CBIC consolidated checkpoint
    inlines every current sub-rule, so comparing the stale parent baseline
    against the checkpoint flags a substantive mismatch that is really just
    stale parent text. This function rewrites each stale sub-rule body
    embedded in ``parent_text`` with the child body valid at
    ``checkpoint_date`` so the comparison reflects current law.
    """
    if "/subrule/" in component_id or not parent_text:
        return parent_text
    prefix = component_id.rstrip("/") + "/subrule/"
    children = sorted(cid for cid in by_component if cid.startswith(prefix))
    if not children:
        return parent_text
    merged = parent_text
    for child_id in children:
        child_rows = by_component.get(child_id) or []
        if not child_rows:
            continue
        sorted_rows = sorted(
            child_rows,
            key=lambda row: row.get("applicability_start") or row.get("valid_from") or "",
        )
        baseline_row = sorted_rows[0]
        latest_row = _version_at(child_rows, checkpoint_date) or sorted_rows[-1]
        if baseline_row is latest_row:
            continue
        baseline_body = _strip_child_prefix(str(baseline_row.get("text") or ""))
        latest_body = _strip_child_prefix(str(latest_row.get("text") or ""))
        if not baseline_body or not latest_body or baseline_body == latest_body:
            continue
        if baseline_body in merged:
            merged = merged.replace(baseline_body, latest_body, 1)
    return merged


def _best_reconciliation_candidate(component_id: str, reconstructed_text: str, candidates: list[str]) -> tuple[str, float]:
    if not candidates:
        return "", 0.0
    reconstructed_clean = _normalize_for_comparison(reconstructed_text, component_id)
    scored = [
        (
            SequenceMatcher(None, reconstructed_clean, _normalize_for_comparison(candidate, component_id)).ratio(),
            len(_normalize_for_comparison(candidate, component_id)),
            candidate,
        )
        for candidate in candidates
    ]
    best = sorted(scored)[-1]
    return best[2], best[0]


def _related_gap_ids(component_id: str, coverage_gaps: list[dict[str, Any]]) -> list[str]:
    return [row["event_id"] for row in _related_gaps(component_id, coverage_gaps) if row.get("event_id")]


def _normalized_component_key(component_id: str) -> str:
    return normalize_version_component_id(component_id).lower()


def _gap_mentions_component(component_id: str, gap: dict[str, Any]) -> bool:
    label = _component_label(component_id)
    if not label:
        return False
    if not any(char.isalpha() for char in label):
        return False
    label_pattern = _component_label_pattern(label)
    if not label_pattern:
        return False
    text = " ".join(
        str(value or "")
        for value in (
            gap.get("excerpt"),
            gap.get("source_document_id"),
            (gap.get("target") or {}).get("anchor_text"),
        )
    )
    return bool(
        re.search(rf"\brule\s+{label_pattern}\b", text, flags=re.IGNORECASE)
        or re.search(rf"(?<![A-Za-z0-9]){label_pattern}\s*\.", text, flags=re.IGNORECASE)
    )


def _related_gaps(component_id: str, coverage_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    component_key = _normalized_component_key(component_id)
    related: list[dict[str, Any]] = []
    for gap in coverage_gaps:
        target = str(gap.get("target", {}).get("component_id") or "")
        target_key = _normalized_component_key(target) if target else ""
        target_is_structural = any(marker in target_key for marker in ("/rule/", "/section/", "/subrule/"))
        target_related = bool(
            target_key
            and (
                target_key == component_key
                or (
                    target_is_structural
                    and (target_key.startswith(component_key + "/") or component_key.startswith(target_key + "/"))
                )
            )
        )
        if target_related or _gap_mentions_component(component_id, gap):
            related.append(gap)
    return sorted(
        {str(row.get("event_id") or ""): row for row in related if row.get("event_id")}.values(),
        key=lambda row: str(row.get("event_id") or ""),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _event_mentions_component(component_id: str, event: dict[str, Any]) -> bool:
    label = _component_label(component_id)
    component_key = _normalized_component_key(component_id)
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    target_id = str(target.get("component_id") or "")
    target_key = _normalized_component_key(target_id) if target_id else ""
    target_is_structural = any(marker in target_key for marker in ("/rule/", "/section/", "/subrule/"))
    if target_is_structural and (
        target_key == component_key or target_key.startswith(component_key + "/") or component_key.startswith(target_key + "/")
    ):
        return True
    if not label:
        return False
    label_pattern = _component_label_pattern(label)
    if not label_pattern:
        return False
    text = " ".join(
        str(value or "")
        for value in (
            event.get("event_id"),
            event.get("source_document_id"),
            event.get("operation"),
            event.get("review", {}).get("triage", {}).get("materialization_hint")
            if isinstance(event.get("review"), dict)
            else "",
            event.get("evidence", {}).get("excerpt") if isinstance(event.get("evidence"), dict) else "",
            event.get("payload", {}).get("content") if isinstance(event.get("payload"), dict) else "",
            event.get("payload", {}).get("new_text") if isinstance(event.get("payload"), dict) else "",
        )
    )
    return bool(
        re.search(rf"\brule\s+{label_pattern}\b", text, flags=re.IGNORECASE)
        or re.search(rf"\bin\s+the\s+said\s+rules?,\s+in\s+rule\s+{label_pattern}\b", text, flags=re.IGNORECASE)
        or re.search(rf"(?<![A-Za-z0-9]){label_pattern}\s*\.", text, flags=re.IGNORECASE)
    )


def _event_audit_summary(event: dict[str, Any]) -> dict[str, Any]:
    review = event.get("review") if isinstance(event.get("review"), dict) else {}
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    return {
        "event_id": event.get("event_id"),
        "operation": event.get("operation"),
        "status": event.get("status"),
        "source_document_id": event.get("source_document_id") or source.get("document_id"),
        "source_span": event.get("source_span") or evidence.get("source_span"),
        "target_component_id": target.get("component_id"),
        "review_reasons": review.get("review_reasons") or event.get("review_reasons") or [],
        "excerpt": str(evidence.get("excerpt") or event.get("excerpt") or "")[:700],
    }


def _component_rule_label(component_id: str) -> str | None:
    match = re.search(r"/rule/([^/]+)", component_id)
    return match.group(1) if match else None


def _candidate_relevant_to_component(component_id: str, event: dict[str, Any]) -> bool:
    target_id = str(event.get("target_component_id") or "")
    if target_id == component_id or target_id.startswith(component_id + "/"):
        return True
    if component_id.startswith(target_id + "/") and "/rule/" in target_id:
        return True

    reasons = set(event.get("review_reasons") or [])
    operation = str(event.get("operation") or "").upper()
    noisy_reasons = {
        "forms_lane_pending_baseline",
        "rules_table_lane",
        "target_component_outside_work",
        "unsupported_form_or_table_mutation",
        "metadata_only",
        "baseline_source_only",
    }
    if target_id and "/forms/" in target_id:
        return False
    if operation == "UNKNOWN" and reasons & noisy_reasons:
        return False

    excerpt = str(event.get("excerpt") or "")
    rule_label = _component_rule_label(component_id)
    names_component_rule = bool(
        rule_label
        and re.search(rf"\brule\s+{re.escape(rule_label)}\b", excerpt, flags=re.IGNORECASE)
    )
    if names_component_rule:
        return True

    return False


def _audit_class_for_outcome(outcome: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    status = str(outcome.get("status") or "")
    if status == "missing_reconstruction":
        return "manual_backfill_needed" if candidates else "no_candidate_found"
    if status == "checkpoint_source_incomplete":
        return "checkpoint_dispute"
    similarity = float(outcome.get("best_similarity") or 0.0)
    if similarity >= FORMAT_ONLY_SIMILARITY_THRESHOLD:
        return "normalization_candidate"
    component_id = str(outcome.get("component_id") or "")
    classification_candidates = [
        event for event in candidates if _candidate_relevant_to_component(component_id, event)
    ]
    if not classification_candidates and candidates:
        return "manual_backfill_needed"

    validated_candidates = [
        event
        for event in classification_candidates
        if str(event.get("status") or "").lower() == "validated" and str(event.get("operation") or "").upper() != "UNKNOWN"
    ]
    validated_reasons = {
        reason
        for event in validated_candidates
        for reason in (event.get("review_reasons") or [])
    }
    validated_operations = {
        str(event.get("operation") or "").upper() for event in validated_candidates
    }
    if "compound_block_contains_multiple_amendments" in validated_reasons:
        return "compound_split_needed"
    if "anchor_not_resolved" in validated_reasons:
        return "missing_insert_child"
    if "OMIT" in validated_operations or "compound_block_contains_unsupported_omission" in validated_reasons:
        return "missing_omit"
    if "SUBSTITUTE" in validated_operations or "SPLICE" in validated_operations:
        return "missing_substitution"
    if "INSERT_SIBLING" in validated_operations or "INSERT_CHILD" in validated_operations:
        return "missing_insert_child"
    if validated_candidates:
        return "manual_backfill_needed"

    all_reasons = {
        reason
        for event in classification_candidates
        for reason in (event.get("review_reasons") or [])
    }
    all_operations = {str(event.get("operation") or "").upper() for event in classification_candidates}
    if "compound_block_contains_multiple_amendments" in all_reasons or "UNKNOWN" in all_operations:
        return "compound_split_needed"
    if "anchor_not_resolved" in all_reasons:
        return "missing_insert_child"
    if "OMIT" in all_operations or "compound_block_contains_unsupported_omission" in all_reasons:
        return "missing_omit"
    if "SUBSTITUTE" in all_operations or "SPLICE" in all_operations:
        return "missing_substitution"
    if "INSERT_SIBLING" in all_operations or "INSERT_CHILD" in all_operations:
        return "missing_insert_child"
    if classification_candidates:
        return "manual_backfill_needed"
    return "no_candidate_found"


def _build_unresolved_audit(
    *,
    component_outcomes: dict[str, dict[str, Any]],
    events_path: Path,
) -> dict[str, Any]:
    unresolved_statuses = {"true_substantive_mismatch", "missing_reconstruction", "checkpoint_source_incomplete"}
    unresolved = [
        outcome
        for outcome in component_outcomes.values()
        if outcome.get("status") in unresolved_statuses
    ]
    events = _read_jsonl(events_path)
    rows: list[dict[str, Any]] = []
    class_counts: dict[str, int] = {}
    for outcome in sorted(unresolved, key=lambda item: (float(item.get("best_similarity") or 0.0), str(item.get("component_id") or ""))):
        component_id = str(outcome.get("component_id") or "")
        candidate_events = [_event_audit_summary(event) for event in events if _event_mentions_component(component_id, event)]
        audit_class = _audit_class_for_outcome(outcome, candidate_events)
        class_counts[audit_class] = class_counts.get(audit_class, 0) + 1
        evidence = outcome.get("evidence") if isinstance(outcome.get("evidence"), dict) else {}
        rows.append(
            {
                "component_id": component_id,
                "status": outcome.get("status"),
                "checkpoint_date": outcome.get("checkpoint_date"),
                "best_similarity": outcome.get("best_similarity"),
                "tier": None,
                "audit_class": audit_class,
                "reconstructed_sha256": outcome.get("reconstructed_sha256"),
                "checkpoint_sha256": outcome.get("checkpoint_sha256"),
                "reconstructed_substantive_sha256": outcome.get("reconstructed_substantive_sha256"),
                "checkpoint_substantive_sha256": outcome.get("checkpoint_substantive_sha256"),
                "reason": outcome.get("reason"),
                "checkpoint_candidate_preview": str(evidence.get("checkpoint_candidate_preview") or "")[:700],
                "reconstructed_version": evidence.get("reconstructed_version") or {},
                "candidate_event_count": len(candidate_events),
                "candidate_source_documents": sorted(
                    {
                        str(event.get("source_document_id") or "")
                        for event in candidate_events
                        if event.get("source_document_id")
                    }
                )[:25],
                "candidate_event_ids": [
                    str(event.get("event_id") or "")
                    for event in candidate_events[:25]
                    if event.get("event_id")
                ],
                "candidate_events": candidate_events[:10],
            }
        )
    return {
        "version": "reconciliation-unresolved-audit-v1",
        "events_path": str(events_path),
        "unresolved_count": len(rows),
        "audit_class_counts": class_counts,
        "rows": rows,
    }


_FUTURE_COMMENCEMENT_PATTERN = re.compile(
    r"\b(?:date|day)\s+(?:as\s+may\s+be|to\s+be)\s+notified\b"
    r"|\bsuch\s+date\s+as\s+may\s+be\s+notified\b"
    r"|\bfrom\s+a\s+date\s+to\s+be\s+notified\b",
    flags=re.IGNORECASE,
)


def _gap_has_unresolved_commencement(gap: dict[str, Any]) -> bool:
    reasons = set(gap.get("review_reasons") or [])
    if "date_not_resolved" in reasons:
        return True
    text = " ".join(str(value or "") for value in (gap.get("excerpt"), gap.get("skip_reason")))
    return bool(_FUTURE_COMMENCEMENT_PATTERN.search(text))


def _gap_summary(gap: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": gap.get("event_id"),
        "source_document_id": gap.get("source_document_id"),
        "source_span": gap.get("source_span"),
        "operation": gap.get("operation"),
        "status": gap.get("status"),
        "skip_reason": gap.get("skip_reason"),
        "review_reasons": gap.get("review_reasons") or [],
        "excerpt": str(gap.get("excerpt") or "")[:700],
    }


def _queue_row(component_id: str, reason: str, similarity: float, related_gaps: list[dict[str, Any]]) -> dict[str, Any]:
    blocker_gaps = [gap for gap in related_gaps if _gap_has_unresolved_commencement(gap)]
    row: dict[str, Any] = {
        "component_id": component_id,
        "reason": reason,
        "best_similarity": similarity,
        "related_gap_count": len(related_gaps),
        "related_event_ids": [str(gap.get("event_id") or "") for gap in related_gaps[:25] if gap.get("event_id")],
    }
    if blocker_gaps:
        row.update(
            {
                "blocker": "unresolved_commencement",
                "blocked_by_unresolved_commencement": True,
                "blocker_reasons": sorted(
                    {
                        reason
                        for gap in blocker_gaps
                        for reason in (gap.get("review_reasons") or [])
                    }
                    | {"future_commencement_language_in_source"}
                ),
                "recommended_action": "find_commencement_notification_before_materialization",
                "related_gap_summaries": [_gap_summary(gap) for gap in blocker_gaps[:10]],
            }
        )
    return row


def _queue_row_from_audit(audit_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "component_id": audit_row.get("component_id"),
        "reason": audit_row.get("status") or "unresolved_reconciliation",
        "audit_class": audit_row.get("audit_class"),
        "best_similarity": audit_row.get("best_similarity"),
        "related_gap_count": 0,
        "related_event_ids": audit_row.get("candidate_event_ids") or [],
        "candidate_event_count": audit_row.get("candidate_event_count") or 0,
        "candidate_source_documents": audit_row.get("candidate_source_documents") or [],
        "recommended_action": _recommended_action_for_audit_class(str(audit_row.get("audit_class") or "")),
    }


def _recommended_action_for_audit_class(audit_class: str) -> str:
    return {
        "normalization_candidate": "inspect normalized diff and add a narrow legally-neutral normalization if justified",
        "missing_substitution": "extract and materialize the source-backed substitution or splice event",
        "missing_insert_child": "extract the missing child or sibling insertion with an exact anchor",
        "missing_omit": "extract the source-backed omission and verify the omitted state",
        "compound_split_needed": "split the source compound block into deterministic child amendment events",
        "checkpoint_dispute": "prove reconstruction from source history or keep the checkpoint warning visible",
        "manual_backfill_needed": "add a source-span-backed narrow repair if compiler generalization is unsafe",
        "no_candidate_found": "inspect source notifications manually and add attribution before materialization",
    }.get(audit_class, "inspect source evidence and classify the reconciliation blocker")


def _audit_priority(audit_class: str) -> int:
    priorities = {
        "compound_split_needed": 0,
        "missing_substitution": 1,
        "missing_insert_child": 2,
        "missing_omit": 3,
        "normalization_candidate": 4,
        "manual_backfill_needed": 5,
        "checkpoint_dispute": 6,
        "no_candidate_found": 7,
    }
    return priorities.get(audit_class, 9)


def _checkpoint_source_manifest(checkpoint_path: Path) -> dict[str, Any] | None:
    base_dir = checkpoint_path.parent if checkpoint_path.is_file() else checkpoint_path
    for name in ["checkpoint_manifest.json", "fetch_manifest.json"]:
        candidate = base_dir / name
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"manifest_path": str(candidate), "manifest_parse_error": True}
        return {
            "manifest_path": str(candidate),
            "source_label": payload.get("source_label"),
            "source_url": payload.get("source_url"),
            "observed_at": payload.get("observed_at"),
            "checkpoint_date": payload.get("checkpoint_date"),
            "source_sha256": payload.get("source_sha256"),
            "checkpoint_source_type": payload.get("checkpoint_source_type"),
            "checkpoint_component_count": payload.get("checkpoint_component_count"),
            "taxinformation_section_limit": payload.get("taxinformation_section_limit"),
            "taxinformation_section_limit_reached": payload.get("taxinformation_section_limit_reached"),
            "taxinformation_skipped_count": payload.get("taxinformation_skipped_count"),
            "taxinformation_skipped_sections": payload.get("taxinformation_skipped_sections"),
            "required_labels": payload.get("required_labels"),
            "present_required_labels": payload.get("present_required_labels"),
            "missing_required_labels": payload.get("missing_required_labels"),
        }
    return None


def _checkpoint_source_warnings(manifest: dict[str, Any] | None, checkpoint_date: str) -> list[str]:
    if not manifest:
        return ["checkpoint_source_manifest_missing"]
    warnings: list[str] = []
    manifest_date = str(manifest.get("checkpoint_date") or "")
    if manifest_date and manifest_date != checkpoint_date:
        warnings.append("checkpoint_date_mismatch")
    if manifest.get("taxinformation_section_limit_reached"):
        warnings.append("checkpoint_source_section_limited")
    if manifest.get("missing_required_labels"):
        warnings.append("checkpoint_source_missing_required_labels")
    return warnings


def _base_outcome(
    *,
    component_id: str,
    checkpoint_date: str,
    status: str,
    reason: str,
    checkpoint_candidates: list[str] | None = None,
    reconstructed: dict[str, Any] | None = None,
    checkpoint_text: str = "",
    similarity: float = 0.0,
    reconstructed_text_override: str | None = None,
) -> dict[str, Any]:
    reconstructed_text = (
        reconstructed_text_override
        if reconstructed_text_override is not None
        else str((reconstructed or {}).get("text") or "")
    )
    return {
        "component_id": component_id,
        "checkpoint_date": checkpoint_date,
        "status": status,
        "reason": reason,
        "reconstructed_sha256": _reconciliation_hash(reconstructed_text, component_id) if reconstructed is not None else None,
        "checkpoint_sha256": _reconciliation_hash(checkpoint_text, component_id) if checkpoint_text else None,
        "reconstructed_substantive_sha256": _substantive_reconciliation_hash(reconstructed_text, component_id)
        if reconstructed is not None
        else None,
        "checkpoint_substantive_sha256": _substantive_reconciliation_hash(checkpoint_text, component_id) if checkpoint_text else None,
        "best_similarity": round(float(similarity or 0.0), 6),
        "selected_checkpoint_candidate_count": len(checkpoint_candidates or []),
        "evidence": {
            "reconstructed_version": _row_evidence(reconstructed),
            "checkpoint_candidate_preview": _clean(checkpoint_text)[:700] if checkpoint_text else "",
        },
    }


def _priority_review_queue(
    *,
    version_dir: Path,
    mismatched: list[dict[str, Any]],
    missing: list[str],
) -> list[dict[str, Any]]:
    gaps_path = version_dir / "coverage_gaps.json"
    if not gaps_path.exists():
        return []
    try:
        coverage_payload = json.loads(gaps_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    coverage_gaps = coverage_payload.get("gaps", coverage_payload) if isinstance(coverage_payload, dict) else coverage_payload
    if not isinstance(coverage_gaps, list):
        return []
    queue: list[dict[str, Any]] = []
    for item in mismatched:
        component_id = str(item.get("component_id") or "")
        related = _related_gaps(component_id, coverage_gaps)
        queue.append(_queue_row(component_id, "checkpoint_mismatch", item.get("best_similarity"), related))
    for component_id in missing:
        related = _related_gaps(component_id, coverage_gaps)
        queue.append(_queue_row(component_id, "checkpoint_missing_reconstruction", 0.0, related))
    return sorted(
        queue,
        key=lambda row: (
            -int(row.get("related_gap_count") or 0),
            float(row.get("best_similarity") or 0.0),
            str(row.get("component_id") or ""),
        ),
    )


def reconcile(
    *,
    target_work: str,
    checkpoint_path: Path,
    checkpoint_date: str,
    output: Path,
    registry_path: Path = Path("data/Law/statute_identity_registry.json"),
    version_dir: Path | None = None,
    events_path: Path = DEFAULT_EVENTS_PATH,
    audit_output: Path | None = None,
) -> dict[str, Any]:
    resolved_dir, resolved_work = resolve_version_dir(
        target_work,
        target_work=target_work,
        registry_path=registry_path,
        version_dir=version_dir,
    )
    rows = read_node_versions(resolved_dir / "node_versions.jsonl")
    by_component: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_component.setdefault(normalize_version_component_id(str(row.get("component_id") or "")), []).append(row)
    checkpoint = checkpoint_component_candidates(checkpoint_path)
    source_manifest = _checkpoint_source_manifest(checkpoint_path)
    checkpoint_source_warnings = _checkpoint_source_warnings(source_manifest, checkpoint_date)
    matched = []
    format_only = []
    mismatched = []
    missing = []
    minor_difference = []
    component_outcomes: dict[str, dict[str, Any]] = {}
    checkpoint_component_ids = set(checkpoint)
    reconstruction_component_ids = set(by_component)
    for component_id, checkpoint_candidates in sorted(checkpoint.items()):
        if _is_ignorable_checkpoint_component(component_id, checkpoint_candidates):
            continue
        component_rows = by_component.get(component_id, [])
        reconstructed = _version_at(component_rows, checkpoint_date)
        if not reconstructed:
            # Skip checkpoint elements that are annotation/footnote IDs, not real rules.
            # CGST Rules 2017 only go to Rule 164 (plus known lettered insertions).
            # The checkpoint XML assigns sequential IDs (165, 166, ...) to annotation elements.
            rule_label = component_id.rsplit("/rule/", 1)[-1].split("/", 1)[0] if "/rule/" in component_id else ""
            if rule_label.isdigit() and int(rule_label) > 164:
                continue  # Annotation element ID, not a CGST Rule
            first_future = _first_version_after(component_rows, checkpoint_date)
            last_before = _last_version_before(component_rows, checkpoint_date)
            if first_future and not last_before:
                component_outcomes[component_id] = _base_outcome(
                    component_id=component_id,
                    checkpoint_date=checkpoint_date,
                    status="post_checkpoint_not_applicable",
                    reason="component_first_exists_after_checkpoint_date",
                    checkpoint_candidates=checkpoint_candidates,
                    reconstructed=first_future,
                    checkpoint_text=_best_static_candidate(component_id, checkpoint_candidates),
                )
                component_outcomes[component_id]["evidence"]["first_future_version"] = _row_evidence(first_future)
                continue
            if _row_is_omitted(last_before):
                component_outcomes[component_id] = _base_outcome(
                    component_id=component_id,
                    checkpoint_date=checkpoint_date,
                    status="omitted_correct",
                    reason="reconstructed_state_omitted_before_checkpoint_date",
                    checkpoint_candidates=checkpoint_candidates,
                    reconstructed=last_before,
                    checkpoint_text=_best_static_candidate(component_id, checkpoint_candidates),
                )
                component_outcomes[component_id]["evidence"]["omission_version"] = _row_evidence(last_before)
                continue
            missing.append(component_id)
            component_outcomes[component_id] = _base_outcome(
                component_id=component_id,
                checkpoint_date=checkpoint_date,
                status="missing_reconstruction",
                reason="checkpoint_component_has_no_reconstructed_state_at_checkpoint_date",
                checkpoint_candidates=checkpoint_candidates,
                reconstructed=None,
                checkpoint_text=_best_static_candidate(component_id, checkpoint_candidates),
            )
            continue
        if _row_is_omitted(reconstructed):
            checkpoint_text = _best_static_candidate(component_id, checkpoint_candidates)
            component_outcomes[component_id] = _base_outcome(
                component_id=component_id,
                checkpoint_date=checkpoint_date,
                status="omitted_correct",
                reason="reconstructed_active_state_is_source_backed_omission",
                checkpoint_candidates=checkpoint_candidates,
                reconstructed=reconstructed,
                checkpoint_text=checkpoint_text,
            )
            component_outcomes[component_id]["evidence"]["omission_version"] = _row_evidence(reconstructed)
            continue
        reconstructed_text_raw = str(reconstructed.get("text") or "")
        # If raw text already matches a checkpoint candidate, skip child merge
        # (full_replacement repair events already contain correct consolidated text).
        raw_checkpoint_text, raw_similarity = _best_reconciliation_candidate(
            component_id,
            reconstructed_text_raw,
            checkpoint_candidates,
        )
        raw_hash = _reconciliation_hash(reconstructed_text_raw, component_id)
        raw_cp_hash = _reconciliation_hash(raw_checkpoint_text, component_id)
        if raw_hash == raw_cp_hash:
            reconstructed_text_for_comparison = reconstructed_text_raw
        else:
            reconstructed_text_for_comparison = _merge_child_versions(
                component_id,
                reconstructed_text_raw,
                by_component,
                checkpoint_date,
            )
        checkpoint_text, similarity = _best_reconciliation_candidate(
            component_id,
            reconstructed_text_for_comparison,
            checkpoint_candidates,
        )
        reconstructed_hash = _reconciliation_hash(reconstructed_text_for_comparison, component_id)
        checkpoint_hash = _reconciliation_hash(checkpoint_text, component_id)
        reconstructed_substantive_hash = _substantive_reconciliation_hash(
            reconstructed_text_for_comparison, component_id
        )
        checkpoint_substantive_hash = _substantive_reconciliation_hash(checkpoint_text, component_id)
        reconstructed_text_override = (
            reconstructed_text_for_comparison
            if reconstructed_text_for_comparison != reconstructed_text_raw
            else None
        )
        heading_prefix_equivalent = _substantive_texts_equivalent(
            reconstructed_text_for_comparison, checkpoint_text, component_id
        )
        if reconstructed_hash == checkpoint_hash:
            matched.append(component_id)
            component_outcomes[component_id] = _base_outcome(
                component_id=component_id,
                checkpoint_date=checkpoint_date,
                status="exact_match",
                reason="strict_normalized_hashes_match",
                checkpoint_candidates=checkpoint_candidates,
                reconstructed=reconstructed,
                checkpoint_text=checkpoint_text,
                similarity=similarity,
                reconstructed_text_override=reconstructed_text_override,
            )
        elif (
            reconstructed_substantive_hash == checkpoint_substantive_hash
            or heading_prefix_equivalent
            or similarity >= FORMAT_ONLY_SIMILARITY_THRESHOLD
        ):
            reason = (
                "substantive_normalized_hashes_match"
                if reconstructed_substantive_hash == checkpoint_substantive_hash
                else (
                    "substantive_texts_match_after_display_heading_prefix"
                    if heading_prefix_equivalent
                    else "substantive_similarity_above_format_only_threshold"
                )
            )
            outcome = _base_outcome(
                component_id=component_id,
                checkpoint_date=checkpoint_date,
                status="format_only_match",
                reason=reason,
                checkpoint_candidates=checkpoint_candidates,
                reconstructed=reconstructed,
                checkpoint_text=checkpoint_text,
                similarity=similarity,
                reconstructed_text_override=reconstructed_text_override,
            )
            component_outcomes[component_id] = outcome
            format_only.append(
                {
                    "component_id": component_id,
                    "reconstructed_sha256": reconstructed_hash,
                    "checkpoint_sha256": checkpoint_hash,
                    "candidate_count": len(checkpoint_candidates),
                    "best_similarity": round(similarity, 6),
                    "reason": "format_or_annotation_only",
                }
            )
        elif _is_minor_substantive_difference(
            reconstructed_text_for_comparison, checkpoint_text, component_id, similarity
        ):
            outcome = _base_outcome(
                component_id=component_id,
                checkpoint_date=checkpoint_date,
                status="minor_substantive_difference",
                reason="deep_normalized_similarity_annotation_only_difference",
                checkpoint_candidates=checkpoint_candidates,
                reconstructed=reconstructed,
                checkpoint_text=checkpoint_text,
                similarity=similarity,
                reconstructed_text_override=reconstructed_text_override,
            )
            component_outcomes[component_id] = outcome
            minor_difference.append(
                {
                    "component_id": component_id,
                    "reconstructed_sha256": reconstructed_hash,
                    "checkpoint_sha256": checkpoint_hash,
                    "candidate_count": len(checkpoint_candidates),
                    "best_similarity": round(similarity, 6),
                    "reason": "annotation_or_display_heading_only",
                }
            )
        else:
            # Check for invalid comparison due to extreme length ratio
            recon_len = len(reconstructed_text_override or (reconstructed or {}).get("text", "") if isinstance(reconstructed, dict) else str(reconstructed or ""))
            cp_len = len(checkpoint_text or "")
            len_ratio = recon_len / max(cp_len, 1) if cp_len > 0 else 0
            if cp_len < 20 or len_ratio > 3.0 or len_ratio < 0.33:
                outcome = _base_outcome(
                    component_id=component_id,
                    checkpoint_date=checkpoint_date,
                    status="comparison_invalid",
                    reason=f"extreme_length_ratio_or_insufficient_checkpoint_text ratio={len_ratio:.2f}",
                    checkpoint_candidates=checkpoint_candidates,
                    reconstructed=reconstructed,
                    checkpoint_text=checkpoint_text,
                    similarity=similarity,
                    reconstructed_text_override=reconstructed_text_override,
                )
                component_outcomes[component_id] = outcome
            else:
                outcome = _base_outcome(
                    component_id=component_id,
                    checkpoint_date=checkpoint_date,
                    status="true_substantive_mismatch",
                    reason="substantive_normalized_hashes_differ",
                    checkpoint_candidates=checkpoint_candidates,
                    reconstructed=reconstructed,
                    checkpoint_text=checkpoint_text,
                    similarity=similarity,
                    reconstructed_text_override=reconstructed_text_override,
                )
                component_outcomes[component_id] = outcome
                mismatched.append(
                    {
                        "component_id": component_id,
                        "reconstructed_sha256": reconstructed_hash,
                        "checkpoint_sha256": checkpoint_hash,
                        "candidate_count": len(checkpoint_candidates),
                        "best_similarity": round(similarity, 6),
                    }
                )
        if 0.90 <= similarity < 0.999 and _has_potential_legal_difference(
            reconstructed_text_for_comparison, checkpoint_text
        ):
            component_outcomes[component_id]["requires_legal_review"] = True
    for component_id, component_rows in sorted(by_component.items()):
        if component_id in checkpoint_component_ids or component_id in component_outcomes:
            continue
        first_future = _first_version_after(component_rows, checkpoint_date)
        active = _version_at(component_rows, checkpoint_date)
        if first_future and not active and not _last_version_before(component_rows, checkpoint_date):
            component_outcomes[component_id] = _base_outcome(
                component_id=component_id,
                checkpoint_date=checkpoint_date,
                status="post_checkpoint_not_applicable",
                reason="component_first_exists_after_checkpoint_date_and_absent_from_checkpoint",
                reconstructed=first_future,
            )
    if source_manifest and source_manifest.get("missing_required_labels"):
        for label in source_manifest.get("missing_required_labels") or []:
            component_id = _component_id_for_manifest_label(resolved_work or target_work, str(label))
            component_outcomes.setdefault(
                component_id,
                _base_outcome(
                    component_id=component_id,
                    checkpoint_date=checkpoint_date,
                    status="checkpoint_source_incomplete",
                    reason="required_checkpoint_label_missing_from_source",
                ),
            )
    unresolved_audit = _build_unresolved_audit(component_outcomes=component_outcomes, events_path=events_path)
    audit_rows_by_component = {row.get("component_id"): row for row in unresolved_audit.get("rows") or []}
    priority_review_queue = _priority_review_queue(version_dir=resolved_dir, mismatched=mismatched, missing=missing)
    for row in priority_review_queue:
        audit_row = audit_rows_by_component.get(row.get("component_id")) or {}
        if audit_row:
            row["audit_class"] = audit_row.get("audit_class")
            row["candidate_event_count"] = audit_row.get("candidate_event_count")
            row["candidate_source_documents"] = audit_row.get("candidate_source_documents") or []
            if not row.get("related_event_ids"):
                row["related_event_ids"] = audit_row.get("candidate_event_ids") or []
            row.setdefault("recommended_action", _recommended_action_for_audit_class(str(audit_row.get("audit_class") or "")))
    queued_components = {row.get("component_id") for row in priority_review_queue}
    for audit_row in unresolved_audit.get("rows") or []:
        if audit_row.get("component_id") not in queued_components:
            priority_review_queue.append(_queue_row_from_audit(audit_row))
    priority_review_queue = sorted(
        priority_review_queue,
        key=lambda row: (
            _audit_priority(str(row.get("audit_class") or "")),
            float(row.get("best_similarity") or 0.0),
            str(row.get("component_id") or ""),
        ),
    )
    commencement_blocked = [
        row for row in priority_review_queue if row.get("blocker") == "unresolved_commencement"
    ]
    outcome_counts: dict[str, int] = {}
    for outcome in component_outcomes.values():
        status = str(outcome.get("status") or "unknown")
        outcome_counts[status] = outcome_counts.get(status, 0) + 1
    unresolved_statuses = {"true_substantive_mismatch", "missing_reconstruction", "checkpoint_source_incomplete"}
    unresolved_outcomes = [
        outcome for outcome in component_outcomes.values() if outcome.get("status") in unresolved_statuses
    ]
    report = {
        "target_work": resolved_work or target_work,
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_date": checkpoint_date,
        "checkpoint_source_manifest": source_manifest,
        "checkpoint_source_warnings": checkpoint_source_warnings,
        "checkpoint_source_coverage_risk": bool(
            {"checkpoint_source_section_limited", "checkpoint_source_missing_required_labels"} & set(checkpoint_source_warnings)
        ),
        "version_dir": str(resolved_dir),
        "matched_count": len(matched),
        "strict_mismatch_count": len(format_only) + len(mismatched),
        "format_only_mismatch_count": len(format_only),
        "minor_substantive_difference_count": len(minor_difference),
        "substantive_mismatch_count": len(mismatched),
        "mismatched_count": len(mismatched),
        "missing_count": len(missing),
        "matched_components": matched,
        "format_only_mismatched_components": format_only,
        "minor_substantive_difference_components": minor_difference,
        "mismatched_components": mismatched,
        "missing_components": missing,
        "component_outcome_counts": outcome_counts,
        "component_outcomes": component_outcomes,
        "unresolved_reconciliation_audit": unresolved_audit,
        "unresolved_reconciliation_audit_path": str(audit_output or output.with_name("reconciliation_unresolved_audit.json")),
        "unresolved_reconciliation_count": len(unresolved_outcomes),
        "unresolved_reconciliation_components": sorted(str(outcome.get("component_id") or "") for outcome in unresolved_outcomes),
        "priority_review_count": len(priority_review_queue),
        "priority_review_queue": priority_review_queue,
        "commencement_blocked_count": len(commencement_blocked),
        "commencement_blocked_components": [row.get("component_id") for row in commencement_blocked],
        "coverage": "complete" if not mismatched and not missing else "incomplete",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    audit_path = audit_output or output.with_name("reconciliation_unresolved_audit.json")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(unresolved_audit, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return report


__all__ = ["checkpoint_component_candidates", "checkpoint_components", "reconcile"]
