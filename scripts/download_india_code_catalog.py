"""Download the full India Code central act catalog.

Scrapes indiacode.nic.in browse-by-short-title endpoint (single request)
to get all 846 central acts with handle IDs, titles, act numbers, and
enactment dates. Saves to data/Law/india_code_catalog.json.

Use --enrich to additionally fetch PDF URLs and act_ids for each act
(~10 min at the default 0.5s/act delay). Without --enrich, runs in ~5 seconds.

Usage:
    python3 scripts/download_india_code_catalog.py              # catalog only
    python3 scripts/download_india_code_catalog.py --enrich     # + PDF URLs
    python3 scripts/download_india_code_catalog.py --enrich --handles 2435 1768  # specific acts
    python3 scripts/download_india_code_catalog.py --validate   # validate saved catalog
"""

from __future__ import annotations

import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "data" / "Law" / "india_code_catalog.json"

BASE_URL = "https://www.indiacode.nic.in"
BROWSE_URL = (
    f"{BASE_URL}/handle/123456789/1362/browse"
    "?type=shorttitle&sort_by=3&order=ASC&rpp=1000&etal=-1"
)
DELAY = 0.5


def _fetch(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "NyayaLedger/1.0"})
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt < retries - 1:
                wait = DELAY * (attempt + 2)
                print(f"  Retry {attempt + 1}/{retries} after {wait:.0f}s: {exc}")
                time.sleep(wait)
            else:
                raise
    return ""


def _html_unescape(text: str) -> str:
    return unescape(text.replace("&#x20;", " ").replace("&#160;", " "))


def fetch_catalog() -> list[dict]:
    print(f"Fetching browse page...")
    html = _fetch(BROWSE_URL)
    print(f"  Got {len(html)} bytes")

    rows = re.findall(
        r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>"
        r"\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
        html,
        re.DOTALL,
    )
    print(f"  Parsed {len(rows)} rows")

    acts = []
    for row in rows:
        enactment_date = _html_unescape(re.sub(r"<[^>]+>", "", row[0]).strip())
        act_number = _html_unescape(re.sub(r"<[^>]+>", "", row[1]).strip())
        short_title = _html_unescape(re.sub(r"<[^>]+>", "", row[2]).strip())

        handle_match = re.search(r"/handle/123456789/(\d+)", row[3])
        if not handle_match:
            continue
        handle_id = handle_match.group(1)

        year_match = re.search(r"(\d{4})", short_title)
        year = int(year_match.group(1)) if year_match else None

        acts.append(
            {
                "handle_id": handle_id,
                "handle_url": f"{BASE_URL}/handle/123456789/{handle_id}",
                "short_title": short_title,
                "act_number": act_number,
                "enactment_date": enactment_date,
                "year": year,
                "pdf_url": None,
                "pdf_filename": None,
                "act_id": None,
            }
        )

    return acts


def _save_catalog(acts: list[dict]) -> None:
    catalog = {
        "source": "indiacode.nic.in",
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_acts": len(acts),
        "acts": acts,
    }
    OUTPUT_FILE.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def discover_pdf_urls(html: str, handle_id: str) -> list[str]:
    """Return full PDF URLs from the handle page, preserving bitstream sequence."""
    escaped_handle = re.escape(handle_id)
    pattern = re.compile(
        rf"""(?i)(?:href\s*=\s*["'])?(/bitstream/123456789/{escaped_handle}/[^"'<>\s]+?\.pdf)"""
    )
    urls: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(html):
        url = urljoin(BASE_URL, _html_unescape(match.group(1)))
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)

    def pdf_rank(url: str) -> tuple[int, int, str]:
        name = Path(urlparse(url).path).name.lower()
        hindi_penalty = 1 if name.startswith("h") else 0
        seq_match = re.search(rf"/{escaped_handle}/(\d+)/", url)
        sequence = int(seq_match.group(1)) if seq_match else 0
        return (hindi_penalty, -sequence, url)

    return sorted(urls, key=pdf_rank)


