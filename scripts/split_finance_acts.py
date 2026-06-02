#!/usr/bin/env python3
"""Split the Income Tax India "Finance Acts" aggregate into per-Act JSON files."""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESIDUAL_DIR = ROOT / "data" / "Law" / "base_laws" / "residual"
INPUT = RESIDUAL_DIR / "finance_acts.json"
OUTPUT_DIR = RESIDUAL_DIR / "finance_acts_split"
REPORT = RESIDUAL_DIR / "finance_acts_split_report.json"

SOURCE_PATH_RE = re.compile(
    r"\\act\\directtaxlaws\\finact\\htmlfiles\\(?P<folder>[^\\]+)\\(?P<file>[^\\\s]+?\.htm)",
    re.IGNORECASE,
)
TAXONOMY_YEAR_RE = re.compile(
    r"(?:^|[;\s])\d+;#(?P<year>(?:19|20)\d{2})(?:\s*\(No\.\s*(?P<number>\d+)\))?\|[0-9a-f-]{36}",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?P<year>(?:19|20)\d{2})")
FINANCE_ACT_TITLE_RE = re.compile(
    r"FINANCE(?:\s*\(NO\.\s*(?P<number>\d+)\))?\s+ACT,\s*(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE,
)


def slug_for(year: str, variant: str) -> str:
    if variant == "no_1":
        return f"finance_no_1_act_{year}"
    if variant == "no_2":
        return f"finance_no_2_act_{year}"
    return f"finance_act_{year}"


def title_for(year: str, variant: str) -> str:
    if variant == "no_1":
        return f"Finance (No. 1) Act, {year}"
    if variant == "no_2":
        return f"Finance (No. 2) Act, {year}"
    return f"Finance Act, {year}"


def variant_from_number(number: str | None) -> str:
    if number == "1":
        return "no_1"
    if number == "2":
        return "no_2"
    return "regular"


def infer_group(section: dict) -> tuple[str | None, str | None, str | None]:
    text = section.get("full_text") or ""
    path_match = SOURCE_PATH_RE.search(text)
    source_path = None
    year = None
    variant = "regular"

    if path_match:
        folder = path_match.group("folder")
        filename = path_match.group("file")
        source_path = f"\\act\\directtaxlaws\\finact\\htmlfiles\\{folder}\\{filename}"

        file_year = YEAR_RE.search(filename)
        folder_year = YEAR_RE.fullmatch(folder)
        if file_year:
            year = file_year.group("year")
        elif folder_year:
            year = folder_year.group("year")

        if re.search(r"(?:^|_)2_(?:19|20)\d{2}\.htm$", filename, re.IGNORECASE):
            variant = "no_2"
        elif re.search(r"_no2\.htm$", filename, re.IGNORECASE):
            variant = "no_2"

    if year is None:
        taxonomy_year = TAXONOMY_YEAR_RE.search(text)
        if taxonomy_year:
            year = taxonomy_year.group("year")
            variant = variant_from_number(taxonomy_year.group("number"))

    title_match = FINANCE_ACT_TITLE_RE.search(text)
    if title_match:
        year = title_match.group("year")
        variant = variant_from_number(title_match.group("number"))

    if year is None:
        fallback_year = YEAR_RE.search(" ".join(str(section.get(k, "")) for k in ("title", "description")))
        if fallback_year:
            year = fallback_year.group("year")

    if year is None:
        return None, None, source_path

    return year, variant, source_path


def main() -> None:
    aggregate = json.loads(INPUT.read_text(encoding="utf-8"))
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    unresolved: list[dict] = []

    for index, section in enumerate(aggregate.get("sections", [])):
        year, variant, source_path = infer_group(section)
        annotated = dict(section)
        annotated["finance_aggregate_index"] = index
        if source_path:
            annotated["finance_source_path"] = source_path
        if year:
            annotated["finance_year"] = year
        if variant:
            annotated["finance_variant"] = variant

        if year is None or variant is None:
            unresolved.append(annotated)
            continue

        groups[(year, variant)].append(annotated)

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat()
    split_files = []

    for (year, variant), sections in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        slug = slug_for(year, variant)
        output_path = OUTPUT_DIR / f"{slug}.json"
        payload = {
            "source": aggregate.get("source"),
            "act": title_for(year, variant),
            "aggregate_source": str(INPUT.relative_to(ROOT)),
            "year": year,
            "variant": variant,
            "total_sections": len(sections),
            "scraped_at": aggregate.get("scraped_at"),
            "split_at": generated_at,
            "sections": sections,
        }
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        split_files.append(
            {
                "file": str(output_path.relative_to(ROOT)),
                "act": payload["act"],
                "year": year,
                "variant": variant,
                "sections": len(sections),
            }
        )

    if unresolved:
        unresolved_path = OUTPUT_DIR / "finance_acts_unsplit.json"
        unresolved_payload = {
            "source": aggregate.get("source"),
            "act": "Finance Acts - Unsplit Entries",
            "aggregate_source": str(INPUT.relative_to(ROOT)),
            "total_sections": len(unresolved),
            "split_at": generated_at,
            "sections": unresolved,
        }
        unresolved_path.write_text(
            json.dumps(unresolved_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    report = {
        "aggregate_source": str(INPUT.relative_to(ROOT)),
        "aggregate_entries": len(aggregate.get("sections", [])),
        "split_at": generated_at,
        "split_files": len(split_files),
        "split_entries": sum(item["sections"] for item in split_files),
        "unsplit_entries": len(unresolved),
        "output_dir": str(OUTPUT_DIR.relative_to(ROOT)),
        "files": split_files,
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Split {report['split_entries']} entries into {report['split_files']} files; "
        f"unsplit={report['unsplit_entries']}"
    )


if __name__ == "__main__":
    main()
