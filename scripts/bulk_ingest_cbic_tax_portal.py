#!/usr/bin/env python3
"""Ingest downloaded CBIC Tax Portal JSON into the canonical corpus.

Usage:
  python3 scripts/bulk_ingest_cbic_tax_portal.py --dry-run
  python3 scripts/bulk_ingest_cbic_tax_portal.py

The ingester is intentionally local-only. It reads JSON already downloaded
under data/Law/cbic_tax_portal and never calls the CBIC server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CBIC_DIR = REPO_ROOT / "data" / "Law" / "cbic_tax_portal"
CORPUS_DIR = REPO_ROOT / "corpus" / "in" / "union"
SOURCES_DIR = REPO_ROOT / "sources" / "in" / "union"

ACT_CANONICAL_SLUGS = {
    "central goods and services tax act 2017": "cgst-act-2017",
    "integrated goods and services tax act 2017": "igst-act-2017",
    "union territory goods and services tax act 2017": "utgst-act-2017",
    "goods and services tax compensation to states act 2017": "gst-compensation-to-states-act-2017",
    "customs act 1962": "customs-act-1962",
    "customs tariff act 1975": "customs-tariff-act-1975",
    "central excise act 1944": "central-excise-act-1944",
}


def _clean_text(text: Any) -> str:
    if text is None:
        return ""
    value = str(text).replace("\r", "\n").replace("\xa0", " ")
    value = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _norm_title(text: str) -> str:
    value = text.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\bthe\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _slugify(text: str) -> str:
    value = _norm_title(text)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-") or "document"


def _safe_eid(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", text.lower()).strip("_") or "item"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _esc(text: Any) -> str:
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _prop(name: str, value: Any) -> str:
    return f'        <property name="{_esc(name)}" value="{_esc(value)}"/>'


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    lines = []
    for key, value in sorted(metadata.items()):
        if value is None:
            value = ""
        value = str(value)
        if any(char in value for char in ':#[]{}"'):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _paragraphs(text: str) -> list[str]:
    chunks = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if chunks:
        return chunks
    return [line.strip() for line in text.splitlines() if line.strip()]


def _source_archive(
    source_dir: Path,
    json_path: Path,
    metadata: dict[str, Any],
    source_text: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "source.txt").write_text(source_text + "\n", encoding="utf-8")
    _write_metadata(source_dir / "metadata.yaml", metadata)
    (source_dir / "extracted_text.json").write_text(
        json.dumps({"text": source_text}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (source_dir / "structure.json").write_text(
        json.dumps({"nodes": [], "references": []}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(json_path, source_dir / "source.json")


def _render_doc_xml(
    *,
    metadata: dict[str, Any],
    source_text: str,
    parts: list[dict[str, str]],
    root_tag: str,
    part_tag: str,
    parser_version: str,
) -> str:
    cid = str(metadata["canonical_id"])
    date = str(metadata.get("effective_from") or metadata.get("publication_date") or "")
    title = str(metadata["title"])
    source_hash = _sha256(source_text)
    lines = [
        "<?xml version='1.0' encoding='utf-8'?>",
        "<akomaNtoso>",
        f'  <{root_tag} name="{_esc(_safe_eid(title))}">',
        "    <meta>",
        '      <identification source="#git-for-law">',
        "        <FRBRWork>",
        f'          <FRBRthis value="{_esc(cid)}"/>',
        f'          <FRBRuri value="{_esc(cid)}"/>',
        f'          <FRBRdate date="{_esc(date)}" name="effective"/>',
        '          <FRBRauthor href="/in/authority/cbic"/>',
        '          <FRBRcountry value="in"/>',
        "        </FRBRWork>",
        "      </identification>",
        '      <proprietary source="#git-for-law">',
        _prop("canonical_id", cid),
        _prop("document_type", metadata["document_type"]),
        _prop("jurisdiction", "IN-UNION"),
        _prop("language", "eng"),
        _prop("parser_version", parser_version),
        _prop("review_status", "parsed"),
        _prop("source_sha256", source_hash),
        _prop("source_type", "source-archive"),
        _prop("title", title),
        _prop("source_url", metadata.get("source_url", "")),
        _prop("publication_date", metadata.get("publication_date", "")),
        _prop("effective_from", metadata.get("effective_from", "")),
        _prop("issuing_authority", "/in/authority/cbic"),
    ]
    for key, value in sorted(metadata.items()):
        if key in {
            "canonical_id",
            "document_type",
            "title",
            "source_url",
            "publication_date",
            "effective_from",
        }:
            continue
        lines.append(_prop(key, value))
    lines.extend(["      </proprietary>", "    </meta>", "    <body>"])

    search_from = 0
    for index, part in enumerate(parts, start=1):
        num = _clean_text(part.get("num"))
        heading = _clean_text(part.get("heading"))
        body = _clean_text(part.get("text"))
        eid = _safe_eid(f"{part_tag}_{index}_{num or heading}")
        start = source_text.find(body, search_from) if body else -1
        if start < 0:
            start = source_text.find(heading, search_from) if heading else search_from
        if start < 0:
            start = search_from
        end = start + len(body or heading)
        refers_to = f"{cid}/{eid}"
        lines.append(
            f'      <{part_tag} eId="{eid}" refersTo="{_esc(refers_to)}" '
            f'sourceStart="{start}" sourceEnd="{end}" '
            f'sourceHash="{_sha256(source_text[start:end])}" sourceNodeType="{part_tag}">'
        )
        if num:
            lines.append(f"        <num>{_esc(num)}</num>")
        if heading:
            lines.append(f"        <heading>{_esc(heading)}</heading>")
        lines.append("        <content>")
        for para_index, para in enumerate(_paragraphs(body), start=1):
            p_start = source_text.find(para, start)
            if p_start < 0:
                p_start = start
            p_end = p_start + len(para)
            lines.append(
                f'          <paragraph eId="{eid}__para_{para_index}" '
                f'sourceStart="{p_start}" sourceEnd="{p_end}" '
                f'sourceHash="{_sha256(source_text[p_start:p_end])}" sourceNodeType="paragraph">'
            )
            lines.append("            <content>")
            lines.append(f"              <p>{_esc(para)}</p>")
            lines.append("            </content>")
            lines.append("          </paragraph>")
        lines.append("        </content>")
        lines.append(f"      </{part_tag}>")
        search_from = end

    lines.extend(["    </body>", f"  </{root_tag}>", "</akomaNtoso>"])
    return "\n".join(lines) + "\n"


def _existing_slugs(kind: str, xml_name: str) -> set[str]:
    root = CORPUS_DIR / kind
    if not root.exists():
        return set()
    return {path.parent.name for path in root.glob(f"*/{xml_name}")}


def _existing_act_titles() -> set[str]:
    titles: set[str] = set()
    for path in (CORPUS_DIR / "acts").glob("*/act.xml"):
        titles.add(_norm_title(path.parent.name))
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:20000]
        except OSError:
            continue
        for match in re.finditer(r'<property name="title" value="([^"]+)"', head):
            titles.add(_norm_title(match.group(1)))
    return titles


def _act_slug(data: dict[str, Any]) -> str:
    title = str(data.get("act") or data.get("name") or "")
    return ACT_CANONICAL_SLUGS.get(_norm_title(title), _slugify(title))


def _form_slug(data: dict[str, Any]) -> str:
    form_no = _clean_text(data.get("formNo") or "")
    if _norm_title(form_no).startswith("form gst "):
        return _slugify(re.sub(r"^\s*form\s+", "", form_no, flags=re.I))
    return _doc_slug(data, "formNo")


def _doc_slug(data: dict[str, Any], preferred: str) -> str:
    return _slugify(str(data.get(preferred) or data.get("slug") or data.get("name") or data.get("formNo") or "document"))


def _act_parts(data: dict[str, Any]) -> list[dict[str, str]]:
    parts = []
    for chapter in data.get("chapters") or []:
        chapter_label = " ".join(
            part for part in [chapter.get("chapterNo"), chapter.get("chapterName")] if part
        )
        for section in chapter.get("sections") or []:
            num = _clean_text(section.get("sectionNo") or chapter_label)
            heading = _clean_text(section.get("sectionName") or chapter_label)
            text = _clean_text(section.get("contentText") or heading or num)
            parts.append({"num": num, "heading": heading, "text": text})
    return parts


def _flat_parts(data: dict[str, Any], part_label: str) -> list[dict[str, str]]:
    if data.get("chapters"):
        parts = []
        for chapter in data.get("chapters") or []:
            num = _clean_text(chapter.get("regulationNo") or chapter.get("chapterNo") or part_label)
            heading = _clean_text(chapter.get("regulationName") or chapter.get("chapterName") or num)
            text = _clean_text(chapter.get("contentText") or heading or num)
            parts.append({"num": num, "heading": heading, "text": text})
        return parts
    title = _clean_text(data.get("name") or data.get("formNo") or data.get("slug") or part_label)
    fields = []
    for key in ("formNo", "name", "ruleDocNo", "ruleCategory", "formCategory", "contentFilePath", "docFilePath"):
        if data.get(key):
            fields.append(f"{key}: {data[key]}")
    text = _clean_text(data.get("contentText") or "\n".join(fields) or title)
    return [{"num": _clean_text(data.get("formNo") or data.get("ruleDocNo") or ""), "heading": title, "text": text}]


def _source_text(title: str, parts: list[dict[str, str]]) -> str:
    lines = [title, ""]
    for part in parts:
        heading = " ".join(x for x in [part.get("num"), part.get("heading")] if x)
        if heading:
            lines.append(heading)
            lines.append("")
        if part.get("text"):
            lines.append(part["text"])
            lines.append("")
    return _clean_text("\n".join(lines))


def _ingest_one(
    json_path: Path,
    kind: str,
    slug: str,
    xml_name: str,
    root_tag: str,
    part_tag: str,
    title: str,
    data: dict[str, Any],
    parts: list[dict[str, str]],
    dry_run: bool,
) -> None:
    document_type = kind[:-1] if kind.endswith("s") else kind
    if kind == "acts":
        cid = f"/in/union/acts/{slug}"
    else:
        cid = f"/in/union/{kind}/{slug}"
    metadata = {
        "canonical_id": cid,
        "document_type": document_type,
        "title": title,
        "source_url": "https://taxinformation.cbic.gov.in/",
        "publication_date": data.get("issueDt") or "",
        "effective_from": data.get("issueDt") or "",
        "cbic_source": data.get("source") or "cbic_tax_portal",
        "cbic_type": data.get("type") or "",
        "cbic_slug": data.get("slug") or "",
        "cbic_id": data.get("actId") or data.get("ruleId") or data.get("docId") or data.get("formId") or "",
        "amend_date": data.get("amendDt") or "",
    }
    source_text = _source_text(title, parts)
    corpus_path = CORPUS_DIR / kind / slug / xml_name
    source_path = SOURCES_DIR / kind / slug
    if dry_run:
        print(f"ingest {kind[:-1]} {slug}: {title}")
        return
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    xml = _render_doc_xml(
        metadata=metadata,
        source_text=source_text,
        parts=parts,
        root_tag=root_tag,
        part_tag=part_tag,
        parser_version="bulk-cbic-tax-portal-v1",
    )
    corpus_path.write_text(xml, encoding="utf-8")
    _source_archive(source_path, json_path, metadata, source_text, dry_run=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Overwrite existing corpus documents")
    args = parser.parse_args()

    if not CBIC_DIR.exists():
        print(f"missing {CBIC_DIR}", file=sys.stderr)
        return 1

    specs = [
        ("acts", "act.xml", "act", "section"),
        ("rules", "rules.xml", "doc", "rule"),
        ("regulations", "regulation.xml", "doc", "regulation"),
        ("forms", "form.xml", "doc", "form"),
    ]
    existing = {kind: _existing_slugs(kind, xml_name) for kind, xml_name, _, _ in specs}
    existing_act_titles = _existing_act_titles()
    counts = {kind: {"ingested": 0, "skipped": 0, "errors": 0} for kind, _, _, _ in specs}

    for kind, xml_name, root_tag, part_tag in specs:
        for json_path in sorted((CBIC_DIR / kind).glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if kind == "acts":
                    title = _clean_text(data.get("act") or data.get("name") or json_path.stem)
                    slug = _act_slug(data)
                    parts = _act_parts(data)
                elif kind == "forms":
                    title = _clean_text(" ".join(part for part in [data.get("formNo"), data.get("name")] if part) or json_path.stem)
                    slug = _form_slug(data)
                    parts = _flat_parts(data, "form")
                elif kind == "rules":
                    title = _clean_text(data.get("name") or data.get("slug") or json_path.stem)
                    slug = _doc_slug(data, "name")
                    parts = _flat_parts(data, "rule")
                else:
                    title = _clean_text(data.get("name") or data.get("slug") or json_path.stem)
                    slug = _doc_slug(data, "name")
                    parts = _flat_parts(data, "regulation")

                if not args.force and slug in existing[kind]:
                    counts[kind]["skipped"] += 1
                    print(f"skip existing {kind[:-1]} {slug}: {title}")
                    continue
                if kind == "acts" and not args.force and _norm_title(title) in existing_act_titles:
                    counts[kind]["skipped"] += 1
                    print(f"skip existing act title {slug}: {title}")
                    continue
                if not parts:
                    counts[kind]["skipped"] += 1
                    print(f"skip empty {kind[:-1]} {slug}: {title}")
                    continue
                _ingest_one(json_path, kind, slug, xml_name, root_tag, part_tag, title, data, parts, args.dry_run)
                counts[kind]["ingested"] += 1
                existing[kind].add(slug)
            except Exception as exc:  # noqa: BLE001
                counts[kind]["errors"] += 1
                print(f"error {json_path}: {exc}", file=sys.stderr)

    print(json.dumps(counts, indent=2, sort_keys=True))
    return 1 if any(value["errors"] for value in counts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
