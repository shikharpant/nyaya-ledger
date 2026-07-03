"""Rate-schedule horizontal validator — cross-schedule HSN consistency checks.

Runs *within* a single materialized snapshot (the output of
``rate_schedule_materializer.materialize_schedule``) rather than against an
external checkpoint. It verifies the internal coherence of the materialized
rate schedules:

* no duplicate S.No. within any schedule,
* S.No. ordering is sequential (no gaps, no out-of-order entries),
* an HSN does not simultaneously appear in two rate schedules at the same
  point in time (with an explicit allowance for chapter-level entries that
  carry an ``[other than ...]`` exclusion clause), and
* a tariff item is not simultaneously taxable in the rate schedule
  (``1/2017``) and exempt in the exemption schedule (``2/2017``).

The exemption cross-check is only performed when an ``exempt_snapshot`` is
supplied — both snapshots must be materialized at the same checkpoint date for
the comparison to be meaningful.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ── normalization ────────────────────────────────────────────────────────────

# Phrases that signal an exclusion clause in a chapter-level entry, e.g.
# "All goods of heading 6302 [other than 6302 10 00]". When a 2-digit chapter
# code overlaps a longer sub-heading code in another schedule, the presence of
# such a clause means the overlap is intentional and expected.
_EXCLUSION_PATTERNS = (
    re.compile(r"\bother\s+than\b", re.IGNORECASE),
    re.compile(r"\bexcluding\b", re.IGNORECASE),
    re.compile(r"\[.*?\]"),  # bracketed carve-outs such as "[other than ...]"
)

# Cells may separate several HSN codes with commas, semicolons or "or".
_CODE_SPLIT_RE = re.compile(r"[,;]|\bor\b")


def _normalize_tariff(text: str) -> str:
    """Collapse all whitespace from a tariff cell: '0101 21 00' → '01012100'."""
    return re.sub(r"\s+", "", str(text or "")).strip()


def _normalize_sno(text: str) -> str:
    """Normalize a serial-number key: strip, drop trailing periods, uppercase."""
    return re.sub(r"\s+", "", str(text or "")).rstrip(".").strip().upper()


def _split_tariff_codes(tariff_item: str) -> list[str]:
    """Split a tariff cell into individual non-empty HSN codes.

    ``"01012100, 010129"`` → ``["01012100", "010129"]``. Pure-text cells such
    as ``"any chapter"`` are dropped — they carry no scannable HSN code.
    """
    raw = _normalize_tariff(tariff_item)
    if not raw:
        return []
    codes: list[str] = []
    for part in _CODE_SPLIT_RE.split(raw):
        part = part.strip().strip(".")
        # A real HSN / tariff item is at least 2 digits and starts digitally.
        if part and re.match(r"^\d{2,}", part):
            codes.append(part)
    return codes


def _has_exclusion(description: str) -> bool:
    """Return True if a description carries an exclusion / carve-out clause."""
    desc = str(description or "")
    return any(pat.search(desc) for pat in _EXCLUSION_PATTERNS)


def _leading_int(sno: str) -> int | None:
    """Extract the leading integer from a S.No. ('3B' → 3, 'iv' → None)."""
    match = re.match(r"\s*(\d+)", str(sno or ""))
    return int(match.group(1)) if match else None


# ── per-schedule checks ──────────────────────────────────────────────────────


def _check_duplicate_sno(
    schedule_id: str,
    entries: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> int:
    """Flag repeated S.No. values within a single schedule. Returns dup count."""
    seen: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for entry in entries:
        if entry.get("is_omitted"):
            continue
        key = _normalize_sno(entry.get("sno", ""))
        if not key:
            continue
        if key in seen:
            duplicates += 1
            errors.append({
                "type": "duplicate_sno",
                "schedule": schedule_id,
                "sno": entry.get("sno", ""),
                "detail": (
                    f"S.No. {entry.get('sno', '')} repeats earlier entry "
                    f"{seen[key].get('sno', '')} in schedule {schedule_id}"
                ),
            })
        else:
            seen[key] = entry
    return duplicates


# A S.No. whose leading integer exceeds this multiple of the schedule length
# is treated as a data-quality anomaly (typically a tariff item such as
# ``22029990`` that leaked into the S.No. column) rather than a genuine serial
# number. Such values are reported once via ``sno_value_anomaly`` and excluded
# from the gap / ordering analysis so they cannot flood the report.
_SNO_ANOMALY_FACTOR = 3
_SNO_ANOMALY_FLOOR = 100
# Hard cap on individually-reported gaps per schedule; beyond this a single
# summary line is emitted instead, to keep reports readable for very large or
# heavily-mangled schedules.
_GAP_REPORT_LIMIT = 100


def _check_sno_ordering(
    schedule_id: str,
    entries: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    """Verify S.No. leading integers are sequential: 1, 2, 3, ... in order.

    Inserted entries that share a leading integer (``2A``, ``2B``) are allowed
    and do not break the sequence. Omitted entries still occupy their numeric
    slot and are therefore not reported as gaps. Three distinct issues are
    surfaced:

    * ``sno_value_anomaly`` — leading integer is implausibly large (almost
      always a tariff item misparsed into the S.No. column);
    * ``sno_out_of_order`` — an entry's leading integer is smaller than the
      preceding entry's (reported at the point of disorder, not as a cascade);
    * ``sno_gap`` — an integer in the expected range is unaccounted for.
    """
    cap = max(_SNO_ANOMALY_FLOOR, len(entries) * _SNO_ANOMALY_FACTOR)

    parsed: list[tuple[int, str, bool]] = []  # (lead, sno, is_omitted)
    anomalies: set[tuple[int, str]] = set()
    for entry in entries:
        sno = str(entry.get("sno", ""))
        num = _leading_int(sno)
        if num is None:
            continue
        if num > cap:
            anomalies.add((num, sno))
            continue
        parsed.append((num, sno, bool(entry.get("is_omitted"))))

    for num, sno in sorted(anomalies):
        errors.append({
            "type": "sno_value_anomaly",
            "schedule": schedule_id,
            "sno": sno,
            "value": num,
            "detail": (
                f"S.No. {sno} (={num}) in schedule {schedule_id} exceeds the "
                f"expected range (cap {cap}); likely a tariff item misparsed "
                f"into the S.No. column"
            ),
        })

    if not parsed:
        return

    # Out-of-order: compare each entry to its predecessor (not a running max)
    # so a single restart point produces one error instead of a cascade.
    prev = 0
    for num, sno, omitted in parsed:
        if not omitted and num < prev:
            errors.append({
                "type": "sno_out_of_order",
                "schedule": schedule_id,
                "sno": sno,
                "detail": (
                    f"S.No. {sno} (={num}) appears after {prev} in schedule "
                    f"{schedule_id}"
                ),
            })
        prev = num

    # Gaps: every integer from 1 .. max should be present. Omitted entries are
    # part of ``parsed`` and therefore count as occupied slots.
    present = {num for num, _, _ in parsed}
    maximum = max(present)
    missing = sorted(set(range(1, maximum + 1)) - present)
    if len(missing) <= _GAP_REPORT_LIMIT:
        for num in missing:
            errors.append({
                "type": "sno_gap",
                "schedule": schedule_id,
                "sno": str(num),
                "detail": f"S.No. {num} is missing from the sequence in schedule {schedule_id}",
            })
    elif missing:
        errors.append({
            "type": "sno_gap",
            "schedule": schedule_id,
            "sno": f"{missing[0]}..{missing[-1]}",
            "count": len(missing),
            "detail": (
                f"{len(missing)} S.No. values missing from schedule "
                f"{schedule_id} ({missing[0]}..{missing[-1]})"
            ),
        })


# ── cross-schedule HSN overlap ───────────────────────────────────────────────


def _build_hsn_map(
    snapshot: dict,
    label: str | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Map each HSN code → {schedule_id: [entry, ...]}.

    *label* is attached to each entry record (as ``"source"``) so callers can
    distinguish rate-schedule entries from exemption entries when both maps are
    merged. Omitted entries are skipped.
    """
    hsn_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for schedule_id, table in snapshot.get("schedules", {}).items():
        if not isinstance(table, dict):
            continue
        for entry in table.get("entries", []):
            if entry.get("is_omitted"):
                continue
            for code in _split_tariff_codes(entry.get("tariff_item", "")):
                bucket = hsn_map.setdefault(code, {})
                record = dict(entry)
                record["schedule"] = schedule_id
                if label:
                    record["source"] = label
                bucket.setdefault(schedule_id, []).append(record)
    return hsn_map


