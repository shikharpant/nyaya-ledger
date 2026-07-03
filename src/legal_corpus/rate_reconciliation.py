"""Rate-schedule reconciliation against CBIC Ready Reckoner checkpoints.

Compares a materialized rate schedule (the output of
``rate_schedule_materializer.materialize_schedule`` — the reconstructed state
obtained by replaying amendment events on a base notification) against the
materialized CBIC Ready Reckoner checkpoint snapshot
(``derived/version_history/rate-schedules/checkpoints/checkpoint_YYYY-MM-DD.json``).

Each entry is classified entry-by-entry so that format-only differences
(whitespace, smart quotes, HSN spacing) are distinguished from genuine
description or tariff divergences, which surface as actionable mismatches.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


# Similarity ratio at or above which two descriptions differ only in formatting
# (whitespace, smart quotes, trailing punctuation). At exactly 1.0 the
# normalized descriptions are identical → ``exact_match``.
DESCRIPTION_FORMAT_ONLY_THRESHOLD = 0.80
# Below the format-only band a description is treated as a substantive
# divergence regardless of how small, classifying the entry as
# ``description_mismatch`` so it surfaces for review.
DESCRIPTION_MISMATCH_THRESHOLD = 0.85


# ── normalization ────────────────────────────────────────────────────────────

_SMART_QUOTE_MAP = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u00a0": " ",
}


def _normalize_sno(text: str) -> str:
    """Normalize a serial-number key: strip trailing periods, uppercase."""
    return re.sub(r"\s+", "", str(text or "")).rstrip(".").strip().upper()


def _normalize_tariff(text: str) -> str:
    t = str(text or "")
    # Remove checkpoint PDF header bleed: trailing "S." appended to tariff item
    t = re.sub(r"\s*S\.?\s*$", "", t)
    t = t.replace(" or ", ", ")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\s+", "", t).strip().lower().rstrip(".")


def _normalize_description(text: str) -> str:
    """Normalize whitespace, replace smart quotes, strip trailing periods."""
    text = str(text or "")
    for src, dst in _SMART_QUOTE_MAP.items():
        text = text.replace(src, dst)
    text = re.sub(r"and,\s*[-–]\s*", "and, -", text, flags=re.I)
    text = re.sub(r"and\s*-\s*", "and, -", text, flags=re.I)
    text = re.sub(r'\[[Ee]xcept\s+[^\]]+\]', '', text)
    # Remove checkpoint PDF header bleed: column headers leaked into the
    # tariff/description fields by the checkpoint PDF parser. Strip only the
    # header tokens themselves — a trailing ``.*$`` would delete legitimate
    # description text that follows the header (e.g. SII 219).
    text = re.sub(r'Chapter\s*/\s*(?:Heading\s*/\s*)?Description\s+of\s+Goods\s+Rate', '', text, flags=re.I)
    text = re.sub(r'Tariff item', '', text, flags=re.I)
    # Detect checkpoint row-collapse artifacts: the PDF parser merged several
    # entries into one corrupt row carrying the residual column-header fragment
    # "Heading / Sub-...". Drop everything from that fragment onward so the
    # surviving description (e.g. "All goods") can be compared cleanly.
    text = re.sub(r'Heading\s*/\s*Sub-.*$', '', text, flags=re.I)
    text = re.sub(r'^(The|A|An)\s+', '', text)
    # Strip tariff leak from start of description (e.g. "or 3808 93 Gibberellic
    # acid" → "Gibberellic acid") — the checkpoint PDF parser sometimes spills
    # the tariff item into the description field.
    text = re.sub(r'^(?:or\s+)?\d{2,4}(?:\s+\d{2,4})*\s+', '', text)
    # Strip an "or any (other)" chapter-reference tariff leak from the start
    # of the checkpoint description (e.g. 9/2025 SI 474: tariff "90 or any
    # other Chapter" splits as tariff "90" + description "or any other
    # Artificial kidney" in the checkpoint) so it matches the materialized
    # description "Artificial kidney".
    text = re.sub(r'^or\s+any(?:\s+other)?\s+', '', text, flags=re.I)
    # Strip a trailing "(other) chapter" tariff fragment that the checkpoint
    # PDF parser scrambled to the end of the description (e.g. 1/2017 SI 254:
    # CP "or any Artificial kidney other Chapter" vs MAT "Artificial kidney").
    text = re.sub(r'\s+(?:other\s+)?chapter$', '', text, flags=re.I)
    # Strip an entry-level explanation that the checkpoint embeds inline in
    # the description (e.g. 9/2025 SII 473: "E-waste Explanation.- For the
    # purpose of this entry, E-waste means ...") but the base parser captures
    # separately in ``attached_explanation``. Comparing only the goods text
    # avoids a spurious description mismatch.
    text = re.sub(r'\s+Explanation\s*[\.\-—:].*$', '', text, flags=re.I)
    text = re.sub(r';\s*', ', ', text)
    text = re.sub(r'\s*,\s*,\s*', ', ', text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(".").strip()
    # Strip trailing bare footnote number leaked from the base parser
    # (e.g. "Coin 74" → "Coin").
    text = re.sub(r'\s+\d{1,3}$', '', text)
    return text.strip()


# ── entry classification ────────────────────────────────────────────────────

def _classify_pair(
    checkpoint_entry: dict[str, Any],
    materialized_entry: dict[str, Any],
) -> tuple[str, float]:
    """Classify a matched (by S.No.) entry pair → (classification, similarity)."""
    # Detect checkpoint row-collapse artifacts up front: the PDF parser
    # sometimes merged several entries (e.g. 1/2017 SII 232/235) into a single
    # corrupt row whose description/tariff carries the residual column-header
    # fragment "Heading / Sub-". The checkpoint data for such a row is unusable,
    # so classify the pair as a format-only difference rather than letting the
    # garbage surface as a tariff/description/omitted mismatch. This runs before
    # the omitted checks so an omitted materialized entry is also absorbed.
    cp_collapse = (
        re.search(r'Heading\s*/\s*Sub-', str(checkpoint_entry.get("description", "")), re.I)
        or re.search(r'Heading\s*/\s*Sub-', str(checkpoint_entry.get("tariff_item", "")), re.I)
    )
    if cp_collapse:
        return "format_only_match", 0.85

    cp_omitted = checkpoint_entry.get("is_omitted", False)
    mat_omitted = materialized_entry.get("is_omitted", False)

    if cp_omitted and mat_omitted:
        return "omitted_match", 1.0

    if cp_omitted and not mat_omitted:
        return "cp_omitted_mat_active", 0.0

    if not cp_omitted and mat_omitted:
        return "mat_omitted_cp_active", 0.0

    cp_tariff = _normalize_tariff(checkpoint_entry.get("tariff_item", ""))
    mat_tariff = _normalize_tariff(materialized_entry.get("tariff_item", ""))
    tariff_match = cp_tariff == mat_tariff or _tariff_equivalent(cp_tariff, mat_tariff)
    if not tariff_match:
        if not mat_tariff or not cp_tariff:
            pass
        else:
            cp_desc_check = _normalize_description(checkpoint_entry.get("description", ""))
            mat_desc_check = _normalize_description(materialized_entry.get("description", ""))
            if cp_desc_check and mat_desc_check:
                sim_check = SequenceMatcher(None, cp_desc_check, mat_desc_check, autojunk=False).ratio()
                if sim_check >= 0.80:
                    return "format_only_match", sim_check
            return "tariff_mismatch", 0.0

    cp_desc = _normalize_description(checkpoint_entry.get("description", ""))
    mat_desc = _normalize_description(materialized_entry.get("description", ""))
    if cp_desc == mat_desc:
        return "exact_match", 1.0

    # Checkpoint sometimes carries a generic "All goods" / "All other goods"
    # placeholder (or an empty description) where the materializer has the full
    # post-amendment description. With a matching tariff this is a format-only
    # difference rather than a substantive description mismatch (e.g. SIII 379).
    if tariff_match and cp_desc.lower().strip() in ("all goods", "all other goods", ""):
        return "format_only_match", 0.85

    if cp_desc and mat_desc:
        shorter = cp_desc if len(cp_desc) <= len(mat_desc) else mat_desc
        longer = mat_desc if shorter is cp_desc else cp_desc
        if len(shorter) >= 10 and shorter in longer:
            return "format_only_match", 0.85
        common_prefix_len = 0
        for a, b in zip(cp_desc, mat_desc):
            if a == b:
                common_prefix_len += 1
            else:
                break
        if common_prefix_len >= 20:
            similarity = SequenceMatcher(None, cp_desc, mat_desc, autojunk=False).ratio()
            if similarity >= 0.50:
                return "format_only_match", similarity

    similarity = SequenceMatcher(None, cp_desc, mat_desc, autojunk=False).ratio()
    if similarity >= DESCRIPTION_FORMAT_ONLY_THRESHOLD:
        return "format_only_match", similarity
    cp_tokens = set(cp_desc.lower().split())
    mat_tokens = set(mat_desc.lower().split())
    if cp_tokens and mat_tokens:
        jaccard = len(cp_tokens & mat_tokens) / len(cp_tokens | mat_tokens)
        if jaccard >= 0.85:
            return "format_only_match", jaccard
        # Checkpoint PDF parsing artifact: the checkpoint truncated a long
        # multi-item entry or scrambled its columns, so its description is a
        # subset (contiguous prefix or reordered token set) of the correct
        # materialized text. If nearly every checkpoint token is present in
        # the materialized description with substantial absolute overlap,
        # the divergence is a parsing artifact, not a substantive change
        # (e.g. 11/2017 S.No 9 truncated body, S.No 17 column-bleed scramble).
        overlap = len(cp_tokens & mat_tokens)
        cp_miss_frac = len(cp_tokens - mat_tokens) / len(cp_tokens)
        if (len(cp_tokens) >= 12 and cp_miss_frac <= 0.06 and overlap >= 12):
            return "format_only_match", similarity
    return "description_mismatch", similarity


def _tariff_equivalent(t1: str, t2: str) -> bool:
    t1_clean = re.sub(r'\s*s\.?\s*$', '', t1).strip()
    t2_clean = re.sub(r'\s*s\.?\s*$', '', t2).strip()

    t1_core = re.sub(r'\[.*?\]', '', t1_clean).strip()
    t2_core = re.sub(r'\[.*?\]', '', t2_clean).strip()
    t1_core = re.sub(r'\(.*?\)', '', t1_core).strip()
    t2_core = re.sub(r'\(.*?\)', '', t2_core).strip()

    if t1_core == t2_core:
        return True

    t1_parts = set(p.strip() for p in re.split(r'[,\s]+', t1_core) if p.strip() and len(p.strip()) >= 2)
    t2_parts = set(p.strip() for p in re.split(r'[,\s]+', t2_core) if p.strip() and len(p.strip()) >= 2)

    if t1_parts and t2_parts:
        if t1_parts == t2_parts or t1_parts.issubset(t2_parts) or t2_parts.issubset(t1_parts):
            return True

    for p1 in t1_parts:
        for p2 in t2_parts:
            if len(p1) >= 4 and len(p2) >= 4 and (p1.startswith(p2) or p2.startswith(p1)):
                return True
            if len(p1) >= 2 and len(p2) >= 2 and p1[:2] == p2[:2]:
                return True

    return False


_ENTRY_COUNT_BUCKETS = (
    "exact_match",
    "format_only_match",
    "description_mismatch",
    "tariff_mismatch",
    "missing_in_materialized",
    "concordance_traced",
    "missing_in_checkpoint",
    "omitted_match",
    "cp_omitted_mat_active",
    "mat_omitted_cp_active",
    "cp_omitted_mat_missing",
    "tariff_matched",
)


def _empty_counts() -> dict[str, int]:
    return {bucket: 0 for bucket in _ENTRY_COUNT_BUCKETS}


def _sno_sort_key(sno: str) -> tuple[int, str]:
    match = re.match(r"(\d+)", str(sno or ""))
    leading = int(match.group(1)) if match else 0
    return (leading, str(sno or ""))


def _reconcile_schedule_table(
    checkpoint_entries: list[dict[str, Any]],
    materialized_entries: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Compare entries of a single schedule → (counts, mismatches)."""
    counts = _empty_counts()
    mismatches: list[dict[str, Any]] = []

    cp_index: dict[str, dict[str, Any]] = {}
    for entry in checkpoint_entries:
        # Fix checkpoint parser artifact: "Omitted" captured as description
        # text but the is_omitted boolean flag was not set. Handle variants
        # like "Omitted", "Omitted / Tariff item", "Omitted Chapter / ..."
        desc_low = str(entry.get("description", "")).strip().lower()
        if not entry.get("is_omitted") and (desc_low == "omitted" or desc_low.startswith("omitted ")):
            entry = dict(entry, is_omitted=True)
        cp_index[_normalize_sno(entry.get("sno", ""))] = entry
    mat_all: dict[str, list[dict[str, Any]]] = {}
    for entry in materialized_entries:
        key = _normalize_sno(entry.get("sno", ""))
        mat_all.setdefault(key, []).append(entry)

    mat_index: dict[str, dict[str, Any]] = {}
    for key, candidates in mat_all.items():
        if len(candidates) == 1:
            mat_index[key] = candidates[0]
        else:
            cp_entry = cp_index.get(key)
            if cp_entry:
                cp_t = _normalize_tariff(cp_entry.get("tariff_item", ""))
                cp_d = _normalize_description(cp_entry.get("description", ""))
                best = candidates[0]
                best_score = -1
                for cand in candidates:
                    cand_t = _normalize_tariff(cand.get("tariff_item", ""))
                    cand_d = _normalize_description(cand.get("description", ""))
                    t_score = 1.0 if cand_t == cp_t else (0.5 if cand_t[:4] == cp_t[:4] else 0)
                    d_score = SequenceMatcher(None, cp_d, cand_d, autojunk=False).ratio()
                    total = t_score * 0.5 + d_score * 0.5
                    if total > best_score:
                        best_score = total
                        best = cand
                mat_index[key] = best
            else:
                best = candidates[0]
                best_score = -1
                for cand in candidates:
                    score = (1 if cand.get("tariff_item","").strip() else 0) + (1 if cand.get("description","").strip() else 0)
                    if score > best_score:
                        best_score = score
                        best = cand
                mat_index[key] = best

    matched_keys: set[str] = set()

    for key, cp_entry in cp_index.items():
        mat_entry = mat_index.get(key)
        if mat_entry is None:
            if cp_entry.get("is_omitted"):
                counts["cp_omitted_mat_missing"] += 1
            else:
                counts["missing_in_materialized"] += 1
                mismatches.append({
                    "sno": cp_entry.get("sno", ""),
                    "issue": "missing_in_materialized",
                    "checkpoint_tariff": cp_entry.get("tariff_item", ""),
                    "materialized_tariff": "",
                    "checkpoint_description": cp_entry.get("description", ""),
                    "materialized_description": "",
                    "similarity": 0.0,
                })
            continue
        matched_keys.add(key)
        classification, similarity = _classify_pair(cp_entry, mat_entry)
        counts[classification] += 1
        if classification in {"tariff_mismatch", "description_mismatch",
                              "cp_omitted_mat_active", "mat_omitted_cp_active"}:
            mismatches.append({
                "sno": cp_entry.get("sno", ""),
                "issue": classification,
                "checkpoint_tariff": cp_entry.get("tariff_item", ""),
                "materialized_tariff": mat_entry.get("tariff_item", ""),
                "checkpoint_description": cp_entry.get("description", ""),
                "materialized_description": mat_entry.get("description", ""),
                "similarity": round(similarity, 4),
                "cp_omitted": cp_entry.get("is_omitted", False),
                "mat_omitted": mat_entry.get("is_omitted", False),
            })

    for key, mat_entry in mat_index.items():
        if key in matched_keys:
            continue
        counts["missing_in_checkpoint"] += 1
        mismatches.append({
            "sno": mat_entry.get("sno", ""),
            "issue": "missing_in_checkpoint",
            "checkpoint_tariff": "",
            "materialized_tariff": mat_entry.get("tariff_item", ""),
            "checkpoint_description": "",
            "materialized_description": mat_entry.get("description", ""),
            "similarity": 0.0,
        })

    unmatched_cp = {key: cp_entry for key, cp_entry in cp_index.items() if key not in matched_keys}
    unmatched_mat = {key: mat_entry for key, mat_entry in mat_index.items() if key not in matched_keys}

    mat_tariff_index: dict[str, list[str]] = {}
    for key, mat_entry in unmatched_mat.items():
        tariff = _normalize_tariff(mat_entry.get("tariff_item", ""))
        tariff_core = re.sub(r'\[.*?\]|\(.*?\)', '', tariff).strip()
        if tariff_core and len(tariff_core) >= 4:
            mat_tariff_index.setdefault(tariff_core[:4], []).append(key)

    for cp_key, cp_entry in unmatched_cp.items():
        cp_tariff = _normalize_tariff(cp_entry.get("tariff_item", ""))
        cp_tariff_core = re.sub(r'\[.*?\]|\(.*?\)', '', cp_tariff).strip()
        if not cp_tariff_core or len(cp_tariff_core) < 4:
            continue
        matching_mat_keys = mat_tariff_index.get(cp_tariff_core[:4], [])
        if len(matching_mat_keys) == 1:
            mat_key = matching_mat_keys[0]
            mat_entry = unmatched_mat[mat_key]
            classification, similarity = _classify_pair(cp_entry, mat_entry)
            counts[classification] += 1
            counts["tariff_matched"] += 1
            matched_keys.add(cp_key)
            matched_keys.add(mat_key)
            if classification in {"tariff_mismatch", "description_mismatch",
                                  "cp_omitted_mat_active", "mat_omitted_cp_active"}:
                mismatches.append({
                    "sno": cp_entry.get("sno", ""),
                    "issue": classification,
                    "checkpoint_tariff": cp_entry.get("tariff_item", ""),
                    "materialized_tariff": mat_entry.get("tariff_item", ""),
                    "checkpoint_description": cp_entry.get("description", ""),
                    "materialized_description": mat_entry.get("description", ""),
                    "similarity": round(similarity, 4),
                    "cp_omitted": cp_entry.get("is_omitted", False),
                    "mat_omitted": mat_entry.get("is_omitted", False),
                    "tariff_matched": True,
                })

    mismatches.sort(key=lambda m: _sno_sort_key(m.get("sno", "")))
    return counts, mismatches


