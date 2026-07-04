#!/usr/bin/env python3
"""Extract form structure JSON for all GST forms lacking populated sections.

Unlike extract_form_structure.py (which only reads node_versions.jsonl), this
script draws form text from three sources in priority of text length:

  1. corpus_xml   - corpus/in/union/forms/*/form.xml (mapped by canonical_id)
  2. node_versions - derived/version_history/forms/node_versions.jsonl (latest)
  3. master_pdf   - data/Law/base_laws/cgst-rules-2017-part-b-forms.pdf,
                    split into per-form chunks by detecting top-of-page
                    "FORM GST XXX-NN" headers.

Forms with >200 chars of source text are sent to the VLM
(kai-os--Grug-12B-VLM-8bit-mlx) for structured extraction. Forms that have
no usable text in any source are rewritten with a descriptive placeholder
note explaining what is missing.

Output (one file per form): derived/form_structure/<form_id>.json
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

VLM_URL = "http://100.79.90.123:8000/v1/chat/completions"
VLM_MODEL = "kai-os--Grug-12B-VLM-8bit-mlx"
VLM_KEY = "omlx-your-secret-key"
MAX_TOKENS = 8000  # model context is 65536; legit large forms (GSTR-3B) need ~4500,
MAX_CONCURRENCY = int(os.environ.get("EXTRACT_CONCURRENCY", "2"))
RETRIES = 3
BACKOFF_SECONDS = 8.0
TEXT_THRESHOLD = 200  # chars required to attempt VLM extraction

FORMS_NODE_VERSIONS = Path("derived/version_history/forms/node_versions.jsonl")
CORPUS_FORMS_DIR = Path("corpus/in/union/forms")
MASTER_PDF = Path("data/Law/base_laws/cgst-rules-2017-part-b-forms.pdf")
OUTPUT_DIR = Path("derived/form_structure")

# Top-of-page form header, e.g. "FORM GST REG-01", "FORM GST DRC-07A"
PDF_HEADER_RE = re.compile(r"FORM\s+GST\s+([A-Z]+)\s*[-\s]\s*(\d+[A-Z]?)", re.IGNORECASE)
PDF_TOC_PAGES = 8  # skip the cover + table-of-contents pages
PDF_MAX_PAGES_PER_FORM = 15  # cap runaway chunks


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------
def load_empty_targets() -> list[dict]:
    """Return form_ids whose form_structure JSON has no populated sections."""
    targets = []
    for jf in sorted(OUTPUT_DIR.glob("*.json")):
        data = json.loads(jf.read_text())
        if data.get("sections"):
            continue
        form_id = jf.stem  # filename without .json
        targets.append({
            "form_id": form_id,
            "canonical_id": data.get("canonical_id", f"/in/union/forms/{form_id}"),
            "existing_form_title": data.get("form_title", ""),
            "out_path": jf,
        })
    return targets


def build_corpus_map() -> dict[str, str]:
    """canonical_id -> concatenated text from corpus XML."""
    out: dict[str, str] = {}
    for xml_path in CORPUS_FORMS_DIR.glob("*/form.xml"):
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError:
            continue
        cid = None
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag == "property" and el.get("name") == "canonical_id":
                cid = el.get("value")
                break
        if cid is None:
            for el in root.iter():
                if el.tag.split("}")[-1] == "FRBRthis":
                    cid = el.get("value")
                    break
        if not cid:
            continue
        text = " ".join(s.strip() for s in root.itertext() if s.strip())
        # keep the longest text if multiple versions share a canonical_id
        if cid not in out or len(text) > len(out[cid]):
            out[cid] = text
    return out


def build_node_versions_map() -> dict[str, str]:
    """canonical_id -> latest-version text from node_versions.jsonl."""
    latest: dict[str, str] = {}
    with open(FORMS_NODE_VERSIONS) as f:
        for line in f:
            if not line.strip():
                continue
            v = json.loads(line)
            cid = v.get("component_id", "")
            if not cid:
                continue
            end = v.get("applicability_end")
            text = v.get("text", "")
            is_current = end is None or end == ""
            # current version always wins; otherwise keep first seen
            if is_current or cid not in latest:
                latest[cid] = text
    return latest


def build_pdf_map() -> dict[str, str]:
    """form_id -> text chunk from the master forms PDF."""
    try:
        import pdfplumber
    except ImportError:
        print("  pdfplumber not available; skipping master PDF source", flush=True)
        return {}

    def norm(alpha: str, num: str) -> str:
        return f"gst-{alpha.lower()}-{num.lower()}"

    with pdfplumber.open(MASTER_PDF) as pdf:
        pages_text = [(page.extract_text() or "") for page in pdf.pages]

    # top-of-page anchors partition the body into per-form runs
    anchors: list[tuple[int, str]] = []
    for i, txt in enumerate(pages_text):
        if i < PDF_TOC_PAGES:
            continue
        head = txt[:400]
        top_ids = {norm(m.group(1), m.group(2)) for m in PDF_HEADER_RE.finditer(head)}
        for fid in top_ids:
            anchors.append((i, fid))
    anchors.sort()

    chunks: dict[str, str] = {}
    for idx, (start, fid) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(pages_text)
        end = min(end, start + PDF_MAX_PAGES_PER_FORM)
        text = "\n".join(pages_text[start:end])
        if fid not in chunks or len(text) > len(chunks[fid]):
            chunks[fid] = text
    return chunks


def select_source(
    target: dict,
    corpus: dict[str, str],
    nodever: dict[str, str],
    pdf: dict[str, str],
) -> tuple[str, str, str]:
    """Pick the longest usable source text. Returns (text, source_name, source_lengths_dbg)."""
    cid = target["canonical_id"]
    form_id = target["form_id"]
    candidates = [
        (corpus.get(cid, ""), "corpus_xml"),
        (nodever.get(cid, ""), "node_versions"),
        (pdf.get(form_id, ""), "master_pdf"),
    ]
    text, source = max(candidates, key=lambda c: len(c[0]))
    dbg = ", ".join(f"{name}={len(t)}" for t, name in candidates)
    return text, source, dbg


# ---------------------------------------------------------------------------
# VLM extraction
# ---------------------------------------------------------------------------
def build_prompt(form_id: str, form_text: str) -> str:
    return f"""You are a legal form structure extractor. Given the text of an Indian GST form, extract its complete structure as JSON.

