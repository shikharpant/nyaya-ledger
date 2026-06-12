#!/usr/bin/env python3
"""Insert missing act sections from CBIC data into corpus XML.

Reads the CBIC JSON for each act, finds sections that exist in CBIC data
but are missing from the corpus XML, and inserts them at the correct position.

Usage:
    python3 scripts/insert_missing_act_sections.py --dry-run
    python3 scripts/insert_missing_act_sections.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CBIC_DIR = REPO_ROOT / "data/Law/cbic_tax_portal/acts"
CORPUS_DIR = REPO_ROOT / "corpus/in/union/acts"

ACT_MAP = {
    "cgst-act-2017": "central-goods-and-services-tax-act-2017.json",
    "igst-act-2017": "integrated-goods-and-services-tax-act-2017.json",
    "customs-act-1962": "customs-act-1962.json",
    "customs-tariff-act-1975": "customs-tariff-act-1975.json",
    "central-excise-act-1944": "central-excise-act-1944.json",
}

INSERT_AFTER = {
    "8b": "8",
    "9a": "9",
    "18a": "18",
    "25": "24",
    "31a": "31",
    "52": "51",
    "53": "52",
    "54": "53",
    "65a": "65",
    "68": "67",
    "83": "82",
    "85": "84",
    "98": "97",
    "99": "98",
    "103": "102",
    "108": "107",
    "111": "110",
    "118": "117",
    "125": "124",
    "127a": "127",
    "129": "128",
    "135": "134",
    "136": "135",
    "136": "135",
    "140": "139",
    "147": "146",
    "159": "158",
    "174": "173",
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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_after_section(xml_text: str, act_slug: str, after_sec: str) -> int | None:
    pattern = f'refersTo="/in/union/acts/{act_slug}/section/{after_sec}"'
    pos = xml_text.find(pattern)
    if pos < 0:
        pattern_ci = f'refersTo="/in/union/acts/{act_slug}/section/{after_sec.lower()}"'
        pos = xml_text.find(pattern_ci)
    if pos < 0:
        return None
    close_pos = xml_text.find("</section>", pos)
    if close_pos < 0:
        return None
    next_line = xml_text.find("\n", close_pos) + 1
    return next_line


def _build_section_xml(act_slug: str, sec_no: str, sec_name: str, raw_text: str) -> str:
    clean = _clean_html(raw_text)
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    filtered = []
    for line in lines:
        if re.match(rf"^Section\s+{re.escape(sec_no)}[\s.]", line, re.I):
            continue
        filtered.append(line)
    body = "\n".join(filtered)
    display = f"* Section {sec_no}. {_esc(sec_name)}.-\n{_esc(body)}"
    start = 0
    end = len(display)
    source_hash = _sha256(display)

    xml_parts = [
        f'      <section eId="section_{sec_no.lower()}" '
        f'refersTo="/in/union/acts/{act_slug}/section/{sec_no.lower()}" '
        f'sourceStart="{start}" sourceEnd="{end}" '
        f'sourceHash="{source_hash}" sourceNodeType="section" sourceConfidence="0.9">',
        f"        <num>{sec_no}</num>",
        f"        <content>",
        f"          <p>{display}</p>",
    ]

    for pi, line in enumerate(filtered, 1):
        p_hash = _sha256(line)
        xml_parts.append(
            f'          <paragraph eId="section_{sec_no.lower()}__para_{pi}" '
            f'sourceStart="0" sourceEnd="{len(line)}" '
            f'sourceHash="{p_hash}" sourceNodeType="paragraph">'
            f"\n            <content>"
            f"\n              <p>{_esc(line)}</p>"
            f"\n            </content>"
            f"\n          </paragraph>"
        )

    xml_parts.extend(["        </content>", "      </section>"])
    return "\n".join(xml_parts) + "\n"


def _get_corpus_sections(xml_text: str, act_slug: str) -> set[str]:
    pattern = rf'refersTo="/in/union/acts/{re.escape(act_slug)}/section/([^"]+)"'
    return set(re.findall(pattern, xml_text))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--act", help="Only process this specific act slug")
    args = parser.parse_args()

    total_inserted = 0

    for act_slug, json_file in sorted(ACT_MAP.items()):
        if args.act and act_slug != args.act:
            continue

        json_path = CBIC_DIR / json_file
        corpus_path = CORPUS_DIR / act_slug / "act.xml"
        if not json_path.exists() or not corpus_path.exists():
            print(f"SKIP {act_slug}: missing files")
            continue

        cbic_data = json.loads(json_path.read_text(encoding="utf-8"))
        cbic_sections = {}
        for s in cbic_data.get("sections", []):
            no = s.get("sectionNo", "").replace("Section ", "").strip()
            cbic_sections[no.upper()] = {**s, "_clean_no": no}

        xml_text = corpus_path.read_text(encoding="utf-8")
        existing = _get_corpus_sections(xml_text, act_slug)
        existing_upper = {e.upper() for e in existing}

        to_insert = []
        for sec_key, sec_data in sorted(cbic_sections.items(), key=lambda x: x[1]["_clean_no"]):
            clean_no = sec_data["_clean_no"]
            if clean_no.upper() in existing_upper:
                continue
            after_sec = INSERT_AFTER.get(clean_no.lower())
            if after_sec is None:
                numeric_match = re.match(r"(\d+)", clean_no)
                if numeric_match:
                    after_sec = str(int(numeric_match.group(1)) - 1)
            if after_sec is None:
                continue
            insert_pos = _find_after_section(xml_text, act_slug, after_sec)
            if insert_pos is None:
                print(f"  SKIP {act_slug}/{clean_no}: section {after_sec} not found in corpus")
                continue
            to_insert.append((insert_pos, clean_no, sec_data, after_sec))

        if not to_insert:
            print(f"{act_slug}: nothing to insert")
            continue

        for insert_pos, clean_no, sec_data, after_sec in sorted(to_insert, key=lambda x: -x[0]):
            sec_name = sec_data.get("sectionName", "")
            raw_text = sec_data.get("contentText") or sec_data.get("contentHtml") or ""
            new_xml = _build_section_xml(act_slug, clean_no, sec_name, raw_text)

            if args.dry_run:
                print(f"Would insert {act_slug}/{clean_no} after section {after_sec}")
            else:
                xml_text = xml_text[:insert_pos] + new_xml + "\n" + xml_text[insert_pos:]
                print(f"Inserted {act_slug}/{clean_no} after section {after_sec}")
            total_inserted += 1

        if not args.dry_run and to_insert:
            corpus_path.write_text(xml_text, encoding="utf-8")

    print(f"\nTotal inserted: {total_inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
