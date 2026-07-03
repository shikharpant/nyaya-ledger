"""Post-hoc context recovery for reviewed amendment events."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RULES_WORK = "/in/union/rules/cgst-rules-2017"
RECOVERY_VERSION = "context-recovery-v1"

_RE_RULE_CONTEXT = re.compile(
    r"\b(?:in\s+(?:the\s+said\s+rules,\s*)?)?in\s+rule\s+([0-9]+[A-Z]?)\b|\brule\s+([0-9]+[A-Z]?)\b",
    re.IGNORECASE,
)
_RE_SUBRULE_CONTEXT = re.compile(
    r"\bin\s+sub-?\s*rule\s*\(?([0-9A-Za-z]+)\)?\s+of\s+rule\s+([0-9]+[A-Z]?)\b",
    re.IGNORECASE,
)
_RE_RULE_THEN_SUBRULE_CONTEXT = re.compile(
    r"\bin\s+rule\s+([0-9]+[A-Z]?)\b(?:\s+of\s+the\s+said\s+rules?)?[\s,–-]*"
    r"\bin\s+sub-?\s*rule\s*\(?([0-9A-Za-z]+)\)?\b",
    re.IGNORECASE,
)
_RE_CHILD_SUBRULE = re.compile(r"\bin\s+sub-?\s*rule\s*\(?([0-9A-Za-z]+)\)?\b", re.IGNORECASE)
_RE_FORM = re.compile(r"\bFORM\s*-?\s*(?:GST\s*-?\s*)?[A-Z0-9]+(?:\s*-\s*[A-Z0-9]+)*\b", re.IGNORECASE)
_RE_FORM_BODY_MARKERS = re.compile(
    r"\bGSTIN\b|\bInvoice\s+details\b|\bTaxable\s+value\b|\bIntegrated\s+Tax\b|"
    r"\bCentral\s+Tax\b|\bState\s*/?\s*UT\s+Tax\b|\bAmount\s+in\s+Rs\.?\b|"
    r"\bConsolidated\s+Statement\b|\bDECLARATION\b|\bDetails\s+of\s+supplies\b|"
    r"\bGross\s+Advance\b|\bPlace\s+of\s+supply\b|\bGSTR\s*-?\s*\d+[A-Z]?\b|"
    r"\bauto-?\s*populated\b|\binput\s+tax\s+credit\s+reported\s+in\s+Table\b|"
    r"\bNet\s+value\s+columns\b|\bgross\s+value\b|\bTax\s+period\b|\bOrder\s+No\.?\b|<<\s*Auto|"
    r"\bbased\s+on\s+Table\b|\bTable\s+No\.?\b|\bColumn\s+nos?\.?\b|\bAmount\s+in\s*[₹Rr]s?\.?\b|"
    r"\bBrief\s+issue\b|\bCategory\s+of\s+case\b|\bMarket\s+value\b|\bNet\s+of\s+advances\b|"
    r"\bTCS\s+liability\b|\btax\s+liability\b|\bQuarterly\s+return\b|\bdefault\s+option\b|"
    r"\bRate\s+of\s+tax\b|\bdebit\s*/?\s*credit\s+notes?\b|\bbank\s+guarantee\b|"
    r"\bcase\s+under\s+dispute\b|\bturnover\b|\be-?\s*commerce\b|\bTDS\b|"
    r"\bfinal\s+return\b|\bcancellation\s+order\b|\bself-?\s*assessed\s+liability\b|"
    r"\bdemand\s+created\b|\bPart\s+[IVXLCDM]+\b|\bAll\s+Tables\b|\breverse\s+charge\b|"
    r"\bzero\s+rated\b|\bB2C\b|\bInput\s+tax\s+credit\b|\bCenvat\s+credit\b|\bISD\b|"
    r"\brecipient\s+tax\s+payer\b|\bTax\s+paid\b|\bBalance\s+amount\b|\blate\s+fee\b|"
    r"\bCredit\s+Transfer\s+Document\b|\bCTD\b|\bchallan\b|\bliabilities\b|\bITC\b|"
    r"\bEligible\s+ITC\b|\bIneligible\s+ITC\b|\be-?\s*way\s+bill\b|\bOver\s+Dimensional\s+Cargo\b|"
    r"\belectronic\s+credit\s+ledger\b|\bRegistration\s+no\.?\b|\bTax\s+period\s+to\s+which\b|"
    r"\border\s+mentioned\s+in\s+Table\b|\bReference\s+number\s+of\s+appeal\b|\bundertake\b|"
    r"\bDemand\s+proceedings\b|\bAppellate\s*/?\s*Revisionary\s+order\b|\bMention\s+section\b|"
    r"\bRecipient\s+of\s+deemed\s+export\b|\bserial\s+no\.?\b|\bserial\s+number\b|"
    r"\border\s+issued\s+under\s+section\s+129\b|\brequires\s+rectification\b|"
    r"\bReason\s+for\s+rectification\b|\bamount\s+remains\s+unpaid\b|"
    r"\brecoverable\s+in\s+accordance\s+with\s+the\s+provisions\s+of\s+section\s+79\b|"
    r"\bliable\s+to\s+be\s+demanded\s+in\s+accordance\s+with\s+the\s+provisions\s+of\s+section\s+73\b|"
    r"\bfilled\s+in\s+by\s+the\s+applicant\b|\border\s+against\s+which\s+the\s+application\b|"
    r"\bappeal\s+filed\s+originally\s+but\s+subsequently\s+withdrawn\b|"
    r"\bUndertaking\s+submitted\s+in\s+respect\s+of\s+Rule\s+164\b|"
    r"\bI\s+hereby\s+undertake\s+not\s+to\s+file\s+an\s+appeal\b",
    re.IGNORECASE,
)
_RE_FORM_FIELD_MARKERS = re.compile(
    r"<<\s*Auto|\bauto\s+filled\b|\bdropdown\b|\bAnnexure\s+[A-Z]\b",
    re.IGNORECASE,
)
_RE_FORM_BODY_HEADER = re.compile(
    r"(?:\b(?:Table\s+(?:\d+(?:\.\d+)?[A-Z]?|\([A-Z]\))|Statement\s*-?\s*\d+[A-Z]?|Declaration\s*-|Column\s+nos?\.?|"
    r"Summary\s+of|Amount\s+in\s*[₹Rr]s?\.?|Details\s+(?:relating\s+to|of)|"
    r"Net\s+amount|Tax\s+liability|Instructions?\s+to\s+fill|Part\s+[IVXLCDM]+\s+consists?|"
    r"Information\s+of|Distribution\s+of|Amendments\s+to|Notice\s+to|under\s+the\s+heading\s+Instructions|"
    r"Demand\s+(?:table|Notice|Order|paid)|Total\s+ITC|Table\s+No\.?|"
    r"Amount\s+of\s+tax\s+credit\s+carried\s+forward|Registered\s+persons\s+having\s+aggregate\s+turnover|"
    r"Whether\s+any\s+particular\s+thing|Consolidated\s+Statement|Verification|Part\s+[A-Z]|Subject:))",
    re.IGNORECASE,
)
_RE_FORM_NOTICE_FRAGMENT = re.compile(
    r"\b(?:requires\s+rectification\s+\(Reason\s+for\s+rectification|"
    r"amount\s+remains\s+unpaid\b.*\brecoverable\s+in\s+accordance\s+with\s+the\s+provisions\s+of\s+section\s+79|"
    r"excess\s+input\s+tax\s+credit\s+remains\s+to\s+be\s+paid\b.*\bliable\s+to\s+be\s+demanded|"
    r"order\s+against\s+which\s+the\s+application\b.*\bfilled\s+in\s+by\s+the\s+applicant|"
    r"Reference\s+number\s+of\s+appeal\s+filed\s+originally\s+but\s+subsequently\s+withdrawn|"
    r"Undertaking\s+submitted\s+in\s+respect\s+of\s+Rule\s+164|"
    r"I\s+hereby\s+undertake\s+not\s+to\s+file\s+an\s+appeal)\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_RULE_TABLE_BODY = re.compile(
    r"\b(?:for\s+(?:the\s+)?Table,\s+the\s+following\s+Table\s+shall\s+be\s+substituted|"
    r"(?:for|in|after)\s+(?:the\s+)?Table\b.*?\bshall\s+be\s+(?:substituted|inserted|omitted)\b|"
    r"in\s+column\s*\([^)]*\)\s+of\s+the\s+table\b.*\bshall\s+be\s+(?:substituted|inserted|omitted)\b|"
    r"Table\s+below\s*[:\-–—]*\s*TABLE\b|S\.?\s*No\.?\s+Offence\s+Compounding\s+amount|"
    r"Offence\s+specified\s+in\s+clause\s+\([a-z]\)\s+of\s+sub-section\s+\(1\)\s+of\s+section\s+132|"
    r"e-?\s*way\s+bill\b|Over\s+Dimensional\s+Cargo\b)",
    re.IGNORECASE | re.DOTALL,
)
_Q_OPEN = r"[\"“‘‗―]"
_Q_CLOSE = r"[\"”’‖]"
_RE_SIMPLE_SUBSTITUTION = re.compile(
    rf"\bfor\s+(?:the\s+)?(?:word|words|figure|figures|letter|letters|brackets|"
    rf"words\s+and\s+figures|letters,\s+words\s+and\s+figures)[^\"“‘‗―]{{0,80}}"
    rf"{_Q_OPEN}\s*(?P<old>.+?)\s*{_Q_CLOSE}\s*,?\s*"
    rf"(?:occurring\s+at\s+both\s+the\s+places,\s*)?"
    rf"(?:the\s+)?(?:word|words|figure|figures|letter|letters|brackets|"
    rf"words\s+and\s+figures|letters,\s+words\s+and\s+figures)[^\"“‘‗―]{{0,80}}"
    rf"{_Q_OPEN}\s*(?P<new>.+?)\s*{_Q_CLOSE}\s+shall\s+be\s+substituted\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_SIMPLE_SPLICE = re.compile(
    rf"\bafter\s+(?:the\s+)?(?:word|words|figure|figures|letter|letters|brackets|"
    rf"words\s+and\s+figures)[^\"“‘‗―]{{0,80}}"
    rf"{_Q_OPEN}\s*(?P<anchor>.+?)\s*{_Q_CLOSE}\s*,?\s*"
    rf"(?:the\s+)?(?:word|words|figure|figures|letter|letters|brackets|"
    rf"words\s+and\s+figures)[^\"“‘‗―]{{0,80}}"
    rf"{_Q_OPEN}\s*(?P<insert>.+?)\s*{_Q_CLOSE}\s+shall\s+be\s+inserted\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_SIMPLE_OMISSION = re.compile(
    rf"\b(?:the\s+)?(?:word|words|figure|figures|letter|letters|brackets|"
    rf"words\s+and\s+figures)[^\"“‘‗―]{{0,80}}"
    rf"{_Q_OPEN}\s*(?P<omit>.+?)\s*{_Q_CLOSE}\s+shall\s+be\s+omitted\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_FOLLOWING_CHILD_INSERT = re.compile(
    rf"\b(?:after\s+(?P<anchor_kind>clause|sub-?\s*rule|proviso|Explanation)\s*\(?"
    rf"(?P<anchor_label>[0-9A-Za-z]+)?\)?[^.;]{{0,120}},\s*)?"
    rf"(?:the\s+)?following\s+(?P<kind>clause|sub-?\s*rule|proviso|Explanation)\s+"
    rf"shall\s+be\s+inserted,\s+namely\s*[,:\-–—]+\s*{_Q_OPEN}\s*(?P<content>.+?)\s*{_Q_CLOSE}",
    re.IGNORECASE | re.DOTALL,
)
_RE_RULE_BODY_SOURCE = re.compile(r"^\s*\d+[A-Z]?\.\s+[^.\n]{3,160}\.-\s*", re.IGNORECASE)
_RE_CONTEXT_HEADER_ONLY = re.compile(
    r"^\s*\(?[a-zivxlcdm]+\)?\s+(?:(?:with\s+effect\s+from\s+.+?,\s*)?in\s+rule\s+\d+[A-Z]?|"
    r"in\s+rule\s+\d+[A-Z]?\s*,?\s*with\s+effect\s+from\s+.+?)\s*[\s,–\-;]*\s*$",
    re.IGNORECASE,
)
_RE_CONTEXT_HEADER_FRAGMENT = re.compile(
    r"(?:^|;\s*)\(?\d+\)?\s+In\s+rule\s+\d+[A-Z]?\s+of\s+the\s+said\s+rules,\s+"
    r"in\s+sub-?\s*rule\s*\([^)]+\).*[-–—]\s*$",
    re.IGNORECASE | re.DOTALL,
)
_RE_NON_RULE_SOURCE_FRAGMENT = re.compile(
    r"\b(?:Schedule\s+II\s+of\s+the\s+Act|paragraph\s+\d+\s+of\s+Schedule\s+II)\b",
    re.IGNORECASE,
)
_RE_CHAPTER_SOURCE_FRAGMENT = re.compile(
    r"\bafter\s+rule\s+\d+[A-Z]?\b.*\bChapter\s*[-–—]?\s*[IVXLCDM]+\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_METADATA_ONLY = re.compile(
    r"\b(?:title|short\s+title|commencement|notification\s+shall\s+come\s+into\s+force|"
    r"shall\s+be\s+deemed\s+to\s+have\s+come\s+into\s+force)\b",
    re.IGNORECASE,
)
_RE_MALFORMED_RULE_ID = re.compile(r"^CGST_Rule_([0-9]+[A-Za-z]?)$", re.IGNORECASE)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _source_archive_dir(event: dict[str, Any], source_archive_root: Path) -> Path | None:
    document_id = str((event.get("source") or {}).get("document_id") or "")
    parts = [part for part in document_id.strip("/").split("/") if part]
    try:
        idx = parts.index("cbic")
    except ValueError:
        return None
    suffix = parts[idx + 1 :]
    if len(suffix) < 3:
        return None
    leaf = suffix[-1]
    match = re.match(r"(\d+)-(\d{4})", leaf)
    if match:
        suffix[-1] = f"{int(match.group(1))}-{match.group(2)}"
    return source_archive_root.joinpath("cbic", *suffix)


def _archive_text(event: dict[str, Any], source_archive_root: Path) -> str:
    archive_dir = _source_archive_dir(event, source_archive_root)
    if not archive_dir:
        return ""
    extracted_path = archive_dir / "extracted_text.json"
    if not extracted_path.exists():
        return ""
    try:
        payload = json.loads(extracted_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("text") or "")


def _pdf_fallback_text(event: dict[str, Any], notifications_dir: Path) -> str:
    record_id = str((event.get("source") or {}).get("record_id") or "")
    if not record_id or not notifications_dir.exists():
        return ""
    candidates = sorted(path for path in notifications_dir.rglob("*.json") if record_id in path.name)
    for path in candidates:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pdf_b64 = str(record.get("contentPdfBase64") or "")
        if not pdf_b64:
            continue
        try:
            import fitz

            doc = fitz.open(stream=base64.b64decode(pdf_b64), filetype="pdf")
            text = "".join(page.get_text() for page in doc)
            doc.close()
            if text.strip():
                return text
        except Exception:
            continue
    return ""


def notification_text(
    event: dict[str, Any],
    *,
    source_archive_root: Path,
    notifications_dir: Path,
) -> tuple[str, str]:
    text = _archive_text(event, source_archive_root)
    if text:
        return text, "source_archive"
    text = _pdf_fallback_text(event, notifications_dir)
    if text:
        return text, "contentPdfBase64"
    return "", "missing"


def _rule_component(rule_label: str) -> str:
    return f"{RULES_WORK}/rule/{rule_label.lower()}"


def _subrule_component(rule_label: str, subrule_label: str) -> str:
    return f"{_rule_component(rule_label)}/subrule/{subrule_label.lower()}"


def _component_label(value: str) -> str:
    paren = re.search(r"\(([0-9A-Za-z]+)\)", value)
    if paren:
        return paren.group(1).lower()
    return re.sub(r"[^0-9A-Za-z]+", "", value).lower()


def _component_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", value.lower()).strip("-")
    return slug or _component_label(value)


def _source_excerpt(event: dict[str, Any]) -> str:
    return str((event.get("evidence") or {}).get("excerpt") or "")


def _looks_like_inserted_rule(event: dict[str, Any], label: str) -> bool:
    payload = event.get("payload") or {}
    evidence = _source_excerpt(event)
    source_text = str(payload.get("source_text") or "")
    if re.search(r"\bfollowing\s+rule\s+shall\s+be\s+inserted\b", evidence, flags=re.IGNORECASE):
        return True
    label_re = re.escape(label).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?:^|[“\"'\s])(?:Rule\s+)?{label_re}\s*[.\-–]", source_text, flags=re.IGNORECASE))


def _composite_child_from_label(label: str) -> tuple[str, str, str] | None:
    parts = [part for part in re.split(r"/+", label.strip()) if part]
    if len(parts) < 2 or not re.match(r"^\d+[A-Za-z]?$", parts[0]):
        return None
    kind = _structural_child_kind(parts[1], parts[1])
    raw_child = parts[2] if len(parts) > 2 else parts[1]
    if kind == "subrule":
        suffix = _component_label(raw_child)
    else:
        suffix = _component_slug(raw_child)
    if not suffix:
        return None
    return _rule_component(parts[0]), kind, suffix


def _inserted_child_label_from_event(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    content = str(payload.get("content") or payload.get("insert_text") or "").strip()
    prefix = r"[“\"'‘’―]?[-–—―]?\s*[“\"'‘’―]?"
    match = re.match(prefix + r"\(?([0-9A-Za-z]+)\)", content)
    if match:
        return match.group(1)
    excerpt = _source_excerpt(event)
    namely_match = re.search(
        r"namely\s*[:-]\s*" + prefix + r"\(?([0-9A-Za-z]+)\)",
        excerpt,
        flags=re.IGNORECASE,
    )
    if namely_match:
        return namely_match.group(1)
    if str(payload.get("node_type") or "").lower() == "proviso" or content.lower().startswith("provided"):
        return "provided"
    return ""


def _inserted_child_label_from_text(content: str) -> str:
    prefix = r"[“\"'‘’―]?[-–—―]?\s*[“\"'‘’―]?"
    match = re.match(prefix + r"\(?([0-9A-Za-z]+)\)", content.strip())
    if match:
        return match.group(1)
    if content.strip().lower().startswith("provided"):
        return "provided"
    if content.strip().lower().startswith("explanation"):
        return "Explanation"
    return ""


def _structural_child_kind(node_type: str, label: str) -> str:
    lowered = node_type.lower().replace("_", "-")
    if lowered in {"sub-rule", "subrule"} or re.search(r"\bsub-?\s*rule\b", label, flags=re.IGNORECASE):
        return "subrule"
    if lowered == "proviso" or "proviso" in label.lower() or label.lower().startswith("provided"):
        return "proviso"
    if lowered == "explanation" or "explanation" in label.lower():
        return "explanation"
    if lowered == "clause" or re.match(r"^\(?[a-z]+\)?$", label.strip(), flags=re.IGNORECASE):
        return "clause"
    return lowered or "subrule"


def _structural_target_component(event: dict[str, Any], context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Derive the inserted component target without losing recovered context."""

    operation = str(event.get("operation") or "")
    payload = event.get("payload") or {}
    context_component = str(context.get("component_id") or "")
    detail: dict[str, Any] = {}
    label = str(payload.get("label") or "").strip()
    composite = _composite_child_from_label(label)
    if operation == "INSERT_SIBLING" and composite:
        parent, kind, suffix = composite
        event["operation"] = "INSERT_CHILD"
        payload["node_type"] = kind
        payload["parent_component_id"] = parent
        payload["label"] = suffix
        detail["structural_target_from"] = f"insert_sibling_composite_{kind}_label"
        detail["parent_component_id"] = parent
        detail["child_kind"] = kind
        return f"{parent}/{kind}/{suffix}", detail
    if (
        operation == "INSERT_SIBLING"
        and str(payload.get("node_type") or "").lower() == "rule"
        and re.match(r"^\d+[A-Za-z]?$", label)
        and _looks_like_inserted_rule(event, label)
    ):
        component_id = _rule_component(_component_label(label))
        detail["structural_target_from"] = "insert_sibling_rule_label"
        return component_id, detail
    if operation == "INSERT_SIBLING" and str(payload.get("node_type") or "").lower() == "rule":
        detail["requires_validation"] = "context_only_insert_sibling_rule"
        return context_component, detail
    if operation == "INSERT_SIBLING" and str(payload.get("node_type") or "").lower() in {
        "clause",
        "proviso",
        "explanation",
        "sub-rule",
        "subrule",
    }:
        event["operation"] = "INSERT_CHILD"
        payload["parent_component_id"] = context_component
        detail["structural_target_from"] = "insert_sibling_structural_child"
        detail["parent_component_id"] = context_component
        detail["child_kind"] = _structural_child_kind(str(payload.get("node_type") or ""), label)
        if label:
            suffix = (
                _component_label(label)
                if detail["child_kind"] == "subrule"
                else _component_slug(label)
            )
            if suffix:
                return f"{context_component}/{detail['child_kind']}/{suffix}", detail
        return context_component, detail
    if operation != "INSERT_CHILD":
        return context_component, detail

    content_label = _inserted_child_label_from_event(event)
    if content_label and (
        not label
        or (
            _structural_child_kind(str(payload.get("node_type") or ""), label) == "subrule"
            and _component_label(content_label) != _component_label(label)
        )
    ):
        label = content_label
        payload["label"] = label

    parent = str(payload.get("parent_component_id") or "").strip()
    if not (parent == RULES_WORK or parent.startswith(RULES_WORK + "/")):
        parent = context_component
    if not parent or parent == RULES_WORK:
        parent = context_component
    if (
        context_component.startswith(parent + "/subrule/")
        and _structural_child_kind(str(payload.get("node_type") or ""), label) in {"clause", "proviso", "explanation"}
    ):
        parent = context_component
    if not parent or parent == RULES_WORK:
        return context_component, detail

    kind = _structural_child_kind(str(payload.get("node_type") or ""), label)
    if kind == "subrule":
        suffix = _component_label(label)
    else:
        suffix = _component_slug(label)
    if not suffix:
        return context_component, detail
    detail["structural_target_from"] = f"insert_child_{kind}_label"
    detail["parent_component_id"] = parent
    detail["child_kind"] = kind
    return f"{parent}/{kind}/{suffix}", detail


