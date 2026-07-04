#!/usr/bin/env python3
"""Fix baseline rows in CGST Act node_versions.jsonl with deconsolidated text.

Reads the deconsolidated baseline_components.jsonl and replaces baseline
(created_by_event_id=None) text in node_versions.jsonl with the clean text.
Also recomputes text_sha256 for modified rows.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--node-versions",
        default="derived/version_history/cgst-act-2017/node_versions.jsonl",
    )
    parser.add_argument(
        "--deconsolidated-baseline",
        default="derived/version_history/baselines/cgst-act-2017/2017-07-01-deconsolidated/baseline_components.jsonl",
    )
    parser.add_argument("--output", default=None, help="Output path (default: overwrite)")
    args = parser.parse_args()

    # Load deconsolidated baseline
    deconsolidated: dict[str, str] = {}
    baseline_path = Path(args.deconsolidated_baseline)
    with open(baseline_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            changes = row.get("deconsolidation_changes", 0)
            if changes > 0:
                deconsolidated[row["component_id"]] = row["text"]

    print(f"Loaded {len(deconsolidated)} deconsolidated components")

    # Fix node_versions.jsonl
    node_versions_path = Path(args.node_versions)
    output_path = Path(args.output) if args.output else node_versions_path

    fixed = 0
    total = 0
    rows: list[str] = []

    with open(node_versions_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1

            # Only fix baseline rows (no created_by_event_id)
            if not row.get("created_by_event_id"):
                cid = row.get("component_id", "")
                if cid in deconsolidated:
                    old_text = row.get("text", "")
                    new_text = deconsolidated[cid]
                    if old_text != new_text:
                        row["text"] = new_text
                        row["text_sha256"] = sha256_text(new_text)
                        row["source_basis"]["deconsolidated"] = True
                        fixed += 1

            rows.append(json.dumps(row, ensure_ascii=False))

    with open(output_path, "w") as f:
        for row in rows:
            f.write(row + "\n")

    print(f"Processed {total} rows")
    print(f"Fixed {fixed} baseline rows with deconsolidated text")
    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
