#!/usr/bin/env python3
"""Discovery-only India Kanoon crawler for GST case-law candidates.

This script is intentionally conservative. It records search-result metadata
and optional lightweight document pages as discovery leads only; it does not
create canonical corpus evidence and does not replace official court/tribunal
PDF archival.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://indiankanoon.org"
DEFAULT_OUTPUT_DIR = Path("data/caselaw/indiakanoon_discovery")
DEFAULT_QUERY = '"GST" "CGST Act"'
DEFAULT_DELAY = 45.0
DEFAULT_JITTER = 8.0
DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class StopRun(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"[^0-9A-Za-z]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-").lower() or "untitled"


def normalize_text(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


class _SearchResultParser(HTMLParser):
    """Small parser for India Kanoon search result pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self.next_href: str | None = None
        self._active: dict[str, Any] | None = None
        self._capture: str | None = None
        self._chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        href = attr.get("href", "")
        text_class = attr.get("class", "")
        if tag == "a" and href:
            if re.search(r"/doc(?:fragment)?/\d+", href):
                if self._active:
                    self._finish_result()
                self._active = {"href": href, "title": "", "snippet": ""}
                self._capture = "title"
                self._chunks = []
                self._depth = 1
                return
            if "next" in normalize_text(attr.get("title", "")).lower() or "page" in href.lower():
                # Kept as a hint only; the caller also supports explicit page
                # offsets, so failure to detect this is harmless.
                if "pagenum=" in href or "page=" in href:
                    self.next_href = href
        if self._active and tag in {"div", "p"} and ("headline" in text_class or "snippet" in text_class):
            self._capture = "snippet"
            self._chunks = []
            self._depth = 1
            return
        if self._capture:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._capture:
            return
        self._depth -= 1
        if self._depth <= 0:
            text = normalize_text(" ".join(self._chunks))
            if self._active is not None:
                if self._capture == "title":
                    self._active["title"] = text
                elif self._capture == "snippet":
                    existing = self._active.get("snippet", "")
                    self._active["snippet"] = (existing + " " + text).strip()
            self._capture = None
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._chunks.append(data)

    def close(self) -> None:
        super().close()
        if self._active:
            self._finish_result()

    def _finish_result(self) -> None:
        if not self._active:
            return
        href = str(self._active.get("href", ""))
        title = normalize_text(str(self._active.get("title", "")))
        snippet = normalize_text(str(self._active.get("snippet", "")))
        if href and title:
            self.results.append({"href": href, "title": title, "snippet": snippet})
        self._active = None


