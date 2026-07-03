"""Portal-reference completeness checks for version-history coverage."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from html import unescape
from pathlib import Path
from typing import Any

from .amendment_events import read_events


_RE_RULE_FROM_NAME = re.compile(r"rule_?([0-9]+[a-z]?)", re.IGNORECASE)
_RE_NOTIFICATION = re.compile(
    r"(?:notification|notif(?:ication)?\.?|notf\.?)\s*(?:no\.?)?\s*"
    r"([0-9]+)\s*/\s*([0-9]{4})",
    re.IGNORECASE,
)
_RE_DOC_NOTIFICATION = re.compile(r"/([0-9]+)-([0-9]{4})(?:-|$)")
_RE_BARE_NOTIFICATION = re.compile(r"^\s*([0-9]+)\s*/\s*([0-9]{4})\s*[-\u2013]?\s*(?:Central\s+Tax|Union\s+Tax|Integrated\s+Tax)\b", re.IGNORECASE)
_RE_DATED_YEAR = re.compile(
    r"\bdated\s+(?:the\s+)?(?:\d{1,2}(?:st|nd|rd|th)?[\s./-]+(?:day\s+of\s+)?[A-Za-z]+|\d{1,2}[./-]\d{1,2})[\s,./-]+(\d{4})",
    re.IGNORECASE,
)
_RE_EXTERNAL_NOTIFICATION_CLASS = re.compile(
    r"(?:Central\s+Tax\s*\(\s*Rate\s*\)|Integrated\s+Tax\s*\(\s*Rate\s*\)|Customs)",
    re.IGNORECASE,
)
_RE_BENEFIT_NOTIFICATION_REFERENCE = re.compile(
    r"\b(?:benefit\s+of|availed\s+the\s+benefit\s+of|under\s+the\s+said\s+notifications?)\b",
    re.IGNORECASE,
)
_RE_DATE_BASIS_NOTIFICATION_REFERENCE = re.compile(
    r"\b(?:as\s+notified\s+by|appointed\s+vide|brought\s+into\s+force(?:.{0,40}?\b(?:by|vide))?|w\.?e\.?f\.?.{0,40}?\b(?:by|vide))\s*(?:notification|notif)",
    re.IGNORECASE,
)
_RE_CONTEXTUAL_NOTIFICATION_REFERENCE = re.compile(
    r"(?:\b(?:kindly\s+also\s+refer\s+to|also\s+refer\s+to|means\s+as\s+notified\s+by)\s*(?:notification|notif)"
    r"|^\s*\d{4}\s*\([^)]*w\.?e\.?f\.?[^)]*\)\s+and\s+(?:notification|notif))",
    re.IGNORECASE,
)
_RE_DATE_BASIS_NOTIFICATION = re.compile(r"(?:notification|notif)[^0-9]*(\d+)[_-](\d{4})", re.IGNORECASE)


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _norm_notif(number: str, year: str) -> str:
    return f"{int(number)}/{year}"


def _correct_portal_ref_from_excerpt(ref: str, excerpt: str, known_event_refs: set[str]) -> tuple[str, str | None]:
    """Correct obvious portal notification-year typos only when the event ledger proves the corrected ref exists."""
    if ref in known_event_refs:
        return ref, None
    number, _, year = ref.partition("/")
    if not number or not year:
        return ref, None
    years = {match.group(1) for match in _RE_DATED_YEAR.finditer(excerpt or "")}
    for dated_year in years:
        candidate = f"{int(number)}/{dated_year}"
        if dated_year != year and candidate in known_event_refs:
            return candidate, ref
    return ref, None


def _extract_rule_number(path: Path) -> str | None:
    match = _RE_RULE_FROM_NAME.search(path.name)
    return match.group(1).lower() if match else None


def _event_notification_ref(event: dict[str, Any]) -> str | None:
    source = event.get("source") or {}
    for value in (
        source.get("instrument_number"),
        source.get("document_id"),
        source.get("record_id"),
        event.get("source_document_id"),
    ):
        text = str(value or "")
        match = _RE_NOTIFICATION.search(text) or _RE_DOC_NOTIFICATION.search(text) or _RE_BARE_NOTIFICATION.search(text)
        if match:
            return _norm_notif(match.group(1), match.group(2))
    return None


def _event_notification_refs(event: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    source_ref = _event_notification_ref(event)
    if source_ref:
        refs.add(source_ref)
    legal_time = event.get("legal_time") or {}
    for match in _RE_DATE_BASIS_NOTIFICATION.finditer(str(legal_time.get("date_basis") or "")):
        refs.add(_norm_notif(match.group(1), match.group(2)))
    return refs


_RE_RULE_FROM_COMPONENT = re.compile(r"/rule/([^/]+)")
_RE_FORM_FROM_COMPONENT = re.compile(r"/(?:forms|form)/([^/]+)")


def _event_target_kind(event: dict[str, Any]) -> str:
    """Classify an event target component_id as rule/form/other."""
    component_id = str((event.get("target") or {}).get("component_id") or "")
    if _RE_RULE_FROM_COMPONENT.search(component_id):
        return "rule"
    if _RE_FORM_FROM_COMPONENT.search(component_id):
        return "form"
    return "other"


def _event_target_label(event: dict[str, Any]) -> str:
    """Return a short label for the event target (rule number, form id, or component tail)."""
    component_id = str((event.get("target") or {}).get("component_id") or "")
    match = _RE_RULE_FROM_COMPONENT.search(component_id)
    if match:
        return f"rule/{match.group(1).lower()}"
    match = _RE_FORM_FROM_COMPONENT.search(component_id)
    if match:
        return f"form/{match.group(1).lower()}"
    return component_id.rsplit("/", 1)[-1] or component_id or "(none)"


def _payload_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    return " ".join(
        str(payload.get(key) or "")
        for key in ("text", "source_text", "content", "heading", "label", "old_text", "new_text", "insert_text")
    )


def _event_mentions_rule(event: dict[str, Any], rule: str) -> bool:
    """True if the event payload text contains a 'rule <rule>' reference."""
    if not rule:
        return False
    pattern = re.compile(rf"\brule\s+{re.escape(rule)}\b", re.IGNORECASE)
    return bool(pattern.search(_payload_text(event)))


def build_notification_linkage_index(
    events_path: Path,
) -> dict[str, dict[str, Any]]:
    """Build a notification_ref -> linkage metadata index across all events.

    For each notification_ref seen in any event source, record:
      - event_ids: list of events sourced from this notification
      - target_rules: sorted list of rule labels targeted (e.g. ['rule/108', 'rule/109'])
      - target_forms: sorted list of form labels targeted
      - target_other: sorted list of other component labels
      - by_target: dict of target_label -> [event_ids] for fine-grained linkage
    """
    index: dict[str, dict[str, Any]] = {}
    for event in read_events(events_path):
        refs = _event_notification_refs(event)
        if not refs:
            continue
        target_label = _event_target_label(event)
        kind = _event_target_kind(event)
        event_id = event.get("event_id")
        for ref in refs:
            slot = index.setdefault(
                ref,
                {
                    "event_ids": [],
                    "target_rules": set(),
                    "target_forms": set(),
                    "target_other": set(),
                    "by_target": defaultdict(list),
                },
            )
            slot["event_ids"].append(event_id)
            slot["by_target"][target_label].append(event_id)
            if kind == "rule":
                slot["target_rules"].add(target_label)
            elif kind == "form":
                slot["target_forms"].add(target_label)
            else:
                slot["target_other"].add(target_label)
    return index


def _linkage_subtype(
    rule: str,
    linkage: dict[str, Any] | None,
    *,
    events_for_ref: list[dict[str, Any]],
) -> str:
    """Classify a source_present_unlinked notification by where it IS linked.

    Subtypes:
      - linked_to_form_only: linked events target only forms (rule-level extraction gap)
      - linked_to_other_rules: linked events target other rules (cross-rule gap)
      - linked_in_text_only: linked events reference this rule in payload text only (extraction gap)
      - linked_to_other_target: linked events target sections/other components
      - not_linked_in_events: notification ref absent from event sources entirely
    """
    if not linkage or not linkage.get("event_ids"):
        return "not_linked_in_events"
    target_rules = linkage.get("target_rules") or set()
    target_forms = linkage.get("target_forms") or set()
    target_other = linkage.get("target_other") or set()
    if any(r == f"rule/{rule}" for r in target_rules):
        return "linked_to_this_rule"
    mentions_this_rule = any(_event_mentions_rule(e, rule) for e in events_for_ref)
    if mentions_this_rule:
        return "linked_in_text_only"
    if target_rules and not target_forms and not target_other:
        return "linked_to_other_rules"
    if target_forms and not target_rules:
        return "linked_to_form_only"
    if target_other and not target_rules and not target_forms:
        return "linked_to_other_target"
    return "linked_to_mixed_targets"


def classify_unlinked_linkage(
    *,
    unlinked_items: list[dict[str, Any]],
    events_path: Path,
) -> dict[str, Any]:
    """Classify each source_present_unlinked notification by its event-ledger linkage.

    Returns a dict with keys:
      - linked_but_undetected: items where the notification_ref appears in events
        (subclassed via linkage_subtype to indicate where the linkage lives)
      - non_material_references: items where the notification_ref appears in no event
      - new_unlinked_count: count of items still genuinely unlinked (non_material)
    Also enriches each input item in-place with linkage_* metadata.
    """
    linkage_index = build_notification_linkage_index(events_path)
    events_for_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in read_events(events_path):
        for ref in _event_notification_refs(event):
            events_for_ref[ref].append(event)

    linked_but_undetected: list[dict[str, Any]] = []
    non_material: list[dict[str, Any]] = []
    for item in unlinked_items:
        rule = str(item.get("rule") or "")
        ref = str(item.get("notification_ref") or "")
        linkage = linkage_index.get(ref)
        events = events_for_ref.get(ref, [])
        subtype = _linkage_subtype(rule, linkage, events_for_ref=events)
        target_rules = sorted(linkage.get("target_rules") or []) if linkage else []
        target_forms = sorted(linkage.get("target_forms") or []) if linkage else []
        target_other = sorted(linkage.get("target_other") or []) if linkage else []
        by_target = {
            tgt: sorted(set(eids))
            for tgt, eids in sorted((linkage or {}).get("by_target", {}).items())
        } if linkage else {}
        enrichment = {
            "linkage_event_count": len(events),
            "linkage_event_ids": sorted({e.get("event_id") for e in events if e.get("event_id")}),
            "linkage_target_rules": target_rules,
            "linkage_target_forms": target_forms,
            "linkage_target_other": target_other,
            "linkage_by_target": by_target,
            "linkage_subtype": subtype,
        }
        item.update(enrichment)
        if events:
            linked_but_undetected.append(item)
        else:
            non_material.append(item)
    return {
        "linked_but_undetected": linked_but_undetected,
        "non_material_references": non_material,
        "new_unlinked_count": len(non_material),
    }


def _portal_notification_class(excerpt: str) -> str:
    """Classify whether a portal citation is a Rules source or an external reference."""
    if _RE_CONTEXTUAL_NOTIFICATION_REFERENCE.search(excerpt or ""):
        return "contextual_reference_notification"
    if _RE_DATE_BASIS_NOTIFICATION_REFERENCE.search(excerpt or ""):
        return "date_basis_notification"
    if _RE_EXTERNAL_NOTIFICATION_CLASS.search(excerpt or ""):
        return "external_reference_notification"
    if _RE_BENEFIT_NOTIFICATION_REFERENCE.search(excerpt or ""):
        return "external_reference_notification"
    return "rules_source_notification"


def _classification_context(text: str, start: int, end: int) -> str:
    """Return the local citation sentence used to classify one notification reference."""
    sentence_start = -1
    search_end = start
    while True:
        candidate = text.rfind(". ", 0, search_end)
        if candidate == -1:
            break
        prefix = text[max(0, candidate - 8): candidate + 1].lower()
        if not re.search(r"(?:\b(?:no|c\.t|s\.r|g\.s\.r|e)\.|w\.e\.f\.)$", prefix):
            sentence_start = candidate
            break
        search_end = candidate
    if sentence_start != -1:
        class_start = sentence_start + 2
    else:
        class_start = max(0, start - 80)
    sentence_end = text.find(". ", end)
    if sentence_end != -1:
        class_end = sentence_end + 1
    else:
        class_end = min(len(text), end + 30)
    comma_before = text.rfind(",", class_start, start)
    if comma_before != -1 and _RE_NOTIFICATION.search(text[class_start:comma_before]):
        class_start = comma_before + 1
    next_notification = _RE_NOTIFICATION.search(text, end)
    if next_notification and next_notification.start() < class_end:
        comma_after = text.find(",", end, next_notification.start())
        if comma_after != -1:
            class_end = comma_after + 1
    return text[class_start:class_end]


def extract_portal_rule_notifications(html_dir: Path, *, top_rules: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Extract notification references from TaxInformation/CBIC rule HTML pages."""
    wanted = {rule.lower() for rule in top_rules or []}
    by_rule: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    if not html_dir.exists():
        return {}
    for path in sorted(html_dir.glob("*.html")):
        rule = _extract_rule_number(path)
        if not rule or (wanted and rule not in wanted):
            continue
        text = _clean_html_text(path.read_text(encoding="utf-8", errors="ignore"))
        for match in _RE_NOTIFICATION.finditer(text):
            ref = _norm_notif(match.group(1), match.group(2))
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 120)
            class_context = _classification_context(text, match.start(), match.end())
            by_rule[rule].setdefault(
                ref,
                {
                    "notification_ref": ref,
                    "notification_class": _portal_notification_class(class_context),
                    "html_path": str(path),
                    "excerpt": text[start:end],
                },
            )
    return {rule: sorted(rows.values(), key=lambda row: row["notification_ref"]) for rule, rows in sorted(by_rule.items())}