Return ONLY valid JSON (no markdown, no explanation) with this schema:
{{
  "form_id": "{form_id}",
  "form_title": "Full title of the form",
  "rule_reference": "The rule that prescribes this form, e.g. 'rule 59(1)'",
  "sections": [
    {{
      "section_id": "A",
      "section_title": "Section title",
      "fields": [
        {{"id": "1", "label": "Field label", "type": "text|number|date|boolean|dropdown"}}
      ],
      "tables": [
        {{
          "table_id": "4",
          "title": "Table description",
          "columns": [
            {{"name": "Column Name", "type": "text|number|date"}}
          ]
        }}
      ]
    }}
  ]
}}

Extract ALL sections, fields, and tables from this form. Be thorough — every numbered item, every table column header.

Form text:
{form_text[:4000]}
"""


def _extract_json_object(content: str) -> str | None:
    """Pull the first balanced {...} object out of a model response."""
    if "```" in content:
        for part in content.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                content = part
                break
    start = content.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[start:i + 1]
    return None


def _repair_truncated_json(s: str) -> str | None:
    """Best-effort recovery of a JSON object that was cut off mid-stream.

    Large forms (e.g. GSTR-3B) can exceed the token budget before the model
    closes the outermost object. This walks the string respecting string
    literals, cuts at the last cleanly-closed container or value separator,
    then appends the closers needed to balance any still-open { / [.
    Returns a string that json.loads can parse, or None if nothing usable.
    """
    if "{" not in s:
        return None
    s = s[s.find("{"):]
    stack: list[str] = []
    in_str = False
    escape = False
    last_safe = -1  # index (exclusive) of last clean cut point
    for i, ch in enumerate(s):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and (
                (ch == "}" and stack[-1] == "{")
                or (ch == "]" and stack[-1] == "[")
            ):
                stack.pop()
                last_safe = i + 1
        elif ch == ",":
            last_safe = i  # cut will drop the trailing comma
    if not stack:
        return s  # already balanced
    cut = s[: last_safe] if last_safe > 0 else s[:1]
    cut = cut.rstrip()
    if cut.endswith(","):
        cut = cut[:-1]
    # recompute the open-container stack for the trimmed string
    stk: list[str] = []
    in_s = False
    esc = False
    for ch in cut:
        if in_s:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_s = False
            continue
        if ch == '"':
            in_s = True
        elif ch in "{[":
            stk.append(ch)
        elif ch in "}]":
            if stk and (
                (ch == "}" and stk[-1] == "{")
                or (ch == "]" and stk[-1] == "[")
            ):
                stk.pop()
    closers = "".join("}" if c == "{" else "]" for c in reversed(stk))
    return cut + closers


def _parse_structure(content: str) -> dict | None:
    """Parse the model response into a structure dict, repairing truncation."""
    json_str = _extract_json_object(content)
    if json_str is not None:
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass
    # truncated: fall back to repairing from the first '{'
    repaired = _repair_truncated_json(content)
    if repaired is not None:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None
    return None


async def extract_form_structure(
    client: httpx.AsyncClient,
    form_id: str,
    form_text: str,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Extract structure for one form via VLM, with retry/backoff."""
    prompt = build_prompt(form_id, form_text)
    payload = {
        "model": VLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {VLM_KEY}"}
    # Transient conditions that warrant a retry. Under concurrency the endpoint
    # sometimes returns HTTP 200 with empty/no-JSON content; those are treated
    # as retryable degradation rather than permanent failures.
    transport_errors = (
        httpx.TimeoutException,
        httpx.TransportError,
        httpx.HTTPStatusError,
    )
    async with semaphore:
        for attempt in range(1, RETRIES + 2):  # initial + RETRIES
            try:
                resp = await client.post(VLM_URL, json=payload, headers=headers, timeout=180)
                resp.raise_for_status()
                data = resp.json()
                content = (data["choices"][0]["message"]["content"] or "").strip()
                structure = _parse_structure(content)
                if structure is None:
                    raise ValueError(
                        f"could not parse JSON (content_len={len(content)})"
                    )
                return structure  # may have empty sections; main() decides
            except transport_errors as e:
                err = f"{type(e).__name__}: {str(e)[:80]}"
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                err = f"{type(e).__name__}: {str(e)[:80]}"
            # transient/parse failure -> retry with backoff
            if attempt <= RETRIES:
                print(
                    f"  retry {attempt}/{RETRIES} {form_id}: {err}",
                    flush=True,
                )
                await asyncio.sleep(BACKOFF_SECONDS)
            else:
                print(f"  ERROR {form_id}: exhausted retries: {err}", flush=True)
                return None
        return None


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------
def write_extracted(target: dict, text: str, source: str, structure: dict) -> None:
    # Normalise alternate keys the model sometimes emits
    if "form_id" not in structure:
        structure["form_id"] = structure.pop(
            "form_name", target["form_id"].upper()
        )
    if "form_title" not in structure:
        structure["form_title"] = structure.pop(
            "title", target["existing_form_title"] or target["form_id"].upper()
        )
    structure.setdefault("form_id", target["form_id"].upper())
    structure["canonical_id"] = target["canonical_id"]
    structure["source_text_length"] = len(text)
    structure["source"] = source
    structure.setdefault("sections", [])
    target["out_path"].write_text(json.dumps(structure, indent=2, ensure_ascii=False))


def write_placeholder(
    target: dict,
    text: str,
    source: str,
    dbg: str,
    reason: str,
) -> None:
    """Write a descriptive placeholder when extraction is not possible."""
    title = target["existing_form_title"] or target["form_id"].upper()
    note = (
        f"{reason}. Source text below the {TEXT_THRESHOLD}-char extraction threshold. "
        f"Sources checked: {dbg}. Selected source: {source or 'none'} "
        f"(len={len(text)}). This form's full body is not present in the corpus XML, "
        f"node_versions, or the 2017 master forms PDF; it must be sourced from the "
        f"CBIC portal or a later-amended rules PDF."
    )
    out = {
        "form_id": target["form_id"].upper(),
        "form_title": title,
        "canonical_id": target["canonical_id"],
        "source_text_length": len(text),
        "sections": [],
        "source": source or "none",
        "note": note,
    }
    target["out_path"].write_text(json.dumps(out, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading targets...", flush=True)
    targets = load_empty_targets()
    print(f"  {len(targets)} forms without populated sections", flush=True)

    print("Loading source text (corpus XML, node_versions, master PDF)...", flush=True)
    corpus = build_corpus_map()
    nodever = build_node_versions_map()
    pdf = build_pdf_map()
    print(f"  corpus_xml: {len(corpus)} ids, master_pdf: {len(pdf)} chunks", flush=True)

    # Decide what to do with each target
    to_extract: list[tuple[dict, str, str]] = []  # (target, text, source)
    placeholders: list[tuple[dict, str, str, str]] = []  # (target, text, source, dbg)
    for t in targets:
        text, source, dbg = select_source(t, corpus, nodever, pdf)
        if len(text) >= TEXT_THRESHOLD:
            to_extract.append((t, text, source))
        else:
            placeholders.append((t, text, source, dbg))

    print(
        f"Will VLM-extract: {len(to_extract)}, placeholder-only: {len(placeholders)}",
        flush=True,
    )

    # Run VLM extraction for all candidate forms. We do NOT use a single
    # probe to gate the whole batch: one degenerate form (e.g. a model that
    # loops on a confusing input) must not poison the others. Instead we run
    # every form with per-form retry, and only declare the endpoint down if
    # every single call returns None.
    extracted = 0
    failed = 0
    endpoint_down = False
    if to_extract:
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        async with httpx.AsyncClient() as client:
            tasks = [
                extract_form_structure(client, t["form_id"], text, semaphore)
                for (t, text, _src) in to_extract
            ]
            results = await asyncio.gather(*tasks)

        # If every result is None, the endpoint is presumed unreachable/degraded
        non_none = [r for r in results if r is not None]
        if results and not non_none:
            endpoint_down = True
            print(
                "  VLM endpoint appears DOWN/fully degraded (all calls returned None); "
                "writing placeholders for extractable forms.",
                flush=True,
            )

        for (t, text, src), structure in zip(to_extract, results):
            if structure and structure.get("sections"):
                write_extracted(t, text, src, structure)
                extracted += 1
                n_sec = len(structure.get("sections", []))
                n_fld = sum(len(s.get("fields", [])) for s in structure.get("sections", []))
                n_tbl = sum(len(s.get("tables", [])) for s in structure.get("sections", []))
                print(
                    f"  OK   {t['form_id']:18s}: {n_sec} sections, {n_fld} fields, "
                    f"{n_tbl} tables (source={src})",
                    flush=True,
                )
            elif structure is not None:
                # VLM returned valid JSON but no sections (metadata-only source);
                # record an honest placeholder rather than a failure.
                failed += 1
                dbg = f"selected={src} (len={len(text)}); VLM returned empty sections"
                write_placeholder(
                    t, text, src, dbg,
                    "VLM found no extractable structure in the available text",
                )
                print(f"  EMPTY {t['form_id']:18s}: VLM returned no sections", flush=True)
            else:
                failed += 1
                print(f"  FAIL {t['form_id']:18s}: VLM returned no structure", flush=True)

    # Placeholders for everything that could not be extracted
    placeholder_count = len(placeholders)
    if endpoint_down and to_extract:
        # VLM down: treat all extractable forms as placeholders too
        placeholder_count += len(to_extract)
        for (t, text, src) in to_extract:
            dbg = f"corpus_xml=?, node_versions=?, master_pdf=?; selected={src}"
            placeholders.append((t, text, src, dbg))

    for (t, text, src, dbg) in placeholders:
        reason = "VLM endpoint unavailable" if endpoint_down else "No source text"
        write_placeholder(t, text, src, dbg, reason)

    # Final summary
    print("\n=== SUMMARY ===", flush=True)
    print(f"  newly extracted (populated): {extracted}", flush=True)
    print(f"  extraction failures:         {failed}", flush=True)
    print(f"  placeholders written:        {placeholder_count}", flush=True)

    # Re-scan the whole output dir for a true final tally
    populated = empty = total_sections = total_fields = total_tables = 0
    for jf in sorted(OUTPUT_DIR.glob("*.json")):
        d = json.loads(jf.read_text())
        secs = d.get("sections", [])
        if secs:
            populated += 1
            total_sections += len(secs)
            total_fields += sum(len(s.get("fields", [])) for s in secs)
            total_tables += sum(len(s.get("tables", [])) for s in secs)
        else:
            empty += 1
    print(f"  --- derived/form_structure/ now ---", flush=True)
    print(f"  populated: {populated}   empty: {empty}   total: {populated + empty}", flush=True)
    print(
        f"  totals across populated: {total_sections} sections, "
        f"{total_fields} fields, {total_tables} tables",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