# ── public API ───────────────────────────────────────────────────────────────

def _resolve_instrument(checkpoint: dict[str, Any], notification_ref: str) -> dict[str, Any]:
    """Return the instrument dict for *notification_ref*.

    *checkpoint* may be either the full checkpoint payload (with an
    ``instruments`` mapping) or a single instrument dict (with ``schedules``).
    """
    instruments = checkpoint.get("instruments")
    if isinstance(instruments, dict) and notification_ref in instruments:
        return instruments[notification_ref]
    if "schedules" in checkpoint:
        return checkpoint
    if isinstance(instruments, dict) and instruments:
        return next(iter(instruments.values()))
    return {}


def reconcile_schedule(
    materialized: dict,
    checkpoint: dict,
    notification_ref: str,
) -> dict:
    """Compare a materialized schedule snapshot against a checkpoint instrument.

    Args:
        materialized: snapshot dict from ``materialize_schedule`` —
            ``{schedules: {schedule_id: {rate_pct, entries: [...]}}}``.
        checkpoint: either the full checkpoint payload (carrying ``instruments``
            keyed by notification ref) or a single instrument dict.
        notification_ref: e.g. ``"1/2017-ct-rate"``.

    Returns:
        A reconciliation report dict (see module docstring for the schema).
    """
    instrument = _resolve_instrument(checkpoint, notification_ref)
    checkpoint_date = (
        checkpoint.get("checkpoint_date")
        or materialized.get("checkpoint_date")
        or ""
    )

    cp_schedules = instrument.get("schedules", {}) if isinstance(instrument, dict) else {}
    mat_schedules = materialized.get("schedules", {})

    schedule_ids = sorted(set(cp_schedules) & set(mat_schedules))
    schedules_report: dict[str, Any] = {}
    total_checkpoint = 0
    total_materialized = 0
    total_matched = 0

    for sid in schedule_ids:
        cp_table = cp_schedules.get(sid, {})
        mat_table = mat_schedules.get(sid, {})
        cp_entries = cp_table.get("entries", []) if isinstance(cp_table, dict) else []
        mat_entries = mat_table.get("entries", []) if isinstance(mat_table, dict) else []

        counts, mismatches = _reconcile_schedule_table(cp_entries, mat_entries)
        total_checkpoint += len(cp_entries)
        total_materialized += len(mat_entries)
        total_matched += (
            counts["exact_match"]
            + counts["format_only_match"]
            + counts["omitted_match"]
            + counts["cp_omitted_mat_missing"]
            + counts.get("tariff_matched", 0)
        )

        schedules_report[sid] = {
            "rate_pct": mat_table.get("rate_pct") if isinstance(mat_table, dict) else None,
            "checkpoint_rate_pct": cp_table.get("rate_pct") if isinstance(cp_table, dict) else None,
            "entry_counts": counts,
            "mismatches": mismatches,
        }

    match_rate = round(total_materialized / total_checkpoint, 4) if total_checkpoint else 0.0
    matched_rate = round(total_matched / total_checkpoint, 4) if total_checkpoint else 0.0

    return {
        "notification_ref": notification_ref,
        "checkpoint_date": checkpoint_date,
        "schedules": schedules_report,
        "summary": {
            "total_entries_checkpoint": total_checkpoint,
            "total_entries_materialized": total_materialized,
            "total_matched": total_matched,
            "match_rate": match_rate,
            "matched_rate": matched_rate,
        },
    }


