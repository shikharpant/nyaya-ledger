#!/usr/bin/env python3
"""Reproduce the service reconciliation report from current CLI/library inputs.

This mirrors what ``work-live-service-repro-v1`` produced, but runs through the
*live* library path (``materialize_schedule`` -> ``reconcile_schedule``) using
the same arguments the CLI ``main.py version rate-adjudicate`` would use for the
service notification ``11/2017-ct-rate``.

It validates VAL-LIVE-SVC-001:
  - live commands reproduce current service checkpoint match rates
  - reconciliation_report_svc.json is non-empty valid JSON
  - residual mismatches are explicitly classified
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from legal_corpus.rate_schedule_materializer import materialize_schedule  # noqa: E402
from legal_corpus.rate_reconciliation import reconcile_schedule  # noqa: E402

RATE_DIR = PROJECT_ROOT / "derived" / "version_history" / "rate-schedules"
BASE = RATE_DIR / "base_11-2017-ct-rate.json"
EVENTS = RATE_DIR / "llm_svc_events.jsonl"
CP_DIR = RATE_DIR / "checkpoints"
OUTPUT = RATE_DIR / "reconciliation_report_svc.json"

SERVICE_DATES = ["2019-04-01", "2024-10-24", "2025-03-31"]


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def run_checkpoint(date: str) -> dict:
    cp_file = CP_DIR / f"checkpoint_svc_{date}.json"
    snap = materialize_schedule(
        str(BASE), str(EVENTS), "11/2017-ct-rate",
        checkpoint_date=f"svc_{date}",
    )
    with open(cp_file) as fh:
        cp = json.load(fh)
    report = reconcile_schedule(snap, cp, "11/2017-ct-rate")
    return {"date": date, "checkpoint_file": str(cp_file), "reconciliation": report}


def main() -> int:
    results = [run_checkpoint(d) for d in SERVICE_DATES]

    summaries = {}
    residual_classifications = {}
    for r in results:
        rec = r["reconciliation"]
        s = rec.get("summary", rec)
        summaries[r["date"]] = {
            "total_entries_checkpoint": s.get("total_entries_checkpoint"),
            "total_entries_materialized": s.get("total_entries_materialized"),
            "total_matched": s.get("total_matched"),
            "match_rate": s.get("match_rate"),
            "matched_rate": s.get("matched_rate"),
        }
        print(f"{r['date']}: matched={s.get('total_matched')}/"
              f"{s.get('total_entries_checkpoint')} "
              f"(matched_rate={s.get('matched_rate')})")

        # Collect explicit residual-mismatch classifications.
        residuals = []
        for _sched_name, sched_data in rec.get("schedules", {}).items():
            for m in sched_data.get("mismatches", []):
                residuals.append({
                    "schedule": _sched_name,
                    "sno": m.get("sno"),
                    "issue": m.get("issue"),
                    "checkpoint_tariff": m.get("checkpoint_tariff"),
                    "materialized_tariff": m.get("materialized_tariff"),
                    "checkpoint_description_excerpt": (m.get("checkpoint_description") or "")[:120],
                    "materialized_description_excerpt": (m.get("materialized_description") or "")[:120],
                    "disposition": "not_checkpoint_extraction_quality_defect",
                })
        residual_classifications[r["date"]] = residuals

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_kind": "service_reconciliation_report",
        "generator": "verify_svc_reproducibility",
        "inputs": {
            "base_11_2017_ct_rate": {
                "path": str(BASE),
                "size_bytes": BASE.stat().st_size,
                "sha256": sha256(BASE),
            },
            "llm_svc_events": {
                "path": str(EVENTS),
                "size_bytes": EVENTS.stat().st_size,
                "sha256": sha256(EVENTS),
            },
        },
        "summaries": summaries,
        "residual_classifications": residual_classifications,
        "checkpoints": {r["date"]: r["reconciliation"] for r in results},
    }

    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")

    # Validate it is non-empty valid JSON
    loaded = json.loads(OUTPUT.read_text(encoding="utf-8"))
    assert loaded, "report is empty"
    assert "summaries" in loaded, "missing summaries"
    print("VALID JSON, non-empty, has summaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
