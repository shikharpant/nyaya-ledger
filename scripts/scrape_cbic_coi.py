#!/usr/bin/env python3
"""Hybrid CBIC scraper for circulars, orders, and instructions.

Uses the requests-based approach from the notification hybrid scraper
for speed and reliability. Adapts the ID-range crawler for circulars
and list-based APIs for orders and instructions.

Usage:
    python3 scripts/scrape_cbic_coi.py --what circulars,orders,instructions
    python3 scripts/scrape_cbic_coi.py --what circulars --id-start 1000001 --id-end 1003400
    python3 scripts/scrape_cbic_coi.py --retry-missing-content --what forms,rules,notifications,circulars,orders,instructions
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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import requests
    import urllib3
except ImportError as exc:
    raise SystemExit(f"Missing dependency: {exc}. Install requests.")

BASE_URL = "https://taxinformation.cbic.gov.in"
TOKEN_URL = f"{BASE_URL}/api/authenticate-token"
STATE_FILE = Path("data/Law/cbic_scrape_state.json")
OUTPUT_DIR = Path("data/Law/cbic_tax_portal")


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


def normalize_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def is_probably_html(data: bytes, content_type: str) -> bool:
    stripped = data.lstrip()[:100].lower()
    return "html" in content_type or stripped.startswith((b"<html", b"<!doctype", b"<div", b"<p"))


def is_probably_pdf(data: bytes, content_type: str) -> bool:
    return data.startswith(b"%PDF-") or "pdf" in content_type or "octet-stream" in content_type


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


def has_content(record: dict[str, Any]) -> bool:
    return bool(record.get("contentHtml") or record.get("contentPdfBase64"))


def atomic_write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


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


class CBICSession:
    def __init__(self, delay: float = 0.5, retries: int = 2, timeout: int = 90, cooldown: int = 300):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.cooldown = cooldown
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
            ),
            "Accept": "application/json, text/plain, */*",
            "language": "en",
        })
        self._token: str | None = None
        self._token_time = 0.0
        self._last_request_at = 0.0

    def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _mark_request(self):
        self._last_request_at = time.monotonic()

    def authenticate(self, force: bool = False) -> str:
        if not force and self._token and (time.time() - self._token_time) < 900:
            return self._token
        for attempt in range(1, self.retries + 2):
            self._pace()
            try:
                resp = self.session.post(TOKEN_URL, timeout=self.timeout)
                self._mark_request()
            except requests.RequestException as exc:
                time.sleep(min(5 * attempt, 30))
                continue
            if resp.status_code == 200:
                token = resp.json().get("id_token")
                if token:
                    self._token = token
                    self._token_time = time.time()
                    self.session.headers["Authorization1"] = f"homeToken {token}"
                    return token
            elif resp.status_code in {403, 429, 503}:
                raise StopRun(f"auth blocked HTTP {resp.status_code}")
            time.sleep(min(5 * attempt, 30))
        raise StopRun("authentication failed")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if not url.startswith("http"):
            url = f"{BASE_URL}/{url.lstrip('/')}"
        for attempt in range(1, self.retries + 2):
            self.authenticate()
            self._pace()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self._mark_request()
            except requests.RequestException:
                if attempt <= self.retries:
                    time.sleep(min(3 * attempt, 30))
                    continue
                return None
            if resp.status_code == 401:
                self.authenticate(force=True)
                continue
            if resp.status_code in {403, 429, 503}:
                print(f"  [cooldown {self.cooldown}s after HTTP {resp.status_code}]", flush=True)
                time.sleep(self.cooldown)
                if attempt <= self.retries:
                    continue
                raise StopRun(f"blocked HTTP {resp.status_code}")
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                if attempt <= self.retries:
                    time.sleep(min(3 * attempt, 30))
                    continue
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    def get_content(self, content_file_path: str) -> dict[str, Any]:
        path = normalize_path(content_file_path)
        if not path:
            return {"contentHtml": None, "contentPdfBase64": None}
        resp = self._request_get(f"{BASE_URL}/content/html/{quote(path, safe='/')}")
        if resp and resp.status_code == 200 and is_probably_html(resp.content, resp.headers.get("Content-Type", "").lower()):
            return {"contentHtml": resp.text, "contentPdfBase64": None}
        resp = self._request_get(f"{BASE_URL}/content/pdf/{quote(path, safe='/')}")
        if resp and resp.status_code == 200:
            parsed = parse_content_response(resp)
            if parsed.get("contentPdfBase64"):
                return parsed
        return {"contentHtml": None, "contentPdfBase64": None}

    def _request_get(self, url: str) -> requests.Response | None:
        for attempt in range(1, self.retries + 2):
            self.authenticate()
            self._pace()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                self._mark_request()
            except requests.RequestException:
                if attempt <= self.retries:
                    time.sleep(min(3 * attempt, 30))
                    continue
                return None
            if resp.status_code in {403, 429, 503}:
                print(f"  [cooldown {self.cooldown}s after HTTP {resp.status_code}]", flush=True)
                time.sleep(self.cooldown)
                if attempt <= self.retries:
                    continue
                raise StopRun(f"blocked HTTP {resp.status_code}")
            return resp
        return None


def download_content(client: CBICSession, record: dict[str, Any]) -> dict[str, Any]:
    content_path = record.get("contentFilePath", "")
    doc_path = record.get("docFilePath", "")
    content = client.get_content(content_path)
    if not content.get("contentHtml") and not content.get("contentPdfBase64") and doc_path:
        content = client.get_content(doc_path)
    return content


def existing_record_id(record: dict[str, Any], category: str) -> str:
    fields = {
        "forms": ("formId", "id"),
        "rules": ("ruleId", "id"),
        "orders": ("orderId", "id"),
        "instructions": ("instructionId", "id"),
        "notifications": ("id",),
        "circulars": ("id",),
    }.get(category, ("id",))
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return ""


def state_key_for_existing(record: dict[str, Any], category: str) -> str:
    record_id = existing_record_id(record, category)
    if category == "forms":
        return f"form_{record_id}"
    if category == "rules":
        return f"rule_{record_id}"
    if category == "orders" and record.get("orderId"):
        return f"order_{record_id}"
    if category == "instructions" and record.get("instructionId"):
        return f"instruction_{record_id}"
    return record_id


def retry_missing_content(
    client: CBICSession,
    state: State,
    categories: set[str],
    save_every: int = 10,
) -> dict[str, dict[str, int]]:
    """Retry only existing JSON records that lack embedded content.

    This mode never overwrites files that already have content and does not
    re-run metadata discovery. It is meant for finishing partial CBIC runs.
    """
    supported = {"rules", "forms", "notifications", "circulars", "orders", "instructions"}
    selected = sorted(categories & supported)
    results: dict[str, dict[str, int]] = {}

    for category in selected:
        output_dir = OUTPUT_DIR / category
        files = sorted(output_dir.glob("*.json"))
        checked = skipped = no_path = updated = failed = 0
        print(f"\n=== Retrying missing CBIC content: {category} ===", flush=True)

        for path in files:
            record = load_json(path)
            if not record:
                failed += 1
                continue
            checked += 1
            if has_content(record):
                skipped += 1
                continue
            if not (record.get("contentFilePath") or record.get("docFilePath")):
                no_path += 1
                continue

            content = download_content(client, record)
            if content.get("contentHtml") or content.get("contentPdfBase64"):
                record["contentHtml"] = content.get("contentHtml")
                record["contentPdfBase64"] = content.get("contentPdfBase64")
                record["contentText"] = extract_text(content.get("contentHtml"))
                atomic_write_json(path, record)
                key = state_key_for_existing(record, category)
                if key:
                    state.mark_done(category, key, {
                        "name": (record.get("name") or record.get("formNo") or record.get("no") or "")[:60],
                        "no": record.get("no", ""),
                        "has_content": True,
                    })
                updated += 1
            else:
                failed += 1

            if (updated + failed) % save_every == 0:
                state.save()
            if (updated + failed) and (updated + failed) % 25 == 0:
                print(
                    f"  [{category}] checked={checked} updated={updated} "
                    f"skipped={skipped} no_path={no_path} failed={failed}",
                    flush=True,
                )

            time.sleep(client.delay)

        state.save()
        results[category] = {
            "checked": checked,
            "updated": updated,
            "skipped": skipped,
            "no_path": no_path,
            "failed": failed,
        }
        print(
            f"  {category}: checked={checked} updated={updated} skipped={skipped} "
            f"no_path={no_path} failed={failed}",
            flush=True,
        )

    return results


def save_record(record: dict[str, Any], output_dir: Path, doc_type: str, state: State, category: str, state_key: str):
    no = record.get("no", "")
    name = record.get("name", "")
    rec_id = record.get("id", "")
    slug = slugify(f"{no} {name}")[:80] if no else slugify(name)[:80]
    path = output_dir / f"{slug}_{rec_id}.json"
    if record.get("contentHtml"):
        record["contentText"] = extract_text(record["contentHtml"])
    atomic_write_json(path, record)
    state.mark_done(category, state_key, {
        "name": (name or "")[:60],
        "no": no,
        "has_content": has_content(record),
    })


def scrape_circulars(client: CBICSession, state: State, id_start: int = 1000001, id_end: int = 1003400):
    print(f"\n=== Scraping CBIC Circulars (ID {id_start}-{id_end}) ===", flush=True)
    output_dir = OUTPUT_DIR / "circulars"
    output_dir.mkdir(parents=True, exist_ok=True)
    found = missing = skipped = 0

    all_ids = list(range(id_start, id_end + 1))
    todo_ids = [nid for nid in all_ids if not state.is_done("circulars", str(nid))]
    skipped = len(all_ids) - len(todo_ids)
    print(f"  Total: {len(all_ids)}, done: {skipped}, to process: {len(todo_ids)}", flush=True)

    for count, nid in enumerate(todo_ids, 1):
        key = str(nid)
        record = client.get_json(f"api/cbic-circular-msts/{nid}")
        if not record:
            missing += 1
            state.mark_done("circulars", key, {"error": "null"})
            if count % 50 == 0:
                state.save()
                print(f"  [circulars] found={found} missing={missing} id={nid} ({count}/{len(todo_ids)})", flush=True)
            continue

        found += 1
        name = record.get("circularName", f"circular_{nid}")
        no = record.get("circularNo", "")
        content = download_content(client, record)

        save_record({
            "source": "cbic_tax_portal", "type": "circular",
            "name": name, "no": no, "id": nid,
            "category": record.get("circularCategory", ""),
            "issueDt": record.get("circularDt") or record.get("issueDt"),
            "amendDt": record.get("amendDt"), "isActive": record.get("isActive"),
            "contentFilePath": record.get("contentFilePath", ""),
            "docFilePath": record.get("docFilePath", ""),
            "docFileType": record.get("docFileType", ""),
            "contentHtml": content.get("contentHtml"),
            "contentPdfBase64": content.get("contentPdfBase64"),
        }, output_dir, "circular", state, "circulars", key)

        if count % 25 == 0:
            state.save()
        if count % 50 == 0:
            print(f"  [circulars] found={found} missing={missing} id={nid} ({count}/{len(todo_ids)})", flush=True)

    state.save()
    print(f"  Circulars done: found={found} missing={missing} total={state.count('circulars')}", flush=True)


def scrape_orders(client: CBICSession, state: State):
    print("\n=== Scraping CBIC Orders ===", flush=True)
    output_dir = OUTPUT_DIR / "orders"
    output_dir.mkdir(parents=True, exist_ok=True)

    orders = client.get_json("api/cbic-order-msts")
    if not orders:
        print("  No orders found", flush=True)
        return
    print(f"  Found {len(orders)} orders", flush=True)

    saved = skipped = 0
    for order in orders:
        order_id = str(order.get("id", ""))
        name = order.get("orderName", "Unknown")
        if state.is_done("orders", order_id):
            skipped += 1
            continue

        detail = client.get_json(f"api/cbic-order-msts/{order_id}")
        if not detail:
            state.mark_done("orders", order_id, {"name": name})
            state.save()
            continue

        content = download_content(client, detail)
        save_record({
            "source": "cbic_tax_portal", "type": "order",
            "name": detail.get("orderName", name),
            "no": detail.get("orderNo", ""),
            "id": order_id,
            "category": detail.get("orderCategory", ""),
            "issueDt": detail.get("issueDt"),
            "amendDt": detail.get("amendDt"),
            "isActive": detail.get("isActive"),
            "contentFilePath": detail.get("contentFilePath", ""),
            "docFilePath": detail.get("docFilePath", ""),
            "contentHtml": content.get("contentHtml"),
            "contentPdfBase64": content.get("contentPdfBase64"),
        }, output_dir, "order", state, "orders", order_id)
        saved += 1
        if saved % 10 == 0:
            state.save()
            print(f"  [orders] saved={saved} skipped={skipped}", flush=True)

    state.save()
    print(f"  Orders done: saved={saved} skipped={skipped}", flush=True)


def scrape_instructions(client: CBICSession, state: State):
    print("\n=== Scraping CBIC Instructions ===", flush=True)
    output_dir = OUTPUT_DIR / "instructions"
    output_dir.mkdir(parents=True, exist_ok=True)

    instructions = client.get_json("api/cbic-instruction-msts")
    if not instructions:
        print("  No instructions found", flush=True)
        return
    print(f"  Found {len(instructions)} instructions", flush=True)

    saved = skipped = 0
    for inst in instructions:
        inst_id = str(inst.get("id", ""))
        name = inst.get("instructionName", "Unknown")
        if state.is_done("instructions", inst_id):
            skipped += 1
            continue

        detail = client.get_json(f"api/cbic-instruction-msts/{inst_id}")
        if not detail:
            state.mark_done("instructions", inst_id, {"name": name})
            state.save()
            continue

        content = download_content(client, detail)
        save_record({
            "source": "cbic_tax_portal", "type": "instruction",
            "name": detail.get("instructionName", name),
            "no": detail.get("instructionNo", ""),
            "id": inst_id,
            "issueDt": detail.get("issueDt"),
            "amendDt": detail.get("amendDt"),
            "isActive": detail.get("isActive"),
            "contentFilePath": detail.get("contentFilePath", ""),
            "docFilePath": detail.get("docFilePath", ""),
            "contentHtml": content.get("contentHtml"),
            "contentPdfBase64": content.get("contentPdfBase64"),
        }, output_dir, "instruction", state, "instructions", inst_id)
        saved += 1
        if saved % 10 == 0:
            state.save()
            print(f"  [instructions] saved={saved} skipped={skipped}", flush=True)

    state.save()
    print(f"  Instructions done: saved={saved} skipped={skipped}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--what", default="circulars,orders,instructions",
                        help="Comma-separated: rules,forms,notifications,circulars,orders,instructions")
    parser.add_argument("--retry-missing-content", action="store_true",
                        help="Retry content downloads only for existing JSON records without embedded content")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--cooldown", type=int, default=300)
    parser.add_argument("--id-start", type=int, default=1000001, help="Circular ID range start")
    parser.add_argument("--id-end", type=int, default=1003400, help="Circular ID range end")
    args = parser.parse_args()

    what = {w.strip() for w in args.what.split(",")}
    state = State(STATE_FILE)
    client = CBICSession(delay=args.delay, retries=args.retries, timeout=args.timeout, cooldown=args.cooldown)

    print(f"CBIC COI scraper — delay={args.delay}s")
    print(f"State: {STATE_FILE}")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Scraping: {', '.join(sorted(what))}")

    try:
        if args.retry_missing_content:
            results = retry_missing_content(client, state, what)
            print("\n=== Retry summary ===")
            for category, result in results.items():
                print("  " + category + ": " + " ".join(f"{k}={v}" for k, v in result.items()))
            return 0
        if "circulars" in what:
            scrape_circulars(client, state, args.id_start, args.id_end)
        if "orders" in what:
            scrape_orders(client, state)
        if "instructions" in what:
            scrape_instructions(client, state)
    except KeyboardInterrupt:
        state.save()
        print("\nInterrupted; state saved", flush=True)
        return 130
    except StopRun as exc:
        state.save()
        print(f"\nSTOP: {exc}", flush=True)
        return 2

    print("\n=== Done ===")
    for cat in ["circulars", "orders", "instructions"]:
        c = state.count(cat)
        if c:
            print(f"  {cat}: {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
