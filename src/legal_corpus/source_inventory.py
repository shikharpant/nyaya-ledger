"""Inventory local source PDFs before archiving and corpus ingestion."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import expected_corpus_relative_path
from .source_archive import sha256_file


CBIC_CATEGORY_FOLDERS = {
    "central tax": "Central_Tax",
    "central tax rate": "Central_Tax_Rate",
    "integrated tax": "Integrated_Tax",
    "integrated tax rate": "Integrated_Tax_Rate",
    "union territory tax": "Union_Territory_Tax",
    "union territory tax rate": "Union_Territory_Tax_Rate",
    "compensation cess": "Compensation_Cess",
    "compensation cess rate": "Compensation_Cess_Rate",
}


@dataclass
class InventoryValidationResult:
    checked_items: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_items": self.checked_items,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _slug(value: str) -> str:
    clean = re.sub(r"[()]", " ", value.lower())
    clean = re.sub(r"[^a-z0-9]+", "-", clean)
    return clean.strip("-")


def _path_text(path: Path | str) -> str:
    return Path(path).as_posix()


def category_slug(category: str) -> str:
    """Normalize a CBIC notification category for canonical IDs and paths."""
    return _slug(category)


def _is_ignored_pdf(path: Path) -> bool:
    return path.name.startswith("._")


def _category_folder(category: str) -> str:
    return CBIC_CATEGORY_FOLDERS.get(category_slug(category).replace("-", " "), category_slug(category).replace("-", "_"))


def _parse_notification_number(value: str, fallback_date: str = "") -> tuple[str, str]:
    match = re.search(r"(\d+)\s*/\s*(\d{4})", value or "")
    if match:
        return str(int(match.group(1))), match.group(2)
    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", fallback_date or "")
    year = year_match.group(1) if year_match else "unknown-year"
    clean = re.sub(r"[^a-z0-9]+", "-", (value or "unknown").lower()).strip("-")
    return clean or "unknown", year


def _date_slug(value: str) -> str:
    return _slug(value) or "undated"


def _corrigendum_target(description: str) -> tuple[str, str] | None:
    match = re.search(r"Notification\s+No\.?\s*0*(\d+)\s*/\s*(\d{4})", description or "", flags=re.IGNORECASE)
    if not match:
        return None
    return str(int(match.group(1))), match.group(2)


def _is_corrigendum(row: dict[str, str]) -> bool:
    return "corrigendum" in (row.get("notification_no", "") + " " + row.get("description", "")).lower()


def canonical_cbic_notification_id(
    category: str,
    notification_no: str,
    publication_date: str = "",
    description: str = "",
) -> str:
    if "corrigendum" in (notification_no or "").lower():
        target = _corrigendum_target(description)
        date = _date_slug(publication_date)
        if target:
            target_number, target_year = target
            return (
                f"/in/union/notifications/cbic/{category_slug(category)}/"
                f"{target_year}/{target_number}-{target_year}/corrigenda/{date}"
            )
        year = _parse_notification_number(notification_no, fallback_date=publication_date)[1]
        return f"/in/union/notifications/cbic/{category_slug(category)}/{year}/corrigenda/{date}"

    number, year = _parse_notification_number(notification_no, fallback_date=publication_date)
    return f"/in/union/notifications/cbic/{category_slug(category)}/{year}/{number}-{year}"


def _source_archive_dir_for_cbic(row: dict[str, str], sources_root: Path) -> Path:
    category = row.get("category", "").strip()
    notification_no = row.get("notification_no", "").strip()
    publication_date = row.get("date", "").strip()
    description = row.get("description", "").strip()
    if _is_corrigendum(row):
        target = _corrigendum_target(description)
        if target:
            target_number, target_year = target
            return (
                sources_root
                / "cbic"
                / category_slug(category)
                / target_year
                / f"{target_number}-{target_year}"
                / "corrigenda"
                / _date_slug(publication_date)
            )
    number, year = _parse_notification_number(notification_no, fallback_date=publication_date)
    return sources_root / "cbic" / category_slug(category) / year / f"{number}-{year}"


def _candidate_names(row: dict[str, str]) -> list[str]:
    names: list[str] = []
    for key in ["pdf_filename", "original_filename"]:
        value = (row.get(key) or "").strip()
        if value and value not in names:
            names.append(value)
    return names


def _pdf_search_index(root_dir: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if not root_dir.exists():
        return index
    for path in sorted(root_dir.rglob("*.pdf")):
        if _is_ignored_pdf(path):
            continue
        index.setdefault(path.name.lower(), []).append(path)
    return index


def _find_pdf(row: dict[str, str], cbic_root: Path, search_index: dict[str, list[Path]]) -> Path | None:
    folder = _category_folder(row.get("category", ""))
    for name in _candidate_names(row):
        direct = cbic_root / folder / name
        if direct.exists():
            return direct
        root_direct = cbic_root / name
        if root_direct.exists():
            return root_direct
        matches = search_index.get(name.lower(), [])
        if matches:
            folder_matches = [path for path in matches if folder in path.parts]
            return folder_matches[0] if folder_matches else matches[0]
    return None


def _pdf_preview(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return ""
            return pdf.pages[0].extract_text() or ""
    except Exception:
        return ""


def _normalized_search_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).replace("–", "-").strip()


def _row_matches_pdf_text(row: dict[str, str], text: str) -> bool:
    normalized = _normalized_search_text(text)
    if not normalized:
        return False

    category = category_slug(row.get("category", "")).replace("-", " ")
    if category and category not in normalized:
        return False

    notification_no = row.get("notification_no", "")
    if _is_corrigendum(row):
        target = _corrigendum_target(row.get("description", ""))
        if not target:
            return "corrigendum" in normalized
        target_number, target_year = target
        target_patterns = [
            f"no.{target_number}/{target_year}",
            f"no. {target_number}/{target_year}",
            f"number {target_number}/{target_year}",
            f"notification {target_number}/{target_year}",
        ]
        return "corrigendum" in normalized and any(pattern in normalized for pattern in target_patterns)

    number, year = _parse_notification_number(notification_no, fallback_date=row.get("date", ""))
    if not number.isdigit():
        return False
    number_patterns = [
        f"notification no. {number}/{year}",
        f"notification no.{number}/{year}",
        f"notification number {number}/{year}",
        f"{number}/{year}",
    ]
    return any(pattern in normalized for pattern in number_patterns)


def _find_pdf_by_text(
    row: dict[str, str],
    cbic_root: Path,
    covered_paths: set[Path],
    preview_cache: dict[Path, str],
) -> Path | None:
    folder = _category_folder(row.get("category", ""))
    category_dir = cbic_root / folder
    if not category_dir.exists():
        return None
    for path in sorted(category_dir.glob("*.pdf")):
        resolved = path.resolve()
        if resolved in covered_paths or _is_ignored_pdf(path):
            continue
        preview = preview_cache.setdefault(path, _pdf_preview(path))
        if _row_matches_pdf_text(row, preview):
            return path
    return None


def _existing_candidate_paths(row: dict[str, str], cbic_root: Path, search_index: dict[str, list[Path]]) -> list[Path]:
    folder = _category_folder(row.get("category", ""))
    paths: list[Path] = []
    seen: set[Path] = set()
    for name in _candidate_names(row):
        candidates = [cbic_root / folder / name, cbic_root / name]
        candidates.extend(search_index.get(name.lower(), []))
        for candidate in candidates:
            if candidate.exists() and candidate not in seen:
                paths.append(candidate)
                seen.add(candidate)
    return paths


def _file_fields(path: Path | None, compute_checksums: bool) -> dict[str, Any]:
    if not path:
        return {"source_path": "", "source_sha256": "", "size_bytes": 0}
    return {
        "source_path": _path_text(path),
        "source_sha256": sha256_file(path) if compute_checksums else "",
        "size_bytes": path.stat().st_size,
    }


def _ingest_command(item: dict[str, Any]) -> list[str]:
    if item.get("status") != "ready":
        return []
    command = [
        "python3",
        "main.py",
        "corpus",
        "ingest",
        item["source_path"],
        item["source_dir"],
        item["output_path"],
        "--canonical-id",
        item["canonical_id"],
        "--document-type",
        item["document_type"],
        "--title",
        item["title"],
        "--issuing-authority",
        item["issuing_authority"],
    ]
    if item.get("publication_date"):
        command.extend(["--publication-date", item["publication_date"]])
    if item.get("source_url"):
        command.extend(["--source-url", item["source_url"]])
    return command


def _cbic_item(
    row: dict[str, str],
    pdf_path: Path | None,
    sources_root: Path,
    corpus_root: Path,
    compute_checksums: bool,
) -> dict[str, Any]:
    category = row.get("category", "").strip()
    notification_no = row.get("notification_no", "").strip()
    publication_date = row.get("date", "").strip()
    description = row.get("description", "").strip()
    canonical_id = canonical_cbic_notification_id(category, notification_no, publication_date, description)
    source_dir = _source_archive_dir_for_cbic(row, sources_root)
    output_path = corpus_root / expected_corpus_relative_path(canonical_id, "notification")
    title = description.rstrip(".") if _is_corrigendum(row) and description else f"Notification No. {notification_no}".strip()
    item: dict[str, Any] = {
        "kind": "cbic_notification",
        "status": "ready" if pdf_path else "missing",
        "canonical_id": canonical_id,
        "document_type": "notification",
        "title": title,
        "description": description,
        "category": category,
        "category_slug": category_slug(category),
        "notification_no": notification_no,
        "subtype": "corrigendum" if _is_corrigendum(row) else "notification",
        "publication_date": publication_date,
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "issuing_authority": "/in/authority/cbic",
        "source_url": row.get("pdf_url", "").strip(),
        "source_dir": _path_text(source_dir),
        "output_path": _path_text(output_path),
        "content_id": row.get("content_id", "").strip(),
        "record_id": row.get("record_id", "").strip(),
        "expected_pdf_filename": row.get("pdf_filename", "").strip(),
        "original_filename": row.get("original_filename", "").strip(),
    }
    item.update(_file_fields(pdf_path, compute_checksums=compute_checksums))
    item["ingest_command"] = _ingest_command(item)
    return item


def _finance_act_slug_and_title(stem: str) -> tuple[str, str] | None:
    act_match = re.fullmatch(r"Finance Act \((\d+) of (\d{4})\) (\d{4})", stem)
    if act_match:
        act_no, act_year, title_year = act_match.groups()
        slug = f"finance-act-{title_year}-{int(act_no)}-of-{act_year}"
        title = f"Finance Act, {title_year} (Act {int(act_no)} of {act_year})"
        return slug, title

    year_match = re.fullmatch(r"Finance Act (\d{4})", stem)
    if year_match:
        year = year_match.group(1)
        return f"finance-act-{year}", f"Finance Act, {year}"

    return None


def _local_act_item(path: Path, sources_root: Path, corpus_root: Path, compute_checksums: bool) -> dict[str, Any] | None:
    stem = path.stem.strip()
    finance = _finance_act_slug_and_title(stem)
    if finance:
        act_slug, title = finance
    elif stem == "Customs Tariff":
        act_slug = "customs-tariff-act-1975"
        title = "Customs Tariff Act, 1975"
    else:
        return None

    canonical_id = f"/in/union/acts/{act_slug}"
    source_dir = sources_root / "in" / "union" / "acts" / act_slug
    output_path = corpus_root / expected_corpus_relative_path(canonical_id, "act")
    item: dict[str, Any] = {
        "kind": "local_act",
        "status": "ready",
        "canonical_id": canonical_id,
        "document_type": "act",
        "title": title,
        "description": "",
        "category": "Act",
        "category_slug": "act",
        "notification_no": "",
        "publication_date": "",
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "issuing_authority": "/in/authority/parliament-of-india",
        "source_url": _path_text(path),
        "source_dir": _path_text(source_dir),
        "output_path": _path_text(output_path),
        "content_id": "",
        "record_id": "",
        "expected_pdf_filename": path.name,
        "original_filename": path.name,
    }
    item.update(_file_fields(path, compute_checksums=compute_checksums))
    item["ingest_command"] = _ingest_command(item)
    return item


def _unclassified_item(path: Path, compute_checksums: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "local_pdf",
        "status": "unclassified",
        "canonical_id": "",
        "document_type": "unknown",
        "title": path.stem,
        "description": "",
        "category": "unclassified",
        "category_slug": "unclassified",
        "notification_no": "",
        "publication_date": "",
        "jurisdiction": "IN-UNION",
        "language": "eng",
        "issuing_authority": "",
        "source_url": _path_text(path),
        "source_dir": "",
        "output_path": "",
        "content_id": "",
        "record_id": "",
        "expected_pdf_filename": path.name,
        "original_filename": path.name,
        "ingest_command": [],
    }
    item.update(_file_fields(path, compute_checksums=compute_checksums))
    return item


def _default_cbic_index(root_dir: Path) -> Path | None:
    candidates = [
        root_dir / "GST_Notifications_CBIC" / "_notification_index.csv",
        root_dir / "_notification_index.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_source_inventory(
    root_dir: Path,
    *,
    index_csv: Path | None = None,
    sources_root: Path = Path("sources"),
    corpus_root: Path = Path("corpus"),
    include_unclassified: bool = True,
    compute_checksums: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a source inventory from local legal PDFs and known metadata indexes."""
    root_dir = Path(root_dir)
    index_csv = Path(index_csv) if index_csv else _default_cbic_index(root_dir)
    items: list[dict[str, Any]] = []
    covered_paths: set[Path] = set()

    if index_csv and index_csv.exists():
        cbic_root = index_csv.parent
        search_index = _pdf_search_index(cbic_root)
        preview_cache: dict[Path, str] = {}
        with index_csv.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            for covered_path in _existing_candidate_paths(row, cbic_root, search_index):
                covered_paths.add(covered_path.resolve())

        for row in rows:
            pdf_path = _find_pdf(row, cbic_root, search_index)
            if not pdf_path:
                pdf_path = _find_pdf_by_text(row, cbic_root, covered_paths, preview_cache)
                if pdf_path:
                    covered_paths.add(pdf_path.resolve())
            items.append(
                _cbic_item(
                    row,
                    pdf_path,
                    sources_root=sources_root,
                    corpus_root=corpus_root,
                    compute_checksums=compute_checksums,
                )
            )
            if limit is not None and len(items) >= limit:
                break

    if include_unclassified and (limit is None or len(items) < limit):
        for path in sorted(root_dir.rglob("*.pdf")):
            if _is_ignored_pdf(path):
                continue
            if path.resolve() in covered_paths:
                continue
            item = _local_act_item(
                path,
                sources_root=sources_root,
                corpus_root=corpus_root,
                compute_checksums=compute_checksums,
            )
            items.append(item or _unclassified_item(path, compute_checksums=compute_checksums))
            if limit is not None and len(items) >= limit:
                break

    status_counts = Counter(item["status"] for item in items)
    category_counts = Counter(item.get("category_slug", "") for item in items)
    return {
        "profile": "git-for-law-source-inventory-v1",
        "source_root": _path_text(root_dir),
        "index_csv": _path_text(index_csv) if index_csv else "",
        "sources_root": _path_text(sources_root),
        "corpus_root": _path_text(corpus_root),
        "stats": {
            "items": len(items),
            "ready": status_counts.get("ready", 0),
            "missing": status_counts.get("missing", 0),
            "unclassified": status_counts.get("unclassified", 0),
            "categories": dict(sorted(category_counts.items())),
        },
        "items": items,
    }