def _check_cross_schedule_overlap(
    hsn_map: dict[str, dict[str, list[dict[str, Any]]]],
    warnings: list[dict[str, Any]],
) -> int:
    """Flag HSN codes that appear in more than one rate schedule.

    Returns the number of cross-schedule HSN codes. Chapter-level (2-digit)
    codes are allowed to overlap a longer sub-heading code in another schedule
    when the chapter entry carries an exclusion clause; 4+ digit codes are
    always flagged.
    """
    cross_count = 0
    for hsn, by_schedule in sorted(hsn_map.items()):
        if len(by_schedule) < 2:
            continue
        schedules = sorted(by_schedule)
        cross_count += 1

        # Gather whether any involved entry carries an exclusion clause and the
        # lengths of the codes involved (always equal to len(hsn) here, since
        # the map is keyed by the exact code string).
        has_exclusion = any(
            _has_exclusion(rec.get("description", ""))
            for recs in by_schedule.values()
            for rec in recs
        )

        if len(hsn) <= 2:
            # Chapter-level code overlapping across schedules is only benign
            # when an exclusion clause documents the carve-out.
            if has_exclusion:
                continue
            warning_type = "hsn_overlap_chapter"
        else:
            warning_type = "hsn_overlap"

        warnings.append({
            "type": warning_type,
            "hsn": hsn,
            "schedules": schedules,
            "has_exclusion": has_exclusion,
            "entries": [
                {"schedule": s, "sno": r.get("sno", ""), "description": r.get("description", "")}
                for s in schedules
                for r in by_schedule[s]
            ],
        })
    return cross_count