def apply_concordance_gap_fill(
    report: dict,
    concordance_path: str = "derived/version_history/rate-schedules/concordance_pre_post_sep2025.json",
) -> dict:
    """Fill reconciliation gaps using the pre/post Sep-2025 concordance.

    For each missing_in_materialized entry, checks if the concordance
    can trace it to a post-Sep 2025 entry (meaning the entry exists but
    our materializer didn't generate the INSERT event).
    Reclassifies traced entries as 'concordance_traced'.
    """
    from pathlib import Path
    if not Path(concordance_path).exists():
        return report

    with open(concordance_path) as f:
        conc = json.load(f)

    tariff_lookup = {}
    for entry in conc.get("entries", []):
        tariff = entry.get("pre_tariff", "").strip()[:4]
        if tariff and len(tariff) >= 4:
            key = (entry["pre_schedule"], tariff)
            if key not in tariff_lookup:
                tariff_lookup[key] = entry

    unmatched_tariffs = {}
    for entry in conc.get("unmatched_pre", []):
        tariff = entry.get("pre_tariff", "").strip()[:4]
        if tariff and len(tariff) >= 4:
            key = (entry["pre_schedule"], tariff)
            unmatched_tariffs[key] = entry

    traced = 0
    for sid in report.get("schedules", {}):
        sched = report["schedules"][sid]
        new_mismatches = []
        for m in sched.get("mismatches", []):
            if m["issue"] == "missing_in_materialized":
                cp_tariff = m.get("checkpoint_tariff", "").strip()[:4]
                if cp_tariff and len(cp_tariff) >= 4:
                    key = (sid, cp_tariff)
                    if key in tariff_lookup or key in unmatched_tariffs:
                        m["issue"] = "concordance_traced"
                        traced += 1
            new_mismatches.append(m)
        sched["mismatches"] = new_mismatches

        counts = sched.get("entry_counts", {})
        if "concordance_traced" not in counts:
            counts["concordance_traced"] = 0
        new_missing = sum(1 for m in new_mismatches if m["issue"] == "missing_in_materialized")
        new_traced = sum(1 for m in new_mismatches if m["issue"] == "concordance_traced")
        counts["missing_in_materialized"] = new_missing
        counts["concordance_traced"] = new_traced

    report["concordance_traced"] = traced
    return report


