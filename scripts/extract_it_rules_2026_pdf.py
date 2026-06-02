#!/usr/bin/env python3
"""Extract Income-tax Rules, 2026 rule blocks from the notified PDF."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "data" / "Law" / "base_laws" / "income_tax_rules_2026.pdf"
TEXT_PATH = ROOT / "data" / "Law" / "base_laws" / "income_tax_rules_2026.txt"
JSON_PATH = ROOT / "data" / "Law" / "base_laws" / "income_tax_rules_2026.json"

OFFICIAL_SOURCE = "https://www.incometaxindia.gov.in/w/notification-no.-22/2026-f.-no.-370142/41/2025-tpl-/-g.s.r.-198-e-"
OFFICIAL_PDF = "https://www.incometaxindia.gov.in/documents/81799/11848482/En-Notified-IT-Rules-2026-20-03-2026.pdf/a332bf2a-da14-8b94-dde2-5a2ea1428318?t=1773990110473"
MIRROR_PDF = "https://www.registrationwala.com/storage/app/public/notifications/April2026/wKGGYwH0449ykPXnK5Si.pdf"

APPENDIX_RE = re.compile(r"^APPENDIX\s+I\s*$", re.IGNORECASE)
RULE_START_RE = re.compile(r"^(?P<number>\d+[A-Z]*)\.\s+(?P<rest>.+)")
GAZETTE_HEADER_RE = re.compile(
    r"^(?:\[[^\]]+\]|THE GAZETTE OF INDIA|भारत|[\d०-९]+$|PART II|SEC\. 3\(i\))",
    re.IGNORECASE,
)


def run_pdftotext() -> str:
    result = subprocess.run(
        ["pdftotext", str(PDF_PATH), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def clean_lines(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if GAZETTE_HEADER_RE.search(line):
            continue
        lines.append(line)
    return lines


def split_heading(rest: str) -> tuple[str, str]:
    match = re.search(r"[.–—-]\s*", rest)
    if not match:
        return rest.strip(), ""
    return rest[: match.start()].strip(), rest[match.end() :].strip()


def extract_rules(lines: list[str]) -> tuple[list[dict], int | None]:
    appendix_index = next((index for index, line in enumerate(lines) if APPENDIX_RE.match(line)), None)
    rule_lines = lines[:appendix_index] if appendix_index is not None else lines

    starts = []
    expected_rule = 1
    for index, line in enumerate(rule_lines):
        match = RULE_START_RE.match(line)
        if not match:
            continue
        number = match.group("number")
        if number.isdigit() and int(number) == expected_rule:
            starts.append((index, number, match.group("rest")))
            expected_rule += 1
        if expected_rule > 333:
            break

    rules = []
    for position, (start_index, number, rest) in enumerate(starts):
        end_index = starts[position + 1][0] if position + 1 < len(starts) else len(rule_lines)
        block_lines = rule_lines[start_index:end_index]
        heading, first_body = split_heading(rest)
        body_lines = []
        if first_body:
            body_lines.append(first_body)
        body_lines.extend(block_lines[1:])
        full_text = " ".join(block_lines).strip()
        rules.append(
            {
                "rule_number": number,
                "title": f"Rule {number}",
                "description": heading,
                "full_text": full_text,
                "body": " ".join(body_lines).strip(),
            }
        )

    return rules, appendix_index


def main() -> None:
    text = run_pdftotext()
    TEXT_PATH.write_text(text, encoding="utf-8")

    lines = clean_lines(text)
    rules, appendix_index = extract_rules(lines)

    payload = {
        "source": OFFICIAL_SOURCE,
        "official_pdf": OFFICIAL_PDF,
        "downloaded_from": MIRROR_PDF,
        "act": "Income-tax Rules, 2026",
        "notification": "Notification No. 22/2026 [F. No. 370142/41/2025-TPL] / G.S.R. 198(E)",
        "notification_date": "2026-03-20",
        "effective_date": "2026-04-01",
        "pdf_path": str(PDF_PATH.relative_to(ROOT)),
        "text_path": str(TEXT_PATH.relative_to(ROOT)),
        "total_rules": len(rules),
        "appendix_boundary_line": appendix_index,
        "appendices_in_pdf": True,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "rules": rules,
    }
    JSON_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(rules)} rules -> {JSON_PATH}")


if __name__ == "__main__":
    main()