def _check_chapter_subsumption(
    hsn_map: dict[str, dict[str, list[dict[str, Any]]]],
    warnings: list[dict[str, Any]],
) -> None:
    """Flag 2-digit chapter codes whose prefix covers a longer code elsewhere.

    A chapter entry ``63`` in schedule I and a sub-heading ``6302`` in schedule
    II is expected *only* when the chapter entry carries an ``[other than]``
    exclusion; otherwise the sub-heading rate is silently shadowed by the
    chapter rate, which is a real consistency bug.
    """
    # Index longer codes by their 2-digit chapter prefix → schedule/code info.
    longer_by_chapter: dict[str, dict[str, set[str]]] = {}
    for hsn, by_schedule in hsn_map.items():
        if len(hsn) >= 4:
            chapter = hsn[:2]
            for schedule_id in by_schedule:
                longer_by_chapter.setdefault(chapter, {}).setdefault(
                    schedule_id, set()
                ).add(hsn)

    for hsn, by_schedule in hsn_map.items():
        if len(hsn) != 2:
            continue
        siblings = longer_by_chapter.get(hsn)
        if not siblings:
            continue
        for chapter_schedule, recs in by_schedule.items():
            chapter_has_exclusion = any(
                _has_exclusion(r.get("description", "")) for r in recs
            )
            for longer_schedule, longer_codes in siblings.items():
                if longer_schedule == chapter_schedule:
                    continue
                if chapter_has_exclusion:
                    # Explicitly documented carve-out — allowed.
                    continue
                warnings.append({
                    "type": "chapter_subsumption",
                    "hsn": hsn,
                    "chapter_schedule": chapter_schedule,
                    "subheading_schedule": longer_schedule,
                    "subheading_codes": sorted(longer_codes),
                    "has_exclusion": False,
                    "detail": (
                        f"Chapter {hsn} in schedule {chapter_schedule} overlaps "
                        f"sub-heading(s) {sorted(longer_codes)} in schedule "
                        f"{longer_schedule} without an exclusion clause"
                    ),
                })