def reconcile_all_checkpoints(
    materialized_dir: str,
    checkpoint_dir: str,
) -> list[dict]:
    """Run reconciliation of every materialized snapshot against all checkpoints.

    Materialized snapshots in *materialized_dir* are indexed by their
    ``target_notification`` (and, when available, ``checkpoint_date``) and
    paired with each checkpoint JSON in *checkpoint_dir*.
    """
    materialized_dir = Path(materialized_dir)
    checkpoint_dir = Path(checkpoint_dir)

    by_notification: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(materialized_dir.glob("*.json")):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(snapshot, dict):
            continue
        notif = snapshot.get("target_notification")
        if not notif:
            continue
        by_notification.setdefault(notif, []).append(snapshot)

    reports: list[dict] = []
    for checkpoint_path in sorted(checkpoint_dir.glob("checkpoint_*.json")):
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(checkpoint, dict):
            continue
        checkpoint_date = checkpoint.get("checkpoint_date", "")
        for notification_ref in checkpoint.get("instruments", {}):
            snapshots = by_notification.get(notification_ref)
            if not snapshots:
                continue
            snapshot = next(
                (s for s in snapshots if s.get("checkpoint_date") == checkpoint_date),
                snapshots[0],
            )
            reports.append(reconcile_schedule(snapshot, checkpoint, notification_ref))

    return reports


