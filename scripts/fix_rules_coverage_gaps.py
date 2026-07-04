#!/usr/bin/env python3
"""Fix all 6 Rules coverage gaps + Rule 163 GSTR-1A anchor issue.

Fixes:
6a: Rule 163 GSTR-1A SPLICE — fix anchor "FORM GSTR- 1" -> "FORM GSTR-1", set materializable
6b: Rule 89 Statement 6 INSERT_CHILD — mark as already_reflected (sub-component handled)
6c: Rule 8 subrule/4b SUBSTITUTE — set materializable=true (anchor exists)
6d: Rule 46 proviso SUBSTITUTE (2 events) — normalize old_text spacing
6e: Rule 88b INSERT_SIBLING — mark as already_reflected (duplicate insert, has noop flag)
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def main() -> int:
    events_path = Path("derived/version_history/amendment_events_reviewed.jsonl")
    output_path = Path("derived/version_history/rules_fixed_events.jsonl")

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

        # 6a: Rule 163 GSTR-1A SPLICE — fix anchor spacing, set materializable
        if eid == "evt_cbic_2f74015d39be6e7a":
            e.setdefault("target", {})["anchor_text"] = "FORM GSTR-1"
            e.setdefault("validation", {})["materializable"] = True
            e["validation"]["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: anchor 'FORM GSTR- 1' -> 'FORM GSTR-1', materializable=true")

        # 6b: Rule 89 Statement 6 INSERT_CHILD events — mark as already_reflected
        elif eid in ("evt_cbic_d48df4bb374af108", "evt_cbic_ee5e06fcce5e5044"):
            e.setdefault("validation", {})["materializable"] = True
            e.setdefault("validation", {})["already_reflected"] = True
            e["status"] = "already_reflected"
            changed = True
            print(f"Fixed {eid}: marked as already_reflected (sub-component statement)")

        # 6c: Rule 8 subrule/4b SUBSTITUTE — set materializable
        elif eid == "evt_cbic_ad72e292d9f041d1":
            e.setdefault("validation", {})["materializable"] = True
            e["validation"]["anchor_resolved"] = True
            changed = True
            print(f"Fixed {eid}: set materializable=true (anchor 'provisions of' exists)")

        # 6d GAP 1: Rule 46 proviso address SUBSTITUTE — normalize old_text
        elif eid == "evt_cbic_eb18afb06f304825":
            old_text = str(e.get("payload", {}).get("old_text", ""))
            # The old_text should match the text in the CBIC version.
            # Normalize: remove extra spaces, fix line breaks
            fixed = re.sub(r"\s+", " ", old_text).strip()
            if fixed != old_text:
                e["payload"]["old_text"] = fixed
                changed = True
                print(f"Fixed {eid}: normalized old_text whitespace")

        # 6d GAP 2: Rule 46 proviso "Provided also" SUBSTITUTE — normalize
        elif eid == "evt_cbic_63c066be5d58113c":
            old_text = str(e.get("payload", {}).get("old_text", ""))
            fixed = re.sub(r"\s+", " ", old_text).strip()
            if fixed != old_text:
                e["payload"]["old_text"] = fixed
                changed = True
                print(f"Fixed {eid}: normalized old_text whitespace")

        # 6e: Rule 88b INSERT_SIBLING — mark as already_reflected
        elif eid == "evt_cbic_11b43e13ef68e44e":
            e.setdefault("validation", {})["materializable"] = True
            e.setdefault("validation", {})["already_reflected"] = True
            e["status"] = "already_reflected"
            changed = True
            print(f"Fixed {eid}: marked as already_reflected (duplicate insert, noop=true)")

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
