#!/usr/bin/env python3
"""Create stub XML entries for missing GST forms referenced in the corpus.

These stubs resolve unresolved cross-references. Content will be filled in
later when the actual form PDFs are scraped from the CBIC portal.

Usage:
    python3 scripts/create_form_stubs.py --dry-run
    python3 scripts/create_form_stubs.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus" / "in" / "union" / "forms"
UNRESOLVED_PATH = REPO_ROOT / "derived" / "references" / "unresolved_references.json"

MISSING_FORMS = [
    "gst-spl-02", "gst-rfd-06", "gst-pmt-06", "gst-rfd-05", "gst-rfd-03",
    "gst-pmt-03", "gst-drc-01c", "gst-spl-01", "gst-spl-07", "gst-rfd-01a",
    "gst-inv-1", "gst-pmt-01", "gst-rfd-04", "gst-mis-1", "gst-apl-05",
    "gst-spl-03", "gst-spl-05", "gst-apl-02", "gst-spl-04", "gst-spl-06",
    "gst-apl-02a", "gst-inv-01", "gst-pmt-09", "gst-rfd-01w", "gst-drc-11",
    "gst-drc-12", "gst-enr-02", "gst-apl-06", "gst-apl-07", "gst-pmt-04",
    "gst-rfd-02", "gst-drc-14", "gst-ewb-06", "gst-rfd-08", "gst-drc-10",
    "gst-pmt-07", "gst-drc-03a", "gst-mis-2", "gst-pmt-05", "gst-apl-08",
    "gst-pmt-2", "gst-gstr-9c", "gst-drc-22a", "gst-pmt-03a", "gst-rfd-09",
    "gst-drc-01d", "gst-drc-01b", "gst-spl-08", "gst-srm-1", "gst-apl-04a",
    "gst-page-21", "gst-mis-3", "gst-pmt-02", "gst-asmt-04", "gst-asmt-07",
    "gst-asmt-12", "gst-adt-02", "gst-adt-04", "gst-drc-09", "gst-drc-16",
    "gst-drc-17", "gst-drc-18", "gst-drc-19", "gst-drc-20", "gst-drc-21",
    "gst-drc-24", "gst-drc-25",
]


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _form_display_name(slug: str) -> str:
    parts = slug.replace("gst-", "GST ").upper().replace("-", " ")
    return f"FORM {parts}"


def _render_stub_xml(slug: str) -> str:
    cid = f"/in/union/forms/{slug}"
    display = _form_display_name(slug)
    source_text = f"{display}\n\n[Content to be sourced from CBIC portal]"
    source_hash = _sha256(source_text)
    eid = slug.replace("-", "_")

    return f"""<?xml version='1.0' encoding='utf-8'?>
<akomaNtoso>
  <doc name="form">
    <meta>
      <identification source="#git-for-law">
        <FRBRWork>
          <FRBRthis value="{_esc(cid)}"/>
          <FRBRuri value="{_esc(cid)}"/>
          <FRBRdate date="" name="effective"/>
          <FRBRauthor href="/in/authority/cbic"/>
          <FRBRcountry value="in"/>
        </FRBRWork>
      </identification>
      <proprietary source="#git-for-law">
        <property name="canonical_id" value="{_esc(cid)}"/>
        <property name="document_type" value="form"/>
        <property name="jurisdiction" value="IN-UNION"/>
        <property name="language" value="eng"/>
        <property name="parser_version" value="stub-v1"/>
        <property name="review_status" value="stub"/>
        <property name="source_sha256" value="{source_hash}"/>
        <property name="source_type" value="stub"/>
        <property name="title" value="{_esc(display)}"/>
        <property name="source_url" value="https://taxinformation.cbic.gov.in/"/>
        <property name="publication_date" value=""/>
        <property name="effective_from" value=""/>
        <property name="issuing_authority" value="/in/authority/cbic"/>
      </proprietary>
    </meta>
    <mainBody>
      <paragraph eId="{eid}__para_1" sourceStart="0" sourceEnd="{len(source_text)}" sourceHash="{_sha256(source_text)}" sourceNodeType="form" sourceConfidence="1.0">
        <num>{display.replace("FORM ", "")}</num>
        <content>
          <p>{_esc(source_text)}</p>
        </content>
      </paragraph>
    </mainBody>
  </doc>
</akomaNtoso>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Overwrite existing stubs")
    args = parser.parse_args()

    created = 0
    skipped = 0
    for slug in MISSING_FORMS:
        form_dir = CORPUS_DIR / slug
        form_xml = form_dir / "form.xml"

        if form_xml.exists() and not args.force:
            skipped += 1
            continue

        if args.dry_run:
            print(f"would create: {slug}")
            created += 1
            continue

        form_dir.mkdir(parents=True, exist_ok=True)
        form_xml.write_text(_render_stub_xml(slug), encoding="utf-8")
        print(f"created: {slug}")
        created += 1

    print(f"\nCreated: {created}, Skipped: {skipped}, Total: {len(MISSING_FORMS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
