"""Bulk ingest scraped India Code schedule JSONs into the canonical corpus.

Usage: python3 scripts/bulk_ingest_schedules.py [--dry-run]

Reads data/Law/base_law_schedules/*.json and writes one canonical schedule
document per schedule under corpus/in/union/acts/<act-slug>/schedules/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_DIR = REPO_ROOT / "data" / "Law" / "base_law_schedules"
SKIP_EXISTING = os.environ.get("SKIP_EXISTING", "1") != "0"
FORCE_SLUGS = {
    value.strip()
    for value in os.environ.get("FORCE_SLUGS", "").split(",")
    if value.strip()
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(name: str) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _safe_eid(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", label.lower()).strip("_") or "schedule"


def _esc_xml(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _metadata_from_json(data: dict, schedule: dict) -> dict:
    act_name = data.get("act", "")
    act_slug = _slugify(act_name)
    title = " ".join(
        part
        for part in [
            act_name,
            str(schedule.get("label") or "").rstrip("."),
            schedule.get("title") or "",
        ]
        if part
    )
    schedule_slug = _slugify(
        " ".join(
            part
            for part in [schedule.get("label"), schedule.get("title"), schedule.get("rid")]
            if part
        )
    )
    if not act_slug or not schedule_slug:
        return {}

    canonical_id = f"/in/union/acts/{act_slug}/schedules/{schedule_slug}"
    year = data.get("year")
    effective_date = f"{year}-04-01" if year else ""
    publication_date = f"{year}-01-01" if year else ""
    return {
        "canonical_id": canonical_id,
        "document_type": "schedule",
        "title": title,
        "act_title": act_name,
        "act_slug": act_slug,
        "schedule_slug": schedule_slug,
        "schedule_number": str(schedule.get("schedule_number") or ""),
        "schedule_label": schedule.get("label") or "",
        "schedule_title": schedule.get("title") or "",
        "effective_date": effective_date,
        "publication_date": publication_date,
        "source_url": schedule.get("source_url") or data.get("source") or "",
        "issuing_authority": "/in/authority/unknown",
    }


def _source_text(data: dict, schedule: dict, meta: dict) -> str:
    lines = [
        meta["title"],
        "",
        f"Act: {data.get('act', '')}",
        f"Schedule: {schedule.get('label', '')} {schedule.get('title', '')}".strip(),
        "",
        (schedule.get("full_text") or "").strip(),
        "",
    ]
    return "\n".join(lines)


def _render_schedule_xml(source_text: str, schedule: dict, meta: dict, source_sha256: str) -> str:
    cid = meta["canonical_id"]
    label = meta.get("schedule_label") or str(schedule.get("schedule_number") or "Schedule")
    title = meta.get("schedule_title") or label
    body = (schedule.get("full_text") or "").strip()
    heading_start = source_text.find(title)
    if heading_start < 0:
        heading_start = 0
    heading_end = heading_start + len(title)
    body_start = source_text.find(body) if body else -1
    body_end = body_start + len(body) if body_start >= 0 else -1
    eid = _safe_eid(label + "_" + title)
    provision_id = f"{cid}/content"
    lines = [
        "<?xml version='1.0' encoding='utf-8'?>",
        "<akomaNtoso>",
        f'  <doc name="{_esc_xml(meta["title"].lower().replace(" ", "_"))}">',
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
        '        <property name="document_type" value="schedule"/>',
        '        <property name="jurisdiction" value="IN-UNION"/>',
        '        <property name="language" value="eng"/>',
        '        <property name="parser_version" value="bulk-schedule-v1"/>',
        '        <property name="review_status" value="parsed"/>',
        f'        <property name="source_sha256" value="{source_sha256}"/>',
        '        <property name="source_type" value="source-archive"/>',
        f'        <property name="title" value="{_esc_xml(meta["title"])}"/>',
        f'        <property name="source_url" value="{_esc_xml(meta["source_url"])}"/>',
        f'        <property name="publication_date" value="{meta["publication_date"]}"/>',
        f'        <property name="effective_from" value="{meta["effective_date"]}"/>',
        f'        <property name="issuing_authority" value="{meta["issuing_authority"]}"/>',
        f'        <property name="act_title" value="{_esc_xml(meta["act_title"])}"/>',
        f'        <property name="schedule_label" value="{_esc_xml(meta["schedule_label"])}"/>',
        f'        <property name="schedule_title" value="{_esc_xml(meta["schedule_title"])}"/>',
        f'        <property name="schedule_rid" value="{_esc_xml(schedule.get("rid", ""))}"/>',
        "      </proprietary>",
        "    </meta>",
        "    <body>",
        f'      <schedule eId="{eid}" refersTo="{provision_id}"',
        f'                sourceStart="{heading_start}" sourceEnd="{heading_end}"',
        f'                sourceHash="{_sha256(source_text[heading_start:heading_end])}" sourceNodeType="schedule">',
        f"        <num>{_esc_xml(label)}</num>",
        f"        <heading>{_esc_xml(title)}</heading>",
        "        <content>",
    ]

    if body:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
        search_from = body_start if body_start >= 0 else 0
        for index, para in enumerate(paragraphs, start=1):
            start = source_text.find(para, search_from)
            if start < 0:
                start = search_from
            end = start + len(para)
            lines.append(
                f'          <paragraph eId="{eid}__para_{index}" sourceStart="{start}" sourceEnd="{end}"'
            )
            lines.append(
                f'                     sourceHash="{_sha256(source_text[start:end])}" sourceNodeType="paragraph">'
            )
            lines.append("            <content>")
            lines.append(f"              <p>{_esc_xml(para)}</p>")
            lines.append("            </content>")
            lines.append("          </paragraph>")
            search_from = end
    else:
        del body_end

    lines.extend(
        [
            "        </content>",
            "      </schedule>",
            "    </body>",
            "  </doc>",
            "</akomaNtoso>",
        ]
    )
    return "\n".join(lines)


def _write_metadata(path: Path, metadata: dict[str, str]) -> None:
    special_chars = set(":#[]{}")
    lines = []
    for key, value in sorted(metadata.items()):
        if isinstance(value, str) and any(char in value for char in special_chars):
            lines.append(f"{key}: {json.dumps(value)}")
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    json_files = sorted(SCHEDULE_DIR.glob("*.json"))
    existing = set()
    if SKIP_EXISTING:
        for path in (REPO_ROOT / "corpus" / "in" / "union" / "acts").glob("*/schedules/*/schedule.xml"):
            existing.add(path.parent.name)

    print(f"Found {len(json_files)} schedule JSON files, {len(existing)} schedules already ingested")
    ingested = skipped = errors = total_schedules = 0

    for json_path in json_files:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        schedules = data.get("schedules") or []
        if not schedules:
            skipped += 1
            continue
        for schedule in schedules:
            meta = _metadata_from_json(data, schedule)
            if not meta:
                skipped += 1
                continue
            if FORCE_SLUGS and meta["act_slug"] not in FORCE_SLUGS:
                skipped += 1
                continue
            if SKIP_EXISTING and meta["schedule_slug"] in existing:
                skipped += 1
                continue
            source_text = _source_text(data, schedule, meta)
            if not source_text.strip():
                skipped += 1
                continue
            source_sha256 = _sha256(source_text)
            source_dir = (
                REPO_ROOT
                / "sources"
                / "in"
                / "union"
                / "acts"
                / meta["act_slug"]
                / "schedules"
                / meta["schedule_slug"]
            )
            corpus_file = (
                REPO_ROOT
                / "corpus"
                / "in"
                / "union"
                / "acts"
                / meta["act_slug"]
                / "schedules"
                / meta["schedule_slug"]
                / "schedule.xml"
            )
            if dry_run:
                print(f"  WOULD ingest {meta['canonical_id']}")
                ingested += 1
                total_schedules += 1
                continue
            try:
                source_dir.mkdir(parents=True, exist_ok=True)
                corpus_file.parent.mkdir(parents=True, exist_ok=True)
                (source_dir / "source.txt").write_text(source_text, encoding="utf-8")
                _write_metadata(
                    source_dir / "metadata.yaml",
                    {
                        "canonical_id": meta["canonical_id"],
                        "document_type": "schedule",
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
                    },
                )
                (source_dir / "extracted_text.json").write_text(
                    json.dumps(
                        {
                            "source_file": "source.txt",
                            "source_sha256": source_sha256,
                            "text": source_text,
                            "pages": [{"page_number": 1, "start": 0, "end": len(source_text), "text": source_text}],
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                (source_dir / "structure.json").write_text(
                    json.dumps(
                        {
                            "profile": "git-for-law-structure-v1",
                            "mode": "bulk-schedule",
                            "nodes": [
                                {
                                    "type": "schedule",
                                    "label": meta["schedule_label"],
                                    "start": 0,
                                    "end": len(source_text),
                                    "text_hash": source_sha256,
                                    "confidence": 0.85,
                                }
                            ],
                            "references": [],
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                corpus_file.write_text(
                    _render_schedule_xml(source_text, schedule, meta, source_sha256),
                    encoding="utf-8",
                )
                ingested += 1
                total_schedules += 1
                print(f"  [{ingested:3d}] {meta['canonical_id']}")
            except Exception as exc:
                errors += 1
                print(f"  ERROR {meta.get('canonical_id', json_path.name)}: {exc}")

    print(f"\n{'DRY RUN: ' if dry_run else ''}Done!")
    print(f"  Ingested: {ingested}")
    print(f"  Skipped:  {skipped}")
    print(f"  Errors:   {errors}")
    print(f"  Total schedules: {total_schedules}")


if __name__ == "__main__":
    main()