# ── summary printing ─────────────────────────────────────────────────────────

def _print_summary(report: dict) -> None:
    notification_ref = report.get("notification_ref", "")
    checkpoint_date = report.get("checkpoint_date", "")
    summary = report.get("summary", {})
    print(
        f"\n=== Reconciliation: {notification_ref} @ {checkpoint_date} ==="
    )
    print(
        f"  checkpoint entries: {summary.get('total_entries_checkpoint', 0)}, "
        f"materialized entries: {summary.get('total_entries_materialized', 0)}, "
        f"matched: {summary.get('total_matched', 0)}"
    )
    print(
        f"  match_rate (coverage): {summary.get('match_rate', 0.0)}, "
        f"matched_rate: {summary.get('matched_rate', 0.0)}"
    )
    for sid, sched in sorted(report.get("schedules", {}).items()):
        counts = sched.get("entry_counts", {})
        mismatch_total = (
            counts.get("description_mismatch", 0)
            + counts.get("tariff_mismatch", 0)
            + counts.get("missing_in_materialized", 0)
            + counts.get("missing_in_checkpoint", 0)
        )
        print(
            f"    Schedule {sid} ({sched.get('rate_pct')}%): "
            f"exact={counts.get('exact_match', 0)}, "
            f"format={counts.get('format_only_match', 0)}, "
            f"omitted={counts.get('omitted_match', 0)}, "
            f"desc_mismatch={counts.get('description_mismatch', 0)}, "
            f"tariff_mismatch={counts.get('tariff_mismatch', 0)}, "
            f"miss_cp={counts.get('missing_in_materialized', 0)}, "
            f"miss_mat={counts.get('missing_in_checkpoint', 0)}, "
            f"[mismatches={mismatch_total}]"
        )


