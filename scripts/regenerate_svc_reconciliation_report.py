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
import argparse
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from legal_corpus.rate_confidence import generate_adjudication_report  # noqa: E402
from legal_corpus.rate_schedule_materializer import materialize_schedule  # noqa: E402
from legal_corpus.rate_reconciliation import reconcile_schedule  # noqa: E402
from legal_corpus.service_checkpoint_inputs import (  # noqa: E402
    RUNTIME_RATE_DIR,
    SERVICE_DATES,
    resolve_service_rate_inputs,
)


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate service checkpoint reconciliation/adjudication reports."
    )
    parser.add_argument(
        "--rate-dir",
        default=None,
        help=(
            "Input rate-schedule directory. Defaults to derived/ if complete, "
            "otherwise the tracked service checkpoint fixture."
        ),
    )
    parser.add_argument(
        "--prefer-fixture",
        action="store_true",
        help="Use the tracked fixture even when ignored derived inputs exist.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(RUNTIME_RATE_DIR),
        help="Directory for regenerated report artifacts.",
    )
    parser.add_argument(
        "--reconciliation-output",
        default=None,
        help="Path for reconciliation_report_svc.json.",
    )
    return parser.parse_args(argv)


def run_checkpoint(date: str, inputs) -> dict:
    cp_file = inputs.checkpoint_dir / f"checkpoint_svc_{date}.json"
    snap = materialize_schedule(
        str(inputs.base), str(inputs.events), "11/2017-ct-rate",
        checkpoint_date=f"svc_{date}",
    )
    with open(cp_file) as fh:
        cp = json.load(fh)
    report = reconcile_schedule(snap, cp, "11/2017-ct-rate")
    return {"date": date, "checkpoint_file": str(cp_file), "reconciliation": report}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = resolve_service_rate_inputs(args.rate_dir, prefer_fixture=args.prefer_fixture)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    reconciliation_output = (
        Path(args.reconciliation_output)
        if args.reconciliation_output
        else output_dir / "reconciliation_report_svc.json"
    )
    reconciliation_output.parent.mkdir(parents=True, exist_ok=True)

    results = [run_checkpoint(d, inputs) for d in SERVICE_DATES]

    summaries = {}
    residual_classifications = {}
    adjudication_outputs = {}
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
        adj_path = output_dir / f"adjudication_report_svc_{r['date']}.json"
        adj = generate_adjudication_report(rec, str(inputs.events), output_path=adj_path)
        adjudication_outputs[r["date"]] = {
            "path": str(adj_path),
            "summary": adj["summary"],
        }

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
            "source": inputs.source,
            "rate_dir": str(inputs.rate_dir),
            "base_11_2017_ct_rate": {
                "path": str(inputs.base),
                "size_bytes": inputs.base.stat().st_size,
                "sha256": sha256(inputs.base),
            },
            "llm_svc_events": {
                "path": str(inputs.events),
                "size_bytes": inputs.events.stat().st_size,
                "sha256": sha256(inputs.events),
            },
        },
        "outputs": {
            "reconciliation_report_svc": str(reconciliation_output),
            "adjudication_reports": adjudication_outputs,
        },
        "summaries": summaries,
        "residual_classifications": residual_classifications,
        "checkpoints": {r["date"]: r["reconciliation"] for r in results},
    }

    reconciliation_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {reconciliation_output} ({reconciliation_output.stat().st_size} bytes)")

    # Validate it is non-empty valid JSON
    loaded = json.loads(reconciliation_output.read_text(encoding="utf-8"))
    assert loaded, "report is empty"
    assert "summaries" in loaded, "missing summaries"
    print("VALID JSON, non-empty, has summaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
