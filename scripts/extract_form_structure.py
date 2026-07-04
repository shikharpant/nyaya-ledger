#!/usr/bin/env python3
"""Extract form structure as JSON for each GST form.

Uses the VLM (kai-os--Grug-12B-VLM-8bit-mlx) to extract tables, fields,
and sections from form text. Outputs one JSON file per form.

For forms with rich text (>500 chars): extract directly from text.
For metadata-only forms: extract from the master PDF using pdfplumber,
then VLM for structure.

Output: derived/form_structure/<form_id>.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

VLM_URL = "http://100.79.90.123:8000/v1/chat/completions"
VLM_MODEL = "kai-os--Grug-12B-VLM-8bit-mlx"
VLM_KEY = "omlx-your-secret-key"
MAX_CONCURRENCY = 4
FORMS_NODE_VERSIONS = "derived/version_history/forms/node_versions.jsonl"
OUTPUT_DIR = Path("derived/form_structure")


def load_forms() -> list[dict]:
    """Load latest version of each form."""
    with open(FORMS_NODE_VERSIONS) as f:
        all_versions = [json.loads(line) for line in f if line.strip()]

    latest: dict[str, dict] = {}
    for v in all_versions:
        cid = v.get("component_id", "")
        end = v.get("applicability_end")
        # Pick the latest version (end is None or empty = current)
        if end is None or end == "":
            latest[cid] = v
        elif cid not in latest:
            latest[cid] = v

    return list(latest.values())


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


async def extract_form_structure(
    client: httpx.AsyncClient,
    form_id: str,
    form_text: str,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Extract structure for one form via VLM."""
    async with semaphore:
        prompt = build_prompt(form_id, form_text)
        try:
            resp = await client.post(
                VLM_URL,
                json={
                    "model": VLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4000,
                    "temperature": 0.0,
                },
                headers={"Authorization": f"Bearer {VLM_KEY}"},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()

            # Aggressive JSON extraction
            # Strip markdown code fences
            if "```" in content:
                parts = content.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        content = part
                        break

            # Find the JSON object
            brace_start = content.find("{")
            if brace_start < 0:
                print(f"  ERROR {form_id}: no JSON object found", flush=True)
                return None

            # Find matching close brace
            depth = 0
            brace_end = brace_start
            for i in range(brace_start, len(content)):
                if content[i] == "{":
                    depth += 1
                elif content[i] == "}":
                    depth -= 1
                    if depth == 0:
                        brace_end = i + 1
                        break

            json_str = content[brace_start:brace_end]
            structure = json.loads(json_str)
            return structure
        except Exception as e:
            print(f"  ERROR {form_id}: {type(e).__name__}: {str(e)[:100]}", flush=True)
            return None


async def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    forms = load_forms()
    print(f"Loaded {len(forms)} forms")

    # Split into rich (>500 chars) and metadata-only.
    # Skip forms that already have structure from a previous run.
    rich = []
    metadata_only = []
    for f in forms:
        text = f.get("text", "")
        form_id = f.get("component_id", "").split("/forms/")[-1]
        safe_id = form_id.replace("/", "_")
        existing = OUTPUT_DIR / f"{safe_id}.json"
        if existing.exists():
            with open(existing) as ef:
                existing_data = json.load(ef)
            if existing_data.get("sections"):
                continue  # Already extracted, skip
        if len(text) > 500:
            rich.append(f)
        else:
            metadata_only.append(f)

    print(f"Rich content forms: {len(rich)}")
    print(f"Metadata-only forms: {len(metadata_only)}")
    print(f"Processing rich forms with VLM (max concurrency={MAX_CONCURRENCY})...\n")

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        tasks = []
        for f in rich:
            form_id = f.get("component_id", "").split("/forms/")[-1]
            text = f.get("text", "")
            tasks.append(extract_form_structure(client, form_id, text, semaphore))

        results = await asyncio.gather(*tasks)

    # Write outputs
    success = 0
    failed = 0
    for form_data, result in zip(rich, results):
        form_id = form_data.get("component_id", "").split("/forms/")[-1]
        if result:
            # Sanitize form_id for filesystem (replace / with _)
            safe_id = form_id.replace("/", "_")
            out_path = OUTPUT_DIR / f"{safe_id}.json"
            result["canonical_id"] = form_data.get("component_id", "")
            result["source_text_length"] = len(form_data.get("text", ""))
            with open(out_path, "w") as out:
                json.dump(result, out, indent=2, ensure_ascii=False)
            success += 1

            n_sections = len(result.get("sections", []))
            n_fields = sum(len(s.get("fields", [])) for s in result.get("sections", []))
            n_tables = sum(len(s.get("tables", [])) for s in result.get("sections", []))
            print(f"  OK {form_id:15s}: {n_sections} sections, {n_fields} fields, {n_tables} tables")
        else:
            failed += 1

    # Write metadata-only forms as placeholder structures
    for f in metadata_only:
        form_id = f.get("component_id", "").split("/forms/")[-1]
        safe_id = form_id.replace("/", "_")
        text = f.get("text", "")
        out_path = OUTPUT_DIR / f"{safe_id}.json"
        result = {
            "form_id": form_id.upper(),
            "form_title": text.split("\n")[1] if "\n" in text else form_id,
            "canonical_id": f.get("component_id", ""),
            "source_text_length": len(text),
            "sections": [],
            "note": "Metadata-only — form structure needs PDF extraction",
        }
        with open(out_path, "w") as out:
            json.dump(result, out, indent=2, ensure_ascii=False)

    print(f"\nResults: {success} extracted, {failed} failed, {len(metadata_only)} metadata-only")
    print(f"Output: {OUTPUT_DIR}/")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