class _DocMetaParser(HTMLParser):
    """Extract basic metadata and visible text hints from a document page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.h1 = ""
        self.meta: dict[str, str] = {}
        self._capture: str | None = None
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.visible_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag == "meta":
            key = attr.get("name") or attr.get("property")
            if key:
                self.meta[key] = attr.get("content", "")
        if tag in {"title", "h1"}:
            self._capture = tag
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if self._capture == tag:
            text = normalize_text(" ".join(self._chunks))
            if tag == "title":
                self.title = text
            elif tag == "h1":
                self.h1 = text
            self._capture = None
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._chunks.append(data)
        if not self._skip_depth:
            text = normalize_text(data)
            if text:
                self.visible_chunks.append(text)


@dataclass
class CrawlConfig:
    output_dir: Path
    delay: float
    jitter: float
    timeout: int
    user_agent: str
    fetch_docs: bool
    dry_run: bool


class IndiaKanoonDiscoveryClient:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._last_request_at = 0.0

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        target = self.config.delay + random.uniform(0, self.config.jitter)
        if elapsed < target:
            time.sleep(target - elapsed)

    def fetch_text(self, url: str) -> tuple[int, str, str]:
        if self.config.dry_run:
            return 0, "", ""
        self._pace()
        req = Request(url, headers={"User-Agent": self.config.user_agent})
        try:
            with urlopen(req, timeout=self.config.timeout) as response:
                body = response.read()
                final_url = response.geturl()
                status = int(getattr(response, "status", 200))
        except Exception as exc:
            self._last_request_at = time.monotonic()
            raise StopRun(f"request failed for {url}: {exc}") from exc
        self._last_request_at = time.monotonic()
        return status, body.decode("utf-8", errors="replace"), final_url


def search_url(query: str, page: int) -> str:
    params = f"formInput={quote_plus(query)}"
    if page > 0:
        params += f"&pagenum={page}"
    return f"{BASE_URL}/search/?{params}"


def doc_id_from_href(href: str) -> str:
    match = re.search(r"/doc(?:fragment)?/(\d+)", href)
    return match.group(1) if match else hashlib.sha256(href.encode("utf-8")).hexdigest()[:16]


def parse_search_results(html_text: str) -> list[dict[str, str]]:
    parser = _SearchResultParser()
    parser.feed(html_text)
    parser.close()
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for item in parser.results:
        doc_id = doc_id_from_href(item["href"])
        if doc_id in seen:
            continue
        seen.add(doc_id)
        item = dict(item)
        item["doc_id"] = doc_id
        item["url"] = urljoin(BASE_URL, item["href"])
        results.append(item)
    return results


def parse_doc_page(html_text: str) -> dict[str, Any]:
    parser = _DocMetaParser()
    parser.feed(html_text)
    parser.close()
    visible_text = normalize_text(" ".join(parser.visible_chunks))
    return {
        "title": parser.h1 or parser.title,
        "html_title": parser.title,
        "meta": parser.meta,
        "visible_text_sample": visible_text[:2000],
        "gst_signal_count": len(re.findall(r"\b(?:GST|CGST|SGST|IGST)\b|Goods and Services Tax", visible_text, re.I)),
    }


def load_existing_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        doc_id = str(item.get("doc_id") or "")
        if doc_id:
            rows[doc_id] = item
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    tmp.replace(path)


def discover(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    query_slug = slugify(args.query)
    run_dir = output_dir / query_slug
    raw_dir = run_dir / "raw_html"
    results_path = run_dir / "results.jsonl"
    manifest_path = run_dir / "manifest.json"
    config = CrawlConfig(
        output_dir=output_dir,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        user_agent=args.user_agent,
        fetch_docs=args.fetch_docs,
        dry_run=args.dry_run,
    )
    client = IndiaKanoonDiscoveryClient(config)
    existing = load_existing_jsonl(results_path)
    events: list[dict[str, Any]] = []
    requests_used = 0

    if args.dry_run:
        urls = [search_url(args.query, page) for page in range(args.pages)]
        return {"dry_run": True, "query": args.query, "planned_search_urls": urls}

    for page in range(args.pages):
        if requests_used >= args.max_requests:
            break
        url = search_url(args.query, page)
        status, html_text, final_url = client.fetch_text(url)
        requests_used += 1
        page_path = raw_dir / f"search_page_{page}.html"
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(html_text, encoding="utf-8")
        page_hash = sha256_text(html_text)
        results = parse_search_results(html_text)
        events.append(
            {
                "at": utc_now(),
                "event": "search_page_fetched",
                "page": page,
                "status": status,
                "url": url,
                "final_url": final_url,
                "html_sha256": page_hash,
                "result_count": len(results),
            }
        )
        for rank, item in enumerate(results, start=1):
            if len(existing) >= args.limit:
                break
            doc_id = item["doc_id"]
            if doc_id in existing:
                continue
            row: dict[str, Any] = {
                "doc_id": doc_id,
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "indiakanoon_url": item.get("url", ""),
                "search_query": args.query,
                "search_page": page,
                "search_rank": rank,
                "discovery_source": "indiakanoon_search",
                "evidence_source": False,
                "official_resolution_status": "not_started",
                "created_at": utc_now(),
            }
            if args.fetch_docs and requests_used < args.max_requests:
                status, doc_html, final_doc_url = client.fetch_text(row["indiakanoon_url"])
                requests_used += 1
                doc_path = raw_dir / f"doc_{doc_id}.html"
                doc_path.write_text(doc_html, encoding="utf-8")
                doc_meta = parse_doc_page(doc_html)
                row.update(
                    {
                        "doc_fetch_status": status,
                        "doc_final_url": final_doc_url,
                        "doc_html_sha256": sha256_text(doc_html),
                        "doc_raw_html_path": str(doc_path),
                        "doc_metadata": doc_meta,
                    }
                )
            existing[doc_id] = row
        if len(existing) >= args.limit:
            break

    rows = list(existing.values())
    rows.sort(key=lambda item: (item.get("search_page", 0), item.get("search_rank", 0), item.get("doc_id", "")))
    write_jsonl(results_path, rows)
    manifest = {
        "created_or_updated_at": utc_now(),
        "query": args.query,
        "query_slug": query_slug,
        "base_url": BASE_URL,
        "output_dir": str(run_dir),
        "results_path": str(results_path),
        "result_count": len(rows),
        "requests_used_this_run": requests_used,
        "delay_seconds": args.delay,
        "jitter_seconds": args.jitter,
        "fetch_docs": args.fetch_docs,
        "discovery_only": True,
        "canonical_evidence_created": False,
        "events": events,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--limit", type=int, default=5, help="Maximum unique discovery candidates to keep.")
    parser.add_argument("--pages", type=int, default=1, help="Search result pages to fetch.")
    parser.add_argument("--max-requests", type=int, default=6, help="Hard request budget for this run.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Minimum seconds between requests.")
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER, help="Random extra delay added to each request.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--fetch-docs", action="store_true", help="Fetch individual India Kanoon document pages as discovery metadata.")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    manifest = discover(args)
    print(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
