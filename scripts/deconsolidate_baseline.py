#!/usr/bin/env python3
"""Deconsolidate CGST Act baseline by stripping amendment annotations.

The India Code / CBIC TaxInformation base law text is a consolidated version
with amendment annotations marking inserted/substituted/omitted text. This
script strips those annotations to recover the original 2017 enactment text.

Annotation patterns removed:
- "N [...] " regions where N is a sequential amendment number
  (e.g., "1 [(aa) ...]" = text inserted by Finance Act)
- "N [***]" regions (omitted text)
- Footnote annotations at section end: "1. Inserted (w.e.f. ..."

The result is a clean baseline_components.jsonl suitable for event-sourced
version-history materialization.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def strip_amendment_annotations(text: str) -> tuple[str, int]:
    """Remove amendment annotations from consolidated legal text.

    Returns (clean_text, change_count).
    """
    changes = 0

    # Find and remove "N [...] " amendment regions
    regions: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        m = re.match(r"(\d+)\s*\[", text[i:])
        if m:
            start = i
            bracket_start = i + m.end() - 1
            depth = 1
            j = bracket_start + 1
            while j < len(text) and depth > 0:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                j += 1
            if depth == 0:
                regions.append((start, j))
                i = j
                changes += 1
            else:
                i += 1
        else:
            i += 1

    for start, end in reversed(regions):
        text = text[:start] + text[end:]

    # Remove footnote annotations at the end: "1. Inserted (w.e.f. ..."
    footnote_match = re.search(
        r"\s*\d+\.\s+(Inserted|Subs\.|Omitted|Substituted|Added|Amended)\s*\(",
        text,
    )
    if footnote_match:
        text = text[: footnote_match.start()]
        changes += 1

    # Clean up whitespace artifacts left by removals
    text = re.sub(r"  +", " ", text)
    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"\s+;", ";", text)
    text = text.strip()

    return text, changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-dir",
        default="derived/version_history/baselines/cgst-act-2017/2017-04-12",
    )
    parser.add_argument(
        "--output-dir",
        default="derived/version_history/baselines/cgst-act-2017/2017-07-01-deconsolidated",
    )
    parser.add_argument(
        "--component-id",
        default=None,
        help="Only process this component (for testing)",
    )
    args = parser.parse_args()

    baseline_dir = Path(args.baseline_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    components_path = baseline_dir / "baseline_components.jsonl"
    if not components_path.exists():
        print(f"Error: {components_path} not found")
        return 1

    total_changes = 0
    total_components = 0
    modified_components = 0

    out_path = output_dir / "baseline_components.jsonl"
    with open(components_path) as f_in, open(out_path, "w") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total_components += 1

            cid = row.get("component_id", "")
            if args.component_id and cid != args.component_id:
                f_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                continue

            text = row.get("text", "")
            clean_text, changes = strip_amendment_annotations(text)

            if changes > 0:
                row = dict(row)
                row["text"] = clean_text
                row["deconsolidation_changes"] = changes
                total_changes += changes
                modified_components += 1

                if args.component_id:
                    print(f"  {cid}: {changes} annotations removed")
                    print(f"    before ({len(text)} chars): {text[:120]}")
                    print(f"    after  ({len(clean_text)} chars): {clean_text[:120]}")

            f_out.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\nProcessed {total_components} components")
    print(f"Modified {modified_components} components")
    print(f"Total annotations removed: {total_changes}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
