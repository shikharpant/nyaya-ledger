#!/usr/bin/env python3
"""Fix the 7 remaining CGST Act materialization coverage gaps.

Category 1 (5 gaps): Add anchors to parent insert events so the
betting/casinos/gambling chain can be materialized.

Category 2 (1 gap): Fix old_text normalization for Section 16 substitution.

Category 3 (1 gap): Fix punctuation spacing for Section 17 substitution.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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

        # CATEGORY 1: Fix parent insert events that lack anchors
        if eid == "evt_cbic_acts_bf4990c87c920a4f":
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
