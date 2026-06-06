"""Gently scrape missing India Code acts into local PDFs and section JSON.

The scraper is intentionally slow and resumable. It reads
data/Law/india_code_missing_from_base_laws.json, archives PDFs, and writes
base_laws-compatible section JSON where India Code exposes section content.

Usage:
    python3 scripts/scrape_india_code_missing_acts.py --dry-run --limit 25
    python3 scripts/scrape_india_code_missing_acts.py --limit 3 --window-hours 1
    python3 scripts/scrape_india_code_missing_acts.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


REPO_ROOT = Path(__file__).resolve().parent.parent
MISSING_FILE = REPO_ROOT / "data" / "Law" / "india_code_missing_from_base_laws.json"
PDF_DIR = REPO_ROOT / "data" / "Law" / "india_code_pdfs"
SCHEDULE_PDF_DIR = REPO_ROOT / "data" / "Law" / "india_code_schedule_pdfs"
BASE_LAWS_DIR = REPO_ROOT / "data" / "Law" / "base_laws"
SCHEDULE_JSON_DIR = REPO_ROOT / "data" / "Law" / "base_law_schedules"
STATE_FILE = REPO_ROOT / "data" / "Law" / "india_code_scrape_state.json"

BASE_URL = "https://www.indiacode.nic.in"
UPLOAD_BASE_URL = "https://upload.indiacode.nic.in"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
PDF_CHUNK_SIZE = 1024 * 128
SERVER_PAUSE_STATUSES = {429, 502, 503, 504}
NO_RETRY_STATUSES = {400, 401, 403, 404, 410}

HIGH_VALUE_TERMS = [
    "goods and services",
    "customs",
    "excise",
    "insolvency",
    "bankruptcy",
    "companies",
    "company",
    "corporate",
    "banking",
    "insurance",
    "securities",
    "foreign exchange",
    "criminal",
    "evidence",
    "arbitration",
    "contract",
    "corruption",
    "labour",
    "wages",
    "industrial",
    "patent",
    "trade mark",
    "copyright",
    "designs",
    "environment",
    "consumer",
]

CPU_CHIP_RE = re.compile(r"^(?:k10temp|coretemp|zenpower|cpu_thermal|acpitz)", re.I)
CPU_LABEL_RE = re.compile(r"^(?:Tctl|Tdie|Package|Core\s+\d+|CPU|temp\d+)", re.I)
TEMP_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*°?C")


@dataclass
class RequestBudget:
    max_requests: int
    used: int = 0
    server_pauses: int = 0

    def consume(self) -> None:
        self.used += 1
        if self.used > self.max_requests:
            raise StopRun("request budget exhausted")


class StopRun(Exception):
    """Raised when the scraper should stop cleanly and preserve state."""


class ServerBackoff(Exception):
    """Raised after server-protection retries are exhausted for one request."""


class NoRetryHttpError(Exception):
    """Raised for HTTP responses that should not be retried."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    value = unescape(value or "")
    value = value.replace("&", " and ")
    value = re.sub(r"\bthe\b", " ", value, flags=re.I)
    value = re.sub(r"[^0-9A-Za-z]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_").lower() or "untitled"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_missing_acts() -> list[dict[str, Any]]:
    data = json.loads(MISSING_FILE.read_text(encoding="utf-8"))
    return list(data["missing_acts"])


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "completed": {},
            "failed": {},
            "events": [],
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.part")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def record_event(state: dict[str, Any], event: str, **details: Any) -> None:
    state.setdefault("events", []).append({"at": utc_now(), "event": event, **details})
    state["events"] = state["events"][-500:]


def output_paths(act: dict[str, Any]) -> tuple[Path, Path]:
    slug = slugify(act.get("short_title", ""))
    handle_id = str(act.get("handle_id", "unknown"))
    return PDF_DIR / f"{slug}__{handle_id}.pdf", BASE_LAWS_DIR / f"{slug}.json"


def schedule_output_paths(act: dict[str, Any], schedule: dict[str, str]) -> tuple[Path, Path]:
    act_slug = slugify(act.get("short_title", ""))
    handle_id = str(act.get("handle_id", "unknown"))
    label = slugify(schedule.get("label") or f"schedule_{schedule.get('rid', 'unknown')}")
    return (
        SCHEDULE_PDF_DIR / f"{act_slug}__{handle_id}__{label}__rid_{schedule.get('rid')}.pdf",
        SCHEDULE_JSON_DIR / f"{act_slug}__{handle_id}.json",
    )


