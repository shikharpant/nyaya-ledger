"""Fetch consolidated source files used as reconciliation checkpoints."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import json
import shutil
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .amendment_events import sha256_text
from .baselines import BaselineComponent, RULES_WORK, _render_components_xml, parse_rules_pdf_deterministic
from .renderer import write_xml

DEFAULT_RULES_CONSOLIDATED_URL = "https://cbic-gst.gov.in/gst-goods-services-rates.html"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _PdfLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        href = attrs_dict.get("href")
        if href:
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        self.links.append((self._current_href, " ".join(self._current_text)))
        self._current_href = None
        self._current_text = []


class _HtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"p", "div", "br", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))).strip()


def _fetch_bytes(url: str, timeout: int, *, verify_tls: bool = True) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "git-for-law-version-history/1.0"})
    context = None if verify_tls else ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read(), response.headers.get("Content-Type", "")


def _looks_like_pdf(content_type: str, url: str, data: bytes) -> bool:
    return "pdf" in content_type.lower() or url.lower().endswith(".pdf") or data.startswith(b"%PDF")


def _decode_json_pdf(data: bytes, content_type: str) -> tuple[bytes, dict[str, Any]] | None:
    if "json" not in content_type.lower():
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), str):
        return None
    try:
        decoded = base64.b64decode(payload["data"], validate=True)
    except (binascii.Error, ValueError):
        return None
    if not decoded.startswith(b"%PDF"):
        return None
    metadata = {
        "json_pdf_wrapper": True,
        "fileName": payload.get("fileName") or "",
    }
    return decoded, metadata


def _pdf_candidates(base_url: str, html: bytes) -> list[dict[str, Any]]:
    parser = _PdfLinkParser()
    parser.feed(html.decode("utf-8", errors="ignore"))
    candidates: list[dict[str, Any]] = []
    for href, text in parser.links:
        combined = f"{href} {text}".lower()
        if ".pdf" not in combined:
            continue
        score = 0
        for token in ["cgst", "central goods and services tax", "rules", "2017"]:
            if token in combined:
                score += 1
        if "rate" in combined and "rules" not in combined:
            score -= 1
        candidates.append(
            {
                "url": urljoin(base_url, href),
                "text": " ".join(text.split()),
                "score": score,
                "accepted": "rules" in combined and ("cgst" in combined or "central goods and services tax" in combined),
            }
        )
    return candidates


def _discover_pdf_url(base_url: str, html: bytes) -> str | None:
    candidates = [candidate for candidate in _pdf_candidates(base_url, html) if candidate["accepted"]]
    if not candidates:
        return None
    return sorted(candidates, key=lambda candidate: (candidate["score"], candidate["url"]))[-1]["url"]


def _write_rules_checkpoint(source_path: Path, output_dir: Path, observed_at: str) -> dict[str, Any]:
    _text, components = parse_rules_pdf_deterministic(source_path)
    checkpoint_path = output_dir / "checkpoint.xml"
    write_xml(
        _render_components_xml(
            components,
            work_id=RULES_WORK,
            title="Central Goods and Services Tax Rules, 2017",
            document_type="rules",
            base_as_of=observed_at[:10],
        ),
        checkpoint_path,
    )
    components_path = output_dir / "checkpoint_components.jsonl"
    components_path.write_text(
        "\n".join(json.dumps(component.to_json(), ensure_ascii=False, sort_keys=True) for component in components) + "\n",
        encoding="utf-8",
    )
    return {
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_components": str(components_path),
        "checkpoint_component_count": len(components),
        "checkpoint_source_type": "pdf_parse",
    }


def _rule_component_id(section_no: str) -> str | None:
    match = re.search(r"\bRule\s+(\d+[A-Za-z]?)\b", section_no, flags=re.I)
    if not match:
        return None
    return f"{RULES_WORK}/rule/{match.group(1).lower()}"


def _html_fragment_text(data: bytes) -> str:
    parser = _HtmlTextParser()
    parser.feed(data.decode("utf-8", errors="ignore"))
    return parser.text()


def _safe_html_name(content_path: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", content_path)


def _write_taxinformation_rules_checkpoint(
    *,
    rule_id: str,
    output_dir: Path,
    observed_at: str,
    timeout: int,
    verify_tls: bool,
    section_limit: int | None = None,
    section_timeout: int | None = None,
    section_concurrency: int = 8,
) -> dict[str, Any]:
    sections_url = f"https://taxinformation.cbic.gov.in/api/cbic-rule-section-msts/viewBySectionAllRules/{rule_id}"
    sections_data, sections_content_type = _fetch_bytes(sections_url, timeout, verify_tls=verify_tls)
    if "json" not in sections_content_type.lower():
        raise ValueError(f"taxinformation_sections_not_json:{sections_content_type}")
    sections = json.loads(sections_data.decode("utf-8"))
    if not isinstance(sections, list):
        raise ValueError("taxinformation_sections_not_list")
    raw_sections_path = output_dir / "taxinformation_sections.json"
    raw_sections_path.write_bytes(sections_data)
    html_dir = output_dir / "html"
    if html_dir.exists():
        shutil.rmtree(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    selected_sections: list[tuple[int, dict[str, Any], str, str]] = []
    skipped: list[dict[str, Any]] = []
    limit_reached = False
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_no = str(section.get("sectionNo") or "")
        component_id = _rule_component_id(section_no)
        if not component_id:
            continue
        if section_limit is not None and len(selected_sections) >= section_limit:
            limit_reached = True
            break
        content_path = str(section.get("contentFilePath") or "").replace("\\", "/").strip("/")
        if not content_path:
            skipped.append({"id": section.get("id"), "sectionNo": section_no, "reason": "missing_content_path"})
            continue
        selected_sections.append((index, section, component_id, content_path))

    def fetch_section(item: tuple[int, dict[str, Any], str, str]) -> tuple[int, BaselineComponent | None, dict[str, Any] | None]:
        index, section, component_id, content_path = item
        section_no = str(section.get("sectionNo") or "")
        html_url = "https://taxinformation.cbic.gov.in/content/html/" + content_path
        try:
            html_data, html_content_type = _fetch_bytes(
                html_url,
                section_timeout or min(timeout, 10),
                verify_tls=verify_tls,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            return index, None, {"id": section.get("id"), "sectionNo": section_no, "reason": str(exc)}
        text = _html_fragment_text(html_data)
        if not text or "request rejected" in text.lower():
            return index, None, {"id": section.get("id"), "sectionNo": section_no, "reason": "html_unusable"}
        safe_name = _safe_html_name(content_path)
        (html_dir / safe_name).write_bytes(html_data)
        label = re.search(r"\d+[A-Za-z]?", section_no)
        return index, (
            BaselineComponent(
                component_id=component_id,
                label=label.group(0) if label else section_no,
                heading=re.sub(r"\s+", " ", str(section.get("sectionName") or "")).strip(),
                text=text,
                component_type="rule",
            )
        ), None

    results: list[tuple[int, BaselineComponent]] = []
    concurrency = max(1, int(section_concurrency or 1))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(fetch_section, item) for item in selected_sections]
        for future in as_completed(futures):
            index, component, skip = future.result()
            if skip:
                skipped.append(skip)
            elif component:
                results.append((index, component))
    components = [component for _index, component in sorted(results, key=lambda item: item[0])]
    if not components:
        raise ValueError("taxinformation_no_rule_components")
    checkpoint_path = output_dir / "checkpoint.xml"
    write_xml(
        _render_components_xml(
            components,
            work_id=RULES_WORK,
            title="Central Goods and Services Tax Rules, 2017",
            document_type="rules",
            base_as_of=observed_at[:10],
        ),
        checkpoint_path,
    )
    components_path = output_dir / "checkpoint_components.jsonl"
    components_path.write_text(
        "\n".join(json.dumps(component.to_json(), ensure_ascii=False, sort_keys=True) for component in components) + "\n",
        encoding="utf-8",
    )
    skipped_path = output_dir / "taxinformation_skipped_sections.json"
    skipped_path.write_text(json.dumps(skipped, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    warnings: list[str] = []
    if limit_reached:
        warnings.append("taxinformation_section_limit_reached")
    if skipped:
        warnings.append("taxinformation_sections_skipped")
    return {
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_components": str(components_path),
        "checkpoint_component_count": len(components),
        "checkpoint_source_type": "taxinformation_section_html",
        "taxinformation_sections_url": sections_url,
        "taxinformation_sections_sha256": sha256_text(sections_data.decode("utf-8", errors="replace")),
        "taxinformation_skipped_sections": str(skipped_path),
        "taxinformation_skipped_count": len(skipped),
        "taxinformation_section_limit": section_limit,
        "taxinformation_section_limit_reached": limit_reached,
        "taxinformation_section_concurrency": concurrency,
        "warnings": warnings,
    }


def _write_taxinformation_checkpoint_from_downloaded_html(
    *,
    source_dir: Path,
    output_dir: Path,
    observed_at: str | None,
) -> dict[str, Any] | None:
    sections_path = source_dir / "taxinformation_sections.json"
    html_dir = source_dir / "html"
    if not sections_path.exists() or not html_dir.exists():
        return None
    sections = json.loads(sections_path.read_text(encoding="utf-8"))
    if not isinstance(sections, list):
        raise ValueError("taxinformation_sections_not_list")
    components: list[tuple[int, BaselineComponent]] = []
    skipped: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_no = str(section.get("sectionNo") or "")
        component_id = _rule_component_id(section_no)
        if not component_id:
            continue
        content_path = str(section.get("contentFilePath") or "").replace("\\", "/").strip("/")
        if not content_path:
            skipped.append({"id": section.get("id"), "sectionNo": section_no, "reason": "missing_content_path"})
            continue
        html_path = html_dir / _safe_html_name(content_path)
        if not html_path.exists():
            skipped.append({"id": section.get("id"), "sectionNo": section_no, "reason": "downloaded_html_missing"})
            continue
        text = _html_fragment_text(html_path.read_bytes())
        if not text or "request rejected" in text.lower():
            skipped.append({"id": section.get("id"), "sectionNo": section_no, "reason": "html_unusable"})
            continue
        label = re.search(r"\d+[A-Za-z]?", section_no)
        components.append(
            (
                index,
                BaselineComponent(
                    component_id=component_id,
                    label=label.group(0) if label else section_no,
                    heading=re.sub(r"\s+", " ", str(section.get("sectionName") or "")).strip(),
                    text=text,
                    component_type="rule",
                ),
            )
        )
    if not components:
        raise ValueError("downloaded_taxinformation_no_rule_components")
    ordered_components = [component for _index, component in sorted(components, key=lambda item: item[0])]
    checkpoint_path = output_dir / "checkpoint.xml"
    write_xml(
        _render_components_xml(
            ordered_components,
            work_id=RULES_WORK,
            title="Central Goods and Services Tax Rules, 2017",
            document_type="rules",
            base_as_of=(observed_at or "")[:10] or "unknown",
        ),
        checkpoint_path,
    )
    components_path = output_dir / "checkpoint_components.jsonl"
    components_path.write_text(
        "\n".join(json.dumps(component.to_json(), ensure_ascii=False, sort_keys=True) for component in ordered_components)
        + "\n",
        encoding="utf-8",
    )
    skipped_path = output_dir / "taxinformation_skipped_sections.json"
    skipped_path.write_text(json.dumps(skipped, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_components": str(components_path),
        "checkpoint_component_count": len(ordered_components),
        "checkpoint_source_type": "taxinformation_downloaded_section_html",
        "taxinformation_skipped_sections": str(skipped_path),
        "taxinformation_skipped_count": len(skipped),
        "rebuilt_from_downloaded_html": True,
    }


def _read_checkpoint_component_labels(components_path: Path) -> dict[str, Any]:
    labels: set[str] = set()
    component_ids: set[str] = set()
    count = 0
    if not components_path.exists():
        return {"component_count": 0, "labels": [], "component_ids": []}
    for line in components_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            component = json.loads(line)
        except json.JSONDecodeError:
            continue
        count += 1
        label = str(component.get("label") or "").strip()
        component_id = str(component.get("component_id") or "").strip()
        if label:
            labels.add(label.upper())
        if component_id:
            component_ids.add(component_id)
    return {
        "component_count": count,
        "labels": sorted(labels),
        "component_ids": sorted(component_ids),
    }


def alias_downloaded_checkpoint(
    *,
    source_dir: Path,
    output_dir: Path,
    checkpoint_date: str,
    source_label: str = "current-cbic-taxinformation",
    required_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Copy an already-downloaded checkpoint into a correctly named source directory."""
    checkpoint_path = source_dir / "checkpoint.xml"
    components_path = source_dir / "checkpoint_components.jsonl"
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"checkpoint.xml not found in {source_dir}")
    if not components_path.exists():
        raise FileNotFoundError(f"checkpoint_components.jsonl not found in {source_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    copied_files: list[str] = []
    for name in [
        "checkpoint.xml",
        "checkpoint_components.jsonl",
        "fetch_manifest.json",
        "taxinformation_sections.json",
        "taxinformation_skipped_sections.json",
        "consolidated.pdf",
        "consolidated.bin",
    ]:
        source = source_dir / name
        if not source.exists():
            continue
        target = output_dir / name
        shutil.copy2(source, target)
        copied_files.append(str(target))
    source_html = source_dir / "html"
    if source_html.exists():
        target_html = output_dir / "html"
        if target_html.exists():
            shutil.rmtree(target_html)
        shutil.copytree(source_html, target_html)
        copied_files.append(str(target_html))

    source_manifest = {}
    source_manifest_path = source_dir / "fetch_manifest.json"
    if source_manifest_path.exists():
        try:
            source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            source_manifest = {"manifest_parse_error": str(source_manifest_path)}
    rebuilt_checkpoint = _write_taxinformation_checkpoint_from_downloaded_html(
        source_dir=output_dir,
        output_dir=output_dir,
        observed_at=source_manifest.get("observed_at"),
    )
    if rebuilt_checkpoint:
        copied_files.extend(
            [
                rebuilt_checkpoint["checkpoint_path"],
                rebuilt_checkpoint["checkpoint_components"],
                rebuilt_checkpoint["taxinformation_skipped_sections"],
            ]
        )
    component_summary = _read_checkpoint_component_labels(output_dir / "checkpoint_components.jsonl")
    required = [label.upper() for label in (required_labels or ["31C", "88C"])]
    present_labels = set(component_summary["labels"])
    manifest = {
        "ok": all(label in present_labels for label in required),
        "source_label": source_label,
        "source_dir": str(source_dir),
        "output_dir": str(output_dir),
        "checkpoint_path": str(output_dir / "checkpoint.xml"),
        "checkpoint_components": str(output_dir / "checkpoint_components.jsonl"),
        "checkpoint_date": checkpoint_date,
        "checkpoint_source_type": (rebuilt_checkpoint or {}).get("checkpoint_source_type")
        or source_manifest.get("checkpoint_source_type")
        or "downloaded_checkpoint_alias",
        "observed_at": source_manifest.get("observed_at"),
        "source_url": source_manifest.get("source_url"),
        "source_sha256": source_manifest.get("source_sha256"),
        "required_labels": required,
        "present_required_labels": sorted(label for label in required if label in present_labels),
        "missing_required_labels": sorted(label for label in required if label not in present_labels),
        "checkpoint_component_count": component_summary["component_count"],
        "rebuilt_from_downloaded_html": bool(rebuilt_checkpoint),
        "taxinformation_skipped_count": (rebuilt_checkpoint or {}).get("taxinformation_skipped_count"),
        "copied_files": copied_files,
        "source_manifest_path": str(source_manifest_path) if source_manifest_path.exists() else None,
    }
    (output_dir / "checkpoint_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def fetch_consolidated(
    *,
    target_work: str,
    url: str,
    output_dir: Path,
    timeout: int = 60,
    verify_tls: bool = True,
    section_limit: int | None = None,
    section_timeout: int | None = None,
    section_concurrency: int = 8,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in ["checkpoint.xml", "checkpoint_components.jsonl"]:
        stale = output_dir / stale_name
        if stale.exists():
            stale.unlink()
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        landing_data, landing_content_type = _fetch_bytes(url, timeout, verify_tls=verify_tls)
    except (urllib.error.URLError, TimeoutError) as exc:
        manifest = {
            "ok": False,
            "target_work": target_work,
            "landing_url": url,
            "source_url": url,
            "discovered_pdf_url": None,
            "observed_at": observed_at,
            "tls_verify": verify_tls,
            "fetch_error": str(exc),
        }
        (output_dir / "fetch_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return manifest
    data = landing_data
    content_type = landing_content_type
    source_url = url
    discovered_pdf_url = None
    scanned_pdf_candidates: list[dict[str, Any]] = []
    fetch_error = None
    source_metadata: dict[str, Any] = {}
    json_pdf = _decode_json_pdf(data, content_type)
    if json_pdf:
        data, source_metadata = json_pdf
        content_type = "application/pdf"
    if not _looks_like_pdf(content_type, url, data):
        scanned_pdf_candidates = _pdf_candidates(url, data)
        discovered_pdf_url = _discover_pdf_url(url, data)
        if discovered_pdf_url:
            try:
                data, content_type = _fetch_bytes(discovered_pdf_url, timeout, verify_tls=verify_tls)
                source_url = discovered_pdf_url
                json_pdf = _decode_json_pdf(data, content_type)
                if json_pdf:
                    data, source_metadata = json_pdf
                    content_type = "application/pdf"
            except (urllib.error.URLError, TimeoutError) as exc:
                fetch_error = str(exc)
    suffix = ".pdf" if _looks_like_pdf(content_type, source_url, data) else ".bin"
    source_path = output_dir / ("consolidated" + suffix)
    source_path.write_bytes(data)
    checkpoint_error = None
    if target_work == RULES_WORK and not _looks_like_pdf(content_type, source_url, data):
        checkpoint_error = "no_rules_pdf_discovered"
    manifest = {
        "ok": fetch_error is None and checkpoint_error is None,
        "target_work": target_work,
        "landing_url": url,
        "source_url": source_url,
        "discovered_pdf_url": discovered_pdf_url,
        "observed_at": observed_at,
        "tls_verify": verify_tls,
        "content_type": content_type,
        "source_path": str(source_path),
        "source_sha256": sha256_bytes(data),
        "bytes": len(data),
    }
    if source_metadata:
        manifest["source_metadata"] = source_metadata
    if scanned_pdf_candidates:
        manifest["scanned_pdf_candidates"] = scanned_pdf_candidates[:50]
    if fetch_error:
        manifest["fetch_error"] = fetch_error
    if checkpoint_error:
        manifest["checkpoint_error"] = checkpoint_error
    if target_work == RULES_WORK and _looks_like_pdf(content_type, source_url, data):
        try:
            manifest.update(_write_rules_checkpoint(source_path, output_dir, observed_at))
        except Exception as exc:
            manifest["checkpoint_error"] = str(exc)
    rule_download_match = re.search(r"taxinformation\.cbic\.gov\.in/api/cbic-rule-msts/download/(\d+)", source_url)
    if target_work == RULES_WORK and rule_download_match:
        try:
            manifest.update(
                _write_taxinformation_rules_checkpoint(
                    rule_id=rule_download_match.group(1),
                    output_dir=output_dir,
                    observed_at=observed_at,
                    timeout=timeout,
                    verify_tls=verify_tls,
                    section_limit=section_limit,
                    section_timeout=section_timeout,
                    section_concurrency=section_concurrency,
                )
            )
            manifest.pop("checkpoint_error", None)
            manifest["ok"] = fetch_error is None
        except Exception as exc:
            manifest["taxinformation_checkpoint_error"] = str(exc)
    (output_dir / "fetch_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


__all__ = [
    "DEFAULT_RULES_CONSOLIDATED_URL",
    "alias_downloaded_checkpoint",
    "fetch_consolidated",
    "sha256_bytes",
]
