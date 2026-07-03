#!/usr/bin/env python3
"""Build provision-level vector chunks from GST corpus XML.

Unlike the flat 128-token window chunking in ``vector_index.py``, this script
creates chunks aligned to legal provisions (sections, rules, sub-rules, forms).
Each chunk carries the provision ``canonical_id`` extracted from the XML
``refersTo`` attribute, plus provision-level metadata (type, number, title).

Output: ``derived/vector/provision_chunks.jsonl``
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# GST corpus targets
# ---------------------------------------------------------------------------

GST_CORPUS_GLOBS = [
    "in/union/acts/cgst-act-2017",
    "in/union/acts/igst-act-2017",
    "in/union/rules/cgst-rules-2017",
    "in/union/forms",
]

TEXT_TAGS = {"num", "heading", "p"}
PROVISION_TAGS = {"section", "article", "chapter", "paragraph", "subrule"}


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) != "property":
            continue
        name = element.attrib.get("name")
        if name:
            props[name] = element.attrib.get("value", "")
    return props


def _first_child_text(element: ET.Element, tag_name: str) -> str:
    for child in element:
        if _local_name(child.tag) == tag_name:
            return _clean_text("".join(child.itertext()))
    return ""


def _element_text(element: ET.Element) -> str:
    """Extract concatenated text from num/heading/p children of an element."""
    lines: list[str] = []
    for child in element.iter():
        if _local_name(child.tag) not in TEXT_TAGS:
            continue
        text = _clean_text("".join(child.itertext()))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _provision_type(canonical_id: str, element_tag: str) -> str:
    """Derive a human-readable provision type from canonical_id or tag."""
    cid_lower = canonical_id.lower()
    if "/section/" in cid_lower:
        return "section"
    if "/rule/" in cid_lower:
        if "/subrule/" in cid_lower:
            return "sub-rule"
        return "rule"
    if "/chapter/" in cid_lower:
        return "chapter"
    if "/forms/" in cid_lower:
        return "form"
    tag = element_tag.lower()
    if tag in PROVISION_TAGS:
        return tag if tag != "article" else "rule"
    return tag or "provision"


def _provision_title(element: ET.Element, document_title: str) -> str:
    heading = _first_child_text(element, "heading")
    if heading:
        return heading
    num = _first_child_text(element, "num")
    if num:
        return f"{document_title} – {num}".strip(" –")
    return document_title


def _split_long_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split a long provision into overlapping sub-chunks.

    Returns a list with at least one entry for non-empty text.
    """
    clean = _clean_text(text)
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]

    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + max_chars)
        if end < len(clean):
            boundary = max(
                clean.rfind(". ", start, end),
                clean.rfind("; ", start, end),
                clean.rfind("\n", start, end),
                clean.rfind(" ", start, end),
            )
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(clean):
            break
        start = max(0, end - overlap)
    return chunks


def _provision_fragment_score(
    text: str,
    number: str,
    canonical_id: str,
) -> tuple[int, int]:
    """Score a fragment to prefer the actual provision body over amendment notes.

    Mirrors the heuristic in ``query._provision_score``: fragments that
    explicitly mention "Section N" / "Rule N" (the provision heading) rank
    higher than amendment footnotes ("Substituted by...", "Inserted by...").
    """
    lowered = text.lower()
    has_explicit_heading = 0
    if number:
        escaped = re.escape(number)
        if re.search(rf"\bsection\s+{escaped}\b", lowered):
            has_explicit_heading = 3
        elif re.search(rf"\brule\s+{escaped}\b", lowered):
            has_explicit_heading = 3
        elif re.search(rf"\bsub-rule\s*\({escaped}\)", lowered):
            has_explicit_heading = 2
    # Penalise pure amendment footnotes (no heading and amendment markers).
    is_amendment_note = any(
        marker in lowered
        for marker in ("substituted for", "substituted by", "inserted by", "omitted by", "inserted vide", "substituted vide")
    )
    if has_explicit_heading == 0 and is_amendment_note:
        has_explicit_heading = -1
    return (has_explicit_heading, len(text))


def _iter_provision_elements(
    root: ET.Element,
) -> Iterable[tuple[ET.Element, str, str]]:
    """Yield (element, canonical_id, eId) for every element with a refersTo."""
    seen_keys: set[tuple[str, str]] = set()
    for element in root.iter():
        canonical_id = element.attrib.get("refersTo")
        if not canonical_id:
            continue
        eId = element.attrib.get("eId", "")
        key = (canonical_id, eId)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        yield element, canonical_id, eId