def handle_url_for_act(act: dict[str, Any]) -> str:
    return (
        f"{BASE_URL}/handle/123456789/{act['handle_id']}"
        "?view_type=search&col=123456789/1362"
    )


def priority_score(act: dict[str, Any]) -> int:
    title = (act.get("short_title") or "").lower()
    return sum(1 for term in HIGH_VALUE_TERMS if term in title)


def ordered_acts(acts: list[dict[str, Any]], priority: str) -> list[dict[str, Any]]:
    if priority == "alphabetical":
        return sorted(acts, key=lambda act: (act.get("short_title") or "").lower())
    if priority == "smallest-first":
        return sorted(
            acts,
            key=lambda act: (
                0 if act.get("pdf_filename") else 1,
                (act.get("pdf_filename") or "").lower(),
                (act.get("short_title") or "").lower(),
            ),
        )
    return sorted(
        acts,
        key=lambda act: (
            -priority_score(act),
            -(act.get("year") or 0),
            (act.get("short_title") or "").lower(),
        ),
    )


def sensors_cpu_temps(output: str) -> list[float]:
    temps: list[float] = []
    in_cpu_chip = False
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            in_cpu_chip = False
            continue
        if not raw_line.startswith((" ", "\t")) and ":" not in line:
            chip = line.strip()
            in_cpu_chip = bool(CPU_CHIP_RE.match(chip))
            continue
        if not in_cpu_chip or ":" not in line:
            continue
        label, rest = line.split(":", 1)
        if CPU_LABEL_RE.match(label.strip()):
            match = TEMP_RE.search(rest)
            if match:
                temps.append(float(match.group(1)))
    return temps


