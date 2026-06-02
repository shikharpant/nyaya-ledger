"""Ingest Income-tax Rules, 2026 from extracted JSON into Nyaya Ledger corpus.

Usage:
    python3 scripts/ingest_it_rules_2026.py

This script converts the extracted rules JSON into:
  - A single consolidated source text file
  - A source archive with metadata, extracted_text.json, structure.json
  - Canonical Akoma Ntoso XML in corpus/
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "Law" / "base_laws" / "income_tax_rules_2026.json"
SOURCE_DIR = REPO_ROOT / "sources" / "in" / "union" / "rules" / "income-tax-rules-2026"
CORPUS_FILE = (
    REPO_ROOT
    / "corpus"
    / "in"
    / "union"
    / "rules"
    / "income-tax-rules-2026"
    / "rules.xml"
)

CANONICAL_ID = "/in/union/rules/income-tax-rules-2026"
DOCUMENT_TYPE = "rules"
TITLE = "Income-tax Rules, 2026"
EFFECTIVE_DATE = "2026-04-01"
PUBLICATION_DATE = "2026-03-20"
ISSUING_AUTHORITY = "/in/authority/cbdt"
SOURCE_URL = "https://www.incometaxindia.gov.in/w/notification-no.-22/2026-f.-no.-370142/41/2025-tpl-/-g.s.r.-198-e-"

IT_ACT_1961 = "/in/union/acts/income-tax-act-1961"
IT_ACT_2025 = "/in/union/acts/income-tax-act-2025"
IT_RULES_2026 = CANONICAL_ID

ACT_MAP = {
    "income-tax act, 1961": IT_ACT_1961,
    "income-tax act 1961": IT_ACT_1961,
    "income-tax act,1961": IT_ACT_1961,
    "income-tax act": IT_ACT_1961,
    "income tax act": IT_ACT_1961,
    "income-tax act, 2025": IT_ACT_2025,
    "income-tax act 2025": IT_ACT_2025,
    "income-tax act,2025": IT_ACT_2025,
    "cgst act": "/in/union/acts/cgst-act-2017",
    "central goods and services tax act": "/in/union/acts/cgst-act-2017",
    "igst act": "/in/union/acts/igst-act-2017",
    "integrated goods and services tax act": "/in/union/acts/igst-act-2017",
    "finance act": None,
    "companies act": "/in/union/acts/companies-act-2013",
    "customs act": "/in/union/acts/customs-act-1962",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_eid(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", label.lower()).strip("_")


def _rule_id(label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", str(label)).lower()
    return f"{CANONICAL_ID}/rule/{clean}"


def _esc_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_source_text(rules: list[dict], meta: dict) -> str:
    lines = [TITLE, f"[{meta.get('notification', '')}]", ""]

    for rule in rules:
        num = rule.get("rule_number", "")
        desc = rule.get("description", "")
        full_text = rule.get("full_text", "")

        heading = f"* Rule {num}."
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


def find_references(text: str, rule_label: str) -> list[dict]:
    refs = []
    seen = set()

    for m in re.finditer(r"section\s+(\d+[A-Za-z]*)\b", text, re.IGNORECASE):
        matched = m.group(0)
        start_pos = max(0, m.start() - 400)
        context = text[start_pos : m.start()].lower()

        target_act = IT_ACT_1961
        for act_name, act_id in ACT_MAP.items():
            if act_name in context:
                if act_id is not None:
                    target_act = act_id
                break

        snum_clean = re.sub(r"[^0-9A-Za-z]", "", m.group(1)).lower()
        ref_id = f"{target_act}/section/{snum_clean}"
        if ref_id not in seen:
            seen.add(ref_id)
            refs.append({"target": ref_id, "text": matched, "kind": "act_section"})

    for m in re.finditer(r"\brule\s+(\d+[A-Za-z]*)\b", text, re.IGNORECASE):
        matched = m.group(0)
        rnum_clean = re.sub(r"[^0-9A-Za-z]", "", m.group(1)).lower()
        ref_id = f"{IT_RULES_2026}/rule/{rnum_clean}"
        own_id = _rule_id(rule_label)
        if ref_id != own_id and ref_id not in seen:
            seen.add(ref_id)
            refs.append({"target": ref_id, "text": matched, "kind": "rule"})

    return refs


def parse_structure(source_text: str, rules: list[dict]) -> dict:
    nodes = []
    references = []

    for i, rule in enumerate(rules):
        label = str(rule.get("rule_number", str(i + 1)))
        desc = rule.get("description", "")
        full_text = rule.get("full_text", "")

        marker = f"* Rule {label}."
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
                "type": "rule",
                "label": label,
                "start": sec_start,
                "end": sec_end,
                "text_hash": _sha256(source_text[sec_start:sec_end]),
                "confidence": 0.95,
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
                        "confidence": 0.95,
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
        "mode": "it-rules-2026-pdf",
        "nodes": nodes,
        "references": references,
    }


def render_rules_xml(source_text: str, rules: list[dict], source_sha256: str) -> str:
    xml = []
    xml.append("<?xml version='1.0' encoding='utf-8'?>")
    xml.append("<akomaNtoso>")
    xml.append('  <act name="income_tax_rules_2026">')
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
    xml.append('        <property name="parser_version" value="it-rules-2026-pdf-v1"/>')
    xml.append('        <property name="review_status" value="parsed"/>')
    xml.append(f'        <property name="source_sha256" value="{source_sha256}"/>')
    xml.append('        <property name="source_type" value="source-archive"/>')
    xml.append(f'        <property name="title" value="{_esc_xml(TITLE)}"/>')
    xml.append(f'        <property name="source_url" value="{_esc_xml(SOURCE_URL)}"/>')
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

    for i, rule in enumerate(rules):
        label = str(rule.get("rule_number", str(i + 1)))
        desc = rule.get("description", "")
        full_text = rule.get("full_text", "")

        marker = f"* Rule {label}."
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

        eid_base = _safe_eid(f"rule_{label}")
        count = eid_counts.get(eid_base, 0)
        eid_counts[eid_base] = count + 1
        eid = eid_base if count == 0 else f"{eid_base}_{count + 1}"
        refers_to = _rule_id(label)

        xml.append(f'      <section eId="{eid}"')
        xml.append(f'               refersTo="{refers_to}"')
        xml.append(f'               sourceStart="{sec_start}"')
        xml.append(f'               sourceEnd="{sec_end}"')
        xml.append(f'               sourceHash="{sec_hash}"')
        xml.append(f'               sourceNodeType="rule"')
        xml.append(f'               sourceConfidence="0.95">')
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

    rules = data.get("rules", [])
    if not rules:
        print("ERROR: No rules found in JSON.")
        sys.exit(1)

    print(f"Loaded {len(rules)} rules from extracted data")
    with_text = sum(1 for r in rules if len(r.get("full_text", "")) > 10)
    print(f"  {with_text} with full text, {len(rules) - with_text} description only")

    source_text = build_source_text(rules, data)
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

    structure = parse_structure(source_text, rules)
    (SOURCE_DIR / "structure.json").write_text(
        json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote source archive: {SOURCE_DIR}")
    print(
        f"  nodes: {len(structure['nodes'])}, references: {len(structure['references'])}"
    )

    xml_content = render_rules_xml(source_text, rules, source_sha256)
    CORPUS_FILE.write_text(xml_content, encoding="utf-8")
    print(f"Wrote corpus XML: {CORPUS_FILE}")

    total_refs = xml_content.count("<ref eId=")
    print(f"\nDone! {len(rules)} rules, {total_refs} references.")


if __name__ == "__main__":
    main()
