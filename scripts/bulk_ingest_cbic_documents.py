#!/usr/bin/env python3
"""Bulk-ingest CBIC scraped JSON (notifications, circulars, orders, instructions) into corpus XML.

Extracts text from embedded PDF base64 using PyMuPDF, then generates canonical
Akoma Ntoso XML and source archives.

Usage:
  python3 scripts/bulk_ingest_cbic_documents.py --dry-run
  python3 scripts/bulk_ingest_cbic_documents.py
  python3 scripts/bulk_ingest_cbic_documents.py --categories notifications,circulars
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

REPO_ROOT = Path(__file__).resolve().parent.parent
CBIC_DIR = REPO_ROOT / "data" / "Law" / "cbic_tax_portal"
CORPUS_DIR = REPO_ROOT / "corpus" / "in" / "union"
SOURCES_DIR = REPO_ROOT / "sources" / "in" / "union"

CATEGORY_KIND = {
    "notifications": "notifications",
    "circulars": "circulars",
    "orders": "orders",
    "instructions": "instructions",
}

CATEGORY_DOC_TYPE = {
    "notifications": "notification",
    "circulars": "circular",
    "orders": "order",
    "instructions": "instruction",
}

CATEGORY_ROOT_TAG = {
    "notifications": "doc",
    "circulars": "doc",
    "orders": "doc",
    "instructions": "doc",
}

CATEGORY_PART_TAG = {
    "notifications": "paragraph",
    "circulars": "paragraph",
    "orders": "paragraph",
    "instructions": "paragraph",
}

NOTIFICATION_CATEGORY_SLUGS: dict[str, str] = {}


def _clean_text(text: Any) -> str:
    if text is None:
        return ""
    value = str(text).replace("\r", "\n").replace("\xa0", " ")
    value = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_eid(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", text.lower()).strip("_") or "item"


def _slugify(text: str) -> str:
    value = text.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-") or "document"


def _category_slug(category: str) -> str:
    return _slugify(category)


def _parse_number_year_suffix(no_str: str) -> tuple[str, str, str]:
    match = re.search(r"(\d+)\s*/\s*(\d{4})\s*(.*)", no_str or "")
    if match:
        number = str(int(match.group(1)))
        year = match.group(2)
        suffix = _slugify(match.group(3).strip()) if match.group(3).strip() else ""
        return number, year, suffix
    return "", "", ""


def _extract_date(iso_dt: str | None) -> str:
    if not iso_dt:
        return ""
    return iso_dt[:10]


def _extract_pdf_text(pdf_b64: str) -> str:
    if not fitz:
        return ""
    try:
        raw = base64.b64decode(pdf_b64)
        doc = fitz.open(stream=raw, filetype="pdf")
        parts: list[str] = []
        for page in doc:
            t = page.get_text()
            if t:
                parts.append(t)
        return "\n".join(parts)
    except Exception:
        return ""


def _notification_canonical_id(data: dict[str, Any], category: str) -> str:
    cat = data.get("category", "") or category
    cat_slug = _category_slug(cat)
    no = data.get("no", "") or ""
    date = _extract_date(data.get("issueDt"))

    is_corrigendum = "corrigendum" in (no + (data.get("name") or "")).lower()

    number, year, suffix = _parse_number_year_suffix(no)

    if not number:
        year_match = re.search(r"\b(20\d{2}|19\d{2})\b", date)
        year = year_match.group(1) if year_match else "unknown-year"
        slug_no = _slugify(no) if no else _slugify(data.get("name") or "unknown")
        return f"/in/union/notifications/cbic/{cat_slug}/{year}/{slug_no}"

    name_part = f"{number}-{year}"
    if suffix:
        name_part = f"{name_part}-{suffix}"

    if is_corrigendum:
        date_slug = _slugify(date) if date else "undated"
        return f"/in/union/notifications/cbic/{cat_slug}/{year}/{name_part}/corrigenda/{date_slug}"

    return f"/in/union/notifications/cbic/{cat_slug}/{year}/{name_part}"


def _coi_canonical_id(data: dict[str, Any], category: str) -> str:
    kind = CATEGORY_KIND[category]
    no = data.get("no", "") or ""
    date = _extract_date(data.get("issueDt"))
    number, year, suffix = _parse_number_year_suffix(no)
    if number:
        name_part = f"{number}-{year}"
        if suffix:
            name_part = f"{name_part}-{suffix}"
        return f"/in/union/{kind}/cbic/{year}/{name_part}"
    name = data.get("name", "") or "unknown"
    date_slug = _slugify(date) if date else "undated"
    return f"/in/union/{kind}/cbic/{date_slug}/{_slugify(name)}"


def _truncate_name(name: str, max_len: int = 200) -> str:
    if len(name) <= max_len:
        return name
    return name[:max_len].rsplit("-", 1)[0] + "-" + _sha256(name)[:8]


def _corpus_xml_path(canonical_id: str, category: str) -> Path:
    parts = [p for p in canonical_id.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "in" and parts[1] == "union":
        parts = parts[2:]
    parts = [_truncate_name(p) for p in parts]
    return CORPUS_DIR / Path(*parts).with_suffix(".xml")


def _source_dir(canonical_id: str) -> Path:
    parts = [p for p in canonical_id.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "in" and parts[1] == "union":
        parts = parts[2:]
    parts = [_truncate_name(p) for p in parts]
    return SOURCES_DIR / Path(*parts)


def _extract_text(data: dict[str, Any]) -> str:
    html = data.get("contentHtml", "")
    if html and "Request Rejected" not in html and len(html) > 50:
        from html.parser import HTMLParser

        class _Extractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self._parts: list[str] = []

            def handle_data(self, d):
                self._parts.append(d)

            def get_text(self):
                return " ".join(self._parts).strip()

        ext = _Extractor()
        ext.feed(html)
        t = ext.get_text()
        if t:
            return t

    pdf_b64 = data.get("contentPdfBase64", "")
    if pdf_b64:
        return _extract_pdf_text(pdf_b64)

    return data.get("contentText", "") or ""


def _build_parts(text: str) -> list[dict[str, str]]:
    if not text:
        return []
    chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
    if not chunks:
        chunks = [l.strip() for l in text.splitlines() if l.strip()]
    parts: list[dict[str, str]] = []
    for i, chunk in enumerate(chunks, 1):
        lines = chunk.splitlines()
        first = lines[0].strip() if lines else ""
        if len(lines) == 1 and len(first) < 120 and not first.endswith("."):
            parts.append({"num": str(i), "heading": first, "text": chunk})
        else:
            parts.append({"num": str(i), "heading": "", "text": chunk})
    return parts


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


def _source_archive(
    source_dir: Path,
    json_path: Path,
    metadata: dict[str, Any],
    source_text: str,
) -> None:
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


def _render_xml(
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
        for para_text in (body or heading).splitlines():
            para_text = para_text.strip()
            if not para_text:
                continue
            p_start = source_text.find(para_text, start)
            if p_start < 0:
                p_start = start
            p_end = p_start + len(para_text)
            lines.append(f"          <p>{_esc(para_text)}</p>")
        lines.append("        </content>")
        lines.append(f"      </{part_tag}>")
        search_from = end

    lines.extend(["    </body>", f"  </{root_tag}>", "</akomaNtoso>"])
    return "\n".join(lines) + "\n"


def _existing_xmls(category: str) -> set[str]:
    kind = CATEGORY_KIND[category]
    root = CORPUS_DIR / kind
    if not root.exists():
        return set()
    return {str(p.relative_to(CORPUS_DIR).with_suffix("")) for p in root.rglob("*.xml")}


def _title(data: dict[str, Any], category: str) -> str:
    no = _clean_text(data.get("no", ""))
    name = _clean_text(data.get("name", ""))
    if no and name:
        return f"{no}: {name}"
    return name or no or "Unknown"


def ingest_category(
    category: str,
    dry_run: bool,
    force: bool,
    skip_no_content: bool = True,
) -> dict[str, int]:
    if fitz is None:
        print("WARNING: PyMuPDF (fitz) not available, PDF text extraction disabled", file=sys.stderr)

    kind = CATEGORY_KIND[category]
    doc_type = CATEGORY_DOC_TYPE[category]
    root_tag = CATEGORY_ROOT_TAG[category]
    part_tag = CATEGORY_PART_TAG[category]
    input_dir = CBIC_DIR / category

    if not input_dir.exists():
        print(f"missing {input_dir}", file=sys.stderr)
        return {"ingested": 0, "skipped": 0, "no_content": 0, "errors": 0}

    existing = _existing_xmls(category)
    counts = {"ingested": 0, "skipped": 0, "no_content": 0, "errors": 0}

    json_files = sorted(input_dir.glob("*.json"))
    total = len(json_files)
    for idx, json_path in enumerate(json_files, 1):
        if idx % 500 == 0 or idx == total:
            print(f"  [{category}] {idx}/{total} processed...", flush=True)
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))

            text = _extract_text(data)
            if not text or len(text.strip()) < 20:
                counts["no_content"] += 1
                continue

            title = _title(data, category)

            if category == "notifications":
                cid = _notification_canonical_id(data, category)
            else:
                cid = _coi_canonical_id(data, category)

            corpus_path = _corpus_xml_path(cid, category)
            rel_key = str(corpus_path.relative_to(CORPUS_DIR).with_suffix(""))

            if not force and rel_key in existing:
                counts["skipped"] += 1
                continue

            parts = _build_parts(text)
            if not parts:
                counts["no_content"] += 1
                continue

            metadata = {
                "canonical_id": cid,
                "document_type": doc_type,
                "title": title,
                "source_url": "https://taxinformation.cbic.gov.in/",
                "publication_date": _extract_date(data.get("issueDt")),
                "effective_from": _extract_date(data.get("issueDt")),
                "cbic_source": data.get("source") or "cbic_tax_portal",
                "cbic_type": data.get("type") or "",
                "cbic_id": str(data.get("id") or ""),
                "cbic_category": data.get("category") or "",
                "cbic_no": data.get("no") or "",
                "amend_date": _extract_date(data.get("amendDt")),
            }

            if dry_run:
                print(f"  [dry-run] {cid}: {title[:80]}")
                counts["ingested"] += 1
                continue

            corpus_path.parent.mkdir(parents=True, exist_ok=True)
            xml = _render_xml(
                metadata=metadata,
                source_text=text,
                parts=parts,
                root_tag=root_tag,
                part_tag=part_tag,
                parser_version="bulk-cbic-document-v1",
            )
            corpus_path.write_text(xml, encoding="utf-8")
            _source_archive(_source_dir(cid), json_path, metadata, text)
            counts["ingested"] += 1
            existing.add(rel_key)

        except Exception as exc:
            counts["errors"] += 1
            print(f"  error {json_path.name}: {exc}", file=sys.stderr)

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Overwrite existing corpus documents")
    parser.add_argument(
        "--categories",
        default="notifications,circulars,orders,instructions",
        help="Comma-separated categories to ingest (default: all)",
    )
    args = parser.parse_args()

    if not CBIC_DIR.exists():
        print(f"missing {CBIC_DIR}", file=sys.stderr)
        return 1

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    all_counts: dict[str, dict[str, int]] = {}

    for category in categories:
        if category not in CATEGORY_KIND:
            print(f"unknown category: {category}", file=sys.stderr)
            continue
        print(f"\n=== Ingesting {category} ===", flush=True)
        counts = ingest_category(category, args.dry_run, args.force)
        all_counts[category] = counts
        print(f"  ingested={counts['ingested']} skipped={counts['skipped']} "
              f"no_content={counts['no_content']} errors={counts['errors']}")

    print(f"\n{json.dumps(all_counts, indent=2, sort_keys=True)}")
    return 1 if any(c["errors"] > 0 for c in all_counts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
