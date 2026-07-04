"""Confidence scoring and adjudication for rate schedule reconciliation.

For each entry in a materialized schedule, assigns a confidence score and
classifies mismatches against the checkpoint as either materializer errors,
checkpoint errors, or genuine CBIC discrepancies.

This module provides the "confident enough to say CBIC was wrong" capability.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET


def _normalize_text(text: str) -> str:
    text = str(text or "")
    for src, dst in {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
                     "\u2013": "-", "\u2014": "-", "\u00a0": " "}.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip().rstrip(".").strip()


def _normalize_tariff(t: str) -> str:
    t = re.sub(r"\s+", "", str(t or "")).strip().lower()
    t = re.sub(r"\s*s\.?\s*$", "", t)
    return re.sub(r"\[.*?\]", "", t).strip()


def _extract_source_texts(
    source_notification: str,
    corpus_dir: str = "corpus/in/union/notifications/cbic/central-tax-rate",
) -> str:
    """Extract all text from the source amending notification XML."""
    if not source_notification:
        return ""
    parts = source_notification.split("/")
    if len(parts) < 2:
        return ""
    year = parts[-2]
    fname = parts[-1]
    if not fname.endswith(".xml"):
        fname = fname + ".xml"
    xml_path = Path(corpus_dir) / year / fname
    if not xml_path.exists():
        return ""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        return " ".join(t.strip() for t in root.itertext())
    except Exception:
        return ""


def _build_multi_source_index(
    events_jsonl_path: str,
    corpus_dir: str = "corpus/in/union/notifications/cbic/central-tax-rate",
) -> dict[tuple[str, str], list[str]]:
    """Build a multi-notification source index.

    Maps (schedule, S.No.) → list of source notification IDs that mention it.
    This allows adjudication to check ALL notifications, not just the last one.
    """
    index: dict[tuple[str, str], list[str]] = {}
    if not Path(events_jsonl_path).exists():
        return index

    source_texts_cache: dict[str, str] = {}

    with open(events_jsonl_path) as f:
        for line in f:
            evt = json.loads(line)
            sched = evt.get("target_schedule", "")
            payload = evt.get("payload", {})
            source_notif = evt.get("source_notification", "")
            if not source_notif:
                continue

            snos: list[str] = []
            sno = str(payload.get("sno", "")).rstrip(".").upper()
            if sno:
                snos.append(sno)
            for s in payload.get("sno_list", []):
                snos.append(str(s).rstrip(".").upper())

            for sno in snos:
                key = (sched, sno)
                if source_notif not in index.setdefault(key, []):
                    index[key].append(source_notif)

    return index


def _check_source_agreement(
    source_text: str,
    materialized_tariff: str,
    materialized_desc: str,
    checkpoint_tariff: str,
    checkpoint_desc: str,
) -> str:
    """Check which source (materialized or checkpoint) the notification XML agrees with.

    Returns: 'materializer_correct', 'checkpoint_correct', 'ambiguous'
    """
    if not source_text:
        return "ambiguous"

    source_low = source_text.lower()
    mat_desc_norm = _normalize_text(materialized_desc).lower()
    cp_desc_norm = _normalize_text(checkpoint_desc).lower()

    mat_in_source = False
    cp_in_source = False

    if mat_desc_norm and len(mat_desc_norm) > 10:
        if mat_desc_norm[:60] in source_low or source_low.find(mat_desc_norm[:40]) >= 0:
            mat_in_source = True

    if cp_desc_norm and len(cp_desc_norm) > 10:
        if cp_desc_norm[:60] in source_low or source_low.find(cp_desc_norm[:40]) >= 0:
            cp_in_source = True

    if mat_in_source and not cp_in_source:
        return "materializer_correct"
    if cp_in_source and not mat_in_source:
        return "checkpoint_correct"
    if mat_in_source and cp_in_source:
        return "ambiguous"

    mat_tariff = _normalize_tariff(materialized_tariff)
    cp_tariff = _normalize_tariff(checkpoint_tariff)
    if mat_tariff and mat_tariff in source_low.replace(" ", ""):
        mat_in_source = True
    if cp_tariff and cp_tariff in source_low.replace(" ", ""):
        cp_in_source = True

    if mat_in_source and not cp_in_source:
        return "materializer_correct"
    if cp_in_source and not mat_in_source:
        return "checkpoint_correct"
    return "ambiguous"


def _check_multi_source_agreement(
    source_notifications: list[str],
    materialized_tariff: str,
    materialized_desc: str,
    checkpoint_tariff: str,
    checkpoint_desc: str,
    corpus_dir: str = "corpus/in/union/notifications/cbic/central-tax-rate",
) -> str:
    """Check ALL source notifications to determine agreement.

    Returns: 'materializer_correct', 'checkpoint_correct', 'ambiguous'
    """
    if not source_notifications:
        return "ambiguous"

    mat_desc_norm = _normalize_text(materialized_desc).lower()
    cp_desc_norm = _normalize_text(checkpoint_desc).lower()
    mat_tariff = _normalize_tariff(materialized_tariff)
    cp_tariff = _normalize_tariff(checkpoint_tariff)

    mat_found = False
    cp_found = False

    for source_notif in source_notifications:
        source_text = _extract_source_texts(source_notif, corpus_dir)
        if not source_text:
            continue
        source_low = source_text.lower()
        source_nospace = source_low.replace(" ", "")

        if mat_desc_norm and len(mat_desc_norm) > 10:
            if mat_desc_norm[:50] in source_low or source_low.find(mat_desc_norm[:30]) >= 0:
                mat_found = True
        if cp_desc_norm and len(cp_desc_norm) > 10:
            if cp_desc_norm[:50] in source_low or source_low.find(cp_desc_norm[:30]) >= 0:
                cp_found = True
        if mat_tariff and mat_tariff in source_nospace:
            mat_found = True
        if cp_tariff and cp_tariff in source_nospace:
            cp_found = True

    if mat_found and not cp_found:
        return "materializer_correct"
    if cp_found and not mat_found:
        return "checkpoint_correct"
    return "ambiguous"


def score_entry_confidence(
    classification: str,
    similarity: float = 0.0,
    event_status: str = "",
) -> float:
    """Assign a confidence score to an entry based on its classification.

    Returns a float from 0.0 to 1.0.
    """
    scores = {
        "exact_match": 1.0,
        "format_only_match": 0.95,
        "omitted_match": 1.0,
        "cp_omitted_mat_missing": 0.9,
        "cp_omitted_mat_active": 0.0,
        "mat_omitted_cp_active": 0.0,
        "missing_in_materialized": 0.0,
        "missing_in_checkpoint": 0.5,
    }

    base = scores.get(classification, 0.0)

    if classification == "description_mismatch":
        if similarity >= 0.85:
            base = 0.7
        elif similarity >= 0.5:
            base = 0.3
        else:
            base = 0.0

    if classification == "tariff_mismatch":
        base = 0.0

    if event_status == "llm_classified":
        base = min(base, 0.7)

    return base


def adjudicate_mismatch(
    mismatch: dict[str, Any],
    source_notification: str = "",
    corpus_dir: str = "corpus/in/union/notifications/cbic/central-tax-rate",
) -> dict[str, Any]:
    """Adjudicate a single mismatch entry.

    Returns a dict with:
        - verdict: 'materializer_error', 'checkpoint_error', 'cbic_discrepancy', 'ambiguous'
        - confidence: float 0-1
        - evidence: explanation string
    """
    issue = mismatch.get("issue", "")
    mat_tariff = mismatch.get("materialized_tariff", "")
    mat_desc = mismatch.get("materialized_description", "")
    cp_tariff = mismatch.get("checkpoint_tariff", "")
    cp_desc = mismatch.get("checkpoint_description", "")

    if issue == "cp_omitted_mat_active":
        sno = str(mismatch.get("sno", ""))
        if source_notification:
            src_text = _extract_source_texts(source_notification, corpus_dir)
            if src_text and sno in src_text and "shall be omitted" in src_text.lower():
                return {
                    "verdict": "cbic_discrepancy",
                    "confidence": 0.85,
                    "evidence": f"Source notification confirms S.No. {sno} shall be omitted; checkpoint still shows it active",
                }
        return {
            "verdict": "ambiguous",
            "confidence": 0.3,
            "evidence": f"Checkpoint marks S.No. {sno} as omitted but materializer has it active; no source confirms omission",
        }

    if issue == "mat_omitted_cp_active":
        return {
            "verdict": "materializer_error",
            "confidence": 0.9,
            "evidence": f"Materializer marks S.No. {mismatch.get('sno','')} as omitted but checkpoint has it active",
        }

    if issue == "missing_in_materialized":
        if not cp_tariff.strip() and not cp_desc.strip():
            return {
                "verdict": "checkpoint_error",
                "confidence": 0.8,
                "evidence": f"Checkpoint entry for S.No. {mismatch.get('sno','')} has empty data",
            }
        source_text = _extract_source_texts(source_notification, corpus_dir) if source_notification else ""
        if source_text:
            cp_desc_norm = _normalize_text(cp_desc).lower()
            if cp_desc_norm[:40] in source_text.lower():
                return {
                    "verdict": "materializer_error",
                    "confidence": 0.8,
                    "evidence": f"Source notification confirms checkpoint data for S.No. {mismatch.get('sno','')}",
                }
        return {
            "verdict": "ambiguous",
            "confidence": 0.3,
            "evidence": f"Missing S.No. {mismatch.get('sno','')} in materialized schedule",
        }

    if issue in ("tariff_mismatch", "description_mismatch"):
        source_text = _extract_source_texts(source_notification, corpus_dir) if source_notification else ""
        agreement = _check_source_agreement(
            source_text, mat_tariff, mat_desc, cp_tariff, cp_desc,
        )
        sim = mismatch.get("similarity", 0.0)

        if agreement == "materializer_correct":
            return {
                "verdict": "cbic_discrepancy",
                "confidence": 0.85,
                "evidence": f"Source notification text matches materialized value for S.No. {mismatch.get('sno','')}, "
                           f"checkpoint differs (similarity={sim:.2f})",
            }
        elif agreement == "checkpoint_correct":
            return {
                "verdict": "materializer_error",
                "confidence": 0.85,
                "evidence": f"Source notification text matches checkpoint value for S.No. {mismatch.get('sno','')}",
            }
        else:
            if sim > 0.8:
                return {
                    "verdict": "ambiguous",
                    "confidence": 0.4,
                    "evidence": f"Minor text difference (similarity={sim:.2f}), could not determine source",
                }
            return {
                "verdict": "ambiguous",
                "confidence": 0.2,
                "evidence": f"Significant difference (similarity={sim:.2f}), no source match found",
            }

    return {
        "verdict": "ambiguous",
        "confidence": 0.0,
        "evidence": f"Unrecognized issue type: {issue}",
    }


def adjudicate_mismatch_multi(
    mismatch: dict[str, Any],
    source_notifications: list[str],
    corpus_dir: str = "corpus/in/union/notifications/cbic/central-tax-rate",
) -> dict[str, Any]:
    """Adjudicate a mismatch using multiple source notifications."""
    issue = mismatch.get("issue", "")
    mat_tariff = mismatch.get("materialized_tariff", "")
    mat_desc = mismatch.get("materialized_description", "")
    cp_tariff = mismatch.get("checkpoint_tariff", "")
    cp_desc = mismatch.get("checkpoint_description", "")

    if issue == "cp_omitted_mat_active":
        sno = str(mismatch.get("sno", ""))
        sched = mismatch.get("schedule", "")
        if source_notifications:
            for src_notif in source_notifications:
                src_text = _extract_source_texts(src_notif, corpus_dir)
                if src_text and sno in src_text and "shall be omitted" in src_text.lower():
                    return {
                        "verdict": "cbic_discrepancy",
                        "confidence": 0.85,
                        "evidence": f"Source notification confirms S.No. {sno} shall be omitted; checkpoint still shows it active",
                    }
        return {
            "verdict": "checkpoint_error",
            "confidence": 0.5,
            "evidence": f"Checkpoint marks S.No. {sno} as omitted but materializer has it active; no source confirms omission in {sched}",
        }

    if issue == "mat_omitted_cp_active":
        if source_notifications:
            sno = str(mismatch.get("sno", ""))
            for src_notif in source_notifications:
                src_text = _extract_source_texts(src_notif, corpus_dir)
                if src_text and sno in src_text and "shall be omitted" in src_text.lower():
                    return {
                        "verdict": "cbic_discrepancy",
                        "confidence": 0.85,
                        "evidence": f"Source notification confirms S.No. {sno} shall be omitted; checkpoint still shows it active",
                    }
        return {
            "verdict": "materializer_error",
            "confidence": 0.9,
            "evidence": f"Materializer marks S.No. {mismatch.get('sno','')} as omitted but checkpoint has it active",
        }

    if issue == "concordance_traced":
        return {
            "verdict": "checkpoint_error",
            "confidence": 0.6,
            "evidence": f"S.No. {mismatch.get('sno','')} traced via concordance",
        }

    if issue == "missing_in_materialized":
        if not cp_tariff.strip() and not cp_desc.strip():
            return {
                "verdict": "checkpoint_error",
                "confidence": 0.8,
                "evidence": f"Checkpoint entry for S.No. {mismatch.get('sno','')} has empty data",
            }
        if source_notifications:
            agreement = _check_multi_source_agreement(
                source_notifications, "", "", cp_tariff, cp_desc, corpus_dir,
            )
            if agreement == "checkpoint_correct":
                return {
                    "verdict": "materializer_error",
                    "confidence": 0.8,
                    "evidence": f"Source notification confirms checkpoint data for S.No. {mismatch.get('sno','')}",
                }
        return {
            "verdict": "ambiguous",
            "confidence": 0.3,
            "evidence": f"Missing S.No. {mismatch.get('sno','')} in materialized schedule",
        }

    if issue in ("tariff_mismatch", "description_mismatch"):
        if source_notifications:
            agreement = _check_multi_source_agreement(
                source_notifications, mat_tariff, mat_desc, cp_tariff, cp_desc, corpus_dir,
            )
        else:
            agreement = "ambiguous"
        sim = mismatch.get("similarity", 0.0)

        if agreement == "materializer_correct":
            return {
                "verdict": "cbic_discrepancy",
                "confidence": 0.85,
                "evidence": f"Source notification text matches materialized value for S.No. {mismatch.get('sno','')}, "
                           f"checkpoint differs (similarity={sim:.2f})",
            }
        elif agreement == "checkpoint_correct":
            return {
                "verdict": "materializer_error",
                "confidence": 0.85,
                "evidence": f"Source notification text matches checkpoint value for S.No. {mismatch.get('sno','')}",
            }
        else:
            if sim > 0.8:
                return {
                    "verdict": "ambiguous",
                    "confidence": 0.4,
                    "evidence": f"Minor text difference (similarity={sim:.2f}), could not determine source",
                }
            return {
                "verdict": "ambiguous",
                "confidence": 0.2,
                "evidence": f"Significant difference (similarity={sim:.2f}), no source match found",
            }

    if issue == "missing_in_checkpoint":
        return {
            "verdict": "checkpoint_error",
            "confidence": 0.6,
            "evidence": f"S.No. {mismatch.get('sno','')} present in materialized but not in checkpoint",
        }

    return {
        "verdict": "ambiguous",
        "confidence": 0.0,
        "evidence": f"Unrecognized issue type: {issue}",
    }


def generate_adjudication_report(
    reconciliation_report: dict,
    events_jsonl_path: str = "derived/version_history/rate-schedules/rate_amendment_events.jsonl",
    corpus_dir: str = "corpus/in/union/notifications/cbic/central-tax-rate",
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate a full adjudication report from a reconciliation report.

    Returns a dict with:
        - summary: {total, matched, mismatched, materializer_errors,
                    checkpoint_errors, cbic_discrepancies, ambiguous,
                    match_rate, confidence_match_rate, confidence_score}
        - schedules: {sid: {total, matched, confidence_score}}
        - entries: list of per-entry adjudication results

    ``summary.confidence_score`` is a weighted average (0.0-1.0) of the
    confidence of every entry counted in ``summary.total``: matched entries
    are scored via :func:`score_entry_confidence` and mismatched entries use
    the per-entry confidence from :func:`adjudicate_mismatch_multi`.
    """
    source_index = _build_multi_source_index(events_jsonl_path, corpus_dir)

    # Matched entry buckets (high confidence) and the buckets that count
    # toward a schedule's entry total but are adjudicated as mismatches.
    matched_buckets = ("exact_match", "format_only_match", "omitted_match", "cp_omitted_mat_missing")
    mismatch_buckets = ("description_mismatch", "tariff_mismatch",
                        "cp_omitted_mat_active", "mat_omitted_cp_active",
                        "missing_in_materialized", "missing_in_checkpoint")

    summary = {
        "total": 0,
        "matched": 0,
        "materializer_errors": 0,
        "checkpoint_errors": 0,
        "cbic_discrepancies": 0,
        "ambiguous": 0,
    }

    entries: list[dict[str, Any]] = []
    schedules_out: dict[str, dict[str, Any]] = {}

    # Running confidence numerator across all schedules.
    confidence_sum = 0.0

    for sid in sorted(reconciliation_report.get("schedules", {}).keys()):
        sched_report = reconciliation_report["schedules"][sid]
        ec = sched_report.get("entry_counts", {})

        sched_total = 0
        sched_matched = 0
        sched_conf_sum = 0.0

        for bucket in matched_buckets:
            cnt = ec.get(bucket, 0)
            summary["matched"] += cnt
            summary["total"] += cnt
            sched_matched += cnt
            sched_total += cnt
            sched_conf_sum += cnt * score_entry_confidence(bucket)

        for bucket in mismatch_buckets:
            cnt = ec.get(bucket, 0)
            summary["total"] += cnt
            sched_total += cnt

        for mismatch in sched_report.get("mismatches", []):
            sno = str(mismatch.get("sno", "")).rstrip(".").upper()
            source_notifs = source_index.get((sid, sno), []) or source_index.get(("", sno), [])
            source_notif = source_notifs[0] if source_notifs else ""

            result = adjudicate_mismatch_multi(mismatch, source_notifs, corpus_dir)
            result["schedule"] = sid
            result["sno"] = mismatch.get("sno", "")
            result["issue"] = mismatch.get("issue", "")

            verdict = result["verdict"]
            if verdict == "materializer_error":
                summary["materializer_errors"] += 1
            elif verdict == "checkpoint_error":
                summary["checkpoint_errors"] += 1
            elif verdict == "cbic_discrepancy":
                summary["cbic_discrepancies"] += 1
            else:
                summary["ambiguous"] += 1

            # Only entries whose issue is counted in `total` contribute to the
            # confidence average, keeping the numerator/denominator consistent.
            # (e.g. concordance_traced entries are excluded from both.)
            if mismatch.get("issue") in mismatch_buckets:
                sched_conf_sum += result.get("confidence", 0.0)

            entries.append(result)

        schedules_out[sid] = {
            "total": sched_total,
            "matched": sched_matched,
            "confidence_score": round(sched_conf_sum / max(sched_total, 1), 4),
        }
        confidence_sum += sched_conf_sum

    summary["mismatched"] = summary["total"] - summary["matched"]
    summary["match_rate"] = round(summary["matched"] / max(summary["total"], 1) * 100, 1)
    summary["confidence_match_rate"] = round(
        (summary["matched"] + summary["checkpoint_errors"] + summary["cbic_discrepancies"]) /
        max(summary["total"], 1) * 100, 1
    )
    summary["confidence_score"] = round(confidence_sum / max(summary["total"], 1), 4)

    report = {"summary": summary, "schedules": schedules_out, "entries": entries}

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    return report
