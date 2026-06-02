"""Ingest Income-tax Act, 2025 from scraped JSON into Nyaya Ledger corpus.

Usage:
    1. Run scripts/scrape_it_rules_2026_api.js (or gentle version) in browser
    2. Save JSON to data/Law/base_laws/income_tax_rules_2026.json
    3. Run: python3 scripts/ingest_it_act_2025.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "Law" / "base_laws" / "income_tax_act_2025.json"
SOURCE_DIR = REPO_ROOT / "sources" / "in" / "union" / "acts" / "income-tax-act-2025"
CORPUS_FILE = (
    REPO_ROOT / "corpus" / "in" / "union" / "acts" / "income-tax-act-2025" / "act.xml"
)

CANONICAL_ID = "/in/union/acts/income-tax-act-2025"
DOCUMENT_TYPE = "act"
TITLE = "The Income-tax Act, 2025"
EFFECTIVE_DATE = "2025-04-01"
PUBLICATION_DATE = "2025-03-20"
ISSUING_AUTHORITY = "/in/authority/unknown"
SOURCE_URL = "https://www.incometaxindia.gov.in/income-tax-rule-2026"

KNOWN_ACT_MAP = {
    "income-tax act, 1961": "/in/union/acts/income-tax-act-1961",
    "income-tax act 1961": "/in/union/acts/income-tax-act-1961",
    "income-tax act, 2025": CANONICAL_ID,
    "income-tax act 2025": CANONICAL_ID,
    "cgst act": "/in/union/acts/cgst-act-2017",
    "central goods and services tax act": "/in/union/acts/cgst-act-2017",
    "igst act": "/in/union/acts/igst-act-2017",
    "integrated goods and services tax act": "/in/union/acts/igst-act-2017",
    "finance act": None,
    "companies act": "/in/union/acts/companies-act-2013",
    "customs act": "/in/union/acts/customs-act-1962",
    "customs tariff act": "/in/union/acts/customs-tariff-act-1975",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_eid(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", label.lower()).strip("_")


def _section_id(label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", label).lower()
    return f"{CANONICAL_ID}/section/{clean}"


def _esc_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_source_text(sections: list[dict]) -> str:
    lines = [TITLE, "[Act No. 30 of 2025]", ""]

    for sec in sections:
        num = sec.get("section_number", "")
        desc = sec.get("description", "")
        full_text = sec.get("full_text", "") or sec.get("full_text_plain", "")

        heading = f"* Section {num}."
        if desc:
            heading += f" {desc}."
            if not heading.endswith(".-"):
                heading = heading.rstrip(".") + ".-"
        lines.append(heading)
        lines.append("")

        if full_text:
            lines.append(full_text.strip())
            lines.append("")
        lines.append("")

    return "\n".join(lines)


def find_references(text: str, section_label: str) -> list[dict]:
    refs = []
    seen = set()

    for m in re.finditer(r"section\s+(\d+[A-Za-z]*)\b", text, re.IGNORECASE):
        matched = m.group(0)
        start_pos = max(0, m.start() - 300)
        context = text[start_pos : m.start()].lower()

        target_act = CANONICAL_ID
        for act_name, act_id in KNOWN_ACT_MAP.items():
            if act_name in context:
                if act_id is not None:
                    target_act = act_id
                break

        snum_clean = re.sub(r"[^0-9A-Za-z]", "", m.group(1)).lower()
        ref_id = f"{target_act}/section/{snum_clean}"
        if ref_id != _section_id(section_label) and ref_id not in seen:
            seen.add(ref_id)
            refs.append(
                {
                    "target": ref_id,
                    "text": matched,
                    "kind": "act_section",
                }
            )

    return refs


def parse_structure(source_text: str, sections: list[dict]) -> dict:
    nodes = []
    references = []

    for i, sec in enumerate(sections):
        label = sec.get("section_number", str(i + 1))
        desc = sec.get("description", "")
        full_text = sec.get("full_text", "") or sec.get("full_text_plain", "")

        marker = f"* Section {label}."
        sec_start = source_text.find(marker)
        if sec_start < 0:
            continue

        if desc:
            marker_full = marker + f" {desc}."
            if not marker_full.endswith(".-"):
                marker_full = marker_full.rstrip(".") + ".-"
        else:
            marker_full = marker

        sec_end = sec_start + len(marker_full)
        nodes.append(
            {
                "type": "section",
                "label": label,
                "start": sec_start,
                "end": sec_end,
                "text_hash": _sha256(source_text[sec_start:sec_end]),
                "confidence": 0.9,
            }
        )

        if full_text:
            full_text_stripped = full_text.strip()
            body_start = source_text.find(full_text_stripped, sec_end)
            if body_start < 0:
                continue

            para_splits = re.split(r"\n\s*\n", full_text_stripped)
            search_from = body_start
            for pi, para in enumerate(para_splits):
                para = para.strip()
                if not para:
                    continue
                p_start = source_text.find(para, search_from)
                if p_start < 0:
                    continue
                p_end = p_start + len(para)

                nodes.append(
                    {
                        "type": "paragraph",
                        "label": str(pi + 1),
                        "start": p_start,
                        "end": p_end,
                        "text_hash": _sha256(source_text[p_start:p_end]),
                        "confidence": 0.9,
                    }
                )

                refs_in_para = find_references(para, label)
                for ref in refs_in_para:
                    ref_pos = source_text.find(ref["text"], p_start)
                    if ref_pos < 0 or ref_pos >= p_end:
                        ref_pos = p_start
                    references.append(
                        {
                            "target": ref["target"],
                            "text": ref["text"],
                            "start": ref_pos,
                            "end": ref_pos + len(ref["text"]),
                            "text_hash": _sha256(
                                source_text[ref_pos : ref_pos + len(ref["text"])]
                            ),
                            "kind": ref["kind"],
                        }
                    )

                search_from = p_end

    return {
        "profile": "git-for-law-structure-v1",
        "mode": "it-act-2025-scraper",
        "nodes": nodes,
        "references": references,
    }


def render_act_xml(source_text: str, sections: list[dict], source_sha256: str) -> str:
    xml = []
    xml.append("<?xml version='1.0' encoding='utf-8'?>")
    xml.append("<akomaNtoso>")
    xml.append('  <act name="the_income_tax_act_2025">')
    xml.append("    <meta>")
    xml.append('      <identification source="#git-for-law">')
    xml.append("        <FRBRWork>")
    xml.append(f'          <FRBRthis value="{CANONICAL_ID}"/>')
    xml.append(f'          <FRBRuri value="{CANONICAL_ID}"/>')
    xml.append(f'          <FRBRdate date="{EFFECTIVE_DATE}" name="effective"/>')
    xml.append(f'          <FRBRdate date="{PUBLICATION_DATE}" name="publication"/>')
    xml.append(f'          <FRBRauthor href="{ISSUING_AUTHORITY}"/>')
    xml.append('          <FRBRcountry value="in"/>')
    xml.append("        </FRBRWork>")
    xml.append("      </identification>")
    xml.append('      <proprietary source="#git-for-law">')
    xml.append(f'        <property name="canonical_id" value="{CANONICAL_ID}"/>')
    xml.append(f'        <property name="document_type" value="{DOCUMENT_TYPE}"/>')
    xml.append('        <property name="jurisdiction" value="IN-UNION"/>')
    xml.append('        <property name="language" value="eng"/>')
    xml.append(
        '        <property name="parser_version" value="it-act-2025-scraper-v1"/>'
    )
    xml.append('        <property name="review_status" value="parsed"/>')
    xml.append(f'        <property name="source_sha256" value="{source_sha256}"/>')
    xml.append('        <property name="source_type" value="source-archive"/>')
    xml.append(f'        <property name="title" value="{_esc_xml(TITLE)}"/>')
    xml.append(f'        <property name="source_url" value="{SOURCE_URL}"/>')
    xml.append(
        f'        <property name="publication_date" value="{PUBLICATION_DATE}"/>'
    )
    xml.append(f'        <property name="effective_from" value="{EFFECTIVE_DATE}"/>')
    xml.append(
        f'        <property name="issuing_authority" value="{ISSUING_AUTHORITY}"/>'
    )
    xml.append("      </proprietary>")
    xml.append("    </meta>")
    xml.append("    <body>")

    eid_counts: dict[str, int] = {}

    for i, sec in enumerate(sections):
        label = sec.get("section_number", str(i + 1))
        desc = sec.get("description", "")
        full_text = sec.get("full_text", "") or sec.get("full_text_plain", "")

        marker = f"* Section {label}."
        sec_start = source_text.find(marker)
        if sec_start < 0:
            continue

        if desc:
            marker_full = marker + f" {desc}."
            if not marker_full.endswith(".-"):
                marker_full = marker_full.rstrip(".") + ".-"
        else:
            marker_full = marker

        sec_end = sec_start + len(marker_full)
        sec_hash = _sha256(source_text[sec_start:sec_end])

        eid_base = _safe_eid(f"section_{label}")
        count = eid_counts.get(eid_base, 0)
        eid_counts[eid_base] = count + 1
        eid = eid_base if count == 0 else f"{eid_base}_{count + 1}"
        refers_to = _section_id(label)

        xml.append(f'      <section eId="{eid}"')
        xml.append(f'               refersTo="{refers_to}"')
        xml.append(f'               sourceStart="{sec_start}"')
        xml.append(f'               sourceEnd="{sec_end}"')
        xml.append(f'               sourceHash="{sec_hash}"')
        xml.append(f'               sourceNodeType="section"')
        xml.append(f'               sourceConfidence="0.9">')
        xml.append(f"        <num>{_esc_xml(label)}</num>")
        xml.append("        <content>")
        xml.append(f"          <p>{_esc_xml(marker_full)}</p>")

        if full_text:
            full_text_stripped = full_text.strip()
            body_start = source_text.find(full_text_stripped, sec_end)
            if body_start >= 0:
                para_splits = re.split(r"\n\s*\n", full_text_stripped)
                search_from = body_start
                pi = 0

                for para in para_splits:
                    para = para.strip()
                    if not para:
                        continue
                    p_start = source_text.find(para, search_from)
                    if p_start < 0:
                        continue
                    p_end = p_start + len(para)
                    p_hash = _sha256(source_text[p_start:p_end])
                    para_eid = f"{eid}__para_{pi + 1}"

                    xml.append(f'          <paragraph eId="{para_eid}"')
                    xml.append(f'                     sourceStart="{p_start}"')
                    xml.append(f'                     sourceEnd="{p_end}"')
                    xml.append(f'                     sourceHash="{p_hash}"')
                    xml.append(f'                     sourceNodeType="paragraph">')
                    xml.append("            <content>")
                    xml.append(f"              <p>{_esc_xml(para)}</p>")

                    refs = find_references(para, label)
                    if refs:
                        xml.append("            </content>")
                        xml.append("            <references>")
                        for ri, ref in enumerate(refs):
                            xml.append(
                                f'              <ref eId="{para_eid}__ref_{ri + 1}"'
                            )
                            xml.append(
                                f'                   href="{_esc_xml(ref["target"])}"'
                            )
                            xml.append(
                                f'                   showAs="{_esc_xml(ref["target"])}"'
                            )
                            xml.append('                   type="REFERS_TO"/>')
                        xml.append("            </references>")
                        xml.append("          </paragraph>")
                    else:
                        xml.append("            </content>")
                        xml.append("          </paragraph>")

                    pi += 1
                    search_from = p_end

        xml.append("        </content>")
        xml.append("      </section>")

    xml.append("    </body>")
    xml.append("  </act>")
    xml.append("</akomaNtoso>")

    return "\n".join(xml)


def main() -> None:
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found.")
        sys.exit(1)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    sections = data.get("sections", [])
    if not sections:
        print("ERROR: No sections found in JSON.")
        sys.exit(1)

    print(f"Loaded {len(sections)} sections from scraped data")
    with_text = sum(1 for s in sections if len(s.get("full_text", "")) > 50)
    print(f"  {with_text} with full text, {len(sections) - with_text} description only")

    source_text = build_source_text(sections)
    source_sha256 = _sha256(source_text)
    print(f"Source text: {len(source_text)} chars, SHA-256: {source_sha256[:16]}...")

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    CORPUS_FILE.parent.mkdir(parents=True, exist_ok=True)

    (SOURCE_DIR / "source.txt").write_text(source_text, encoding="utf-8")

    special_chars = set(":#[]{}")
    meta_lines = []
    metadata = {
        "canonical_id": CANONICAL_ID,
        "document_type": DOCUMENT_TYPE,
        "effective_from": EFFECTIVE_DATE,
        "issuing_authority": ISSUING_AUTHORITY,
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "publication_date": PUBLICATION_DATE,
        "source_file": "source.txt",
        "source_sha256": source_sha256,
        "source_type": "source-archive",
        "source_url": SOURCE_URL,
        "title": TITLE,
    }
    for k, v in sorted(metadata.items()):
        if isinstance(v, str) and any(c in v for c in special_chars):
            meta_lines.append(f"{k}: {json.dumps(v)}")
        else:
            meta_lines.append(f"{k}: {v}")
    (SOURCE_DIR / "metadata.yaml").write_text(
        "\n".join(meta_lines) + "\n", encoding="utf-8"
    )

    extracted = {
        "source_file": "source.txt",
        "source_sha256": source_sha256,
        "text": source_text,
        "pages": [
            {"page_number": 1, "start": 0, "end": len(source_text), "text": source_text}
        ],
    }
    (SOURCE_DIR / "extracted_text.json").write_text(
        json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    structure = parse_structure(source_text, sections)
    (SOURCE_DIR / "structure.json").write_text(
        json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote source archive: {SOURCE_DIR}")
    print(
        f"  nodes: {len(structure['nodes'])}, references: {len(structure['references'])}"
    )

    xml_content = render_act_xml(source_text, sections, source_sha256)
    CORPUS_FILE.write_text(xml_content, encoding="utf-8")
    print(f"Wrote corpus XML: {CORPUS_FILE}")

    total_refs = xml_content.count("<ref eId=")
    print(f"\nDone! {len(sections)} sections, {total_refs} references.")
    print("Next: python3 main.py pipeline verify")


if __name__ == "__main__":
    main()
