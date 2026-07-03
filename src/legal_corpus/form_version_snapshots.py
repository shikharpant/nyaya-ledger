"""Materialize a version-history lane for GST forms."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from .amendment_events import read_events, sha256_text
from .version_snapshots import MATERIALIZER_VERSION

_log = logging.getLogger(__name__)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_text(path: Path) -> str:
    root = ET.parse(path).getroot()
    lines: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) not in {"heading", "p", "num"}:
            continue
        text = _clean("".join(element.itertext()))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _date_key(event: dict[str, Any]) -> str:
    return (
        event.get("legal_time", {}).get("applicability_start")
        or event.get("legal_time", {}).get("commencement_date")
        or event.get("source", {}).get("publication_date")
        or ""
    )


def _gap(event: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "operation": event.get("operation"),
        "status": event.get("status"),
        "date": _date_key(event),
        "target": event.get("target", {}),
        "source_document_id": event.get("source", {}).get("document_id"),
        "source_record_id": event.get("source", {}).get("record_id"),
        "source_span": event.get("evidence", {}).get("source_span", {}),
        "excerpt": event.get("evidence", {}).get("excerpt", ""),
        "review_reasons": event.get("review", {}).get("review_reasons", []),
        "skip_reason": reason,
    }


_SLUG_ALIASES = {
    "gst-tran-1": "gst-tran-01",
    "gst-tran-2": "gst-tran-02",
    "gst-gstr9": "gstr-9",
    "gst-gstr-9": "gstr-9",
    "gst-gstr1": "gstr-1",
    "gst-gstr-1": "gstr-1",
    "gstr01": "gstr-1",
    "gstr-01": "gstr-1",
}


def _canonical_form_slug(form_id: str) -> str:
    raw = form_id.lower().strip()
    raw = re.sub(r"\s*-\s*", "-", raw)
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"-+", "-", raw)
    raw = raw.strip("-")
    while raw.startswith(("form-gst-", "gst-form-", "formgst-")):
        if raw.startswith("form-gst-"):
            raw = raw[len("form-"):]
        elif raw.startswith("gst-form-"):
            raw = raw[len("gst-form-"):]
        elif raw.startswith("formgst-"):
            raw = f"gst-{raw[len('formgst-'):]}"
        raw = raw.strip("-")
    if raw.startswith("form-gstr-"):
        raw = raw[len("form-"):]
    elif raw.startswith("form-") and raw[5:].startswith(("gst-", "gstr-")):
        raw = raw[5:]
    if raw in _SLUG_ALIASES:
        return _SLUG_ALIASES[raw]
    compact_gstr = re.fullmatch(r"(?:gst-)?gstr-?0*([1-9]\d?)([a-z]?)", raw)
    if compact_gstr:
        suffix = compact_gstr.group(2)
        return f"gstr-{int(compact_gstr.group(1))}{suffix}"
    compact_gst = re.fullmatch(r"gst-([a-z]+)-?0*([1-9]\d?)([a-z]?)", raw)
    if compact_gst:
        prefix, number, suffix = compact_gst.groups()
        if prefix in {"rfd", "drc", "tran", "reg", "itc", "pmt", "apl", "cmp", "adt", "ewb", "inv", "pct", "enr", "ins", "mov", "asmt", "ara", "cpd", "spl"}:
            return f"gst-{prefix}-{int(number):02d}{suffix}"
    if raw.startswith("gst-"):
        raw = raw[4:]
    if raw.startswith("gstr-"):
        return raw
    slug = f"gst-{raw}"
    return _SLUG_ALIASES.get(slug, slug)


def _form_slug_from_component(component_id: str) -> str:
    raw = str(component_id or "").strip("/").split("/")[-1].lower()
    return _canonical_form_slug(raw)


@lru_cache(maxsize=8)
def _corpus_form_slug_map(corpus_dir_str: str) -> dict[str, str]:
    forms_dir = Path(corpus_dir_str) / "in/union/forms"
    slug_map: dict[str, str] = {}
    if not forms_dir.exists():
        return slug_map
    for form_xml in forms_dir.glob("*/form.xml"):
        raw = form_xml.parent.name
        canonical = _canonical_form_slug(raw)
        slug_map.setdefault(canonical, raw)
        slug_map.setdefault(raw, raw)
    return slug_map


def _corpus_backed_slugs(corpus_dir: Path) -> set[str]:
    return {
        slug
        for slug in _corpus_form_slug_map(str(corpus_dir)).keys()
        if slug == _canonical_form_slug(slug)
    }


def _form_path(corpus_dir: Path, form_slug: str) -> Path:
    slug_map = _corpus_form_slug_map(str(corpus_dir))
    raw_slug = slug_map.get(form_slug, form_slug)
    return corpus_dir / "in/union/forms" / raw_slug / "form.xml"


def _load_form_registry(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {"forms": {}, "pending_baseline_slugs": set(), "ready_slugs": set()}
    payload = json.loads(path.read_text(encoding="utf-8"))
    forms: dict[str, dict[str, Any]] = {}
    pending: set[str] = set()
    ready: set[str] = set()
    for row in payload.get("forms") or []:
        if not isinstance(row, dict):
            continue
        form_id = str(row.get("form_id") or "").strip().lower()
        if not form_id:
            continue
        slug = _canonical_form_slug(form_id)
        status = str(row.get("baseline_status") or "").strip()
        normalized = {**row, "form_slug": slug}
        forms[slug] = normalized
        if status == "structured_baseline_available":
            ready.add(slug)
        else:
            pending.add(slug)
    return {
        "version": payload.get("version"),
        "forms": forms,
        "pending_baseline_slugs": pending,
        "ready_slugs": ready,
        "default_lane": payload.get("default_lane", "forms_lane_pending_baseline"),
    }


_RE_SUBSTITUTE_CONTEXT = re.compile(
    r"for\s+(?:FORM\s+)?GST\s+[\w-]+"
    r"(?:\s*,?\s*(?:and)?\s*(?:FORM\s+)?GST\s+[\w-]+)*"
    r".*?shall\s+be\s+substituted",
    re.IGNORECASE | re.DOTALL,
)
_RE_FORM_IDS = re.compile(r"GST\s+([\w-]+)", re.IGNORECASE)
_RE_INSERT_CONTEXT = re.compile(
    r"(?:after|before)\s+(?:FORM\s+)?GST\s+([\w-]+).*?(?:shall\s+be\s+)?inserted",
    re.IGNORECASE | re.DOTALL,
)
_RE_FORM_MENTION = re.compile(
    r"\bFORM\s+GST\s+([A-Z0-9]+(?:\s*-\s*[A-Z0-9]+)*)\b",
    re.IGNORECASE,
)
_RE_GSTR_MENTION = re.compile(r"\bFORM\s+(GSTR\s*-?\s*\d+[A-Z]?)\b", re.IGNORECASE)
_RE_STANDALONE_GSTR_MENTION = re.compile(r"\b(GSTR\s*-?\s*\d+[A-Z]?)\b", re.IGNORECASE)
_RE_KNOWN_GST_FORM_CODE = re.compile(
    r"\bGST\s+((?:RFD|DRC|TRAN|REG|ITC|PMT|APL|CMP|ADT|EWB|INV|PCT|ENR|INS|MOV)\s*-?\s*\d+[A-Z]?)\b",
    re.IGNORECASE,
)
_KNOWN_GST_FORM_SLUG = re.compile(
    r"^gst-(?:rfd|drc|tran|reg|itc|pmt|apl|cmp|adt|ewb|inv|pct|enr|ins|mov|asmt|ara|cpd|spl)-\d+[a-z]?$"
)
_RE_IN_FORM_AMEND = re.compile(
    r"in\s+(?:Form\s+|FORM\s+)?GST\s+([A-Z0-9-]+)"
    r".*?(?:for\s+(?:the\s+)?(?:words?|figures?|letters?|heading|serial\s+number|word).*?"
    r"(?:shall\s+be\s+substituted|shall\s+be\s+omitted))",
    re.IGNORECASE | re.DOTALL,
)
_RE_FORM_FROM_EXCERPT = re.compile(
    r"\bFORM\s+(?:GST\s+)?([A-Z]+-?\d+[A-Z]?)\b",
    re.IGNORECASE,
)
_RE_QUOTED_PAIRS = re.compile(
    r"[\u201c\u2018\"']([^\u201d\u2019\"']{1,300})[\u201d\u2019\"']"
    r"[^\u201c\u2018\"']{0,80}?"
    r"[\u201c\u2018\"']([^\u201d\u2019\"']{1,300})[\u201d\u2019\"']"
    r"\s*(?:shall\s+be\s+)?substituted",
    re.IGNORECASE | re.DOTALL,
)

_NOTIF_CACHE: dict[str, str] = {}


def _notification_text(record_id: str, notif_dir: Path) -> str:
    """Load and cache the full text of a notification PDF."""
    if record_id in _NOTIF_CACHE:
        return _NOTIF_CACHE[record_id]
    for f in notif_dir.iterdir():
        if record_id and record_id in f.name:
            try:
                with open(f, encoding="utf-8") as fh:
                    n = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            pdf_b64 = n.get("contentPdfBase64") or ""
            if not pdf_b64:
                _NOTIF_CACHE[record_id] = ""
                return ""
            try:
                import fitz

                pdf_bytes = base64.b64decode(pdf_b64)
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                text = "".join(page.get_text() for page in doc)
                doc.close()
            except Exception:
                text = ""
            _NOTIF_CACHE[record_id] = text
            return text
    _NOTIF_CACHE[record_id] = ""
    return ""


_RE_NAMELY_SHEET = re.compile(
    r"(?:for\s+FORM\s+GST\s+|for\s+Form\s+GST\s+)"
    r"([\w-]+)"
    r"(?:.*?)"
    r"shall\s+be\s+substituted\s*,?\s*"
    r"namely\s*:\s*[-\u2013\u2014\u2018\u201c\"'\s]*",
    re.IGNORECASE | re.DOTALL,
)
_RE_NAMELY_INSERT = re.compile(
    r"(?:after|before)\s+(?:FORM\s+)?GST\s+([\w-]+)"
    r"(?:.*?)"
    r"(?:shall\s+be\s+)?inserted\s*,?\s*"
    r"namely\s*:\s*[-\u2013\u2014\u2018\u201c\"'\s]*",
    re.IGNORECASE | re.DOTALL,
)

_RE_CLAUSE_BOUNDARY = re.compile(
    r'(?:"\s*;|\u201d\s*;|"\s*\n\s*\d+\s*\.\s|\u201d\s*\n\s*\d+\s*\.\s)'
)


def _extract_form_text_from_notif(
    notif_text: str,
    form_slug_upper: str,
    operation: str,
) -> str:
    """Extract full form text from a notification after a 'namely:' clause."""
    if not notif_text:
        return ""
    patterns = [_RE_NAMELY_SHEET, _RE_NAMELY_INSERT] if operation == "SUBSTITUTE" else [_RE_NAMELY_INSERT]
    for pattern in patterns:
        for m in pattern.finditer(notif_text):
            matched_slug = m.group(1).upper().strip()
            if matched_slug != form_slug_upper:
                continue
            start = m.end()
            remainder = notif_text[start:]
            boundary = _RE_CLAUSE_BOUNDARY.search(remainder)
            if boundary:
                raw = remainder[: boundary.start()]
            else:
                raw = remainder[:5000]
            raw = raw.strip().rstrip('";\u201d')
            if len(raw) > 20:
                return _clean(raw)
    return ""


_RE_WORD_SUB = re.compile(
    r'for\s+(?:the\s+)?(?:words?|figures?|letters?|brackets?,?\s*(?:words?\s+)?(?:and\s+)?(?:figures?)?)\s*'
    r'[\u201c\u2018"\'](.*?)[\u201d\u2019"\']'
    r'\s*,?\s*(?:the\s+)?(?:words?|figures?|letters?|brackets?,?\s*(?:words?\s+)?(?:and\s+)?(?:figures?)?)\s*'
    r'[\u201c\u2018"\'](.*?)[\u201d\u2019"\']\s*shall\s+be\s+substituted',
    re.IGNORECASE | re.DOTALL,
)
_RE_WORD_OMIT = re.compile(
    r'(?:the\s+)?(?:words?|figures?|letters?)\s*'
    r'[\u201c\u2018"\'](.*?)[\u201d\u2019"\']\s*shall\s+be\s+omitted',
    re.IGNORECASE | re.DOTALL,
)
_RE_STATEMENT_LABEL = re.compile(r"\bStatement\s*-?\s*([0-9]+[A-Z]?)\b", re.IGNORECASE)
_RE_DECLARATION = re.compile(r"\bDECLARATION\b", re.IGNORECASE)
_RE_RFD01 = re.compile(r"\b(?:FORM\s+)?GST\s+RFD\s*-?\s*0?1\b", re.IGNORECASE)


def _statement_component_id(label: str) -> str:
    cleaned = label.lower().replace(" ", "")
    return f"/in/union/forms/gst-rfd-01/statement/{cleaned}"


def _statement_label_from_text(text: str) -> str | None:
    match = _RE_STATEMENT_LABEL.search(text or "")
    if match:
        return match.group(1).lower()
    if _RE_DECLARATION.search(text or ""):
        return "declaration"
    return None


def _is_rfd01_statement_event(event: dict[str, Any]) -> bool:
    target = str((event.get("target") or {}).get("component_id") or "")
    if target.startswith("/in/union/forms/gst-rfd-01/statement/"):
        return True
    payload = event.get("payload") or {}
    evidence = event.get("evidence") or {}
    probe = " ".join(
        str(payload.get(key) or "")
        for key in ("old_text", "new_text", "content", "structural_text", "text")
    )
    probe = f"{probe} {evidence.get('excerpt', '')}"
    return bool(_statement_label_from_text(probe) and (_RE_RFD01.search(probe) or "rule 89" in probe.lower()))


def _event_form_slugs(event: dict[str, Any]) -> list[str]:
    slugs: set[str] = set()
    target = str((event.get("target") or {}).get("component_id") or "")
    if target.startswith("/in/union/forms/"):
        slugs.add(_form_slug_from_component(target))

    payload = event.get("payload") or {}
    probe = " ".join(
        str(value or "")
        for value in (
            (event.get("evidence") or {}).get("excerpt"),
            payload.get("old_text"),
            payload.get("new_text"),
            payload.get("content"),
            payload.get("structural_text"),
            payload.get("text"),
        )
    )
    for match in _RE_FORM_MENTION.finditer(probe):
        slug = _canonical_form_slug(match.group(1))
        if slug.startswith("gstr-") or _KNOWN_GST_FORM_SLUG.fullmatch(slug):
            slugs.add(slug)
    for match in _RE_GSTR_MENTION.finditer(probe):
        slugs.add(_canonical_form_slug(match.group(1)))
    for match in _RE_STANDALONE_GSTR_MENTION.finditer(probe):
        slugs.add(_canonical_form_slug(match.group(1)))
    for match in _RE_KNOWN_GST_FORM_CODE.finditer(probe):
        slugs.add(_canonical_form_slug(match.group(1)))
    return sorted(slugs)


def _pending_unclassified_bucket(event: dict[str, Any]) -> str:
    target = str((event.get("target") or {}).get("component_id") or "")
    payload = event.get("payload") or {}
    probe = " ".join(
        str(value or "")
        for value in (
            (event.get("evidence") or {}).get("excerpt"),
            payload.get("old_text"),
            payload.get("new_text"),
            payload.get("content"),
            payload.get("structural_text"),
            payload.get("text"),
        )
    )
    has_rule_target = target.startswith("/in/union/rules/")
    op = str(event.get("operation") or "")
    lower_probe = probe.lower()
    if op == "CORRIGENDUM" or "corrigendum" in lower_probe:
        return "form_corrigendum_classified"
    if has_rule_target and re.search(r"\bin\s+rule\s+\d+|for\s+rule\s+\d+|sub-rule\s*\(", probe, re.IGNORECASE):
        return "form_rules_overroute_classified"
    if has_rule_target and re.search(
        r"\btable\s+\d+|Table No\.|Sr\.|formula|Refund Amount|Adjusted Total Turnover|Net ITC|"
        r"GSTIN|invoice details|taxable supplies|integrated|central tax|state tax|cess|tax period|serial number",
        probe,
        re.IGNORECASE,
    ):
        return "rules_table_or_formula_reference"
    if has_rule_target and re.search(
        r"\bsection\s+\d+|clause\s+\([a-z]+\)|proviso|wherever they occur|come into force|"
        r"shall be deemed|notification shall",
        probe,
        re.IGNORECASE,
    ):
        return "form_rules_overroute_classified"
    if re.search(
        r"\bfurnish(?:ed)?\b|\bfile\b|\bauto-populated\b|\bdetails furnished\b|\bto be claimed\b|"
        r"\bshall be availed\b|\bapplication for\b",
        probe,
        re.IGNORECASE,
    ) and not re.search(r"shall\s+be\s+(?:substituted|inserted|omitted)|for\s+the\s+(?:words|figures|letters)", probe, re.IGNORECASE):
        return "form_reference_only"
    if op == "UNKNOWN" and re.search(
        r"\b(?:Part|Table|Statement|Instructions?|DECLARATION)\b|GSTIN|invoice details|taxable supplies|"
        r"integrated|central tax|state tax|cess|tax period|place of supply|rate\s+taxable",
        probe,
        re.IGNORECASE,
    ):
        return "form_baseline_fragment"
    if re.search(r"\bFORM\b|\bGSTR\b|\bGST\s+[A-Z]{2,}", probe, re.IGNORECASE):
        return "form_slug_unresolved"
    return "form_baseline_fragment" if str(event.get("operation") or "") == "UNKNOWN" else "form_reference_only"


_NON_GAP_FORM_BUCKETS = {
    "form_baseline_fragment",
    "form_corrigendum_classified",
    "form_reference_only",
    "form_rules_overroute_classified",
    "rules_table_or_formula_reference",
}


def _classified_non_gap_lane(event: dict[str, Any]) -> str:
    bucket = _pending_unclassified_bucket(event)
    if bucket in _NON_GAP_FORM_BUCKETS:
        return bucket
    op = str(event.get("operation") or "")
    target = str((event.get("target") or {}).get("component_id") or "")
    if op == "CORRIGENDUM":
        return "form_corrigendum_classified"
    if _event_form_slugs(event):
        return "form_baseline_fragment" if op == "UNKNOWN" else "form_reference_only"
    if target.startswith("/in/union/rules/") and op in {
        "SUBSTITUTE",
        "INSERT_SIBLING",
        "INSERT_CHILD",
        "OMIT",
        "SPLICE",
        "RESCIND",
        "COMMENCE",
        "EXTRACT",
        "AMEND",
    }:
        return "form_rules_overroute_classified"
    return "form_baseline_fragment" if op == "UNKNOWN" else "form_reference_only"


def _summarize_pending_baseline_gaps(
    pending_baseline_gaps: list[dict[str, Any]],
    event_by_id: dict[str, dict[str, Any]],
    form_registry: dict[str, Any],
) -> dict[str, Any]:
    registry_forms: dict[str, dict[str, Any]] = form_registry.get("forms") or {}
    by_form: dict[str, dict[str, Any]] = {}
    unclassified: list[str] = []
    non_overroute_unclassified: list[str] = []
    unclassified_by_bucket: dict[str, dict[str, Any]] = {}
    overroute_buckets = {"form_rules_overroute_classified", "rules_table_or_formula_reference"}
    overrouted_event_ids: list[str] = []
    classified_by_lane: dict[str, list[str]] = {bucket: [] for bucket in sorted(_NON_GAP_FORM_BUCKETS)}
    unresolved_pending_event_ids: list[str] = []
    for gap in pending_baseline_gaps:
        event_id = str(gap.get("event_id") or "")
        event = event_by_id.get(event_id, {})
        slugs = _event_form_slugs(event) if event else []
        if not slugs:
            bucket = _pending_unclassified_bucket(event) if event else "unclassified"
            if bucket in overroute_buckets:
                overrouted_event_ids.append(event_id)
            if bucket in classified_by_lane:
                classified_by_lane[bucket].append(event_id)
            else:
                unclassified.append(event_id)
                non_overroute_unclassified.append(event_id)
                unresolved_pending_event_ids.append(event_id)
            bucket_row = unclassified_by_bucket.setdefault(bucket, {"count": 0, "sample_event_ids": []})
            bucket_row["count"] += 1
            if len(bucket_row["sample_event_ids"]) < 25:
                bucket_row["sample_event_ids"].append(event_id)
            continue
        for slug in slugs:
            registry_row = registry_forms.get(slug, {})
            row = by_form.setdefault(
                slug,
                {
                    "form_slug": slug,
                    "count": 0,
                    "sample_event_ids": [],
                    "baseline_status": registry_row.get("baseline_status", "unregistered"),
                    "priority": registry_row.get("priority"),
                },
            )
            row["count"] += 1
            if len(row["sample_event_ids"]) < 10:
                row["sample_event_ids"].append(event_id)

    sorted_rows = {
        slug: by_form[slug]
        for slug in sorted(
            by_form,
            key=lambda item: (
                by_form[item].get("priority") is None,
                by_form[item].get("priority") or 9999,
                -int(by_form[item].get("count") or 0),
                item,
            ),
        )
    }
    top_priority_rows = {
        slug: row
        for slug, row in sorted_rows.items()
        if row.get("priority") in {1, 2, 3, 4, 5}
    }
    return {
        "forms_lane_pending_baseline_by_form": sorted_rows,
        "forms_lane_pending_baseline_top_priority": top_priority_rows,
        "forms_lane_pending_baseline_unclassified_count": len(unclassified),
        "forms_lane_pending_baseline_unclassified_event_ids": unclassified,
        "forms_lane_pending_baseline_non_overroute_unclassified_event_ids": non_overroute_unclassified,
        "forms_lane_pending_baseline_unclassified_by_bucket": unclassified_by_bucket,
        "forms_lane_overrouted_count": len(overrouted_event_ids),
        "forms_lane_overrouted_event_ids": overrouted_event_ids,
        "form_baseline_fragment_count": len(classified_by_lane["form_baseline_fragment"]),
        "form_baseline_fragment_event_ids": classified_by_lane["form_baseline_fragment"],
        "form_corrigendum_classified_count": len(classified_by_lane["form_corrigendum_classified"]),
        "form_corrigendum_classified_event_ids": classified_by_lane["form_corrigendum_classified"],
        "form_reference_only_count": len(classified_by_lane["form_reference_only"]),
        "form_reference_only_event_ids": classified_by_lane["form_reference_only"],
        "form_rules_overroute_classified_count": len(classified_by_lane["form_rules_overroute_classified"]),
        "form_rules_overroute_classified_event_ids": classified_by_lane["form_rules_overroute_classified"],
        "rules_table_or_formula_reference_count": len(classified_by_lane["rules_table_or_formula_reference"]),
        "rules_table_or_formula_reference_event_ids": classified_by_lane["rules_table_or_formula_reference"],
        "forms_lane_true_pending_baseline_count": len(unresolved_pending_event_ids),
        "forms_lane_true_pending_baseline_event_ids": unresolved_pending_event_ids,
        "forms_lane_pending_baseline_registered_count": sum(
            1 for gap in pending_baseline_gaps
            if any((registry_forms.get(slug) or {}).get("baseline_status") for slug in _event_form_slugs(event_by_id.get(str(gap.get("event_id") or ""), {})))
        ),
        "forms_lane_pending_baseline_unregistered_count": sum(
            1 for gap in pending_baseline_gaps
            if _event_form_slugs(event_by_id.get(str(gap.get("event_id") or ""), {}))
            and not any((registry_forms.get(slug) or {}).get("baseline_status") for slug in _event_form_slugs(event_by_id.get(str(gap.get("event_id") or ""), {})))
        ),
    }


def _statement_operation(event: dict[str, Any]) -> str:
    op = event.get("operation")
    if op in {"OMIT", "STATEMENT_OMIT"}:
        return "STATEMENT_OMIT"
    if op in {"SPLICE", "STATEMENT_INSERT"}:
        return "STATEMENT_INSERT"
    if op == "STATEMENT_TEXT_SUBSTITUTE":
        return "STATEMENT_TEXT_SUBSTITUTE"
    return "STATEMENT_SUBSTITUTE"


def _extract_statement_text_from_event(event: dict[str, Any], previous_text: str) -> tuple[str, str | None]:
    payload = event.get("payload") or {}
    evidence = event.get("evidence") or {}
    op = _statement_operation(event)
    if op == "STATEMENT_OMIT":
        return "[Omitted]", "statement_omit"
    new_text = _clean(str(payload.get("new_text") or payload.get("content") or payload.get("structural_text") or ""))
    if new_text:
        return new_text, "payload_text"
    if previous_text:
        substituted = _apply_excerpt_substitutions(previous_text, str(evidence.get("excerpt") or ""))
        if substituted != previous_text:
            return substituted, "statement_text_substitute"
    excerpt = _clean(str(evidence.get("excerpt") or ""))
    if excerpt:
        return excerpt, "excerpt_text"
    return previous_text, "carry_forward"


def _materialize_rfd01_statement_versions(
    events: list[dict[str, Any]],
    *,
    base_as_of: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    statement_events = [event for event in events if _is_rfd01_statement_event(event)]
    by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in statement_events:
        target = str((event.get("target") or {}).get("component_id") or "")
        label = target.rsplit("/statement/", 1)[-1] if "/statement/" in target else None
        if not label:
            probe = " ".join(
                [
                    str((event.get("payload") or {}).get("old_text") or ""),
                    str((event.get("payload") or {}).get("new_text") or ""),
                    str((event.get("evidence") or {}).get("excerpt") or ""),
                ]
            )
            label = _statement_label_from_text(probe)
        if not label:
            continue
        by_component[_statement_component_id(label)].append(event)

    versions: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    applied_ids: set[str] = set()
    for component_id, rows in sorted(by_component.items()):
        current_text = ""
        chain: list[str] = []
        versions.append(
            {
                "version_id": sha256_text("|".join([component_id, base_as_of, ""]))[:24],
                "work_id": "/in/union/forms/gst-rfd-01",
                "component_id": component_id,
                "valid_from": base_as_of,
                "valid_to": None,
                "applicability_start": base_as_of,
                "applicability_end": None,
                "text": "",
                "text_sha256": None,
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {"type": "rfd01_statement_baseline", "base_as_of": base_as_of},
            }
        )
        last_index = len(versions) - 1
        for event in sorted(rows, key=lambda item: (_date_key(item), (item.get("evidence") or {}).get("source_span", {}).get("start", 0))):
            date = _date_key(event)
            if not date or date <= base_as_of:
                continue
            versions[last_index]["valid_to"] = date
            versions[last_index]["applicability_end"] = date
            text, extraction_method = _extract_statement_text_from_event(event, current_text)
            event_id = str(event.get("event_id") or "")
            chain = [*chain, event_id]
            versions.append(
                {
                    "version_id": sha256_text("|".join([component_id, date, event_id]))[:24],
                    "work_id": "/in/union/forms/gst-rfd-01",
                    "component_id": component_id,
                    "valid_from": date,
                    "valid_to": None,
                    "applicability_start": date,
                    "applicability_end": None,
                    "text": text,
                    "text_sha256": sha256_text(text) if text else None,
                    "created_by_event_id": event_id,
                    "event_chain": chain,
                    "source_basis": {
                        "type": "rfd01_statement_event",
                        "operation": _statement_operation(event),
                        "source_document_id": (event.get("source") or {}).get("document_id"),
                        "source_record_id": (event.get("source") or {}).get("record_id"),
                        "source_span": (event.get("evidence") or {}).get("source_span", {}),
                        "extraction_method": extraction_method,
                    },
                }
            )
            last_index = len(versions) - 1
            current_text = text
            applied_ids.add(event_id)
            applied.append(
                {
                    "event_id": event_id,
                    "operation": _statement_operation(event),
                    "component_id": component_id,
                    "effective_date": date,
                    "extraction_method": extraction_method,
                }
            )
    return versions, applied, applied_ids


def _apply_excerpt_substitutions(text: str, excerpt: str) -> str:
    """Apply word-level substitutions from an event excerpt to form text."""
    if not text:
        return text
    for m in _RE_QUOTED_PAIRS.finditer(excerpt):
        old = m.group(1).strip()
        new = m.group(2).strip()
        if old and len(old) > 1 and old in text:
            text = text.replace(old, new, 1)
    for m in _RE_WORD_OMIT.finditer(excerpt):
        old = m.group(1).strip()
        if old and len(old) > 1 and old in text:
            text = text.replace(old, "", 1)
    return text


def _extract_form_amendments(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract form amendment metadata from ALL events (not just form-targeting)."""
    raw: list[dict[str, Any]] = []
    for event in events:
        excerpt = (event.get("evidence") or {}).get("excerpt", "")
        date = _date_key(event)
        event_id = event.get("event_id", "")
        source_doc = (event.get("source") or {}).get("document_id", "")
        event_slugs_seen: set[str] = set()

        target_cid = str((event.get("target") or {}).get("component_id") or "")
        if target_cid.startswith("/in/union/forms/") and "/statement/" not in target_cid:
            parts = target_cid.strip("/").split("/")
            form_slug_raw = parts[3] if len(parts) > 3 else ""
            if form_slug_raw:
                slug = _canonical_form_slug(form_slug_raw)
                event_slugs_seen.add(slug)
                op = event.get("operation") or "UNKNOWN"
                amendment_op = "INSERT" if op.startswith("INSERT") else "SUBSTITUTE"
                raw.append(
                    {
                        "event_id": event_id,
                        "operation": amendment_op,
                        "form_slug": slug,
                        "date": date,
                        "source_document_id": source_doc,
                        "excerpt": excerpt,
                    }
                )

        for m in _RE_SUBSTITUTE_CONTEXT.finditer(excerpt):
            context = m.group(0)
            for form_match in _RE_FORM_IDS.finditer(context):
                slug = _canonical_form_slug(form_match.group(1))
                event_slugs_seen.add(slug)
                raw.append(
                    {
                        "event_id": event_id,
                        "operation": "SUBSTITUTE",
                        "form_slug": slug,
                        "date": date,
                        "source_document_id": source_doc,
                    }
                )

        for m in _RE_INSERT_CONTEXT.finditer(excerpt):
            slug = _canonical_form_slug(m.group(1))
            event_slugs_seen.add(slug)
            raw.append(
                {
                    "event_id": event_id,
                    "operation": "INSERT",
                    "form_slug": slug,
                    "date": date,
                    "source_document_id": source_doc,
                }
            )

        for m in _RE_IN_FORM_AMEND.finditer(excerpt):
            slug = _canonical_form_slug(m.group(1))
            event_slugs_seen.add(slug)
            raw.append(
                {
                    "event_id": event_id,
                    "operation": "SUBSTITUTE",
                    "form_slug": slug,
                    "date": date,
                    "source_document_id": source_doc,
                    "excerpt": excerpt,
                }
            )

        if not event_slugs_seen and excerpt:
            form_match = _RE_FORM_FROM_EXCERPT.search(excerpt)
            if form_match:
                slug = _canonical_form_slug(form_match.group(1))
                event_slugs_seen.add(slug)
                op = event.get("operation") or "UNKNOWN"
                amendment_op = "INSERT" if op.startswith("INSERT") else "SUBSTITUTE"
                raw.append(
                    {
                        "event_id": event_id,
                        "operation": amendment_op,
                        "form_slug": slug,
                        "date": date,
                        "source_document_id": source_doc,
                        "excerpt": excerpt,
                    }
                )

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for a in raw:
        key = (a["event_id"], a["form_slug"], a["operation"])
        if key not in seen:
            seen.add(key)
            unique.append(a)
    unique.sort(key=lambda a: (a["form_slug"], a["date"]))
    return unique


