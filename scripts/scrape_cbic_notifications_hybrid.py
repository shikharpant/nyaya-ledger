#!/usr/bin/env python3
"""Hybrid CBIC notification scraper.

This combines the fast GST notification listing API with the existing
ID-range crawler shape used by scripts/scrape_cbic_tax_portal.py.

Output is the project JSON format in:
  data/Law/cbic_tax_portal/notifications/

The scraper is resumable and skips notification JSON files that already have
contentHtml or contentPdfBase64.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import requests
    import urllib3
except ImportError as exc:  # pragma: no cover - dependency should exist locally
    raise SystemExit(f"Missing dependency: {exc}. Install requests to run this scraper.")


BASE_URL = "https://taxinformation.cbic.gov.in"
TOKEN_URL = f"{BASE_URL}/api/authenticate-token"
BULK_API_URL = f"{BASE_URL}/api/cbic-notification-msts/fetchNotificationByYearAndCategory"
DETAIL_API_URL = f"{BASE_URL}/api/cbic-notification-msts"
TAX_ID_GST = 1000001

STATE_FILE = Path("data/Law/cbic_scrape_state.json")
OUTPUT_DIR = Path("data/Law/cbic_tax_portal")
NOTIFICATION_DIR = OUTPUT_DIR / "notifications"

DEFAULT_ID_START = 1000001
DEFAULT_ID_END = 1010700
DEFAULT_PAGE_SIZE = 200

GST_CATEGORIES: list[tuple[str, str]] = [
    ("Central Tax", "Central_Tax"),
    ("Central Tax (Rate)", "Central_Tax_Rate"),
    ("Integrated Tax", "Integrated_Tax"),
    ("Integrated Tax (Rate)", "Integrated_Tax_Rate"),
    ("Union Territory Tax", "Union_Territory_Tax"),
    ("Union Territory Tax (Rate)", "Union_Territory_Tax_Rate"),
    ("Compensation Cess", "Compensation_Cess"),
    ("Compensation Cess (Rate)", "Compensation_Cess_Rate"),
]


class StopRun(Exception):
    pass


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag in {"script", "style"}:
            self._skip_depth += 1
        if tag in {"br", "p", "div", "tr", "li"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "tr", "li"}:
            self._parts.append("\n")

    def handle_data(self, data: str):
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def extract_text(html: str | None) -> str:
    if not html:
        return ""
    parser = HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def slugify(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-") or "untitled"


def compact(value: str, limit: int = 60) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:limit]


def has_content(record: dict[str, Any]) -> bool:
    return bool(record.get("contentHtml") or record.get("contentPdfBase64"))


def is_probably_html(data: bytes, content_type: str) -> bool:
    stripped = data.lstrip()[:100].lower()
    return "html" in content_type or stripped.startswith((b"<html", b"<!doctype", b"<div", b"<p"))


def is_probably_pdf(data: bytes, content_type: str) -> bool:
    return data.startswith(b"%PDF-") or "pdf" in content_type or "octet-stream" in content_type


def normalize_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def maybe_base64_pdf(value: str) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if "base64," in raw:
        raw = raw.split("base64,", 1)[1]
    if raw.startswith("%PDF-"):
        return base64.b64encode(raw.encode("latin1", errors="ignore")).decode("ascii")
    try:
        sample = base64.b64decode(raw[: min(len(raw), 4096)], validate=False)
    except (binascii.Error, ValueError):
        return None
    if sample.startswith(b"%PDF-") or len(raw) > 1000:
        return raw
    return None


def atomic_write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def find_json_for_id(notification_id: str) -> Path | None:
    if not NOTIFICATION_DIR.exists():
        return None
    matches = list(NOTIFICATION_DIR.glob(f"*_{notification_id}.json"))
    return matches[0] if matches else None


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def bucket(self, category: str) -> dict[str, Any]:
        bucket = self.data.setdefault(category, {})
        if not isinstance(bucket, dict):
            bucket = {}
            self.data[category] = bucket
        return bucket

    def is_done(self, category: str, key: str) -> bool:
        return key in self.bucket(category)

    def mark_done(self, category: str, key: str, value: Any = "done"):
        self.bucket(category)[key] = value

    def count(self, category: str) -> int:
        return len(self.bucket(category))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        atomic_write_json(self.path, self.data)


@dataclass
class ClientConfig:
    request_delay: float
    retries: int
    timeout: int
    cooldown: int
    manual_token: str = ""


class CBICSession:
    def __init__(self, config: ClientConfig):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.config = config
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE_URL}/content-page/explore-notification",
            "language": "en",
        })
        self._token: str | None = None
        self._token_time = 0.0
        self._last_request_at = 0.0

    def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.config.request_delay:
            time.sleep(self.config.request_delay - elapsed)

    def _mark_request(self):
        self._last_request_at = time.monotonic()

    def authenticate(self, force: bool = False) -> str:
        if self.config.manual_token:
            self._token = self.config.manual_token
            self.session.headers["Authorization1"] = f"homeToken {self._token}"
            return self._token
        if not force and self._token and (time.time() - self._token_time) < 900:
            return self._token

        last_error = ""
        for attempt in range(1, self.config.retries + 2):
            self._pace()
            try:
                resp = self.session.post(
                    TOKEN_URL,
                    json=None,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/plain, */*",
                    },
                    timeout=self.config.timeout,
                )
                self._mark_request()
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(min(5 * attempt, 30))
                continue

            if resp.status_code == 200:
                try:
                    token = resp.json().get("id_token")
                except ValueError:
                    token = None
                if token:
                    self._token = token
                    self._token_time = time.time()
                    self.session.headers["Authorization1"] = f"homeToken {token}"
                    return token
                last_error = "token response missing id_token"
            elif resp.status_code in {403, 429, 503}:
                raise StopRun(f"authentication blocked with HTTP {resp.status_code}")
            else:
                last_error = f"authentication HTTP {resp.status_code}"
                time.sleep(min(5 * attempt, 30))

        raise StopRun(f"authentication failed: {last_error}")

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response | None:
        if not url.startswith("http"):
            url = f"{BASE_URL}/{url.lstrip('/')}"
        for attempt in range(1, self.config.retries + 2):
            self.authenticate()
            self._pace()
            try:
                resp = self.session.request(method, url, timeout=self.config.timeout, **kwargs)
                self._mark_request()
            except requests.Timeout:
                if attempt <= self.config.retries:
                    time.sleep(min(2 * attempt, 20))
                    continue
                return None
            except requests.ConnectionError:
                if attempt <= self.config.retries:
                    time.sleep(min(3 * attempt, 30))
                    continue
                return None
            except requests.RequestException:
                if attempt <= self.config.retries:
                    time.sleep(min(3 * attempt, 30))
                    continue
                return None

            if resp.status_code == 401 and attempt <= self.config.retries + 1:
                self.authenticate(force=True)
                continue
            if resp.status_code in {403, 429, 503}:
                print(f"  [cooldown {self.config.cooldown}s after HTTP {resp.status_code}]", flush=True)
                time.sleep(self.config.cooldown)
                if attempt <= self.config.retries:
                    continue
                raise StopRun(f"blocked with HTTP {resp.status_code} for {url}")
            return resp
        return None

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("GET", url, params=params)
        if resp is None:
            raise StopRun(f"request failed for {url}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise StopRun(f"HTTP {resp.status_code} for {url}")
        if not resp.content.strip():
            return None
        try:
            return resp.json()
        except ValueError:
            raise StopRun(f"non-JSON response for {url}")

    def bulk_notifications(self, category: str, year: int, page: int, size: int) -> list[dict[str, Any]]:
        params = {
            "year": year,
            "page": page,
            "size": size,
            "taxId": TAX_ID_GST,
            "category": category,
        }
        data = self.get_json(BULK_API_URL, params=params)
        return data if isinstance(data, list) else []

    def notification_detail(self, notification_id: int) -> dict[str, Any] | None:
        data = self.get_json(f"{DETAIL_API_URL}/{notification_id}")
        return data if isinstance(data, dict) else None

    def get_content_html(self, content_file_path: str) -> str | None:
        path = normalize_path(content_file_path)
        if not path:
            return None
        resp = self.request("GET", f"{BASE_URL}/content/html/{quote(path, safe='/')}")
        if resp is None or resp.status_code in {403, 404, 500}:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        if is_probably_html(resp.content, resp.headers.get("Content-Type", "").lower()):
            return resp.text
        return None

    def get_content_pdf(self, content_file_path: str) -> str | None:
        path = normalize_path(content_file_path)
        if not path:
            return None
        resp = self.request("GET", f"{BASE_URL}/content/pdf/{quote(path, safe='/')}")
        if resp is None or resp.status_code in {403, 404, 500}:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        parsed = parse_content_response(resp)
        return parsed.get("contentPdfBase64")

    def download_notification_content(self, record: dict[str, Any]) -> dict[str, Any]:
        content_path = record.get("contentFilePath") or ""
        if content_path:
            html = self.get_content_html(content_path)
            if html:
                return {"contentHtml": html, "contentPdfBase64": None}
            pdf_b64 = self.get_content_pdf(content_path)
            if pdf_b64:
                return {"contentHtml": None, "contentPdfBase64": pdf_b64}

        for url in candidate_notification_urls(record):
            resp = self.request("GET", url)
            if resp is None:
                continue
            if resp.status_code in {403, 404, 500}:
                continue
            if resp.status_code != 200 or len(resp.content) <= 20:
                continue
            parsed = parse_content_response(resp)
            if parsed.get("contentHtml") or parsed.get("contentPdfBase64"):
                return parsed

        return {"contentHtml": None, "contentPdfBase64": None}


def parse_content_response(resp: requests.Response) -> dict[str, Any]:
    content_type = resp.headers.get("Content-Type", "").lower()
    body = resp.content or b""
    if is_probably_pdf(body, content_type) and len(body) > 100:
        return {"contentHtml": None, "contentPdfBase64": base64.b64encode(body).decode("ascii")}

    if is_probably_html(body, content_type):
        return {"contentHtml": resp.text, "contentPdfBase64": None}

    if "json" in content_type or body.lstrip().startswith((b"{", b"[")):
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("data", "content", "file", "pdf", "fileContent", "pdfContent", "body", "docContent"):
                value = data.get(key)
                if not isinstance(value, str):
                    continue
                stripped = value.strip()
                if stripped.startswith("<"):
                    return {"contentHtml": stripped, "contentPdfBase64": None}
                pdf_b64 = maybe_base64_pdf(stripped)
                if pdf_b64:
                    return {"contentHtml": None, "contentPdfBase64": pdf_b64}

    return {"contentHtml": None, "contentPdfBase64": None}


def candidate_notification_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    record_id = record.get("id")
    content_id = record.get("contentId")
    doc_path = normalize_path(record.get("docFilePath") or "")
    if record_id:
        urls.append(f"{BASE_URL}/api/cbic-notification-msts/download/{record_id}/ENG")
    if content_id and str(content_id) != str(record_id):
        urls.append(f"{BASE_URL}/api/cbic-notification-msts/download/{content_id}/ENG")
    if doc_path:
        urls.append(f"{BASE_URL}/{quote(doc_path, safe='/')}")
    return urls


def notification_json_path(record: dict[str, Any]) -> Path:
    notification_id = str(record.get("id") or record.get("notificationId") or "")
    existing = find_json_for_id(notification_id)
    if existing:
        return existing
    no = record.get("notificationNo") or record.get("no") or ""
    name = record.get("notificationName") or record.get("name") or f"notification_{notification_id}"
    slug = slugify(f"{no} {name}")[:80] if no else slugify(name)[:80]
    return NOTIFICATION_DIR / f"{slug}_{notification_id}.json"


def normalize_notification(record: dict[str, Any], existing: dict[str, Any] | None = None,
                           content: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    content = content or {}
    content_html = content.get("contentHtml")
    content_pdf = content.get("contentPdfBase64")
    if not content_html and not content_pdf:
        content_html = existing.get("contentHtml")
        content_pdf = existing.get("contentPdfBase64")

    name = record.get("notificationName") or record.get("name") or existing.get("name") or ""
    no = record.get("notificationNo") or record.get("no") or existing.get("no") or ""
    category = record.get("notificationCategory") or record.get("category") or existing.get("category") or ""
    notification_id = record.get("id") or existing.get("id")

    return {
        "source": "cbic_tax_portal",
        "type": "notification",
        "name": name,
        "no": no,
        "id": notification_id,
        "category": category,
        "issueDt": (
            record.get("issueDt")
            or record.get("notificationDt")
            or existing.get("issueDt")
        ),
        "amendDt": record.get("amendDt", existing.get("amendDt")),
        "isActive": record.get("isActive", existing.get("isActive")),
        "contentFilePath": record.get("contentFilePath", existing.get("contentFilePath", "")) or "",
        "docFilePath": record.get("docFilePath", existing.get("docFilePath", "")) or "",
        "docFileType": record.get("docFileType", existing.get("docFileType", "")) or "",
        "contentId": record.get("contentId", existing.get("contentId", "")) or "",
        "taxId": record.get("taxId", existing.get("taxId", "")) or "",
        "contentHtml": content_html,
        "contentPdfBase64": content_pdf,
        "contentText": extract_text(content_html),
    }


def save_notification(record: dict[str, Any], state: State, content: dict[str, Any] | None = None) -> tuple[Path, bool]:
    notification_id = str(record.get("id") or "")
    path = notification_json_path(record)
    existing = load_json(path) if path.exists() else {}
    merged = normalize_notification(record, existing=existing, content=content)
    atomic_write_json(path, merged)
    state.mark_done("notifications", notification_id, {
        "name": compact(merged.get("name", "")),
        "no": merged.get("no", ""),
        "has_content": has_content(merged),
    })
    return path, has_content(merged)


def should_skip_content(record: dict[str, Any], force: bool = False) -> bool:
    if force:
        return False
    notification_id = str(record.get("id") or "")
    path = find_json_for_id(notification_id)
    if not path:
        return False
    return has_content(load_json(path))


def run_bulk_gst(client: CBICSession, state: State, args: argparse.Namespace) -> dict[str, int]:
    print("\n=== Bulk GST notifications ===", flush=True)
    total_indexed = 0
    saved = 0
    skipped_content = 0
    downloaded = 0
    failed_content = 0

    categories = GST_CATEGORIES
    if args.categories:
        wanted = {c.strip().lower() for c in args.categories.split(",") if c.strip()}
        categories = [item for item in GST_CATEGORIES if item[0].lower() in wanted or item[1].lower() in wanted]

    for category, _folder in categories:
        print(f"\n[category] {category}", flush=True)
        for year in range(args.end_year, args.start_year - 1, -1):
            page = 0
            year_count = 0
            while True:
                records = client.bulk_notifications(category, year, page, args.page_size)
                if not records:
                    break

                total_indexed += len(records)
                year_count += len(records)
                for record in records:
                    if args.max_records and saved >= args.max_records:
                        print("Reached --max-records", flush=True)
                        state.save()
                        return {
                            "indexed": total_indexed,
                            "saved": saved,
                            "downloaded": downloaded,
                            "skipped_content": skipped_content,
                            "failed_content": failed_content,
                        }
                    notification_id = str(record.get("id") or "")
                    content = {"contentHtml": None, "contentPdfBase64": None}
                    if args.download_content and not should_skip_content(record, args.force_content):
                        content = client.download_notification_content(record)
                        if content.get("contentHtml") or content.get("contentPdfBase64"):
                            downloaded += 1
                        else:
                            failed_content += 1
                        time.sleep(args.download_delay)
                    else:
                        skipped_content += 1

                    path, ok = save_notification(record, state, content=content)
                    saved += 1
                    if saved % args.save_every == 0:
                        state.save()
                    if saved % args.progress_every == 0:
                        print(
                            f"  saved={saved} downloaded={downloaded} skipped_content={skipped_content} "
                            f"latest_id={notification_id} file={path.name}",
                            flush=True,
                        )

                if len(records) < args.page_size:
                    break
                page += 1

            if year_count:
                state.mark_done("notifications_bulk_gst_years", f"{category}|{year}", {"count": year_count})
                state.save()
                print(f"  {year}: {year_count}", flush=True)

    state.save()
    return {
        "indexed": total_indexed,
        "saved": saved,
        "downloaded": downloaded,
        "skipped_content": skipped_content,
        "failed_content": failed_content,
    }


def run_content_fill(client: CBICSession, state: State, args: argparse.Namespace) -> dict[str, int]:
    print("\n=== Notification content fill ===", flush=True)
    files = sorted(NOTIFICATION_DIR.glob("*.json"))
    checked = updated = skipped = failed = 0

    for path in files:
        if args.max_records and updated >= args.max_records:
            break
        record = load_json(path)
        if record.get("type") != "notification":
            continue
        checked += 1
        notification_id = str(record.get("id") or "")
        if has_content(record) and not args.force_content:
            skipped += 1
            continue
        if not (record.get("contentFilePath") or record.get("docFilePath") or record.get("contentId") or record.get("id")):
            failed += 1
            continue
        content = client.download_notification_content(record)
        if content.get("contentHtml") or content.get("contentPdfBase64"):
            merged = normalize_notification(record, existing=record, content=content)
            atomic_write_json(path, merged)
            state.mark_done("notifications", notification_id, {
                "name": compact(merged.get("name", "")),
                "no": merged.get("no", ""),
                "has_content": True,
            })
            updated += 1
        else:
            state.mark_done("notifications_content_failed", notification_id, {
                "name": compact(record.get("name", "")),
                "no": record.get("no", ""),
            })
            failed += 1
        if (updated + failed) % args.save_every == 0:
            state.save()
        if (updated + failed) % args.progress_every == 0:
            print(f"  checked={checked} updated={updated} skipped={skipped} failed={failed}", flush=True)
        time.sleep(args.download_delay)

    state.save()
    return {"checked": checked, "updated": updated, "skipped": skipped, "failed": failed}


def run_id_fill(client: CBICSession, state: State, args: argparse.Namespace) -> dict[str, int]:
    print(f"\n=== Notification ID fill {args.id_start}-{args.id_end} ===", flush=True)
    found = missing = saved = downloaded = skipped = transient_failed = 0

    for notification_id in range(args.id_start, args.id_end + 1):
        key = str(notification_id)
        if state.is_done("notifications", key) and find_json_for_id(key):
            skipped += 1
            continue
        if state.is_done("notifications_missing", key) and not args.retry_missing:
            skipped += 1
            continue
        if args.max_records and saved >= args.max_records:
            break

        try:
            record = client.notification_detail(notification_id)
        except StopRun as exc:
            message = str(exc)
            if "blocked with HTTP" in message or "authentication" in message:
                raise
            state.mark_done("notifications_transient_failed", key, message[:200])
            state.save()
            transient_failed += 1
            print(f"  [transient] id={notification_id}: {message}", flush=True)
            time.sleep(args.transient_delay)
            continue
        if not record:
            state.mark_done("notifications_missing", key, "null")
            missing += 1
            if missing % args.save_every == 0:
                state.save()
            continue

        found += 1
        content = {"contentHtml": None, "contentPdfBase64": None}
        if args.download_content and not should_skip_content(record, args.force_content):
            content = client.download_notification_content(record)
            if content.get("contentHtml") or content.get("contentPdfBase64"):
                downloaded += 1
            time.sleep(args.download_delay)

        save_notification(record, state, content=content)
        saved += 1
        if saved % args.save_every == 0:
            state.save()
        if saved % args.progress_every == 0:
            print(
                f"  saved={saved} found={found} missing={missing} transient={transient_failed} "
                f"downloaded={downloaded} id={notification_id}",
                flush=True,
            )

    state.save()
    return {
        "found": found,
        "missing": missing,
        "saved": saved,
        "downloaded": downloaded,
        "skipped": skipped,
        "transient_failed": transient_failed,
    }


def run_staged_plan(client: CBICSession, state: State, args: argparse.Namespace) -> dict[str, dict[str, int]]:
    """Fast notification plan.

    1. Use the bulk GST endpoint to write JSON metadata stubs first.
    2. Fill the remaining notification metadata by ID without downloading content.
    3. Enrich all JSON records that still lack content.

    Separating metadata from content lets us discover the corpus quickly and
    avoids repeatedly paying the PDF download cost while we are still indexing.
    """
    results: dict[str, dict[str, int]] = {}
    original_download_content = args.download_content

    args.download_content = False
    print("\n=== Stage 1/3: GST metadata via bulk API ===", flush=True)
    results["bulk-gst-metadata"] = run_bulk_gst(client, state, args)

    print("\n=== Stage 2/3: Remaining metadata via ID API ===", flush=True)
    results["id-fill-metadata"] = run_id_fill(client, state, args)

    if original_download_content:
        args.download_content = True
        print("\n=== Stage 3/3: Content enrichment ===", flush=True)
        results["content"] = run_content_fill(client, state, args)
    else:
        args.download_content = original_download_content

    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["bulk-gst", "content", "id-fill", "all", "staged"], default="staged")
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--request-delay", type=float, default=0.5, help="Global seconds between HTTP requests")
    parser.add_argument("--download-delay", type=float, default=0.5, help="Extra seconds after content downloads")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--cooldown", type=int, default=300)
    parser.add_argument("--manual-token", default=os.environ.get("CBIC_HOME_TOKEN", ""))
    parser.add_argument("--start-year", type=int, default=2017)
    parser.add_argument("--end-year", type=int, default=datetime.now().year)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--categories", default="", help="Comma-separated GST category names or folder aliases")
    parser.add_argument("--id-start", type=int, default=DEFAULT_ID_START)
    parser.add_argument("--id-end", type=int, default=DEFAULT_ID_END)
    parser.add_argument("--download-content", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force-content", action="store_true")
    parser.add_argument("--retry-missing", action="store_true", help="Retry IDs already recorded as null/missing")
    parser.add_argument("--transient-delay", type=float, default=10.0, help="Seconds to sleep after a non-blocking ID request failure")
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--save-every", type=int, default=25)
    parser.add_argument("--progress-every", type=int, default=50)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    global STATE_FILE
    STATE_FILE = args.state_file

    NOTIFICATION_DIR.mkdir(parents=True, exist_ok=True)
    state = State(args.state_file)
    client = CBICSession(ClientConfig(
        request_delay=args.request_delay,
        retries=args.retries,
        timeout=args.timeout,
        cooldown=args.cooldown,
        manual_token=args.manual_token,
    ))

    print("CBIC hybrid notifications scraper")
    print(f"Mode: {args.mode}")
    print(f"State: {args.state_file}")
    print(f"Output: {NOTIFICATION_DIR}")
    print(f"Request delay: {args.request_delay}s; download delay: {args.download_delay}s")
    print(f"Existing notification state: {state.count('notifications')}")

    try:
        results: dict[str, dict[str, int]] = {}
        if args.mode == "staged":
            results = run_staged_plan(client, state, args)
        else:
            if args.mode in {"bulk-gst", "all"}:
                results["bulk-gst"] = run_bulk_gst(client, state, args)
            if args.mode in {"content", "all"}:
                results["content"] = run_content_fill(client, state, args)
            if args.mode in {"id-fill", "all"}:
                results["id-fill"] = run_id_fill(client, state, args)
    except KeyboardInterrupt:
        state.save()
        print("\nInterrupted; state saved", flush=True)
        return 130
    except StopRun as exc:
        state.save()
        print(f"\nSTOP: {exc}", flush=True)
        return 2

    print("\n=== Summary ===")
    for name, result in results.items():
        parts = " ".join(f"{key}={value}" for key, value in result.items())
        print(f"  {name}: {parts}")
    print(f"  notifications_state={state.count('notifications')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
