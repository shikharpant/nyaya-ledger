#!/usr/bin/env python3
"""Batch-generate citation-grade evidence bundles for all Tier A/B components."""

import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

RULES_TIERS = "derived/version_history/confidence_tiers.json"
ACT_TIERS = "derived/version_history/cgst-act-2017/confidence_tiers.json"

RULES_FROM_DATE = "2017-06-19"
ACT_FROM_DATE = "2017-04-12"
TO_DATE = "2026-06-17"

WORK_CONFIG = {
    "rules": {
        "work_marker": "/rules/",
        "work_id": "cgst-rules-2017",
        "tiers_path": RULES_TIERS,
        "version_dir": "derived/version_history/cgst-rules-2017",
        "events_path": "derived/version_history/amendment_events_reviewed.jsonl",
        "confidence_tiers_arg": RULES_TIERS,
        "from_date": RULES_FROM_DATE,
        "display_name": "CGST Rules 2017",
        "out_subdir": "rules",
    },
    "acts": {
        "work_marker": "/acts/",
        "work_id": "cgst-act-2017",
        "tiers_path": ACT_TIERS,
        "version_dir": "derived/version_history/cgst-act-2017",
        "events_path": "derived/version_history/cgst-act-2017/merged_amendment_events.jsonl",
        "confidence_tiers_arg": ACT_TIERS,
        "from_date": ACT_FROM_DATE,
        "display_name": "CGST Act 2017",
        "out_subdir": "acts",
    },
}

OUTPUT_ROOT = Path("derived/version_history/evidence_bundles")


def slug_for(component_id, work_id):
    marker = work_id + "/"
    if marker in component_id:
        suffix = component_id.split(marker, 1)[1]
    else:
        suffix = component_id.split("/")[-1]
    suffix = suffix.strip("/")
    if not suffix:
        suffix = "overview"
    return suffix.replace("/", "-")


def load_components(work_key, tiers):
    cfg = WORK_CONFIG[work_key]
    tiers_path = PROJECT_ROOT / cfg["tiers_path"]
    if not tiers_path.exists():
        print(f"WARN: tiers file not found: {tiers_path}", file=sys.stderr)
        return []
    data = json.loads(tiers_path.read_text(encoding="utf-8"))
    component_tiers = data.get("component_tiers", {})
    matched = []
    marker = cfg["work_marker"]
    for cid, tier in component_tiers.items():
        if marker not in cid:
            continue
        if tier not in tiers:
            continue
        matched.append((cid, tier))
    matched.sort(key=lambda x: (x[1], x[0]))
    return matched