def _gap_classification_from_linkage_subtype(linkage_subtype: str) -> str:
    """Map a source_present_unlinked notification's linkage_subtype to a gap classification.

    - contextual_reference: the notification IS referenced by events that touch this rule
      (cross-rule amendments whose text mentions this rule, or events with mixed targets).
    - cross_rule_reference: the notification is material in the event ledger but only for
      other components (other rules, forms, sections) -- not this rule.
    - true_unlinked: the notification_ref is absent from the event ledger entirely.
    """
    if linkage_subtype in {"linked_in_text_only", "linked_to_mixed_targets"}:
        return "contextual_reference"
    if linkage_subtype in {
        "linked_to_other_rules",
        "linked_to_form_only",
        "linked_to_other_target",
    }:
        return "cross_rule_reference"
    return "true_unlinked"


def build_portal_completeness_report(
    *,
    html_dir: Path,
    events_path: Path,
    output: Path | None = None,
    top_rules: list[str] | None = None,
) -> dict[str, Any]:
    portal_by_rule = extract_portal_rule_notifications(html_dir, top_rules=top_rules)
    event_refs_by_rule: dict[str, set[str]] = defaultdict(set)
    all_event_refs: set[str] = set()
    for event in read_events(events_path):
        event_refs = _event_notification_refs(event)
        all_event_refs.update(event_refs)
        component_id = str((event.get("target") or {}).get("component_id") or "")
        match = re.search(r"/rule/([^/]+)", component_id)
        if not match:
            continue
        for ref in event_refs:
            event_refs_by_rule[match.group(1).lower()].add(ref)

    rules: dict[str, Any] = {}
    missing: list[dict[str, Any]] = []
    source_present_unlinked: list[dict[str, Any]] = []
    external_reference_count = 0
    for rule, portal_refs in portal_by_rule.items():
        event_refs = event_refs_by_rule.get(rule, set())
        rule_missing = []
        normalized_portal_refs: list[dict[str, Any]] = []
        rule_external_count = 0
        for item in portal_refs:
            notification_class = item.get("notification_class") or _portal_notification_class(item.get("excerpt", ""))
            notification_ref = item["notification_ref"]
            corrected_ref, original_ref = _correct_portal_ref_from_excerpt(
                notification_ref,
                item.get("excerpt", ""),
                all_event_refs,
            )
            if original_ref:
                item = {**item, "notification_ref": corrected_ref, "portal_notification_ref": original_ref}
                notification_ref = corrected_ref
            if notification_class != "rules_source_notification":
                rule_external_count += 1
                normalized_portal_refs.append(item)
                continue
            normalized_portal_refs.append(item)
            if notification_ref in event_refs:
                continue
            gap = {
                "rule": rule,
                "component_id": f"/in/union/rules/cgst-rules-2017/rule/{rule}",
                "notification_ref": notification_ref,
                "notification_class": notification_class,
                "html_path": item["html_path"],
                "excerpt": item["excerpt"],
            }
            if original_ref:
                gap["portal_notification_ref"] = original_ref
                gap["classification_note"] = "portal_notification_year_corrected_from_dated_year"
            if notification_ref in all_event_refs:
                gap["classification"] = "source_present_unlinked_notification"
                rule_missing.append(gap)
                source_present_unlinked.append(gap)
                continue
            gap["classification"] = "missing_source_notification"
            rule_missing.append(gap)
            missing.append(gap)
        external_reference_count += rule_external_count
        rules[rule] = {
            "portal_notification_refs": normalized_portal_refs,
            "event_notification_refs": sorted(event_refs),
            "missing_source_notifications": rule_missing,
            "portal_completeness_status": "incomplete" if rule_missing else "complete",
            "external_reference_notification_count": rule_external_count,
        }

    report = {
        "html_dir": str(html_dir),
        "events_path": str(events_path),
        "rule_count": len(rules),
        "missing_source_notification_count": len(missing),
        "source_present_unlinked_notification_count": len(source_present_unlinked),
        "external_reference_notification_count": external_reference_count,
        "rules": rules,
        "missing_source_notifications": missing,
        "source_present_unlinked_notifications": source_present_unlinked,
    }
    linkage = classify_unlinked_linkage(
        unlinked_items=source_present_unlinked,
        events_path=events_path,
    )
    subtype_counts: dict[str, int] = defaultdict(int)
    gap_classification_counts: dict[str, int] = defaultdict(int)
    for item in source_present_unlinked:
        subtype = item.get("linkage_subtype") or "not_linked_in_events"
        subtype_counts[subtype] += 1
        gap_classification = _gap_classification_from_linkage_subtype(subtype)
        item["gap_classification"] = gap_classification
        gap_classification_counts[gap_classification] += 1
    report["source_present_unlinked_linkage"] = {
        "linked_but_undetected_count": len(linkage["linked_but_undetected"]),
        "non_material_reference_count": len(linkage["non_material_references"]),
        "new_unlinked_count": linkage["new_unlinked_count"],
        "linkage_subtype_counts": dict(sorted(subtype_counts.items())),
        "gap_classification_counts": dict(sorted(gap_classification_counts.items())),
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return report


def _gap_rule_key(gap: dict[str, Any]) -> str | None:
    for value in (
        (gap.get("target") or {}).get("component_id") if isinstance(gap.get("target"), dict) else gap.get("target"),
        gap.get("component_id"),
        gap.get("target_component_id"),
    ):
        match = re.search(r"/rule/([^/]+)", str(value or ""))
        if match:
            return f"rule/{match.group(1).lower()}"
    return None


def _gap_lane(gap: dict[str, Any]) -> str:
    reason = str(gap.get("skip_reason") or gap.get("reason") or "")
    operation = str(gap.get("operation") or "").upper()
    message = f"{reason} {gap.get('message') or ''}".lower()
    target = str((gap.get("target") or {}).get("component_id") if isinstance(gap.get("target"), dict) else gap.get("target") or "")
    if "inserted_component_already_exists" in message:
        return "duplicate_insert"
    if "anchor" in message or "not found" in message:
        return "anchor_normalization"
    if "target" in message and ("missing" in message or "not found" in message or "unresolved" in message):
        return "target_creation"
    if "/form" in target or "form" in message or "statement" in message or "table" in message:
        return "form_statement"
    if operation in {"SUBSTITUTE", "SPLICE"} and ("prior" in message or "sequence" in message):
        return "sequencing_chain"
    return "event_resolution"


def rebuild_top10_gap_report(
    *,
    coverage_gaps_path: Path,
    output: Path,
    top_n: int = 10,
) -> dict[str, Any]:
    """Rebuild the top gap report from the current Rules coverage gaps."""
    payload = json.loads(coverage_gaps_path.read_text(encoding="utf-8"))
    gaps = payload.get("gaps", payload) if isinstance(payload, dict) else payload
    if not isinstance(gaps, list):
        gaps = []

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        key = _gap_rule_key(gap)
        if key:
            grouped[key].append(gap)

    ranked = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:top_n]
    rules: dict[str, Any] = {}
    for key, rule_gaps in ranked:
        lane_counts: dict[str, int] = defaultdict(int)
        rendered = []
        for gap in rule_gaps:
            lane = _gap_lane(gap)
            lane_counts[lane] += 1
            target = gap.get("target")
            if isinstance(target, dict):
                target_value = target.get("component_id") or target.get("anchor_component_id")
            else:
                target_value = target
            rendered.append(
                {
                    "event_id": gap.get("event_id"),
                    "operation": gap.get("operation"),
                    "status": gap.get("status", "coverage_gap"),
                    "target": target_value,
                    "source_notification": (gap.get("source") or {}).get("instrument_number") if isinstance(gap.get("source"), dict) else None,
                    "skip_reason": gap.get("skip_reason") or gap.get("reason") or gap.get("message"),
                    "lane": lane,
                }
            )
        rules[key] = {
            "gap_count": len(rule_gaps),
            "lane_counts": dict(sorted(lane_counts.items())),
            "gaps": rendered,
        }

    report = {
        "summary": {
            "coverage_gaps_path": str(coverage_gaps_path),
            "total_gap_count": len(gaps),
            "reported_rule_count": len(rules),
        },
        "target_rules": list(rules),
        "rules": rules,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"updated": True, "top10_gap_report": str(output), **report["summary"]}


