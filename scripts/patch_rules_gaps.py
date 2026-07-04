#!/usr/bin/env python3
"""Manually patch 4 remaining Rules version-history gaps.

These events couldn't be materialized by the materializer due to:
- Sub-component content node issues (rule/8/4b, rule/46 proviso)
- Forms lane routing (rule/163 GSTR-1A splice)

This script directly patches node_versions.jsonl to apply the amendments.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    input_path = Path("derived/version_history/cgst-rules-2017/node_versions.jsonl")
    output_path = Path("derived/version_history/cgst-rules-2017/node_versions.jsonl")

    rows: list[dict] = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    patched = 0

    for row in rows:
        cid = row.get("component_id", "")
        start = row.get("applicability_start", "")
        text = row.get("text", "")

        # Fix 1: Rule 8/subrule/4b — substitute "provisions of" -> "proviso to"
        # Effective from 2022-12-26 onwards
        if cid == "/in/union/rules/cgst-rules-2017/rule/8/subrule/4b":
            if start >= "2022-12-26" and "provisions of sub-rule" in text:
                row["text"] = text.replace(
                    "provisions of sub-rule (4A)",
                    "proviso to sub-rule (4A)",
                )
                row["text_sha256"] = sha256_text(row["text"])
                row["manual_patch"] = "rule8_4b_provisions_to_proviso"
                patched += 1
                print(f"Patched rule/8/subrule/4b at {start}: 'provisions of' -> 'proviso to'")

        # Fix 2: Rule 163 — insert GSTR-1A reference
        # The SPLICE should insert ", as amended in FORM GSTR-1A if any," 
        # after "FORM GSTR-1" in clause (c) of sub-rule (1)
        # Effective from 2024-07-10
        if cid == "/in/union/rules/cgst-rules-2017/rule/163":
            if start >= "2024-07-10" and "GSTR-1A" not in text and "FORM GSTR-1" in text:
                # Find "FORM GSTR-1" and insert after it
                old_phrase = "FORM GSTR-1"
                new_phrase = "FORM GSTR-1, as amended in FORM GSTR-1A if any,"
                # But only for the occurrence in clause (c), not the heading
                # The text has "FORM GST REG-01" and "FORM GSTR-3B" and "FORM GSTR-1"
                # We need to find the specific "FORM GSTR-1" reference (not GSTR-1A or GSTR-1B)
                patched_text = text
                # Replace "FORM GSTR-1" that's NOT followed by "A" or ","
                patched_text = re.sub(
                    r"(FORM GSTR-1)(?![AB],)",
                    r"\1, as amended in FORM GSTR-1A if any,",
                    patched_text,
                    count=1,  # Only first occurrence
                )
                if patched_text != text:
                    row["text"] = patched_text
                    row["text_sha256"] = sha256_text(patched_text)
                    row["manual_patch"] = "rule163_gstr1a_splice"
                    patched += 1
                    print(f"Patched rule/163 at {start}: inserted GSTR-1A reference")

        # Fix 3: Rule 46 proviso — normalize for "Provided also that"
        # The substitution changes "Provided also that in the case of" to
        # "Provided further that in the case of" effective 2024-11-01
        if cid == "/in/union/rules/cgst-rules-2017/rule/46":
            if start >= "2024-11-01" and "Provided also that" in text:
                row["text"] = text.replace(
                    "Provided also that in the case of",
                    "Provided further that in\nthe case of",
                )
                row["text_sha256"] = sha256_text(row["text"])
                row["manual_patch"] = "rule46_provided_also_to_further"
                patched += 1
                print(f"Patched rule/46 at {start}: 'Provided also' -> 'Provided further'")

    # Write output
    with open(output_path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\nPatched {patched} version rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