def current_cpu_temp_c() -> float | None:
    if not shutil.which("sensors"):
        return None
    result = subprocess.run(
        ["sensors"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return None
    temps = sensors_cpu_temps(result.stdout)
    return max(temps) if temps else None


def wait_for_cpu_temperature(args: argparse.Namespace, dry_run: bool = False) -> None:
    if args.skip_cpu_temp_check:
        return
    while True:
        temp = current_cpu_temp_c()
        if temp is None:
            if args.allow_missing_cpu_temp:
                print("CPU temp unavailable; continuing because --allow-missing-cpu-temp is set", flush=True)
                return
            raise RuntimeError(
                "CPU temp unavailable from sensors; pass --allow-missing-cpu-temp to continue"
            )
        if temp <= args.cpu_temp_threshold_c:
            return
        print(
            f"CPU temp {temp:.1f}C exceeds {args.cpu_temp_threshold_c:.1f}C; "
            f"pausing {args.cpu_temp_pause_seconds}s",
            flush=True,
        )
        if dry_run:
            return
        time.sleep(args.cpu_temp_pause_seconds)


def polite_sleep(min_seconds: float, max_seconds: float, dry_run: bool = False) -> None:
    delay = random.uniform(min_seconds, max_seconds)
    if dry_run:
        print(f"DRY sleep {delay:.1f}s", flush=True)
        return
    try:
        time.sleep(delay)
    except KeyboardInterrupt as exc:
        raise StopRun("interrupted by user") from exc


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def request_with_guards(
    session: requests.Session,
    method: str,
    url: str,
    args: argparse.Namespace,
    budget: RequestBudget,
    *,
    stream: bool = False,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, args.max_attempts + 1):
        wait_for_cpu_temperature(args)
        budget.consume()
        try:
            response = session.request(
                method,
                url,
                timeout=args.timeout,
                stream=stream,
                headers=headers,
            )
            if response.status_code in SERVER_PAUSE_STATUSES:
                print(
                    f"HTTP {response.status_code} for {url}; server pause "
                    f"{args.server_pause_seconds}s",
                    flush=True,
                )
                response.close()
                budget.server_pauses += 1
                if budget.server_pauses >= args.max_server_pauses:
                    raise StopRun("server-protection pause limit reached")
                time.sleep(args.server_pause_seconds)
                continue
            if response.status_code in NO_RETRY_STATUSES:
                message = (
                    f"HTTP {response.status_code} non-retryable for {url} "
                    f"content_type={response.headers.get('content-type')}"
                )
                response.close()
                raise NoRetryHttpError(message)
            response.raise_for_status()
            polite_sleep(args.global_delay_min, args.global_delay_max)
            return response
        except StopRun:
            raise
        except NoRetryHttpError:
            raise
        except Exception as exc:
            last_error = exc
            print(f"request failed attempt={attempt}/{args.max_attempts} url={url}: {exc}", flush=True)
            if attempt < args.max_attempts:
                time.sleep(args.retry_delay_seconds)
    raise ServerBackoff(str(last_error) if last_error else f"failed {url}")


def text_from_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    for tag in soup.find_all(["br", "p", "div", "tr", "li"]):
        tag.append("\n")
    text = soup.get_text(" ")
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_section_html(response_text: str) -> str:
    text = response_text or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        parts = [str(data.get("content") or ""), str(data.get("footnote") or "")]
        return "\n".join(part for part in parts if part.strip())
    return text


def validate_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def response_summary(response: requests.Response, tmp_path: Path | None = None) -> str:
    head = b""
    if tmp_path and tmp_path.exists():
        try:
            with tmp_path.open("rb") as handle:
                head = handle.read(80)
        except OSError:
            head = b""
    return (
        f"status={response.status_code} "
        f"content_type={response.headers.get('content-type')} "
        f"content_length={response.headers.get('content-length')} "
        f"head={head!r}"
    )


def discover_pdf_urls(
    session: requests.Session,
    act: dict[str, Any],
    args: argparse.Namespace,
    budget: RequestBudget,
) -> list[str]:
    handle_id = re.escape(str(act["handle_id"]))
    with request_with_guards(
        session,
        "GET",
        handle_url_for_act(act),
        args,
        budget,
    ) as response:
        html = response.text

    urls: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        rf"""(?i)(?:href\s*=\s*["'])?(/bitstream/123456789/{handle_id}/[^"'<>\s]+?\.pdf)"""
    )
    for match in pattern.finditer(html):
        url = urljoin(BASE_URL, unescape(match.group(1)))
        if url not in seen:
            seen.add(url)
            urls.append(url)

    def pdf_rank(url: str) -> tuple[int, int, str]:
        name = Path(url).name
        hindi_penalty = 1 if name.lower().startswith("h") else 0
        seq_match = re.search(rf"/{handle_id}/(\d+)/", url)
        sequence = int(seq_match.group(1)) if seq_match else 0
        return (hindi_penalty, -sequence, url)

    return sorted(urls, key=pdf_rank)


def try_download_pdf_url(
    session: requests.Session,
    act: dict[str, Any],
    url: str,
    pdf_path: Path,
    tmp_path: Path,
    args: argparse.Namespace,
    budget: RequestBudget,
) -> tuple[bool, str]:
    with request_with_guards(
        session,
        "GET",
        url,
        args,
        budget,
        stream=True,
        headers={"Referer": handle_url_for_act(act)},
    ) as response:
        with tmp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=PDF_CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)
        if validate_pdf(tmp_path):
            tmp_path.replace(pdf_path)
            return True, response_summary(response)
        summary = response_summary(response, tmp_path)
        tmp_path.unlink(missing_ok=True)
        return False, summary


def download_pdf_url(
    session: requests.Session,
    url: str,
    output_path: Path,
    args: argparse.Namespace,
    budget: RequestBudget,
    *,
    referer: str | None = None,
) -> dict[str, Any]:
    if output_path.exists() and not args.force:
        return {
            "status": "exists",
            "path": str(output_path),
            "sha256": sha256_file(output_path) if validate_pdf(output_path) else None,
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    with request_with_guards(
        session,
        "GET",
        url,
        args,
        budget,
        stream=True,
        headers={"Referer": referer} if referer else None,
    ) as response:
        with tmp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=PDF_CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)
        if not validate_pdf(tmp_path):
            summary = response_summary(response, tmp_path)
            tmp_path.unlink(missing_ok=True)
            raise ValueError(f"download did not look like a PDF: {summary}")
        tmp_path.replace(output_path)
    return {
        "status": "downloaded",
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "url": url,
    }


