"""Split and ingest appendices and forms from Income-tax Rules, 2026 PDF.

Reads the extracted text from data/Law/base_laws/income_tax_rules_2026.txt,
identifies Appendix I, II, III boundaries and individual Form boundaries within
Appendix III, and ingests each as a separate source archive + corpus XML.

Usage:
    python3 scripts/split_ingest_it_rules_forms.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEXT_FILE = REPO_ROOT / "data" / "Law" / "base_laws" / "income_tax_rules_2026.txt"
JSON_FILE = REPO_ROOT / "data" / "Law" / "base_laws" / "income_tax_rules_2026.json"

RULES_CANONICAL = "/in/union/rules/income-tax-rules-2026"
PARENT_SOURCE_URL = "https://www.incometaxindia.gov.in/w/notification-no.-22/2026-f.-no.-370142/41/2025-tpl-/-g.s.r.-198-e-"
PUBLICATION_DATE = "2026-03-20"
EFFECTIVE_DATE = "2026-04-01"
ISSUING_AUTHORITY = "/in/authority/cbdt"

IT_ACT_1961 = "/in/union/acts/income-tax-act-1961"
IT_ACT_2025 = "/in/union/acts/income-tax-act-2025"
IT_RULES_2026 = RULES_CANONICAL


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _esc_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_eid(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", label.lower()).strip("_")


def find_appendix_boundaries(text: str) -> dict:
    app1 = text.find("APPENDIX I\n")
    app2 = text.find("APPENDIX II\n")
    app3 = text.find("APPENDIX III\n")
    return {"I": app1, "II": app2, "III": app3}


def find_form_boundaries(text: str, app3_start: int) -> list[dict]:
    pat = re.compile(
        r"(?:FORM\s+NO\.\s*(\d[\d\s]*\d)|FORM\s+No\.\s*(\d+))"
        r"\s*\n"
        r"(?:\[([^\]]+)\])?",
        re.IGNORECASE,
    )
    forms = []
    seen_positions = set()
    for m in pat.finditer(text):
        pos = m.start()
        if pos <= app3_start:
            continue
        if pos in seen_positions:
            continue
        seen_positions.add(pos)

        raw_num = (m.group(1) or m.group(2) or "").replace(" ", "").strip()
        if not raw_num.isdigit():
            continue
        form_no = int(raw_num)

        rule_ref = (m.group(3) or "").strip()
        context_after = text[m.end() : m.end() + 300]
        title_match = re.search(r"\n([^\n]{10,100})\n", context_after)
        title = title_match.group(1).strip() if title_match else f"Form No. {form_no}"

        forms.append(
            {
                "form_no": form_no,
                "rule_ref": rule_ref,
                "position": pos,
                "header_end": m.end(),
                "title": title,
            }
        )

    deduped = {}
    for f in forms:
        if f["form_no"] not in deduped:
            deduped[f["form_no"]] = f
    return sorted(deduped.values(), key=lambda x: x["form_no"])


def find_references_in_text(text: str) -> list[dict]:
    refs = []
    seen = set()

    act_map = {
        "representation of the people act": "/in/union/acts/representation-of-the-people-act-1951",
        "narcotic drugs and psychotropic substances act": "/in/union/acts/narcotic-drugs-and-psychotropic-substances-act-1985",
        "national housing bank act": "/in/union/acts/national-housing-bank-act-1987",
        "companies act, 1956": "/in/union/acts/companies-act-1956-repealed",
        "companies act 1956": "/in/union/acts/companies-act-1956-repealed",
        "the companies act, 1956": "/in/union/acts/companies-act-1956-repealed",
        "companies act": "/in/union/acts/companies-act-2013",
        "income-tax act, 1961": IT_ACT_1961,
        "income-tax act 1961": IT_ACT_1961,
        "income-tax act,1961": IT_ACT_1961,
        "income-tax act": IT_ACT_1961,
        "income tax act": IT_ACT_1961,
        "income-tax act, 2025": IT_ACT_2025,
        "income-tax act 2025": IT_ACT_2025,
        "income-tax act,2025": IT_ACT_2025,
    }

    for m in re.finditer(r"section\s+(\d+[A-Za-z]*)\b", text, re.IGNORECASE):
        matched = m.group(0)
        start_pos = max(0, m.start() - 400)
        before = text[start_pos : m.start()].lower()
        after = text[m.end() : min(len(text), m.end() + 180)].lower()

        target_act = IT_ACT_1961
        for act_name, act_id in act_map.items():
            if re.search(rf"^\W*of\s+(?:the\s+)?{re.escape(act_name)}", after):
                target_act = act_id
                break
        else:
            matches = [
                (before.rfind(act_name), act_id)
                for act_name, act_id in act_map.items()
                if act_name in before
            ]
            if matches:
                target_act = max(matches, key=lambda item: item[0])[1]
        if target_act is None:
            continue

        snum_clean = re.sub(r"[^0-9A-Za-z]", "", m.group(1)).lower()
        ref_id = f"{target_act}/section/{snum_clean}"
        if ref_id not in seen:
            seen.add(ref_id)
            refs.append({"target": ref_id, "text": matched, "kind": "act_section"})

    for m in re.finditer(r"\brule\s+(\d+[A-Za-z]*)\b", text, re.IGNORECASE):
        matched = m.group(0)
        rnum_clean = re.sub(r"[^0-9A-Za-z]", "", m.group(1)).lower()
        ref_id = f"{IT_RULES_2026}/rule/{rnum_clean}"
        if ref_id not in seen:
            seen.add(ref_id)
            refs.append({"target": ref_id, "text": matched, "kind": "rule"})

    return refs


def build_form_source_text(form_text: str, form_no: int, rule_ref: str) -> str:
    lines = [f"Form No. {form_no}"]
    if rule_ref:
        lines.append(f"[{rule_ref}]")
    lines.append("")
    lines.append(form_text.strip())
    lines.append("")
    return "\n".join(lines)


def render_form_xml(
    form_no: int,
    form_text: str,
    source_text: str,
    rule_ref: str,
    source_sha256: str,
) -> str:
    canonical_id = f"/in/union/forms/it-rules-2026-form-{form_no}"
    title = f"Form No. {form_no}"
    if rule_ref:
        title += f" [{rule_ref}]"
    title += f" - Income-tax Rules, 2026"

    xml = []
    xml.append("<?xml version='1.0' encoding='utf-8'?>")
    xml.append("<akomaNtoso>")
    xml.append(f'  <doc name="it_rules_2026_form_{form_no}">')
    xml.append("    <meta>")
    xml.append('      <identification source="#git-for-law">')
    xml.append("        <FRBRWork>")
    xml.append(f'          <FRBRthis value="{canonical_id}"/>')
    xml.append(f'          <FRBRuri value="{canonical_id}"/>')
    xml.append(f'          <FRBRdate date="{EFFECTIVE_DATE}" name="effective"/>')
    xml.append(f'          <FRBRdate date="{PUBLICATION_DATE}" name="publication"/>')
    xml.append(f'          <FRBRauthor href="{ISSUING_AUTHORITY}"/>')
    xml.append('          <FRBRcountry value="in"/>')
    xml.append("        </FRBRWork>")
    xml.append("      </identification>")
    xml.append('      <proprietary source="#git-for-law">')
    xml.append(f'        <property name="canonical_id" value="{canonical_id}"/>')
    xml.append('        <property name="document_type" value="form"/>')
    xml.append('        <property name="jurisdiction" value="IN-UNION"/>')
    xml.append('        <property name="language" value="eng"/>')
    xml.append(
        '        <property name="parser_version" value="it-rules-2026-forms-v1"/>'
    )
    xml.append('        <property name="review_status" value="parsed"/>')
    xml.append(f'        <property name="source_sha256" value="{source_sha256}"/>')
    xml.append('        <property name="source_type" value="source-archive"/>')
    xml.append(f'        <property name="title" value="{_esc_xml(title)}"/>')
    xml.append(
        f'        <property name="source_url" value="{_esc_xml(PARENT_SOURCE_URL)}"/>'
    )
    xml.append(
        f'        <property name="publication_date" value="{PUBLICATION_DATE}"/>'
    )
    xml.append(f'        <property name="effective_from" value="{EFFECTIVE_DATE}"/>')
    xml.append(
        f'        <property name="issuing_authority" value="{ISSUING_AUTHORITY}"/>'
    )
    xml.append(f'        <property name="parent_rules" value="{RULES_CANONICAL}"/>')
    xml.append(f'        <property name="form_number" value="{form_no}"/>')
    if rule_ref:
        xml.append(f'        <property name="see_rule" value="{_esc_xml(rule_ref)}"/>')
    xml.append("      </proprietary>")
    xml.append("    </meta>")
    xml.append("    <mainBody>")

    para_splits = re.split(r"\n\s*\n", form_text.strip())
    for pi, para in enumerate(para_splits):
        para = para.strip()
        if not para:
            continue
        p_start = source_text.find(para)
        if p_start < 0:
            continue
        p_end = p_start + len(para)
        p_hash = _sha256(source_text[p_start:p_end])
        para_eid = f"form_{form_no}__para_{pi + 1}"

        xml.append(f'      <paragraph eId="{para_eid}"')
        xml.append(f'                 sourceStart="{p_start}"')
        xml.append(f'                 sourceEnd="{p_end}"')
        xml.append(f'                 sourceHash="{p_hash}"')
        xml.append(f'                 sourceNodeType="form">')
        xml.append("        <content>")
        xml.append(f"          <p>{_esc_xml(para)}</p>")

        refs = find_references_in_text(para)
        if refs:
            xml.append("        </content>")
            xml.append("        <references>")
            for ri, ref in enumerate(refs):
                xml.append(f'          <ref eId="{para_eid}__ref_{ri + 1}"')
                xml.append(f'               href="{_esc_xml(ref["target"])}"')
                xml.append(f'               showAs="{_esc_xml(ref["target"])}"')
                xml.append('               type="REFERS_TO"/>')
            xml.append("        </references>")
            xml.append("      </paragraph>")
        else:
            xml.append("        </content>")
            xml.append("      </paragraph>")

    xml.append("    </mainBody>")
    xml.append("  </doc>")
    xml.append("</akomaNtoso>")

    return "\n".join(xml)


def render_appendix_xml(
    appendix_label: str,
    appendix_text: str,
    source_text: str,
    source_sha256: str,
) -> str:
    canonical_id = (
        f"/in/union/rules/income-tax-rules-2026/appendix-{appendix_label.lower()}"
    )
    title = f"Appendix {appendix_label} - Income-tax Rules, 2026"

    xml = []
    xml.append("<?xml version='1.0' encoding='utf-8'?>")
    xml.append("<akomaNtoso>")
    xml.append(f'  <act name="it_rules_2026_appendix_{appendix_label.lower()}">')
    xml.append("    <meta>")
    xml.append('      <identification source="#git-for-law">')
    xml.append("        <FRBRWork>")
    xml.append(f'          <FRBRthis value="{canonical_id}"/>')
    xml.append(f'          <FRBRuri value="{canonical_id}"/>')
    xml.append(f'          <FRBRdate date="{EFFECTIVE_DATE}" name="effective"/>')
    xml.append(f'          <FRBRdate date="{PUBLICATION_DATE}" name="publication"/>')
    xml.append(f'          <FRBRauthor href="{ISSUING_AUTHORITY}"/>')
    xml.append('          <FRBRcountry value="in"/>')
    xml.append("        </FRBRWork>")
    xml.append("      </identification>")
    xml.append('      <proprietary source="#git-for-law">')
    xml.append(f'        <property name="canonical_id" value="{canonical_id}"/>')
    xml.append('        <property name="document_type" value="appendix"/>')
    xml.append('        <property name="jurisdiction" value="IN-UNION"/>')
    xml.append('        <property name="language" value="eng"/>')
    xml.append(
        '        <property name="parser_version" value="it-rules-2026-forms-v1"/>'
    )
    xml.append('        <property name="review_status" value="parsed"/>')
    xml.append(f'        <property name="source_sha256" value="{source_sha256}"/>')
    xml.append('        <property name="source_type" value="source-archive"/>')
    xml.append(f'        <property name="title" value="{_esc_xml(title)}"/>')
    xml.append(
        f'        <property name="source_url" value="{_esc_xml(PARENT_SOURCE_URL)}"/>'
    )
    xml.append(
        f'        <property name="publication_date" value="{PUBLICATION_DATE}"/>'
    )
    xml.append(f'        <property name="effective_from" value="{EFFECTIVE_DATE}"/>')
    xml.append(
        f'        <property name="issuing_authority" value="{ISSUING_AUTHORITY}"/>'
    )
    xml.append(f'        <property name="parent_rules" value="{RULES_CANONICAL}"/>')
    xml.append("      </proprietary>")
    xml.append("    </meta>")
    xml.append("    <body>")

    para_splits = re.split(r"\n\s*\n", appendix_text.strip())
    for pi, para in enumerate(para_splits):
        para = para.strip()
        if not para:
            continue
        p_start = source_text.find(para)
        if p_start < 0:
            continue
        p_end = p_start + len(para)
        p_hash = _sha256(source_text[p_start:p_end])
        para_eid = f"appendix_{appendix_label.lower()}__para_{pi + 1}"

        xml.append(f'      <paragraph eId="{para_eid}"')
        xml.append(f'                 sourceStart="{p_start}"')
        xml.append(f'                 sourceEnd="{p_end}"')
        xml.append(f'                 sourceHash="{p_hash}"')
        xml.append(f'                 sourceNodeType="paragraph"')
        xml.append(f'                 sourceConfidence="0.95">')
        xml.append("        <content>")
        xml.append(f"          <p>{_esc_xml(para)}</p>")

        refs = find_references_in_text(para)
        if refs:
            xml.append("        </content>")
            xml.append("        <references>")
            for ri, ref in enumerate(refs):
                xml.append(f'          <ref eId="{para_eid}__ref_{ri + 1}"')
                xml.append(f'               href="{_esc_xml(ref["target"])}"')
                xml.append(f'               showAs="{_esc_xml(ref["target"])}"')
                xml.append('               type="REFERS_TO"/>')
            xml.append("        </references>")
            xml.append("      </paragraph>")
        else:
            xml.append("        </content>")
            xml.append("      </paragraph>")

    xml.append("    </body>")
    xml.append("  </act>")
    xml.append("</akomaNtoso>")

    return "\n".join(xml)


def write_source_archive(
    source_dir: Path,
    canonical_id: str,
    document_type: str,
    title: str,
    source_text: str,
    extra_metadata: dict | None = None,
):
    source_dir.mkdir(parents=True, exist_ok=True)
    source_sha256 = _sha256(source_text)

    (source_dir / "source.txt").write_text(source_text, encoding="utf-8")

    special_chars = set(":#[]{}")
    meta_lines = []
    metadata = {
        "canonical_id": canonical_id,
        "document_type": document_type,
        "effective_from": EFFECTIVE_DATE,
        "issuing_authority": ISSUING_AUTHORITY,
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "publication_date": PUBLICATION_DATE,
        "source_file": "source.txt",
        "source_sha256": source_sha256,
        "source_type": "source-archive",
        "source_url": PARENT_SOURCE_URL,
        "title": title,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    for k, v in sorted(metadata.items()):
        if isinstance(v, str) and any(c in v for c in special_chars):
            meta_lines.append(f"{k}: {json.dumps(v)}")
        else:
            meta_lines.append(f"{k}: {v}")
    (source_dir / "metadata.yaml").write_text(
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
    (source_dir / "extracted_text.json").write_text(
        json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    nodes = []
    para_splits = re.split(r"\n\s*\n", source_text.strip())
    search_from = 0
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
        search_from = p_end

    references = []
    for node in nodes:
        para_text = source_text[node["start"] : node["end"]]
        refs = find_references_in_text(para_text)
        for ref in refs:
            ref_pos = source_text.find(ref["text"], node["start"])
            if ref_pos < 0 or ref_pos >= node["end"]:
                ref_pos = node["start"]
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

    structure = {
        "profile": "git-for-law-structure-v1",
        "mode": "it-rules-2026-forms",
        "nodes": nodes,
        "references": references,
    }
    (source_dir / "structure.json").write_text(
        json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return source_sha256


def main() -> None:
    if not TEXT_FILE.exists():
        print(f"ERROR: {TEXT_FILE} not found.")
        sys.exit(1)

    with open(TEXT_FILE, "r", encoding="utf-8") as f:
        full_text = f.read()

    boundaries = find_appendix_boundaries(full_text)
    print("Appendix boundaries:")
    for label, pos in sorted(boundaries.items()):
        print(f"  Appendix {label}: position {pos}")

    forms = find_form_boundaries(full_text, boundaries["III"])
    print(f"\nFound {len(forms)} unique forms in Appendix III")
    print(
        f"  Form numbers: {min(f['form_no'] for f in forms)} to {max(f['form_no'] for f in forms)}"
    )

    total_xml = 0
    total_refs = 0

    # --- Appendix I ---
    app1_start = boundaries["I"]
    app1_end = boundaries["II"] if boundaries["II"] > 0 else boundaries["III"]
    if app1_start >= 0 and app1_end > app1_start:
        app1_text = full_text[app1_start:app1_end].strip()
        source_dir = (
            REPO_ROOT
            / "sources"
            / "in"
            / "union"
            / "rules"
            / "income-tax-rules-2026"
            / "appendix-i"
        )
        corpus_file = (
            REPO_ROOT
            / "corpus"
            / "in"
            / "union"
            / "rules"
            / "income-tax-rules-2026"
            / "appendix-i.xml"
        )
        canonical_id = "/in/union/rules/income-tax-rules-2026/appendix-i"
        title = "Appendix I (See rule 25) - Table of Rates at which Depreciation is Admissible - Income-tax Rules, 2026"

        print(f"\nIngesting Appendix I ({len(app1_text)} chars)...")
        source_text = f"Appendix I\n(See rule 25)\n\n{app1_text}"
        sha = write_source_archive(
            source_dir, canonical_id, "appendix", title, source_text
        )
        xml = render_appendix_xml("I", app1_text, source_text, sha)
        corpus_file.parent.mkdir(parents=True, exist_ok=True)
        corpus_file.write_text(xml, encoding="utf-8")
        refs = xml.count("<ref eId=")
        print(f"  Appendix I: {len(app1_text)} chars, {refs} refs -> {corpus_file}")
        total_xml += 1
        total_refs += refs

    # --- Appendix II ---
    app2_start = boundaries["II"]
    app2_end = boundaries["III"]
    if app2_start >= 0 and app2_end > app2_start:
        app2_text = full_text[app2_start:app2_end].strip()
        source_dir = (
            REPO_ROOT
            / "sources"
            / "in"
            / "union"
            / "rules"
            / "income-tax-rules-2026"
            / "appendix-ii"
        )
        corpus_file = (
            REPO_ROOT
            / "corpus"
            / "in"
            / "union"
            / "rules"
            / "income-tax-rules-2026"
            / "appendix-ii.xml"
        )
        canonical_id = "/in/union/rules/income-tax-rules-2026/appendix-ii"
        title = "Appendix II (See rule 25) - Table of Rates at which Depreciation is Admissible - Income-tax Rules, 2026"

        print(f"\nIngesting Appendix II ({len(app2_text)} chars)...")
        source_text = f"Appendix II\n(See rule 25)\n\n{app2_text}"
        sha = write_source_archive(
            source_dir, canonical_id, "appendix", title, source_text
        )
        xml = render_appendix_xml("II", app2_text, source_text, sha)
        corpus_file.parent.mkdir(parents=True, exist_ok=True)
        corpus_file.write_text(xml, encoding="utf-8")
        refs = xml.count("<ref eId=")
        print(f"  Appendix II: {len(app2_text)} chars, {refs} refs -> {corpus_file}")
        total_xml += 1
        total_refs += refs

    # --- Forms from Appendix III ---
    text_end = len(full_text)
    for i, form in enumerate(forms):
        form_start = form["position"]
        next_start = forms[i + 1]["position"] if i + 1 < len(forms) else text_end
        form_text = full_text[form_start:next_start].strip()

        form_no = form["form_no"]
        rule_ref = form["rule_ref"]
        padded = str(form_no).zfill(3)

        source_dir = (
            REPO_ROOT
            / "sources"
            / "in"
            / "union"
            / "forms"
            / f"it-rules-2026-form-{form_no}"
        )
        corpus_file = (
            REPO_ROOT
            / "corpus"
            / "in"
            / "union"
            / "forms"
            / f"it-rules-2026-form-{form_no}"
            / "form.xml"
        )
        canonical_id = f"/in/union/forms/it-rules-2026-form-{form_no}"

        rule_display = f" [See rule {rule_ref}]" if rule_ref else ""
        title = f"Form No. {form_no}{rule_display} - Income-tax Rules, 2026"

        source_text = build_form_source_text(form_text, form_no, rule_ref)
        sha = write_source_archive(
            source_dir,
            canonical_id,
            "form",
            title,
            source_text,
            {"parent_rules": RULES_CANONICAL, "form_number": form_no},
        )
        xml = render_form_xml(form_no, form_text, source_text, rule_ref, sha)
        corpus_file.parent.mkdir(parents=True, exist_ok=True)
        corpus_file.write_text(xml, encoding="utf-8")
        refs = xml.count("<ref eId=")
        total_xml += 1
        total_refs += refs

        if i < 5 or i >= len(forms) - 3 or (i + 1) % 50 == 0:
            print(
                f"  Form {form_no:>3} ({len(form_text):>6} chars, {refs:>2} refs) -> {corpus_file.name}"
            )

    print(
        f"\nDone! {total_xml} documents ({len(forms)} forms + 2 appendices), {total_refs} total references."
    )


if __name__ == "__main__":
    main()