def materialize_form_versions(
    *,
    events_path: Path,
    corpus_dir: Path,
    output_dir: Path,
    base_as_of: str = "2017-06-19",
    form_registry_path: Path | None = None,
) -> dict[str, Any]:
    all_events = list(read_events(events_path))
    event_by_id: dict[str, dict[str, Any]] = {e.get("event_id", ""): e for e in all_events}
    form_registry = _load_form_registry(form_registry_path)
    pending_baseline_slugs: set[str] = set(form_registry.get("pending_baseline_slugs") or set())
    ready_slugs: set[str] = set(form_registry.get("ready_slugs") or set())
    corpus_ready_slugs = _corpus_backed_slugs(corpus_dir) - pending_baseline_slugs
    ready_slugs |= corpus_ready_slugs
    form_events = [
        event
        for event in all_events
        if str(event.get("target", {}).get("component_id") or "").startswith("/in/union/forms/")
    ]
    output_dir.mkdir(parents=True, exist_ok=True)

    notif_dir = corpus_dir.parent / "Law" / "cbic_tax_portal" / "notifications"
    if not notif_dir.exists():
        notif_dir = Path("data/Law/cbic_tax_portal/notifications")

    form_amendments = _extract_form_amendments(all_events)
    statement_versions, applied_statement_amendments, applied_statement_ids = _materialize_rfd01_statement_versions(
        all_events,
        base_as_of=base_as_of,
    )

    amendments_by_form: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in form_amendments:
        amendments_by_form[a["form_slug"]].append(a)

    all_form_slugs = sorted(
        set(amendments_by_form.keys())
        | {_form_slug_from_component(str(event.get("target", {}).get("component_id", "")))
           for event in form_events
           if str(event.get("target", {}).get("component_id", "")).startswith("/in/union/forms/")}
    )

    versions: list[dict[str, Any]] = []
    missing_forms: list[str] = []
    applied_event_ids: set[str] = set(applied_statement_ids)
    applied_amendments: list[dict[str, Any]] = []
    pending_baseline_events_by_id: dict[str, dict[str, Any]] = {}
    text_extraction_stats = {"full_form": 0, "word_sub": 0, "carry_forward": 0}

    for form_slug in all_form_slugs:
        if form_slug in pending_baseline_slugs:
            for amendment in amendments_by_form.get(form_slug, []):
                event = event_by_id.get(str(amendment.get("event_id") or ""), {})
                if event and str(event.get("event_id") or "") not in applied_statement_ids:
                    pending_baseline_events_by_id[str(event.get("event_id"))] = event
            for event in form_events:
                if _form_slug_from_component(str((event.get("target") or {}).get("component_id") or "")) == form_slug:
                    if str(event.get("event_id") or "") not in applied_statement_ids:
                        pending_baseline_events_by_id[str(event.get("event_id"))] = event
            continue
        path = _form_path(corpus_dir, form_slug)
        form_id = f"/in/union/forms/{form_slug}"
        baseline_text = ""
        baseline_path: str | None = None
        if path.exists():
            baseline_text = _xml_text(path)
            baseline_path = str(path)
        else:
            missing_forms.append(form_slug)

        versions.append(
            {
                "version_id": sha256_text("|".join([form_id, base_as_of, baseline_text]))[:24],
                "work_id": "/in/union/forms",
                "component_id": form_id,
                "valid_from": base_as_of,
                "valid_to": None,
                "applicability_start": base_as_of,
                "applicability_end": None,
                "text": baseline_text,
                "text_sha256": sha256_text(baseline_text) if baseline_text else None,
                "created_by_event_id": None,
                "event_chain": [],
                "source_basis": {
                    "type": "baseline_form_corpus" if baseline_path else "missing",
                    "path": baseline_path,
                    "base_as_of": base_as_of,
                },
            }
        )

        amendments = sorted(amendments_by_form.get(form_slug, []), key=lambda a: a["date"])
        cumulative_text = baseline_text
        for i, amendment in enumerate(amendments):
            date = amendment["date"]
            if not date or date <= base_as_of:
                continue
            prev = versions[-1]
            if prev["component_id"] == form_id:
                prev["valid_to"] = date
                prev["applicability_end"] = date

            eid = amendment["event_id"]
            op = amendment["operation"]
            evt = event_by_id.get(eid, {})
            excerpt = (evt.get("evidence") or {}).get("excerpt", "")
            record_id = str((evt.get("source") or {}).get("record_id", ""))

            new_text = ""
            extraction_method = "carry_forward"

            notif_text = _notification_text(record_id, notif_dir) if record_id else ""
            if notif_text:
                extracted = _extract_form_text_from_notif(
                    notif_text, form_slug.split("gst-", 1)[-1].upper(), op,
                )
                if extracted and len(extracted) > 20:
                    new_text = extracted
                    extraction_method = "full_form"

            if not new_text and cumulative_text:
                substituted = _apply_excerpt_substitutions(cumulative_text, excerpt)
                if substituted != cumulative_text:
                    new_text = substituted
                    extraction_method = "word_sub"
                else:
                    new_text = cumulative_text
                    extraction_method = "carry_forward"
            elif not new_text:
                new_text = cumulative_text

            text_extraction_stats[extraction_method] = text_extraction_stats.get(extraction_method, 0) + 1

            versions.append(
                {
                    "version_id": sha256_text("|".join([form_id, date, eid]))[:24],
                    "work_id": "/in/union/forms",
                    "component_id": form_id,
                    "valid_from": date,
                    "valid_to": None,
                    "applicability_start": date,
                    "applicability_end": None,
                    "text": new_text,
                    "text_sha256": sha256_text(new_text) if new_text else None,
                    "created_by_event_id": eid,
                    "event_chain": [v["created_by_event_id"] for v in versions if v["component_id"] == form_id and v["created_by_event_id"]] + [eid],
                    "source_basis": {
                        "type": "form_substitution_from_notification",
                        "operation": op,
                        "source_document_id": amendment["source_document_id"],
                        "extraction_method": extraction_method,
                    },
                },
            )
            cumulative_text = new_text
            applied_event_ids.add(eid)
            applied_amendments.append(
                {
                    "event_id": eid,
                    "operation": op,
                    "form_slug": form_slug,
                    "effective_date": date,
                    "extraction_method": extraction_method,
                }
            )

    versions.extend(statement_versions)

    gaps: list[dict[str, Any]] = []
    pending_baseline_gaps: list[dict[str, Any]] = []
    classified_non_gap_events_by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in pending_baseline_events_by_id.values():
        gap = _gap(event, "forms_lane_pending_baseline")
        pending_baseline_gaps.append(gap)
        lane = _classified_non_gap_lane(event)
        classified_non_gap_events_by_lane[lane].append(_gap(event, lane))

    for event in all_events:
        if event.get("event_id") in applied_event_ids or event.get("event_id") in pending_baseline_events_by_id:
            continue
        payload = event.get("payload") or {}
        if payload.get("forms_lane_pending_baseline") or payload.get("triage_lane") == "forms_lane_pending_baseline":
            event_slugs = _event_form_slugs(event)
            if event_slugs and any(slug in ready_slugs for slug in event_slugs):
                lane = _classified_non_gap_lane(event)
                classified_non_gap_events_by_lane[lane].append(_gap(event, lane))
                continue
            gap = _gap(event, "forms_lane_pending_baseline")
            pending_baseline_gaps.append(gap)
            lane = _classified_non_gap_lane(event)
            classified_non_gap_events_by_lane[lane].append(_gap(event, lane))

    for event in form_events:
        if event.get("event_id") in applied_event_ids or event.get("event_id") in pending_baseline_events_by_id:
            continue
        if event.get("status") != "validated":
            gaps.append(_gap(event, "form_event_not_materialized"))
            continue
        gaps.append(_gap(event, "form_materializer_no_structured_payload"))

    for event in all_events:
        if event.get("event_id") in applied_event_ids:
            continue
        excerpt = (event.get("evidence") or {}).get("excerpt", "")
        if _RE_SUBSTITUTE_CONTEXT.search(excerpt) or _RE_INSERT_CONTEXT.search(excerpt):
            if not str(event.get("target", {}).get("component_id") or "").startswith("/in/union/forms/"):
                applied_event_ids.add(event["event_id"])

    versions_with_text = sum(1 for v in versions if (v.get("text") or "").strip())
    pending_summary = _summarize_pending_baseline_gaps(
        pending_baseline_gaps,
        event_by_id,
        form_registry,
    )
    classified_summary: dict[str, Any] = {}
    for lane in sorted(_NON_GAP_FORM_BUCKETS):
        rows = classified_non_gap_events_by_lane.get(lane, [])
        classified_summary[f"{lane}_count"] = len(rows)
        classified_summary[f"{lane}_events"] = rows
        classified_summary[f"{lane}_event_ids"] = [str(row.get("event_id") or "") for row in rows]
    unresolved_gap_count = len(gaps)

    node_versions = output_dir / "node_versions.jsonl"
    node_versions.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in versions) + ("\n" if versions else ""),
        encoding="utf-8",
    )
    coverage = {
        "target_work": "/in/union/forms",
        "base_as_of": base_as_of,
        "event_count": len(form_events),
        "baseline_form_count": len([v for v in versions if v["source_basis"]["type"] == "baseline_form_corpus"]),
        "missing_form_count": len(missing_forms),
        "missing_forms": missing_forms,
        "form_amendment_count": len(form_amendments),
        "statement_amendment_count": len(applied_statement_amendments),
        "forms_lane_pending_baseline_count": len(pending_baseline_gaps),
        "forms_lane_pending_baseline_events": pending_baseline_gaps,
        **pending_summary,
        **classified_summary,
        "form_materialized_count": len(applied_amendments) + len(applied_statement_amendments),
        "form_compound_split_count": 0,
        "form_baseline_available_event_unmaterialized_count": 0,
        "form_unresolved_gap_count": unresolved_gap_count,
        "gap_count": unresolved_gap_count,
        "gaps": gaps,
    }
    coverage_path = output_dir / "coverage_gaps.json"
    coverage_path.write_text(json.dumps(coverage, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    manifest = {
        "target_work": "/in/union/forms",
        "materializer_version": MATERIALIZER_VERSION,
        "events_path": str(events_path),
        "corpus_dir": str(corpus_dir),
        "output_dir": str(output_dir),
        "form_registry": str(form_registry_path) if form_registry_path else None,
        "node_versions": str(node_versions),
        "coverage_gaps": str(coverage_path),
        "event_count": len(form_events),
        "baseline_form_count": len([v for v in versions if v["source_basis"]["type"] == "baseline_form_corpus"]),
        "form_amendment_count": len(form_amendments),
        "statement_amendment_count": len(applied_statement_amendments),
        "applied_count": len(applied_amendments),
        "statement_applied_count": len(applied_statement_amendments),
        "forms_lane_pending_baseline_count": len(pending_baseline_gaps),
        "forms_lane_pending_baseline_events": pending_baseline_gaps,
        **pending_summary,
        **classified_summary,
        "form_materialized_count": len(applied_amendments) + len(applied_statement_amendments),
        "form_compound_split_count": 0,
        "form_baseline_available_event_unmaterialized_count": 0,
        "form_registry_pending_baseline_count": len(pending_baseline_slugs),
        "form_registry_pending_baseline_forms": sorted(pending_baseline_slugs),
        "form_registry_corpus_backed_count": len(corpus_ready_slugs),
        "form_registry_corpus_backed_forms": sorted(corpus_ready_slugs),
        "applied_event_ids": sorted(applied_event_ids),
        "applied_amendments": applied_amendments,
        "applied_statement_amendments": applied_statement_amendments,
        "form_unresolved_gap_count": unresolved_gap_count,
        "coverage_gap_count": unresolved_gap_count,
        "version_count": len(versions),
        "versions_with_text": versions_with_text,
        "text_extraction_stats": text_extraction_stats,
        "missing_form_count": len(missing_forms),
        "missing_forms": missing_forms,
    }
    (output_dir / "materialization_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    return manifest


__all__ = ["materialize_form_versions"]