def _parse_effective_date_from_context(text: str, context: dict[str, Any], start: int) -> str | None:
    if not text or start < 0:
        return None
    match_offset = context.get("match_offset")
    try:
        context_start = int(match_offset)
    except (TypeError, ValueError):
        context_start = max(0, start - 500)
    window = text[max(0, context_start) : min(len(text), start + 500)]
    matches = list(
        re.finditer(
            r"\bwith\s+effect\s+from\s+(?:the\s+)?(.+?)(?=,?\s*[-–;.]|\s+\([ivxlcdm]+\)|\s+for\s+the\s+words|\s+after\s+the\b)",
            window,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if not matches:
        return None
    value = matches[-1].group(1)
    value = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE)
    value = re.sub(r"\bday\s+of\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" ,-")
    try:
        from dateutil import parser as date_parser

        return date_parser.parse(value, dayfirst=True, fuzzy=True).date().isoformat()
    except Exception:
        return None


def _recover_context_from_excerpt(excerpt: str) -> dict[str, Any] | None:
    if not excerpt:
        return None
    rule_then_subrule = _RE_RULE_THEN_SUBRULE_CONTEXT.search(excerpt)
    if rule_then_subrule:
        best = {
            "rule": rule_then_subrule.group(1).lower(),
            "subrule": rule_then_subrule.group(2).lower(),
            "matched_text": rule_then_subrule.group(0),
            "match_offset": rule_then_subrule.start(),
            "matched_in_excerpt": True,
        }
        best["component_id"] = _subrule_component(best["rule"], best["subrule"])
        return best
    subrule_matches = list(_RE_SUBRULE_CONTEXT.finditer(excerpt))
    rule_matches = list(_RE_RULE_CONTEXT.finditer(excerpt))
    instruction_prefix = re.split(r"\bshall\s+be\b|\bnamely\b|[“\"‘‗―]", excerpt, maxsplit=1, flags=re.IGNORECASE)[0]
    prefix_rule_matches = list(_RE_RULE_CONTEXT.finditer(instruction_prefix))
    best: dict[str, Any] | None = None
    if subrule_matches:
        match = subrule_matches[-1]
        best = {
            "rule": match.group(2).lower(),
            "subrule": match.group(1).lower(),
            "matched_text": match.group(0),
            "match_offset": match.start(),
            "matched_in_excerpt": True,
        }
    elif prefix_rule_matches:
        match = prefix_rule_matches[-1]
        best = {
            "rule": (match.group(1) or match.group(2)).lower(),
            "subrule": None,
            "matched_text": match.group(0),
            "match_offset": match.start(),
            "matched_in_excerpt": True,
            "matched_instruction_prefix": True,
        }
    elif rule_matches:
        match = rule_matches[-1]
        best = {
            "rule": (match.group(1) or match.group(2)).lower(),
            "subrule": None,
            "matched_text": match.group(0),
            "match_offset": match.start(),
            "matched_in_excerpt": True,
        }
    if not best:
        return None
    child_match = _RE_CHILD_SUBRULE.search(excerpt)
    if child_match:
        best["subrule"] = child_match.group(1).lower()
        best["subrule_promoted_from_excerpt"] = True
    best["component_id"] = (
        _subrule_component(best["rule"], best["subrule"]) if best.get("subrule") else _rule_component(best["rule"])
    )
    return best


def recover_context_from_text(text: str, start: int, excerpt: str = "") -> dict[str, Any] | None:
    """Recover nearest rule/subrule context before a source-span offset."""
    excerpt_context = _recover_context_from_excerpt(excerpt)
    if excerpt_context:
        return excerpt_context
    if not text or start < 0:
        return None
    prefix = text[: min(start, len(text))]
    window = prefix[-5000:]
    rule_then_subrule_matches = list(_RE_RULE_THEN_SUBRULE_CONTEXT.finditer(window))
    subrule_matches = list(_RE_SUBRULE_CONTEXT.finditer(window))
    rule_matches = list(_RE_RULE_CONTEXT.finditer(window))
    best: dict[str, Any] | None = None
    if rule_then_subrule_matches:
        match = rule_then_subrule_matches[-1]
        best = {
            "rule": match.group(1).lower(),
            "subrule": match.group(2).lower(),
            "matched_text": match.group(0),
            "match_offset": len(prefix) - len(window) + match.start(),
        }
    elif subrule_matches:
        match = subrule_matches[-1]
        best = {
            "rule": match.group(2).lower(),
            "subrule": match.group(1).lower(),
            "matched_text": match.group(0),
            "match_offset": len(prefix) - len(window) + match.start(),
        }
    elif rule_matches:
        match = rule_matches[-1]
        best = {
            "rule": (match.group(1) or match.group(2)).lower(),
            "subrule": None,
            "matched_text": match.group(0),
            "match_offset": len(prefix) - len(window) + match.start(),
        }
    if not best:
        return None
    child_match = _RE_CHILD_SUBRULE.search(excerpt)
    if child_match:
        best["subrule"] = child_match.group(1).lower()
        best["subrule_promoted_from_excerpt"] = True
    best["component_id"] = (
        _subrule_component(best["rule"], best["subrule"]) if best.get("subrule") else _rule_component(best["rule"])
    )
    return best


def _event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    evidence = event.get("evidence") or {}
    parts = [
        str(evidence.get("excerpt") or ""),
        str(payload.get("old_text") or ""),
        str(payload.get("new_text") or ""),
        str(payload.get("anchor_text") or ""),
        str(payload.get("insert_text") or ""),
        str(payload.get("text") or ""),
    ]
    return "\n".join(part for part in parts if part)


def _event_search_text(event: dict[str, Any]) -> str:
    target = event.get("target") or {}
    parts = [
        _event_text(event),
        str(target.get("component_id") or ""),
    ]
    return "\n".join(part for part in parts if part)


def triage_event(event: dict[str, Any]) -> str:
    text = _event_text(event)
    search_text = _event_search_text(event)
    target = event.get("target") or {}
    component_id = str(target.get("component_id") or "")
    if _RE_MALFORMED_RULE_ID.match(component_id):
        return "canonical_id_normalized"
    if _RE_RULE_TABLE_BODY.search(text) and "/rule/" in component_id:
        return "rules_table_lane"
    if (
        _RE_FORM.search(search_text)
        or _RE_FORM_FIELD_MARKERS.search(text)
        or _RE_FORM_NOTICE_FRAGMENT.search(text)
        or (_RE_FORM_BODY_HEADER.search(text) and _RE_FORM_BODY_MARKERS.search(text))
    ):
        return "forms_lane_pending_baseline"
    if (
        str(event.get("operation") or "") == "UNKNOWN"
        and (
            _RE_RULE_BODY_SOURCE.search(text)
            or _RE_CONTEXT_HEADER_ONLY.search(text)
            or _RE_CONTEXT_HEADER_FRAGMENT.search(text)
            or _RE_NON_RULE_SOURCE_FRAGMENT.search(text)
        )
    ) or _RE_CHAPTER_SOURCE_FRAGMENT.search(text):
        return "baseline_source_only"
    span = (event.get("evidence") or {}).get("source_span") or {}
    zero_span = "start" in span and "end" in span and span.get("start") == span.get("end")
    if zero_span or (_RE_METADATA_ONLY.search(text) and not any((event.get("payload") or {}).get(k) for k in ("old_text", "new_text", "insert_text"))):
        return "metadata_only"
    return "amendment_language"


def _normalize_malformed_component(component_id: str) -> str:
    match = _RE_MALFORMED_RULE_ID.match(component_id)
    if not match:
        return component_id
    return _rule_component(match.group(1))


def _mark_lane(event: dict[str, Any], lane: str, reason: str) -> None:
    payload = event.setdefault("payload", {})
    payload["triage_lane"] = lane
    if lane == "metadata_only":
        payload["metadata_only"] = True
    if lane == "forms_lane_pending_baseline":
        payload["forms_lane_pending_baseline"] = True
    if lane == "baseline_source_only":
        payload["baseline_source_only"] = True
    validation = event.setdefault("validation", {})
    validation["materializable"] = False
    event["status"] = "rejected" if lane == "baseline_source_only" else "needs_review"
    review = event.setdefault("review", {})
    reasons = list(review.get("review_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    review["review_reasons"] = reasons
    review["required"] = lane != "baseline_source_only"


def _candidate_requires_llm(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    if event.get("operation") in {"SPLICE", "SUBSTITUTE"}:
        fields = ["old_text", "new_text", "anchor_text", "insert_text", "structural_text"]
        return not any(str(payload.get(field) or "").strip() for field in fields)
    if event.get("operation") in {"INSERT_CHILD", "INSERT_SIBLING"}:
        return not any(str(payload.get(field) or "").strip() for field in ("content", "heading", "label", "text"))
    return False


def _single_instruction_excerpt(text: str) -> bool:
    lowered = text.lower()
    return lowered.count("shall be") == 1 and not re.search(
        r";\s*\(?[a-zivxlcdm]+\)|\(\s*[A-Z]\s*\).*;\s*\(\s*[A-Z]\s*\)",
        text,
        flags=re.IGNORECASE,
    )


def _clean_extracted_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ;,")


def _remove_review_reasons(event: dict[str, Any], resolved: set[str]) -> None:
    review = event.setdefault("review", {})
    review["review_reasons"] = [r for r in review.get("review_reasons", []) if r not in resolved]


def _event_describes_child_insert(text: str) -> bool:
    return bool(
        re.search(
            r"\bfollowing\s+(?:clause|sub-?\s*rule|proviso|Explanation)\s+shall\s+be\s+inserted\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _undo_stale_source_window_insert(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    deterministic = payload.get("deterministic_reextraction") or {}
    if deterministic.get("strategy") != "following_child_insert_from_source_window":
        return False
    if _event_describes_child_insert(_event_text(event)):
        return False
    context_recovery = payload.get("context_recovery") or {}
    parent_component_id = str(payload.get("parent_component_id") or context_recovery.get("parent_component_id") or "")
    if not parent_component_id:
        parent_component_id = str((event.get("target") or {}).get("component_id") or "")
        for marker in ("/clause/", "/proviso/", "/explanation/", "/subrule/"):
            if marker in parent_component_id:
                parent_component_id = parent_component_id.split(marker, 1)[0]
                break
    event["operation"] = "UNKNOWN"
    if parent_component_id:
        event.setdefault("target", {})["component_id"] = parent_component_id
    preserved_payload = {
        key: value
        for key, value in payload.items()
        if key in {"context_recovered_target", "context_recovery", "corrigendum_applications", "triage_lane"}
    }
    event["payload"] = preserved_payload
    review = event.setdefault("review", {})
    reasons = list(review.get("review_reasons") or [])
    for reason in ("llm_candidate_not_validated", "unsupported_materializer_operation"):
        if reason not in reasons:
            reasons.append(reason)
    review["review_reasons"] = reasons
    event.setdefault("validation", {})["materializable"] = False
    return True


def _deterministic_reextract_payload(event: dict[str, Any], source_window: str = "") -> dict[str, Any] | None:
    text = _event_text(event)
    simple_text_edit_candidate = bool(text and _single_instruction_excerpt(text))
    if not text and not source_window:
        return None
    payload = event.setdefault("payload", {})

    substitution = _RE_SIMPLE_SUBSTITUTION.search(text) if simple_text_edit_candidate else None
    if substitution:
        old_text = _clean_extracted_text(substitution.group("old"))
        new_text = _clean_extracted_text(substitution.group("new"))
        if not old_text or not new_text or old_text == new_text:
            return None
        event["operation"] = "SUBSTITUTE"
        payload["old_text"] = old_text
        payload["new_text"] = new_text
        payload["deterministic_reextraction"] = {
            "strategy": "simple_substitution_from_excerpt",
            "requires_materializer_validation": True,
        }
        _remove_review_reasons(
            event,
            {
                "incomplete_text_edit_payload",
                "unsupported_materializer_operation",
                "llm_candidate_not_validated",
            },
        )
        return {"operation": "SUBSTITUTE", "old_text": old_text, "new_text": new_text}

    splice = _RE_SIMPLE_SPLICE.search(text) if simple_text_edit_candidate else None
    if splice:
        anchor = _clean_extracted_text(splice.group("anchor"))
        insert_text = _clean_extracted_text(splice.group("insert"))
        if not anchor or not insert_text:
            return None
        event["operation"] = "SPLICE"
        event.setdefault("target", {})["anchor_text"] = anchor
        payload["insert_text"] = insert_text
        payload["position"] = "after"
        payload["deterministic_reextraction"] = {
            "strategy": "simple_splice_from_excerpt",
            "requires_materializer_validation": True,
        }
        _remove_review_reasons(
            event,
            {
                "anchor_not_resolved",
                "incomplete_text_edit_payload",
                "unsupported_materializer_operation",
                "llm_candidate_not_validated",
            },
        )
        return {"operation": "SPLICE", "anchor_text": anchor, "insert_text": insert_text}

    omission = _RE_SIMPLE_OMISSION.search(text) if simple_text_edit_candidate else None
    if omission:
        omit_text = _clean_extracted_text(omission.group("omit"))
        if not omit_text:
            return None
        event["operation"] = "OMIT"
        payload["omit_text"] = omit_text
        payload["deterministic_reextraction"] = {
            "strategy": "simple_omission_from_excerpt",
            "requires_materializer_validation": True,
        }
        _remove_review_reasons(
            event,
            {
                "incomplete_text_edit_payload",
                "omit_instruction_ambiguous",
                "partial_omit_requires_precise_delete_payload",
                "unsupported_materializer_operation",
                "llm_candidate_not_validated",
            },
        )
        return {"operation": "OMIT", "omit_text": omit_text}

    event_describes_child_insert = _event_describes_child_insert(text)
    if source_window and event.get("operation") == "UNKNOWN" and event_describes_child_insert:
        child_insert = _RE_FOLLOWING_CHILD_INSERT.search(source_window)
        if child_insert and source_window[child_insert.start() : child_insert.start("content")].lower().count("shall be") == 1:
            content = _clean_extracted_text(child_insert.group("content"))
            content_label = _inserted_child_label_from_text(content)
            if not content or not content_label:
                return None
            kind = _structural_child_kind(child_insert.group("kind"), content_label)
            event["operation"] = "INSERT_CHILD"
            payload["content"] = content
            payload["label"] = content_label
            payload["node_type"] = kind
            payload["position"] = "after" if child_insert.group("anchor_kind") else "append"
            if child_insert.group("anchor_label"):
                anchor_kind = _structural_child_kind(child_insert.group("anchor_kind"), child_insert.group("anchor_label"))
                anchor_suffix = (
                    _component_label(child_insert.group("anchor_label"))
                    if anchor_kind == "subrule"
                    else _component_slug(child_insert.group("anchor_label"))
                )
                payload["anchor_child_kind"] = anchor_kind
                payload["anchor_child_label"] = child_insert.group("anchor_label")
                payload["anchor_child_suffix"] = anchor_suffix
            payload["deterministic_reextraction"] = {
                "strategy": "following_child_insert_from_source_window",
                "requires_materializer_validation": True,
            }
            _remove_review_reasons(
                event,
                {
                    "incomplete_text_edit_payload",
                    "unsupported_materializer_operation",
                    "llm_candidate_not_validated",
                },
            )
            return {
                "operation": "INSERT_CHILD",
                "node_type": kind,
                "label": content_label,
                "content": content,
            }
    return None


def _decision_row(event: dict[str, Any], *, action: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "action": action,
        "detail": detail,
        "reviewed_by": RECOVERY_VERSION,
        "reviewed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def recover_context(
    *,
    events_path: Path,
    output: Path,
    decisions_output: Path,
    report_output: Path,
    source_archive_root: Path = Path("sources"),
    notifications_dir: Path = Path("data/Law/cbic_tax_portal/notifications"),
) -> dict[str, Any]:
    rows = _read_jsonl(events_path)
    output_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    counts = {
        "input_count": len(rows),
        "metadata_only_count": 0,
        "context_recovered_count": 0,
        "forms_lane_pending_baseline_count": 0,
        "baseline_source_only_count": 0,
        "rules_table_lane_count": 0,
        "context_unresolved_count": 0,
        "canonical_id_normalized_count": 0,
        "deterministic_reextraction_count": 0,
        "llm_reextraction_candidate_count": 0,
        "stale_source_window_cleanup_count": 0,
    }
    llm_candidates: list[dict[str, Any]] = []

    for original in rows:
        event = copy.deepcopy(original)
        target = event.get("target") or {}
        work_id = str(target.get("work_id") or "")
        component_id = str(target.get("component_id") or "")
        if work_id != RULES_WORK and component_id != RULES_WORK and not component_id.startswith(RULES_WORK):
            output_rows.append(event)
            continue
        if event.get("status") != "needs_review":
            output_rows.append(event)
            continue
        if _undo_stale_source_window_insert(event):
            counts["stale_source_window_cleanup_count"] += 1
            decisions.append(_decision_row(event, action="stale_source_window_cleanup", detail={}))

        lane = triage_event(event)
        if lane == "canonical_id_normalized":
            normalized = _normalize_malformed_component(component_id)
            event.setdefault("target", {})["component_id"] = normalized
            counts["canonical_id_normalized_count"] += 1
            decisions.append(_decision_row(event, action="canonical_id_normalized", detail={"component_id": normalized}))
            lane = "amendment_language"
        if lane == "metadata_only":
            _mark_lane(event, lane, "metadata_only")
            counts["metadata_only_count"] += 1
            decisions.append(_decision_row(event, action=lane, detail={}))
            output_rows.append(event)
            continue
        if lane == "forms_lane_pending_baseline":
            _mark_lane(event, lane, "forms_lane_pending_baseline")
            counts["forms_lane_pending_baseline_count"] += 1
            decisions.append(_decision_row(event, action=lane, detail={}))
            output_rows.append(event)
            continue
        if lane == "rules_table_lane":
            _mark_lane(event, lane, "rules_table_lane")
            counts["rules_table_lane_count"] += 1
            decisions.append(_decision_row(event, action=lane, detail={}))
            output_rows.append(event)
            continue
        if lane == "baseline_source_only":
            _mark_lane(event, lane, "baseline_source_only")
            counts["baseline_source_only_count"] += 1
            decisions.append(_decision_row(event, action=lane, detail={}))
            output_rows.append(event)
            continue

        span = (event.get("evidence") or {}).get("source_span") or {}
        try:
            start = int(span.get("start"))
        except (TypeError, ValueError):
            start = -1
        full_text, text_source = notification_text(
            event,
            source_archive_root=source_archive_root,
            notifications_dir=notifications_dir,
        )
        context = recover_context_from_text(full_text, start, str((event.get("evidence") or {}).get("excerpt") or ""))
        if context:
            component_id, structural_detail = _structural_target_component(event, context)
            event.setdefault("target", {})["component_id"] = component_id
            if structural_detail.get("parent_component_id"):
                event.setdefault("payload", {})["parent_component_id"] = structural_detail["parent_component_id"]
            if structural_detail.get("requires_validation"):
                event.setdefault("validation", {})["materializable"] = False
            effective_date = _parse_effective_date_from_context(full_text, context, start)
            if effective_date:
                legal_time = event.setdefault("legal_time", {})
                legal_time["applicability_start"] = effective_date
                legal_time["commencement_date"] = effective_date
                legal_time["date_basis"] = "source_effective_date_context"
            event.setdefault("payload", {})["context_recovered_target"] = True
            event["payload"]["context_recovery"] = {
                "strategy": "context_recovered_target",
                "text_source": text_source,
                "matched_text": context.get("matched_text"),
                "match_offset": context.get("match_offset"),
                **structural_detail,
            }
            if effective_date:
                event["payload"]["context_recovery"]["effective_date"] = effective_date
            review = event.setdefault("review", {})
            reasons = [r for r in review.get("review_reasons", []) if r != "target_not_resolved"]
            if not (event.get("validation") or {}).get("materializable"):
                if "context_recovered_target_pending_validation" not in reasons:
                    reasons.append("context_recovered_target_pending_validation")
            review["review_reasons"] = reasons
            counts["context_recovered_count"] += 1
            decisions.append(_decision_row(event, action="context_recovered_target", detail=context))
            source_window = full_text[start : min(len(full_text), start + 3000)] if full_text and start >= 0 else ""
            reextracted = _deterministic_reextract_payload(event, source_window)
            if reextracted:
                component_id, structural_detail = _structural_target_component(event, context)
                event.setdefault("target", {})["component_id"] = component_id
                if structural_detail.get("parent_component_id"):
                    event.setdefault("payload", {})["parent_component_id"] = structural_detail["parent_component_id"]
                if structural_detail:
                    event["payload"].setdefault("context_recovery", {}).update(structural_detail)
                counts["deterministic_reextraction_count"] += 1
                decisions.append(_decision_row(event, action="deterministic_reextraction", detail=reextracted))
            if _candidate_requires_llm(event):
                counts["llm_reextraction_candidate_count"] += 1
                llm_candidates.append(
                    {
                        "event_id": event.get("event_id"),
                        "target": event.get("target"),
                        "source_span": span,
                        "excerpt": (event.get("evidence") or {}).get("excerpt", ""),
                        "context_window_sha256": hashlib.sha256(
                            full_text[max(0, start - 1500) : min(len(full_text), start + 1500)].encode("utf-8")
                        ).hexdigest()
                        if full_text and start >= 0
                        else "",
                    }
                )
        else:
            _mark_lane(event, "context_unresolved", "context_unresolved")
            counts["context_unresolved_count"] += 1
            decisions.append(_decision_row(event, action="context_unresolved", detail={"text_source": text_source}))
        output_rows.append(event)

    _write_jsonl(output, output_rows)
    decisions_output.parent.mkdir(parents=True, exist_ok=True)
    decisions_output.write_text(
        json.dumps({"version": RECOVERY_VERSION, "decisions": decisions}, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    report = {
        "ok": True,
        "version": RECOVERY_VERSION,
        "events_path": str(events_path),
        "output": str(output),
        "source_archive_root": str(source_archive_root),
        "notifications_dir": str(notifications_dir),
        **counts,
    }
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    llm_candidates_path = report_output.parent / "llm_reextraction_candidates.json"
    llm_report_path = report_output.parent / "llm_reextraction_report.json"
    llm_candidates_path.write_text(json.dumps(llm_candidates, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    llm_report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "version": RECOVERY_VERSION,
                "candidate_count": len(llm_candidates),
                "promotion_policy": "candidate_only_requires_deterministic_validation",
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return report


__all__ = ["RECOVERY_VERSION", "recover_context", "recover_context_from_text", "triage_event"]
