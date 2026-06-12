#!/usr/bin/env python3
"""Insert missing CGST Act sections from CBIC data into corpus XML.

The CBIC portal has the latest amended version of the CGST Act with sections
added by Finance Act 2024 (74A, 122A, 128A, 148A, 158A). This script extracts
those sections and inserts them into the existing corpus XML at the correct
positions.

Usage:
    python3 scripts/insert_missing_cgst_sections.py --dry-run
    python3 scripts/insert_missing_cgst_sections.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CBIC_JSON = REPO_ROOT / "data/Law/cbic_tax_portal/acts/central-goods-and-services-tax-act-2017.json"
CORPUS_XML = REPO_ROOT / "corpus/in/union/acts/cgst-act-2017/act.xml"
SOURCE_TXT = REPO_ROOT / "sources/in/union/acts/cgst-act-2017/source.txt"

INSERTIONS = {
    "74A": "74",
    "122A": "122",
    "128A": "128",
    "148A": "148",
    "158A": "158",
}


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_section_text(raw: str) -> str:
    text = _clean_html(raw)
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r"^Section\s+\d+A", line, re.I):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_section_xml(section_no: str, section_name: str, section_text: str, source_text: str) -> tuple[str, int, int]:
    display = f"* Section {section_no}. {_esc(section_name)}.-\n{_esc(_clean_section_text(section_text))}"
    start = source_text.find(f"Section {section_no}")
    if start < 0:
        start = len(source_text)
    end = start + len(display)
    source_hash = _sha256(display)

    paragraphs = []
    for line in _clean_section_text(section_text).splitlines():
        line = line.strip()
        if line:
            p_start = display.find(line)
            if p_start < 0:
                p_start = start
            p_end = p_start + len(line)
            paragraphs.append(
                f'          <paragraph eId="section_{section_no.lower()}__para_{len(paragraphs)+1}" '
                f'sourceStart="{p_start}" sourceEnd="{p_end}" '
                f'sourceHash="{_sha256(line)}" sourceNodeType="paragraph">'
                f"\n            <content>"
                f"\n              <p>{_esc(line)}</p>"
                f"\n            </content>"
                f"\n          </paragraph>"
            )

    xml = (
        f'      <section eId="section_{section_no.lower()}" '
        f'refersTo="/in/union/acts/cgst-act-2017/section/{section_no.lower()}" '
        f'sourceStart="{start}" sourceEnd="{end}" '
        f'sourceHash="{source_hash}" sourceNodeType="section" sourceConfidence="0.9">'
        f"\n        <num>{section_no}</num>"
        f"\n        <content>"
        f"\n          <p>{display}</p>"
    )
    for p in paragraphs:
        xml += "\n" + p
    xml += "\n        </content>\n      </section>"
    return xml, start, end


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not CBIC_JSON.exists():
        print(f"Missing: {CBIC_JSON}", file=__import__("sys").stderr)
        return 1

    cbic_data = json.loads(CBIC_JSON.read_text(encoding="utf-8"))
    cbic_sections = {s["sectionNo"].replace("Section ", "").strip(): s for s in cbic_data.get("sections", [])}

    xml_text = CORPUS_XML.read_text(encoding="utf-8")
    lines = xml_text.splitlines(keepends=True)

    inserted = 0
    for new_sec, after_sec in sorted(INSERTIONS.items(), key=lambda x: -int(re.match(r"\d+", x[1]).group())):
        cbic_sec = cbic_sections.get(new_sec)
        if not cbic_sec:
            print(f"SKIP {new_sec}: not in CBIC data")
            continue

        pattern = f'refersTo="/in/union/acts/cgst-act-2017/section/{after_sec}"'
        pos = xml_text.find(pattern)
        if pos < 0:
            print(f"SKIP {new_sec}: section {after_sec} not found in corpus XML")
            continue

        close_pos = xml_text.find("</section>", pos)
        if close_pos < 0:
            print(f"SKIP {new_sec}: could not find closing tag after section {after_sec}")
            continue

        line_no = xml_text[:close_pos].count("\n") + 1
        next_line_start = xml_text.find("\n", close_pos) + 1

        section_text = cbic_sec.get("contentText") or cbic_sec.get("contentHtml") or ""
        section_name = cbic_sec.get("sectionName", "")

        new_xml, _, _ = _build_section_xml(new_sec, section_name, section_text, "")
        new_xml += "\n"

        if args.dry_run:
            print(f"Would insert section {new_sec} after line {line_no} (after section {after_sec})")
            print(f"  Name: {section_name}")
            print(f"  Text length: {len(section_text)}")
        else:
            xml_text = xml_text[:next_line_start] + new_xml + "\n" + xml_text[next_line_start:]
            print(f"Inserted section {new_sec} after section {after_sec}")

        inserted += 1

    if not args.dry_run and inserted > 0:
        CORPUS_XML.write_text(xml_text, encoding="utf-8")
        print(f"\nSaved {inserted} sections to {CORPUS_XML}")

    print(f"Total inserted: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