def write_source_inventory(inventory: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def validate_source_inventory(inventory: dict[str, Any]) -> InventoryValidationResult:
    """Validate a generated source inventory before batch ingestion."""
    result = InventoryValidationResult()
    items = inventory.get("items")
    if not isinstance(items, list):
        result.errors.append("inventory: items must be a list")
        return result

    status_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    ready_ids: dict[str, int] = {}

    for index, item in enumerate(items, start=1):
        result.checked_items += 1
        if not isinstance(item, dict):
            result.errors.append(f"item {index}: must be an object")
            continue

        status = item.get("status", "")
        status_counts[status] += 1
        category_counts[item.get("category_slug", "")] += 1
        if status not in {"ready", "missing", "unclassified"}:
            result.errors.append(f"item {index}: unsupported status: {status}")

        if status == "unclassified":
            result.warnings.append(f"item {index}: unclassified source: {item.get('source_path', '')}")
            continue

        canonical_id = item.get("canonical_id", "")
        document_type = item.get("document_type", "")
        if status == "ready":
            for field_name in ["source_path", "source_dir", "output_path", "canonical_id", "document_type"]:
                if not item.get(field_name):
                    result.errors.append(f"item {index}: ready item missing {field_name}")

            if canonical_id:
                previous = ready_ids.get(canonical_id)
                if previous:
                    result.errors.append(f"item {index}: duplicate ready canonical_id also used by item {previous}: {canonical_id}")
                ready_ids[canonical_id] = index
                if not canonical_id.startswith("/in/"):
                    result.errors.append(f"item {index}: canonical_id must start with /in/: {canonical_id}")
                if not re.fullmatch(r"/[a-z0-9][a-z0-9/_-]*", canonical_id):
                    result.errors.append(f"item {index}: canonical_id contains unsupported characters: {canonical_id}")

            source_path = Path(item.get("source_path", ""))
            if item.get("source_path") and not source_path.exists():
                result.errors.append(f"item {index}: source_path does not exist: {source_path}")
            expected_hash = item.get("source_sha256", "")
            if expected_hash:
                if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                    result.errors.append(f"item {index}: source_sha256 must be a SHA-256 hex digest")
                elif source_path.exists() and sha256_file(source_path) != expected_hash:
                    result.errors.append(f"item {index}: source_sha256 does not match source_path: {source_path}")

            output_path = item.get("output_path", "")
            if canonical_id and document_type and output_path:
                expected_suffix = expected_corpus_relative_path(canonical_id, document_type).as_posix()
                if not output_path.endswith(expected_suffix):
                    result.errors.append(f"item {index}: output_path does not match canonical_id; expected suffix {expected_suffix}")

            ingest_command = item.get("ingest_command", [])
            if not isinstance(ingest_command, list) or len(ingest_command) < 10:
                result.errors.append(f"item {index}: ready item must include an ingest_command")

        if status == "missing" and item.get("source_path"):
            result.warnings.append(f"item {index}: missing item has source_path set: {item.get('source_path')}")

    stats = inventory.get("stats", {})
    if isinstance(stats, dict):
        expected_counts = {
            "items": len(items),
            "ready": status_counts.get("ready", 0),
            "missing": status_counts.get("missing", 0),
            "unclassified": status_counts.get("unclassified", 0),
            "categories": dict(sorted(category_counts.items())),
        }
        for key in ["items", "ready", "missing", "unclassified"]:
            if stats.get(key) != expected_counts[key]:
                result.errors.append(f"stats.{key} mismatch: expected {expected_counts[key]}, found {stats.get(key)}")
        if stats.get("categories") != expected_counts["categories"]:
            result.errors.append("stats.categories mismatch")
    else:
        result.errors.append("inventory: stats must be an object")

    return result


def validate_source_inventory_file(path: Path) -> InventoryValidationResult:
    return validate_source_inventory(json.loads(path.read_text(encoding="utf-8")))


def build_inventory_report(inventory: dict[str, Any]) -> dict[str, Any]:
    """Build a compact review report from a source inventory."""
    items = inventory.get("items", [])
    status_counts = Counter(item.get("status", "") for item in items if isinstance(item, dict))
    category_counts = Counter(item.get("category_slug", "") for item in items if isinstance(item, dict))
    ready_by_category = Counter(
        item.get("category_slug", "")
        for item in items
        if isinstance(item, dict) and item.get("status") == "ready"
    )
    missing = [
        {
            "category": item.get("category", ""),
            "category_slug": item.get("category_slug", ""),
            "notification_no": item.get("notification_no", ""),
            "publication_date": item.get("publication_date", ""),
            "expected_pdf_filename": item.get("expected_pdf_filename", ""),
            "original_filename": item.get("original_filename", ""),
            "source_url": item.get("source_url", ""),
            "canonical_id": item.get("canonical_id", ""),
        }
        for item in items
        if isinstance(item, dict) and item.get("status") == "missing"
    ]
    unclassified = [
        {
            "title": item.get("title", ""),
            "source_path": item.get("source_path", ""),
            "source_sha256": item.get("source_sha256", ""),
            "size_bytes": item.get("size_bytes", 0),
        }
        for item in items
        if isinstance(item, dict) and item.get("status") == "unclassified"
    ]
    ready_samples = [
        {
            "category_slug": item.get("category_slug", ""),
            "canonical_id": item.get("canonical_id", ""),
            "source_path": item.get("source_path", ""),
            "output_path": item.get("output_path", ""),
        }
        for item in items
        if isinstance(item, dict) and item.get("status") == "ready"
    ][:20]
    validation = validate_source_inventory(inventory)
    return {
        "profile": "git-for-law-source-inventory-report-v1",
        "source_root": inventory.get("source_root", ""),
        "index_csv": inventory.get("index_csv", ""),
        "stats": {
            "items": len(items) if isinstance(items, list) else 0,
            "ready": status_counts.get("ready", 0),
            "missing": status_counts.get("missing", 0),
            "unclassified": status_counts.get("unclassified", 0),
            "categories": dict(sorted(category_counts.items())),
            "ready_by_category": dict(sorted(ready_by_category.items())),
        },
        "validation": validation.to_dict(),
        "missing": missing,
        "unclassified": unclassified,
        "ready_samples": ready_samples,
    }


def build_inventory_report_file(path: Path) -> dict[str, Any]:
    return build_inventory_report(json.loads(path.read_text(encoding="utf-8")))


def write_inventory_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