def annotate_top10_gap_report(*, top10_path: Path, portal_report_path: Path) -> dict[str, Any]:
    if not top10_path.exists() or not portal_report_path.exists():
        return {"updated": False, "reason": "input_missing"}
    report = json.loads(top10_path.read_text(encoding="utf-8"))
    portal = json.loads(portal_report_path.read_text(encoding="utf-8"))
    rules = report.setdefault("rules", {})
    for row in rules.values():
        gaps = row.get("gaps") or []
        stale_count = sum(1 for gap in gaps if gap.get("lane") == "portal_completeness")
        if not stale_count:
            continue
        row["gaps"] = [gap for gap in gaps if gap.get("lane") != "portal_completeness"]
        row["gap_count"] = max(0, int(row.get("gap_count", 0)) - stale_count)
        lane_counts = row.setdefault("lane_counts", {})
        lane_counts["portal_completeness"] = max(0, int(lane_counts.get("portal_completeness", 0)) - stale_count)
    added = 0
    for item in portal.get("missing_source_notifications", []):
        rule = str(item.get("rule") or "").lower()
        key = f"rule/{rule}"
        row = rules.setdefault(key, {"gap_count": 0, "gaps": [], "lane_counts": {}})
        gaps = row.setdefault("gaps", [])
        marker = f"portal_missing::{item.get('notification_ref')}"
        if any(gap.get("event_id") == marker for gap in gaps):
            continue
        gaps.append(
            {
                "event_id": marker,
                "operation": "PORTAL_COMPLETENESS",
                "status": "coverage_gap",
                "target": item.get("component_id"),
                "source_notification": item.get("notification_ref"),
                "skip_reason": "missing_source_notification",
                "lane": "portal_completeness",
                "excerpt": item.get("excerpt", ""),
            }
        )
        row["gap_count"] = int(row.get("gap_count", 0)) + 1
        lane_counts = row.setdefault("lane_counts", {})
        lane_counts["portal_completeness"] = int(lane_counts.get("portal_completeness", 0)) + 1
        added += 1
    report["portal_completeness_report"] = str(portal_report_path)
    report["portal_missing_notification_blockers_added"] = added
    top10_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"updated": True, "added": added, "top10_gap_report": str(top10_path)}


__all__ = [
    "annotate_top10_gap_report",
    "build_notification_linkage_index",
    "build_portal_completeness_report",
    "classify_unlinked_linkage",
    "extract_portal_rule_notifications",
    "rebuild_top10_gap_report",
]