def build_provision_chunks(
    corpus_dir: Path,
    *,
    corpus_globs: list[str] | None = None,
    max_chars: int = 1520,
    overlap: int = 200,
) -> list[dict[str, Any]]:
    """Build provision-level chunks from GST corpus XML.

    Multiple XML elements sharing the same ``refersTo`` (e.g. a section split
    across source fragments) are merged into a single chunk per canonical_id,
    selecting the fragment with the richest text.
    """
    if corpus_globs is None:
        corpus_globs = GST_CORPUS_GLOBS

    # Gather target XML files.
    xml_files: list[Path] = []
    for pattern in corpus_globs:
        base = corpus_dir / pattern
        if base.is_file() and base.suffix == ".xml":
            xml_files.append(base)
        elif base.is_dir():
            xml_files.extend(sorted(base.rglob("*.xml")))

    # Deduplicate while preserving order.
    seen_files: set[Path] = set()
    unique_files: list[Path] = []
    for path in xml_files:
        resolved = path.resolve()
        if resolved not in seen_files:
            seen_files.add(resolved)
            unique_files.append(path)

    # Provision accumulator: canonical_id -> best fragment data.
    provisions: dict[str, dict[str, Any]] = {}

    for xml_path in unique_files:
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue
        root = tree.getroot()
        props = _properties(root)
        document_id = props.get("canonical_id", "")
        document_title = props.get("title", "")
        document_type = props.get("document_type", "")

        for element, canonical_id, eId in _iter_provision_elements(root):
            text = _element_text(element)
            if not text:
                continue
            element_tag = _local_name(element.tag)
            number = _first_child_text(element, "num")
            title = _first_child_text(element, "heading") or _provision_title(element, document_title)
            provision_type = _provision_type(canonical_id, element_tag)

            existing = provisions.get(canonical_id)
            if existing is not None:
                # Merge: keep the fragment with the best score
                # (provision body preferred over amendment footnotes).
                new_score = _provision_fragment_score(text, number, canonical_id)
                old_score = _provision_fragment_score(existing["text"], existing["number"], canonical_id)
                if new_score > old_score:
                    existing["text"] = text
                    existing["eId"] = eId
                    existing["number"] = number
                    existing["title"] = title
                # Always record document linkage.
                continue

            provisions[canonical_id] = {
                "canonical_id": canonical_id,
                "provision_type": provision_type,
                "text": text,
                "title": title,
                "number": number,
                "eId": eId,
                "document_id": document_id,
                "document_type": document_type,
                "document_title": document_title,
                "path": str(xml_path),
            }

    # Build chunks, splitting very long provisions into sub-chunks that all
    # carry the same canonical_id.
    chunks: list[dict[str, Any]] = []
    for canonical_id in sorted(provisions):
        data = provisions[canonical_id]
        full_text = data["text"]
        sub_texts = _split_long_text(full_text, max_chars, overlap)
        for index, sub_text in enumerate(sub_texts, start=1):
            chunk: dict[str, Any] = {
                "chunk_id": f"{canonical_id}#provision-{index:04d}",
                "canonical_id": canonical_id,
                "provision_type": data["provision_type"],
                "text": sub_text,
                "title": data["title"],
                "number": data["number"],
                "eId": data["eId"],
                "document_id": data["document_id"],
                "document_type": data["document_type"],
                "document_title": data["document_title"],
                "chunk_index": index,
                "path": data["path"],
                "token_estimate": len(re.findall(r"[A-Za-z0-9]+", sub_text)),
            }
            chunks.append(chunk)

    return chunks


def write_provision_chunks(chunks: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n" for chunk in chunks),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="corpus", help="Corpus root directory")
    parser.add_argument(
        "--output",
        default="derived/vector/provision_chunks.jsonl",
        help="Output JSONL path",
    )
    parser.add_argument("--max-chars", type=int, default=1520, help="Max chars per sub-chunk for long provisions")
    parser.add_argument("--overlap", type=int, default=200, help="Overlap chars for sub-chunks")
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    if not corpus_dir.exists():
        raise SystemExit(f"Corpus directory not found: {corpus_dir}")

    chunks = build_provision_chunks(
        corpus_dir,
        max_chars=args.max_chars,
        overlap=args.overlap,
    )
    output_path = Path(args.output)
    write_provision_chunks(chunks, output_path)

    # Summary by provision type
    type_counts: dict[str, int] = {}
    for chunk in chunks:
        ptype = chunk["provision_type"]
        type_counts[ptype] = type_counts.get(ptype, 0) + 1

    print(f"provision_chunks={len(chunks)} output={output_path}", flush=True)
    for ptype, count in sorted(type_counts.items()):
        print(f"  {ptype}: {count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