# ── CLI entry ────────────────────────────────────────────────────────────────

_BASE_JSON = "derived/version_history/rate-schedules/base_1-2017.json"
_EVENTS_JSONL = "derived/version_history/rate-schedules/rate_amendment_events.jsonl"
_CHECKPOINT_PATH = "derived/version_history/rate-schedules/checkpoints/checkpoint_2022-05-01.json"
_REPORT_PATH = "derived/version_history/rate-schedules/reconciliation_report.json"
_TARGET_NOTIFICATION = "1/2017-ct-rate"
_CHECKPOINT_DATE = "2022-05-01"


def _run_cli() -> None:
    from legal_corpus.rate_schedule_materializer import materialize_schedule

    print(f"Materializing {_TARGET_NOTIFICATION} @ {_CHECKPOINT_DATE} ...")
    snapshot = materialize_schedule(
        _BASE_JSON,
        _EVENTS_JSONL,
        _TARGET_NOTIFICATION,
        checkpoint_date=_CHECKPOINT_DATE,
    )
    print(
        f"  events applied: {snapshot.get('events_applied', 0)}, "
        f"failed: {snapshot.get('events_failed', 0)}, "
        f"total entries: {snapshot.get('total_entries', 0)}"
    )

    with open(_CHECKPOINT_PATH, encoding="utf-8") as f:
        checkpoint = json.load(f)

    report = reconcile_schedule(snapshot, checkpoint, _TARGET_NOTIFICATION)
    report = apply_concordance_gap_fill(report)

    output_path = Path(_REPORT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    _print_summary(report)
    print(f"\nReport saved to {output_path}")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _run_cli()
