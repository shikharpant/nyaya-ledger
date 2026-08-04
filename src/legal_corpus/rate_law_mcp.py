"""Rate-change and law-change MCP tool service.

Deterministic, read-only retrieval over the event-sourced rate schedules
(``derived/version_history/rate-schedules/``) and the statute version history
(``derived/version_history/<work>/``). No model calls; pure JSONL/XML retrieval.

The envelope returned by every public method::

    {
      "result": "ok" | "unresolved" | "error",
      "snapshot_id": "...",
      "retrieved_at": "<ISO8601>",
      "coverage_warning": str | None,
      "unresolved_gaps": [str, ...],
      "source_refs": [{"document_id", "artifact_sha256", "locator"}, ...],
      <tool-specific fields>,
    }

Temporal rule (enforced everywhere): a rate/law version for date D is returned
only when ``effective_from <= D`` and (``effective_to`` is null or ``> D``).
If nothing covers D the result is ``unresolved`` -- never a silent fallback to
the current version.

Review separation (``trace_amendments``): reviewed amendment determinations
(materialized ``node_versions.jsonl`` rows whose ``created_by_event_id`` is set,
or events with ``review.status`` in {accepted, validated}) are the main result.
Unreviewed candidates (``llm_candidates.jsonl`` or non-accepted review state)
appear only when ``include_unreviewed=True`` and live in a separate
``unreviewed_candidates`` array with a ``coverage_warning``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rate_schedule_materializer import BASE_JSON_MAP, materialize_schedule
from .version_compare import (
    compare_component_versions,
    normalize_version_component_id,
    read_node_versions,
    resolve_version_dir,
)
from .version_reconstruct import reconstruct_component

DEFAULT_RATE_DATA_ROOT = "derived/version_history/rate-schedules"
DEFAULT_VERSION_HISTORY_ROOT = "derived/version_history"
DEFAULT_REGISTRY = "data/Law/statute_identity_registry.json"

_REVIEWED_STATES = {"accepted", "validated"}

# Notification families -> jurisdiction tag used by get_rate_for_hsn.
_JURISDICTION_FAMILIES = {
    "cgst": ["1/2017-ct-rate"],
    "igst": ["2/2017-ct-rate"],
    "cess": ["1/2017-cc-rate", "2/2017-cc-rate", "1/2025-cc-rate"],
}

# Goods schedules carry the HSN in tariff_item. Services (11/2017-ct-rate) are
# heading/S.No keyed and are intentionally excluded from HSN lookup.
_HSN_NOTIFICATIONS = ["1/2017-ct-rate", "2/2017-ct-rate",
                      "1/2017-cc-rate", "2/2017-cc-rate", "1/2025-cc-rate"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_hsn(hsn: str) -> str:
    """Strip spaces/punctuation, keep digits (leading zeros preserved)."""
    return re.sub(r"[^0-9]", "", hsn or "").lstrip().strip()


_CHAPTER_RE = re.compile(r"^[Cc]hapter\s+([0-9]{1,2})$")


def _tariff_codes(tariff_item: str) -> list[tuple[str, str]]:
    """Tokenize a tariff_item into clean (kind, code) pairs.

    HSN codes are frequently written with internal spaces or dots marking the
    hierarchy (``"2403 99 10"`` == 2403.99.10), while distinct tariff items in
    one entry are comma/semicolon/slash separated. So we split only on the
    latter and collapse the former. ``kind`` is ``"hsn"`` for numeric codes
    (2-8 digits) or ``"chapter"`` for ``Chapter NN``. Junk such as ``"00]"`` or
    ``"90 or any other Chapter"`` (letters/brackets) is rejected so it cannot
    prefix-match a real HSN.
    """
    codes: list[tuple[str, str]] = []
    for raw in re.split(r"[,;/]+", (tariff_item or "")):
        raw = raw.strip().strip(".")
        if not raw:
            continue
        m = _CHAPTER_RE.match(raw)
        if m:
            codes.append(("chapter", m.group(1).zfill(2)))
            continue
        compact = re.sub(r"[\s.\-]+", "", raw)
        if compact.isdigit() and 2 <= len(compact) <= 8:
            codes.append(("hsn", compact))
    return codes


def _hsn_matches(query: str, tariff: str) -> bool:
    q = _normalize_hsn(query)
    if len(q) < 2:
        return False
    for kind, code in _tariff_codes(tariff):
        if kind == "chapter":
            # A chapter reference matches any HSN whose first digits are that
            # chapter (e.g. "Chapter 03" matches HSN 0303).
            if len(code) >= 2 and q.startswith(code):
                return True
            continue
        if q == code:
            return True
        # Prefix match only when one is strictly shorter than the other.
        # (min/max-by-len is unsafe: on equal lengths both return the first
        # argument, making the check trivially true.)
        if len(q) < len(code):
            shorter, longer = q, code
        elif len(code) < len(q):
            shorter, longer = code, q
        else:
            continue
        if len(shorter) >= 2 and longer.startswith(shorter):
            return True
    return False


def _snapshot_id(rate_root: Path, vh_root: Path | None) -> str:
    import hashlib
    parts = []
    try:
        parts.append(",".join(sorted(p.name for p in rate_root.glob("base_*.json")) if rate_root.exists() else []))
    except OSError:
        pass
    if vh_root is not None:
        nv = vh_root / "cgst-rules-2017" / "node_versions.jsonl"
        try:
            if nv.exists():
                parts.append(f"nv:{nv.stat().st_size}:{int(nv.stat().st_mtime)}")
        except OSError:
            pass
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"nyaya-vh-{digest}"


@dataclass
class RateLawService:
    rate_data_root: Path = Path(DEFAULT_RATE_DATA_ROOT)
    # When None, law-version tools resolve via the statute identity registry
    # (the same path the production NyayaToolService uses). Pass a Path only for
    # injected test roots.
    version_history_root: Path | None = None
    registry_path: Path = Path(DEFAULT_REGISTRY)
    # Overridable event-ledger filenames (the service-checkpoint fixture uses
    # llm_svc_events.jsonl instead of the production rate_amendment_events.jsonl).
    rate_events_filename: str = "rate_amendment_events.jsonl"
    cess_events_filename: str = "cess_amendment_events.jsonl"

    def _vh_kwargs(self) -> dict[str, Any]:
        """Build version_dir/registry_path kwargs only when overriding."""
        kw: dict[str, Any] = {}
        if self.version_history_root is not None:
            kw["version_dir"] = self.version_history_root
        return kw

    # ---------- envelope helpers ----------

    def _envelope(self, *, result: str, coverage_warning: str | None = None,
                  unresolved_gaps: list[str] | None = None,
                  source_refs: list[dict] | None = None, **fields: Any) -> dict[str, Any]:
        env = {
            "result": result,
            "snapshot_id": _snapshot_id(self.rate_data_root, self.version_history_root),
            "retrieved_at": _now_iso(),
            "coverage_warning": coverage_warning,
            "unresolved_gaps": unresolved_gaps or [],
            "source_refs": source_refs or [],
        }
        env.update(fields)
        return env

    def _rate_events_path(self, target_notification: str) -> Path:
        # Compensation cess events live in a separate JSONL.
        if target_notification.endswith("-cc-rate"):
            return self.rate_data_root / self.cess_events_filename
        return self.rate_data_root / self.rate_events_filename

    def _load_rate_events(self, target_notification: str) -> list[dict[str, Any]]:
        path = self._rate_events_path(target_notification)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def _materialize(self, target_notification: str, as_of_date: str | None) -> dict[str, Any] | None:
        target_notification = self._normalize_target(target_notification)
        base_name = BASE_JSON_MAP.get(target_notification)
        if not base_name:
            return None
        # BASE_JSON_MAP values are workspace-relative paths ending in a filename
        # under the rate data root. Resolve by basename so both production paths
        # and injected test roots work identically.
        base_path = self.rate_data_root / Path(base_name).name
        if not base_path.exists():
            return None
        return materialize_schedule(
            base_path,
            self._rate_events_path(target_notification),
            target_notification,
            checkpoint_date=as_of_date,
        )

    def _normalize_target(self, target_notification: str) -> str:
        """Accept short forms like '1/2017' by defaulting the family suffix."""
        t = (target_notification or "").strip()
        if t in BASE_JSON_MAP:
            return t
        for suffix in ("-ct-rate", "-cc-rate"):
            if (t + suffix) in BASE_JSON_MAP:
                return t + suffix
        return t

    def _instrument_type(self, target_notification: str) -> str:
        """Return the base schedule's instrument_type (e.g. goods_rate /
        goods_exempt / service_rate). Empty string when unknown."""
        target_notification = self._normalize_target(target_notification)
        base_name = BASE_JSON_MAP.get(target_notification)
        if not base_name:
            return ""
        base_path = self.rate_data_root / Path(base_name).name
        if not base_path.exists():
            return ""
        try:
            with open(base_path, encoding="utf-8") as f:
                return str(json.load(f).get("instrument_type") or "")
        except (OSError, json.JSONDecodeError):
            return ""

    def _base_effective_from(self, target_notification: str) -> str:
        """Read the base schedule's effective_from (when the rate first exists)."""
        target_notification = self._normalize_target(target_notification)
        base_name = BASE_JSON_MAP.get(target_notification)
        if not base_name:
            return ""
        base_path = self.rate_data_root / Path(base_name).name
        if not base_path.exists():
            return ""
        try:
            with open(base_path, encoding="utf-8") as f:
                return str(json.load(f).get("effective_from") or "")
        except (OSError, json.JSONDecodeError):
            return ""

    # ---------- rate-change tools ----------

    def get_rate_for_hsn(self, hsn_code: str, as_of_date: str,
                         jurisdiction: str | None = None) -> dict[str, Any]:
        norm = _normalize_hsn(hsn_code)
        if not norm:
            return self._envelope(result="unresolved",
                                  unresolved_gaps=[f"invalid HSN code: {hsn_code!r}"],
                                  hsn_code=hsn_code, as_of_date=as_of_date, matches=[])
        targets = _JURISDICTION_FAMILIES.get(jurisdiction, _HSN_NOTIFICATIONS) if jurisdiction else _HSN_NOTIFICATIONS
        matches: list[dict[str, Any]] = []
        source_refs: list[dict[str, Any]] = []
        for target in targets:
            snap = self._materialize(target, as_of_date)
            if not snap:
                continue
            active = snap.get("active_notification") or target
            instrument_type = self._instrument_type(target)
            # Distinguish positive rate schedules from exemption/nil notifications.
            # The same HSN can appear in both with mutually-exclusive conditions
            # (e.g. HSN 0303: pre-packaged fish taxed at 2.5%, loose fish exempt).
            is_exempt = "exempt" in instrument_type or instrument_type in (
                "goods_exempt", "service_exempt")
            kind = "exemption" if is_exempt else "rate"
            for sid, sched in (snap.get("schedules") or {}).items():
                for entry in sched.get("entries") or []:
                    if entry.get("is_omitted"):
                        continue
                    if not _hsn_matches(norm, entry.get("tariff_item", "")):
                        continue
                    rate_pct = float(entry.get("rate_pct") or sched.get("rate_pct") or 0.0)
                    matches.append({
                        "notification": active,
                        "instrument_type": instrument_type,
                        "notification_kind": kind,
                        "schedule_id": sid,
                        "sno": entry.get("sno", ""),
                        "tariff_item": entry.get("tariff_item", ""),
                        "description": entry.get("description", ""),
                        "rate_pct": rate_pct,
                        "rate_breakdown": _breakdown(rate_pct, target),
                        "as_of_date": as_of_date,
                        "checkpoint_date": snap.get("checkpoint_date", as_of_date),
                    })
                    source_refs.append({
                        "document_id": snap.get("notification_id") or active,
                        "artifact_sha256": "",
                        "locator": f"schedule {sid} sno {entry.get('sno', '')}",
                    })
        if not matches:
            return self._envelope(
                result="unresolved",
                coverage_warning=f"no rate entry covers HSN {norm} on {as_of_date}",
                unresolved_gaps=[f"no non-omitted entry for HSN {norm} in {targets} at {as_of_date}"],
                source_refs=source_refs,
                hsn_code=hsn_code, normalized_hsn=norm, as_of_date=as_of_date, matches=[],
            )
        # Flag conditional-exemption conflicts: HSN matches both a positive rate
        # schedule and an exemption notification (legally distinct treatments).
        kinds = {m["notification_kind"] for m in matches}
        warning = None
        if {"rate", "exemption"} <= kinds:
            warning = (
                f"HSN {norm} appears in both a rate schedule and an exemption "
                "notification; the applicable treatment depends on the entry "
                "conditions (see each match's description)."
            )
        return self._envelope(
            result="ok", coverage_warning=warning, source_refs=source_refs,
            hsn_code=hsn_code, normalized_hsn=norm, as_of_date=as_of_date,
            matches=matches,
        )

    def trace_rate_changes(self, hsn_code: str, from_date: str | None = None,
                           to_date: str | None = None) -> dict[str, Any]:
        norm = _normalize_hsn(hsn_code)
        if not norm:
            return self._envelope(result="unresolved",
                                  unresolved_gaps=[f"invalid HSN code: {hsn_code!r}"],
                                  hsn_code=hsn_code, changes=[])
        # Collect all distinct event effective dates across goods notifications.
        dates: set[str] = set()
        per_target_events: dict[str, list[dict[str, Any]]] = {}
        for target in _HSN_NOTIFICATIONS:
            evs = self._load_rate_events(target)
            per_target_events[target] = evs
            for e in evs:
                d = e.get("effective_date")
                if d and (not from_date or d >= from_date) and (not to_date or d <= to_date):
                    dates.add(d)
        # Baseline date = one day before earliest event, to capture the base rate.
        checkpoints = sorted(dates | {d for d in (from_date, to_date) if d})
        # Always include the base effective date so the first state is visible.
        changes: list[dict[str, Any]] = []
        prev_by_target: dict[str, dict[str, Any] | None] = {}
        for target in _HSN_NOTIFICATIONS:
            prev_by_target[target] = None
        for cp in checkpoints:
            for target in _HSN_NOTIFICATIONS:
                # A rate does not exist before its instrument commenced.
                base_from = self._base_effective_from(target)
                if base_from and cp < base_from:
                    prev_by_target[target] = None
                    continue
                snap = self._materialize(target, cp)
                if not snap:
                    continue
                entry = _find_entry_for_hsn(snap, norm)
                cur = None
                if entry is not None and not entry.get("is_omitted"):
                    cur = {
                        "rate_pct": float(entry.get("rate_pct") or (snap.get("schedules") or {}).get(
                            list((snap.get("schedules") or {}).keys())[0] if (snap.get("schedules")) else "I",
                            {}).get("rate_pct") or 0.0),
                        "description": entry.get("description", ""),
                        "omitted": False,
                    }
                elif entry is not None and entry.get("is_omitted"):
                    cur = {"rate_pct": 0.0, "description": "", "omitted": True}
                prev = prev_by_target[target]
                if _state_changed(prev, cur):
                    amending = _amending_event_for_date(per_target_events.get(target, []), cp)
                    changes.append({
                        "effective_date": cp,
                        "notification": target,
                        "amending_notification": amending.get("source_notification", "") if amending else "",
                        "amending_citation": amending.get("source_cbic_no", "") if amending else "",
                        "operation": amending.get("operation", "") if amending else "",
                        "old_rate_pct": prev.get("rate_pct") if prev else None,
                        "new_rate_pct": cur.get("rate_pct") if cur else None,
                        "omitted": bool(cur and cur.get("omitted")),
                        "retrospective_flag": _retrospective(amending) if amending else False,
                    })
                prev_by_target[target] = cur
        changes.sort(key=lambda c: (c["effective_date"], c["notification"]))
        return self._envelope(
            result="ok" if changes else "unresolved",
            coverage_warning=None if changes else f"no rate changes recorded for HSN {norm}",
            unresolved_gaps=[] if changes else [f"no events touched an entry matching HSN {norm}"],
            hsn_code=hsn_code, normalized_hsn=norm, from_date=from_date, to_date=to_date,
            changes=changes,
        )

    def get_rate_conditions(self, rate_entry_id_or_hsn_plus_date: str,
                            as_of_date: str | None = None) -> dict[str, Any]:
        target, locator, hsn, sno = _parse_rate_locator(rate_entry_id_or_hsn_plus_date)
        if target is None:
            return self._envelope(result="unresolved",
                                  unresolved_gaps=[f"could not parse rate locator: {rate_entry_id_or_hsn_plus_date!r}"],
                                  rate_entry_id=rate_entry_id_or_hsn_plus_date, conditions=[])
        snap = self._materialize(target, as_of_date)
        if not snap:
            return self._envelope(result="unresolved",
                                  unresolved_gaps=[f"no materialized schedule for {target}"],
                                  rate_entry_id=rate_entry_id_or_hsn_plus_date, conditions=[])
        entry, schedule = _find_entry(snap, hsn=hsn, sno=sno, locator=locator)
        if entry is None:
            return self._envelope(result="unresolved",
                                  coverage_warning="entry not found in schedule",
                                  unresolved_gaps=[f"no entry for locator in {target}"],
                                  rate_entry_id=rate_entry_id_or_hsn_plus_date, conditions=[])
        conditions = {
            "notification": snap.get("active_notification") or target,
            "schedule_id": (schedule or {}).get("schedule_id", ""),
            "sno": entry.get("sno", ""),
            "tariff_item": entry.get("tariff_item", ""),
            "description": entry.get("description", ""),
            "sub_items": entry.get("sub_items") or [],
            "attached_explanation": entry.get("attached_explanation") or "",
            "schedule_heading": (schedule or {}).get("heading", ""),
            "opening_paragraph": snap.get("opening_paragraph") or "",
            "explanations": snap.get("explanations") or [],
        }
        return self._envelope(
            result="ok",
            source_refs=[{
                "document_id": snap.get("notification_id") or target,
                "artifact_sha256": "",
                "locator": f"schedule {conditions['schedule_id']} sno {entry.get('sno','')}",
            }],
            rate_entry_id=rate_entry_id_or_hsn_plus_date, as_of_date=as_of_date,
            conditions=conditions,
        )

    def compare_rates(self, hsn_codes: list[str], as_of_date: str) -> dict[str, Any]:
        side_by_side = {}
        gaps: list[str] = []
        for code in hsn_codes:
            r = self.get_rate_for_hsn(code, as_of_date)
            side_by_side[code] = {
                "normalized_hsn": r.get("normalized_hsn"),
                "result": r.get("result"),
                "matches": r.get("matches", []),
            }
            if r.get("result") != "ok":
                gaps.extend(r.get("unresolved_gaps") or [])
        return self._envelope(
            result="ok",
            coverage_warning="some HSNs unresolved" if gaps else None,
            unresolved_gaps=gaps,
            hsn_codes=hsn_codes, as_of_date=as_of_date, comparison=side_by_side,
        )

    # ---------- law-change tools ----------

    def get_law_as_of(self, citation: str, as_of_date: str) -> dict[str, Any]:
        component = normalize_version_component_id(citation)
        try:
            rec = reconstruct_component(
                component, date=as_of_date, **self._vh_kwargs(),
            )
        except Exception as exc:  # resolver/path failure -> unresolved, not a crash
            return self._envelope(result="error", unresolved_gaps=[f"reconstruct failed: {exc}"],
                                  citation=citation, as_of_date=as_of_date)
        if rec.get("status") != "ok":
            return self._envelope(
                result="unresolved",
                coverage_warning="no version covers the requested date",
                unresolved_gaps=[f"no materialized version of {component} covers {as_of_date}"],
                citation=citation, component_id=component, as_of_date=as_of_date,
                version=None, text="",
            )
        version = rec.get("version") or {}
        basis = version.get("source_basis") or {}
        return self._envelope(
            result="ok",
            source_refs=[{
                "document_id": basis.get("source_document_id", ""),
                "artifact_sha256": version.get("text_sha256", ""),
                "locator": component,
            }],
            citation=citation, component_id=component, as_of_date=as_of_date,
            target_work=rec.get("target_work"),
            text=rec.get("text", ""),
            version_id=version.get("version_id"),
            text_sha256=version.get("text_sha256"),
            applicability_start=version.get("applicability_start"),
            applicability_end=version.get("applicability_end") or version.get("valid_to"),
            event_chain=version.get("event_chain") or [],
            source_basis=basis,
        )

    def trace_amendments(self, citation: str, include_unreviewed: bool = False) -> dict[str, Any]:
        component = normalize_version_component_id(citation)
        try:
            version_dir, resolved_work = resolve_version_dir(
                component, target_work=None, **self._vh_kwargs(),
            )
        except Exception as exc:
            return self._envelope(result="error", unresolved_gaps=[f"resolve failed: {exc}"],
                                  citation=citation, amendments=[], unreviewed_candidates=[])
        nv = version_dir / "node_versions.jsonl"
        if not nv.exists():
            return self._envelope(result="unresolved",
                                  unresolved_gaps=[f"no node_versions.jsonl under {version_dir}"],
                                  citation=citation, component_id=component, amendments=[])
        rows = [r for r in read_node_versions(nv) if normalize_version_component_id(
            str(r.get("component_id") or "")) == component]
        rows.sort(key=lambda r: r.get("applicability_start") or r.get("valid_from") or "")
        amendments: list[dict[str, Any]] = []
        prev_text = ""
        for row in rows:
            event_id = row.get("created_by_event_id")
            basis = row.get("source_basis") or {}
            text = row.get("text", "")
            if event_id:
                amendments.append({
                    "event_id": event_id,
                    "effective_date": row.get("applicability_start") or row.get("valid_from"),
                    "operation": basis.get("operation", ""),
                    "source_document_id": basis.get("source_document_id", ""),
                    "source_record_id": basis.get("source_record_id", ""),
                    "text_diff": _short_diff(prev_text, text),
                    "retrospective_flag": _retrospective(row.get("event") or basis),
                })
            prev_text = text
        env_extras: dict[str, Any] = {
            "citation": citation, "component_id": component, "target_work": resolved_work,
            "amendments": amendments,
        }
        if include_unreviewed:
            unreviewed = self._unreviewed_candidates(component, version_dir)
            env_extras["unreviewed_candidates"] = unreviewed
            warning = (f"{len(unreviewed)} unreviewed candidate(s) separated from verified amendments")
        else:
            env_extras["unreviewed_candidates"] = []
            warning = None
        return self._envelope(
            result="ok" if amendments else "unresolved",
            coverage_warning=warning,
            unresolved_gaps=[] if amendments else [f"no reviewed amendments for {component}"],
            citation=citation, **{k: env_extras[k] for k in (
                "component_id", "target_work", "amendments", "unreviewed_candidates")},
        )

    def _unreviewed_candidates(self, component: str, version_dir: Path) -> list[dict[str, Any]]:
        cands: list[dict[str, Any]] = []
        for name in ("llm_candidates.jsonl",):
            p = version_dir / name
            if not p.exists():
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tgt = ev.get("target") or {}
                    if normalize_version_component_id(str(tgt.get("component_id") or tgt.get("id") or "")) != component:
                        # fall back to payload-level component hints
                        continue
                    review = (ev.get("review") or {})
                    if review.get("status") in _REVIEWED_STATES:
                        continue
                    cands.append({
                        "event_id": ev.get("event_id"),
                        "operation": ev.get("operation"),
                        "effective_date": (ev.get("legal_time") or {}).get("effective_date")
                        or (ev.get("payload") or {}).get("effective_date"),
                        "review_status": review.get("status", "proposed"),
                        "source_document_id": (ev.get("source") or {}).get("document_id", ""),
                    })
        return cands

    def get_amendment_instrument(self, amendment_id_or_citation_plus_date: str) -> dict[str, Any]:
        # Accept "<canonical_id>@<date>" or a bare event/source document id.
        citation, _, date = amendment_id_or_citation_plus_date.partition("@")
        component = normalize_version_component_id(citation)
        try:
            version_dir, _ = resolve_version_dir(
                component, target_work=None, **self._vh_kwargs(),
            )
        except Exception as exc:
            return self._envelope(result="error", unresolved_gaps=[f"resolve failed: {exc}"],
                                  query=amendment_id_or_citation_plus_date, instrument=None)
        nv = version_dir / "node_versions.jsonl"
        instrument_id = ""
        if nv.exists():
            rows = [r for r in read_node_versions(nv) if normalize_version_component_id(
                str(r.get("component_id") or "")) == component]
            rows.sort(key=lambda r: r.get("applicability_start") or "")
            # Prefer a real corpus document id; reconciliation-source ids start
            # with "derived/" and are not corpus instruments.
            def _real_doc(row: dict[str, Any]) -> str:
                doc = (row.get("source_basis") or {}).get("source_document_id", "")
                return doc if doc and not str(doc).startswith("derived/") else ""
            if date:
                # nearest-on-or-before with a real instrument, else fall back to any
                on_or_before = [r for r in rows if (r.get("applicability_start") or "") <= date]
                for r in reversed(on_or_before):
                    if _real_doc(r):
                        instrument_id = _real_doc(r)
                        break
            if not instrument_id:
                for r in reversed(rows):
                    if _real_doc(r):
                        instrument_id = _real_doc(r)
                        break
        if not instrument_id:
            return self._envelope(
                result="unresolved",
                unresolved_gaps=[f"no instrument resolves for {component}@{date or '*'}"],
                query=amendment_id_or_citation_plus_date, instrument=None,
            )
        # Look up the instrument's corpus text.
        try:
            from .serving import NyayaToolService  # local import; heavy module
            inst = NyayaToolService().lookup_provision(instrument_id, include_text=True)
        except Exception:
            inst = {"canonical_id": instrument_id}
        return self._envelope(
            result="ok",
            source_refs=[{"document_id": instrument_id, "artifact_sha256": "",
                          "locator": component}],
            query=amendment_id_or_citation_plus_date,
            instrument={
                "document_id": instrument_id,
                "commencement_date": (inst.get("provision") or {}).get("effective_from")
                if isinstance(inst, dict) else None,
                "text": (inst.get("text") or "") if isinstance(inst, dict) else "",
                "raw": inst if isinstance(inst, dict) else {},
            },
        )

    def get_commencement_chain(self, citation: str, amendment_date: str) -> dict[str, Any]:
        component = normalize_version_component_id(citation)
        try:
            version_dir, resolved_work = resolve_version_dir(
                component, target_work=None, **self._vh_kwargs(),
            )
        except Exception as exc:
            return self._envelope(result="error", unresolved_gaps=[f"resolve failed: {exc}"],
                                  citation=citation, commencement=None)
        nv = version_dir / "node_versions.jsonl"
        if not nv.exists():
            return self._envelope(result="unresolved",
                                  unresolved_gaps=[f"no node_versions.jsonl under {version_dir}"],
                                  citation=citation, commencement=None)
        rows = [r for r in read_node_versions(nv) if normalize_version_component_id(
            str(r.get("component_id") or "")) == component]
        rows.sort(key=lambda r: r.get("applicability_start") or "")
        row = next((r for r in rows if (r.get("applicability_start") or "") == amendment_date), None) \
            or next((r for r in rows if (r.get("applicability_start") or "") <= amendment_date), None)
        if not row:
            return self._envelope(
                result="unresolved",
                unresolved_gaps=[f"no version of {component} at or before {amendment_date}"],
                citation=citation, amendment_date=amendment_date, commencement=None,
            )
        basis = row.get("source_basis") or {}
        legal_time = row.get("legal_time") or (row.get("event") or {}).get("legal_time") or {}
        applicability = row.get("applicability_start") or row.get("valid_from")
        # The version history stores the instrument's notification date as
        # applicability_start; enactment_date is not separately captured in
        # node_versions, so default it to the applicability date and warn.
        enactment = (legal_time.get("enactment_date")
                     or legal_time.get("publication_date")
                     or applicability)
        commencement = {
            "enactment_date": enactment,
            "commencement_date": applicability,
            "retrospective_operation": bool(legal_time.get("retrospective")),
            "saving_clauses": _extract_clauses(row.get("text", ""), ("saving", "save", "notwithstand")),
            "transition_provisions": _extract_clauses(row.get("text", ""), ("transition", "transitional")),
            "source_document_id": basis.get("source_document_id", ""),
        }
        gaps: list[str] = []
        warning = None
        if not legal_time:
            # No explicit legal_time in the version row: enactment/commencement
            # both fall back to the stored notification date. The statutory
            # appointed date can differ (e.g. GST rules notified 2017-06-19 but
            # in force from 2017-07-01) and is not separately captured.
            warning = ("enactment_date and commencement_date both reflect the "
                       "instrument's notification date; the statutory appointed "
                       "date may differ and is not separately captured")
            gaps.append("no explicit legal_time in version row; using notification date")
        if not commencement["commencement_date"]:
            commencement["commencement_date"] = None
            commencement["commencement_status"] = "commencement_unspecified"
            gaps.append("commencement date absent")
            warning = "commencement date unspecified"
        return self._envelope(
            result="ok",
            coverage_warning=warning,
            unresolved_gaps=gaps,
            citation=citation, component_id=component, target_work=resolved_work,
            amendment_date=amendment_date, commencement=commencement,
        )

    def compare_law_versions(self, citation: str, version_a_date: str,
                             version_b_date: str) -> dict[str, Any]:
        component = normalize_version_component_id(citation)
        try:
            cmp = compare_component_versions(
                component, from_date=version_a_date, to_date=version_b_date,
                **self._vh_kwargs(),
            )
        except Exception as exc:
            return self._envelope(result="error", unresolved_gaps=[f"compare failed: {exc}"],
                                  citation=citation, comparison=None)
        if cmp.get("status") not in ("ok", "changed", "unchanged") and cmp.get("status") in (
                "not_found", "no_materialized_history"):
            return self._envelope(
                result="unresolved",
                unresolved_gaps=[f"compare status={cmp.get('status')} for {component}"],
                citation=citation, comparison=cmp)
        fv = cmp.get("from_version") or {}
        tv = cmp.get("to_version") or {}
        return self._envelope(
            result="ok",
            source_refs=[
                {"document_id": (fv.get("source_basis") or {}).get("source_document_id", ""),
                 "artifact_sha256": fv.get("text_sha256", ""), "locator": f"{component}@{version_a_date}"},
                {"document_id": (tv.get("source_basis") or {}).get("source_document_id", ""),
                 "artifact_sha256": tv.get("text_sha256", ""), "locator": f"{component}@{version_b_date}"},
            ],
            citation=citation, component_id=component,
            version_a_date=version_a_date, version_b_date=version_b_date,
            text_changed=bool(cmp.get("text_changed")),
            unified_diff=cmp.get("unified_diff", ""),
            events_between=cmp.get("events_between", []),
            from_version=_slim(fv), to_version=_slim(tv),
        )


