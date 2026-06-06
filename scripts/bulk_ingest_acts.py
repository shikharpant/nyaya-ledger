"""Bulk ingest all scraped act JSONs from data/Law/base_laws/ into corpus.

Usage: python3 scripts/bulk_ingest_acts.py [--dry-run]

Skips acts already present in corpus/in/union/acts/<slug>/act.xml.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = REPO_ROOT / "data" / "Law" / "base_laws"
SKIP_FILES = {"residual"}
SKIP_EXISTING = os.environ.get("SKIP_EXISTING", "1") != "0"
FORCE_SLUGS = {
    value.strip()
    for value in os.environ.get("FORCE_SLUGS", "").split(",")
    if value.strip()
}

KNOWN_ACT_MAP = {
    "representation of the people act": "/in/union/acts/representation-of-the-people-act-1951",
    "narcotic drugs and psychotropic substances act": "/in/union/acts/narcotic-drugs-and-psychotropic-substances-act-1985",
    "national housing bank act": "/in/union/acts/national-housing-bank-act-1987",
    "companies act, 1956": "/in/union/acts/companies-act-1956-repealed",
    "companies act 1956": "/in/union/acts/companies-act-1956-repealed",
    "the companies act, 1956": "/in/union/acts/companies-act-1956-repealed",
    "companies act": "/in/union/acts/companies-act-2013",
    "income-tax act, 1961": "/in/union/acts/income-tax-act-1961",
    "income-tax act 1961": "/in/union/acts/income-tax-act-1961",
    "income-tax act, 2025": "/in/union/acts/income-tax-act-2025",
    "income-tax act 2025": "/in/union/acts/income-tax-act-2025",
    "income-tax act": "/in/union/acts/income-tax-act-1961",
    "income tax act": "/in/union/acts/income-tax-act-1961",
    "central goods and services tax act": "/in/union/acts/cgst-act-2017",
    "cgst act": "/in/union/acts/cgst-act-2017",
    "integrated goods and services tax act": "/in/union/acts/igst-act-2017",
    "igst act": "/in/union/acts/igst-act-2017",
    "customs tariff act": "/in/union/acts/customs-tariff-act-1975",
    "customs act": "/in/union/acts/customs-act-1962",
    "central excise act": "/in/union/acts/central-excise-act-1944",
    "finance act": None,
}


def _target_act_from_context(before: str, after: str, fallback: str) -> str | None:
    del before
    for act_name, act_id in sorted(KNOWN_ACT_MAP.items(), key=lambda item: -len(item[0])):
        if re.search(rf"^\W*of\s+(?:the\s+)?{re.escape(act_name)}", after):
            return act_id

    return fallback


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _safe_eid(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", label.lower()).strip("_")


def _esc_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _section_id(canonical_id: str, label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", "", label).lower()
    return f"{canonical_id}/section/{clean}"


def act_metadata_from_json(data: dict) -> dict:
    act_name = data.get("act", "")
    source_url = data.get("source", "")
    page_url = data.get("pageURL", "")
    slug = _slugify(act_name) if act_name else ""

    if not slug:
        return {}

    canonical_id = f"/in/union/acts/{slug}"
    document_type = "act"
    title = act_name

    effective_date = ""
    publication_date = ""
    year_match = re.search(r"(19\d{2}|20\d{2})", act_name)
    if year_match:
        year = year_match.group(1)
        effective_date = f"{year}-04-01"
        publication_date = f"{year}-01-01"

    return {
        "canonical_id": canonical_id,
        "document_type": document_type,
        "title": title,
        "slug": slug,
        "effective_date": effective_date,
        "publication_date": publication_date,
        "source_url": source_url
        or page_url
        or f"https://www.incometaxindia.gov.in/{slug}",
        "issuing_authority": "/in/authority/unknown",
    }


def build_source_text(sections: list[dict], title: str) -> str:
    lines = [title, ""]
    for sec in sections:
        num = sec.get("rule_number", sec.get("section_number", ""))
        desc = sec.get("description", "")
        full_text = sec.get("full_text", "")

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


def find_references(
    text: str,
    section_label: str,
    canonical_id: str,
    available_sections: set[str] | None = None,
) -> list[dict]:
    refs = []
    seen = set()
    for m in re.finditer(r"section\s+(\d+[A-Za-z]*)\b", text, re.IGNORECASE):
        matched = m.group(0)
        start_pos = max(0, m.start() - 400)
        before = text[start_pos : m.start()].lower()
        after = text[m.end() : min(len(text), m.end() + 180)].lower()
        target_act = _target_act_from_context(before, after, canonical_id)
        if target_act is None:
            continue

        snum_clean = re.sub(r"[^0-9A-Za-z]", "", m.group(1)).lower()
        if (
            target_act == canonical_id
            and available_sections is not None
            and snum_clean not in available_sections
        ):
            continue

        ref_id = f"{target_act}/section/{snum_clean}"
        if ref_id != _section_id(canonical_id, section_label) and ref_id not in seen:
            seen.add(ref_id)
            refs.append({"target": ref_id, "text": matched, "kind": "act_section"})
    return refs


def parse_structure(source_text: str, sections: list[dict], canonical_id: str) -> dict:
    nodes = []
    references = []
    available_sections = {
        re.sub(r"[^0-9A-Za-z]", "", str(sec.get("section_number", ""))).lower()
        for sec in sections
        if sec.get("section_number")
    }

    for i, sec in enumerate(sections):
        label = sec.get("rule_number", sec.get("section_number", str(i + 1)))
        desc = sec.get("description", "")
        full_text = sec.get("full_text", "")

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
            ft = full_text.strip()
            body_start = source_text.find(ft, sec_end)
            if body_start < 0:
                continue
            para_splits = re.split(r"\n\s*\n", ft)
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
                for ref in find_references(para, label, canonical_id, available_sections):
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
        "mode": "bulk-scraper",
        "nodes": nodes,
        "references": references,
    }


def render_act_xml(
    source_text: str, sections: list[dict], meta: dict, source_sha256: str
) -> str:
    cid = meta["canonical_id"]
    xml = [
        "<?xml version='1.0' encoding='utf-8'?>",
        "<akomaNtoso>",
        f'  <act name="{_esc_xml(meta["title"].lower().replace(" ", "_"))}">',
        "    <meta>",
        '      <identification source="#git-for-law">',
        "        <FRBRWork>",
        f'          <FRBRthis value="{cid}"/>',
        f'          <FRBRuri value="{cid}"/>',
        f'          <FRBRdate date="{meta["effective_date"]}" name="effective"/>',
        f'          <FRBRdate date="{meta["publication_date"]}" name="publication"/>',
        f'          <FRBRauthor href="{meta["issuing_authority"]}"/>',
        '          <FRBRcountry value="in"/>',
        "        </FRBRWork>",
        "      </identification>",
        '      <proprietary source="#git-for-law">',
        f'        <property name="canonical_id" value="{cid}"/>',
        f'        <property name="document_type" value="{meta["document_type"]}"/>',
        '        <property name="jurisdiction" value="IN-UNION"/>',
        '        <property name="language" value="eng"/>',
        '        <property name="parser_version" value="bulk-scraper-v1"/>',
        '        <property name="review_status" value="parsed"/>',
        f'        <property name="source_sha256" value="{source_sha256}"/>',
        '        <property name="source_type" value="source-archive"/>',
        f'        <property name="title" value="{_esc_xml(meta["title"])}"/>',
        f'        <property name="source_url" value="{_esc_xml(meta["source_url"])}"/>',
        f'        <property name="publication_date" value="{meta["publication_date"]}"/>',
        f'        <property name="effective_from" value="{meta["effective_date"]}"/>',
        f'        <property name="issuing_authority" value="{meta["issuing_authority"]}"/>',
        "      </proprietary>",
        "    </meta>",
        "    <body>",
    ]

    eid_counts: dict[str, int] = {}
    available_sections = {
        re.sub(r"[^0-9A-Za-z]", "", str(sec.get("section_number", ""))).lower()
        for sec in sections
        if sec.get("section_number")
    }
    for i, sec in enumerate(sections):
        label = sec.get("rule_number", sec.get("section_number", str(i + 1)))
        desc = sec.get("description", "")
        full_text = sec.get("full_text", "")

        marker = f"* Section {label}."
        sec_start = source_text.find(marker)
        if sec_start < 0:
            continue
        marker_full = marker + f" {desc}." if desc else marker
        if desc and not marker_full.endswith(".-"):
            marker_full = marker_full.rstrip(".") + ".-"
        sec_end = sec_start + len(marker_full)
        sec_hash = _sha256(source_text[sec_start:sec_end])

        eid_base = _safe_eid(f"section_{label}")
        count = eid_counts.get(eid_base, 0)
        eid_counts[eid_base] = count + 1
        eid = eid_base if count == 0 else f"{eid_base}_{count + 1}"
        refers_to = _section_id(cid, label)

        xml.append(f'      <section eId="{eid}" refersTo="{refers_to}"')
        xml.append(f'               sourceStart="{sec_start}" sourceEnd="{sec_end}"')
        xml.append(f'               sourceHash="{sec_hash}" sourceNodeType="section"')
        xml.append(f'               sourceConfidence="0.9">')
        xml.append(f"        <num>{_esc_xml(label)}</num>")
        xml.append("        <content>")
        xml.append(f"          <p>{_esc_xml(marker_full)}</p>")

        if full_text:
            ft = full_text.strip()
            body_start = source_text.find(ft, sec_end)
            if body_start >= 0:
                para_splits = re.split(r"\n\s*\n", ft)
                sf = body_start
                pi = 0
                for para in para_splits:
                    para = para.strip()
                    if not para:
                        continue
                    ps = source_text.find(para, sf)
                    if ps < 0:
                        continue
                    pe = ps + len(para)
                    ph = _sha256(source_text[ps:pe])
                    peid = f"{eid}__para_{pi + 1}"

                    xml.append(
                        f'          <paragraph eId="{peid}" sourceStart="{ps}" sourceEnd="{pe}"'
                    )
                    xml.append(
                        f'                     sourceHash="{ph}" sourceNodeType="paragraph">'
                    )
                    xml.append("            <content>")
                    xml.append(f"              <p>{_esc_xml(para)}</p>")

                    refs = find_references(para, label, cid, available_sections)
                    if refs:
                        xml.append("            </content>")
                        xml.append("            <references>")
                        for ri, ref in enumerate(refs):
                            xml.append(f'              <ref eId="{peid}__ref_{ri + 1}"')
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
                    sf = pe

        xml.append("        </content>")
        xml.append("      </section>")

    xml.extend(["    </body>", "  </act>", "</akomaNtoso>"])
    return "\n".join(xml)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    json_files = sorted(
        [
            f
            for f in os.listdir(BASE_DIR)
            if f.endswith(".json")
            and not any(s in f for s in SKIP_FILES)
            and not f.startswith(
                "income_tax_act_"
            )  # already ingested via dedicated scripts
        ]
    )

    # Filter out non-act files (PDFs, HTML etc are not JSON)
    json_files = [f for f in json_files if f.endswith(".json")]

    existing = set()
    if SKIP_EXISTING:
        for p in (REPO_ROOT / "corpus" / "in" / "union" / "acts").glob("*/act.xml"):
            existing.add(p.parent.name)

    print(f"Found {len(json_files)} JSON files, {len(existing)} acts already ingested")

    ingested = 0
    skipped = 0
    errors = 0
    total_sections = 0
    total_refs = 0

    for fname in json_files:
        fpath = BASE_DIR / fname
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        sections = data.get("rules", data.get("sections", []))
        if not sections:
            skipped += 1
            continue

        meta = act_metadata_from_json(data)
        if not meta or not meta.get("slug"):
            print(f"  SKIP {fname}: could not determine act metadata")
            skipped += 1
            continue

        slug = meta["slug"]
        if FORCE_SLUGS and slug not in FORCE_SLUGS:
            skipped += 1
            continue
        if slug in existing:
            skipped += 1
            continue

        source_text = build_source_text(sections, meta["title"])
        source_sha256 = _sha256(source_text)

        source_dir = REPO_ROOT / "sources" / "in" / "union" / "acts" / slug
        corpus_file = REPO_ROOT / "corpus" / "in" / "union" / "acts" / slug / "act.xml"

        if dry_run:
            print(
                f"  WOULD ingest {fname} -> {meta['canonical_id']} ({len(sections)} sections)"
            )
            ingested += 1
            continue

        source_dir.mkdir(parents=True, exist_ok=True)
        corpus_file.parent.mkdir(parents=True, exist_ok=True)

        (source_dir / "source.txt").write_text(source_text, encoding="utf-8")

        special_chars = set(":#[]{}")
        meta_lines = []
        metadata = {
            "canonical_id": meta["canonical_id"],
            "document_type": meta["document_type"],
            "effective_from": meta["effective_date"],
            "issuing_authority": meta["issuing_authority"],
            "jurisdiction": "IN-UNION",
            "language": "eng",
            "publication_date": meta["publication_date"],
            "source_file": "source.txt",
            "source_sha256": source_sha256,
            "source_type": "source-archive",
            "source_url": meta["source_url"],
            "title": meta["title"],
        }
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
                {
                    "page_number": 1,
                    "start": 0,
                    "end": len(source_text),
                    "text": source_text,
                }
            ],
        }
        (source_dir / "extracted_text.json").write_text(
            json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        structure = parse_structure(source_text, sections, meta["canonical_id"])
        (source_dir / "structure.json").write_text(
            json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        xml_content = render_act_xml(source_text, sections, meta, source_sha256)
        corpus_file.write_text(xml_content, encoding="utf-8")

        n_refs = xml_content.count("<ref eId=")
        total_sections += len(sections)
        total_refs += n_refs
        ingested += 1
        print(
            f"  [{ingested:3d}] {fname} -> {meta['canonical_id']} ({len(sections)} sections, {n_refs} refs)"
        )

    print(f"\n{'DRY RUN: ' if dry_run else ''}Done!")
    print(f"  Ingested: {ingested}")
    print(f"  Skipped:  {skipped}")
    print(f"  Errors:   {errors}")
    print(f"  Total sections: {total_sections}")
    print(f"  Total references: {total_refs}")
    if not dry_run:
        print("  Next: python3 main.py pipeline verify")


if __name__ == "__main__":
    main()
