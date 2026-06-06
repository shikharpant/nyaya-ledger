#!/usr/bin/env python3
"""Scrape all content from CBIC Tax Portal (taxinformation.cbic.gov.in).

Uses curl subprocess for HTTP (bypasses TLS fingerprint blocking).
Sequential, rate-limited, resumable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

BASE_URL = "https://taxinformation.cbic.gov.in"
STATE_FILE = Path("data/Law/cbic_scrape_state.json")
OUTPUT_DIR = Path("data/Law/cbic_tax_portal")
DELAY_DEFAULT = 60.0
MAX_RETRIES = 1
TIMEOUT = 30
COOLDOWN = 300  # 5 min pause when rate-limited


class StopRun(Exception):
    pass


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text_parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("br", "p", "div", "tr", "li"):
            self._text_parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text_parts.append(data)

    def get_text(self) -> str:
        return "".join(self._text_parts).strip()


def _extract_text(html: str) -> str:
    ext = HTMLTextExtractor()
    ext.feed(html)
    return ext.get_text()


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "untitled"


class CBICClient:
    def __init__(self, delay: float = DELAY_DEFAULT, retries: int = MAX_RETRIES):
        self.delay = delay
        self.retries = retries
        self._token: str | None = None
        self._token_time: float = 0
        self._last_request_at: float = 0

    def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _curl(self, url: str, method: str = "GET", data: str | None = None) -> tuple[int, bytes]:
        self._pace()
        cmd = ["curl", "-sk", "--max-time", str(TIMEOUT), "-X", method, "-w", "\n%{http_code}", url]
        if data is not None:
            cmd.extend(["-H", "Content-Type: application/json", "-d", data])
        if self._token:
            cmd.extend(["-H", f"Authorization: Bearer {self._token}"])
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 15)
        except subprocess.TimeoutExpired as exc:
            self._last_request_at = time.monotonic()
            raise StopRun(f"curl timed out for {url}") from exc
        self._last_request_at = time.monotonic()
        output = proc.stdout
        parts = output.rsplit(b"\n", 1)
        if len(parts) == 2:
            body, code_bytes = parts
            try:
                code = int(code_bytes.strip())
            except ValueError:
                code = 0
        else:
            body = output
            code = 0
        if code == 0:
            raise StopRun(f"HTTP 0 (connection failed) for {url}")
        return code, body

    def _ensure_token(self) -> str:
        if self._token and (time.time() - self._token_time) < 600:
            return self._token
        try:
            code, body = self._curl(f"{BASE_URL}/api/authenticate-token", method="POST", data="{}")
            if code == 200:
                self._token = json.loads(body.decode())["id_token"]
                self._token_time = time.time()
                return self._token
        except StopRun:
            pass
        self._token = None
        raise StopRun("authentication failed")

    def get_json(self, endpoint: str) -> Any:
        url = f"{BASE_URL}/{endpoint}"
        for attempt in range(1, self.retries + 1):
            try:
                self._ensure_token()
                code, body = self._curl(url)
                if code == 404:
                    return None
                if code in (403, 429, 503, 0):
                    raise StopRun(f"HTTP {code} for {endpoint}")
                if code != 200:
                    if attempt < self.retries:
                        time.sleep(self.delay)
                        continue
                    return None
                raw = body.decode()
                if not raw.strip():
                    return None
                return json.loads(raw)
            except StopRun:
                raise
            except Exception as e:
                if attempt < self.retries:
                    self._token = None
                    time.sleep(self.delay)
                    continue
                raise StopRun(f"request failed for {endpoint}: {e}")

    def get_content(self, content_file_path: str) -> dict:
        if not content_file_path:
            return {"contentHtml": None, "contentPdfBase64": None}
        html = self._get_content_html(content_file_path)
        if html:
            return {"contentHtml": html, "contentPdfBase64": None}
        pdf_b64 = self._get_content_pdf(content_file_path)
        if pdf_b64:
            return {"contentHtml": None, "contentPdfBase64": pdf_b64}
        return {"contentHtml": None, "contentPdfBase64": None}

    def get_content_nonfatal(self, content_file_path: str) -> dict:
        return self.get_content(content_file_path)

    def _get_content_html(self, content_file_path: str) -> str | None:
        path = content_file_path.replace("\\", "/")
        url = f"{BASE_URL}/content/html/{quote(path, safe='/')}"
        for attempt in range(1, self.retries + 1):
            try:
                self._ensure_token()
                code, body = self._curl(url)
                if code in (403, 404, 500):
                    return None
                if code in (429, 503, 0):
                    raise StopRun(f"HTTP {code} for content html")
                if code == 200:
                    return body.decode()
                if attempt < self.retries:
                    time.sleep(self.delay)
                    continue
                return None
            except StopRun:
                raise
            except Exception:
                if attempt < self.retries:
                    time.sleep(self.delay)
                    continue
                return None

    def _get_content_pdf(self, content_file_path: str) -> str | None:
        path = content_file_path.replace("\\", "/")
        url = f"{BASE_URL}/content/pdf/{quote(path, safe='/')}"
        for attempt in range(1, self.retries + 1):
            try:
                self._ensure_token()
                code, body = self._curl(url)
                if code in (403, 404, 500):
                    return None
                if code in (429, 503, 0):
                    raise StopRun(f"HTTP {code} for content pdf")
                if code == 200:
                    return json.loads(body.decode()).get("data")
                if attempt < self.retries:
                    time.sleep(self.delay)
                    continue
                return None
            except StopRun:
                raise
            except Exception:
                if attempt < self.retries:
                    time.sleep(self.delay)
                    continue
                return None


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            with open(path) as f:
                self.data = json.load(f)

    def mark_done(self, category: str, item_id: str, record: dict | None = None):
        cat = self.data.setdefault(category, {})
        cat[item_id] = record if record else "done"

    def is_done(self, category: str, item_id: str) -> bool:
        return item_id in self.data.get(category, {})

    def clear(self, category: str, item_id: str):
        self.data.get(category, {}).pop(item_id, None)

    def clear_prefix(self, category: str, prefix: str):
        cat = self.data.get(category, {})
        for key in list(cat):
            if key.startswith(prefix):
                cat.pop(key, None)

    def count_done(self, category: str) -> int:
        return len(self.data.get(category, {}))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)


def _save_json(data: Any, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _record_ref_id(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return str(value or "")


def _section_act_id(section: dict) -> str:
    return _record_ref_id(section.get("actId"))


def _section_chapter_id(section: dict) -> str:
    return _record_ref_id(section.get("chapterId"))


def _chapter_act_id(chapter: dict) -> str:
    return _record_ref_id(chapter.get("actId"))


def _sections_for_act(sections: list[dict], act_id: str) -> list[dict]:
    return [s for s in sections if _section_act_id(s) == act_id]


def _chapters_for_act(chapters: list[dict], act_id: str) -> list[dict]:
    return [ch for ch in chapters if _chapter_act_id(ch) == act_id]


def _sanitize_sections(sections: list[dict]) -> list[dict]:
    return [{
        "sectionNo": s.get("sectionNo", ""),
        "sectionName": s.get("sectionName", ""),
        "contentFilePath": s.get("contentFilePath", ""),
        "contentData": s.get("contentData"),
        "id": s.get("id"),
    } for s in sections]


def scrape_acts(client: CBICClient, state: State):
    print("\n=== Scraping CBIC Acts ===")
    acts = client.get_json("api/cbic-act-msts")
    if not acts:
        print("  No acts found")
        return
    print(f"  Found {len(acts)} acts")

    for act in acts:
        act_id = str(act["id"])
        act_name = act.get("actName", "Unknown")
        state_key = f"act_{act_id}"

        if state.is_done("acts", state_key):
            print(f"  [SKIP] {act_name}")
            continue

        print(f"  [ACT] {act_name} (id={act_id})")
        act_detail = client.get_json(f"api/cbic-act-msts/{act_id}") or act
        chapters_raw = client.get_json(f"api/cbic-act-chapter-msts?actId={act_id}") or []
        chapters = _chapters_for_act(chapters_raw, act_id)
        all_sections_raw = client.get_json(f"api/cbic-act-section-msts?actId={act_id}") or []
        act_sections_raw = _sections_for_act(all_sections_raw, act_id)

        if not chapters or not act_sections_raw:
            content_path = act_detail.get("contentHtmlFilePath") or act_detail.get("contentFilePath") or ""
            content = client.get_content(content_path)
            act_record = {
                "source": "cbic_tax_portal", "type": "act", "act": act_name, "actId": act_id,
                "slug": _slugify(act_name), "issueDt": act_detail.get("issueDt"),
                "amendDt": act_detail.get("amendDt"), "isActive": act_detail.get("isActive"),
                "total_chapters": 0, "total_sections": 0, "chapters": [], "sections": [],
                "section_status": "unavailable_api_mismatch",
                "contentFilePath": act_detail.get("contentFilePath", ""),
                "contentHtmlFilePath": act_detail.get("contentHtmlFilePath", ""),
                "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
                "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
            }
            out_path = OUTPUT_DIR / "acts" / f"{_slugify(act_name)}.json"
            _save_json(act_record, out_path)
            state.clear_prefix("acts_chapters", f"{state_key}_ch_")
            state.mark_done("acts", state_key, {"act": act_name, "chapters": 0, "sections": 0, "section_status": "unavailable_api_mismatch", "has_content": bool(content.get("contentHtml") or content.get("contentPdfBase64"))})
            state.save()
            continue

        ch_id_to_sections: dict[str, list[dict]] = {str(ch["id"]): [] for ch in chapters}
        for sec in act_sections_raw:
            cid = _section_chapter_id(sec)
            ch_id_to_sections.setdefault(cid, []).append(sec)

        all_sections: list[dict] = []
        chapter_data: list[dict] = []

        for ch in chapters:
            ch_id = str(ch["id"])
            ch_no = ch.get("chapterNo", "")
            ch_name = ch.get("chapterName", "")
            ch_sections = _sanitize_sections(ch_id_to_sections.get(ch_id, []))
            for s in ch_sections:
                path = s.get("contentFilePath", "")
                content = client.get_content(path)
                s["contentHtml"] = content.get("contentHtml")
                s["contentPdfBase64"] = content.get("contentPdfBase64")
                s["contentText"] = _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else ""
            all_sections.extend(ch_sections)
            chapter_data.append({"chapterNo": ch_no, "chapterName": ch_name, "id": ch["id"], "sections": ch_sections})
            print(f"    {ch_no}: {ch_name} - {len(ch_sections)} sections", flush=True)

        out_path = OUTPUT_DIR / "acts" / f"{_slugify(act_name)}.json"
        _save_json({
            "source": "cbic_tax_portal", "type": "act", "act": act_name, "actId": act_id,
            "slug": _slugify(act_name), "issueDt": act.get("issueDt"), "amendDt": act.get("amendDt"),
            "isActive": act.get("isActive"), "total_chapters": len(chapters), "total_sections": len(all_sections),
            "chapters": chapter_data, "sections": all_sections, "section_status": "ok",
        }, out_path)
        state.mark_done("acts", state_key, {"act": act_name, "chapters": len(chapters), "sections": len(all_sections)})
        state.save()
        print(f"    -> Saved {len(all_sections)} sections")
    print(f"  Acts done: {state.count_done('acts')}")


def scrape_rules(client: CBICClient, state: State):
    print("\n=== Scraping CBIC Rules ===")
    rules = client.get_json("api/cbic-rule-msts")
    if not rules:
        print("  No rules found")
        return
    print(f"  Found {len(rules)} rules")

    for rule in rules:
        rule_id = str(rule["id"])
        rule_name = rule.get("ruleDocName", "Unknown")
        state_key = f"rule_{rule_id}"
        if state.is_done("rules", state_key):
            continue
        rule_detail = client.get_json(f"api/cbic-rule-msts/{rule_id}")
        if not rule_detail:
            state.mark_done("rules", state_key, {"name": rule_name})
            state.save()
            continue
        content_path = rule_detail.get("contentFilePath", "")
        content = client.get_content(content_path)
        _save_json({
            "source": "cbic_tax_portal", "type": "rule", "name": rule_name, "ruleId": rule_id,
            "slug": _slugify(rule_name), "ruleDocNo": rule_detail.get("ruleDocNo"),
            "ruleCategory": rule_detail.get("ruleCategory", ""), "issueDt": rule_detail.get("issueDt"),
            "amendDt": rule_detail.get("amendDt"), "isActive": rule_detail.get("isActive"),
            "contentFilePath": content_path,
            "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
            "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
        }, OUTPUT_DIR / "rules" / f"{_slugify(rule_name)}.json")
        state.mark_done("rules", state_key, {"name": rule_name, "has_content": bool(content.get("contentHtml") or content.get("contentPdfBase64"))})
        state.save()
    print(f"  Rules done: {state.count_done('rules')}")


def scrape_regulations(client: CBICClient, state: State):
    print("\n=== Scraping CBIC Regulations ===")
    reg_docs = client.get_json("api/cbic-regulation-doc-msts")
    if not reg_docs:
        print("  No regulation docs found")
        return
    print(f"  Found {len(reg_docs)} regulation docs")
    all_regulations = client.get_json("api/cbic-regulation-msts") or []
    print(f"  Found {len(all_regulations)} regulation chapters total")

    reg_by_doc: dict[str, list[dict]] = {}
    for reg in all_regulations:
        doc = reg.get("regulationDoc") or reg.get("regulationDocId")
        doc_id = str(doc.get("id") if isinstance(doc, dict) else doc) if doc else "unknown"
        reg_by_doc.setdefault(doc_id, []).append(reg)

    for doc in reg_docs:
        doc_id = str(doc["id"])
        doc_name = doc.get("regulationDocName", "Unknown")
        state_key = f"regdoc_{doc_id}"
        if state.is_done("regulations", state_key):
            continue
        chapters = reg_by_doc.get(doc_id, [])
        chapter_records = []
        for ch in chapters:
            ch_path = ch.get("contentFilePath", "")
            content = client.get_content(ch_path)
            chapter_records.append({
                "regulationNo": ch.get("regulationNo", ""), "regulationName": ch.get("regulationName", ""),
                "contentFilePath": ch_path,
                "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
                "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
                "issueDt": ch.get("issueDt"), "amendDt": ch.get("amendDt"),
            })
        _save_json({
            "source": "cbic_tax_portal", "type": "regulation_doc", "name": doc_name, "docId": doc_id,
            "slug": _slugify(doc_name), "regulationCategory": doc.get("regulationCategory", ""),
            "issueDt": doc.get("issueDt"), "amendDt": doc.get("amendDt"),
            "total_chapters": len(chapter_records), "chapters": chapter_records,
        }, OUTPUT_DIR / "regulations" / f"{_slugify(doc_name)}.json")
        state.mark_done("regulations", state_key, {"name": doc_name, "chapters": len(chapter_records)})
        state.save()
        print(f"    -> Saved {len(chapter_records)} chapters")
    print(f"  Regulation docs done: {state.count_done('regulations')}")


def scrape_forms(client: CBICClient, state: State):
    print("\n=== Scraping CBIC Forms ===")
    forms = client.get_json("api/cbic-form-msts")
    if not forms:
        print("  No forms found")
        return
    print(f"  Found {len(forms)} forms")

    for form in forms:
        form_id = str(form["id"])
        form_name = form.get("formName", form.get("formNo", "Unknown"))
        state_key = f"form_{form_id}"
        if state.is_done("forms", state_key):
            continue
        form_detail = client.get_json(f"api/cbic-form-msts/{form_id}")
        if not form_detail:
            state.mark_done("forms", state_key, {"name": form_name})
            state.save()
            continue
        content_path = form_detail.get("contentFilePath", "")
        doc_path = form_detail.get("docFilePath", "")
        content = client.get_content_nonfatal(content_path)
        if not content.get("contentHtml") and not content.get("contentPdfBase64") and doc_path:
            doc_content = client.get_content_nonfatal(doc_path)
            if doc_content.get("contentHtml") or doc_content.get("contentPdfBase64"):
                content = doc_content
        _save_json({
            "source": "cbic_tax_portal", "type": "form",
            "formNo": form_detail.get("formNo", ""), "name": form_detail.get("formName", form_name),
            "formId": form_id, "slug": _slugify(f"{form_detail.get('formNo', '')} {form_name}"),
            "formCategory": form_detail.get("formCategory", ""),
            "issueDt": form_detail.get("issueDt"), "amendDt": form_detail.get("amendDt"),
            "contentFilePath": content_path, "docFilePath": doc_path,
            "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
            "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
        }, OUTPUT_DIR / "forms" / f"{_slugify(form_name)[:80]}_{form_id}.json")
        state.mark_done("forms", state_key, {"name": form_name, "has_content": bool(content.get("contentHtml") or content.get("contentPdfBase64"))})
        state.save()
    print(f"  Forms done: {state.count_done('forms')}")


def scrape_orders(client: CBICClient, state: State):
    print("\n=== Scraping CBIC Orders ===")
    orders = client.get_json("api/cbic-order-msts")
    if not orders:
        print("  No orders found")
        return
    print(f"  Found {len(orders)} orders")

    for order in orders:
        order_id = str(order["id"])
        order_name = order.get("orderName", "Unknown")
        state_key = f"order_{order_id}"
        if state.is_done("orders", state_key):
            continue
        order_detail = client.get_json(f"api/cbic-order-msts/{order_id}")
        if not order_detail:
            state.mark_done("orders", state_key, {"name": order_name})
            state.save()
            continue
        content_path = order_detail.get("contentFilePath", "")
        doc_path = order_detail.get("docFilePath", "")
        content = client.get_content(content_path)
        if not content.get("contentHtml") and not content.get("contentPdfBase64") and doc_path:
            content = client.get_content(doc_path)
        slug = _slugify(f"{order_detail.get('orderNo', '')} {order_name}")[:80]
        _save_json({
            "source": "cbic_tax_portal", "type": "order",
            "name": order_detail.get("orderName", order_name), "no": order_detail.get("orderNo", ""),
            "orderId": order_id, "category": order_detail.get("orderCategory", ""),
            "issueDt": order_detail.get("issueDt"), "amendDt": order_detail.get("amendDt"),
            "isActive": order_detail.get("isActive"),
            "contentFilePath": content_path, "docFilePath": doc_path,
            "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
            "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
        }, OUTPUT_DIR / "orders" / f"{slug}_{order_id}.json")
        state.mark_done("orders", state_key, {"name": order_name, "has_content": bool(content.get("contentHtml") or content.get("contentPdfBase64"))})
        state.save()
    print(f"  Orders done: {state.count_done('orders')}")


def scrape_instructions(client: CBICClient, state: State):
    print("\n=== Scraping CBIC Instructions ===")
    instructions = client.get_json("api/cbic-instruction-msts")
    if not instructions:
        print("  No instructions found")
        return
    print(f"  Found {len(instructions)} instructions")

    for inst in instructions:
        inst_id = str(inst["id"])
        inst_name = inst.get("instructionName", "Unknown")
        state_key = f"instruction_{inst_id}"
        if state.is_done("instructions", state_key):
            continue
        inst_detail = client.get_json(f"api/cbic-instruction-msts/{inst_id}")
        if not inst_detail:
            state.mark_done("instructions", state_key, {"name": inst_name})
            state.save()
            continue
        content_path = inst_detail.get("contentFilePath", "")
        doc_path = inst_detail.get("docFilePath", "")
        content = client.get_content(content_path)
        if not content.get("contentHtml") and not content.get("contentPdfBase64") and doc_path:
            content = client.get_content(doc_path)
        slug = _slugify(f"{inst_detail.get('instructionNo', '')} {inst_name}")[:80]
        _save_json({
            "source": "cbic_tax_portal", "type": "instruction",
            "name": inst_detail.get("instructionName", inst_name), "no": inst_detail.get("instructionNo", ""),
            "instructionId": inst_id, "issueDt": inst_detail.get("issueDt"),
            "amendDt": inst_detail.get("amendDt"), "isActive": inst_detail.get("isActive"),
            "contentFilePath": content_path, "docFilePath": doc_path,
            "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
            "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
        }, OUTPUT_DIR / "instructions" / f"{slug}_{inst_id}.json")
        state.mark_done("instructions", state_key, {"name": inst_name, "has_content": bool(content.get("contentHtml") or content.get("contentPdfBase64"))})
        state.save()
    print(f"  Instructions done: {state.count_done('instructions')}")


def _scrape_by_id_range(client: CBICClient, state: State, category: str, api_prefix: str,
                        id_start: int, id_end: int, name_field: str, no_field: str, output_subdir: str):
    print(f"\n=== Scraping CBIC {category} (ID {id_start}-{id_end}) ===")
    found = failed = skipped = 0
    save_every = 50

    all_ids = list(range(id_start, id_end + 1))
    todo_ids = [nid for nid in all_ids if not state.is_done(category, str(nid))]
    skipped = len(all_ids) - len(todo_ids)
    print(f"  Total: {len(all_ids)}, done: {skipped}, to process: {len(todo_ids)}")

    for count, nid in enumerate(todo_ids, 1):
        state_key = str(nid)
        record = client.get_json(f"api/{api_prefix}/{nid}")

        if record is None:
            failed += 1
            state.mark_done(category, state_key, {"error": "null"})
            state.save()
            if count % save_every == 0:
                print(f"  [{category}] found={found} failed={failed} id={nid} ({count}/{len(todo_ids)})", flush=True)
            continue

        found += 1
        name = record.get(name_field, f"{category}_{nid}")
        no = record.get(no_field, "")
        content_path = record.get("contentFilePath", "")
        doc_path = record.get("docFilePath", "")
        doc_type = record.get("docFileType", "")

        content = client.get_content(content_path)
        if not content.get("contentHtml") and not content.get("contentPdfBase64") and doc_path:
            content = client.get_content(doc_path)

        category_val = record.get("notificationCategory", record.get("circularCategory", record.get("orderCategory", "")))
        slug = _slugify(f"{no} {name}")[:80] if no else _slugify(name)[:80]
        _save_json({
            "source": "cbic_tax_portal", "type": category.rstrip("s"),
            "name": name, "no": no, "id": nid, "category": category_val,
            "issueDt": record.get("issueDt") or record.get("notificationDt") or record.get("circularDt"),
            "amendDt": record.get("amendDt"), "isActive": record.get("isActive"),
            "contentFilePath": content_path, "docFilePath": doc_path, "docFileType": doc_type,
            "contentHtml": content.get("contentHtml"), "contentPdfBase64": content.get("contentPdfBase64"),
            "contentText": _extract_text(content.get("contentHtml") or "") if content.get("contentHtml") else "",
        }, OUTPUT_DIR / output_subdir / f"{slug}_{nid}.json")

        state.mark_done(category, state_key, {"name": name[:60], "no": no, "has_content": bool(content.get("contentHtml") or content.get("contentPdfBase64"))})
        state.save()
        if count % save_every == 0:
            print(f"  [{category}] found={found} failed={failed} id={nid} ({count}/{len(todo_ids)})", flush=True)

    state.save()
    print(f"  {category} done: found={found} failed={failed} total={state.count_done(category)}")


def scrape_notifications(client: CBICClient, state: State, start_id: int = 1000001, end_id: int = 1010700):
    _scrape_by_id_range(client, state, "notifications", "cbic-notification-msts", start_id, end_id, "notificationName", "notificationNo", "notifications")


def scrape_circulars(client: CBICClient, state: State, start_id: int = 1000001, end_id: int = 1003400):
    _scrape_by_id_range(client, state, "circulars", "cbic-circular-msts", start_id, end_id, "circularName", "circularNo", "circulars")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--what", default="all", help="Comma-separated: all,acts,rules,regulations,forms,notifications,circulars,orders,instructions")
    parser.add_argument("--delay", type=float, default=DELAY_DEFAULT, help="Seconds between requests")
    parser.add_argument("--retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--notif-start", type=int, default=1000001)
    parser.add_argument("--notif-end", type=int, default=1010700)
    parser.add_argument("--circ-start", type=int, default=1000001)
    parser.add_argument("--circ-end", type=int, default=1003400)
    args = parser.parse_args()

    what = {w.strip() for w in args.what.split(",")}
    if "all" in what:
        what = {"acts", "rules", "regulations", "forms", "notifications", "circulars", "orders", "instructions"}

    client = CBICClient(delay=args.delay, retries=args.retries)
    state = State(STATE_FILE)

    print(f"CBIC Tax Portal Scraper (curl) — delay={args.delay}s")
    print(f"State: {STATE_FILE}")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Scraping: {', '.join(sorted(what))}")

    try:
        if "acts" in what: scrape_acts(client, state)
        if "rules" in what: scrape_rules(client, state)
        if "regulations" in what: scrape_regulations(client, state)
        if "forms" in what: scrape_forms(client, state)
        if "notifications" in what: scrape_notifications(client, state, args.notif_start, args.notif_end)
        if "circulars" in what: scrape_circulars(client, state, args.circ_start, args.circ_end)
        if "orders" in what: scrape_orders(client, state)
        if "instructions" in what: scrape_instructions(client, state)
    except StopRun as exc:
        state.save()
        print(f"\nSTOP: {exc}", flush=True)
        return 2

    print("\n=== Done ===")
    for cat in ["acts", "rules", "regulations", "forms", "notifications", "circulars", "orders", "instructions"]:
        count = state.count_done(cat)
        if count > 0:
            print(f"  {cat}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
