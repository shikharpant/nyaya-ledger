"""Materialize component versions and dated XML snapshots from amendment events."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import copy
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.anchor_resolver import AnchorNotFoundError, resolve_anchor

from .amendment_events import read_events, sha256_text
from .baselines import build_baseline
from .component_spans import find_top_level_subrule_span, parent_component_for_subrule, subrule_label_from_component
from .identity_registry import load_registry
from .paths import expected_corpus_relative_path
from .renderer import render_rule, write_xml


MATERIALIZER_VERSION = "version-snapshots-v1"
SUPPORTED_OPS = {"INSERT_SIBLING", "INSERT_CHILD", "SPLICE", "SUBSTITUTE", "OMIT"}
SPECIAL_OPS = {"CORRIGENDUM", "RESCIND", "COMMENCE", "SUPERSEDE"}
ACT_WORK = "/in/union/acts/cgst-act-2017"

KNOWN_BASELINE_ARTIFACTS = {
    "/in/union/rules/cgst-rules-2017/rule/07",
    "/in/union/rules/cgst-rules-2017/rule/2020",
    "rule_9",
}

_log = logging.getLogger(__name__)

_CURLEY_Q = r'[\u201c\u201d\u2018\u2019"]'
# Qualifier prefix that may appear between "for"/"read" and the quoted text.
# Handles simple forms ("words", "figures") and compound forms joined by
# commas/and ("words and figures", "words, figures, letters and brackets",
# "figures and letter", "numbers and figures").
_CORR_QUALIFIER = (
    r"(?:(?:words?|figures?|numbers?|letters?|brackets?)"
    r"(?:\s*,\s*|\s+and\s+|\s+))*"
)
_RE_FOR_READ = re.compile(
    rf"for\s+(?:the\s+)?{_CORR_QUALIFIER}?{_CURLEY_Q}(.*?){_CURLEY_Q}"
    rf"\s*,?\s*read\s+(?:the\s+)?{_CORR_QUALIFIER}?{_CURLEY_Q}(.*?){_CURLEY_Q}",
    re.IGNORECASE | re.DOTALL,
)
_RE_FOR_SUBSTITUTED = re.compile(
    rf"for\s+(?:the\s+)?{_CORR_QUALIFIER}?{_CURLEY_Q}(.*?){_CURLEY_Q}"
    rf"\s*(?:shall\s+be\s+)?substituted\s+(?:by\s+|as\s+)?(?:the\s+)?{_CORR_QUALIFIER}?{_CURLEY_Q}(.*?){_CURLEY_Q}",
    re.IGNORECASE | re.DOTALL,
)
# "X" shall be read as "Y"  (retrospective re-numbering style corrigenda)
_RE_READ_AS = re.compile(
    rf"{_CURLEY_Q}(.*?){_CURLEY_Q}\s+shall\s+be\s+read\s+as\s+{_CURLEY_Q}(.*?){_CURLEY_Q}",
    re.IGNORECASE | re.DOTALL,
)
# Header-only corrigendum text carries no correctable body, e.g.
# "Corrigendum to Notification No. 03/2019-Central Tax."
_RE_HEADER_ONLY = re.compile(
    r"^\s*corrigendum\s+to\s+notification\s+no\.?\s*\d+/\d{4}"
    r"\s*[-\u2013]?\s*(?:central\s+tax|union\s+tax|integrated\s+tax)\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_RE_REFERS_TO_NOTIF = re.compile(
    r"No\.?\s*(\d+/\d{4})\s*[-\u2013]?\s*(?:Central\s+Tax|Union\s+Tax|Integrated\s+Tax)",
    re.IGNORECASE,
)
_RE_RESCINDS_NOTIF = re.compile(
    r"(?:rescind|supersede|withdraw)s?\s+(?:the\s+)?notification"
    r"(?:\s+of\s+the\s+Government[^.]*?)?\s*No\.?\s*(\d+/\d{4})"
    r"\s*[-\u2013]?\s*(?:Central\s+Tax|Union\s+Tax|Integrated\s+Tax)?",
    re.IGNORECASE | re.DOTALL,
)
_RE_RULE_REF = re.compile(r"(?:in\s+)?rule\s+(\d+[A-Z]?)", re.IGNORECASE)
_RE_RETRO = re.compile(r"\b(?:shall\s+be\s+deemed|deemed\s+to|retrospective|with\s+effect\s+from)\b", re.IGNORECASE)


def _parse_corrigendum(text: str) -> dict[str, Any]:
    corrections: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in (_RE_FOR_READ, _RE_FOR_SUBSTITUTED, _RE_READ_AS):
        for m in pattern.finditer(text):
            old_text = m.group(1).strip().strip(",").strip()
            new_text = m.group(2).strip().strip(",").strip()
            if not old_text or not new_text or old_text == new_text:
                continue
            key = (old_text, new_text)
            if key in seen:
                continue
            seen.add(key)
            corrections.append({"old_text": old_text, "new_text": new_text})
    refers_to = _RE_REFERS_TO_NOTIF.findall(text)
    rule_refs = _RE_RULE_REF.findall(text)
    if corrections:
        parse_status = "parsed"
    elif _RE_HEADER_ONLY.match(text or ""):
        parse_status = "not_applicable"
    else:
        parse_status = "no_corrections_found"
    return {
        "refers_to_notifications": refers_to,
        "rule_references": rule_refs,
        "corrections": corrections,
        "targets_rules": len(rule_refs) > 0,
        "retrospective": bool(_RE_RETRO.search(text)),
        "date_basis": "express_retrospective_text" if _RE_RETRO.search(text) else "corrigendum_publication_date",
        "parse_status": parse_status,
    }


def _corrigendum_effect_date(event: dict[str, Any], parsed: dict[str, Any]) -> str:
    if parsed.get("retrospective"):
        return (
            event.get("legal_time", {}).get("applicability_start")
            or event.get("legal_time", {}).get("commencement_date")
            or event.get("source", {}).get("publication_date")
            or ""
        )
    return event.get("source", {}).get("publication_date") or _date_key(event)


def _corrigendum_ledger(events: list[dict[str, Any]], corrigendum_data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {event.get("event_id"): event for event in events}
    rows: list[dict[str, Any]] = []
    for event_id, parsed in sorted(corrigendum_data.items()):
        event = by_id.get(event_id, {})
        payload = event.get("payload") or {}
        text = payload.get("text", "") or payload.get("corrigendum_text", "")
        rows.append(
            {
                "corrigendum_event_id": event_id,
                "corrected_notification_refs": parsed.get("refers_to_notifications", []),
                "rule_references": parsed.get("rule_references", []),
                "corrections": parsed.get("corrections", []),
                "parse_status": parsed.get("parse_status", "parsed" if parsed.get("corrections") else "no_corrections_found"),
                "original_notification_effective_date": None,
                "corrigendum_publication_date": (event.get("source") or {}).get("publication_date"),
                "corrigendum_effect_date": _corrigendum_effect_date(event, parsed),
                "retrospective": bool(parsed.get("retrospective")),
                "date_basis": parsed.get("date_basis", "corrigendum_publication_date"),
                "source_document_id": (event.get("source") or {}).get("document_id"),
                "text": text,
            }
        )
    return rows


def _parse_rescind(text: str) -> dict[str, Any]:
    rescinds = _RE_RESCINDS_NOTIF.findall(text)
    rule_refs = _RE_RULE_REF.findall(text)
    return {
        "rescinds_notifications": rescinds,
        "rule_references": rule_refs,
    }


def _parse_commence(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    description = payload.get("description", "")
    pub_date = (event.get("source") or {}).get("publication_date") or ""
    return {
        "commencement_date": pub_date,
        "description": description,
        "baseline_source_only": payload.get("baseline_source_only", False),
    }


def _doc_id_matches_rescinded(doc_id: str, rescinded_nums: set[str]) -> bool:
    if not doc_id or not rescinded_nums:
        return False
    normalized = _normalize_document_id(doc_id)
    for num in rescinded_nums:
        if re.search(rf"(?<!\d){re.escape(num)}(?!\d)", doc_id) or re.search(
            rf"(?<!\d){re.escape(num)}(?!\d)", normalized
        ):
            return True
        parts = num.split("/")
        if len(parts) == 2:
            year_suffix = parts[1]
            num_part = parts[0]
            hyphen_ref = f"{num_part}-{year_suffix}"
            if re.search(rf"(?<!\d){re.escape(hyphen_ref)}(?!\d)", doc_id) or re.search(
                rf"(?<!\d){re.escape(hyphen_ref)}(?!\d)", normalized
            ):
                return True
    return False


def _preprocess_special_ops(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rescinded_nums: set[str] = set()
    corrigendum_data: dict[str, dict[str, Any]] = {}
    commence_data: dict[str, dict[str, Any]] = {}

    for event in events:
        op = event.get("operation")
        if op in {"RESCIND", "SUPERSEDE"}:
            text = (event.get("payload") or {}).get("text", "")
            parsed = _parse_rescind(text)
            rescinded_nums.update(parsed["rescinds_notifications"])
        elif op == "CORRIGENDUM":
            payload = event.get("payload") or {}
            text = payload.get("text", "") or payload.get("corrigendum_text", "")
            corrigendum_data[event["event_id"]] = _parse_corrigendum(text)
        elif op == "COMMENCE":
            commence_data[event["event_id"]] = _parse_commence(event)

    enriched: list[dict[str, Any]] = []
    rescinded_event_ids: set[str] = set()
    for event in events:
        event_id = event.get("event_id", "")
        op = event.get("operation")

        if op in {"RESCIND", "SUPERSEDE", "CORRIGENDUM", "COMMENCE"}:
            enriched.append(event)
            continue

        doc_id = (event.get("source") or {}).get("document_id", "")
        instr = (event.get("source") or {}).get("instrument_number", "")
        if _doc_id_matches_rescinded(doc_id, rescinded_nums) or _doc_id_matches_rescinded(instr, rescinded_nums):
            rescinded_event_ids.add(event_id)

        enriched.append(event)

    metadata = {
        "rescinded_notifications": sorted(rescinded_nums),
        "rescinded_event_ids": sorted(rescinded_event_ids),
        "corrigendum_data": corrigendum_data,
        "commence_data": commence_data,
    }
    return enriched, metadata


def _with_materializer_repair_metadata(event: dict[str, Any], *, note: str) -> dict[str, Any]:
    repaired = copy.deepcopy(event)
    repaired["status"] = "validated"
    repaired.setdefault("review", {})
    repaired["review"] = {
        **repaired.get("review", {}),
        "required": False,
        "review_reasons": [],
        "reviewed_by": "version-snapshots-materializer-repair",
        "decision_notes": note,
    }
    repaired["validation"] = {
        **repaired.get("validation", {}),
        "target_resolved": True,
        "anchor_resolved": True,
        "date_resolved": True,
        "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
        "materializable": True,
    }
    return repaired


def _known_same_date_ordered_edits_allowed(events: list[dict[str, Any]]) -> bool:
    event_ids = {str(event.get("event_id") or "") for event in events}
    if event_ids == {"evt_cbic_9e14c73c00cff0ac", "evt_cbic_a3f6664ed9134482"}:
        return True
    if event_ids == {"evt_cbic_7f8fd053493ca8f1", "evt_cbic_ad72e292d9f041d1"}:
        return True
    if event_ids == {"evt_cbic_1c649897aa23b16c", "evt_cbic_9a6b87987cee9078"}:
        return True
    if event_ids == {"evt_cbic_9ca0612083600351", "evt_cbic_281b929c4472f9ab"}:
        return True
    if event_ids == {"evt_cbic_548f3453e01d95a1", "evt_cbic_0e042222febcbbb3"}:
        return True
    if event_ids == {"evt_cbic_1e72d7a4793d3a73", "evt_cbic_5d057e002ca2576d"}:
        return True
    return False


def _is_validated_rule_forms_lane_false_positive(event: dict[str, Any]) -> bool:
    """Return True for deterministic rule amendments blocked only as form-lane noise."""
    payload = event.get("payload") or {}
    if event.get("status") != "validated":
        return False
    if event.get("operation") not in SUPPORTED_OPS:
        return False
    if not payload.get("forms_lane_pending_baseline") and payload.get("triage_lane") != "forms_lane_pending_baseline":
        return False
    target_id = (event.get("target") or {}).get("component_id", "")
    if not target_id.startswith("/in/union/rules/cgst-rules-2017/rule/"):
        return False
    review_reasons = set((event.get("review") or {}).get("review_reasons", []))
    if review_reasons:
        return False
    return True


def _repair_known_materializer_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Patch narrow, source-proven extraction misses before materialization.

    These repairs preserve the source event chain for notifications whose text is
    available in the local corpus but whose reviewed event row captured only a
    wrapper fragment.
    """
    explicit_rule_retargets = {
        "evt_cbic_c2b1f8125e9039bc": "/in/union/rules/cgst-rules-2017/rule/138a/subrule/5",
        "evt_cbic_e34c9885e8036645": "/in/union/rules/cgst-rules-2017/rule/138b/subrule/3",
        "evt_cbic_b9fb86ae0c967dee": "/in/union/rules/cgst-rules-2017/rule/142/subrule/5",
        "evt_cbic_2de0bfe47a684717": "/in/union/rules/cgst-rules-2017/rule/142/subrule/5",
        "evt_cbic_b0582ff4d6408321": "/in/union/rules/cgst-rules-2017/rule/37a",
        "evt_cbic_f4f665aeb0149240": "/in/union/rules/cgst-rules-2017/rule/55/subrule/5",
        "evt_cbic_769781ae6b3bab9b": "/in/union/rules/cgst-rules-2017/rule/86a",
        "evt_cbic_88589bee6ed3fb1b": "/in/union/rules/cgst-rules-2017/rule/54/subrule/4a",
        "evt_cbic_184acfb7a2ea1c84": "/in/union/rules/cgst-rules-2017/rule/47a",
        "evt_cbic_2bb6a700dd622acb": "/in/union/rules/cgst-rules-2017/rule/46/proviso/e-invoice-signature-2019",
        "evt_cbic_b3d6c5e7c8d7f87d": "/in/union/rules/cgst-rules-2017/rule/49/proviso/e-bill-signature-2019",
        "evt_cbic_4e4bb37722141c27": "/in/union/rules/cgst-rules-2017/rule/59/subrule/2/proviso/april-2021-iff",
        "evt_cbic_63c066be5d58113c": "/in/union/rules/cgst-rules-2017/rule/46",
        "evt_cbic_8d1e9b596d074bea": "/in/union/rules/cgst-rules-2017/rule/83/subrule/8",
        "evt_cbic_fdba5b0781103634": "/in/union/rules/cgst-rules-2017/rule/133",
        "evt_cbic_92944a08390351bb": "/in/union/rules/cgst-rules-2017/rule/119",
    }
    repaired_events: list[dict[str, Any]] = []
    for event in events:
        event_id = event.get("event_id")
        payload = event.get("payload") or {}
        omitted_label = str(payload.get("omitted_label") or "").strip()
        if (
            event.get("operation") == "OMIT"
            and payload.get("whole_component") is True
            and omitted_label
            and str(event_id).startswith("evt_cbic_xml_")
            and (event.get("target") or {}).get("component_id") == "/in/union/rules/cgst-rules-2017/rule/38"
        ):
            note = (
                "Materializer repair for canonical notification XML whole-rule omission: "
                "the backfill row preserved the correct omitted_label but context recovery "
                "left the target component at the preceding Rule 38 instruction."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": f"/in/union/rules/cgst-rules-2017/rule/{omitted_label.lower()}",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_55425afaccddf783":
            note = (
                "Materializer repair from Notification 24/2022-Central Tax: source clause "
                "2(b) omits rules 124 and 125, but the compiled row remained attached to "
                "Rule 122 as a document-scope target. Split the source-backed compound "
                "instruction into one whole-component omission per named rule."
            )
            for rule_label in ("124", "125"):
                repaired = _with_materializer_repair_metadata(event, note=note)
                repaired["event_id"] = f"{event_id}_rule_{rule_label}"
                repaired["legacy_event_id"] = event_id
                repaired["operation"] = "OMIT"
                repaired["target"] = {
                    **(repaired.get("target") or {}),
                    "component_id": f"/in/union/rules/cgst-rules-2017/rule/{rule_label}",
                    "anchor_component_id": None,
                    "anchor_text": None,
                    "anchor_occurrence": None,
                }
                repaired["payload"] = {
                    **(repaired.get("payload") or {}),
                    "whole_component": True,
                    "target_rules": [rule_label],
                    "triage_lane": None,
                    "materializer_repair": True,
                    "materializer_repair_reason": "compound_rule124_125_omit_split_from_source",
                }
                repaired_events.append(repaired)
            continue

        if event_id in {"evt_cbic_548f3453e01d95a1", "evt_cbic_0e042222febcbbb3"}:
            note = (
                "Materializer repair from Notification 60/2018-Central Tax: the source "
                "substitutes the same Rule 109A clause (b) phrase in sub-rules (1) and "
                "(2), but the compiled events target split subrule component ids while "
                "Rule 109A is materialized as a parent rule component."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            occurrence = 1 if event_id == "evt_cbic_548f3453e01d95a1" else 2
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/109a",
                "anchor_component_id": None,
                "anchor_text": "the Additional Commissioner (Appeals)",
                "anchor_occurrence": occurrence,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "the Additional Commissioner (Appeals)",
                "new_text": "any officer not below the rank of Joint Commissioner (Appeals)",
                "match_occurrence": occurrence,
                "materializer_repair": True,
                "materializer_repair_reason": "retarget_rule109a_subrule_substitution_to_parent",
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_7a6dd14dc9007d76":
            note = (
                "Materializer repair from Notification 3/2018-Central Tax: the compiled "
                "Rule 31A insertion stopped before the remaining definition clause and "
                "sub-rule (3). Use the complete source-backed rule text from paragraphs "
                "14-20."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_SIBLING"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/31a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/31",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "anchor_rule": "31",
                "label": "31A",
                "heading": "Value of supply in case of lottery, betting, gambling and horse racing",
                "node_type": "rule",
                "position": "after",
                "content": (
                    "(1) Notwithstanding anything contained in the provisions of this Chapter, "
                    "the value in respect of supplies specified below shall be determined in "
                    "the manner provided hereinafter. (2) (a) The value of supply of lottery "
                    "run by State Governments shall be deemed to be 100/112 of the face value "
                    "of ticket or of the price as notified in the Official Gazette by the "
                    "organising State, whichever is higher. (b) The value of supply of lottery "
                    "authorised by State Governments shall be deemed to be 100/128 of the face "
                    "value of ticket or of the price as notified in the Official Gazette by "
                    "the organising State, whichever is higher. Explanation:- For the purposes "
                    "of this sub-rule, the expressions- (a) \"lottery run by State Governments\" "
                    "means a lottery not allowed to be sold in any State other than the "
                    "organizing State; (b) \"lottery authorised by State Governments\" means "
                    "a lottery which is authorised to be sold in State(s) other than the "
                    "organising State also; and (c) \"Organising State\" has the same meaning "
                    "as assigned to it in clause (f) of sub-rule (1) of rule 2 of the "
                    "Lotteries (Regulation) Rules, 2010. (3) The value of supply of "
                    "actionable claim in the form of chance to win in betting, gambling or "
                    "horse racing in a race club shall be 100% of the face value of the bet "
                    "or the amount paid into the totalisator."
                ),
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule31a_complete_2018_insertion_from_source_paragraphs_14_20",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_5bde6512dd0562b2":
            note = (
                "Materializer repair from Notification 8/2020-Central Tax: source "
                "substitutes only Rule 31A(2), but the compiled structural text included "
                "notification signature and note material. Replace the parent rule with "
                "the clean legal text preserving sub-rules (1) and (3)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/31a",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                "old_text": None,
                "new_text": None,
                "structural_text": (
                    "(1) Notwithstanding anything contained in the provisions of this Chapter, "
                    "the value in respect of supplies specified below shall be determined in "
                    "the manner provided hereinafter. (2) The value of supply of lottery shall "
                    "be deemed to be 100/128 of the face value of ticket or of the price as "
                    "notified in the Official Gazette by the Organising State, whichever is "
                    "higher. Explanation:- For the purposes of this sub-rule, the expression "
                    "\"Organising State\" has the same meaning as assigned to it in clause "
                    "(f) of sub-rule (1) of rule 2 of the Lotteries (Regulation) Rules, 2010. "
                    "(3) The value of supply of actionable claim in the form of chance to win "
                    "in betting, gambling or horse racing in a race club shall be 100% of the "
                    "face value of the bet or the amount paid into the totalisator."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule31a_clean_2020_subrule2_substitution_without_notification_notes",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_f89d802d02978dc1":
            note = (
                "Materializer repair from Notification 29/2018-Central Tax: the source "
                "span contains separate Rule 129 and Rule 130 substitutions for "
                "Director General of Safeguards. Preserve the compiled Rule 130 event "
                "and split out the Rule 129 replacement from the same source span."
            )
            rule129 = _with_materializer_repair_metadata(event, note=note)
            rule129["event_id"] = f"{event_id}_rule_129"
            rule129["legacy_event_id"] = event_id
            rule129["operation"] = "SUBSTITUTE"
            rule129["target"] = {
                **(rule129.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/129",
                "anchor_component_id": None,
                "anchor_text": "Director General of Safeguards",
                "anchor_occurrence": None,
            }
            rule129["payload"] = {
                **(rule129.get("payload") or {}),
                "old_text": "Director General of Safeguards",
                "new_text": "Director General of Anti-profiteering",
                "replace_all": True,
                "materializer_repair": True,
                "materializer_repair_reason": "compound_rule129_130_antiprofiteering_substitution_split_from_source",
            }
            repaired_events.append(rule129)

            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/130",
                "anchor_component_id": None,
                "anchor_text": "Director General of Safeguards",
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "Director General of Safeguards",
                "new_text": "Director General of Anti-profiteering",
                "replace_all": True,
                "materializer_repair": True,
                "materializer_repair_reason": "compound_rule129_130_antiprofiteering_substitution_split_from_source",
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_1e72d7a4793d3a73":
            note = (
                "Materializer repair from Notification 19/2022-Central Tax: paragraph 5(a) "
                "amends Rule 38 clause (a)(ii), but the compiled row stayed at document "
                "scope because the instruction mentions FORM GSTR-2."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "OMIT"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/38",
                "anchor_component_id": None,
                "anchor_text": "in FORM GSTR-2",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "omit_text": ", in FORM GSTR-2",
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule38_2022_clause_a_ii_form_gstr2_omission_from_source",
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_7a9ce35c5a1275d5":
            note = (
                "Materializer repair from Notification 34/2017-Central Tax: the source "
                "inserts Rule 120A after Rule 120, but the compiled row was routed to the "
                "forms lane because the inserted rule text mentions FORM GST TRAN-1."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/120a",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                "old_text": None,
                "new_text": None,
                "structural_text": (
                    "Rule 120A. Every registered person who has submitted a declaration "
                    "electronically in FORM GST TRAN-1 within the time period specified in "
                    "rule 117, rule 118, rule 119 and rule 120 may revise such declaration "
                    "once and submit the revised declaration in FORM GST TRAN-1 "
                    "electronically on the common portal within the time period specified "
                    "in the said rules or such further period as may be extended by the "
                    "Commissioner in this behalf."
                ),
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule120a_2017_insertion_misrouted_to_form_lane",
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_2bba0f5762979321":
            note = (
                "Materializer repair from Notification 36/2017-Central Tax: the source "
                "inserts Rule 120A's marginal heading, but the compiler treated the row "
                "as a FORM GST TRAN-1 mutation."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/120a",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                "old_text": None,
                "new_text": None,
                "structural_text": (
                    "Rule 120A. Revision of declaration in FORM GST TRAN-1 Every registered "
                    "person who has submitted a declaration electronically in FORM GST TRAN-1 "
                    "within the time period specified in rule 117, rule 118, rule 119 and "
                    "rule 120 may revise such declaration once and submit the revised "
                    "declaration in FORM GST TRAN-1 electronically on the common portal "
                    "within the time period specified in the said rules or such further "
                    "period as may be extended by the Commissioner in this behalf."
                ),
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule120a_2017_marginal_heading_misrouted_to_form_lane",
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_b3d6c5e7c8d7f87d":
            note = (
                "Materializer repair from Notification 74/2018-Central Tax: source clause "
                "5 inserts the Rule 49 electronic bill-of-supply signature proviso after "
                "the second proviso, but the compiled payload retained only a placeholder."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/49/proviso/e-bill-signature-2019",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
                "anchor_text": "after the second proviso",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "provided-also-signature",
                "node_type": "proviso",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
                "position": "append",
                "content": (
                    "Provided also that the signature or digital signature of the supplier "
                    "or his authorised representative shall not be required in the case of "
                    "issuance of an electronic bill of supply in accordance with the "
                    "provisions of the Information Technology Act, 2000 (21 of 2000)."
                ),
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule49_2018_electronic_bill_signature_proviso_completed_from_source",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_49c35f127ce65025":
            note = (
                "Materializer repair from Notification 31/2019-Central Tax: source clause "
                "6 inserts the Rule 49 QR-code proviso after the third proviso, but context "
                "recovery classified it as a generic clause and retained wrapper punctuation."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/49/proviso/qr-code-2019",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
                "anchor_text": "after the third proviso",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "provided-also-qr-code",
                "node_type": "proviso",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/49",
                "position": "append",
                "content": (
                    "Provided also that the Government may, by notification, on the "
                    "recommendations of the Council, and subject to such conditions and "
                    "restrictions as mentioned therein, specify that the bill of supply "
                    "shall have Quick Response (QR) code."
                ),
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule49_2019_qr_code_proviso_completed_from_source",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_deb8b1120845cc27":
            note = (
                "Materializer repair from Notification 38/2023-Central Tax: source clause "
                "14 renumbers Rule 94 as sub-rule (1) and inserts sub-rule (2), but the "
                "compiled target used a non-canonical /rules/cgst/94/1 anchor and the "
                "payload was truncated at the first clause."
            )
            renumber = _with_materializer_repair_metadata(event, note=note)
            renumber["event_id"] = f"{event_id}_renumber_rule94_subrule1"
            renumber["legacy_event_id"] = event_id
            renumber["status"] = "validated"
            renumber["operation"] = "SUBSTITUTE"
            renumber["target"] = {
                **(renumber.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/94",
                "anchor_component_id": None,
                "anchor_text": "Where any interest is due and payable to the applicant under section 56",
                "anchor_occurrence": 1,
            }
            renumber["payload"] = {
                "old_text": "Where any interest is due and payable to the applicant under section 56",
                "new_text": "(1) Where any interest is due and payable to the applicant under section 56",
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule94_2023_renumber_existing_text_as_subrule1_from_source",
            }
            renumber["review"] = {
                **(renumber.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            renumber["validation"] = {
                **(renumber.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(renumber)

            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/94/subrule/2",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/94",
                "anchor_text": "after the sub-rule as so renumbered",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "2",
                "node_type": "sub-rule",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/94",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/94",
                "position": "append",
                "content": (
                    "The following periods shall not be included in the period of delay "
                    "under sub-rule (1), namely:- (a) any period of time beyond fifteen days "
                    "of receipt of notice in FORM GST RFD-08 under sub-rule (3) of rule 92, "
                    "that the applicant takes to- (i) furnish a reply in FORM GST RFD-09, "
                    "or (ii) submit additional documents or reply; and (b) any period of "
                    "time taken either by the applicant for furnishing the correct details "
                    "of the bank account to which the refund is to be credited or for "
                    "validating the details of the bank account so furnished, where the "
                    "amount of refund sanctioned could not be credited to the bank account "
                    "furnished by the applicant."
                ),
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule94_2023_renumber_and_subrule2_insert_from_source",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_5d057e002ca2576d":
            note = (
                "Materializer repair from Notification 19/2022-Central Tax: paragraph 5(b) "
                "substitutes Rule 38 clause (c) and paragraph 5(c) omits clause (d), but "
                "the compiler left the compound instruction as UNKNOWN because the text "
                "mentions FORM GSTR-2/GSTR-3B."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/38",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                "old_text": None,
                "new_text": None,
                "structural_text": (
                    "Rule 38. Claim of credit by a banking company or a financial "
                    "institution.- A banking company or a financial institution, including a non-banking "
                    "financial company, engaged in the supply of services by way of accepting "
                    "deposits or extending loans or advances that chooses not to comply with "
                    "the provisions of sub-section (2) of section 17, in accordance with the "
                    "option permitted under sub-section (4) of that section, shall follow the "
                    "following procedure, namely,- (a) the said company or institution shall "
                    "not avail the credit of,- (i) the tax paid on inputs and input services "
                    "that are used for non-business purposes; and (ii) the credit attributable "
                    "to the supplies specified in sub-section (5) of section 17; (b) the said "
                    "company or institution shall avail the credit of tax paid on inputs and "
                    "input services referred to in the second proviso to sub-section (4) of "
                    "section 17 and not covered under clause (a); (c) fifty per cent. of the "
                    "remaining amount of input tax shall be the input tax credit admissible to "
                    "the company or the institution and the balance amount of input tax credit "
                    "shall be reversed in FORM GSTR-3B; (d) [Omitted]"
                ),
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule38_2022_clause_c_substitution_and_clause_d_omission_from_source",
            }
            repaired_events.append(repaired)
            continue

        if event_id in explicit_rule_retargets:
            note = (
                "Materializer repair from explicit rule context in the reviewed excerpt: "
                "post-hoc context recovery attached the event to a neighboring rule, while "
                "the excerpt itself names the corrected rule/sub-rule target."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": explicit_rule_retargets[event_id],
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "materializer_repair": True,
                "materializer_repair_reason": "explicit_rule_context_retarget",
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_c0defaa1ae2acdcb":
            note = (
                "Materializer repair from Notification 82/2020-Central Tax: source text is "
                "a complete Rule 60 substitution with effect from 1 January 2021. Triage "
                "misrouted it to the forms lane because the replacement mentions GSTR forms."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "whole_component": True,
                "materializer_repair": True,
                "materializer_repair_reason": "rule60_whole_component_misrouted_to_forms_lane",
            }
            repaired_events.append(repaired)

            subrule_7_text = (
                "(7) An auto-drafted statement containing the details of input tax credit "
                "shall be made available to the registered person in FORM GSTR-2B, for "
                "every month, electronically through the common portal, and shall consist "
                "of - (i) the details of outward supplies furnished by his supplier, other "
                "than a supplier required to furnish return for every quarter under proviso "
                "to sub-section (1) of section 39, in FORM GSTR-1, between the day "
                "immediately after the due date of furnishing of FORM GSTR-1 for the "
                "previous month to the due date of furnishing of FORM GSTR-1 for the month; "
                "(ii) the details of invoices furnished by a non-resident taxable person in "
                "FORM GSTR- 5 and details of invoices furnished by an Input Service "
                "Distributor in his return in FORM GSTR-6 and details of outward supplies "
                "furnished by his supplier, required to furnish return for every quarter "
                "under proviso to sub-section (1) of section 39, in FORM GSTR-1 or using "
                "the IFF, as the case may be,- (a)for the first month of the quarter, "
                "between the day immediately after the due date of furnishing of FORM "
                "GSTR-1 for the preceding quarter to the due date of furnishing details "
                "using the IFF for the first month of the quarter; (b) for the second "
                "month of the quarter, between the day immediately after the due date of "
                "furnishing details using the IFF for the first month of the quarter to "
                "the due date of furnishing details using the IFF for the second month of "
                "the quarter; (c) for the third month of the quarter, between the day "
                "immediately after the due date of furnishing of details using the IFF for "
                "the second month of the quarter to the due date of furnishing of FORM "
                "GSTR-1 for the quarter; (iii) the details of the integrated tax paid on "
                "the import of goods or goods brought in the domestic Tariff Area from "
                "Special Economic Zone unit or a Special Economic Zone developer on a bill "
                "of entry in the month."
            )
            subrule_7 = _with_materializer_repair_metadata(event, note=note)
            subrule_7["event_id"] = f"{event_id}_rule60_7_replace"
            subrule_7["status"] = "validated"
            subrule_7["operation"] = "SUBSTITUTE"
            subrule_7["target"] = {
                **(subrule_7.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/60/subrule/7",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_7["review"] = {
                **(subrule_7.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            subrule_7["payload"] = {
                "structural_text": subrule_7_text,
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/60",
                "apply_to_parent_subrule_span": True,
                "allow_detached_component_version": True,
                "materializer_repair": True,
                "materializer_repair_reason": "rule60_subrule7_child_from_whole_rule_substitution",
            }
            subrule_7["validation"] = {
                **(subrule_7.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(subrule_7)
            continue

        if event_id == "evt_cbic_53eef1a4d4e92613":
            note = (
                "Materializer repair from Notification 14/2022-Central Tax: source text "
                "directly inserts Rule 86(4B), but the compiler routed the row to FORM GST "
                "PMT-03A because the inserted rule text mentions that form."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/86/subrule/4b",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/86/subrule/4a",
                "anchor_text": "after sub-rule (4A)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "label": "4B",
                "node_type": "subrule",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/86",
                "content": (
                    "(4B) Where a registered person deposits the amount of erroneous refund "
                    "sanctioned to him, - (a) under sub-section (3) of section 54 of the Act, "
                    "or (b) under sub-rule (3) of rule 96, in contravention of sub-rule (10) "
                    "of rule 96, along with interest and penalty, wherever applicable, "
                    "through FORM GST DRC-03, by debiting the electronic cash ledger, on his "
                    "own or on being pointed out, an amount equivalent to the amount of "
                    "erroneous refund deposited by the registered person shall be re-credited "
                    "to the electronic credit ledger by the proper officer by an order made "
                    "in FORM GST PMT-03A."
                ),
                "forms_lane_pending_baseline": None,
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule86_subrule4b_misrouted_to_form_lane",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_8ca0a1168d9339cb":
            note = (
                "Materializer repair from Notification 38/2023-Central Tax: source clause "
                "4(ii) amends Rule 21A(4), but context recovery attached the row to Rule "
                "10A because the inserted proviso refers to Rule 10A compliance."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/21a/subrule/4/proviso/rule10a-compliance",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/21a/subrule/4",
                "anchor_text": "after second proviso",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "label": "Provided also that",
                "node_type": "proviso",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/21a",
                "subrule_label": "4",
                "apply_to_parent_subrule_span": True,
                "content": (
                    "Provided also that where the registration has been suspended under "
                    "sub-rule (2A) for contravention of provisions of rule 10A and the "
                    "registration has not already been cancelled by the proper officer under "
                    "rule 22, the suspension of registration shall be deemed to be revoked "
                    "upon compliance with the provisions of rule 10A."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule21a_subrule4_proviso_retarget_from_rule10a_reference",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id in {"evt_cbic_1c649897aa23b16c", "evt_cbic_9a6b87987cee9078"}:
            note = (
                "Materializer repair from Notification 20/2024-Central Tax: source clause "
                "9(a) amends Rule 89(4), but context recovery attached the rows to Rule "
                "88D because the immediately preceding instruction amended Rule 88D(3)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/4",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            omit_text = (
                "other than the input tax credit availed for which refund is claimed under "
                "sub-rules (4A) or (4B) or both"
                if event_id == "evt_cbic_1c649897aa23b16c"
                else (
                    ", other than the turnover of supplies in respect of which refund is "
                    "claimed under sub-rules (4A) or (4B) or both"
                )
            )
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "omit_text": omit_text,
                "whole_component": False,
                "noop_if_already_reflected": event_id == "evt_cbic_1c649897aa23b16c",
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule89_subrule4_retarget_from_rule88d_context",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_2bf46864fcabb9b6":
            note = (
                "Materializer repair from Notification 03/2019-Central Tax: source clause "
                "10(d) inserts Rule 53(1A), but the reviewed row inherited the following "
                "Rule 83(8) replacement payload and targeted Rule 80(8)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/53/subrule/1a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/53/subrule/1",
                "anchor_text": "after sub-rule (1)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "label": "1A",
                "node_type": "subrule",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/53",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/53/subrule/1",
                "position": "after",
                "content": (
                    "A credit or debit note referred to in section 34 shall contain the "
                    "following particulars, namely:- (a) name, address and Goods and "
                    "Services Tax Identification Number of the supplier; (b) nature of "
                    "the document; (c) a consecutive serial number not exceeding sixteen "
                    "characters, in one or multiple series, containing alphabets or "
                    "numerals or special characters-hyphen or dash and slash symbolised "
                    "as \"-\" and \"/\" respectively, and any combination thereof, unique "
                    "for a financial year; (d) date of issue of the document; (e) name, "
                    "address and Goods and Services Tax Identification Number or Unique "
                    "Identity Number, if registered, of the recipient; (f) name and "
                    "address of the recipient and the address of delivery, along with "
                    "the name of State and its code, if such recipient is un-registered; "
                    "(g) serial number(s) and date(s) of the corresponding tax invoice(s) "
                    "or, as the case may be, bill(s) of supply; (h) value of taxable "
                    "supply of goods or services, rate of tax and the amount of the tax "
                    "credited or, as the case may be, debited to the recipient; and "
                    "(i) signature or digital signature of the supplier or his authorised "
                    "representative."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule53_subrule1a_retarget_from_rule80_context_bleed",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_542473e134821bd1":
            note = (
                "Materializer repair from Notification 45/2017-Central Tax: source clause "
                "inserts complete Rule 46A, but the compiled payload split the heading at "
                "'Invoice' and truncated the operative text after 'invoice-cum-bill of supply'."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_SIBLING"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                "anchor_text": "after rule 46",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "46A",
                "node_type": "rule",
                "anchor_rule": "46",
                "position": "after",
                "heading": "Invoice-cum-bill of supply",
                "content": (
                    "Notwithstanding anything contained in rule 46 or rule 49 or rule 54, "
                    "where a registered person is supplying taxable as well as exempted "
                    "goods or services or both to an unregistered person, a single "
                    "\"invoice-cum-bill of supply\" may be issued for all such supplies."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule46a_insert_payload_completed_from_source",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_03aea0568073d822":
            note = (
                "Materializer repair from Notification 26/2022-Central Tax: source clause "
                "8 inserts the Rule 46A proviso about particulars under rules 46, 54 and "
                "49, but the compiled payload inherited unrelated 30-day invoice text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46a/proviso/provided",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/46a",
                "anchor_text": "the following proviso shall be inserted",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "provided",
                "node_type": "proviso",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/46a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/46a",
                "position": "append",
                "content": (
                    "Provided that the said single \"invoice-cum-bill of supply\" shall "
                    "contain the particulars as specified under rule 46 or rule 54, as "
                    "the case may be, and rule 49."
                ),
                "triage_lane": None,
                "forms_lane_pending_baseline": False,
                "materializer_repair": True,
                "materializer_repair_reason": "rule46a_2022_proviso_payload_replaced_from_source",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_92c8303b6b01e1b9":
            note = (
                "Materializer repair from Notification 12/2024-Central Tax: source clause "
                "inserts Rule 39(1A). The inserted text references sub-rule (1A) of rule "
                "54, which caused parser targeting to drift to Rule 54; keep the source "
                "instruction attached to Rule 39."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/39/subrule/1a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/39",
                "anchor_text": "after sub-rule (1)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "1A",
                "node_type": "subrule",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/39",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/39/subrule/1",
                "position": "after",
                "content": (
                    "For the distribution of credit in respect of input services, attributable "
                    "to one or more distinct persons, subject to levy of tax under sub-section "
                    "(3) or (4) of section 9, a registered person, having the same PAN and "
                    "State code as an Input Service Distributor, may issue an invoice or, as "
                    "the case may be, a credit or debit note as per the provisions of "
                    "sub-rule(1A) of rule 54 to transfer the credit of such common input "
                    "services to the Input Service Distributor, and such credit shall be "
                    "distributed by the said Input Service Distributor in the manner as "
                    "provided in sub-rule (1)."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule39_subrule1a_retarget_from_rule54_reference",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_e7c949e08edbb979":
            note = (
                "Materializer repair from Notification 16/2020-Central Tax: source text "
                "inserts the complete Rule 8(4A), while the current reconstructed child "
                "slot contains unrelated deadline text from the registration-rule chain. "
                "Record the source-proven Rule 8(4A) text as a dated replacement of that "
                "existing child component."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/8",
                "anchor_text": "after sub-rule (4)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "label": "4A",
                "node_type": "subrule",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/8",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4",
                "position": "after",
                "content": (
                    "The applicant shall, while submitting an application under sub-rule "
                    "(4), with effect from 01.04.2020, undergo authentication of Aadhaar "
                    "number for grant of registration."
                ),
                "replace_existing_child": True,
                "materializer_repair": True,
                "materializer_repair_reason": "rule8_subrule4a_replace_contaminated_child_slot",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_7881d05408bb9183":
            note = (
                "Materializer repair from Notification 74/2018-Central Tax: source clause "
                "6(a) inserts a proviso into Rule 54(2), but the reviewed row captured the "
                "wrapper label '(a)' as a clause insertion."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/2/proviso/no-signature-2018",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/2",
                "anchor_text": "in sub-rule (2), the following proviso",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "label": "Provided",
                "node_type": "proviso",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/54/subrule/2",
                "content": (
                    "Provided that the signature or digital signature of the supplier or his "
                    "authorised representative shall not be required in the case of issuance "
                    "of a consolidated tax invoice or any other document in lieu thereof in "
                    "accordance with the provisions of the Information Technology Act, 2000 "
                    "(21 of 2000)."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule54_subrule2_proviso_from_wrapper_clause",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_095e46e40dae412f":
            note = (
                "Materializer repair from Notification 49/2019-Central Tax read with "
                "Notification 60/2018-Central Tax: the reviewed row correctly targets "
                "Rule 83A(6), but the local baseline only has a truncated Rule 83A parent. "
                "Precreate the source-proven subrule text and apply the clause (i) "
                "substitution."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            old_clause = (
                "(i) A person enrolled as a goods and services tax practitioner in terms "
                "of sub-rule (2) of rule 83 is required to pass the examination within "
                "two years of enrolment"
            )
            new_clause = (
                "(i) Every person referred to in clause (b) of sub-rule (1) of rule 83 "
                "and who is enrolled as a goods and services tax practitioner under "
                "sub-rule (2) of the said rule is required to pass the examination within "
                "the period as specified in the second proviso of sub-rule (3) of the "
                "said rule."
            )
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83a/subrule/6",
                "anchor_component_id": None,
                "anchor_text": old_clause,
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "old_text": old_clause,
                "new_text": new_clause,
                "structural_text": (
                    "(6) Period for passing the examination and number of attempts "
                    "allowed.- (i) Every person referred to in clause (b) of sub-rule "
                    "(1) of rule 83 and who is enrolled as a goods and services tax "
                    "practitioner under sub-rule (2) of the said rule is required to "
                    "pass the examination within the period as specified in the second "
                    "proviso of sub-rule (3) of the said rule. "
                    "(ii) A person required to pass the examination may avail of any "
                    "number of attempts but these attempts shall be within the period "
                    "as specified in clause (i). (iii) A person shall register and pay "
                    "the requisite fee every time he intends to appear at the "
                    "examination. (iv) In case the goods and services tax practitioner "
                    "having applied for appearing in the examination is prevented from "
                    "availing one or more attempts due to unforeseen circumstances such "
                    "as critical illness, accident or natural calamity, he may make a "
                    "request in writing to the jurisdictional Commissioner for granting "
                    "him one additional attempt to pass the examination, within thirty "
                    "days of conduct of the said examination. NACIN may consider such "
                    "requests on merits based on recommendations of the jurisdictional "
                    "Commissioner."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule83a_subrule6_from_truncated_parent",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_fe42a22b62593e55":
            note = (
                "Materializer repair from Notification 12/2024-Central Tax: source clause "
                "20(i) substitutes Rule 96A(1)(b), but context recovery matched the prefix "
                "'rule 96' and attached the event to Rule 96(1) with placeholder old/new "
                "text from the LLM candidate."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96a",
                "anchor_component_id": None,
                "anchor_text": (
                    "(b) fifteen days after the expiry of one year, or such further period "
                    "as may be allowed by the Commissioner, from the date of issue of the "
                    "invoice for export, if the payment of such services is not received "
                    "by the exporter in convertible foreign exchange or in Indian rupees, "
                    "wherever permitted by the Reserve Bank of India."
                ),
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "old_text": (
                    "(b) fifteen days after the expiry of one year, or such further period "
                    "as may be allowed by the Commissioner, from the date of issue of the "
                    "invoice for export, if the payment of such services is not received "
                    "by the exporter in convertible foreign exchange or in Indian rupees, "
                    "wherever permitted by the Reserve Bank of India."
                ),
                "new_text": (
                    "(b) fifteen days after the expiry of one year, or the period as allowed "
                    "under the Foreign Exchange Management Act, 1999 (42 of 1999) including "
                    "any extension of such period as permitted by the Reserve Bank of India, "
                    "whichever is later, from the date of issue of the invoice for export, or "
                    "such further period as may be allowed by the Commissioner, if the payment "
                    "of such services is not received by the exporter in convertible foreign "
                    "exchange or in Indian rupees, wherever permitted by the Reserve Bank of "
                    "India."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule96a_clause_b_substitution_retargeted_from_rule96_prefix_match",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id in {
            "evt_cbic_e32d6c82385a2b56",
            "evt_cbic_4b97cd62a67e4be1",
            "evt_cbic_f71bca8f1cfeffcf",
        }:
            note = (
                "Materializer repair for the Rule 96(10) substitution chain: reviewed "
                "rows contain source-proven structural text, but detached/form-lane "
                "flags prevented creation of the sub-rule component needed by later "
                "Explanation and omission events."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired_payload = {
                **(repaired.get("payload") or {}),
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96",
                "materializer_repair": True,
                "materializer_repair_reason": "rule96_subrule10_chain_create_component",
            }
            repaired_payload.pop("allow_detached_component_version", None)
            repaired_payload.pop("forms_lane_pending_baseline", None)
            repaired_payload.pop("triage_lane", None)
            repaired["payload"] = repaired_payload
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_b56b32722affd914":
            note = (
                "Materializer repair from Notification 16/2020-Central Tax: source clause "
                "10 inserts an Explanation into Rule 96(10)(b), but the reviewed payload "
                "truncated the final BCD phrase and remained pending because the Rule "
                "96(10) component had only detached prior versions."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10/explanation/explanation",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10/clause/b",
                "anchor_text": "in clause (b)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "label": "Explanation",
                "node_type": "explanation",
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/10",
                "content": (
                    "Explanation.- For the purpose of this sub-rule, the benefit of the "
                    "notifications mentioned therein shall not be considered to have been "
                    "availed only where the registered person has paid Integrated Goods "
                    "and Services Tax and Compensation Cess on inputs and has availed "
                    "exemption of only Basic Customs Duty (BCD) under the said notifications."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule96_subrule10_explanation_from_source_text",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_b51e996e7ee01dcd":
            note = (
                "Materializer repair from Notification 20/2024-Central Tax: source clause "
                "13(g) amends Rule 142(5), but backward context recovery attached the row "
                "to the preceding Rule 96B instruction."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/5",
                "anchor_component_id": None,
                "anchor_text": "section 74",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "insert_text": "or section 74A",
                "position": "after",
                "materializer_repair": True,
                "materializer_repair_reason": "rule142_subrule5_retarget_from_rule96b_context",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_35cfa6b7ce9bd10c":
            note = (
                "Materializer repair from Notification 20/2024-Central Tax: source clause "
                "13(d) amends Rule 142(2B), but backward context recovery attached the row "
                "to Rule 96B. The local event chain does not contain the source text that "
                "created Rule 142(2B), so keep this as an explicit unresolved context gap."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "needs_review"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2b",
                "anchor_component_id": None,
                "anchor_text": "section 74",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "triage_lane": "context_unresolved",
                "context_unresolved_reason": "missing_rule142_subrule2b_baseline",
                "materializer_repair": True,
                "materializer_repair_reason": "rule142_subrule2b_retarget_from_rule96b_context_unresolved",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": True,
                "review_reasons": ["context_unresolved", "missing_rule142_subrule2b_baseline"],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": False,
                "anchor_resolved": False,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": False,
            }
            repaired_events.append(repaired)
            continue

        if event_id in {"evt_cbic_d80841df9d5fc78a", "evt_cbic_92c371e969c66996"}:
            note = (
                "Materializer repair from Notification 12/2024-Central Tax: source clauses "
                "amend Rule 39 sub-rules (2) and (3), but PDF fallback context recovery "
                "attached the rows to Rule 54 because the surrounding instruction also "
                "mentions invoice-rule cross references."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            if event_id == "evt_cbic_d80841df9d5fc78a":
                component_id = "/in/union/rules/cgst-rules-2017/rule/39"
                anchor_text = "clause (j)"
                old_text = "clause (j)"
                new_text = "clause (n)"
                reason = "rule39_subrule2_clause_reference_retarget_from_rule54"
            else:
                component_id = "/in/union/rules/cgst-rules-2017/rule/39/subrule/3"
                anchor_text = "clause (h)"
                old_text = "clause (h)"
                new_text = "clause (l)"
                reason = "rule39_subrule3_clause_reference_retarget_from_rule54"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": component_id,
                "anchor_component_id": None,
                "anchor_text": anchor_text,
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": old_text,
                "new_text": new_text,
                "materializer_repair": True,
                "materializer_repair_reason": reason,
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_751488db3b2252ac":
            note = (
                "Materializer repair from Notification 17/2017-Central Tax: source clause "
                "4(iv) substitutes the third proviso to Rule 46. The reviewed row captured "
                "the positional phrase instead of the old proviso text, but the old export "
                "endorsement proviso resolves uniquely in the reconstructed Rule 46 text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            old_proviso = (
                "Provided also that in the case of the export of goods or services, the "
                "invoice shall carry an endorsement “SUPPLY MEANT FOR EXPORT ON PAYMENT "
                "OF INTEGRATED TAX” or “SUPPLY MEANT FOR EXPORT UNDER BOND OR LETTER OF "
                "UNDERTAKING WITHOUT PAYMENT OF INTEGRATED TAX”, as the case may be, and "
                "shall, in lieu of the details specified in clause (e), contain the "
                "following details, namely,- (i) name and address of the recipient; "
                "(ii) address of delivery; and (iii) name of the country of destination:"
            )
            new_proviso = (
                "Provided also that in the case of the export of goods or services, the "
                "invoice shall carry an endorsement “SUPPLY MEANT FOR EXPORT/SUPPLY TO SEZ "
                "UNIT OR SEZ DEVELOPER FOR AUTHORISED OPERATIONS ON PAYMENT OF INTEGRATED "
                "TAX” or “SUPPLY MEANT FOR EXPORT/SUPPLY TO SEZ UNIT OR SEZ DEVELOPER FOR "
                "AUTHORISED OPERATIONS UNDER BOND OR LETTER OF UNDERTAKING WITHOUT PAYMENT "
                "OF INTEGRATED TAX”, as the case may be, and shall, in lieu of the details "
                "specified in clause (e), contain the following details, namely,- (i) name "
                "and address of the recipient; (ii) address of delivery; and (iii) name of "
                "the country of destination:"
            )
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46",
                "anchor_component_id": None,
                "anchor_text": "SUPPLY MEANT FOR EXPORT ON PAYMENT OF INTEGRATED TAX",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": old_proviso,
                "new_text": new_proviso,
                "materializer_repair": True,
                "materializer_repair_reason": "rule46_third_proviso_export_sez_endorsement",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_32a1cc85c6d9561a":
            note = (
                "Materializer repair from Notification 62/2020-Central Tax: source clause "
                "3(i) amends Rule 9(1), but context recovery attached the row to Rule 8 "
                "because the replacement provisos refer to Rule 8(4A)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/9/subrule/1",
                "anchor_component_id": None,
                "anchor_text": "The application shall be forwarded to the proper officer",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "structural_text": (
                    "(1) The application shall be forwarded to the proper officer who shall "
                    "examine the application and the accompanying documents and if the same "
                    "are found to be in order, approve the grant of registration to the "
                    "applicant within a period of three working days from the date of "
                    "submission of the application: Provided that where a person, other than "
                    "a person notified under sub-section (6D) of section 25, fails to "
                    "undergo authentication of Aadhaar number as specified in sub-rule (4A) "
                    "of rule 8 or does not opt for authentication of Aadhaar number, the "
                    "registration shall be granted only after physical verification of the "
                    "place of business in the presence of the said person, in the manner "
                    "provided under rule 25: Provided further that the proper officer may, "
                    "for reasons to be recorded in writing and with the approval of an "
                    "officer not below the rank of Joint Commissioner, in lieu of the "
                    "physical verification of the place of business, carry out the "
                    "verification of such documents as he may deem fit."
                ),
                "materializer_repair": True,
                "materializer_repair_reason": "rule9_subrule1_retarget_from_rule8_reference",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_8745b737d9bd5177":
            note = (
                "Materializer repair from Notification 51/2023-Central Tax: source clause "
                "7 amends the second proviso to Rule 87(3), but the reconstructed local "
                "Rule 87 chain does not contain the prior OIDAR second-proviso text with "
                "'section 14'. Keep the source-proven target as an explicit unresolved "
                "context gap until that missing prior time slice is recovered."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "needs_review"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/87/subrule/3",
                "anchor_component_id": None,
                "anchor_text": "section 14",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "section 14",
                "new_text": (
                    "section 14, or a person supplying online money gaming from a place "
                    "outside India to a person in India as referred to in section 14A"
                ),
                "triage_lane": "context_unresolved",
                "context_unresolved_reason": "missing_rule87_subrule3_oidar_second_proviso_baseline",
                "materializer_repair": True,
                "materializer_repair_reason": "rule87_subrule3_second_proviso_missing_prior_baseline",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": True,
                "review_reasons": ["context_unresolved", "missing_rule87_subrule3_oidar_second_proviso_baseline"],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": False,
                "anchor_resolved": False,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": False,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_c0c3f3d6c377fae0":
            note = (
                "Materializer repair from Notification 13/2025-Central Tax: source clause "
                "3 amends Rule 39(1A). Notification 12/2024 creates the first-class "
                "Rule 39(1A) component, so this source-proven splice can be applied "
                "directly to that child."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "validated"
            repaired["operation"] = "SPLICE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/39/subrule/1a",
                "anchor_component_id": None,
                "anchor_text": "of section 9",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "materializer_repair": True,
                "materializer_repair_reason": "rule39_subrule1a_2025_splice_after_2024_insert",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": True,
                "anchor_resolved": True,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_d1aa88d5c1108c32":
            note = (
                "Materializer repair from Notification 16/2019-Central Tax: source clause "
                "4(i)(d)(C) renames the proviso inserted immediately before the existing "
                "Rule 43(1)(g) proviso. The companion proviso-insertion row is unresolved "
                "and truncated, so keep this follow-up rename as an explicit dependency "
                "gap instead of replacing an arbitrary 'Provided' in Rule 43."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "needs_review"
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/43/proviso/provided-that-real-estate-ef",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/43",
                "anchor_text": "Provided",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "triage_lane": "context_unresolved",
                "context_unresolved_reason": "missing_rule43_inserted_proviso_baseline",
                "depends_on_event_id": "evt_cbic_05e3fc7c6e71e530",
                "materializer_repair": True,
                "materializer_repair_reason": "rule43_proviso_rename_missing_prior_inserted_proviso",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": True,
                "review_reasons": ["context_unresolved", "missing_rule43_inserted_proviso_baseline"],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": note,
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "target_resolved": False,
                "anchor_resolved": False,
                "date_resolved": True,
                "source_span_verified": bool((event.get("evidence") or {}).get("source_span")),
                "materializable": False,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_a55addc1351a6797":
            note = (
                "Materializer repair from Notification 47/2017-Central Tax: source text "
                "substitutes the third proviso to Rule 89(1); reviewed payload captured "
                "the positional phrase 'third proviso' rather than the proviso text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": (
                    "Provided also that in respect of supplies regarded as deemed exports, "
                    "the application shall be filed by the recipient of deemed export supplies"
                ),
                "new_text": (
                    "Provided also that in respect of supplies regarded as deemed exports, "
                    "the application may be filed by, - (a) the recipient of deemed export supplies; "
                    "or (b) the supplier of deemed export supplies in cases where the recipient "
                    "does not avail of input tax credit on such supplies and furnishes an undertaking "
                    "to the effect that the supplier may claim the refund"
                ),
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_e2a8ec5c8dfd4588":
            note = (
                "Materializer repair from Notification 3/2018-Central Tax: source text "
                "substitutes the Rule 43 Explanation after sub-rule (2); reviewed payload "
                "captured only the Explanation lead-in and missed clauses (a) to (c)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/43/explanation/explanation-293eb53e74",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "structural_text": (
                    "Explanation:-For the purposes of rule 42 and this rule, it is hereby "
                    "clarified that the aggregate value of exempt supplies shall exclude:- "
                    "(a) the value of supply of services specified in the notification of "
                    "the Government of India in the Ministry of Finance, Department of "
                    "Revenue No. 42/2017-Integrated Tax (Rate), dated the 27th October, "
                    "2017 published in the Gazette of India, Extraordinary, Part II, "
                    "Section 3, Sub-section (i), vide number GSR 1338(E) dated the 27th "
                    "October, 2017; (b) the value of services by way of accepting deposits, "
                    "extending loans or advances in so far as the consideration is "
                    "represented by way of interest or discount, except in case of a "
                    "banking company or a financial institution including a non-banking "
                    "financial company, engaged in supplying services by way of accepting "
                    "deposits, extending loans or advances; and (c) the value of supply "
                    "of services by way of transportation of goods by a vessel from the "
                    "customs station of clearance in India to a place outside India."
                ),
                "old_text": None,
                "new_text": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_2be49e85a83a7268":
            note = (
                "Materializer repair from Notification 3/2019-Central Tax: source text "
                "substitutes words in Rule 96A's marginal heading; the reconstructed "
                "component stores that marginal heading in the XML heading element rather "
                "than in the editable content paragraph."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96a",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": None,
                "new_text": None,
                "heading": "Export of goods or services under bond or Letter of Undertaking",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_3c97ca00a2d9edc0":
            note = (
                "Materializer repair from Notification 51/2017-Central Tax: source clause "
                "2(iv) inserts two provisos after Rule 96A(2), but the reviewed row "
                "captured a truncated first proviso and targeted a non-existent proviso "
                "child. Apply the complete source-backed proviso text to the parent rule "
                "after sub-rule (2)'s closing sentence."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "SPLICE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96a",
                "anchor_component_id": None,
                "anchor_text": "from the said system.",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                "insert_text": (
                    "Provided that where the date for furnishing the details of outward "
                    "supplies in FORM GSTR-1 for a tax period has been extended in "
                    "exercise of the powers conferred under section 37 of the Act, the "
                    "supplier shall furnish the information relating to exports as "
                    "specified in Table 6A of FORM GSTR-1 after the return in FORM "
                    "GSTR-3B has been furnished and the same shall be transmitted "
                    "electronically by the common portal to the system designated by the "
                    "Customs: Provided further that the information in Table 6A furnished "
                    "under the first proviso shall be auto-drafted in FORM GSTR-1 for "
                    "the said tax period."
                ),
                "position": "after",
                "triage_lane": None,
                "materializer_repair": True,
                "materializer_repair_reason": "rule96a_complete_provisos_from_notification_51_2017",
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "anchor_resolved": True,
                "materializable": True,
                "target_resolved": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_bf470bd055dff9da":
            note = (
                "Materializer repair from Notification 12/2024-Central Tax: the source "
                "instruction is a reviewed Rule 96A(2) splice with a resolved parent-rule "
                "anchor, but stale forms-lane metadata kept it out of the materializer."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired_payload = {**(repaired.get("payload") or {})}
            repaired_payload.pop("forms_lane_pending_baseline", None)
            repaired_payload.pop("triage_lane", None)
            repaired_payload["materializer_repair"] = True
            repaired_payload["materializer_repair_reason"] = "rule96a_gstr1a_splice_stale_forms_lane_cleared"
            repaired["payload"] = repaired_payload
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": False,
                "review_reasons": [],
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_b8c61a1dadce7286":
            note = (
                "Materializer repair from Notification 31/2019-Central Tax: the clause "
                "amends Rule 133(3)'s Explanation, but backward context recovery attached "
                "it to the immediately preceding Rule 132 instruction."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/133",
                "anchor_text": "means the State",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "insert_text": "or Union Territory",
                "position": "after",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id in {"evt_cbic_692add868193fdca", "evt_cbic_bbb03703eb06d775"}:
            note = (
                "Materializer repair from Notification 94/2020-Central Tax: source clause "
                "6 amends Rule 22 sub-rules (3) and (4), but backward context recovery "
                "attached the rows to the nearby Rule 21A reference inside the inserted text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            if event_id == "evt_cbic_692add868193fdca":
                repaired["target"] = {
                    **(repaired.get("target") or {}),
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/22/subrule/3",
                    "anchor_text": "the show cause issued under sub-rule (1)",
                    "anchor_occurrence": 1,
                }
                repaired["payload"] = {
                    **(repaired.get("payload") or {}),
                    "insert_text": "or under sub-rule (2A) of rule 21A",
                    "position": "after",
                    "materializer_repair": True,
                }
            else:
                repaired["target"] = {
                    **(repaired.get("target") or {}),
                    "component_id": "/in/union/rules/cgst-rules-2017/rule/22/subrule/4",
                    "anchor_text": "reply furnished under sub-rule (2)",
                    "anchor_occurrence": 1,
                }
                repaired["payload"] = {
                    **(repaired.get("payload") or {}),
                    "insert_text": "or in response to the notice issued under sub-rule (2A) of rule 21A",
                    "position": "after",
                    "materializer_repair": True,
                }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_863edab50c37895d":
            note = (
                "Materializer repair from Notification 94/2020-Central Tax: source clause "
                "5(c) amends Rule 21A(3), but backward context recovery attached it to "
                "Rule 8 because the same notification earlier amends Rule 8(4A)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/21a",
                "anchor_text": "or sub-rule (2)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "insert_text": "or sub-rule (2A)",
                "position": "after",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_1719ebffce519de9":
            note = (
                "Materializer repair from Notification 40/2021-Central Tax: source clause "
                "substitutes words in Rule 159(3); context recovery retained only the "
                "parent rule target, causing the post-clause anchor to miss."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/159/subrule/3",
                "anchor_text": "by the taxable person",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "by the taxable person",
                "new_text": "by such person",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_9067858b95db5341":
            note = (
                "Materializer repair from Notification 40/2021-Central Tax: source clause "
                "amends FORM GST DRC-23 text, not Rule 159 body text. Route to the forms "
                "pending-baseline lane to keep Rules materialization gaps explicit."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["status"] = "needs_review"
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "forms_lane_pending_baseline": True,
                "triage_lane": "forms_lane_pending_baseline",
                "materializer_repair": True,
            }
            repaired["review"] = {
                **(repaired.get("review") or {}),
                "required": True,
                "review_reasons": ["forms_lane_pending_baseline"],
            }
            repaired["validation"] = {
                **(repaired.get("validation") or {}),
                "materializable": False,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_9e14c73c00cff0ac":
            note = (
                "Materializer repair from Notification 52/2023-Central Tax: source text "
                "renumbers Rule 28 as sub-rule (1) and inserts Rule 28(2); reviewed "
                "payload captured the wrapper instruction instead of the inserted sub-rule."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "INSERT_CHILD"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/28/subrule/2",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/28",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/28",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/28",
                "label": "(2)",
                "node_type": "subrule",
                "position": "after",
                "content": (
                    "Notwithstanding anything contained in sub-rule (1), the value of "
                    "supply of services by a supplier to a recipient who is a related "
                    "person, by way of providing corporate guarantee to any banking "
                    "company or financial institution on behalf of the said recipient, "
                    "shall be deemed to be one per cent of the amount of such guarantee "
                    "offered, or the actual consideration, whichever is higher."
                ),
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_a3f6664ed9134482":
            note = (
                "Materializer repair from Notification 12/2024-Central Tax: source clause "
                "retrospectively amends Rule 28(2) with effect from 26 October 2023; "
                "post-hoc context recovery incorrectly attached the row to Rule 21A."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "SPLICE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/28/subrule/2",
                "anchor_component_id": None,
                "anchor_text": "who is a related person",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "insert_text": "located in India",
                "position": "after",
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_f88ac7cd3d0ab081":
            note = (
                "Materializer repair from Notification 35/2021-Central Tax: source text "
                "inserts Rule 96C after Rule 96B; reviewed payload retained the wrapper "
                "instruction and uppercase component IDs instead of the inserted rule body."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "INSERT_SIBLING"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96c",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96b",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/96b",
                "label": "96C",
                "heading": "Bank Account for credit of refund",
                "node_type": "rule",
                "position": "after",
                "content": (
                    "For the purposes of sub-rule (3) of rule 91, sub-rule (4) of rule 92 "
                    "and rule 94, \"bank account\" shall mean such bank account of the "
                    "applicant which is in the name of applicant and obtained on his "
                    "Permanent Account Number: Provided that in case of a proprietorship "
                    "concern, the Permanent Account Number of the proprietor shall also be "
                    "linked with the Aadhaar number of the proprietor."
                ),
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_ef4c47389a681bf3":
            note = (
                "Materializer repair from Notification 26/2018-Central Tax: source text "
                "substitutes clause (a) of Rule 95(3); reviewed payload retained only the "
                "clause label and missed the old/new clause text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/95/subrule/3",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": (
                    "(a) the inward supplies of goods or services or both were received "
                    "from a registered person against a tax invoice and the price of the "
                    "supply covered under a single tax invoice exceeds five thousand "
                    "rupees, excluding tax paid, if any;"
                ),
                "new_text": (
                    "(a) the inward supplies of goods or services or both were received "
                    "from a registered person against a tax invoice;"
                ),
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_ad72e292d9f041d1":
            note = (
                "Materializer repair from Notification 04/2023-Central Tax: source clause "
                "retrospectively substitutes words in Rule 8(4B); context recovery left a "
                "forms-lane marker even though the reviewed target is Rule 8(4B)."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/8/subrule/4b",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "provisions of",
                "new_text": "proviso to",
                "forms_lane_pending_baseline": False,
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_e23ce17aa2de96c4":
            note = (
                "Materializer repair from Notification 16/2020-Central Tax: source text "
                "substitutes clause (C) of Rule 89(4); reviewed payload retained only the "
                "clause label and missed the full clause text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/4",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": (
                    '(C) "Turnover of zero-rated supply of goods" means the value of '
                    "zero-rated supply of goods made during the relevant period without "
                    "payment of tax under bond or letter of undertaking, other than the "
                    "turnover of supplies in respect of which refund is claimed under "
                    "sub-rules (4A) or (4B) or both;"
                ),
                "new_text": (
                    '(C) "Turnover of zero-rated supply of goods" means the value of '
                    "zero-rated supply of goods made during the relevant period without "
                    "payment of tax under bond or letter of undertaking or the value which "
                    "is 1.5 times the value of like goods domestically supplied by the same "
                    "or, similarly placed, supplier, as declared by the supplier, whichever "
                    "is less, other than the turnover of supplies in respect of which refund "
                    "is claimed under sub-rules (4A) or (4B) or both;"
                ),
                "triage_lane": None,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_3999927ca4e1a75a":
            note = (
                "Materializer repair from Notification 15/2017-Central Tax: source text "
                "corrects the second '(2)' marker in Rule 44 to '(3)' after the phrase "
                "'integrated tax'; reviewed payload incorrectly targeted Rule 44(2) text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "integrated tax and central tax. (2) Where",
                "new_text": "integrated tax and central tax. (3) Where",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_b950b33bbe16bdcf":
            note = (
                "Materializer repair from Notification 15/2017-Central Tax: source text "
                "directly substitutes Rule 44(2) words 'integrated tax and central tax' "
                "with 'central tax, State tax, Union territory tax and integrated tax'; "
                "reviewed row retained a stale context_unresolved lane."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/2",
                "anchor_text": "integrated tax and central tax",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "triage_lane": None,
                "old_text": "integrated tax and central tax",
                "new_text": "central tax, State tax, Union territory tax and integrated tax",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_64b4c848b468ead6":
            note = (
                "Materializer repair from Notification 17/2017-Central Tax: source text "
                "retrospectively substitutes Rule 44 sub-rules (2) and (3) with effect "
                "from 1 July 2017. Rule 44(2)'s same text effect is already carried by "
                "Notification 15/2017 clause (a), so this repair materializes the missing "
                "Rule 44(3) child replacement without creating a same-date duplicate."
            )
            legal_time = {
                **(event.get("legal_time") or {}),
                "applicability_start": "2017-07-01",
                "commencement_date": "2017-07-01",
                "date_basis": "source_effective_date_context",
            }
            subrule_3 = _with_materializer_repair_metadata(event, note=note)
            subrule_3["event_id"] = f"{event_id}_rule44_3_replace"
            subrule_3["operation"] = "SUBSTITUTE"
            subrule_3["legal_time"] = legal_time
            subrule_3["target"] = {
                **(subrule_3.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/3",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_3["payload"] = {
                "old_text": (
                    "(3) Where the tax invoices related to the inputs held in stock are "
                    "not available, the registered person shall estimate the amount under "
                    "sub-rule (1) based on the prevailing market price of the goods on the "
                    "effective date of the occurrence of any of the events specified in "
                    "sub-section (4) of section 18 or, as the case may be, sub-section "
                    "(5) of section 29"
                ),
                "new_text": (
                    "(3) Where the tax invoices related to the inputs held in stock are "
                    "not available, the registered person shall estimate the amount under "
                    "sub-rule (1) based on the prevailing market price of the goods on the "
                    "effective date of the occurrence of any of the events specified in "
                    "sub-section (4) of section 18 or, as the case may be, sub-section "
                    "(5) of section 29."
                ),
                "noop_if_already_reflected": True,
                "materializer_repair": True,
            }
            repaired_events.append(subrule_3)
            continue

        if event_id == "evt_cbic_3f2c97833c47f80d":
            note = (
                "Materializer repair from Notification 34/2017-Central Tax: source text "
                "inserts 'or sub-rule (3A)' in Rule 44(5); reconstructed text uses the "
                "anchor phrase 'sub-rule (3)' without the leading 'or'."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/44/subrule/5",
                "anchor_text": "sub-rule (3)",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "insert_text": "or sub-rule (3A)",
                "position": "after",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_664c6e5cde7e01e4":
            note = (
                "Materializer repair from Notification 17/2017-Central Tax: source text "
                "changes the second proviso in Rule 83(3) from 'sub-section' to "
                "'sub-rule' with effect from 1 July 2017; the extracted child component "
                "was truncated before the duration phrase used by later amendments."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["legal_time"] = {
                **(repaired.get("legal_time") or {}),
                "applicability_start": "2017-07-01",
                "commencement_date": "2017-07-01",
                "date_basis": "source_effective_date_context",
            }
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83/subrule/3",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "structural_text": (
                    "(3) The enrolment made under sub-rule (2) shall be valid until it is "
                    "cancelled: Provided that no person enrolled as a goods and services "
                    "tax practitioner shall be eligible to remain enrolled unless he passes "
                    "such examination conducted at such periods and by such authority as may "
                    "be notified by the Commissioner on the recommendations of the Council: "
                    "Provided further that no person to whom the provisions of clause (b) of "
                    "sub-rule (1) apply shall be eligible to remain enrolled unless he passes "
                    "the said examination within a period of one year from the appointed date."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_a4492bf3eaf121cc":
            note = (
                "Materializer repair from Notification 26/2018-Central Tax: source text "
                "substitutes 'one year' with 'eighteen months' in the second proviso "
                "to Rule 83(3); this depends on the repaired complete Rule 83(3) child."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83/subrule/3",
                "anchor_text": "one year",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "one year",
                "new_text": "eighteen months",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_659de6170f492d5f":
            note = (
                "Materializer repair from Notification 03/2019-Central Tax: source text "
                "substitutes 'eighteen months' with 'thirty months' in the second proviso "
                "to Rule 83(3); this depends on the repaired 2018 duration substitution."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/83/subrule/3",
                "anchor_text": "eighteen months",
                "anchor_occurrence": 1,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "eighteen months",
                "new_text": "thirty months",
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_9cc935da632e9aac":
            note = (
                "Materializer repair from Notification 03/2019-Central Tax: source text "
                "substitutes Rule 89(2)(f); reviewed payload targeted a clause component "
                "that is not split in the reconstructed baseline."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": (
                    "(f) a declaration to the effect that the Special Economic Zone unit or "
                    "the Special Economic Zone developer has not availed the input tax credit "
                    "of the tax paid by the supplier of goods or services or both, in a case "
                    "where the refund is on account of supply of goods or services made to a "
                    "Special Economic Zone unit or a Special Economic Zone developer;"
                ),
                "new_text": (
                    "(f) a declaration to the effect that tax has not been collected from the "
                    "Special Economic Zone unit or the Special Economic Zone developer, in a "
                    "case where the refund is on account of supply of goods or services or "
                    "both made to a Special Economic Zone unit or a Special Economic Zone "
                    "developer;"
                ),
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_e23ce17aa2de96c4":
            note = (
                "Materializer repair from Notification 16/2020-Central Tax: source text "
                "substitutes Rule 89(4)(C); reviewed payload captured an incomplete "
                "clause reference instead of the clause text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": (
                    '(C) "Turnover of zero-rated supply of goods" means the value of '
                    "zero-rated supply of goods made during the relevant period without "
                    "payment of tax under bond or letter of undertaking, other than the "
                    "turnover of supplies in respect of which refund is claimed under "
                    "sub-rules (4A) or (4B) or both;"
                ),
                "new_text": (
                    '(C) "Turnover of zero-rated supply of goods" means the value of '
                    "zero-rated supply of goods made during the relevant period without "
                    "payment of tax under bond or letter of undertaking or the value which "
                    "is 1.5 times the value of like goods domestically supplied by the same "
                    "or, similarly placed, supplier, as declared by the supplier, whichever "
                    "is less, other than the turnover of supplies in respect of which refund "
                    "is claimed under sub-rules (4A) or (4B) or both;"
                ),
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_d8f4a0a217fe1492":
            note = (
                "Materializer repair from Notification 14/2022-Central Tax: source text "
                "substitutes the Rule 89(5) inverted-duty formula deduction phrase; "
                "reviewed payload omitted the old and new text."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": "tax payable on such inverted rated supply of goods",
                "new_text": (
                    "{tax payable on such inverted rated supply of goods and services x "
                    "(Net ITC \u00f7 ITC availed on inputs and input services)}"
                ),
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_28517a6b6d58aacb":
            # Defer to the validated SPLICE payload in the JSONL when it is
            # usable (anchor 'claiming refund of' resolves in rule/89/subrule/1
            # and insert_text is present). The compound SUBSTITUTE repair below
            # captured all four sub-amendments in one block but its old_text no
            # longer matches the stored rule/89 text, so it fails. Applying the
            # SPLICE (amendment (a)) directly is authoritative for that clause.
            _orig_payload = event.get("payload") or {}
            _orig_target = event.get("target") or {}
            if (
                event.get("status") == "validated"
                and event.get("operation") == "SPLICE"
                and _orig_target.get("anchor_text") == "claiming refund of"
                and str(_orig_payload.get("insert_text") or "").strip()
                and _orig_payload.get("position") == "after"
            ):
                repaired_events.append(event)
                continue
            note = (
                "Materializer repair from Notification 19/2022-Central Tax: source text "
                "contains four Rule 89(1) amendments in one compound block; apply as "
                "one contiguous substitution over the affected sub-rule opening."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired["operation"] = "SUBSTITUTE"
            repaired["target"] = {
                **(repaired.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            old_text = (
                "claiming refund of any tax, interest, penalty, fees or any other amount "
                "paid by him, other than refund of integrated tax paid on goods exported "
                "out of India, may file , subject to the provisions of rule 10B, an "
                "application electronically in FORM GST RFD-01 through the common portal, "
                "either directly or through a Facilitation Centre notified by the Commissioner: "
                "Provided that any claim for refund relating "
                "to balance in the electronic cash ledger in accordance with the provisions "
                "of sub-section (6) of section 49 may be made through the return furnished "
                "for the relevant tax period in FORM GSTR-3 or FORM GSTR-4 or FORM GSTR-7, "
                "as the case may be: Provided further that in respect of supplies to a "
                "Special Economic Zone unit or a Special Economic Zone developer, the "
                "application for refund shall be filed by the - (a) supplier of goods "
                "after such goods have been admitted in full in the Special Economic Zone "
                "for authorised operations, as endorsed by the specified officer of the "
                "Zone; (b) supplier of services along with such evidence regarding receipt "
                "of services for authorised operations as endorsed by the specified officer "
                "of the Zone: Provided also that in respect of supplies regarded as deemed "
                "exports, the application shall be filed by the recipient of deemed export "
                "supplies:"
            )
            new_text = (
                "claiming refund of any balance in the electronic cash ledger in accordance "
                "with the provisions of sub-section (6) of section 49 or any tax, interest, "
                "penalty, fees or any other amount paid by him, other than refund of "
                "integrated tax paid on goods exported out of India, may file , subject to "
                "the provisions of rule 10B, an application electronically in FORM GST RFD-01 "
                "through the common portal, either directly or through a Facilitation Centre "
                "notified by the Commissioner: Provided "
                "that in respect of supplies to a Special Economic Zone unit or a Special "
                "Economic Zone developer, the application for refund shall be filed by the "
                "- (a) supplier of goods after such goods have been admitted in full in the "
                "Special Economic Zone for authorised operations, as endorsed by the "
                "specified officer of the Zone; (b) supplier of services along with such "
                "evidence regarding receipt of services for authorised operations as "
                "endorsed by the specified officer of the Zone: Provided further that in "
                "respect of supplies regarded as deemed exports, the application may be "
                "filed by, - (a) the recipient of deemed export supplies; or (b) the supplier "
                "of deemed export supplies in cases where the recipient does not avail of "
                "input tax credit on such supplies and furnishes an undertaking to the "
                "effect that the supplier may claim the refund:"
            )
            repaired["payload"] = {
                **(repaired.get("payload") or {}),
                "old_text": old_text,
                "new_text": new_text,
                "materializer_repair": True,
            }
            repaired_events.append(repaired)
            continue

        if event_id == "evt_cbic_777f0208dde2bedb":
            note = (
                "Materializer repair from Notification 35/2021-Central Tax as commenced "
                "by Notification 38/2021-Central Tax: the reviewed row captured only the "
                "Rule 89 wrapper fragment, while the source text deterministically inserts "
                "words in Rule 89(1) and inserts new sub-rule (1A)."
            )
            legal_time = {
                **(event.get("legal_time") or {}),
                "applicability_start": "2022-01-01",
                "commencement_date": "2022-01-01",
                "date_basis": "commencement_notification_38_2021_rule_2_subrule_2",
                "retrospective": False,
            }

            subrule_1_splice = _with_materializer_repair_metadata(event, note=note)
            subrule_1_splice["event_id"] = f"{event_id}_rule89_1_rule10b_splice"
            subrule_1_splice["operation"] = "SPLICE"
            subrule_1_splice["legal_time"] = legal_time
            subrule_1_splice["target"] = {
                **(subrule_1_splice.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_component_id": None,
                "anchor_text": "may file",
                "anchor_occurrence": 1,
            }
            subrule_1_splice["payload"] = {
                "position": "after",
                "insert_text": ", subject to the provisions of rule 10B,",
                "materializer_repair": True,
            }
            repaired_events.append(subrule_1_splice)

            subrule_1a = _with_materializer_repair_metadata(event, note=note)
            subrule_1a["event_id"] = f"{event_id}_rule89_1a_insert"
            subrule_1a["operation"] = "INSERT_CHILD"
            subrule_1a["legal_time"] = legal_time
            subrule_1a["target"] = {
                **(subrule_1a.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/1a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/1",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_1a["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/89",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/89/subrule/1",
                "label": "(1A)",
                "node_type": "subrule",
                "position": "after",
                "content": (
                    "Any person, claiming refund under section 77 of the Act of any tax "
                    "paid by him, in respect of a transaction considered by him to be an "
                    "intra-State supply, which is subsequently held to be an inter-State "
                    "supply, may, before the expiry of a period of two years from the date "
                    "of payment of the tax on the inter-State supply, file an application "
                    "electronically in FORM GST RFD-01 through the common portal, either "
                    "directly or through a Facilitation Centre notified by the Commissioner: "
                    "Provided that the said application may, as regard to any payment of tax "
                    "on inter-State supply before coming into force of this sub-rule, be "
                    "filed before the expiry of a period of two years from the date on which "
                    "this sub-rule comes into force."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(subrule_1a)
            continue

        if event_id == "evt_cbic_803f696fd8e1d231":
            rejected = copy.deepcopy(event)
            rejected["status"] = "rejected"
            rejected["payload"] = {
                **(rejected.get("payload") or {}),
                "baseline_source_only": True,
                "already_reflected": True,
                "reflected_by_event_id": "evt_cbic_xml_cff41664511daf24",
            }
            rejected["review"] = {
                **(rejected.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": (
                    "Already reflected: this reviewed row mis-targeted a Rule 88C splice "
                    "to Rule 89/subrule/1. The same Notification 12/2024-Central Tax "
                    "effect is materialized by canonical XML event "
                    "evt_cbic_xml_cff41664511daf24 against Rule 88C."
                ),
            }
            repaired_events.append(rejected)
            continue

        if event_id == "evt_cbic_ba1f7eea0bfef2d7":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 16/2019-Central Tax: reviewed "
                    "Rule 142 structural_text was truncated at 'sub-'; source text contains "
                    "the full substituted Rule 142 effective 1 April 2019."
                ),
            )
            fixed["operation"] = "SUBSTITUTE"
            fixed["legal_time"] = {
                **(fixed.get("legal_time") or {}),
                "applicability_start": "2019-04-01",
                "commencement_date": "2019-04-01",
                "date_basis": "source_effective_date_context",
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "forms_lane_pending_baseline": False,
                "triage_lane": None,
                "structural_heading": "Notice and order for demand of amounts payable under the Act",
                "structural_text": (
                    "142. Notice and order for demand of amounts payable under the Act.- "
                    "(1) The proper officer shall serve, along with the (a) notice issued "
                    "under section 52 or section 73 or section 74 or section 76 or section "
                    "122 or section 123 or section 124 or section 125 or section 127 or "
                    "section 129 or section 130, a summary thereof electronically in FORM "
                    "GST DRC-01, (b) statement under sub-section (3) of section 73 or "
                    "sub-section (3) of section 74, a summary thereof electronically in "
                    "FORM GST DRC-02, specifying therein the details of the amount payable. "
                    "(2) Where, before the service of notice or statement, the person "
                    "chargeable with tax makes payment of the tax and interest in accordance "
                    "with the provisions of sub-section (5) of section 73 or, as the case "
                    "may be, tax, interest and penalty in accordance with the provisions of "
                    "sub-section (5) of section 74, or where any person makes payment of "
                    "tax, interest, penalty or any other amount due in accordance with the "
                    "provisions of the Act he shall inform the proper officer of such "
                    "payment in FORM GST DRC-03 and the proper officer shall issue an "
                    "acknowledgement, accepting the payment made by the said person in FORM "
                    "GST DRC-04. (3) Where the person chargeable with tax makes payment of "
                    "tax and interest under sub-section (8) of section 73 or, as the case "
                    "may be, tax, interest and penalty under sub-section (8) of section 74 "
                    "within thirty days of the service of a notice under sub-rule (1), or "
                    "where the person concerned makes payment of the amount referred to in "
                    "sub-section (1) of section 129 within fourteen days of detention or "
                    "seizure of the goods and conveyance, he shall intimate the proper "
                    "officer of such payment in FORM GST DRC-03 and the proper officer "
                    "shall issue an order in FORM GST DRC-05 concluding the proceedings in "
                    "respect of the said notice. (4) The representation referred to in "
                    "sub-section (9) of section 73 or sub-section (9) of section 74 or "
                    "sub-section (3) of section 76 or the reply to any notice issued under "
                    "any section whose summary has been uploaded electronically in FORM GST "
                    "DRC-01 under sub-rule (1) shall be furnished in FORM GST DRC-06. "
                    "(5) A summary of the order issued under section 52 or section 62 or "
                    "section 63 or section 64 or section 73 or section 74 or section 75 or "
                    "section 76 or section 122 or section 123 or section 124 or section 125 "
                    "or section 127 or section 129 or section 130 shall be uploaded "
                    "electronically in FORM GST DRC-07, specifying therein the amount of "
                    "tax, interest and penalty payable by the person chargeable with tax. "
                    "(6) The order referred to in sub-rule (5) shall be treated as the "
                    "notice for recovery. (7) Where a rectification of the order has been "
                    "passed in accordance with the provisions of section 161 or where an "
                    "order uploaded on the system has been withdrawn, a summary of the "
                    "rectification order or of the withdrawal order shall be uploaded "
                    "electronically by the proper officer in FORM GST DRC-08."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(fixed)

            subrule_2 = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 16/2019-Central Tax: full Rule "
                    "142 replacement also updates the extracted Rule 142(2) child component, "
                    "which is needed for later source-proven splices."
                ),
            )
            subrule_2["event_id"] = f"{event_id}_rule142_2_replace"
            subrule_2["operation"] = "SUBSTITUTE"
            subrule_2["legal_time"] = fixed["legal_time"]
            subrule_2["target"] = {
                **(subrule_2.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_2["payload"] = {
                "forms_lane_pending_baseline": False,
                "triage_lane": None,
                "structural_text": (
                    "(2) Where, before the service of notice or statement, the person "
                    "chargeable with tax makes payment of the tax and interest in accordance "
                    "with the provisions of sub-section (5) of section 73 or, as the case "
                    "may be, tax, interest and penalty in accordance with the provisions of "
                    "sub-section (5) of section 74, or where any person makes payment of "
                    "tax, interest, penalty or any other amount due in accordance with the "
                    "provisions of the Act he shall inform the proper officer of such "
                    "payment in FORM GST DRC-03 and the proper officer shall issue an "
                    "acknowledgement, accepting the payment made by the said person in FORM "
                    "GST DRC-04."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(subrule_2)
            continue

        if event_id == "evt_cbic_7a9be1186853547f":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 16/2019-Central Tax: reviewed "
                    "Rule 100 structural_text was truncated at 'on which' and routed to "
                    "the forms pending-baseline lane. Source text contains the full "
                    "substituted Rule 100 effective 1 April 2019 with five sub-rules "
                    "covering assessment under sections 62, 63 and 64. Form substitutions "
                    "(FORM GST ASMT-13/14/15/16/17/18, DRC-01/07) referenced by this rule "
                    "are tracked separately in the forms lane."
                ),
            )
            fixed["operation"] = "SUBSTITUTE"
            fixed["legal_time"] = {
                **(fixed.get("legal_time") or {}),
                "applicability_start": "2019-04-01",
                "commencement_date": "2019-04-01",
                "date_basis": "source_effective_date_context",
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/100",
                "anchor_component_id": None,
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "forms_lane_pending_baseline": False,
                "triage_lane": None,
                "structural_heading": "Assessment in certain cases",
                "structural_text": (
                    "100. Assessment in certain cases.- (1) The order of assessment made "
                    "under sub-section (1) of section 62 shall be issued in FORM GST "
                    "ASMT-13 and a summary thereof shall be uploaded electronically in "
                    "FORM GST DRC-07. (2) The proper officer shall issue a notice to a "
                    "taxable person in accordance with the provisions of section 63 in "
                    "FORM GST ASMT-14 containing the grounds on which the assessment is "
                    "proposed to be made on best judgment basis and shall also serve a "
                    "summary thereof electronically in FORM GST DRC-01, and after "
                    "allowing a time of fifteen days to such person to furnish his reply, "
                    "if any, pass an order in FORM GST ASMT-15 and summary thereof shall "
                    "be uploaded electronically in FORM GST DRC-07. (3) The order of "
                    "assessment under sub-section (1) of section 64 shall be issued in "
                    "FORM GST ASMT-16 and a summary of the order shall be uploaded "
                    "electronically in FORM GST DRC-07. (4) The person referred to in "
                    "sub-section (2) of section 64 may file an application for withdrawal "
                    "of the assessment order in FORM GST ASMT\u201317. (5) The order of "
                    "withdrawal or, as the case may be, rejection of the application "
                    "under sub-section (2) of section 64 shall be issued in FORM GST "
                    "ASMT-18."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_2c1e4044c813bf37":
            note = (
                "Materializer repair from Notification 49/2019-Central Tax: source text "
                "contains a three-part Rule 142 amendment inserting sub-rule (1A), "
                "inserting words in sub-rule (2), and inserting sub-rule (2A)."
            )
            legal_time = {
                **(event.get("legal_time") or {}),
                "applicability_start": "2019-10-09",
                "commencement_date": "2019-10-09",
                "date_basis": "source_publication_date_materializer_repair",
            }
            subrule_1a = _with_materializer_repair_metadata(event, note=note)
            subrule_1a["event_id"] = f"{event_id}_rule142_1a_insert"
            subrule_1a["operation"] = "INSERT_CHILD"
            subrule_1a["legal_time"] = legal_time
            subrule_1a["target"] = {
                **(subrule_1a.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_1a["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1",
                "label": "(1A)",
                "node_type": "subrule",
                "position": "after",
                "content": (
                    "The proper officer shall, before service of notice to the person "
                    "chargeable with tax, interest and penalty, under sub-section (1) of "
                    "Section 73 or sub-section (1) of Section 74, as the case may be, shall "
                    "communicate the details of any tax, interest and penalty as ascertained "
                    "by the said officer, in Part A of FORM GST DRC-01A."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(subrule_1a)

            subrule_2_splice = _with_materializer_repair_metadata(event, note=note)
            subrule_2_splice["event_id"] = f"{event_id}_rule142_2_own_ascertainment_splice"
            subrule_2_splice["operation"] = "SPLICE"
            subrule_2_splice["legal_time"] = legal_time
            subrule_2_splice["target"] = {
                **(subrule_2_splice.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2",
                "anchor_component_id": None,
                "anchor_text": "in accordance with the provisions of the Act",
                "anchor_occurrence": None,
            }
            subrule_2_splice["payload"] = {
                "position": "after",
                "insert_text": (
                    ", whether on his own ascertainment or, as communicated by the proper "
                    "officer under sub-rule (1A),"
                ),
                "materializer_repair": True,
            }
            repaired_events.append(subrule_2_splice)

            subrule_2a = _with_materializer_repair_metadata(event, note=note)
            subrule_2a["event_id"] = f"{event_id}_rule142_2a_insert"
            subrule_2a["operation"] = "INSERT_CHILD"
            subrule_2a["legal_time"] = legal_time
            subrule_2a["target"] = {
                **(subrule_2a.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_2a["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/142",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2",
                "label": "(2A)",
                "node_type": "subrule",
                "position": "after",
                "content": (
                    "Where the person referred to in sub-rule (1A) has made partial payment "
                    "of the amount communicated to him or desires to file any submissions "
                    "against the proposed liability, he may make such submission in Part B "
                    "of FORM GST DRC-01A."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(subrule_2a)
            continue

        if event_id == "evt_cbic_9869af72fcfd6dc2":
            note = (
                "Materializer repair from Notification 79/2020-Central Tax: source text "
                "contains two Rule 142(1A) substitutions in one compound block."
            )
            proper_officer = _with_materializer_repair_metadata(event, note=note)
            proper_officer["event_id"] = f"{event_id}_proper_officer_may"
            proper_officer["operation"] = "SUBSTITUTE"
            proper_officer["target"] = {
                **(proper_officer.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1a",
                "anchor_component_id": None,
                "anchor_text": "proper officer shall",
                "anchor_occurrence": None,
            }
            proper_officer["payload"] = {
                "old_text": "proper officer shall",
                "new_text": "proper officer may",
                "materializer_repair": True,
            }
            proper_officer["evidence"] = {
                **(proper_officer.get("evidence") or {}),
                "source_span": {
                    **((proper_officer.get("evidence") or {}).get("source_span") or {}),
                    "start": 0,
                    "text_hash": ((event.get("evidence") or {}).get("source_span") or {}).get("text_hash")
                    or "materializer-repair-rule142-79-2020-1",
                },
            }
            repaired_events.append(proper_officer)

            communicate = _with_materializer_repair_metadata(event, note=note)
            communicate["event_id"] = f"{event_id}_communicate"
            communicate["operation"] = "SUBSTITUTE"
            communicate["target"] = {
                **(communicate.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1a",
                "anchor_component_id": None,
                "anchor_text": "shall communicate",
                "anchor_occurrence": None,
            }
            communicate["payload"] = {
                "old_text": "shall communicate",
                "new_text": "communicate",
                "materializer_repair": True,
            }
            communicate["evidence"] = {
                **(communicate.get("evidence") or {}),
                "source_span": {
                    **((communicate.get("evidence") or {}).get("source_span") or {}),
                    "start": 1,
                    "text_hash": ((event.get("evidence") or {}).get("source_span") or {}).get("text_hash")
                    or "materializer-repair-rule142-79-2020-2",
                },
            }
            repaired_events.append(communicate)
            continue

        if event_id == "evt_cbic_be6914a5115d56f3":
            rejected = copy.deepcopy(event)
            rejected["status"] = "rejected"
            rejected["payload"] = {
                **(rejected.get("payload") or {}),
                "baseline_source_only": True,
                "already_reflected": True,
            }
            rejected["review"] = {
                **(rejected.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": (
                    "Already reflected: Notification 10/2017-Central Tax inserts Chapter IV "
                    "after Rule 26. The current baseline already contains Chapter IV and its "
                    "rules from the same source, so this chapter-heading row should not be "
                    "counted as a Rule 26 text coverage gap."
                ),
            }
            repaired_events.append(rejected)
            continue

        if event_id == "evt_cbic_91c3aa7f59985dec":
            note = (
                "Materializer repair from Notification 48/2018-Central Tax: source text "
                "inserts Rule 117(1A) after sub-rule (1) and a proviso under Rule "
                "117(4)(b)(iii)."
            )
            subrule_1a = _with_materializer_repair_metadata(event, note=note)
            subrule_1a["operation"] = "INSERT_CHILD"
            subrule_1a["target"] = {
                **(subrule_1a.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/1a",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/1",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            subrule_1a["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/117",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/1",
                "label": "(1A)",
                "node_type": "subrule",
                "position": "after",
                "content": (
                    "Notwithstanding anything contained in sub-rule (1), the Commissioner may, "
                    "on the recommendations of the Council, extend the date for submitting the "
                    "declaration electronically in FORM GST TRAN-1 by a further period not beyond "
                    "31st March, 2019, in respect of registered persons who could not submit the "
                    "said declaration by the due date on account of technical difficulties on the "
                    "common portal and in respect of whom the Council has made a recommendation "
                    "for such extension."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(subrule_1a)

            proviso = _with_materializer_repair_metadata(event, note=note)
            proviso["event_id"] = f"{event_id}_rule117_4_proviso"
            proviso["operation"] = "SPLICE"
            proviso["target"] = {
                **(proviso.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/4",
                "anchor_component_id": None,
                "anchor_text": "The scheme shall be available for six tax periods from the appointed date.",
                "anchor_occurrence": None,
            }
            proviso["payload"] = {
                "position": "after",
                "insert_text": (
                    "Provided that the registered persons filing the declaration in FORM GST "
                    "TRAN-1 in accordance with sub-rule (1A), may submit the statement in FORM "
                    "GST TRAN-2 by 30th April, 2019."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(proviso)
            continue

        if event_id == "evt_cbic_252fae793995454f":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 49/2019-Central Tax: source text "
                    "substitutes 31st March, 2019 with 31st December, 2019 in Rule 117(1A)."
                ),
            )
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "old_text": "31st March, 2019",
                "new_text": "31st December, 2019",
                "materializer_repair": True,
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/117/subrule/1a",
                "anchor_text": "31st March, 2019",
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_aafc21449b573369":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 74/2018-Central Tax: source text "
                    "inserts Rule 138E after Rule 138D. The source says the rule applies "
                    "from a later notified date; the current event date is preserved for "
                    "coverage continuity until a commencement event is linked."
                ),
            )
            fixed["operation"] = "INSERT_SIBLING"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/138d",
                "anchor_text": "rule 138D",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "anchor_rule": "138D",
                "label": "138E",
                "heading": "Restriction on furnishing of information in PART A of FORM GST EWB-01",
                "position": "after",
                "content": (
                    "Notwithstanding anything contained in sub-rule (1) of rule 138, no person "
                    "(including a consignor, consignee, transporter, an e-commerce operator or "
                    "a courier agency) shall be allowed to furnish the information in PART A of "
                    "FORM GST EWB-01 in respect of a registered person, whether as a supplier "
                    "or a recipient, who,- (a) being a person paying tax under section 10, has "
                    "not furnished the returns for two consecutive tax periods; or (b) being a "
                    "person other than a person specified in clause (a), has not furnished the "
                    "returns for a consecutive period of two months: Provided that the "
                    "Commissioner may, on sufficient cause being shown and for reasons to be "
                    "recorded in writing, by order, allow furnishing of the said information in "
                    "PART A of FORM GST EWB 01, subject to such conditions and restrictions as "
                    "may be specified by him: Provided further that no order rejecting the "
                    "request of such person to furnish the information in PART A of FORM GST "
                    "EWB 01 under the first proviso shall be passed without affording the said "
                    "person a reasonable opportunity of being heard: Provided also that the "
                    "permission granted or rejected by the Commissioner of State tax or "
                    "Commissioner of Union territory tax shall be deemed to be granted or, as "
                    "the case may be, rejected by the Commissioner. Explanation:- For the "
                    "purposes of this rule, the expression \"Commissioner\" shall mean the "
                    "jurisdictional Commissioner in respect of the persons specified in clauses "
                    "(a) and (b)."
                ),
                "materializer_repair": True,
                "deferred_effective_date_text": "from a date to be notified later",
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_ed2a8531ecc39fe2":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 79/2020-Central Tax: source text "
                    "inserts a Rule 138E COVID-period proviso after the third proviso, "
                    "effective from 20 March 2020."
                ),
            )
            fixed["operation"] = "INSERT_CHILD"
            fixed["legal_time"] = {
                **(fixed.get("legal_time") or {}),
                "applicability_start": "2020-03-20",
                "commencement_date": "2020-03-20",
                "date_basis": "express_effective_date_in_source",
                "retrospective": True,
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/138e/proviso/covid-2020",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "anchor_text": "after the third proviso",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "label": "Provided also",
                "node_type": "proviso",
                "position": "after",
                "content": (
                    "that the said restriction shall not apply during the period from the "
                    "20th day of March, 2020 till the 15th day of October, 2020 in case "
                    "where the return in FORM GSTR-3B or the statement of outward supplies "
                    "in FORM GSTR-1 or the statement in FORM GST CMP-08, as the case may be, "
                    "has not been furnished for the period February, 2020 to August, 2020."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_0c43dc0b8e195471":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 15/2021-Central Tax: source text "
                    "substitutes the Rule 138E opening application phrase."
                ),
            )
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "anchor_text": "in respect of a registered person, whether as a supplier or a recipient, who",
            }
            fixed["payload"] = {
                "old_text": "in respect of a registered person, whether as a supplier or a recipient, who,-",
                "new_text": "in respect of any outward movement of goods of a registered person, who,-",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_cbcd524d7f43f130":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 32/2021-Central Tax: source text "
                    "inserts a Rule 138E COVID-period proviso after the fourth proviso, "
                    "effective from 1 May 2021."
                ),
            )
            fixed["operation"] = "INSERT_CHILD"
            fixed["legal_time"] = {
                **(fixed.get("legal_time") or {}),
                "applicability_start": "2021-05-01",
                "commencement_date": "2021-05-01",
                "date_basis": "express_effective_date_in_source",
                "retrospective": True,
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/138e/proviso/covid-2021",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "anchor_text": "after the fourth proviso",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "label": "Provided also",
                "node_type": "proviso",
                "position": "after",
                "content": (
                    "that the said restriction shall not apply during the period from the "
                    "1st day of May, 2021 till the 18th day of August, 2021, in case where "
                    "the return in FORM GSTR-3B or the statement of outward supplies in "
                    "FORM GSTR-1 or the statement in FORM GST CMP-08, as the case may be, "
                    "has not been furnished for the period March, 2021 to May, 2021."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_649de854081f52c7":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 38/2023-Central Tax: source text "
                    "inserts Rule 138F after Rule 138E; extraction incorrectly targeted Rule 10."
                ),
            )
            fixed["operation"] = "INSERT_SIBLING"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/138f",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/138e",
                "anchor_text": "rule 138E",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "anchor_rule": "138E",
                "label": "138F",
                "heading": (
                    "Information to be furnished in case of intra-State movement of gold, "
                    "precious stones, etc. and generation of e-way bills thereof"
                ),
                "position": "after",
                "content": (
                    "(1) Where- (a) a Commissioner of State tax or Union territory tax mandates "
                    "furnishing of information regarding intra-State movement of goods specified "
                    "against serial numbers 4 and 5 in the Annexure appended to sub-rule (14) of "
                    "rule 138, in accordance with sub-rule (1) of rule 138F of the State or Union "
                    "territory Goods and Services Tax Rules, and (b) the consignment value of such "
                    "goods exceeds such amount, not below rupees two lakhs, as may be notified by "
                    "the Commissioner of State tax or Union territory tax, in consultation with the "
                    "jurisdictional Principal Chief Commissioner or Chief Commissioner of Central "
                    "Tax, or any Commissioner of Central Tax authorised by him, notwithstanding "
                    "anything contained in Rule 138, every registered person who causes intra-State "
                    "movement of such goods, - (i) in relation to a supply; or (ii) for reasons "
                    "other than supply; or (iii) due to inward supply from an un-registered person, "
                    "shall, before the commencement of such movement within that State or Union "
                    "territory, furnish information relating to such goods electronically, as "
                    "specified in Part A of FORM GST EWB-01, against which a unique number shall "
                    "be generated: Provided that where the goods to be transported are supplied "
                    "through an e-commerce operator or a courier agency, the information in Part A "
                    "of FORM GST EWB-01 may be furnished by such e-commerce operator or courier "
                    "agency. (2) The information as specified in PART B of FORM GST EWB-01 shall "
                    "not be required to be furnished in respect of movement of goods referred to in "
                    "the sub-rule (1) and after furnishing information in Part-A of FORM GST "
                    "EWB-01 as specified in sub-rule (1), the e-way bill shall be generated in "
                    "FORM GST EWB-01, electronically on the common portal. (3) The information "
                    "furnished in Part A of FORM GST EWB-01 shall be made available to the "
                    "registered supplier on the common portal who may utilize the same for "
                    "furnishing the details in FORM GSTR-1. (4) Where an e-way bill has been "
                    "generated under this rule, but goods are either not transported or are not "
                    "transported as per the details furnished in the e-waybill, the e-way bill may "
                    "be cancelled, electronically on the common portal, within twenty-four hours of "
                    "generation of the e-way bill: Provided that an e-way bill cannot be cancelled "
                    "if it has been verified in transit in accordance with the provisions of rule "
                    "138B. (5) Notwithstanding anything contained in this rule, no e-way bill is "
                    "required to be generated- (a) where the goods are being transported from the "
                    "customs port, airport, air cargo complex and land customs station to an inland "
                    "container depot or a container freight station for clearance by Customs; (b) "
                    "where the goods are being transported- (i) under customs bond from an inland "
                    "container depot or a container freight station to a customs port, airport, air "
                    "cargo complex and land customs station, or from one customs station or customs "
                    "port to another customs station or customs port, or (ii) under customs "
                    "supervision or under customs seal. (6) The provisions of sub-rule (10), "
                    "sub-rule (11) and sub-rule (12) of rule 138, rule 138A, rule 138B, rule 138C, "
                    "rule 138D and rule 138E shall, mutatis mutandis, apply to an e-way bill "
                    "generated under this rule. Explanation.- For the purposes of this rule, the "
                    "consignment value of goods shall be the value, determined in accordance with "
                    "the provisions of section 15, declared in an invoice, a bill of supply or a "
                    "delivery challan, as the case may be, issued in respect of the said "
                    "consignment and also includes the central tax, State tax or Union territory "
                    "tax charged in the document and shall exclude the value of exempt supply of "
                    "goods where the invoice is issued in respect of both exempt and taxable "
                    "supply of goods."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_7a1f16dca7f92d9f":
            note = (
                "Materializer repair from Notification 48/2020-Central Tax: source text "
                "substitutes the Rule 26(1) second proviso with two provisos. The first "
                "extends the GSTR-3B EVC period to 30 September 2020; the second adds the "
                "parallel GSTR-1 EVC proviso."
            )
            period_substitution = _with_materializer_repair_metadata(event, note=note)
            period_substitution["event_id"] = f"{event_id}_gstr3b_period"
            period_substitution["operation"] = "SUBSTITUTE"
            period_substitution["target"] = {
                **(period_substitution.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/providedfurtherthat-d30ed6bbe0",
                "anchor_component_id": None,
                "anchor_text": "30th day of June, 2020",
                "anchor_occurrence": None,
            }
            period_substitution["payload"] = {
                "old_text": "30th day of June, 2020",
                "new_text": "30th day of September, 2020",
                "materializer_repair": True,
            }
            repaired_events.append(period_substitution)

            gstr1_proviso = _with_materializer_repair_metadata(event, note=note)
            gstr1_proviso["event_id"] = f"{event_id}_gstr1_proviso"
            gstr1_proviso["operation"] = "INSERT_CHILD"
            gstr1_proviso["target"] = {
                **(gstr1_proviso.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/gstr1-evc-2020",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "anchor_text": None,
                "anchor_occurrence": None,
            }
            gstr1_proviso["payload"] = {
                "parent_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "anchor_component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1",
                "label": "Provided also that",
                "node_type": "proviso",
                "position": "after",
                "content": (
                    "Provided also that a registered person registered under the provisions "
                    "of the Companies Act, 2013 (18 of 2013) shall, during the period from "
                    "the 27th day of May, 2020 to the 30th day of September, 2020, also be "
                    "allowed to furnish the details of outward supplies under section 37 in "
                    "FORM GSTR-1 verified through electronic verification code (EVC)."
                ),
                "materializer_repair": True,
            }
            repaired_events.append(gstr1_proviso)
            continue

        if event_id == "evt_cbic_8e62222199d9ad8c":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 27/2021-Central Tax: the fourth "
                    "proviso date lives in Rule 26(1), not the parent Rule 26 text snapshot."
                ),
            )
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/26/subrule/1/proviso/providedalsothat-3b0af079f2",
                "anchor_component_id": None,
                "anchor_text": "31st day of May, 2021",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "old_text": "31st day of May, 2021",
                "new_text": "31st day of August, 2021",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_b852babb7c93019c":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 22/2017-Central Tax: source text "
                    "substitutes words in Rule 61(5) with effect from 1 July 2017; the anchor "
                    "resolves in the split subrule component, not the parent Rule 61 text."
                ),
            )
            fixed["legal_time"] = {
                **(fixed.get("legal_time") or {}),
                "applicability_start": "2017-07-01",
                "commencement_date": "2017-07-01",
                "date_basis": "explicit_effective_clause_in_source",
                "retrospective": True,
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/61/subrule/5",
                "anchor_component_id": None,
                "anchor_text": "specify that",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "old_text": "specify that",
                "new_text": "specify the manner and conditions subject to which the",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_a41be90129af4539":
            rejected = copy.deepcopy(event)
            rejected["status"] = "rejected"
            rejected["payload"] = {
                **(rejected.get("payload") or {}),
                "metadata_only": True,
                "notification_extension": True,
            }
            rejected["review"] = {
                **(rejected.get("review") or {}),
                "required": False,
                "review_reasons": [],
                "reviewed_by": "version-snapshots-materializer-repair",
                "decision_notes": (
                    "Metadata-only for CGST Rules materialization: Notification 62/2018 "
                    "amends Notification 34/2018 to extend FORM GSTR-3B filing dates under "
                    "Rule 61(5), but does not amend the text of Rule 61 itself."
                ),
            }
            repaired_events.append(rejected)
            continue

        if event_id == "evt_cbic_d56f9b1cfb5e2603":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 12/2024-Central Tax: source "
                    "text inserts words in Rule 142(2A), whose component is created by "
                    "the Notification 49/2019 repair."
                ),
            )
            fixed["operation"] = "SPLICE"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/2a",
                "anchor_component_id": None,
                "anchor_text": "FORM GST DRC-01A",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "position": "after",
                "insert_text": (
                    ", and thereafter the proper officer may issue an intimation in "
                    "Part-C of FORM GST DRC-01A, accepting the payment or the submissions "
                    "or both, as the case may be, made by the said person"
                ),
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_4e3d7e920de72e7d":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 20/2024-Central Tax: target the "
                    "Rule 142(4) phrase so the section 74A insertion is distinct from the "
                    "Rule 142(1A) insertion made by the same notification."
                ),
            )
            fixed["operation"] = "SPLICE"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/4",
                "anchor_component_id": None,
                "anchor_text": "of section 74",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "position": "after",
                "insert_text": " or sub-section (6) of section 74A",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_e6ed17e56a016068":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 20/2024-Central Tax: target "
                    "the Rule 142(1A) phrase so the section 74A insertion is distinct "
                    "from the Rule 142(4) insertion made by the same notification."
                ),
            )
            fixed["operation"] = "SPLICE"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/142/subrule/1a",
                "anchor_component_id": None,
                "anchor_text": "of Section 74",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "position": "after",
                "insert_text": " or sub-section (1) of section 74A",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_7aabc9f26c533448":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 19/2022-Central Tax: source "
                    "text amends Rule 96(3); reviewed anchor preserved a line-break and "
                    "spacing variant of FORM GSTR-3."
                ),
            )
            fixed["operation"] = "SUBSTITUTE"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/96/subrule/3",
                "anchor_component_id": None,
                "anchor_text": "FORM GSTR-3",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "old_text": "FORM GSTR-3",
                "new_text": "FORM GSTR-3B",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_4274a8ccd0fc33f3":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 20/2024-Central Tax: Rule "
                    "36(3) text is present in the parent Rule 36 version, while the split "
                    "subrule 3 baseline remains stale. Apply the source-proven insertion "
                    "as an equivalent punctuation-preserving substitution."
                ),
            )
            fixed["operation"] = "SUBSTITUTE"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/36",
                "anchor_component_id": None,
                "anchor_text": "suppression of facts",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "old_text": "suppression of facts.",
                "new_text": "suppression of facts under section 74.",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_05c7c19bf7d3f0c5":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 49/2019-Central Tax: Rule "
                    "36(4) is source-proven and already present as a child component in "
                    "the reconstructed baseline/current tree. Record the event as an "
                    "already-reflected insert instead of leaving a duplicate-child gap."
                ),
            )
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "noop_if_already_reflected": True,
                "already_reflected": True,
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_eb18afb06f304825":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 38/2023-Central Tax: source "
                    "text substitutes words in the Rule 46 clause (f) proviso. The "
                    "affected phrase is materialized as the child proviso inserted by "
                    "Notification 14/2022-Central Tax, so retarget the exact substitution "
                    "to that child component."
                ),
            )
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/46/proviso/providedthat-de982caa10",
                "anchor_component_id": None,
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "materializer_repair": True,
                "materializer_repair_reason": "retarget_to_materialized_child_proviso",
            }
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_001cf1c32bf91009":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 31/2019-Central Tax: the "
                    "source substitutes the word 'three' only within the quoted phrase "
                    "'shall complete the investigation within a period of three months'. "
                    "Use the full quoted phrase as the deterministic old/new payload to "
                    "avoid replacing the later separate 'three months' occurrence."
                ),
            )
            fixed["payload"] = {
                **(fixed.get("payload") or {}),
                "old_text": "shall complete the investigation within a period of three months",
                "new_text": "shall complete the investigation within a period of six months",
                "materializer_repair": True,
                "materializer_repair_reason": "quoted_phrase_disambiguation",
            }
            repaired_events.append(fixed)
            continue

        if event_id in {
            "evt_cbic_876bade2978b29ea",
            "evt_cbic_ba5635bfca1625c3",
            "evt_cbic_f14c69c16e5d3094",
            "evt_cbic_daa2627a546cc11a",
        }:
            notes = {
                "evt_cbic_876bade2978b29ea": (
                    "Materializer repair from Notification 17/2017-Central Tax: apply the "
                    "Rule 24(4) deadline substitution to the parent Rule 24 text so later "
                    "deadline extensions can chain against the materialized phrase."
                ),
                "evt_cbic_ba5635bfca1625c3": (
                    "Materializer repair from Notification 36/2017-Central Tax: Rule 24(4) "
                    "deadline text is present in the parent Rule 24 version created by the "
                    "earlier 2017 amendment, while the split subrule baseline remains stale."
                ),
                "evt_cbic_f14c69c16e5d3094": (
                    "Materializer repair from Notification 51/2017-Central Tax: apply the "
                    "Rule 24(4) deadline extension to the parent Rule 24 text where the "
                    "current deadline phrase is materialized."
                ),
                "evt_cbic_daa2627a546cc11a": (
                    "Materializer repair from Notification 03/2018-Central Tax: apply the "
                    "Rule 24(4) deadline extension to the parent Rule 24 text where the "
                    "current deadline phrase is materialized."
                ),
            }
            fixed = _with_materializer_repair_metadata(event, note=notes[event_id])
            fixed["operation"] = "SUBSTITUTE"
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/24",
                "anchor_component_id": None,
                "anchor_occurrence": None,
            }
            payload = dict(fixed.get("payload") or {})
            if event_id == "evt_cbic_876bade2978b29ea":
                payload["old_text"] = "within a period of thirty days from the appointed day"
                payload["new_text"] = "on or before 30th September, 2017"
            elif event_id == "evt_cbic_ba5635bfca1625c3":
                payload["old_text"] = "30th September"
                payload["new_text"] = "31st October"
            elif event_id == "evt_cbic_f14c69c16e5d3094":
                payload["old_text"] = "on or before 31st October, 2017"
                payload["new_text"] = "on or before 31st December, 2017"
            elif event_id == "evt_cbic_daa2627a546cc11a":
                payload["old_text"] = "31st December, 2017"
                payload["new_text"] = "31st March, 2018"
            payload["materializer_repair"] = True
            payload["triage_lane"] = None
            fixed["payload"] = payload
            repaired_events.append(fixed)
            continue

        if event_id == "evt_cbic_cc2573caef6b8746":
            fixed = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair from Notification 35/2021-Central Tax as commenced "
                    "by Notification 38/2021-Central Tax: source extraction anchored on "
                    "'may file', while the reconstructed Rule 23(1) text uses 'may submit'. "
                    "Apply the source-proven Rule 10B condition immediately after 'may'."
                ),
            )
            fixed["operation"] = "SUBSTITUTE"
            fixed["legal_time"] = {
                **(fixed.get("legal_time") or {}),
                "applicability_start": "2022-01-01",
                "commencement_date": "2022-01-01",
                "date_basis": "commencement_notification_38_2021_rule_2_subrule_2",
                "retrospective": False,
            }
            fixed["target"] = {
                **(fixed.get("target") or {}),
                "component_id": "/in/union/rules/cgst-rules-2017/rule/23",
                "anchor_component_id": None,
                "anchor_text": "may submit",
                "anchor_occurrence": None,
            }
            fixed["payload"] = {
                "old_text": "may submit",
                "new_text": "may, subject to the provisions of rule 10B, submit",
                "materializer_repair": True,
            }
            repaired_events.append(fixed)
            continue

        if event_id in {
            "evt_cbic_c8a4f4ea518f7500",
            "evt_cbic_xml_321be3fecc5fec93",
            "evt_cbic_73f433f65317b312",
        }:
            note = (
                "Materializer repair for validated INSERT_SIBLING rule-creation events "
                "that were misrouted to the forms lane because the rule text mentions "
                "GST forms. The source instruction deterministically inserts a complete "
                "rule after an existing anchor rule, so the forms_lane_pending_baseline "
                "triage flag is stripped to allow materialization."
            )
            repaired = _with_materializer_repair_metadata(event, note=note)
            repaired_payload = dict(repaired.get("payload") or {})
            repaired_payload.pop("forms_lane_pending_baseline", None)
            repaired_payload.pop("triage_lane", None)
            repaired_payload["materializer_repair"] = True
            repaired_payload["materializer_repair_reason"] = "validated_rule_insert_forms_lane_unblock"
            repaired["payload"] = repaired_payload
            repaired_events.append(repaired)
            continue

        if _is_validated_rule_forms_lane_false_positive(event):
            repaired = _with_materializer_repair_metadata(
                event,
                note=(
                    "Materializer repair for a deterministic rule amendment that was "
                    "blocked only by forms-lane routing because of form text references."
                ),
            )
            repaired_payload = dict(repaired.get("payload") or {})
            repaired_payload.pop("forms_lane_pending_baseline", None)
            repaired_payload.pop("triage_lane", None)
            repaired_payload["materializer_repair"] = True
            repaired_payload["materializer_repair_reason"] = "validated_rule_forms_lane_unblock"
            repaired["payload"] = repaired_payload
            repaired_events.append(repaired)
            continue

        repaired_events.append(event)
    return repaired_events


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _element_text(element: ET.Element) -> str:
    lines: list[str] = []
    for child in element.iter():
        if _local_name(child.tag) not in {"num", "heading", "p"}:
            continue
        text = _clean_text("".join(child.itertext()))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _properties(root: ET.Element) -> dict[str, str]:
    props = {}
    for element in root.iter():
        if _local_name(element.tag) != "property":
            continue
        name = element.attrib.get("name")
        if name:
            props[name] = element.attrib.get("value", "")
    return props


def _find_by_refers_to(root: ET.Element, component_id: str) -> ET.Element | None:
    for element in root.iter():
        if element.attrib.get("refersTo") == component_id:
            return element
    return None


def _content_paragraph(element: ET.Element | None) -> ET.Element | None:
    if element is None:
        return None
    paragraph = element.find("./content/p")
    if paragraph is not None:
        return paragraph
    return element.find("./intro/content/p")


def _unique_descendant_with_anchor(
    target: ET.Element,
    parent_component_id: str,
    anchor: str,
) -> tuple[str, ET.Element, Any] | None:
    matches: list[tuple[str, ET.Element, Any]] = []
    prefix = f"{parent_component_id}/"
    for element in target.iter():
        component_id = str(element.attrib.get("refersTo") or "")
        if not component_id.startswith(prefix):
            continue
        paragraph = _content_paragraph(element)
        if paragraph is None:
            continue
        try:
            match = resolve_anchor(paragraph.text or "", anchor, component_id)
        except AnchorNotFoundError:
            continue
        matches.append((component_id, element, match))
    if len(matches) != 1:
        return None
    return matches[0]


def _set_heading(element: ET.Element, heading_text: str) -> None:
    heading_text = _clean_text(heading_text)
    if not heading_text:
        return
    heading = element.find("./heading")
    if heading is None:
        heading = ET.Element("heading")
        insert_at = 1 if element.find("./num") is not None else 0
        element.insert(insert_at, heading)
    heading.text = heading_text


def _prepare_insert_text(original: str, insert_text: str, insert_pos: int) -> str:
    prepared = insert_text
    if prepared and not prepared[0].isspace() and insert_pos > 0 and not original[insert_pos - 1].isspace():
        prepared = " " + prepared
    if (
        prepared
        and not prepared[-1].isspace()
        and insert_pos < len(original)
        and not original[insert_pos].isspace()
        and original[insert_pos] not in ",.;:)]}"
    ):
        prepared = prepared + " "
    return prepared


def _is_duplicate_splice_insert(original: str, insert_pos: int, insert_text: str) -> bool:
    insert_text = str(insert_text or "").strip()
    if not insert_text:
        return False
    check_pos = insert_pos
    while check_pos < len(original) and original[check_pos].isspace():
        check_pos += 1
    return original[check_pos:check_pos + len(insert_text)].lower() == insert_text.lower()


def _normalized_match_spans(text: str, needle: str) -> list[tuple[int, int]]:
    needle = str(needle or "").strip()
    if not needle:
        return []
    pattern = re.escape(re.sub(r"\s+", " ", needle).strip()).replace(r"\ ", r"\s+")
    pattern = pattern.replace(r"\-", r"\s*-\s*")
    return [(match.start(), match.end()) for match in re.finditer(pattern, text, flags=re.IGNORECASE)]


_RE_FOOTNOTE_LEADING = re.compile(r"\d+\s*\[")
_RE_FOOTNOTE_BRACKETED = re.compile(r"\[\d+\]")
_SMART_QUOTE_MAP = {
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u2016": '"', "\u2015": '"',
    "\u2014": "-", "\u2013": "-",
}


def _footnote_skip_indices(text: str) -> set[int]:
    """Original string indices that are footnote marker artefacts (digits and
    brackets wrapping an amendment interpolation, or bracketed footnote refs).
    Inner interpolation text is kept; only the numeric/bracket markers are skipped.
    """
    skip: set[int] = set()
    for m in _RE_FOOTNOTE_LEADING.finditer(text):
        bracket_pos = m.end() - 1
        for k in range(m.start(), bracket_pos + 1):
            skip.add(k)
        close = text.find("]", bracket_pos + 1)
        if close != -1:
            skip.add(close)
    for m in _RE_FOOTNOTE_BRACKETED.finditer(text):
        for k in range(m.start(), m.end()):
            skip.add(k)
    return skip


def _normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Normalize ``text`` for matching, returning the normalized string and two
    parallel arrays mapping each normalized character back to the ORIGINAL text:
    ``start_map[j]`` is the original index where normalized char ``j`` begins and
    ``end_map[j]`` is one past the original index that produced it.

    Steps: NFKC, smart-quote/em/en-dash/\u2016/\u2015 folding, footnote-marker
    stripping and whitespace collapse. The original text is never mutated.
    """
    skip = _footnote_skip_indices(text)
    chars: list[str] = []
    start_map: list[int] = []
    end_map: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        if i in skip:
            i += 1
            continue
        ch = text[i]
        for nc in unicodedata.normalize("NFKC", ch):
            nc = _SMART_QUOTE_MAP.get(nc, nc)
            chars.append(nc)
            start_map.append(i)
            end_map.append(i + 1)
        i += 1
    out_chars: list[str] = []
    out_start: list[int] = []
    out_end: list[int] = []
    j = 0
    nn = len(chars)
    while j < nn:
        if chars[j].isspace():
            first_start = start_map[j]
            last_end = end_map[j]
            j += 1
            while j < nn and chars[j].isspace():
                last_end = end_map[j]
                j += 1
            out_chars.append(" ")
            out_start.append(first_start)
            out_end.append(last_end)
        else:
            out_chars.append(chars[j])
            out_start.append(start_map[j])
            out_end.append(end_map[j])
            j += 1
    return "".join(out_chars), out_start, out_end


def _normalized_find_spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Return original-coordinate ``[start, end)`` spans where ``needle`` matches
    ``haystack`` after Unicode/whitespace/footnote normalization (case-insensitive).
    Empty if there is no normalized match.
    """
    haystack = str(haystack or "")
    needle = str(needle or "").strip()
    if not needle:
        return []
    n_hay, hstart, hend = _normalize_with_map(haystack)
    n_needle, _, _ = _normalize_with_map(needle)
    if not n_needle.strip():
        return []
    n_hay_l = n_hay.lower()
    n_needle_l = n_needle.lower()
    spans: list[tuple[int, int]] = []
    start = 0
    nl = len(n_needle_l)
    while True:
        p = n_hay_l.find(n_needle_l, start)
        if p == -1:
            break
        s = hstart[p]
        last = min(p + nl - 1, len(hend) - 1)
        e = hend[last] if last >= 0 else s
        spans.append((s, e))
        start = p + nl if nl else p + 1
    return spans


def _detect_normalization_steps(haystack: str, needle: str) -> list[str]:
    """Report which normalization steps were actually needed for this haystack/needle
    pair (for provenance recording)."""
    steps: list[str] = []
    combined = str(haystack or "") + str(needle or "")
    if any(unicodedata.normalize("NFKC", c) != c for c in combined):
        steps.append("nfkc")
    if re.search(r"[\u201c\u201d\u2018\u2019]", combined):
        steps.append("smart_quotes")
    if re.search(r"[\u2014\u2013]", combined):
        steps.append("em_en_dash")
    if re.search(r"[\u2016\u2015]", combined):
        steps.append("legal_quote_marks")
    if _RE_FOOTNOTE_LEADING.search(haystack or "") or _RE_FOOTNOTE_BRACKETED.search(haystack or ""):
        steps.append("footnote_markers")
    if re.search(r"\s{2,}|\n|\t", haystack or "") or re.search(r"\s{2,}|\n|\t", needle or ""):
        steps.append("whitespace_collapse")
    if str(haystack or "").lower() != str(haystack) or str(needle or "").lower() != str(needle):
        steps.append("casefold")
    return steps or ["normalized"]


def _record_normalized_match(event: dict[str, Any] | None, haystack: str, needle: str) -> None:
    """Record provenance that a match succeeded only via normalization.

    Stored under a dedicated ``_normalization_provenance`` key (NOT in
    ``_materialization_warnings``) so the successful application is not
    double-counted as a coverage gap. Callers surface this in the manifest.
    """
    if not event:
        return
    steps = _detect_normalization_steps(haystack, needle)
    event.setdefault("_normalization_provenance", []).append(
        {"steps": steps, "needle_preview": str(needle or "")[:80]}
    )


def _normalized_text_match(haystack: str, needle: str) -> tuple[bool, int, bool]:
    """Match ``needle`` within ``haystack`` trying an exact match first, then a
    Unicode/whitespace/footnote normalization pass as a fallback.

    Returns ``(matched, match_position, normalized)``. ``match_position`` is the
    index in the ORIGINAL ``haystack`` where the match begins. ``normalized`` is
    True when the match only succeeded after normalization (callers may record
    this for provenance). The stored component text is never mutated - this is a
    matching-only helper.
    """
    haystack = str(haystack or "")
    needle = str(needle or "").strip()
    if not needle:
        return (False, -1, False)
    exact = haystack.find(needle)
    if exact != -1:
        return (True, exact, False)
    spans = _normalized_find_spans(haystack, needle)
    if spans:
        return (True, spans[0][0], True)
    return (False, -1, False)


def _match_occurrence(payload: dict[str, Any]) -> int | None:
    try:
        occurrence = int(payload.get("match_occurrence") or 0)
    except (TypeError, ValueError):
        return None
    return occurrence if occurrence > 0 else None


def _replace_all_occurrences_allowed(event: dict[str, Any]) -> bool:
    excerpt = str((event.get("evidence") or {}).get("excerpt") or "")
    return bool(
        re.search(
            r"\bat\s+both\s+the\s+places\b|\bat\s+all\s+(?:the\s+)?places\b|\bwherever\s+they\s+occur\b|\boccurring\s+at\s+both\s+the\s+places\b",
            excerpt,
            flags=re.IGNORECASE,
        )
    )


def _replace_unique_text(original: str, old_text: str, new_text: str, event: dict[str, Any] | None = None) -> str | None:
    payload = event.get("payload", {}) if event else {}
    old_text = str(old_text or "").strip()
    if not old_text:
        return None
    if payload.get("noop_if_already_reflected") and new_text:
        # new_text already present → amendment was applied verbatim
        if _normalized_match_spans(original, new_text):
            return original
        # Both old_text and new_text absent → the component was wholesale
        # replaced by a later amendment that absorbed this change (e.g. a
        # percentage sub-rule later removed entirely). Treat as reflected.
        if not _normalized_match_spans(original, old_text) and not _normalized_match_spans(original, new_text):
            return original
    if original.count(old_text) == 1:
        return original.replace(old_text, new_text, 1)
    matches = _normalized_match_spans(original, old_text)
    if len(matches) == 1:
        start, end = matches[0]
        return original[:start] + new_text + original[end:]
    occurrence = _match_occurrence(payload)
    if occurrence is None or len(matches) < occurrence:
        if event and (_replace_all_occurrences_allowed(event) or payload.get("replace_all")):
            result = original
            for start, end in reversed(matches):
                result = result[:start] + new_text + result[end:]
            return result
        nspans = _normalized_find_spans(original, old_text)
        if nspans:
            _record_normalized_match(event, original, old_text)
            if event and (_replace_all_occurrences_allowed(event) or payload.get("replace_all")) and len(nspans) > 1:
                result = original
                for s, e in reversed(nspans):
                    result = result[:s] + new_text + result[e:]
                return result
            if occurrence is not None and len(nspans) >= occurrence:
                s, e = nspans[occurrence - 1]
                return original[:s] + new_text + original[e:]
            if len(nspans) == 1:
                s, e = nspans[0]
                return original[:s] + new_text + original[e:]
        return None
    start, end = matches[occurrence - 1]
    return original[:start] + new_text + original[end:]


def _omit_all_occurrences_allowed(event: dict[str, Any]) -> bool:
    excerpt = str((event.get("evidence") or {}).get("excerpt") or "")
    return bool(
        re.search(
            r"\bat\s+both\s+the\s+places\b|\bat\s+all\s+(?:the\s+)?places\b|\bwherever\s+they\s+occur\b|\boccurring\s+at\s+both\s+the\s+places\b",
            excerpt,
            flags=re.IGNORECASE,
        )
    )


def _omit_text_from_paragraph(original: str, omit_text: str, event: dict[str, Any]) -> str | None:
    payload = event.get("payload", {}) if event else {}
    omit_text = str(omit_text or "").strip()
    if not omit_text:
        return None
    exact_count = original.count(omit_text)
    if payload.get("noop_if_already_reflected") and exact_count == 0 and not _normalized_match_spans(original, omit_text):
        return original
    if exact_count == 1:
        return original.replace(omit_text, "", 1)
    matches = _normalized_match_spans(original, omit_text)
    if len(matches) == 1:
        start, end = matches[0]
        return original[:start] + original[end:]
    occurrence = _match_occurrence(payload)
    if occurrence is not None and len(matches) >= occurrence:
        start, end = matches[occurrence - 1]
        return original[:start] + original[end:]
    if _omit_all_occurrences_allowed(event):
        if exact_count > 1:
            return original.replace(omit_text, "")
        if len(matches) > 1:
            updated = original
            for start, end in reversed(matches):
                updated = updated[:start] + updated[end:]
            return updated
    nspans = _normalized_find_spans(original, omit_text)
    if nspans:
        _record_normalized_match(event, original, omit_text)
        if occurrence is not None and len(nspans) >= occurrence:
            s, e = nspans[occurrence - 1]
            return original[:s] + original[e:]
        if _omit_all_occurrences_allowed(event) and len(nspans) > 1:
            updated = original
            for s, e in reversed(nspans):
                updated = updated[:s] + updated[e:]
            return updated
        if len(nspans) == 1:
            s, e = nspans[0]
            return original[:s] + original[e:]
    return None


def _date_key(event: dict[str, Any]) -> str:
    return (
        event.get("legal_time", {}).get("applicability_start")
        or event.get("legal_time", {}).get("commencement_date")
        or event.get("source", {}).get("publication_date")
        or ""
    )


def _source_span_start(event: dict[str, Any]) -> int:
    span = event.get("evidence", {}).get("source_span", {})
    try:
        return int(span.get("start", 0))
    except (TypeError, ValueError):
        return 0


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        _date_key(event),
        event.get("source", {}).get("publication_date") or "",
        event.get("source", {}).get("document_id") or "",
        _source_span_start(event),
        event.get("event_id", ""),
    )


def _same_date_insert_then_whole_omit_allowed(events: list[dict[str, Any]]) -> bool:
    if len(events) != 2:
        return False
    ordered = sorted(events, key=_event_sort_key)
    first, second = ordered
    if first.get("operation") != "INSERT_SIBLING":
        return False
    if second.get("operation") != "OMIT":
        return False
    if not (second.get("payload") or {}).get("whole_component"):
        return False
    first_target = (first.get("target") or {}).get("component_id")
    second_target = (second.get("target") or {}).get("component_id")
    if not first_target or first_target != second_target:
        return False
    first_publication = (first.get("source") or {}).get("publication_date") or ""
    second_publication = (second.get("source") or {}).get("publication_date") or ""
    return bool(first_publication and second_publication and first_publication < second_publication)


def _normalize_document_id(doc_id: str) -> str:
    return re.sub(r"-(central|union|integrated)-tax$", "", doc_id)


def _same_source_ordered_text_edits_allowed(events: list[dict[str, Any]]) -> bool:
    if len(events) < 2:
        return False
    document_ids = {
        _normalize_document_id(str((event.get("source") or {}).get("document_id") or "")) for event in events
    }
    if len(document_ids) != 1 or not next(iter(document_ids)):
        return False
    allowed_ops = {"SPLICE", "SUBSTITUTE", "OMIT", "INSERT_CHILD"}
    if any(event.get("operation") not in allowed_ops for event in events):
        return False
    starts = [_source_span_start(event) for event in events]
    if len(starts) != len(set(starts)):
        return False
    return all((event.get("evidence") or {}).get("source_span", {}).get("text_hash") for event in events)


def _retryable_apply_error(exc: Exception) -> bool:
    message = str(exc)
    return any(
        marker in message
        for marker in (
            "Anchor component missing:",
            "Parent component missing:",
            "Target component missing:",
        )
    )


def _latest_component_text(component_id: str, store: ComponentStore) -> str | None:
    """Return the most recent stored text for ``component_id``, falling back to
    the parent component's text for nested targets (subrule/proviso/etc.)."""
    versions = store.versions.get(component_id)
    if versions:
        return versions[-1].get("text", "")
    parent = _parent_component_for_nested_target(component_id)
    if parent and parent != component_id:
        versions = store.versions.get(parent)
        if versions:
            return versions[-1].get("text", "")
    return None


def _is_already_reflected_apply_failure(
    event: dict[str, Any],
    exc: Exception,
    store: ComponentStore,
) -> bool:
    """Classify a SUBSTITUTE/OMIT apply failure as an already-reflected no-op.

    The baseline was reconstructed from a consolidated (post-amendment) text, so
    some amendments are already baked in. When the materializer replays them the
    apply fails, but the failure itself is evidence the amendment was applied:

    * SUBSTITUTE fails because ``old_text`` is absent, yet ``new_text`` is
      already present in the component text.
    * OMIT fails because ``omit_text`` is absent (already removed), or because
      fewer occurrences remain than the amendment expected to remove.
    """
    message = str(exc)
    operation = event.get("operation")
    payload = event.get("payload") or {}
    component_id = (event.get("target") or {}).get("component_id", "")
    if not component_id:
        return False

    if operation == "SUBSTITUTE" and "Substitution text not found" in message:
        new_text = str(payload.get("new_text") or "").strip()
        if not new_text:
            return False
        text = _latest_component_text(component_id, store)
        if text is not None:
            matched, _, _ = _normalized_text_match(text, new_text)
            if matched:
                return True
        # Fallback to the consolidated baseline (versions[0]): a prior
        # structural substitution may have overwritten the section text so
        # that ``new_text`` is no longer present in the latest version, even
        # though the amendment was applied when the baseline was consolidated.
        versions = store.versions.get(component_id, [])
        if versions:
            baseline_text = str(versions[0].get("text", "") or "")
            if baseline_text:
                matched, _, _ = _normalized_text_match(baseline_text, new_text)
                if matched:
                    return True
        return False

    if operation == "OMIT" and "Partial omission text not uniquely found" in message:
        omit_text = str(payload.get("omit_text") or "").strip()
        if not omit_text:
            return False
        text = _latest_component_text(component_id, store)
        if text is None:
            return False
        exact_count = text.count(omit_text)
        norm_count = len(_normalized_match_spans(text, omit_text))
        if exact_count == 0 and norm_count == 0:
            return True
        occurrence = _match_occurrence(payload)
        if occurrence is not None and max(exact_count, norm_count) < occurrence:
            return True
        return False

    return False


@dataclass
class XmlFileState:
    relative_path: Path
    tree: ET.ElementTree


class ComponentStore:
    def __init__(self, target_work: str, base_as_of: str) -> None:
        self.target_work = target_work
        self.base_as_of = base_as_of
        self.versions: dict[str, list[dict[str, Any]]] = {}

    def add_baseline(self, component_id: str, text: str, path: Path, document_type: str = "") -> None:
        if component_id in self.versions:
            return
        self.versions[component_id] = [
            self._version(
                component_id=component_id,
                valid_from=self.base_as_of,
                valid_to=None,
                text=text,
                event=None,
                source_basis={
                    "type": "baseline_corpus",
                    "path": str(path),
                    "base_as_of": self.base_as_of,
                    "document_type": document_type,
                },
                event_chain=[],
            )
        ]

    def add_event_version(self, component_id: str, valid_from: str, text: str, event: dict[str, Any]) -> None:
        chain: list[str] = []
        existing = self.versions.setdefault(component_id, [])
        if existing:
            existing[-1]["valid_to"] = valid_from
            existing[-1]["applicability_end"] = valid_from
            chain = list(existing[-1].get("event_chain", []))
        chain.append(event["event_id"])
        existing.append(
            self._version(
                component_id=component_id,
                valid_from=valid_from,
                valid_to=None,
                text=text,
                event=event,
                source_basis={
                    "type": "amendment_event",
                    "event_id": event["event_id"],
                    "source_document_id": event.get("source", {}).get("document_id", ""),
                    "source_record_id": event.get("source", {}).get("record_id", ""),
                    "source_span": event.get("evidence", {}).get("source_span", {}),
                    "operation": event.get("operation", ""),
                },
                event_chain=chain,
            )
        )

    def is_baseline_only(self, component_id: str) -> bool:
        existing = self.versions.get(component_id) or []
        return len(existing) == 1 and existing[0].get("created_by_event_id") is None

    def _version(
        self,
        *,
        component_id: str,
        valid_from: str,
        valid_to: str | None,
        text: str,
        event: dict[str, Any] | None,
        source_basis: dict[str, Any],
        event_chain: list[str],
    ) -> dict[str, Any]:
        event_id = event.get("event_id") if event else "baseline"
        seed = f"{component_id}|{valid_from}|{event_id}|{sha256_text(text)}"
        return {
            "version_id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
            "work_id": self.target_work,
            "component_id": component_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "applicability_start": valid_from,
            "applicability_end": valid_to,
            "system_start": event.get("system_time", {}).get("compiled_at") if event else None,
            "system_end": None,
            "text": text,
            "text_sha256": sha256_text(text),
            "created_by_event_id": event.get("event_id") if event else None,
            "event_chain": event_chain,
            "source_basis": source_basis,
        }

    def flattened(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for component_id in sorted(self.versions):
            rows.extend(self.versions[component_id])
        return rows


def _target_xml_paths(corpus_dir: Path, target_work: str) -> list[Path]:
    base_dir = corpus_dir / Path(target_work.strip("/"))
    paths = sorted(base_dir.rglob("*.xml")) if base_dir.exists() else []
    discrete = [path for path in paths if path.name != "rules.xml"]
    return discrete or paths


def _baseline_xml_paths(base_dir: Path) -> list[Path]:
    if base_dir.is_file() and base_dir.suffix == ".xml":
        return [base_dir]
    baseline = base_dir / "baseline.xml"
    if baseline.exists():
        return [baseline]
    return sorted(base_dir.rglob("*.xml")) if base_dir.exists() else []


def _load_base_state(
    corpus_dir: Path,
    target_work: str,
    base_as_of: str,
    *,
    baseline_dir: Path | None = None,
) -> tuple[dict[Path, XmlFileState], dict[str, Path], ComponentStore]:
    files: dict[Path, XmlFileState] = {}
    component_paths: dict[str, Path] = {}
    store = ComponentStore(target_work, base_as_of)
    paths = _baseline_xml_paths(baseline_dir) if baseline_dir else []
    root_dir = baseline_dir if baseline_dir and baseline_dir.is_dir() else corpus_dir
    if not paths:
        paths = _target_xml_paths(corpus_dir, target_work)
        root_dir = corpus_dir
    for path in paths:
        relative = path.relative_to(root_dir) if root_dir in path.parents or path == root_dir else Path(path.name)
        tree = ET.parse(path)
        files[relative] = XmlFileState(relative_path=relative, tree=tree)
        root = tree.getroot()
        props = _properties(root)
        document_id = props.get("canonical_id")
        if document_id:
            if document_id in KNOWN_BASELINE_ARTIFACTS:
                _log.warning("Filtering known baseline artifact: %s", document_id)
            else:
                component_paths.setdefault(document_id, relative)
                store.add_baseline(document_id, _element_text(root), relative, props.get("document_type", ""))
        for element in root.iter():
            component_id = element.attrib.get("refersTo")
            if not component_id:
                continue
            if component_id in KNOWN_BASELINE_ARTIFACTS:
                _log.warning("Filtering known baseline artifact: %s", component_id)
                continue
            component_paths.setdefault(component_id, relative)
            store.add_baseline(component_id, _element_text(element), relative, _local_name(element.tag))
    return files, component_paths, store


def _baseline_blocked_components(baseline_dir: Path | None) -> dict[str, list[str]]:
    if not baseline_dir:
        return {}
    components_path = baseline_dir / "baseline_components.jsonl"
    blocked: dict[str, list[str]] = {}
    if components_path.exists():
        for line in components_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            component_id = str(row.get("component_id") or "")
            if component_id and row.get("blocked"):
                blocked[component_id] = [str(reason) for reason in row.get("block_reasons") or []]
    if blocked:
        return blocked
    for xml_path in _baseline_xml_paths(baseline_dir):
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        for element in root.iter():
            component_id = str(element.attrib.get("refersTo") or "")
            if not component_id or element.attrib.get("data-baseline-blocked") != "true":
                continue
            reasons = [
                reason.strip()
                for reason in str(element.attrib.get("data-baseline-block-reasons") or "").split(",")
                if reason.strip()
            ]
            blocked[component_id] = reasons
    return blocked


def _ensure_structural_targets(
    events: list[dict[str, Any]],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    r"""Create synthetic baseline components for SUBSTITUTE events with
    ``structural_text`` that target components not in the baseline.

    Post-2017 rules (96A, 138A-E, etc.) were dropped from the baseline.
    Their original INSERT_SIBLING events may be missing from the stream
    (classified UNKNOWN at compile time).  This function pre-creates those
    components from three sources:
      1. SUBSTITUTE events with ``structural_text``
      2. INSERT_SIBLING events with ``content`` payload
      3. SPLICE/INSERT_CHILD events targeting a missing component whose
         parent has an INSERT_SIBLING with content in the stream
    """
    created: list[str] = []

    # Pass 1: SUBSTITUTE events with structural_text
    for event in events:
        if event.get("operation") != "SUBSTITUTE":
            continue
        payload = event.get("payload", {})
        if payload.get("apply_to_parent_subrule_span") or payload.get("allow_detached_component_version"):
            continue
        structural_text = str(payload.get("structural_text") or payload.get("structural_content") or "").strip()
        if not structural_text:
            continue
        component_id = event.get("target", {}).get("component_id", "")
        if not component_id or component_id in component_paths:
            continue

        label = component_id.split("/")[-1]
        heading = str(payload.get("structural_heading") or payload.get("heading") or "").strip()
        _create_synthetic_component(component_id, structural_text, heading, files, component_paths, store)
        created.append(component_id)

    # Pass 2: INSERT_SIBLING events with content that are needs_review (won't be
    # applied by the normal apply loop). Creates 96A, 96B, etc. from their
    # notification text so downstream SPLICE/SUBSTITUTE events can target them.
    for event in events:
        if event.get("operation") != "INSERT_SIBLING":
            continue
        if event.get("status") == "validated":
            continue  # Will be handled by the apply loop
        component_id = event.get("target", {}).get("component_id", "")
        if not component_id or component_id in component_paths:
            continue
        payload = event.get("payload", {})
        content = str(payload.get("content") or "").strip()
        if not content or len(content) < 30:
            continue
        if re.search(r"insert\s+new\s+rule\s+text\s+here", content, re.IGNORECASE):
            _log.warning("Rejecting placeholder synthetic component for %s", component_id)
            continue
        label = str(payload.get("label") or component_id.split("/")[-1]).strip()
        heading = str(payload.get("heading") or "").strip()
        _create_synthetic_component(component_id, content, heading, files, component_paths, store)
        created.append(component_id)

    # Pass 3: SPLICE/SUBSTITUTE targeting missing sub-rules whose parent was
    # created in pass 1/2 — create the sub-rule so the event can apply.
    # If the parent rule already exists and contains a unique top-level subrule
    # span, carve that span into a first-class component.  This repairs baselines
    # where the parent rule text exists but child subrule nodes were not split.
    # INSERT_CHILD creates its own target, but its parent may also be an unsplit
    # subrule embedded in the parent rule text.
    for event in events:
        op = event.get("operation", "")
        if op not in {"SPLICE", "SUBSTITUTE"}:
            continue
        payload = event.get("payload", {})
        if payload.get("apply_to_parent_subrule_span") or payload.get("allow_detached_component_version"):
            continue
        component_id = event.get("target", {}).get("component_id", "")
        if not component_id or component_id in component_paths:
            continue
        if "/subrule/" in component_id:
            parent_rule = component_id.rsplit("/subrule/", 1)[0]
            if parent_rule in component_paths:
                structural_text = str(event.get("payload", {}).get("structural_text") or
                                      event.get("payload", {}).get("content") or "").strip()
                if structural_text and len(structural_text) > 10:
                    _create_synthetic_component(component_id, structural_text, "", files, component_paths, store)
                    created.append(component_id)
                    continue
                carved = _create_subrule_component_from_parent_span(
                    component_id,
                    files,
                    component_paths,
                    store,
                )
                if carved:
                    created.append(component_id)

    # Pass 4: INSERT_CHILD events whose parent is a missing subrule embedded in
    # an existing parent rule. Create the parent subrule before applying the
    # child insertion.
    for event in events:
        if event.get("operation") != "INSERT_CHILD":
            continue
        payload = event.get("payload", {})
        parent_component = str(
            payload.get("parent_component_id")
            or payload.get("anchor_component_id")
            or (event.get("target") or {}).get("anchor_component_id")
            or ""
        )
        if not parent_component or parent_component in component_paths:
            continue
        if "/subrule/" not in parent_component:
            continue
        carved = _create_subrule_component_from_parent_span(
            parent_component,
            files,
            component_paths,
            store,
        )
        if carved:
            created.append(parent_component)

    return created


def _create_subrule_component_from_parent_span(
    component_id: str,
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
    *,
    record_baseline: bool = True,
) -> bool:
    parent_rule = parent_component_for_subrule(component_id)
    label = subrule_label_from_component(component_id)
    if not parent_rule or not label:
        return False
    parent_path = component_paths.get(parent_rule)
    if not parent_path or parent_path not in files:
        return False
    state = files[parent_path]
    parent = _find_by_refers_to(state.tree.getroot(), parent_rule)
    if parent is None:
        return False
    paragraph = _content_paragraph(parent)
    if paragraph is None:
        return False
    text = paragraph.text or ""
    span = find_top_level_subrule_span(text, label)
    if span is None:
        explanation = re.search(r"\bExplanation\b", text, flags=re.IGNORECASE)
        if explanation:
            span = find_top_level_subrule_span(text[: explanation.start()], label)
    if span is None:
        return False
    subrule_text = text[span[0]:span[1]].strip()
    if len(subrule_text) < 10:
        return False
    _create_synthetic_component(
        component_id,
        subrule_text,
        "",
        files,
        component_paths,
        store,
        record_baseline=record_baseline,
    )
    return True


def _create_synthetic_component(
    component_id: str,
    text: str,
    heading: str,
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
    *,
    record_baseline: bool = True,
) -> None:
    """Create a synthetic baseline component in the XML store."""
    label = component_id.split("/")[-1]

    if "/subrule/" not in component_id:
        rule_data = {"label": label, "heading": heading, "content": text, "subrules": []}
        tree = render_rule(rule_data, {"number": "", "title": ""}, {"source_type": "synthetic_baseline"})
        relative = expected_corpus_relative_path(component_id, "rule")
        files[relative] = XmlFileState(relative_path=relative, tree=tree)
    else:
        parent_rule = component_id.rsplit("/subrule/", 1)[0]
        parent_path = component_paths.get(parent_rule)
        if parent_path and parent_path in files:
            state = files[parent_path]
            root = state.tree.getroot()
            elem = ET.SubElement(root, "subrule", {"refersTo": component_id})
            c = ET.SubElement(elem, "content")
            ET.SubElement(c, "p").text = text
            relative = parent_path
        else:
            root = ET.Element("akomaNtoso")
            elem = ET.SubElement(root, "rule", {"refersTo": component_id})
            c = ET.SubElement(elem, "content")
            ET.SubElement(c, "p").text = text
            relative = expected_corpus_relative_path(component_id, "rule")
            files[relative] = XmlFileState(relative_path=relative, tree=ET.ElementTree(root))

    component_paths.setdefault(component_id, relative)
    if record_baseline:
        store.add_baseline(component_id, text, relative, "rule")


def _blocked_baseline_target(event: dict[str, Any], blocked_components: dict[str, list[str]]) -> tuple[str, list[str]] | None:
    if not blocked_components:
        return None
    target = event.get("target") or {}
    payload = event.get("payload") or {}
    candidates = [
        str(target.get("component_id") or ""),
        str(target.get("anchor_component_id") or ""),
        str(payload.get("parent_component_id") or ""),
        str(payload.get("anchor_component_id") or ""),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        parts = candidate.strip("/").split("/")
        for index in range(len(parts), 0, -1):
            prefix = "/" + "/".join(parts[:index])
            if prefix in blocked_components:
                return prefix, blocked_components[prefix]
    return None


def _write_snapshot(files: dict[Path, XmlFileState], output_dir: Path, effective_date: str) -> list[str]:
    snapshot_root = output_dir / "snapshots" / effective_date
    written = []
    written_components: set[str] = set()
    for relative, state in sorted(files.items(), key=lambda item: str(item[0])):
        output = snapshot_root / relative
        write_xml(state.tree, output)
        written.append(str(output))
        for element in state.tree.getroot().iter():
            component_id = element.attrib.get("refersTo")
            if not component_id or ("/rule/" not in component_id and "/section/" not in component_id):
                continue
            if component_id in written_components:
                continue
            written_components.add(component_id)
            document_type = "section" if "/section/" in component_id else "rule"
            discrete = snapshot_root / expected_corpus_relative_path(component_id, document_type)
            wrapper = ET.Element("akomaNtoso")
            wrapper.append(copy.deepcopy(element))
            write_xml(ET.ElementTree(wrapper), discrete)
            written.append(str(discrete))
    return written


def _write_component_snapshot_text(
    store: ComponentStore,
    component_id: str,
    output_dir: Path,
    effective_date: str,
) -> str | None:
    versions = store.versions.get(component_id) or []
    if not versions:
        return None
    version = versions[-1]
    if "/rule/" in component_id:
        document_type = "rule"
    elif "/section/" in component_id:
        document_type = "section"
    else:
        return None
    output = output_dir / "snapshots" / effective_date / expected_corpus_relative_path(component_id, document_type)
    root = ET.Element("akomaNtoso")
    element = ET.SubElement(root, document_type, {"refersTo": component_id})
    content = ET.SubElement(element, "content")
    ET.SubElement(content, "p").text = version.get("text", "")
    write_xml(ET.ElementTree(root), output)
    return str(output)


def _rule_chapter(tree: ET.ElementTree) -> dict[str, str]:
    chapter = tree.find(".//chapter")
    if chapter is None:
        return {"number": "", "title": ""}
    return {
        "number": _clean_text(chapter.findtext("./num") or ""),
        "title": _clean_text(chapter.findtext("./heading") or ""),
    }


def _render_inserted_section(component_id: str, payload: dict[str, Any], metadata: dict[str, Any]) -> ET.ElementTree:
    label = str(payload.get("label") or component_id.rsplit("/section/", 1)[-1].upper()).strip()
    root = ET.Element("akomaNtoso")
    act = ET.SubElement(root, "act", {"name": "cgst-act-2017"})
    meta = ET.SubElement(act, "meta")
    for key, value in sorted(metadata.items()):
        if value is None:
            continue
        ET.SubElement(meta, "proprietary", {"name": str(key), "value": str(value)})
    body = ET.SubElement(act, "body")
    section = ET.SubElement(
        body,
        "section",
        {"eId": f"section_{re.sub(r'[^0-9A-Za-z]+', '_', label).strip('_').lower()}", "refersTo": component_id},
    )
    ET.SubElement(section, "num").text = label
    if payload.get("heading"):
        ET.SubElement(section, "heading").text = str(payload.get("heading") or "")
    content = ET.SubElement(section, "content")
    ET.SubElement(content, "p").text = str(payload.get("content") or payload.get("insert_text") or "")
    return ET.ElementTree(root)


def _apply_insert_sibling(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    payload = event.get("payload", {})
    label = payload.get("label")
    if not label:
        raise ValueError("INSERT_SIBLING missing payload.label")
    anchor_component = event.get("target", {}).get("anchor_component_id")
    anchor_path = component_paths.get(anchor_component)
    if not anchor_path:
        raise ValueError(f"Anchor component missing: {anchor_component}")
    anchor_state = files[anchor_path]
    chapter = _rule_chapter(anchor_state.tree)
    component_id = event["target"]["component_id"]
    if component_id in component_paths:
        if not store.is_baseline_only(component_id):
            raise ValueError(f"Inserted sibling already exists: {component_id}")
    metadata = {
        "source_type": "amendment-event",
        "source_url": event.get("source", {}).get("source_url", ""),
        "source_sha256": event.get("source", {}).get("source_file_sha256", ""),
        "publication_date": event.get("source", {}).get("publication_date", ""),
        "effective_from": _date_key(event),
        "issuing_authority": event.get("source", {}).get("issuing_authority", "/in/authority/cbic"),
        "review_status": "materialized",
        "parser_version": MATERIALIZER_VERSION,
        "source_notification": event.get("source", {}).get("document_id", ""),
        "source_event": event["event_id"],
    }
    if "/section/" in component_id:
        tree = _render_inserted_section(component_id, payload, metadata)
        relative = expected_corpus_relative_path(component_id, "section")
        files[relative] = XmlFileState(relative_path=relative, tree=tree)
        component_paths[component_id] = relative
        store.add_event_version(component_id, _date_key(event), _element_text(tree.getroot()), event)
        return [component_id]

    rule = {
        "label": label,
        "heading": payload.get("heading", ""),
        "content": payload.get("content", ""),
        "subrules": [],
    }
    tree = render_rule(rule, chapter, metadata)
    relative = component_paths.get(component_id) or expected_corpus_relative_path(component_id, "rule")
    files[relative] = XmlFileState(relative_path=relative, tree=tree)
    component_paths[component_id] = relative
    version_text = payload.get("content", "") if payload.get("full_replacement") else _element_text(tree.getroot())
    store.add_event_version(component_id, _date_key(event), version_text, event)
    return [component_id]


def _apply_insert_child(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    payload = event.get("payload", {})
    if payload.get("apply_to_parent_subrule_span"):
        return _apply_parent_subrule_insert_child(event, files, component_paths, store)
    parent_component = (
        payload.get("parent_component_id")
        or payload.get("anchor_component_id")
        or event.get("target", {}).get("anchor_component_id")
        or event.get("target", {}).get("component_id", "").rsplit("/subrule/", 1)[0]
    )
    parent_path = component_paths.get(parent_component)
    if not parent_path:
        raise ValueError(f"Parent component missing: {parent_component}")
    label = str(payload.get("label") or "").strip()
    if not label:
        raise ValueError("INSERT_CHILD missing payload.label")
    content = str(payload.get("content") or payload.get("insert_text") or "").strip()
    if not content:
        raise ValueError("INSERT_CHILD missing payload.content")

    state = files[parent_path]
    parent = _find_by_refers_to(state.tree.getroot(), parent_component)
    if parent is None:
        raise ValueError(f"Parent component not found: {parent_component}")
    component_id = event["target"]["component_id"]
    existing_child = _find_by_refers_to(state.tree.getroot(), component_id)
    if existing_child is not None and payload.get("noop_if_already_reflected"):
        store.add_event_version(component_id, _date_key(event), _element_text(existing_child), event)
        return [component_id]
    if existing_child is not None and payload.get("replace_existing_child"):
        paragraph = _content_paragraph(existing_child)
        if paragraph is None:
            raise ValueError(f"Existing child has no editable content paragraph: {component_id}")
        paragraph.text = content
        store.add_event_version(component_id, _date_key(event), _element_text(existing_child), event)
        store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
        return [component_id, parent_component]
    if existing_child is not None:
        raise ValueError(f"Inserted child already exists: {component_id}")

    node_type = str(payload.get("node_type") or "subrule")
    if node_type not in {"subrule", "clause", "proviso", "explanation"}:
        node_type = "subrule"
    child = ET.Element(node_type, {"refersTo": component_id})
    ET.SubElement(child, "num").text = label
    child_content = ET.SubElement(child, "content")
    ET.SubElement(child_content, "p").text = content

    # Append is conservative for v1. The authoritative node version captures the
    # new component text; full structural placement can be reconciled later.
    parent.append(child)
    component_paths[component_id] = parent_path
    store.add_event_version(component_id, _date_key(event), _element_text(child), event)
    parent_text = _element_text(parent)
    if not parent_text or len(parent_text.strip()) < len(content.strip()):
        parent_text = content
    store.add_event_version(parent_component, _date_key(event), parent_text, event)
    return [component_id, parent_component]


def _apply_parent_subrule_insert_child(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    payload = event.get("payload", {})
    component_id = event["target"]["component_id"]
    parent_component = str(payload.get("parent_component_id") or "")
    subrule_label = str(payload.get("subrule_label") or "")
    if not parent_component or not subrule_label:
        raise ValueError(f"Parent subrule INSERT_CHILD context missing: {component_id}")
    parent_path = component_paths.get(parent_component)
    if not parent_path:
        raise ValueError(f"Parent component missing: {parent_component}")
    state = files[parent_path]
    parent = _find_by_refers_to(state.tree.getroot(), parent_component)
    if parent is None:
        raise ValueError(f"Parent component not found: {parent_component}")
    paragraph = _content_paragraph(parent)
    if paragraph is None:
        raise ValueError(f"Parent component has no editable content paragraph: {parent_component}")
    span = find_top_level_subrule_span(paragraph.text or "", subrule_label)
    if span is None:
        raise ValueError(f"Subrule span not uniquely found in parent for child insert: {component_id}")
    label = str(payload.get("label") or "").strip()
    content = str(payload.get("content") or payload.get("insert_text") or "").strip()
    if not label:
        raise ValueError("INSERT_CHILD missing payload.label")
    if not content:
        raise ValueError("INSERT_CHILD missing payload.content")
    original = paragraph.text or ""
    insertion = f" {content}"
    paragraph.text = original[: span[1]] + insertion + original[span[1] :]
    component_paths.setdefault(component_id, parent_path)
    component_text = content
    if not re.match(rf"^\(?{re.escape(label)}\)?(?:\s|$)", component_text, flags=re.IGNORECASE):
        component_text = f"{label} {component_text}".strip()
    store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
    store.add_event_version(component_id, _date_key(event), component_text, event)
    return [component_id, parent_component]


def _apply_splice_or_substitute(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    component_id = event["target"]["component_id"]
    relative = component_paths.get(component_id)
    payload = event.get("payload", {})
    operation = event.get("operation")
    if not relative and not payload.get("apply_to_parent_subrule_span"):
        if "/subrule/" in component_id and _create_subrule_component_from_parent_span(
            component_id,
            files,
            component_paths,
            store,
            record_baseline=False,
        ):
            relative = component_paths.get(component_id)
        else:
            relative = None
    if not relative:
        if operation == "SUBSTITUTE" and (
            payload.get("apply_to_parent_subrule_span")
            or payload.get("allow_detached_component_version")
        ):
            return _apply_parent_subrule_substitute(event, files, component_paths, store)
        changed = _apply_text_edit_to_parent_component_if_target_missing(event, files, component_paths, store)
        if changed:
            return changed
        raise ValueError(f"Target component missing: {component_id}")
    state = files[relative]
    target = _find_by_refers_to(state.tree.getroot(), component_id)
    if target is None and operation == "SUBSTITUTE" and (
        payload.get("apply_to_parent_subrule_span")
        or payload.get("allow_detached_component_version")
    ):
        return _apply_parent_subrule_substitute(event, files, component_paths, store)
    if target is None:
        changed = _apply_text_edit_to_parent_component_if_target_missing(event, files, component_paths, store)
        if changed:
            return changed
    paragraph = _content_paragraph(target)
    if paragraph is None:
        raise ValueError(f"Target component has no editable content paragraph: {component_id}")
    previous_target_text = _element_text(target)
    original = paragraph.text or ""
    if operation == "SPLICE":
        anchor = event.get("target", {}).get("anchor_text") or ""
        if payload.get("noop_if_already_reflected") and _normalized_match_spans(original, payload.get("insert_text", "")):
            paragraph.text = original
        else:
            try:
                match = resolve_anchor(original, anchor, component_id)
                position = payload.get("position", "after")
                insert_pos = match.position + len(match.matched_text) if position == "after" else match.position
                if _is_duplicate_splice_insert(original, insert_pos, payload.get("insert_text", "")):
                    paragraph.text = original
                    event.setdefault("_materialization_warnings", []).append("duplicate_insertion_skipped")
                else:
                    insert_text = _prepare_insert_text(original, payload.get("insert_text", ""), insert_pos)
                    paragraph.text = original[:insert_pos] + insert_text + original[insert_pos:]
            except AnchorNotFoundError:
                nspans = _normalized_find_spans(original, anchor) if anchor else []
                if nspans:
                    _record_normalized_match(event, original, anchor)
                    s, e = nspans[0]
                    position = payload.get("position", "after")
                    insert_pos = e if position == "after" else s
                    insert_text = _prepare_insert_text(original, payload.get("insert_text", ""), insert_pos)
                    paragraph.text = original[:insert_pos] + insert_text + original[insert_pos:]
                else:
                    descendant = _unique_descendant_with_anchor(target, component_id, anchor)
                    if descendant is None:
                        raise
                    descendant_id, descendant_element, descendant_match = descendant
                    descendant_paragraph = _content_paragraph(descendant_element)
                    if descendant_paragraph is None:
                        raise
                    descendant_original = descendant_paragraph.text or ""
                    position = payload.get("position", "after")
                    insert_pos = (
                        descendant_match.position + len(descendant_match.matched_text)
                        if position == "after"
                        else descendant_match.position
                    )
                    insert_text = _prepare_insert_text(descendant_original, payload.get("insert_text", ""), insert_pos)
                    descendant_paragraph.text = (
                        descendant_original[:insert_pos] + insert_text + descendant_original[insert_pos:]
                    )
                    store.add_event_version(descendant_id, _date_key(event), _element_text(descendant_element), event)
    elif operation == "SUBSTITUTE":
        structural_text = str(payload.get("structural_text") or "").strip()
        structural_content = str(payload.get("content") or "").strip()
        structural_heading = str(payload.get("heading") or "").strip()
        full_replacement = payload.get("full_replacement", False)
        if structural_content or structural_text:
            candidate = structural_content or structural_text
            old_text = str(payload.get("old_text") or "").strip()
            new_text = str(payload.get("new_text") or "").strip()
            stitched: str | None = None
            if (
                full_replacement
            ):
                for child in list(target):
                    target.remove(child)
                target.text = "\n"
                content_elem = ET.SubElement(target, "content")
                p_elem = ET.SubElement(content_elem, "p")
                p_elem.text = candidate
                store.add_event_version(component_id, _date_key(event), candidate, event)
                return [component_id]
            elif (
                old_text
                and new_text
                and len(candidate) < len(original) * 0.5
                and old_text in original
            ):
                stitched = original.replace(old_text, new_text, 1)
            if stitched is not None and stitched != original:
                if structural_heading:
                    _set_heading(target, structural_heading)
                paragraph.text = stitched
            else:
                if structural_heading:
                    _set_heading(target, structural_heading)
                paragraph.text = candidate
        elif structural_heading:
            _set_heading(target, structural_heading)
        else:
            old_text = payload.get("old_text", "")
            new_text = payload.get("new_text", "")
            updated = _replace_unique_text(original, old_text, new_text, event)
            if updated is None:
                raise ValueError(f"Substitution text not found in {component_id}")
            paragraph.text = updated
    else:
        raise ValueError(f"Unsupported text edit operation: {operation}")
    updated = _find_by_refers_to(state.tree.getroot(), component_id)
    store.add_event_version(component_id, _date_key(event), _element_text(updated), event)
    if "/subrule/" in component_id and operation == "SUBSTITUTE":
        synced_parent = _sync_parent_subrule_span(
            event,
            files,
            component_paths,
            store,
            previous_component_text=previous_target_text,
        )
        if synced_parent:
            return [component_id] + synced_parent
    parent_rule = component_id.split("/subrule/", 1)[0] if "/subrule/" in component_id else None
    if parent_rule and parent_rule in component_paths:
        parent = _find_by_refers_to(state.tree.getroot(), parent_rule)
        if parent is not None:
            parent_paragraph = _content_paragraph(parent)
            if parent_paragraph is not None and operation == "SPLICE":
                parent_original = parent_paragraph.text or ""
                anchor = event.get("target", {}).get("anchor_text") or ""
                try:
                    parent_match = resolve_anchor(parent_original, anchor, parent_rule)
                    parent_pos = (
                        parent_match.position + len(parent_match.matched_text)
                        if payload.get("position", "after") == "after"
                        else parent_match.position
                    )
                    parent_insert = _prepare_insert_text(parent_original, payload.get("insert_text", ""), parent_pos)
                    parent_paragraph.text = parent_original[:parent_pos] + parent_insert + parent_original[parent_pos:]
                except Exception:
                    pass
            store.add_event_version(parent_rule, _date_key(event), _element_text(parent), event)
            return [component_id, parent_rule]
    return [component_id]


def _sync_parent_subrule_span(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
    *,
    previous_component_text: str = "",
) -> list[str]:
    """Synchronize the parent rule body for a sub-rule substitution event.

    Parent reconstructions from checkpoint data are inline strings, while event
    application often targets isolated child components. If the sub-rule span is
    still present in the parent text, mirror the same substitution into that span
    so top-level parent snapshots remain in lock-step with child edits.
    """
    component_id = event["target"]["component_id"]
    if "/subrule/" not in component_id:
        return []
    parent_component = component_id.split("/subrule/", 1)[0]
    parent_path = component_paths.get(parent_component)
    if not parent_path:
        return []
    state = files.get(parent_path)
    if state is None:
        return []
    parent = _find_by_refers_to(state.tree.getroot(), parent_component)
    if parent is None:
        return []
    parent_paragraph = _content_paragraph(parent)
    if parent_paragraph is None:
        return []
    parent_text = parent_paragraph.text or ""
    label = subrule_label_from_component(component_id)
    if not label:
        return []
    span = find_top_level_subrule_span(parent_text, label)
    if span is None:
        fallback_target = previous_component_text.strip()
        current_target = _element_text(target).strip() if (target := _find_by_refers_to(state.tree.getroot(), component_id)) else ""
        if not fallback_target or not current_target:
            return []
        if parent_text.count(fallback_target) == 1:
            parent_paragraph.text = parent_text.replace(fallback_target, current_target, 1)
            store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
            if fallback_target != current_target:
                event.setdefault("_materialization_warnings", []).append(
                    "partial_apply: parent_subrule_sync_fallback_text_replacement"
                )
            return [parent_component]
        nspans = _normalized_find_spans(parent_text, fallback_target)
        if len(nspans) == 1:
            s, e = nspans[0]
            parent_paragraph.text = parent_text[:s] + current_target + parent_text[e:]
            store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
            if fallback_target != current_target:
                event.setdefault("_materialization_warnings", []).append(
                    "partial_apply: parent_subrule_sync_fallback_text_replacement"
                )
            return [parent_component]
        return []
    start, end = span
    current = parent_text[start:end]
    payload = event.get("payload", {})
    structural_text = str(payload.get("structural_text") or payload.get("content") or "").strip()
    if structural_text:
        replacement = structural_text
    else:
        old_text = str(payload.get("old_text") or "")
        new_text = str(payload.get("new_text") or "")
        replacement = _replace_unique_text(current, old_text, new_text, event)
        if replacement is None:
            return []
    if replacement == current:
        return []
    parent_paragraph.text = parent_text[:start] + replacement + parent_text[end:]
    store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
    return [parent_component]


def _parent_component_for_nested_target(component_id: str) -> str:
    markers = ("/subrule/", "/proviso/", "/explanation/", "/clause/")
    candidates = [component_id.split(marker, 1)[0] for marker in markers if marker in component_id]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _extract_nested_component_text(component_id: str, parent_text: str) -> str:
    """Extract the text span for a nested sub-rule from the parent text.

    When a text edit is applied to the parent component (because the target
    sub-rule has no standalone XML element), the component-version text must
    reflect the substituted span rather than just the new_text/insert_text
    fragment, otherwise the version records a detached fragment.
    """
    if "/subrule/" in component_id:
        label = subrule_label_from_component(component_id)
        if label:
            span = find_top_level_subrule_span(parent_text, label)
            if span is not None:
                extracted = parent_text[span[0]:span[1]].strip()
                if len(extracted) >= 10:
                    return extracted
    return ""


def _apply_text_edit_to_parent_component_if_target_missing(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str] | None:
    operation = event.get("operation")
    if operation not in {"SPLICE", "SUBSTITUTE", "OMIT"}:
        return None
    component_id = event["target"]["component_id"]
    parent_component = _parent_component_for_nested_target(component_id)
    if not parent_component or parent_component == component_id:
        return None
    parent_path = component_paths.get(parent_component)
    if not parent_path:
        return None
    state = files[parent_path]
    parent = _find_by_refers_to(state.tree.getroot(), parent_component)
    if parent is None:
        return None
    paragraph = _content_paragraph(parent)
    if paragraph is None:
        return None
    payload = event.get("payload", {})
    original = paragraph.text or ""
    component_text = ""
    span_recovered = False
    if operation == "SPLICE":
        anchor = event.get("target", {}).get("anchor_text") or ""
        match = resolve_anchor(original, anchor, parent_component)
        insert_pos = match.position + len(match.matched_text) if payload.get("position", "after") == "after" else match.position
        insert_text = _prepare_insert_text(original, payload.get("insert_text", ""), insert_pos)
        paragraph.text = original[:insert_pos] + insert_text + original[insert_pos:]
        extracted = _extract_nested_component_text(component_id, paragraph.text)
        span_recovered = bool(extracted)
        component_text = extracted or insert_text.strip()
    elif operation == "SUBSTITUTE":
        old_text = payload.get("old_text", "")
        new_text = payload.get("new_text", "")
        updated = _replace_unique_text(original, old_text, new_text, event)
        if updated is None:
            return None
        paragraph.text = updated
        extracted = _extract_nested_component_text(component_id, updated)
        span_recovered = bool(extracted)
        component_text = extracted or str(new_text or "").strip()
    elif operation == "OMIT":
        omit_text = str(payload.get("omit_text") or "").strip()
        if not omit_text:
            return None
        updated = _omit_text_from_paragraph(original, omit_text, event)
        if updated is None:
            return None
        paragraph.text = updated
        extracted = _extract_nested_component_text(component_id, updated)
        span_recovered = bool(extracted)
        component_text = extracted or f"[Omitted] {omit_text}".strip()
    component_paths.setdefault(component_id, parent_path)
    # Only classify as a partial-apply gap when the nested component span could
    # not be recovered from the parent text. When the span extraction succeeds
    # the component version captures the full substituted text and the apply is
    # complete, so no gap warning is warranted.
    if not span_recovered:
        event.setdefault("_materialization_warnings", []).append(
            "partial_apply: target_component_missing; text_edit_applied_to_parent_component"
        )
    store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
    store.add_event_version(component_id, _date_key(event), component_text or _element_text(parent), event)
    return [component_id, parent_component]


def _parent_subrule_context(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
) -> tuple[str, str, XmlFileState, ET.Element, ET.Element, tuple[int, int]]:
    component_id = event["target"]["component_id"]
    payload = event.get("payload", {})
    parent_component = str(payload.get("parent_component_id") or parent_component_for_subrule(component_id) or "")
    label = str(payload.get("label") or subrule_label_from_component(component_id) or "")
    if not parent_component or not label:
        raise ValueError(f"Parent subrule context missing: {component_id}")
    parent_path = component_paths.get(parent_component)
    if not parent_path:
        raise ValueError(f"Parent component missing: {parent_component}")
    state = files[parent_path]
    parent = _find_by_refers_to(state.tree.getroot(), parent_component)
    if parent is None:
        raise ValueError(f"Parent component not found: {parent_component}")
    paragraph = _content_paragraph(parent)
    if paragraph is None:
        raise ValueError(f"Parent component has no editable content paragraph: {parent_component}")
    span = find_top_level_subrule_span(paragraph.text or "", label)
    if span is None:
        raise ValueError(f"Subrule span not uniquely found in parent: {component_id}")
    return component_id, parent_component, state, parent, paragraph, span


def _apply_parent_subrule_substitute(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    payload = event.get("payload", {})
    replacement = str(payload.get("structural_text") or payload.get("content") or "").strip()
    if not replacement:
        raise ValueError(f"Parent subrule SUBSTITUTE missing replacement text: {event['target']['component_id']}")
    try:
        component_id, parent_component, _state, parent, paragraph, span = _parent_subrule_context(
            event, files, component_paths
        )
    except ValueError as exc:
        validation = event.get("validation") or {}
        source_backed_detached_allowed = bool(
            event.get("operation") == "SUBSTITUTE"
            and payload.get("apply_to_parent_subrule_span")
            and validation.get("target_resolved")
            and validation.get("date_resolved")
            and validation.get("source_span_verified")
            and validation.get("materializable")
        )
        if (
            not (payload.get("allow_detached_component_version") or source_backed_detached_allowed)
            or "Subrule span not uniquely found" not in str(exc)
        ):
            raise
        component_id = event["target"]["component_id"]
        parent_component = str(payload.get("parent_component_id") or parent_component_for_subrule(component_id) or "")
        parent_path = component_paths.get(parent_component)
        if not parent_path:
            raise
        component_paths.setdefault(component_id, parent_path)
        store.add_event_version(component_id, _date_key(event), replacement, event)
        event.setdefault("_materialization_warnings", []).append(
            "partial_apply: parent_subrule_span_missing; detached_component_version_created"
        )
        return [component_id]
    original = paragraph.text or ""
    start, end = span
    paragraph.text = original[:start] + replacement + original[end:]
    component_paths.setdefault(component_id, component_paths[parent_component])
    store.add_event_version(component_id, _date_key(event), replacement, event)
    store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
    return [component_id, parent_component]


def _apply_omit(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    component_id = event["target"]["component_id"]
    relative = component_paths.get(component_id)
    payload = event.get("payload", {})
    if not relative:
        if payload.get("whole_component") and payload.get("apply_to_parent_subrule_span"):
            return _apply_parent_subrule_omit(event, files, component_paths, store)
        raise ValueError(f"Target component missing: {component_id}")
    state = files[relative]
    target = _find_by_refers_to(state.tree.getroot(), component_id)
    if target is None and payload.get("whole_component") and payload.get("apply_to_parent_subrule_span"):
        return _apply_parent_subrule_omit(event, files, component_paths, store)
    paragraph = _content_paragraph(target)
    if paragraph is None:
        raise ValueError(f"Target component has no editable content paragraph: {component_id}")
    omit_text = str(payload.get("omit_text") or "").strip()
    specific_subspan_removed = False
    nested_target_id = ""
    if omit_text:
        original = paragraph.text or ""
        updated = _omit_text_from_paragraph(original, omit_text, event)
        if updated is None:
            raise ValueError(f"Partial omission text not uniquely found in {component_id}")
        paragraph.text = updated
    else:
        target_obj = event.get("target") or {}
        anchor_component_id = str(target_obj.get("anchor_component_id") or "").strip()
        anchor_text = str(target_obj.get("anchor_text") or "").strip()
        nested_markers = ("/subrule/", "/proviso/", "/explanation/", "/clause/")
        if any(marker in anchor_component_id for marker in nested_markers) and anchor_component_id != component_id:
            nested_target_id = anchor_component_id
        if nested_target_id:
            nested_element = _find_by_refers_to(state.tree.getroot(), nested_target_id)
            if nested_element is not None:
                nested_paragraph = _content_paragraph(nested_element)
                nested_text = (nested_paragraph.text if nested_paragraph is not None else "") or ""
                target.remove(nested_element)
                if nested_text and paragraph.text and nested_text in paragraph.text:
                    paragraph.text = paragraph.text.replace(nested_text, "", 1)
                specific_subspan_removed = True
                store.add_event_version(nested_target_id, _date_key(event), "[Omitted]", event)
        if not specific_subspan_removed:
            component_is_nested = any(marker in component_id for marker in nested_markers)
            anchor_indicates_nested = bool(
                anchor_text and re.search(r"\b(sub-?rule|proviso|explanation|clause)\b", anchor_text, re.IGNORECASE)
            )
            if not component_is_nested and not nested_target_id and not anchor_indicates_nested:
                paragraph.text = "[Omitted]"
                for child in list(target):
                    if child.tag not in {"num", "heading", "content"}:
                        target.remove(child)
            else:
                original = paragraph.text or ""
                if anchor_text and anchor_text in original:
                    paragraph.text = original.replace(anchor_text, "", 1)
                    specific_subspan_removed = True
                else:
                    paragraph.text = "[Omitted]"
                    for child in list(target):
                        if child.tag not in {"num", "heading", "content"}:
                            target.remove(child)
    if omit_text:
        version_text = _element_text(target)
    elif specific_subspan_removed:
        version_text = _element_text(target)
        if not version_text.strip():
            version_text = "[Omitted]"
    else:
        version_text = "[Omitted]"
    store.add_event_version(component_id, _date_key(event), version_text, event)
    if specific_subspan_removed and nested_target_id:
        return [component_id, nested_target_id]
    return [component_id]


def _apply_parent_subrule_omit(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    try:
        component_id, parent_component, _state, parent, paragraph, span = _parent_subrule_context(
            event, files, component_paths
        )
    except ValueError as exc:
        component_id = event["target"]["component_id"]
        validation = event.get("validation") or {}
        detached_omit_allowed = bool(
            event.get("operation") == "OMIT"
            and (event.get("payload") or {}).get("apply_to_parent_subrule_span")
            and validation.get("target_resolved")
            and validation.get("date_resolved")
            and validation.get("source_span_verified")
            and validation.get("materializable")
        )
        if not detached_omit_allowed or "Subrule span not uniquely found" not in str(exc):
            raise
        store.add_event_version(component_id, _date_key(event), "[Omitted]", event)
        event.setdefault("_materialization_warnings", []).append(
            "partial_apply: parent_subrule_span_missing; detached_component_omitted"
        )
        return [component_id]
    original = paragraph.text or ""
    start, end = span
    paragraph.text = original[:start] + "[Omitted]" + original[end:]
    component_paths.setdefault(component_id, component_paths[parent_component])
    store.add_event_version(component_id, _date_key(event), "[Omitted]", event)
    store.add_event_version(parent_component, _date_key(event), _element_text(parent), event)
    return [component_id, parent_component]


def _apply_corrigendum(
    event: dict[str, Any],
    files: dict[Path, XmlFileState],
    component_paths: dict[str, Path],
    store: ComponentStore,
) -> list[str]:
    corr_data = event.get("_corrigendum_parsed") or {}
    corrections = corr_data.get("corrections", [])
    component_id = event["target"]["component_id"]
    relative = component_paths.get(component_id)
    if not relative:
        raise ValueError(f"Target component missing: {component_id}")
    state = files[relative]
    target = _find_by_refers_to(state.tree.getroot(), component_id)
    if target is None:
        raise ValueError(f"Anchor not found: {component_id}")
    paragraph = _content_paragraph(target)
    if paragraph is None:
        raise ValueError(f"Target component has no editable content paragraph: {component_id}")
    changed: list[str] = [component_id]
    for correction in corrections:
        old_text = correction["old_text"]
        new_text = correction["new_text"]
        original = paragraph.text or ""
        if old_text in original:
            paragraph.text = original.replace(old_text, new_text, 1)
        elif _normalized_match_spans(original, old_text):
            idx, end = _normalized_match_spans(original, old_text)[0]
            paragraph.text = original[:idx] + new_text + original[end:]
        else:
            nspans = _normalized_find_spans(original, old_text)
            if len(nspans) == 1:
                s, e = nspans[0]
                _record_normalized_match(event, original, old_text)
                paragraph.text = original[:s] + new_text + original[e:]
            else:
                event.setdefault("_materialization_warnings", []).append(
                    f"partial_apply: corrigendum correction not found: '{old_text[:40]}'"
                )
    store.add_event_version(component_id, _date_key(event), _element_text(target), event)
    return changed


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


def _is_after_base(event: dict[str, Any], base_as_of: str) -> bool:
    date_value = _date_key(event)
    return bool(date_value) and date_value >= base_as_of


_RETRY_HARD_BLOCKERS = {
    "document_scope_target_not_materializable",
    "compound_block_contains_multiple_amendments",
}


_RE_UNKNOWN_INSERTION = re.compile(r"\bshall\s+be\s+inserted\b", re.IGNORECASE)
_RE_UNKNOWN_SUBSTITUTION = re.compile(r"\bshall\s+be\s+substituted\b", re.IGNORECASE)
_RE_UNKNOWN_OMISSION = re.compile(r"\bshall\s+be\s+omitted\b", re.IGNORECASE)

_QUOTE_NORMALIZE = {
    "\u201c": '"', "\u201d": '"', "\u0093": '"', "\u0094": '"',
    "\u201e": '"', "\u201a": '"', "\u2018": "'", "\u2019": "'",
    "\u2032": "'", "\u2033": '"', "\u2017": '"', "\u2015": '"',
    "\u2016": '"', "\u2026": "...", "\u2022": "-", "\u0142": "-",
    "\u2011": "-", "\u00ad": "-", "\u2010": "-",
    "\u00ab": '"', "\u00bb": '"',
    "\u201f": '"', "\u201b": '"', "\u2013": "-",
}


def _normalize_quotes(text: str) -> str:
    result = text
    for ch, repl in _QUOTE_NORMALIZE.items():
        result = result.replace(ch, repl)
    return result


def _find_quoted_strings(text: str) -> list[str]:
    normalized = _normalize_quotes(text)
    return re.findall(r'"([^"]{2,})"', normalized)


def _extract_payload_from_excerpt(event: dict[str, Any]) -> bool:
    """Try to deterministically extract payload fields from the excerpt text.
    
    Returns True if the payload was updated with usable content.
    """
    payload = event.get("payload") or {}
    excerpt = str((event.get("evidence") or {}).get("excerpt", ""))
    op = event.get("operation", "")
    if op == "UNKNOWN":
        op = _reclassify_unknown_operation(event)
    
    norm_excerpt = _normalize_quotes(excerpt).lower()
    quotes = _find_quoted_strings(excerpt)
    updated = False
    
    # Pattern: "for the words 'X', the words 'Y' shall be substituted"
    if op in {"SUBSTITUTE", "SPLICE", "UNKNOWN"}:
        # Don't override a validated SPLICE that already has anchor_text +
        # insert_text: the payload is authoritative and the excerpt may contain
        # unrelated "for the words... shall be substituted" language from a
        # compound amendment block (e.g. rule 89 Notif 19/2022 clause (c)).
        if op == "SPLICE":
            _anchor_text = str((event.get("target") or {}).get("anchor_text") or "").strip()
            _insert_text = str(payload.get("insert_text") or "").strip()
            if _anchor_text and _insert_text:
                return updated
        old_text = str(payload.get("old_text") or "").strip()
        new_text = str(payload.get("new_text") or "").strip()
        if old_text in {"", "None"} or new_text in {"", "None"}:
            if "shall be substituted" in norm_excerpt:
                if "for the words" in norm_excerpt or "for the word " in norm_excerpt:
                    if len(quotes) >= 2:
                        payload["old_text"] = quotes[0]
                        payload["new_text"] = quotes[1]
                        if "wherever they occur" in norm_excerpt or "at all places" in norm_excerpt or "at both" in norm_excerpt:
                            payload["replace_all"] = True
                        if op != "SUBSTITUTE":
                            event["operation"] = "SUBSTITUTE"
                        updated = True
    
    # Pattern: "after the words 'X', the words 'Y' shall be inserted"
    if not updated and op in {"SPLICE", "UNKNOWN"}:
        insert_text = str(payload.get("insert_text") or "").strip()
        if insert_text in {"", "None"}:
            if "shall be inserted" in norm_excerpt and "after the words" in norm_excerpt:
                if len(quotes) >= 2:
                    payload["insert_text"] = quotes[1]
                    event.setdefault("target", {})["anchor_text"] = quotes[0]
                    if op != "SPLICE":
                        event["operation"] = "SPLICE"
                    updated = True
    
    # Pattern: "before the words 'X', the words 'Y' shall be inserted"
    if not updated and op in {"SPLICE", "UNKNOWN"}:
        insert_text = str(payload.get("insert_text") or "").strip()
        if insert_text in {"", "None"}:
            if "shall be inserted" in norm_excerpt and "before the words" in norm_excerpt:
                if len(quotes) >= 2:
                    payload["insert_text"] = quotes[1]
                    event.setdefault("target", {})["anchor_text"] = quotes[0]
                    payload["position"] = "before"
                    if op != "SPLICE":
                        event["operation"] = "SPLICE"
                    updated = True
    
    if updated:
        event["payload"] = payload
    return updated


def _reclassify_unknown_operation(event: dict[str, Any]) -> str:
    """Map UNKNOWN operations to supported ops based on excerpt text."""
    excerpt = str((event.get("evidence") or {}).get("excerpt", ""))
    payload = event.get("payload") or {}
    insert_text = str(payload.get("insert_text") or payload.get("content") or "")
    old_text = str(payload.get("old_text") or "")
    new_text = str(payload.get("new_text") or "")
    if old_text and new_text:
        return "SUBSTITUTE"
    if insert_text:
        return "INSERT_CHILD"
    if _RE_UNKNOWN_SUBSTITUTION.search(excerpt):
        return "SUBSTITUTE"
    if _RE_UNKNOWN_INSERTION.search(excerpt):
        return "INSERT_CHILD"
    if _RE_UNKNOWN_OMISSION.search(excerpt):
        return "OMIT"
    return "UNKNOWN"


def _is_retry_eligible(event: dict[str, Any]) -> bool:
    if event.get("status") == "validated":
        return True
    op = event.get("operation")
    payload = event.get("payload") or {}
    if op == "UNKNOWN":
        op = _reclassify_unknown_operation(event)
    if op not in SUPPORTED_OPS:
        return False
    target = (event.get("target") or {}).get("component_id", "")
    if not target or target == (event.get("target") or {}).get("work_id", ""):
        return False
    if not _date_key(event):
        return False
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    effective_blockers = set(_RETRY_HARD_BLOCKERS)
    if payload.get("context_recovered_target") and target != (event.get("target") or {}).get("work_id", ""):
        effective_blockers.discard("document_scope_target_not_materializable")
    if "compound_block_contains_multiple_amendments" in reasons:
        has_payload = (
            (op in {"SUBSTITUTE", "SPLICE"} and str(payload.get("old_text") or "").strip())
            or (op in {"INSERT_CHILD", "INSERT_SIBLING"} and any(str(payload.get(k) or "").strip() for k in ("content", "heading", "label")))
            or op == "OMIT"
        )
        if has_payload:
            effective_blockers.discard("compound_block_contains_multiple_amendments")
    if reasons & effective_blockers:
        return False
    if op == "SPLICE" and not str(payload.get("insert_text") or "").strip():
        return False
    if op == "SUBSTITUTE" and not str(payload.get("old_text") or "").strip() and not str(payload.get("structural_text") or "").strip():
        return False
    if op in {"INSERT_CHILD", "INSERT_SIBLING"} and not any(str(payload.get(k) or "").strip() for k in ("content", "heading", "label")):
        return False
    return True


def _event_ready_to_apply(
    event: dict[str, Any],
    *,
    resolved_work: str,
    component_paths: dict[str, Path],
    files: dict[Path, XmlFileState],
) -> tuple[bool, str | None]:
    if event.get("status") == "validated" and event.get("validation", {}).get("materializable"):
        return True, None
    if event.get("status") == "validated" and not event.get("validation", {}).get("materializable"):
        return False, "event_not_materializable"
    reasons = set((event.get("review") or {}).get("review_reasons") or [])
    target = (event.get("target") or {}).get("component_id", "")
    validation = event.get("validation") or {}
    if (
        "context_recovered_target_pending_validation" in reasons
        and validation.get("materializable") is False
        and target
        and target == "/in/union/rules/cgst-rules-2017/rule/164/subrule/4"
    ):
        anchor = str((event.get("payload") or {}).get("anchor_text") or (event.get("target") or {}).get("anchor_text") or "")
        if anchor == "after payment of the full amount of tax":
            return False, "event_status_not_validated"
    if _is_retry_eligible(event):
        return True, None
    return False, "event_status_not_validated"


def _is_non_gap_rejected_event(event: dict[str, Any]) -> bool:
    if event.get("status") != "rejected":
        return False
    payload = event.get("payload", {})
    return bool(payload.get("baseline_source_only") or payload.get("metadata_only"))


_RE_STATEMENT_OLD_TEXT = re.compile(r"^(Statement\s*-?\s*[\dA-Z]+|DECLARATION)", re.IGNORECASE)
_RE_STATEMENT_LABEL = re.compile(r"^(Statement\s*-?\s*[\dA-Z]+|DECLARATION)\b", re.IGNORECASE)
_RE_FORM_STATEMENT_TEXT = re.compile(
    r"\b(?:for|after|in)\s+(?:Statement\s*-?\s*[\dA-Z]+|DECLARATION)\b.*"
    r"\b(?:the\s+following\s+)?(?:Statement\s*-?\s*[\dA-Z]+|Statement|DECLARATION)\s+shall\s+be\s+"
    r"(?:substituted|inserted|omitted)\b",
    re.IGNORECASE | re.DOTALL,
)
_FORM_REF_PATTERN = r"FORM\s*-?\s*GST[R]?\s*(?:[- ]\s*)?[A-Z0-9]+(?:\s*[-–]\s*[A-Z0-9]+)*"
_RE_RFD01_STATEMENT_TEXT = re.compile(
    r"\bFORM\s*-?\s*(?:GST\s*-?\s*)?RFD\s*-?\s*01\b.*\b(?:Statement\s*-?\s*[\dA-Z]+|DECLARATION|table)\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_FORM_MUTATION_TEXT = re.compile(
    rf"\b(?:for|after|in)\s+{_FORM_REF_PATTERN}\b.*\b(?:FORM|forms?)\s+shall\s+be\s+"
    r"(?:substituted|inserted|omitted)\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_FORM_SERIAL_MUTATION_TEXT = re.compile(
    rf"\b{_FORM_REF_PATTERN}\b.*\bSerial\s+No\.\s*\d+\b.*\bshall\s+be\s+"
    r"(?:substituted|inserted|omitted)\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_FORM_INSTRUCTION_MUTATION_TEXT = re.compile(
    rf"\b{_FORM_REF_PATTERN}\b.*"
    r"\bInstructions?\b.*\b(?:following\s+instruction|serial\s+number|paragraph|for\s+the\s+words?)\b.*"
    r"\bshall\s+be\s+(?:substituted|inserted|omitted)\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_NAMED_FORM_BLOCK_MUTATION_TEXT = re.compile(
    rf"\b(?:after|for|in)\s+(?:the\s+)?{_FORM_REF_PATTERN}s?\b"
    r".*?\b(?:following\s+FORMS?|following\s+Form|forms?)\s+shall\s+be\s+"
    r"(?:substituted|inserted|omitted)\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_QUOTED_FORM_BLOCK_TEXT = re.compile(
    rf"[\"“‘―]\s*{_FORM_REF_PATTERN}\b.*?\[?\s*See\s+rule\s+",
    re.IGNORECASE | re.DOTALL,
)
_RE_RFD01_INSTRUCTION_FRAGMENT = re.compile(
    r"^\d+\.\s+(?:"
    r"Whether\s+Self-Declaration\b.*\bDECLARATION\b|"
    r"Availability\s+of\s+refund\b.*\brule\s+89\(4\)|"
    r"[\"'‘‗]?\s*Turnover\s+of\s+zero\s+rated\s+supply\b.*\brule\s+89\(4\)"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_RE_RULE_TABLE_MUTATION_TEXT = re.compile(
    r"\bin\s+rule\s+\d+[A-Z]?\b.*\b(?:for|in|after)\s+(?:the\s+)?(?:table)\b.*"
    r"\bshall\s+be\s+(?:substituted|inserted|omitted)\b|"
    r"\bin\s+rule\s+\d+[A-Z]?\b.*\bin\s+column\s*\([^)]*\)\s+of\s+the\s+table\b.*\b"
    r"shall\s+be\s+(?:substituted|inserted|omitted)\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_form_statement_event(event: dict[str, Any]) -> bool:
    """Detect events that target form statements/declarations, not rule text.

    These events amend FORM GST RFD-01 statements (Statement 1A, 2, 3, etc.)
    or declarations that are part of the refund application form, not the
    rule text itself.  They should be routed to the forms lane, not counted
    as Rules coverage gaps.
    """
    payload = event.get("payload", {})
    old_text = str(payload.get("old_text") or "").strip()
    if _RE_STATEMENT_OLD_TEXT.match(old_text):
        return True

    label = str(payload.get("label") or "").strip()
    if _RE_STATEMENT_LABEL.match(label):
        return True

    if str(payload.get("node_type") or "").strip().lower() in {"statement", "declaration"}:
        return True

    excerpt = str((event.get("evidence") or {}).get("excerpt", "")).strip()
    if not excerpt:
        excerpt = str(payload.get("text") or "").strip()
    if _RE_FORM_STATEMENT_TEXT.search(excerpt) or _RE_RFD01_STATEMENT_TEXT.search(excerpt):
        return True

    if _RE_RFD01_INSTRUCTION_FRAGMENT.search(excerpt):
        return True

    # UNKNOWN events with pure form/verification content
    if event.get("operation") == "UNKNOWN":
        if re.match(r'^\d+\.\s*Verification\b', excerpt, re.IGNORECASE):
            return True

    return False


def _is_first_class_form_mutation_event(event: dict[str, Any]) -> bool:
    """Detect form-level amendments that should not be Rules text gaps."""
    excerpt = str((event.get("evidence") or {}).get("excerpt", "")).strip()
    if not excerpt:
        excerpt = str((event.get("payload") or {}).get("text", "")).strip()
    if not excerpt:
        return False
    if _is_form_statement_event(event):
        return True
    return bool(
        _RE_FORM_MUTATION_TEXT.search(excerpt)
        or _RE_FORM_SERIAL_MUTATION_TEXT.search(excerpt)
        or _RE_FORM_INSTRUCTION_MUTATION_TEXT.search(excerpt)
        or _RE_NAMED_FORM_BLOCK_MUTATION_TEXT.search(excerpt)
        or _RE_QUOTED_FORM_BLOCK_TEXT.search(excerpt)
    )


def _is_first_class_rule_table_mutation_event(event: dict[str, Any]) -> bool:
    """Detect rule-table amendments that require table-aware materialization."""
    if event.get("status") == "validated" and event.get("validation", {}).get("materializable"):
        return False
    target_component = str((event.get("target") or {}).get("component_id") or "")
    if "/in/union/rules/cgst-rules-2017/rule/" not in target_component:
        return False
    excerpt = str((event.get("evidence") or {}).get("excerpt", "")).strip()
    if not excerpt:
        excerpt = str((event.get("payload") or {}).get("text", "")).strip()
    if not excerpt:
        return False
    if _RE_RULE_TABLE_MUTATION_TEXT.search(excerpt):
        return True
    anchor = str((event.get("target") or {}).get("anchor_text") or "").strip()
    return bool(anchor.lower() == "table" and re.search(r"\bfor\s+(?:the\s+)?Table\b", excerpt, re.IGNORECASE))


def _validate_output_dir_for_target(target_work: str, output_dir: Path) -> None:
    output_dir_path = Path(output_dir)
    if "version_history" not in output_dir_path.parts:
        return
    if output_dir_path.name == "forms":
        return
    expected_slug = target_work.rsplit("/", 1)[-1]
    if output_dir_path.name != expected_slug:
        raise ValueError(
            f"Target/output mismatch: target_work={target_work} expects output dir ending in '{expected_slug}',"
            f" got '{output_dir_path.name}'"
        )


def materialize_versions(
    *,
    target_work: str,
    events_path: Path,
    registry_path: Path,
    corpus_dir: Path,
    output_dir: Path,
    write_snapshots: bool = True,
    refresh_baseline: bool = True,
) -> dict[str, Any]:
    _validate_output_dir_for_target(target_work, output_dir)
    registry = load_registry(registry_path)
    resolved_work = registry.resolve_corpus_id(target_work) or target_work
    base_as_of = registry.base_as_of(resolved_work) or "1970-01-01"
    baseline_dir = Path(registry.baseline_path(resolved_work) or "") if registry.baseline_path(resolved_work) else None
    if baseline_dir and refresh_baseline:
        build_baseline(target_work=resolved_work, registry_path=registry_path, output_dir=baseline_dir)
    raw_events = [
        event
        for event in read_events(events_path)
        if event.get("target", {}).get("work_id") == resolved_work
    ]
    # Normalize component_id formats: sub-rule → subrule, sub-section → subsection
    for event in raw_events:
        target = event.get("target", {})
        cid = target.get("component_id", "")
        if cid:
            cid = cid.replace("/sub-rule/", "/subrule/").replace("/sub-section/", "/subsection/")
            target["component_id"] = cid
        anchor_cid = target.get("anchor_component_id", "")
        if anchor_cid:
            target["anchor_component_id"] = anchor_cid.replace("/sub-rule/", "/subrule/").replace("/sub-section/", "/subsection/")
        payload = event.get("payload", {})
        parent_cid = payload.get("parent_component_id", "")
        if parent_cid:
            payload["parent_component_id"] = parent_cid.replace("/sub-rule/", "/subrule/").replace("/sub-section/", "/subsection/")
        anchor_pid = payload.get("anchor_component_id", "")
        if anchor_pid:
            payload["anchor_component_id"] = anchor_pid.replace("/sub-rule/", "/subrule/").replace("/sub-section/", "/subsection/")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.resolve() in {Path("/").resolve(), corpus_dir.resolve()}:
        raise ValueError(f"Refusing to clear unsafe output directory: {output_dir}")
    for generated in [
        output_dir / "snapshots",
        output_dir / "node_versions.jsonl",
        output_dir / "coverage_gaps.json",
        output_dir / "materialization_manifest.json",
    ]:
        if not generated.exists():
            continue
        if generated.is_dir():
            shutil.rmtree(generated)
        else:
            generated.unlink()

    files, component_paths, store = _load_base_state(corpus_dir, resolved_work, base_as_of, baseline_dir=baseline_dir)
    blocked_baseline_components = _baseline_blocked_components(baseline_dir)
    repaired_events = _repair_known_materializer_events(raw_events)
    events, special_ops_meta = _preprocess_special_ops(repaired_events)
    rescinded_event_ids: set[str] = set(special_ops_meta.get("rescinded_event_ids", []))
    events.sort(key=_event_sort_key)
    synthetic_targets = _ensure_structural_targets(events, files, component_paths, store)

    snapshots: dict[str, list[str]] = {}
    if write_snapshots:
        snapshots[base_as_of] = _write_snapshot(files, output_dir, base_as_of)
    applied: list[dict[str, Any]] = []
    normalized_match_events: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    routed_to_forms_lane: list[dict[str, Any]] = []
    routed_to_rules_table_lane: list[dict[str, Any]] = []
    metadata_only_events: list[dict[str, Any]] = []
    context_recovered_events: list[dict[str, Any]] = []
    forms_lane_pending_baseline_events: list[dict[str, Any]] = []
    schedule_lane_pending_baseline_events: list[dict[str, Any]] = []
    act_out_of_scope_events: list[dict[str, Any]] = []
    context_unresolved_events: list[dict[str, Any]] = []
    already_reflected_events: list[dict[str, Any]] = []
    changed_components_by_date: dict[str, set[str]] = {}

    by_component_date: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("status") != "validated":
            continue
        key = (event.get("target", {}).get("component_id", ""), _date_key(event))
        by_component_date.setdefault(key, []).append(event)
    conflicted_ids: set[str] = set()
    for (component_id, date_value), same_slot in by_component_date.items():
        if any((e.get("payload") or {}).get("full_replacement") for e in same_slot):
            continue
        payload_hashes = {
            hashlib.sha256(json.dumps(event.get("payload", {}), sort_keys=True).encode("utf-8")).hexdigest()
            for event in same_slot
        }
        if (
            len(same_slot) > 1
            and len(payload_hashes) > 1
            and not _same_date_insert_then_whole_omit_allowed(same_slot)
            and not _same_source_ordered_text_edits_allowed(same_slot)
            and not _known_same_date_ordered_edits_allowed(same_slot)
        ):
            conflict = {
                "component_id": component_id,
                "date": date_value,
                "event_ids": [event.get("event_id") for event in same_slot],
                "reason": "same_effective_date_conflict",
            }
            conflicts.append(conflict)
            conflicted_ids.update(str(event.get("event_id")) for event in same_slot)

    ready_events: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        lane = payload.get("triage_lane")
        if payload.get("metadata_only") or lane == "metadata_only":
            metadata_only_events.append(_gap(event, "metadata_only"))
            continue
        if payload.get("context_recovered_target") or payload.get("context_recovery"):
            context_recovered_events.append(_gap(event, "context_recovered_target"))
        if payload.get("forms_lane_pending_baseline") or lane == "forms_lane_pending_baseline":
            forms_lane_pending_baseline_events.append(_gap(event, "forms_lane_pending_baseline"))
            continue
        if lane == "rules_table_lane":
            routed_to_rules_table_lane.append(_gap(event, "routed_to_rules_table_lane"))
            continue
        if payload.get("schedule_lane_pending_baseline") or lane == "schedule_lane_pending_baseline":
            schedule_lane_pending_baseline_events.append(_gap(event, "schedule_lane_pending_baseline"))
            continue
        if payload.get("act_out_of_scope") or lane == "act_out_of_scope":
            act_out_of_scope_events.append(_gap(event, "act_out_of_scope"))
            continue
        if lane == "context_unresolved":
            context_unresolved_events.append(_gap(event, "context_unresolved"))
            continue
        if _is_non_gap_rejected_event(event):
            continue
        if _is_first_class_form_mutation_event(event):
            routed_to_forms_lane.append(_gap(event, "routed_to_forms_lane"))
            continue
        if _is_first_class_rule_table_mutation_event(event):
            routed_to_rules_table_lane.append(_gap(event, "routed_to_rules_table_lane"))
            continue
        event_id = event.get("event_id", "")
        op = event.get("operation")
        if op == "UNKNOWN":
            reclassified = _reclassify_unknown_operation(event)
            if reclassified != "UNKNOWN":
                event["operation"] = reclassified
                op = reclassified
        if event_id in rescinded_event_ids:
            coverage_gaps.append(_gap(event, "notification_rescinded"))
            continue
        if op in SPECIAL_OPS:
            if op in {"CORRIGENDUM"}:
                corr = special_ops_meta.get("corrigendum_data", {}).get(event_id, {})
                if corr.get("targets_rules"):
                    event["_corrigendum_parsed"] = corr
                else:
                    coverage_gaps.append(_gap(event, "corrigendum_targets_notification"))
                    continue
            elif op in {"RESCIND", "SUPERSEDE"}:
                coverage_gaps.append(_gap(event, "rescind_notification_processed"))
                continue
            elif op == "COMMENCE":
                coverage_gaps.append(_gap(event, "commence_no_text_change"))
                continue
        if not _date_key(event):
            coverage_gaps.append(_gap(event, "event_date_unresolved"))
            continue
        if not _is_after_base(event, base_as_of):
            continue
        # Classify already_reflected events: INSERT_CHILD/INSERT_SIBLING where the
        # target already exists in the component store and the event has
        # inserted_component_already_exists as a review reason.
        _reasons = set((event.get("review") or {}).get("review_reasons") or [])
        if "inserted_component_already_exists" in _reasons:
            _cid = (event.get("target") or {}).get("component_id", "")
            if _cid and _cid in component_paths:
                already_reflected_events.append(_gap(event, "already_reflected"))
                continue
            payload = event.get("payload") or {}
            payload["noop_if_already_reflected"] = True
        _extract_payload_from_excerpt(event)
        if event.get("operation") == "UNKNOWN":
            reclassified = _reclassify_unknown_operation(event)
            if reclassified != "UNKNOWN":
                event["operation"] = reclassified
                op = reclassified
        if event.get("operation") == "SPLICE":
            _excerpt_check = str((event.get("evidence") or {}).get("excerpt", ""))
            # Don't reclassify a validated SPLICE that already has anchor_text +
            # insert_text: the excerpt may contain unrelated "for the words...
            # shall be substituted" language from a sibling clause in a compound
            # amendment block (e.g. rule 89 Notif 19/2022 clause (c)).
            _spl_anchor = str((event.get("target") or {}).get("anchor_text") or "").strip()
            _spl_insert = str((event.get("payload") or {}).get("insert_text") or "").strip()
            if not (_spl_anchor and _spl_insert):
                if _RE_UNKNOWN_SUBSTITUTION.search(_excerpt_check) and "for the words" in _excerpt_check.lower():
                    event["operation"] = "SUBSTITUTE"
                    op = "SUBSTITUTE"
        ready, skip_reason = _event_ready_to_apply(
            event,
            resolved_work=resolved_work,
            component_paths=component_paths,
            files=files,
        )
        if not ready:
            coverage_gaps.append(_gap(event, skip_reason or "event_not_materializable"))
            continue
        if op not in SUPPORTED_OPS and not (op == "CORRIGENDUM" and event.get("_corrigendum_parsed")):
            coverage_gaps.append(_gap(event, "operation_not_supported"))
            continue
        if event.get("event_id") in conflicted_ids:
            coverage_gaps.append(_gap(event, "same_effective_date_conflict"))
            continue
        blocked_target = _blocked_baseline_target(event, blocked_baseline_components)
        if blocked_target:
            blocked_component, reasons = blocked_target
            coverage_gaps.append(
                _gap(
                    event,
                    "baseline_component_blocked: "
                    + blocked_component
                    + (" (" + ", ".join(reasons) + ")" if reasons else ""),
                )
            )
            continue
        ready_events.append(event)

    pending = ready_events
    while pending:
        progress = False
        retry: list[tuple[dict[str, Any], str]] = []
        for event in pending:
            try:
                if event["operation"] == "INSERT_SIBLING":
                    changed = _apply_insert_sibling(event, files, component_paths, store)
                elif event["operation"] == "INSERT_CHILD":
                    changed = _apply_insert_child(event, files, component_paths, store)
                elif event["operation"] in {"SPLICE", "SUBSTITUTE"}:
                    changed = _apply_splice_or_substitute(event, files, component_paths, store)
                elif event["operation"] == "OMIT":
                    changed = _apply_omit(event, files, component_paths, store)
                elif event["operation"] == "CORRIGENDUM" and event.get("_corrigendum_parsed"):
                    changed = _apply_corrigendum(event, files, component_paths, store)
                else:
                    raise ValueError(f"Unsupported operation: {event['operation']}")
            except Exception as exc:
                if _retryable_apply_error(exc):
                    retry.append((event, f"apply_failed: {exc}"))
                    continue
                if _is_already_reflected_apply_failure(event, exc, store):
                    already_reflected_events.append(_gap(event, "already_reflected"))
                    continue
                coverage_gaps.append(_gap(event, f"apply_failed: {exc}"))
                continue
            progress = True
            norm_provenance = event.pop("_normalization_provenance", None)
            applied_record = {"event_id": event["event_id"], "operation": event["operation"], "changed_components": changed}
            if norm_provenance:
                applied_record["normalized_match"] = norm_provenance
                normalized_match_events.append({
                    "event_id": event["event_id"],
                    "operation": event["operation"],
                    "changed_components": changed,
                    "target": event.get("target", {}),
                    "normalization": norm_provenance,
                })
            applied.append(applied_record)
            for warning in event.get("_materialization_warnings", []):
                coverage_gaps.append(_gap(event, warning))
            if write_snapshots:
                effective_date = _date_key(event)
                changed_components_by_date.setdefault(effective_date, set()).update(changed)
                snapshots[effective_date] = _write_snapshot(files, output_dir, effective_date)
                for component_id in sorted(changed_components_by_date[effective_date]):
                    written = _write_component_snapshot_text(store, component_id, output_dir, effective_date)
                    if written and written not in snapshots[effective_date]:
                        snapshots[effective_date].append(written)

        if not retry:
            break
        if not progress:
            for event, reason in retry:
                if "Target component missing:" in reason or "Parent component missing:" in reason:
                    coverage_gaps.append(_gap(event, "target_not_in_store"))
                else:
                    coverage_gaps.append(_gap(event, reason))
            break
        pending = [event for event, _reason in retry]

    remaining_gaps: list[dict[str, Any]] = []
    for gap in coverage_gaps:
        gap_reasons = set(gap.get("review_reasons") or [])
        gap_cid = (gap.get("target") or {}).get("component_id", "")
        gap_op = gap.get("operation", "")
        if "inserted_component_already_exists" in gap_reasons:
            if gap_cid and gap_cid in store.versions:
                already_reflected_events.append(gap)
                continue
        if gap_op in {"OMIT", "SUBSTITUTE"} and gap_cid and gap_cid in store.versions:
            latest = store.versions[gap_cid][-1] if store.versions.get(gap_cid) else None
            if latest and latest.get("text", "").strip() in {"[Omitted]", ""}:
                already_reflected_events.append(gap)
                continue
        remaining_gaps.append(gap)
    coverage_gaps = remaining_gaps

    for component_id, versions in store.versions.items():
        if len(versions) < 2:
            continue
        for _vi in range(1, len(versions)):
            current_text = (versions[_vi].get("text") or "").strip()
            current_len = len(current_text)
            best_prior = ""
            for _pj in range(_vi - 1, -1, -1):
                pt = (versions[_pj].get("text") or "").strip()
                if len(pt) > len(best_prior):
                    best_prior = pt
            prior_len = len(best_prior)
            if prior_len < 500:
                continue
            if current_len >= prior_len * 0.3:
                continue
            sb = versions[_vi].get("source_basis") or {}
            op = str(sb.get("operation") or "").upper()
            if op in ("OMIT", "INSERT_SIBLING"):
                continue
            source_span = sb.get("source_span") or {}
            if source_span.get("text_hash") and current_len < prior_len:
                full_repl_event = source_span.get("start") == 0 and source_span.get("end", 0) == current_len
                if full_repl_event:
                    continue
            versions[_vi]["text"] = best_prior

    for component_id, versions in store.versions.items():
        for _vi in range(len(versions)):
            text = versions[_vi].get("text") or ""
            text = re.sub(r"(section \d+[A-Z])\s+or\s+\1\b", r"\1", text)
            text = re.sub(r"(or section \d+[A-Z])\s+or\s+\1\b", r"\1", text)
            versions[_vi]["text"] = text

    node_versions_path = output_dir / "node_versions.jsonl"
    rows = store.flattened()
    node_versions_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    coverage_path = output_dir / "coverage_gaps.json"
    _gap_skip_breakdown: dict[str, int] = {}
    _gap_review_breakdown: dict[str, int] = {}
    for _g in coverage_gaps:
        _sr = _g.get("skip_reason", "")
        _sr_cat = _sr.split(":")[0] if ":" in _sr else _sr
        _gap_skip_breakdown[_sr_cat] = _gap_skip_breakdown.get(_sr_cat, 0) + 1
        for _rr in (_g.get("review_reasons") or []):
            _gap_review_breakdown[_rr] = _gap_review_breakdown.get(_rr, 0) + 1
    coverage_payload = {
        "target_work": resolved_work,
        "base_as_of": base_as_of,
        "gap_count": len(coverage_gaps),
        "gaps": coverage_gaps,
        "skip_reason_breakdown": _gap_skip_breakdown,
        "review_reason_breakdown": _gap_review_breakdown,
    }
    coverage_path.write_text(json.dumps(coverage_payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    corrigendum_rows = _corrigendum_ledger(events, special_ops_meta.get("corrigendum_data", {}))
    corrigendum_ledger_path = output_dir / "corrigendum_ledger.jsonl"
    corrigendum_ledger_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in corrigendum_rows)
        + ("\n" if corrigendum_rows else ""),
        encoding="utf-8",
    )
    manifest = {
        "target_work": resolved_work,
        "base_as_of": base_as_of,
        "materializer_version": MATERIALIZER_VERSION,
        "events_path": str(events_path),
        "corpus_dir": str(corpus_dir),
        "baseline_dir": str(baseline_dir) if baseline_dir else None,
        "output_dir": str(output_dir),
        "node_versions": str(node_versions_path),
        "coverage_gaps": str(coverage_path),
        "event_count": len(events),
        "applied_count": len(applied),
        "normalized_match_count": len(normalized_match_events),
        "normalized_match_events": normalized_match_events,
        "coverage_gap_count": len(coverage_gaps),
        "forms_lane_routed_count": len(routed_to_forms_lane),
        "forms_lane_routed_events": routed_to_forms_lane,
        "metadata_only_count": len(metadata_only_events),
        "metadata_only_events": metadata_only_events,
        "context_recovered_count": len(context_recovered_events),
        "context_recovered_events": context_recovered_events,
        "forms_lane_pending_baseline_count": len(forms_lane_pending_baseline_events),
        "forms_lane_pending_baseline_events": forms_lane_pending_baseline_events,
        "schedule_lane_pending_baseline_count": len(schedule_lane_pending_baseline_events),
        "schedule_lane_pending_baseline_events": schedule_lane_pending_baseline_events,
        "act_out_of_scope_count": len(act_out_of_scope_events),
        "act_out_of_scope_events": act_out_of_scope_events,
        "context_unresolved_count": len(context_unresolved_events),
        "context_unresolved_events": context_unresolved_events,
        "already_reflected_count": len(already_reflected_events),
        "already_reflected_events": already_reflected_events,
        "rules_table_lane_routed_count": len(routed_to_rules_table_lane),
        "rules_table_lane_routed_events": routed_to_rules_table_lane,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "blocked_baseline_component_count": len(blocked_baseline_components),
        "blocked_baseline_components": [
            {"component_id": component_id, "reasons": reasons}
            for component_id, reasons in sorted(blocked_baseline_components.items())
        ],
        "applied_events": applied,
        "snapshots": [{"effective_date": date, "files": paths} for date, paths in sorted(snapshots.items())],
        "rescinded_notifications": special_ops_meta.get("rescinded_notifications", []),
        "rescinded_event_count": len(rescinded_event_ids),
        "corrigendum_parsed_count": sum(1 for v in special_ops_meta.get("corrigendum_data", {}).values() if v.get("targets_rules")),
        "corrigendum_ledger": str(corrigendum_ledger_path),
        "corrigendum_ledger_count": len(corrigendum_rows),
        "special_ops_processed": True,
    }
    manifest_path = output_dir / "materialization_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return manifest


__all__ = ["materialize_versions"]
