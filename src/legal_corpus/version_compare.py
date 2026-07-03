"""Snapshot-backed component version comparison."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from .identity_registry import load_registry
from .query import normalize_query_id


DEFAULT_VERSION_DIR = Path("derived/version_history/cgst-rules-2017")
DEFAULT_REGISTRY = Path("data/Law/statute_identity_registry.json")


def _infer_work_id(component_id: str) -> str | None:
    marker = "/rule/"
    if marker in component_id:
        return component_id.split(marker, 1)[0]
    marker = "/section/"
    if marker in component_id:
        return component_id.split(marker, 1)[0]
    return None


def normalize_version_component_id(component_id: str) -> str:
    """Normalize legacy padded rule/section IDs for version-history lookups."""
    normalized = normalize_query_id(component_id)
    for marker in ("/rule/", "/section/", "/subrule/"):
        if marker not in normalized:
            continue
        prefix, tail = normalized.split(marker, 1)
        first, sep, rest = tail.partition("/")
        match = re.fullmatch(r"0*(\d+)([a-z]*)", first, flags=re.I)
        if not match:
            return normalized
        label = f"{int(match.group(1))}{match.group(2).lower()}"
        return f"{prefix}{marker}{label}{sep}{rest}" if sep else f"{prefix}{marker}{label}"
    return normalized


def resolve_version_dir(
    component_id: str,
    *,
    target_work: str | None = None,
    registry_path: Path = DEFAULT_REGISTRY,
    version_dir: Path | None = None,
) -> tuple[Path, str | None]:
    if version_dir is not None:
        return version_dir, target_work
    registry = load_registry(registry_path)
    work_id = registry.resolve_corpus_id(target_work) if target_work else None
    work_id = work_id or _infer_work_id(component_id)
    if work_id:
        resolved = registry.resolve_corpus_id(work_id) or work_id
        configured = registry.version_history_dir(resolved)
        if configured:
            return Path(configured), resolved
    return DEFAULT_VERSION_DIR, work_id


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_node_versions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _version_at(versions: list[dict[str, Any]], date_value: str) -> dict[str, Any] | None:
    candidates = []
    for version in versions:
        start = version.get("applicability_start") or version.get("valid_from")
        end = version.get("applicability_end") or version.get("valid_to")
        if start and start <= date_value and (not end or date_value < end):
            candidates.append(version)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.get("applicability_start") or item.get("valid_from") or "")[-1]


def _slim_version(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {
        "version_id": version.get("version_id"),
        "component_id": version.get("component_id"),
        "valid_from": version.get("valid_from"),
        "valid_to": version.get("valid_to"),
        "applicability_start": version.get("applicability_start"),
        "applicability_end": version.get("applicability_end"),
        "text": version.get("text", ""),
        "text_sha256": version.get("text_sha256"),
        "created_by_event_id": version.get("created_by_event_id"),
        "event_chain": version.get("event_chain", []),
        "source_basis": version.get("source_basis", {}),
    }


def _diff(from_text: str, to_text: str, from_date: str, to_date: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            from_text.splitlines(),
            to_text.splitlines(),
            fromfile=from_date,
            tofile=to_date,
            lineterm="",
        )
    )


def _event_summaries(versions: list[dict[str, Any]], from_date: str, to_date: str) -> list[dict[str, Any]]:
    lo, hi = sorted([from_date, to_date])
    summaries: dict[str, dict[str, Any]] = {}
    for version in versions:
        start = version.get("applicability_start") or version.get("valid_from") or ""
        event_id = version.get("created_by_event_id")
        if not event_id or not (lo < start <= hi):
            continue
        basis = version.get("source_basis", {})
        summaries[event_id] = {
            "event_id": event_id,
            "operation": basis.get("operation", ""),
            "source_document_id": basis.get("source_document_id", ""),
            "source_record_id": basis.get("source_record_id", ""),
            "source_span": basis.get("source_span", {}),
            "effective_date": start,
        }
    return [summaries[key] for key in sorted(summaries)]


def _coverage_gaps(version_dir: Path, from_date: str, to_date: str) -> list[dict[str, Any]]:
    payload = _read_json(version_dir / "coverage_gaps.json")
    lo, hi = sorted([from_date, to_date])
    gaps = []
    for gap in payload.get("gaps", []):
        date_value = gap.get("date") or ""
        if not date_value or lo <= date_value <= hi:
            gaps.append(gap)
    return gaps


def _reconciliation_gaps(version_dir: Path, component_id: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    reports = []
    candidates = [version_dir / "reconciliation_report.json", *sorted(version_dir.glob("reconciliation*.json"))]
    lo, hi = sorted([from_date, to_date])
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        payload = _read_json(path)
        checkpoint_date = payload.get("checkpoint_date") or ""
        if checkpoint_date and not (lo <= checkpoint_date <= hi):
            continue
        outcomes = payload.get("component_outcomes") or {}
        unresolved_statuses = {"true_substantive_mismatch", "missing_reconstruction", "checkpoint_source_incomplete"}
        if outcomes:
            if isinstance(outcomes, dict):
                outcome = outcomes.get(component_id) or {}
            else:
                outcome = next(
                    (item for item in outcomes if isinstance(item, dict) and item.get("component_id") == component_id),
                    {},
                )
            status = outcome.get("status")
            if status not in unresolved_statuses:
                continue
            reason = status
        else:
            mismatched = {
                item.get("component_id")
                for item in payload.get("mismatched_components", [])
                if isinstance(item, dict)
            }
            missing = set(payload.get("missing_components", []))
            if component_id not in mismatched and component_id not in missing:
                continue
            reason = "checkpoint_mismatch" if component_id in mismatched else "checkpoint_missing"
        if reason:
            reports.append(
                {
                    "type": "reconciliation_gap",
                    "checkpoint_date": checkpoint_date,
                    "checkpoint_path": payload.get("checkpoint_path"),
                    "component_id": component_id,
                    "reason": reason,
                }
            )
    return reports


def _confidence_detail(version_dir: Path, component_id: str) -> dict[str, Any]:
    for path in (version_dir / "confidence_tiers.json", version_dir.parent / "confidence_tiers.json"):
        payload = _read_json(path)
        if not payload:
            continue
        details = payload.get("component_details") or {}
        tiers = payload.get("component_tiers") or {}
        if component_id in details:
            return details[component_id]
        if component_id in tiers:
            return {"tier": tiers[component_id]}
    return {"tier": "unknown"}


def _portal_completeness(version_dir: Path, component_id: str) -> dict[str, Any]:
    for path in (version_dir / "portal_completeness_report.json", version_dir.parent / "portal_completeness_report.json"):
        payload = _read_json(path)
        if not payload:
            continue
        match = re.search(r"/rule/([^/]+)", component_id)
        if not match:
            return {"status": "not_applicable"}
        row = (payload.get("rules") or {}).get(match.group(1).lower())
        if row:
            return {
                "status": row.get("portal_completeness_status", "unknown"),
                "missing_source_notifications": row.get("missing_source_notifications", []),
            }
    return {"status": "not_checked"}


def _form_statement_evidence(component_id: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    match = re.search(r"/rule/([^/]+)", component_id)
    if not match or match.group(1).lower() != "89":
        return []
    forms_versions = read_node_versions(Path("derived/version_history/forms/node_versions.jsonl"))
    lo, hi = sorted([from_date, to_date])
    evidence = []
    for version in forms_versions:
        cid = str(version.get("component_id") or "")
        if not cid.startswith("/in/union/forms/gst-rfd-01/statement/"):
            continue
        start = version.get("applicability_start") or version.get("valid_from") or ""
        if lo < start <= hi:
            evidence.append(
                {
                    "component_id": cid,
                    "effective_date": start,
                    "event_id": version.get("created_by_event_id"),
                    "source_basis": version.get("source_basis", {}),
                }
            )
    return evidence


def compare_component_versions(
    canonical_id: str,
    *,
    from_date: str | None,
    to_date: str | None,
    temporal_dimension: str = "applicability",
    version_dir: Path | None = None,
    registry_path: Path = DEFAULT_REGISTRY,
    target_work: str | None = None,
) -> dict[str, Any]:
    component_id = normalize_version_component_id(canonical_id)
    version_dir, resolved_work = resolve_version_dir(
        component_id,
        target_work=target_work,
        registry_path=registry_path,
        version_dir=version_dir,
    )
    node_versions_path = version_dir / "node_versions.jsonl"
    manifest = _read_json(version_dir / "materialization_manifest.json")
    base_as_of = manifest.get("base_as_of")
    if not node_versions_path.exists():
        return {
            "canonical_id": component_id,
            "target_work": resolved_work,
            "from_date": from_date,
            "to_date": to_date,
            "status": "no_materialized_history",
            "coverage": "incomplete",
            "temporal_dimension": temporal_dimension,
            "from_version": None,
            "to_version": None,
            "text_changed": False,
            "unified_diff": "",
            "events_between": [],
            "coverage_gaps": [],
            "warnings": ["No materialized version history found."],
        }

    rows = read_node_versions(node_versions_path)
    component_rows = [row for row in rows if normalize_version_component_id(str(row.get("component_id") or "")) == component_id]
    if not component_rows:
        return {
            "canonical_id": component_id,
            "target_work": resolved_work,
            "from_date": from_date,
            "to_date": to_date,
            "status": "not_found",
            "coverage": "incomplete",
            "temporal_dimension": temporal_dimension,
            "from_version": None,
            "to_version": None,
            "text_changed": False,
            "unified_diff": "",
            "events_between": [],
            "coverage_gaps": [],
            "warnings": ["Component not found in materialized version history."],
        }

    from_date = from_date or base_as_of
    to_date = to_date or from_date
    warnings = []
    status = "ok"
    if base_as_of and (from_date < base_as_of or to_date < base_as_of):
        status = "partial_history"
        warnings.append(f"History before {base_as_of} is partial for this work.")

    from_version = _version_at(component_rows, from_date)
    to_version = _version_at(component_rows, to_date)
    if not from_version or not to_version:
        status = "partial_history" if status == "ok" else status
        warnings.append("One or both requested dates do not have a materialized component version.")

    gaps = _coverage_gaps(version_dir, from_date, to_date)
    reconciliation_gaps = _reconciliation_gaps(version_dir, component_id, from_date, to_date)
    confidence = _confidence_detail(version_dir, component_id)
    portal = _portal_completeness(version_dir, component_id)
    statement_evidence = _form_statement_evidence(component_id, from_date, to_date)
    coverage = "incomplete" if gaps or reconciliation_gaps or status != "ok" else "complete"
    if gaps:
        warnings.append("One or more target-work amendment events in this interval were not materialized.")
    if reconciliation_gaps:
        warnings.append("One or more reconciliation checkpoints mismatch this component.")

    from_text = from_version.get("text", "") if from_version else ""
    to_text = to_version.get("text", "") if to_version else ""
    return {
        "canonical_id": component_id,
        "target_work": resolved_work,
        "from_date": from_date,
        "to_date": to_date,
        "status": status,
        "coverage": coverage,
        "confidence_tier": confidence.get("tier", "unknown"),
        "confidence_detail": confidence,
        "portal_completeness": portal,
        "temporal_dimension": temporal_dimension,
        "from_version": _slim_version(from_version),
        "to_version": _slim_version(to_version),
        "text_changed": from_text != to_text,
        "unified_diff": _diff(from_text, to_text, from_date, to_date) if from_text != to_text else "",
        "events_between": _event_summaries(component_rows, from_date, to_date),
        "coverage_gaps": gaps,
        "reconciliation_gaps": reconciliation_gaps,
        "form_statement_evidence": statement_evidence,
        "warnings": warnings,
    }


__all__ = ["compare_component_versions", "read_node_versions", "resolve_version_dir", "normalize_version_component_id"]