def run_bundle(cid, cfg, json_out, html_out):
    json_cmd = [
        sys.executable, "main.py", "version", "evidence-bundle",
        "--component-id", cid,
        "--from-date", cfg["from_date"],
        "--to-date", TO_DATE,
        "--version-dir", cfg["version_dir"],
        "--events", cfg["events_path"],
        "--confidence-tiers", cfg["confidence_tiers_arg"],
        "--output", str(json_out),
    ]
    result = subprocess.run(json_cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode != 0 or not json_out.exists():
        return False, result.stderr.strip() or result.stdout.strip()
    html_cmd = [
        sys.executable, "main.py", "version", "evidence-bundle-html",
        "--bundle", str(json_out),
        "--output", str(html_out),
    ]
    result = subprocess.run(html_cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode != 0 or not html_out.exists():
        return False, result.stderr.strip() or result.stdout.strip()
    return True, ""


def render_index(entries, stats):
    rows = []
    for e in sorted(entries, key=lambda x: (x["work"], x["tier"], x["component_id"])):
        tier_class = f"tier-{e['tier']}"
        link = f'<a href="{html.escape(e["html_rel"])}">{html.escape(e["slug"])}</a>'
        rows.append(
            f'<tr><td>{html.escape(e["work"])}</td>'
            f'<td class="{tier_class}">{e["tier"]}</td>'
            f'<td><code>{html.escape(e["component_id"])}</code></td>'
            f'<td>{link}</td></tr>'
        )
    rows_html = "\n".join(rows)

    stat_items = ", ".join(
        f"Tier {t}: {count}" for t, count in sorted(stats["by_tier"].items())
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Evidence Bundles Index</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem auto; max-width: 1100px; color: #222; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: .3rem; }}
.summary {{ background: #f0f7ff; padding: .8rem 1rem; border-radius: 6px; margin-bottom: 1.5rem; }}
.summary h2 {{ margin: 0 0 .4rem 0; font-size: 1rem; }}
.tier-A {{ color: #1a7d1a; font-weight: bold; }}
.tier-B {{ color: #0050a0; font-weight: bold; }}
.tier-C {{ color: #b86000; font-weight: bold; }}
.tier-D {{ color: #c00; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
th, td {{ border: 1px solid #ddd; padding: .35rem .4rem; text-align: left; }}
th {{ background: #f0f0f0; }}
code {{ font-size: .8rem; }}
</style>
</head>
<body>
<h1>Evidence Bundles Index</h1>
<div class="summary">
<h2>Summary</h2>
<div><strong>Total bundles:</strong> {stats["total"]}</div>
<div><strong>By tier:</strong> {stat_items}</div>
<div><strong>Skipped:</strong> {stats["skipped"]}</div>
</div>
<table>
<tr><th>Work</th><th>Tier</th><th>Component ID</th><th>Bundle</th></tr>
{rows_html}
</table>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="A,B", help="Comma-separated tiers (default A,B)")
    parser.add_argument("--work", default="rules,acts", help="Comma-separated works (default rules,acts)")
    parser.add_argument("--dry-run", action="store_true", help="List what would be generated without calling the CLI")
    args = parser.parse_args()

    tiers = {t.strip().upper() for t in args.tier.split(",") if t.strip()}
    works = [w.strip().lower() for w in args.work.split(",") if w.strip()]
    invalid = [w for w in works if w not in WORK_CONFIG]
    if invalid:
        print(f"ERROR: unknown work(s): {invalid}. Valid: {list(WORK_CONFIG)}", file=sys.stderr)
        return 2

    plan = []
    for work_key in works:
        components = load_components(work_key, tiers)
        cfg = WORK_CONFIG[work_key]
        out_dir = OUTPUT_ROOT / cfg["out_subdir"]
        for cid, tier in components:
            slug = slug_for(cid, cfg["work_id"])
            json_out = out_dir / f"{slug}.json"
            html_out = out_dir / f"{slug}.html"
            plan.append((work_key, cfg, cid, tier, slug, json_out, html_out))

    total = len(plan)
    print(f"Planning {total} bundles across works={works} tiers={sorted(tiers)}")
    if not plan:
        print("Nothing to generate.")
        return 0

    if args.dry_run:
        for i, (work_key, cfg, cid, tier, slug, json_out, html_out) in enumerate(plan, 1):
            print(f"[{i}/{total}] [{cfg['display_name']}/Tier {tier}] {cid} -> {json_out.name} + {html_out.name}")
        by_tier = {}
        for _, _, _, tier, _, _, _ in plan:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        print(f"\nDRY RUN: would generate {total} bundles. By tier: {by_tier}")
        return 0

    success = 0
    skipped = 0
    entries = []
    for i, (work_key, cfg, cid, tier, slug, json_out, html_out) in enumerate(plan, 1):
        print(f"[{i}/{total}] Generating {slug}...")
        json_out.parent.mkdir(parents=True, exist_ok=True)
        ok, err = run_bundle(cid, cfg, json_out, html_out)
        if ok:
            success += 1
            entries.append({
                "work": cfg["display_name"],
                "tier": tier,
                "component_id": cid,
                "slug": slug,
                "html_rel": f"{cfg['out_subdir']}/{html_out.name}",
            })
        else:
            skipped += 1
            print(f"  SKIP: {cid} ({err[:200]})", file=sys.stderr)

    index_path = PROJECT_ROOT / OUTPUT_ROOT / "index.html"
    by_tier = {}
    for e in entries:
        by_tier[e["tier"]] = by_tier.get(e["tier"], 0) + 1
    stats = {"total": success, "skipped": skipped, "by_tier": by_tier}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(render_index(entries, stats), encoding="utf-8")

    print(f"\nGenerated {success}/{total} bundles ({skipped} skipped)")
    print(f"Index: {index_path}")
    return 0 if success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