# ── rate vs. exemption cross-check ───────────────────────────────────────────


def _check_rate_exempt_conflict(
    rate_hsn_map: dict[str, dict[str, list[dict[str, Any]]]],
    exempt_hsn_map: dict[str, dict[str, list[dict[str, Any]]]],
    errors: list[dict[str, Any]],
) -> None:
    """Flag HSN codes present in both the rate and the exemption schedules.

    A tariff item cannot be simultaneously taxable (notification 1/2017) and
    exempt (notification 2/2017) at the same point in time.
    """
    rate_hsns = set(rate_hsn_map)
    exempt_hsns = set(exempt_hsn_map)
    for hsn in sorted(rate_hsns & exempt_hsns):
        rate_entries = [
            {"schedule": s, "sno": r.get("sno", "")}
            for s, recs in rate_hsn_map[hsn].items()
            for r in recs
        ]
        exempt_entries = [
            {"schedule": s, "sno": r.get("sno", "")}
            for s, recs in exempt_hsn_map[hsn].items()
            for r in recs
        ]
        errors.append({
            "type": "rate_exempt_conflict",
            "hsn": hsn,
            "rate_entries": rate_entries,
            "exempt_entries": exempt_entries,
            "detail": (
                f"HSN {hsn} appears in both the rate schedule and the exemption "
                f"schedule at the same checkpoint"
            ),
        })


# ── public API ───────────────────────────────────────────────────────────────