def download_pdf(
    session: requests.Session,
    act: dict[str, Any],
    pdf_path: Path,
    args: argparse.Namespace,
    budget: RequestBudget,
) -> dict[str, Any]:
    if not act.get("pdf_url"):
        return {"status": "unavailable", "reason": "missing pdf_url"}
    if pdf_path.exists() and not args.force:
        return {
            "status": "exists",
            "path": str(pdf_path),
            "sha256": sha256_file(pdf_path) if validate_pdf(pdf_path) else None,
        }
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pdf_path.with_suffix(pdf_path.suffix + ".part")
    attempted: list[tuple[str, str]] = []
    ok, summary = try_download_pdf_url(
        session,
        act,
        act["pdf_url"],
        pdf_path,
        tmp_path,
        args,
        budget,
    )
    if not ok:
        attempted.append((act["pdf_url"], summary))
        for url in discover_pdf_urls(session, act, args, budget):
            if url == act["pdf_url"]:
                continue
            ok, summary = try_download_pdf_url(
                session,
                act,
                url,
                pdf_path,
                tmp_path,
                args,
                budget,
            )
            if ok:
                act["pdf_url"] = url
                break
            attempted.append((url, summary))
    if not ok:
        detail = "; ".join(f"{url} ({summary})" for url, summary in attempted[:5])
        raise ValueError(f"download did not look like a PDF after {len(attempted)} attempt(s): {detail}")
    return {
        "status": "downloaded",
        "path": str(pdf_path),
        "bytes": pdf_path.stat().st_size,
        "sha256": sha256_file(pdf_path),
        "pdf_url": act.get("pdf_url"),
    }


def extract_schedule_refs(handle_html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(handle_html, "html.parser")
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in soup.find_all("a", href=True):
        href = unescape(link.get("href", ""))
        if "schedulefile" not in href.lower():
            continue
        rid_match = re.search(r"[?&]rid=([0-9]+)", href, re.I)
        aid_match = re.search(r"[?&]aid=([^&\"'\s]+)", href, re.I)
        if not rid_match or not aid_match:
            continue
        rid = rid_match.group(1)
        aid = aid_match.group(1)
        container = link
        for parent in link.parents:
            if parent.name in {"tr", "div"}:
                container = parent
                break
        label = ""
        title = ""
        label_node = container.find(class_=re.compile(r"label-default-schedule"))
        title_node = container.find(class_=re.compile(r"schedulebtn|scheduleTitle", re.I))
        if label_node:
            label = label_node.get_text(" ", strip=True)
        if title_node:
            title = title_node.get_text(" ", strip=True)
            title = re.sub(r"^\s*Schedule\s+\d+\.\s*", "", title, flags=re.I).strip()
        if not title:
            text = container.get_text(" ", strip=True)
            title_match = re.search(
                r"Schedule\s+([0-9]+)\.\s*([A-Za-z0-9 .(),/-]+?)(?:\s+Order|\s+Appendix|\s+Forms|$)",
                text,
                re.I,
            )
            if title_match:
                label = label or f"Schedule {title_match.group(1)}."
                title = title_match.group(2).strip()
        if not label:
            label = f"Schedule {len(refs) + 1}."
        if not title:
            title = label.rstrip(".")
        key = (aid, rid)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "aid": aid,
                "rid": rid,
                "label": label,
                "title": title,
                "url": urljoin(UPLOAD_BASE_URL, href),
            }
        )
    return refs