def validate_catalog(acts: list[dict]) -> dict[str, object]:
    required = {
        "handle_id",
        "handle_url",
        "short_title",
        "act_number",
        "enactment_date",
        "year",
        "pdf_url",
        "pdf_filename",
        "act_id",
    }
    handle_counts: dict[str, int] = {}
    for act in acts:
        handle_id = act.get("handle_id")
        if handle_id:
            handle_counts[handle_id] = handle_counts.get(handle_id, 0) + 1

    return {
        "total_acts": len(acts),
        "required_keys_present": all(required <= set(act) for act in acts),
        "unique_handles": all(count == 1 for count in handle_counts.values()),
        "with_pdf_url": sum(1 for act in acts if act.get("pdf_url")),
        "with_act_id": sum(1 for act in acts if act.get("act_id")),
        "with_year": sum(1 for act in acts if act.get("year")),
        "missing_act_id": [
            {
                "handle_id": act.get("handle_id"),
                "short_title": act.get("short_title"),
                "pdf_filename": act.get("pdf_filename"),
            }
            for act in acts
            if not act.get("act_id")
        ],
        "missing_year": [
            {
                "handle_id": act.get("handle_id"),
                "short_title": act.get("short_title"),
                "pdf_filename": act.get("pdf_filename"),
            }
            for act in acts
            if not act.get("year")
        ],
    }


def enrich_acts(acts: list[dict], only_handles: set[str] | None = None) -> list[dict]:
    to_enrich = acts
    if only_handles:
        to_enrich = [a for a in acts if a["handle_id"] in only_handles]

    already_done = [a for a in to_enrich if a.get("pdf_url") or a.get("act_id")]
    if already_done:
        skip = len(already_done)
        to_enrich = [a for a in to_enrich if not (a.get("pdf_url") or a.get("act_id"))]
        print(f"  Skipping {skip} already enriched, {len(to_enrich)} remaining")

    if not to_enrich:
        return acts
    print(f"\nEnriching {len(to_enrich)} acts with PDF URLs...")

    for i, act in enumerate(to_enrich):
        if i > 0 and i % 50 == 0:
            print(f"  {i}/{len(to_enrich)}...")
            _save_catalog(acts)
        try:
            html = _fetch(act["handle_url"])
        except Exception as exc:
            print(f"  FAILED handle {act['handle_id']}: {exc}")
            time.sleep(5)
            continue

        pdf_urls = discover_pdf_urls(html, act["handle_id"])
        if pdf_urls:
            chosen = pdf_urls[0]
            act["pdf_filename"] = Path(urlparse(chosen).path).name
            act["pdf_url"] = chosen

        title_match = re.search(r"<title>[^<]*India Code:\s*([^<]+)", html)
        if title_match:
            full_title = _html_unescape(title_match.group(1).strip())
            if len(full_title) > len(act["short_title"]):
                act["short_title"] = full_title

        actid_match = re.search(r"actid=(AC_[A-Za-z0-9_]+)", html)
        if actid_match:
            act["act_id"] = actid_match.group(1)

        time.sleep(DELAY)

    return acts


def main() -> None:
    args = sys.argv[1:]
    do_enrich = "--enrich" in args
    do_validate = "--validate" in args
    handles = []
    if "--handles" in args:
        idx = args.index("--handles")
        handles = args[idx + 1 :]

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if OUTPUT_FILE.exists():
        existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        acts = existing["acts"]
        print(f"Loaded {len(acts)} acts from existing catalog")
    else:
        acts = fetch_catalog()
        print(f"\nFound {len(acts)} central acts")
        _save_catalog(acts)

    if do_enrich:
        only = set(handles) if handles else None
        acts = enrich_acts(acts, only_handles=only)

    _save_catalog(acts)

    if do_validate:
        validation = validate_catalog(acts)
        print("\nValidation:")
        print(json.dumps(validation, indent=2, ensure_ascii=False))

    with_pdf = sum(1 for a in acts if a["pdf_url"])
    with_actid = sum(1 for a in acts if a["act_id"])
    with_year = sum(1 for a in acts if a["year"])

    print(f"\nResults:")
    print(f"  Total acts: {len(acts)}")
    print(f"  With PDF URL: {with_pdf}")
    print(f"  With act_id: {with_actid}")
    print(f"  With year: {with_year}")
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