# ---------- module-level helpers ----------

def _breakdown(rate_pct: float, target: str) -> dict[str, float]:
    """Derive CGST/SGST/IGST/cess from a schedule rate and notification family."""
    if target.endswith("-cc-rate"):
        return {"cess": round(rate_pct, 4)}
    # CGST/IGST main schedules: rate_pct is the CGST component; SGST mirrors it,
    # IGST is double.
    return {
        "cgst": round(rate_pct, 4),
        "sgst": round(rate_pct, 4),
        "igst": round(2 * rate_pct, 4),
    }


def _find_entry_for_hsn(snap: dict[str, Any], norm_hsn: str) -> dict[str, Any] | None:
    for sched in (snap.get("schedules") or {}).values():
        for entry in sched.get("entries") or []:
            if _hsn_matches(norm_hsn, entry.get("tariff_item", "")):
                return entry
    return None


def _find_entry(snap: dict[str, Any], hsn: str | None = None, sno: str | None = None,
                locator: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    scheds = snap.get("schedules") or {}
    for sched in scheds.values():
        for entry in sched.get("entries") or []:
            if sno and entry.get("sno", "").rstrip(".").upper() == sno.rstrip(".").upper():
                return entry, sched
            if hsn and _hsn_matches(hsn, entry.get("tariff_item", "")):
                return entry, sched
        if locator and sched.get("schedule_id", "") == locator:
            # locator matched schedule only; return first entry as anchor
            ents = sched.get("entries") or []
            return (ents[0] if ents else None), sched
    return None, None


def _parse_rate_locator(raw: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse '11/2017-ct-rate::sno=3', '1/2017::hsn=0101@2024-01-01', or notification id."""
    raw = raw.strip()
    if "::" in raw:
        target, spec = raw.split("::", 1)
    else:
        # bare notification id, e.g. '1/2017-ct-rate'
        if raw in BASE_JSON_MAP:
            return raw, None, None, None
        return None, None, None, None
    hsn = sno = locator = None
    m = re.search(r"hsn=([0-9]+)", spec)
    if m:
        hsn = m.group(1)
    m = re.search(r"sno=([0-9A-Za-z.]+)", spec)
    if m:
        sno = m.group(1)
    m = re.search(r"schedule=([IVX0-9]+)", spec)
    if m:
        locator = m.group(1)
    return target, locator, hsn, sno


def _state_changed(prev: dict[str, Any] | None, cur: dict[str, Any] | None) -> bool:
    if prev is None and cur is None:
        return False
    if prev is None or cur is None:
        return True
    return (prev.get("rate_pct") != cur.get("rate_pct")) or (prev.get("omitted") != cur.get("omitted"))


def _amending_event_for_date(events: list[dict[str, Any]], date: str) -> dict[str, Any] | None:
    candidates = [e for e in events if e.get("effective_date") == date]
    if not candidates:
        return None
    # Prefer an event that actually changed a rate value.
    for e in candidates:
        if e.get("operation", "").startswith("RATE_SUBSTITUTE") or e.get("operation") in (
                "RATE_SET", "RATE_OMIT_ENTRY", "RATE_INSERT_ENTRY"):
            return e
    return candidates[0]


def _retrospective(event: dict[str, Any] | None) -> bool:
    if not event:
        return False
    lt = event.get("legal_time") or {}
    if lt.get("retrospective"):
        return True
    return bool(event.get("retrospective"))


def _short_diff(a: str, b: str) -> str:
    import difflib
    if not a and not b:
        return ""
    return "\n".join(difflib.unified_diff(
        (a or "").splitlines(), (b or "").splitlines(),
        lineterm="", n=1))[:2000]


def _slim(version: dict[str, Any] | None) -> dict[str, Any]:
    if not version:
        return {}
    return {
        "version_id": version.get("version_id"),
        "text_sha256": version.get("text_sha256"),
        "applicability_start": version.get("applicability_start") or version.get("valid_from"),
        "applicability_end": version.get("applicability_end") or version.get("valid_to"),
        "snippet": (version.get("text") or "")[:280],
    }


def _extract_clauses(text: str, keywords: tuple[str, ...]) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for sent in re.split(r"(?<=[.;])\s+", text):
        low = sent.lower()
        if any(k in low for k in keywords):
            out.append(sent.strip()[:400])
    return out[:5]


__all__ = ["RateLawService", "DEFAULT_RATE_DATA_ROOT", "DEFAULT_VERSION_HISTORY_ROOT"]