def pdf_text(path: Path) -> str:
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is required to extract schedule PDFs")
    result = subprocess.run(
        ["pdftotext", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


def fetch_schedules(
    session: requests.Session,
    act: dict[str, Any],
    args: argparse.Namespace,
    budget: RequestBudget,
) -> dict[str, Any]:
    handle_url = handle_url_for_act(act)
    response = request_with_guards(session, "GET", handle_url, args, budget)
    refs = extract_schedule_refs(response.text)
    schedules: list[dict[str, Any]] = []
    for index, ref in enumerate(refs, start=1):
        pdf_path, _ = schedule_output_paths(act, ref)
        download = download_pdf_url(
            session,
            ref["url"],
            pdf_path,
            args,
            budget,
            referer=handle_url,
        )
        schedules.append(
            {
                "schedule_number": index,
                "label": ref.get("label"),
                "title": ref.get("title"),
                "aid": ref.get("aid"),
                "rid": ref.get("rid"),
                "source_url": ref.get("url"),
                "pdf": download,
                "full_text": pdf_text(pdf_path),
            }
        )
    if not schedules:
        return {"status": "none", "schedules": []}
    return {"status": "scraped", "schedules": schedules}


def write_schedule_json(
    act: dict[str, Any],
    schedules: list[dict[str, Any]],
    json_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if json_path.exists() and not args.force:
        return {"status": "exists", "path": str(json_path)}
    payload = {
        "source": handle_url_for_act(act),
        "act": act.get("short_title", ""),
        "handle_id": act.get("handle_id"),
        "act_id": act.get("act_id"),
        "act_number": act.get("act_number"),
        "enactment_date": act.get("enactment_date"),
        "year": act.get("year"),
        "total_schedules": len(schedules),
        "scraped_at": utc_now(),
        "schedules": schedules,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(json_path)
    return {"status": "written", "path": str(json_path), "schedules": len(schedules)}


def extract_section_refs(handle_html: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    soup = BeautifulSoup(handle_html, "html.parser")
    for tag in soup.find_all(["a", "button", "option"]):
        raw = " ".join(
            value
            for value in [
                tag.get("href", ""),
                tag.get("onclick", ""),
                tag.get("value", ""),
                tag.get_text(" ", strip=True),
            ]
            if value
        )
        raw = re.sub(r"§ion(id|no)", r"&section\1", raw, flags=re.I)
        raw_lower = raw.lower()
        if "sectionpagecontent" not in raw_lower and "sectionid" not in raw_lower:
            continue
        actid_match = re.search(r"actid=([A-Za-z0-9_]+)", raw, re.I)
        section_match = re.search(r"sectionid=([0-9]+)", raw, re.I)
        if not section_match:
            section_match = re.search(r"SectionPageContent\([^0-9]*([0-9]+)", raw)
        if not section_match:
            continue
        section_id = section_match.group(1)
        act_id = actid_match.group(1) if actid_match else ""
        label_match = re.search(r"\b(?:section|sec\.?)\s*([0-9]+[A-Za-z]*)", raw, re.I)
        label = label_match.group(1) if label_match else ""
        title = tag.get_text(" ", strip=True)
        key = (act_id, section_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"act_id": act_id, "section_id": section_id, "section_number": label, "title": title})
    return refs


def fallback_section_refs_from_ids(handle_html: str, act_id: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for section_id in re.findall(r"(?:sectionID|sectionId|sectionid)[=:\"'\s]+([0-9]+)", handle_html):
        if section_id in seen:
            continue
        seen.add(section_id)
        refs.append({"act_id": act_id, "section_id": section_id, "section_number": "", "title": ""})
    return refs


def section_content_url(act_id: str, section_id: str) -> str:
    return f"{BASE_URL}/SectionPageContent?actid={quote(act_id)}&sectionID={quote(section_id)}"


def infer_section_number(ref: dict[str, str], html: str, text: str, fallback: int) -> str:
    if ref.get("section_number"):
        return ref["section_number"]
    title = ref.get("title", "")
    section_match = re.search(r"\b(?:section|sec\.?)\s*([0-9]+[A-Za-z]*)\b", title, re.I)
    if section_match:
        return section_match.group(1)
    for source in [text[:800], html[:2000]]:
        heading_match = re.search(r"(?:^|\n|\s)([0-9]+[A-Za-z]*)\s*\.\s+[A-Z(]", source)
        if heading_match:
            return heading_match.group(1)
    if re.fullmatch(r"\d{1,3}[A-Za-z]?", ref.get("section_id", "")):
        return ref["section_id"]
    return str(fallback)


def normalize_section_numbers(sections: list[dict[str, str]]) -> None:
    seen: set[str] = set()
    for index, section in enumerate(sections, start=1):
        value = str(section.get("section_number") or "").strip()
        if not value or value in seen or re.fullmatch(r"\d{4,}", value):
            section["section_number"] = str(index)
        seen.add(section["section_number"])


def infer_description(section_number: str, html: str, text: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.find("title")
    if title and title.get_text(strip=True):
        return title.get_text(" ", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:12]:
        if not re.fullmatch(rf"{re.escape(section_number)}\.?", line):
            if len(line) <= 180 and not line.lower().startswith(("act|", "empty", "chapter")):
                return line
    return ""


def fetch_sections(
    session: requests.Session,
    act: dict[str, Any],
    args: argparse.Namespace,
    budget: RequestBudget,
) -> dict[str, Any]:
    act_id = act.get("act_id")
    if not act_id:
        return {"status": "unavailable", "reason": "missing act_id", "sections": []}

    handle_url = handle_url_for_act(act)
    response = request_with_guards(session, "GET", handle_url, args, budget)
    handle_html = response.text
    refs = extract_section_refs(handle_html)
    if not refs:
        refs = fallback_section_refs_from_ids(handle_html, act_id)
    for ref in refs:
        if not ref.get("act_id"):
            ref["act_id"] = act_id
    refs = [ref for ref in refs if ref.get("section_id")]

    if not refs:
        return {"status": "unavailable", "reason": "no section ids found", "sections": []}

    sections: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in refs:
        key = f"{ref['act_id']}:{ref['section_id']}"
        if key in seen:
            continue
        seen.add(key)
        url = section_content_url(ref["act_id"], ref["section_id"])
        sec_response = request_with_guards(
            session,
            "GET",
            url,
            args,
            budget,
            headers={"Referer": handle_url},
        )
        html = normalize_section_html(sec_response.text)
        text = text_from_html(html)
        section_number = infer_section_number(ref, html, text, len(sections) + 1)
        sections.append(
            {
                "section_number": section_number,
                "description": infer_description(section_number, html, text),
                "full_text": text,
                "html_content": html,
                "india_code_section_id": ref["section_id"],
                "india_code_section_url": url,
            }
        )

    normalize_section_numbers(sections)
    return {"status": "scraped", "sections": sections}


def write_base_law_json(
    act: dict[str, Any],
    sections: list[dict[str, str]],
    json_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if json_path.exists() and not args.force:
        return {"status": "exists", "path": str(json_path)}
    payload = {
        "source": handle_url_for_act(act),
        "act": act.get("short_title", ""),
        "handle_id": act.get("handle_id"),
        "act_id": act.get("act_id"),
        "act_number": act.get("act_number"),
        "enactment_date": act.get("enactment_date"),
        "year": act.get("year"),
        "pdf_url": act.get("pdf_url"),
        "pdf_filename": act.get("pdf_filename"),
        "total_sections": len(sections),
        "scraped_at": utc_now(),
        "throttle": {
            "global_delay_min": args.global_delay_min,
            "global_delay_max": args.global_delay_max,
            "act_delay_min": args.act_delay_min,
            "act_delay_max": args.act_delay_max,
            "retry_delay_seconds": args.retry_delay_seconds,
            "max_attempts": args.max_attempts,
            "cpu_temp_threshold_c": args.cpu_temp_threshold_c,
            "cpu_temp_pause_seconds": args.cpu_temp_pause_seconds,
        },
        "sections": sections,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(json_path)
    return {"status": "written", "path": str(json_path), "sections": len(sections)}


def fetch_robots(session: requests.Session, args: argparse.Namespace, budget: RequestBudget) -> None:
    if args.skip_robots:
        return
    try:
        response = request_with_guards(session, "GET", f"{BASE_URL}/robots.txt", args, budget)
        crawl_delay = re.search(r"(?im)^crawl-delay:\s*([0-9.]+)", response.text)
        if crawl_delay:
            delay = float(crawl_delay.group(1))
            if delay > args.global_delay_min:
                print(f"robots.txt crawl-delay={delay}; raising minimum request delay", flush=True)
                args.global_delay_min = delay
                args.global_delay_max = max(args.global_delay_max, delay)
    except Exception as exc:
        print(f"robots.txt check failed; continuing with conservative defaults: {exc}", flush=True)


def should_skip(act: dict[str, Any], state: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.force:
        return False
    handle_id = str(act.get("handle_id"))
    if state.get("failed", {}).get(handle_id) and not args.retry_failed:
        return True
    completed = state.get("completed", {}).get(handle_id, {})
    if args.mode == "pdf-only":
        return bool(completed.get("pdf"))
    if args.mode == "json-only":
        return bool(completed.get("json") or completed.get("sections_unavailable"))
    if args.mode == "schedules-only":
        return bool(completed.get("schedules") or completed.get("schedules_unavailable"))
    return bool(completed.get("pdf") and (completed.get("json") or completed.get("sections_unavailable")))


def process_act(
    session: requests.Session,
    act: dict[str, Any],
    state: dict[str, Any],
    args: argparse.Namespace,
    budget: RequestBudget,
) -> None:
    handle_id = str(act.get("handle_id"))
    pdf_path, json_path = output_paths(act)

    if args.dry_run:
        print(
            f"DRY {handle_id} score={priority_score(act)} title={act.get('short_title')} "
            f"pdf={pdf_path} json={json_path}",
            flush=True,
        )
        wait_for_cpu_temperature(args, dry_run=True)
        return

    completed = state.setdefault("completed", {}).setdefault(handle_id, {})
    print(f"ACT {handle_id}: {act.get('short_title')}", flush=True)

    if args.mode in {"pdf-json", "pdf-only"} and (args.force or not completed.get("pdf")):
        try:
            original_pdf_url = act.get("pdf_url")
            result = download_pdf(session, act, pdf_path, args, budget)
            completed["pdf"] = result
            if result.get("pdf_url") and result.get("pdf_url") != original_pdf_url:
                corrected = state.setdefault("corrected_pdf_urls", {})
                corrected[handle_id] = {
                    "from": original_pdf_url,
                    "to": result["pdf_url"],
                    "at": utc_now(),
                }
            record_event(state, "pdf", handle_id=handle_id, result=result)
            print(f"  pdf {result['status']} -> {pdf_path}", flush=True)
        except Exception as exc:
            if args.mode == "pdf-only":
                raise
            result = {"status": "failed", "error": str(exc), "at": utc_now()}
            completed["pdf_failed"] = result
            record_event(state, "pdf_failed", handle_id=handle_id, result=result)
            print(f"  pdf failed; continuing to sections: {exc}", flush=True)
        save_state(state)

    if args.mode in {"pdf-json", "json-only"} and (
        args.force
        or not (completed.get("json") or completed.get("sections_unavailable"))
    ):
        result = fetch_sections(session, act, args, budget)
        if result["status"] == "scraped" and result["sections"]:
            write_result = write_base_law_json(act, result["sections"], json_path, args)
            completed["json"] = write_result
            record_event(state, "json", handle_id=handle_id, result=write_result)
            print(f"  json {write_result['status']} sections={len(result['sections'])}", flush=True)
        else:
            completed["sections_unavailable"] = {
                "status": result["status"],
                "reason": result.get("reason"),
                "at": utc_now(),
            }
            record_event(
                state,
                "sections_unavailable",
                handle_id=handle_id,
                reason=result.get("reason"),
            )
            print(f"  sections unavailable: {result.get('reason')}", flush=True)
        save_state(state)

    if args.mode == "schedules-only" and (
        args.force
        or not (completed.get("schedules") or completed.get("schedules_unavailable"))
    ):
        result = fetch_schedules(session, act, args, budget)
        _, schedule_json_path = schedule_output_paths(act, {"label": "schedules", "rid": "all"})
        if result["status"] == "scraped" and result["schedules"]:
            write_result = write_schedule_json(act, result["schedules"], schedule_json_path, args)
            completed["schedules"] = write_result
            record_event(state, "schedules", handle_id=handle_id, result=write_result)
            print(f"  schedules {write_result['status']} count={len(result['schedules'])}", flush=True)
        else:
            completed["schedules_unavailable"] = {
                "status": result["status"],
                "reason": result.get("reason", "no schedules found"),
                "at": utc_now(),
            }
            record_event(
                state,
                "schedules_unavailable",
                handle_id=handle_id,
                reason=completed["schedules_unavailable"]["reason"],
            )
            print("  schedules unavailable: no schedules found", flush=True)
        save_state(state)

    state.get("failed", {}).pop(handle_id, None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["pdf-json", "pdf-only", "json-only", "schedules-only"],
        default="pdf-json",
    )
    parser.add_argument("--priority", choices=["high-value", "alphabetical", "smallest-first"], default="high-value")
    parser.add_argument("--limit", type=int, default=0, help="Maximum acts to consider in this run")
    parser.add_argument("--window-hours", type=float, default=10)
    parser.add_argument("--max-requests-per-run", type=int, default=2400)
    parser.add_argument("--global-delay-min", type=float, default=2)
    parser.add_argument("--global-delay-max", type=float, default=2)
    parser.add_argument("--act-delay-min", type=float, default=60)
    parser.add_argument("--act-delay-max", type=float, default=120)
    parser.add_argument("--retry-delay-seconds", type=float, default=5)
    parser.add_argument("--server-pause-seconds", type=float, default=1800)
    parser.add_argument("--max-server-pauses", type=int, default=2)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--cpu-temp-threshold-c", type=float, default=85)
    parser.add_argument("--cpu-temp-pause-seconds", type=float, default=120)
    parser.add_argument("--allow-missing-cpu-temp", action="store_true")
    parser.add_argument("--skip-cpu-temp-check", action="store_true")
    parser.add_argument("--skip-robots", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def self_test() -> None:
    sample = """
k10temp-pci-00c3
Adapter: PCI adapter
Tctl:         +51.2°C
Tccd3:        +60.8°C

nvme-pci-0100
Adapter: PCI adapter
Composite:    +89.9°C
"""
    assert sensors_cpu_temps(sample) == [51.2]
    assert slugify("The Customs Act,1962") == "customs_act_1962"
    html = '<a href="/SectionPageContent?actid=AC_X&sectionID=123">Section 1</a>'
    refs = extract_section_refs(html)
    assert refs[0]["act_id"] == "AC_X"
    assert refs[0]["section_id"] == "123"
    assert refs[0]["section_number"] == "1"
    schedule_html = (
        '<a class="schedulebtnzain"><span class="label label-default-schedule">'
        'Schedule 1.</span>&nbsp;SCHEDULE I</a>'
        '<a href="https://upload.indiacode.nic.in/schedulefile?aid=AC_X&rid=52">pdf</a>'
    )
    schedules = extract_schedule_refs(schedule_html)
    assert schedules[0]["aid"] == "AC_X"
    assert schedules[0]["rid"] == "52"


def main() -> int:
    args = parse_args()
    if args.global_delay_max < args.global_delay_min:
        raise SystemExit("--global-delay-max must be >= --global-delay-min")
    if args.act_delay_max < args.act_delay_min:
        raise SystemExit("--act-delay-max must be >= --act-delay-min")

    self_test()
    acts = ordered_acts(load_missing_acts(), args.priority)
    state = load_state()
    budget = RequestBudget(max_requests=args.max_requests_per_run)
    deadline = time.monotonic() + args.window_hours * 3600
    session = make_session()

    print(
        f"acts_available={len(acts)} limit={args.limit or 'none'} "
        f"mode={args.mode} priority={args.priority} "
        f"dry_run={args.dry_run}",
        flush=True,
    )

    if not args.dry_run:
        fetch_robots(session, args, budget)

    processed = 0
    skipped = 0
    failed = 0
    try:
        for act in acts:
            if time.monotonic() >= deadline:
                raise StopRun("time window exhausted")
            if args.limit and processed + failed >= args.limit:
                raise StopRun("act limit reached")
            if should_skip(act, state, args):
                skipped += 1
                continue
            try:
                process_act(session, act, state, args, budget)
                processed += 1
            except StopRun:
                raise
            except Exception as exc:
                failed += 1
                handle_id = str(act.get("handle_id"))
                state.setdefault("failed", {})[handle_id] = {
                    "at": utc_now(),
                    "title": act.get("short_title"),
                    "error": str(exc),
                }
                record_event(state, "failed", handle_id=handle_id, error=str(exc))
                save_state(state)
                print(f"  failed {handle_id}: {exc}", flush=True)
            if not args.dry_run:
                polite_sleep(args.act_delay_min, args.act_delay_max)
    except StopRun as exc:
        record_event(state, "stopped", reason=str(exc), requests=budget.used)
        if not args.dry_run:
            save_state(state)
        print(f"STOP {exc}", flush=True)
    except KeyboardInterrupt:
        record_event(state, "stopped", reason="interrupted by user", requests=budget.used)
        if not args.dry_run:
            save_state(state)
        print("STOP interrupted by user", flush=True)

    if not args.dry_run:
        save_state(state)
    print(
        f"DONE processed={processed} skipped={skipped} failed={failed} "
        f"requests={budget.used} state={STATE_FILE}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