def validate_horizontal(
    rate_snapshot: dict,
    exempt_snapshot: dict | None = None,
) -> dict:
    """Validate cross-schedule HSN consistency of a materialized rate snapshot.

    Args:
        rate_snapshot: materialized snapshot from
            ``rate_schedule_materializer.materialize_schedule`` —
            ``{notification_id, target_notification, schedules: {id: {rate_pct, entries}}}``.
        exempt_snapshot: optional materialized exemption snapshot (e.g.
            notification ``2/2017``) for the rate-vs-exempt cross-check. Both
            snapshots should be materialized at the same checkpoint date.

    Returns:
        ``{valid, errors, warnings, stats}`` (see module docstring for schema).
        ``valid`` is False when any hard error is present; warnings never
        affect validity.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    schedules = rate_snapshot.get("schedules", {})
    duplicate_snos = 0
    total_entries = 0
    for schedule_id, table in sorted(schedules.items()):
        if not isinstance(table, dict):
            continue
        entries = table.get("entries", [])
        duplicate_snos += _check_duplicate_sno(schedule_id, entries, errors)
        _check_sno_ordering(schedule_id, entries, errors)
        total_entries += sum(1 for e in entries if not e.get("is_omitted"))

    # HSN map across rate schedules.
    rate_hsn_map = _build_hsn_map(rate_snapshot, label="rate")
    cross_schedule_hsns = _check_cross_schedule_overlap(rate_hsn_map, warnings)
    _check_chapter_subsumption(rate_hsn_map, warnings)

    # Optional rate-vs-exempt cross-check.
    if exempt_snapshot is not None:
        exempt_hsn_map = _build_hsn_map(exempt_snapshot, label="exempt")
        _check_rate_exempt_conflict(rate_hsn_map, exempt_hsn_map, errors)
        exempt_hsn_count = len(exempt_hsn_map)
    else:
        exempt_hsn_count = 0

    unique_hsns = len(rate_hsn_map)

    stats = {
        "total_entries": total_entries,
        "unique_hsns": unique_hsns,
        "duplicate_snos": duplicate_snos,
        "cross_schedule_hsns": cross_schedule_hsns,
        "exempt_hsns_checked": exempt_hsn_count,
    }

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
    }


# ── summary printing ─────────────────────────────────────────────────────────


def _print_summary(result: dict, target: str, checkpoint: str) -> None:
    stats = result.get("stats", {})
    print(f"\n=== Horizontal validation: {target} @ {checkpoint} ===")
    print(f"  valid: {result.get('valid')}")
    print(
        f"  total_entries: {stats.get('total_entries', 0)}, "
        f"unique_hsns: {stats.get('unique_hsns', 0)}, "
        f"duplicate_snos: {stats.get('duplicate_snos', 0)}, "
        f"cross_schedule_hsns: {stats.get('cross_schedule_hsns', 0)}"
    )
    print(f"  errors: {len(result.get('errors', []))}")
    for err in result.get("errors", [])[:20]:
        print(f"    [{err.get('type')}] {err.get('detail', err)}")
    if len(result.get("errors", [])) > 20:
        print(f"    ... {len(result['errors']) - 20} more errors")
    print(f"  warnings: {len(result.get('warnings', []))}")
    for warn in result.get("warnings", [])[:20]:
        wtype = warn.get("type", "")
        if wtype in {"hsn_overlap", "hsn_overlap_chapter"}:
            print(
                f"    [{wtype}] {warn.get('hsn')} in schedules "
                f"{', '.join(warn.get('schedules', []))} "
                f"(exclusion: {warn.get('has_exclusion')})"
            )
        else:
            print(f"    [{wtype}] {warn.get('detail') or warn}")
    if len(result.get("warnings", [])) > 20:
        print(f"    ... {len(result['warnings']) - 20} more warnings")


# ── CLI entry ────────────────────────────────────────────────────────────────

_BASE_RATE_JSON = "derived/version_history/rate-schedules/base_1-2017.json"
_BASE_EXEMPT_JSON = "derived/version_history/rate-schedules/base_2-2017.json"
_EVENTS_JSONL = "derived/version_history/rate-schedules/rate_amendment_events.jsonl"
_TARGET_RATE = "1/2017-ct-rate"
_TARGET_EXEMPT = "2/2017-ct-rate"
_CHECKPOINT_DATE = "2022-05-01"


def _run_cli() -> None:
    from legal_corpus.rate_schedule_materializer import materialize_schedule

    print(f"Materializing {_TARGET_RATE} @ {_CHECKPOINT_DATE} ...")
    rate_snapshot = materialize_schedule(
        _BASE_RATE_JSON,
        _EVENTS_JSONL,
        _TARGET_RATE,
        checkpoint_date=_CHECKPOINT_DATE,
    )
    print(
        f"  events applied: {rate_snapshot.get('events_applied', 0)}, "
        f"failed: {rate_snapshot.get('events_failed', 0)}, "
        f"total entries: {rate_snapshot.get('total_entries', 0)}"
    )

    exempt_snapshot = None
    if Path(_BASE_EXEMPT_JSON).exists():
        print(f"Materializing {_TARGET_EXEMPT} @ {_CHECKPOINT_DATE} ...")
        exempt_snapshot = materialize_schedule(
            _BASE_EXEMPT_JSON,
            _EVENTS_JSONL,
            _TARGET_EXEMPT,
            checkpoint_date=_CHECKPOINT_DATE,
        )
        print(
            f"  events applied: {exempt_snapshot.get('events_applied', 0)}, "
            f"failed: {exempt_snapshot.get('events_failed', 0)}, "
            f"total entries: {exempt_snapshot.get('total_entries', 0)}"
        )

    result = validate_horizontal(rate_snapshot, exempt_snapshot)
    _print_summary(result, _TARGET_RATE, _CHECKPOINT_DATE)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _run_cli()
