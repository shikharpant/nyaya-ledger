"""Service-rate checkpoint closure regression gate.

Closes GAP-004 (terminal review: tests did not gate service checkpoint closure).
Materializes 11/2017-ct-rate against the service event ledger at each CBIC
checkpoint date and asserts the reconciliation holds at the achieved closure
baseline. If the event ledger, base notification, or materializer silently
regresses the service-rate reconstruction, this test fails loudly instead of
letting ``pytest tests/ -q`` stay green.

The gate mirrors the live library path used by
``scripts/regenerate_svc_reconciliation_report.py`` and the CLI
``main.py version rate-adjudicate``: ``materialize_schedule`` ->
``reconcile_schedule``. It intentionally runs against tracked fixtures so the
closure baseline is reproducible without ignored ``derived/`` inputs.
"""
import json
import sys

sys.path.insert(0, "src")

from legal_corpus.rate_schedule_materializer import materialize_schedule  # noqa: E402
from legal_corpus.rate_reconciliation import reconcile_schedule  # noqa: E402
from legal_corpus.service_checkpoint_inputs import (  # noqa: E402
    SERVICE_DATES,
    resolve_service_rate_inputs,
)

INPUTS = resolve_service_rate_inputs(prefer_fixture=True)
BASE = INPUTS.base
EVENTS = INPUTS.events
CHECKPOINT_DIR = INPUTS.checkpoint_dir

# Achieved closure baseline after the sno=3 / sno=7 / sno=17 fixes.
# total = checkpoint entry count for that date; matched must equal total.
EXPECTED = {
    "2019-04-01": 40,   # 40/40 (100%)
    "2024-10-24": 41,   # 41/41 (100%)
    "2025-03-31": 41,   # 41/41 (100%)
}

assert set(EXPECTED) == set(SERVICE_DATES)


def _reconcile(date: str) -> dict:
    snap = materialize_schedule(
        str(BASE), str(EVENTS), "11/2017-ct-rate",
        checkpoint_date=f"svc_{date}",
    )
    cp_path = CHECKPOINT_DIR / f"checkpoint_svc_{date}.json"
    checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
    return reconcile_schedule(snap, checkpoint, "11/2017-ct-rate")


def _summary_totals(report: dict) -> tuple[int, int]:
    s = report.get("summary", report)
    matched = s.get("total_matched")
    total = s.get("total_entries_checkpoint")
    if matched is None or total is None:
        # Fall back to counting across schedules.
        matched = 0
        total = 0
        for _sched, data in report.get("schedules", {}).items():
            total += len(data.get("checkpoint_entries", [])) or data.get(
                "total_checkpoint", 0
            )
            matched += data.get("matched", 0)
    return int(matched), int(total)


def test_service_checkpoint_closure_baseline_holds_for_all_dates():
    assert INPUTS.source == "tracked_fixture"
    failures = []
    for date, expected_total in EXPECTED.items():
        report = _reconcile(date)
        matched, total = _summary_totals(report)
        assert total == expected_total, (
            f"{date}: checkpoint total drift {total} != {expected_total}; "
            "expected entry count changed — update EXPECTED deliberately"
        )
        if matched != total:
            # Collect residual serials for a loud, actionable failure message.
            residuals = []
            for _sched, data in report.get("schedules", {}).items():
                for m in data.get("mismatches", []):
                    residuals.append(f"sno={m.get('sno')} issue={m.get('issue')}")
            failures.append(
                f"{date}: matched {matched}/{total} (expected {total}/{total}); "
                f"residuals: {residuals or 'see report'}"
            )
    assert not failures, (
        "Service checkpoint closure regressed. Materializer/event-ledger change "
        "broke reconciliation:\n  " + "\n  ".join(failures)
    )


def test_no_cross_serial_text_contamination_in_leasing_entry_sno17():
    """sno=17 (Leasing, Heading 9973) must not carry construction (sno=3) text.

    Regression guard for the bare-marker ``RATE_INSERT_WORDS`` routing defect
    that let sno=3 construction/percentage-invoicing clauses contaminate sno=17.
    """
    report = _reconcile("2024-10-24")
    contam_markers = [
        "percentage invoicing",
        "percentage completion",
        "Composite supply of works contract",
    ]
    for _sched, data in report.get("schedules", {}).items():
        for m in data.get("mismatches", []):
            if str(m.get("sno")).rstrip(".") != "17":
                continue
            mat_desc = (m.get("materialized_description") or "").lower()
            leaked = [mk for mk in contam_markers if mk.lower() in mat_desc]
            assert not leaked, (
                f"sno=17 contaminated with sno=3 construction text: {leaked}"
            )
