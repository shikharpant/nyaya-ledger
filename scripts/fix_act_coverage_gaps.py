#!/usr/bin/env python3
"""Fix the 7 remaining CGST Act materialization coverage gaps.

Category 1 (5 gaps): Add anchors to parent insert events so the
betting/casinos/gambling chain can be materialized.

Category 2 (1 gap): Fix old_text normalization for Section 16 substitution.

Category 3 (1 gap): Fix punctuation spacing for Section 17 substitution.

Category 4: Classify Finance Act, 2021 section 123 split events as out of
scope for the CGST Act lane. The source amends section 16 of the Integrated
GST Act, not section 16 of the Central GST Act.

Category 5: Reject the stale 2026 consolidated-corpus repair for CGST Act
section 16. It is a data repair event, not a legal amendment, and it drops the
Finance (No. 2) Act, 2024 insertion of sub-sections (5) and (6).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def _event_by_id(events: list[dict], event_id: str) -> dict | None:
    return next((event for event in events if event.get("event_id") == event_id), None)


def _apply_unique(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Expected unique Section 16 repair anchor: {old[:80]!r}")
    return text.replace(old, new, 1)


def _section16_post_2022_content(events: list[dict]) -> str:
    base_event = _event_by_id(events, "evt_cbic_2020_repair_16")
    if not base_event:
        raise ValueError("evt_cbic_2020_repair_16 missing; cannot build Section 16 repair")
    text = str((base_event.get("payload") or {}).get("content") or "")
    if not text:
        raise ValueError("evt_cbic_2020_repair_16 has no payload.content")

    text = _apply_unique(
        text,
        "to which such invoice or invoice relating to such debit note pertains",
        "to which such invoice or debit note pertains",
    )
    text = _apply_unique(
        text,
        (
            "(a) he is in possession of a tax invoice or debit note issued by a supplier "
            "registered under this Act, or such other tax paying documents as may be prescribed;"
        ),
        (
            "(a) he is in possession of a tax invoice or debit note issued by a supplier "
            "registered under this Act, or such other tax paying documents as may be prescribed; "
            "(aa) the details of the invoice or debit note referred to in clause (a) has been "
            "furnished by the supplier in the statement of outward supplies and such details "
            "have been communicated to the recipient of such invoice or debit note in the "
            "manner specified under section 37;"
        ),
    )
    text = _apply_unique(
        text,
        ";]35 (c) subject to the provisions of section 41",
        (
            ";]35 (ba) the details of input tax credit in respect of the said supply "
            "communicated to such registered person under section 38 has not been restricted; "
            "(c) subject to the provisions of section 41"
        ),
    )
    text = _apply_unique(
        text,
        "after the due date of furnishing of the return under section 39 for the month of September following",
        "after the thirtieth day of November following",
    )
    return text


def _section16_post_2023_content(events: list[dict]) -> str:
    text = _section16_post_2022_content(events)
    text = _apply_unique(
        text,
        "added to his output tax liability, along with interest thereon",
        "paid by him along with interest payable under section 50",
    )
    text = _apply_unique(
        text,
        "payment made by him of the amount towards the value of supply",
        "payment made by him to the supplier of the amount towards the value of supply",
    )
    return text


def main() -> int:
    events_path = Path("derived/version_history/cgst-act-2017/merged_amendment_events.jsonl")
    output_path = Path("derived/version_history/cgst-act-2017/fixed_amendment_events.jsonl")

    events: list[dict] = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    fixes_applied = 0

    for e in events:
        eid = e["event_id"]
        changed = False

        # CATEGORY 4: Finance Act, 2021 s.123 amends IGST Act s.16.
        # Keeping these split events materializable in the CGST Act lane leaks
        # zero-rated-supply/authorised-operations text into CGST input-tax
        # credit section 16.
        if eid in {
            "evt_finance_acts_59a15973566d9d27a",
            "evt_finance_acts_59a15973566d9d27b",
        }:
            e["status"] = "rejected"
            payload = e.setdefault("payload", {})
            payload["act_out_of_scope"] = True
            payload["out_of_scope_reason"] = (
                "Finance Act, 2021 section 123 amends section 16 of the "
                "Integrated Goods and Services Tax Act, 2017, not section 16 "
                "of the Central Goods and Services Tax Act, 2017."
            )
            review = e.setdefault("review", {})
            review["required"] = False
            review["review_reasons"] = ["cross_act_igst_section_16_not_cgst_section_16"]
            review["reviewed_by"] = "fix_act_coverage_gaps"
            review["decision_notes"] = payload["out_of_scope_reason"]
            validation = e.setdefault("validation", {})
            validation["materializable"] = False
            validation["target_resolved"] = False
            changed = True
            print(f"Fixed {eid}: marked IGST section 16 amendment out-of-scope for CGST Act")

        # CATEGORY 5: the consolidated corpus repair is stale for Section 16
        # and would roll back Finance (No. 2) Act, 2024 sub-sections (5) and
        # (6). Leave the source-backed Finance Act event as the live latest
        # version until the corpus XML itself is updated.
        elif eid == "evt_corpus_2026_repair_16":
            e["status"] = "rejected"
            payload = e.setdefault("payload", {})
            payload["stale_consolidated_corpus_repair"] = True
            payload["out_of_scope_reason"] = (
                "Consolidated corpus repair for CGST Act section 16 omits the "
                "Finance (No. 2) Act, 2024 insertion of sub-sections (5) and "
                "(6), so replaying it would roll back source-backed law."
            )
            review = e.setdefault("review", {})
            review["required"] = False
            review["review_reasons"] = ["stale_consolidated_corpus_repair"]
            review["reviewed_by"] = "fix_act_coverage_gaps"
            review["decision_notes"] = payload["out_of_scope_reason"]
            validation = e.setdefault("validation", {})
            validation["materializable"] = False
            changed = True
            print(f"Fixed {eid}: rejected stale Section 16 corpus repair")

        # CATEGORY 1: Fix parent insert events that lack anchors
        elif eid == "evt_cbic_acts_bf4990c87c920a4f":
            # Insert (102A) after clause (102)
            e.setdefault("target", {})["anchor_text"] = "(102)"
            e.setdefault("payload", {})["position"] = "after"
            e.setdefault("validation", {})["materializable"] = True
            e["validation"]["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: added anchor '(102)', position 'after'")

        elif eid == "evt_cbic_acts_071523f7578afd31":
            # Insert (80A) after clause (80)
            e.setdefault("target", {})["anchor_text"] = "(80)"
            e.setdefault("payload", {})["position"] = "after"
            e.setdefault("validation", {})["materializable"] = True
            e["validation"]["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: added anchor '(80)', position 'after'")

        elif eid == "evt_cbic_acts_292b1864ba3b9564":
            # Insert (117A) after clause (117)
            e.setdefault("target", {})["anchor_text"] = "(117)"
            e.setdefault("payload", {})["position"] = "after"
            e.setdefault("validation", {})["materializable"] = True
            e["validation"]["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: added anchor '(117)', position 'after'")

        # CATEGORY 2: Fix Section 16 substitution text normalization
        elif eid == "evt_finance_acts_59a15973566d9d27b":
            old_text = e.get("payload", {}).get("old_text", "")
            # Fix "Income tax Act" -> "Income- tax Act" to match materialized text
            fixed_old = old_text.replace("Income tax Act", "Income- tax Act")
            # Remove the "(43 of 1961)" reference which isn't in the materialized text
            fixed_old = re.sub(r"\s*\(43 of 1961\)\s*", " ", fixed_old)
            # Fix "shall not be allowed." -> "shall not be allowed." (normalize trailing)
            if fixed_old != old_text:
                e["payload"]["old_text"] = fixed_old
                changed = True
                print(f"Fixed {eid}: normalized old_text (Income tax Act -> Income- tax Act, removed act reference)")

        elif eid == "evt_finance_acts_4a911cae2581b0cf":
            e["status"] = "validated"
            e["operation"] = "SUBSTITUTE"
            e["payload"] = {
                "old_text": "to which such invoice or invoice relating to such debit note pertains",
                "new_text": "to which such invoice or debit note pertains",
                "materializer_repair": True,
                "materializer_repair_reason": "section16_debit_note_omission_precise_substitution",
            }
            review = e.setdefault("review", {})
            review["required"] = False
            review["review_reasons"] = []
            review["reviewed_by"] = "fix_act_coverage_gaps"
            review["decision_notes"] = "Finance Act, 2020 s.120 omits the phrase from CGST Act s.16(4)."
            validation = e.setdefault("validation", {})
            validation["materializable"] = True
            validation["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: converted Section 16(4) omission to precise substitution")

        elif eid == "evt_finance_acts_76007a839bf92634":
            e["status"] = "validated"
            e["target"]["anchor_text"] = (
                "(a) he is in possession of a tax invoice or debit note issued by a supplier "
                "registered under this Act, or such other tax paying documents as may be prescribed;"
            )
            payload = e.setdefault("payload", {})
            payload["insert_text"] = (
                " (aa) the details of the invoice or debit note referred to in clause (a) has "
                "been furnished by the supplier in the statement of outward supplies and such "
                "details have been communicated to the recipient of such invoice or debit note "
                "in the manner specified under section 37;"
            )
            payload["position"] = "after"
            payload["materializer_repair"] = True
            payload["materializer_repair_reason"] = "section16_clause_aa_anchor"
            review = e.setdefault("review", {})
            review["required"] = False
            review["review_reasons"] = []
            review["reviewed_by"] = "fix_act_coverage_gaps"
            validation = e.setdefault("validation", {})
            validation["materializable"] = True
            validation["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: anchored Section 16(2)(aa) insertion")

        elif eid == "evt_finance_acts_9bbfe697ff8369cf":
            e["status"] = "validated"
            e["operation"] = "SUBSTITUTE"
            e["payload"] = {
                "content": _section16_post_2022_content(events),
                "full_replacement": True,
                "materializer_repair": True,
                "materializer_repair_reason": "section16_finance_act_2022_compound_amendment",
            }
            review = e.setdefault("review", {})
            review["required"] = False
            review["review_reasons"] = []
            review["reviewed_by"] = "fix_act_coverage_gaps"
            validation = e.setdefault("validation", {})
            validation["materializable"] = True
            validation["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: materialized compound Finance Act 2022 Section 16 amendment")

        elif eid == "evt_finance_acts_348c5f085ed748cd":
            e["status"] = "validated"
            e["operation"] = "SUBSTITUTE"
            e["payload"] = {
                "content": _section16_post_2023_content(events),
                "full_replacement": True,
                "materializer_repair": True,
                "materializer_repair_reason": "section16_finance_act_2023_compound_amendment",
            }
            review = e.setdefault("review", {})
            review["required"] = False
            review["review_reasons"] = []
            review["reviewed_by"] = "fix_act_coverage_gaps"
            validation = e.setdefault("validation", {})
            validation["materializable"] = True
            validation["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: materialized compound Finance Act 2023 Section 16 amendment")

        # CATEGORY 3: Fix Section 17 punctuation spacing
        elif eid == "evt_finance_acts_d661f6aa9fe8aae3":
            old_text = e.get("payload", {}).get("old_text", "")
            # Fix "sections 74 , 129 and 130" -> "sections 74, 129 and 130"
            fixed_old = re.sub(r"\s+,\s*", ", ", old_text)
            if fixed_old != old_text:
                e["payload"]["old_text"] = fixed_old
                changed = True
                print(f"Fixed {eid}: normalized punctuation spacing in old_text")

        if changed:
            fixes_applied += 1

    with open(output_path, "w") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"\nTotal fixes: {fixes_applied}")
    print(f"Output: {output_path}")
    print(f"Events: {len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
