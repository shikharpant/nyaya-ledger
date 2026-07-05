"""Rate schedule materializer — replay amendment events on base schedule JSON.

Loads a base schedule (from rate_schedule_parser), applies rate amendment
events (from rate_schedule_compiler) in chronological order, and emits
versioned snapshots suitable for reconciliation against checkpoints.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

FUZZY_MATCH_THRESHOLD = 0.70

# Roman-numeral → ordinal map for computing the next sub-item marker in a
# service-schedule entry description (used by RATE_SUBSTITUTE_ITEM).
_ROMAN_TO_ORD = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
    "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13,
    "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
}
_ORD_TO_ROMAN = {v: k for k, v in _ROMAN_TO_ORD.items()}


def _next_item_marker(item_id: str) -> str:
    """Given an item marker like '(iii)' or '(a)', return the next sequential
    marker ('(iv)' / '(b)'), or '' if it cannot be computed."""
    inner = item_id.strip().strip("()").lower()
    if inner in _ROMAN_TO_ORD:
        nxt = _ORD_TO_ROMAN.get(_ROMAN_TO_ORD[inner] + 1)
        return f"({nxt})" if nxt else ""
    if len(inner) == 1 and inner.isalpha():
        return f"({chr(ord(inner) + 1)})"
    return ""

# A "simple" cess rate that the checkpoint PDF parser strips out of the
# description (pure ``N%`` or ``NIL``). Compound rates (e.g. "21% or Rs. 4170
# per thousand") are NOT stripped by the checkpoint and must therefore remain
# in the materialized description for reconciliation to match.
_SIMPLE_RATE_RE = re.compile(r"^\d+(?:\.\d+)?\s*%$|^NIL$|^Nil$")


def _join_rate_into_desc(description: str, rate: str) -> str:
    """Append a compound cess rate back into the description text.

    Mirrors the CBIC ready-reckoner checkpoint, which only peels simple
    ``N%`` / ``NIL`` rate cells out of the description column; compound rate
    strings stay embedded in the goods description.
    """
    if not rate:
        return description
    if _SIMPLE_RATE_RE.match(rate.strip()):
        return description
    text = f"{description} {rate}".strip() if description else rate
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ScheduleEntry:
    sno: str
    tariff_item: str = ""
    description: str = ""
    is_omitted: bool = False
    rate_pct: float = 0.0
    rate: str = ""

    def key(self) -> str:
        return self.sno.rstrip(".").strip().upper()


@dataclass
class RateScheduleState:
    """Mutable schedule state that the materializer operates on."""
    schedule_id: str
    rate_pct: float
    entries: list[ScheduleEntry] = field(default_factory=list)

    def entry_index(self) -> dict[str, int]:
        """Build a S.No → position index."""
        return {e.key(): i for i, e in enumerate(self.entries)}

    def find_entry(self, sno: str) -> Optional[ScheduleEntry]:
        key = sno.rstrip(".").strip().upper()
        for e in self.entries:
            if e.key() == key:
                return e
        return None

    def find_position(self, sno: str) -> int:
        """Return the position of the entry with this S.No., or -1."""
        key = sno.rstrip(".").strip().upper()
        for i, e in enumerate(self.entries):
            if e.key() == key:
                return i
        return -1

    def entry_count(self) -> int:
        return len([e for e in self.entries if not e.is_omitted])

    def to_dict(self) -> dict:
        entries_out: list[dict] = []
        for e in self.entries:
            d = asdict(e)
            if e.rate and not e.is_omitted:
                d["description"] = _join_rate_into_desc(e.description, e.rate)
            entries_out.append(d)
        return {
            "schedule_id": self.schedule_id,
            "rate_pct": self.rate_pct,
            "entries": entries_out,
        }


class RateMaterializer:
    """Apply rate amendment events to schedule state."""

    def __init__(self, base_notification: dict):
        """Initialize from a parsed base notification JSON."""
        self.notification_id = base_notification.get("notification_id", "")
        self.instrument_type = base_notification.get("instrument_type", "")
        self.schedules: dict[str, RateScheduleState] = {}

        for sid, sched_data in base_notification.get("schedules", {}).items():
            entries = []
            for e in sched_data.get("entries", []):
                entries.append(ScheduleEntry(
                    sno=e.get("sno", ""),
                    tariff_item=e.get("tariff_item", ""),
                    description=e.get("description", ""),
                    is_omitted=e.get("is_omitted", False),
                    rate=e.get("rate", ""),
                ))
            self.schedules[sid] = RateScheduleState(
                schedule_id=sid,
                rate_pct=sched_data.get("rate_pct", 0.0),
                entries=entries,
            )

        self.applied_events: list[dict] = []
        self.failed_events: list[dict] = []
        self.skipped_events: list[dict] = []
        self.effective_events: list[dict] = []

    # ── public API ───────────────────────────────────────────────────────

    def apply_events(self, events: list[dict], verbose: bool = False, lenient: bool = True) -> None:
        """Apply a list of amendment events in order.

        In lenient mode (default), failures are logged but don't block
        subsequent events.  Missing targets are handled gracefully.
        Tracks effective vs skipped events for audit.
        Uses a retry queue to mitigate cascade failures where INSERT events
        that fail prevent subsequent SUBSTITUTE/OMIT events from finding targets.
        Pre-sorts INSERT events within the same notification+schedule by S.No.
        to ensure correct ordering of alpha-suffixed insertions.
        """
        events = self._pre_sort_insert_events(events)

        retry_queue: list[dict] = []
        for evt in events:
            try:
                was_effective = self._apply_event(evt, lenient=lenient)
                self.applied_events.append(evt)
                if was_effective:
                    self.effective_events.append(evt)
                else:
                    retry_queue.append(evt)
            except Exception as e:
                evt_copy = dict(evt)
                evt_copy["_error"] = str(e)
                self.failed_events.append(evt_copy)
                if verbose:
                    print(f"  WARNING: {evt.get('operation')} failed: {e}")

        retry_queue.sort(key=self._retry_sort_key)

        insert_retry: list[dict] = []
        for evt in retry_queue:
            try:
                was_effective = self._apply_event(evt, lenient=lenient)
                if was_effective:
                    self.effective_events.append(evt)
                elif evt.get("operation") == "RATE_INSERT_ENTRIES":
                    insert_retry.append(evt)
                else:
                    self.skipped_events.append(evt)
            except Exception:
                if evt.get("operation") == "RATE_INSERT_ENTRIES":
                    insert_retry.append(evt)
                else:
                    self.skipped_events.append(evt)

        for evt in insert_retry:
            try:
                was_effective = self._apply_insert_with_relaxed_anchor(evt)
                if was_effective:
                    self.effective_events.append(evt)
                else:
                    self.skipped_events.append(evt)
            except Exception:
                self.skipped_events.append(evt)

    @staticmethod
    def _retry_sort_key(evt: dict) -> tuple:
        """Sort retry queue: non-INSERT events first, then INSERT by S.No."""
        op = evt.get("operation", "")
        if op == "RATE_INSERT_ENTRIES":
            entries = evt.get("payload", {}).get("entries", [])
            first_sno = entries[0].get("sno", "0") if entries and isinstance(entries[0], dict) else "0"
            m = re.match(r"(\d+)", str(first_sno))
            sno_num = int(m.group(1)) if m else 99999
            return (1, sno_num)
        return (0, 0)

    def _pre_sort_insert_events(self, events: list[dict]) -> list[dict]:
        """Sort INSERT events within same notification+schedule by first entry S.No.

        Groups consecutive INSERT events from the same notification+schedule
        and reorders them so that lower S.Nos are applied before higher ones.
        This prevents cascade failures where e.g. 29B is inserted before 29A
        and 29A's anchor (29) doesn't exist yet.
        """
        result: list[dict] = []
        i = 0
        while i < len(events):
            evt = events[i]
            if evt.get("operation") != "RATE_INSERT_ENTRIES":
                result.append(evt)
                i += 1
                continue

            group: list[dict] = [evt]
            key = (evt.get("source_notification", ""), evt.get("target_schedule", ""))
            j = i + 1
            while j < len(events):
                next_evt = events[j]
                if next_evt.get("operation") == "RATE_INSERT_ENTRIES":
                    next_key = (next_evt.get("source_notification", ""), next_evt.get("target_schedule", ""))
                    if next_key == key:
                        group.append(next_evt)
                        j += 1
                        continue
                break

            if len(group) > 1:
                group.sort(key=lambda e: self._sno_to_num(
                    e.get("payload", {}).get("entries", [{}])[0].get("sno", "999") if
                    e.get("payload", {}).get("entries") else "999"
                ))
            result.extend(group)
            i = j

        return result

    def _apply_insert_with_relaxed_anchor(self, evt: dict) -> bool:
        """Try INSERT with progressively relaxed anchor S.No."""
        payload = evt.get("payload", {})
        after_sno = str(payload.get("after_sno", "") or "")
        entries_data = payload.get("entries", [])
        if not entries_data:
            return False

        first_sno = str(entries_data[0].get("sno", "")) if isinstance(entries_data[0], dict) else ""
        m = re.match(r"(\d+)([A-Z]?)", first_sno)
        if not m:
            return False
        target_num = int(m.group(1))

        sid = evt.get("target_schedule", "")
        sched = self.schedules.get(sid)
        if not sched:
            for s in self.schedules.values():
                if any(s.find_entry(e.get("sno", "")) for e in entries_data if isinstance(e, dict)):
                    sched = s
                    break
            if not sched:
                return False

        for decrement in range(1, min(target_num, 20)):
            try_sno = str(target_num - decrement)
            if sched.find_entry(try_sno):
                pos = sched.find_position(try_sno)
                if pos >= 0:
                    new_entries = []
                    for e in entries_data:
                        if isinstance(e, dict):
                            new_entries.append(ScheduleEntry(
                                sno=str(e.get("sno", "")),
                                tariff_item=str(e.get("tariff_item", "")),
                                description=str(e.get("description", "")),
                            ))
                    for i, entry in enumerate(new_entries):
                        sched.entries.insert(pos + 1 + i, entry)
                    return True
        return False

    def get_schedule_at(self, schedule_id: str) -> Optional[RateScheduleState]:
        return self.schedules.get(schedule_id)

    def snapshot(self) -> dict:
        """Take a snapshot of the current schedule state."""
        return {
            "notification_id": self.notification_id,
            "schedules": {sid: s.to_dict() for sid, s in self.schedules.items()},
            "total_entries": sum(s.entry_count() for s in self.schedules.values()),
        }

    def summary(self) -> str:
        parts = [f"Materializer for {self.notification_id}:"]
        for sid, sched in sorted(self.schedules.items()):
            parts.append(f"  Schedule {sid} ({sched.rate_pct}%): {sched.entry_count()} active entries, {len(sched.entries)} total")
        parts.append(f"  Applied: {len(self.applied_events)}, Failed: {len(self.failed_events)}")
        return "\n".join(parts)

    # ── event handlers ───────────────────────────────────────────────────

    def _get_schedule(self, evt: dict) -> RateScheduleState:
        sid = evt.get("target_schedule", "")
        sched = self.schedules.get(sid)
        if not sched:
            if len(self.schedules) == 1:
                sched = next(iter(self.schedules.values()))
            if not sched:
                raise ValueError(f"Schedule '{sid}' not found")
        return sched

    def _find_entry_any_schedule(self, sno: str, evt: dict) -> Optional[ScheduleEntry]:
        """Search all schedules for an entry with this S.No.

        Only used as a fallback when target_schedule is empty or the entry
        genuinely can't be found in the specified schedule.
        """
        target_sched = evt.get("target_schedule", "")
        if target_sched:
            return None
        for sid, sched in self.schedules.items():
            entry = sched.find_entry(sno)
            if entry:
                return entry
        return None

    def _apply_event(self, evt: dict, lenient: bool = True) -> bool:
        """Apply a single event. Returns True if the event had an effect."""
        op = evt.get("operation", "")
        handler = getattr(self, f"_op_{op.lower()}", None)
        if handler:
            try:
                result = handler(evt)
                return result if isinstance(result, bool) else True
            except ValueError as e:
                if lenient and "not found" in str(e):
                    return False
                raise
        else:
            if not lenient:
                raise ValueError(f"Unknown operation: {op}")
            return False

    def _op_rate_omit_entries(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno_list = payload.get("sno_list", [])
        any_found = False
        for sno in sno_list:
            entry = sched.find_entry(str(sno))
            if entry:
                entry.is_omitted = True
                entry.tariff_item = ""
                entry.description = "Omitted"
                any_found = True
        if not any_found:
            raise ValueError(f"S.No. {sno_list} not found in schedule {sched.schedule_id}")
        return True

    def _op_rate_insert_entries(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        new_entries_data = payload.get("entries", [])
        after_sno = str(payload.get("after_sno", "") or "")
        before_sno = str(payload.get("before_sno", "") or "")

        new_entries = []
        for e in new_entries_data:
            if isinstance(e, dict):
                new_entries.append(ScheduleEntry(
                    sno=str(e.get("sno", "")),
                    tariff_item=str(e.get("tariff_item", "")),
                    description=str(e.get("description", "")),
                    rate=str(e.get("rate", "")),
                ))
            elif isinstance(e, str):
                new_entries.append(ScheduleEntry(sno=e, tariff_item="", description=""))
            elif isinstance(e, (list, tuple)) and len(e) >= 3:
                new_entries.append(ScheduleEntry(sno=str(e[0]), tariff_item=str(e[1]), description=str(e[2])))

        if after_sno:
            pos = sched.find_position(after_sno)
            if pos < 0:
                found_sched = None
                for s in self.schedules.values():
                    if s.schedule_id == sched.schedule_id:
                        continue
                    p = s.find_position(after_sno)
                    if p >= 0:
                        found_sched = s
                        pos = p
                        break
                if found_sched:
                    first_tariff = str(new_entries[0].tariff_item[:4]) if new_entries and new_entries[0].tariff_item else ""
                    if first_tariff and first_tariff.isdigit():
                        has_match = any(
                            e.tariff_item.startswith(first_tariff)
                            for e in found_sched.entries[:20]
                        )
                        if not has_match:
                            found_sched = None
                            pos = -1
                    if found_sched:
                        sched = found_sched
                    else:
                        pos = self._find_insert_position_by_sno(sched, new_entries[0].sno if new_entries else "")
                else:
                    pos = self._find_insert_position_by_sno(sched, new_entries[0].sno if new_entries else "")
                if pos < 0:
                    sched.entries.extend(new_entries)
                    return True
            for i, entry in enumerate(new_entries):
                sched.entries.insert(pos + 1 + i, entry)
            return True
        elif before_sno:
            pos = sched.find_position(before_sno)
            if pos < 0:
                pos = self._find_insert_position_by_sno(sched, new_entries[0].sno if new_entries else "")
                if pos < 0:
                    sched.entries.extend(new_entries)
                    return True
            for i, entry in enumerate(new_entries):
                sched.entries.insert(pos + i, entry)
            return True
        else:
            pos = self._find_insert_position_by_sno(sched, new_entries[0].sno if new_entries else "")
            if pos >= 0:
                for i, entry in enumerate(new_entries):
                    sched.entries.insert(pos + i, entry)
            else:
                sched.entries.extend(new_entries)
            return True

    def _find_insert_position_by_sno(self, sched: RateScheduleState, sno: str) -> int:
        """Find the correct position to insert an entry based on S.No. ordering."""
        if not sno:
            return -1
        target_num = self._sno_to_num(sno)
        for i, entry in enumerate(sched.entries):
            entry_num = self._sno_to_num(entry.sno)
            if entry_num > target_num:
                return i
        return -1

    @staticmethod
    def _sno_to_num(sno: str) -> float:
        m = re.match(r"(\d+)([A-Z]?)", sno.strip().upper())
        if not m:
            return 99999
        num = int(m.group(1))
        letter = m.group(2)
        return num + (ord(letter) - ord('A') + 1) * 0.1 if letter else float(num)

    @staticmethod
    def _fuzzy_find_in_desc(desc: str, target: str, threshold: float = FUZZY_MATCH_THRESHOLD) -> tuple[int, int] | None:
        """Find the best fuzzy match of *target* in *desc*.

        Returns (start, end) indices if a match above threshold is found, else None.
        """
        if not target or len(target) < 3 or not desc:
            return None
        desc_low = desc.lower()
        target_low = target.lower()

        idx = desc_low.find(target_low)
        if idx >= 0:
            return idx, idx + len(target_low)

        tlen = len(target_low)
        dlen = len(desc_low)
        best_ratio = 0.0
        best_span: tuple[int, int] | None = None
        step = max(1, tlen // 3)
        window = max(tlen, int(tlen * 1.3))

        for i in range(0, max(dlen - 3, 0), step):
            end = min(i + window, dlen)
            chunk = desc_low[i:end]
            ratio = SequenceMatcher(None, target_low, chunk).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_span = (i, end)

        if best_ratio >= threshold and best_span:
            return best_span

        return None

    def _substitute_item_in_entry(self, entry, item_id: str, new_text: str) -> bool:
        """Surgically replace a single sub-item within an entry description.

        Shared by RATE_SUBSTITUTE_ITEM and RATE_SUBSTITUTE_COLUMN (when the
        column-3 substitution targets a single item). The sub-item marker
        ``item_id`` (e.g. ``(iii)``) is located in the description and the
        span up to the next sequential item marker is replaced with
        ``new_text``. Sibling sub-items are left intact.
        """
        desc = entry.description
        idx = desc.find(item_id)
        if idx < 0:
            # The sub-item marker is not in the description — most likely a
            # prior event destroyed the multi-item structure, OR the item is
            # being newly introduced. When the description is non-empty,
            # append the new item rather than overwriting the whole entry so
            # existing sibling items are preserved (cf. 27/2018 S.No 17 item
            # (viii)). When the description was wiped, set it directly.
            if not new_text:
                return False
            if desc.strip():
                sep = " " if (desc[-1] != " " and new_text[:1] != " ") else ""
                entry.description = desc.rstrip() + sep + new_text.lstrip()
            else:
                entry.description = new_text
            return True

        end_idx = len(desc)
        nxt = _next_item_marker(item_id)
        if nxt:
            nxt_idx = desc.find(nxt, idx + len(item_id))
            if nxt_idx >= 0:
                end_idx = nxt_idx

        entry.description = desc[:idx] + new_text + desc[end_idx:]
        return True

    def _op_rate_substitute_column(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = str(payload.get("sno", ""))
        col = payload.get("column", 0)
        new_val = str(payload.get("new_value", ""))

        # Skip empty substitutions — they destroy data without adding anything.
        # The compiler sometimes captures empty quoted text for complex clauses.
        if not new_val.strip():
            return False

        entry = sched.find_entry(sno)
        if not entry:
            entry = self._find_entry_any_schedule(sno, evt)
            if not entry:
                raise ValueError(f"S.No. {sno} not found")
        if col == 2:
            entry.tariff_item = new_val
        elif col == 3:
            # When the substitution targets a single item within the
            # column-3 description (services schedule: "for item (i) and the
            # entries relating thereto in columns (3), (4) and (5)"), replace
            # only that sub-item instead of clobbering the whole description
            # and destroying sibling items. When both ``item`` and
            # ``sub_item`` are present the operative target is the sub-item
            # marker (e.g. ``(c)`` under item ``(i)``).
            sub_item = str(payload.get("sub_item", "") or "")
            item = str(payload.get("item", "") or "")
            leaf = sub_item or item
            if leaf:
                item_id = f"({leaf})" if not leaf.startswith("(") else leaf
                cleaned = re.sub(
                    r'\s+\d+\.?\d*\s+(?:Provided|None|Nil)\b.*$', '',
                    new_val, flags=re.I | re.DOTALL,
                )
                cleaned = re.sub(r'\s+\d+\.?\d*$', '', cleaned)
                return self._substitute_item_in_entry(entry, item_id, cleaned.strip() or new_val)
            entry.description = new_val
        elif col == 4:
            # Compensation Cess per-entry rate column.
            entry.rate = new_val
        return True

    def _op_rate_substitute_item(self, evt: dict) -> bool:
        """Replace a single sub-item within a multi-item entry description.

        Unlike RATE_SUBSTITUTE_COLUMN (which replaces the *entire* column-3
        description), this surgically replaces only the targeted sub-item —
        e.g. ``(iii) ...`` — leaving sibling sub-items ``(i)``, ``(ii)``,
        ``(iv)`` intact. The end of the sub-item is the next sequential item
        marker (``(iv)`` after ``(iii)``) when present, otherwise the end of
        the description.
        """
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = str(payload.get("sno", ""))
        item_id = str(payload.get("item_id", ""))  # e.g. "(iii)"
        new_text = str(payload.get("new_text", ""))

        entry = sched.find_entry(sno)
        if not entry:
            entry = self._find_entry_any_schedule(sno, evt)
            if not entry:
                raise ValueError(f"S.No. {sno} not found")

        # Strip trailing rate + condition text that leaked from the PDF table.
        cleaned = re.sub(r'\s+\d+\.?\d*\s+(?:Provided|None|Nil)\b.*$', '', new_text, flags=re.I | re.DOTALL)
        # Also strip bare trailing rate like " 2.5" or " 6"
        cleaned = re.sub(r'\s+\d+\.?\d*$', '', cleaned)
        if cleaned.strip():
            new_text = cleaned

        return self._substitute_item_in_entry(entry, item_id, new_text)

    def _op_rate_substitute_row(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = str(payload.get("sno", ""))
        sno_list = payload.get("sno_list", [])
        new_entries_data = payload.get("new_entries", payload.get("entries", []))

        if sno_list and len(sno_list) > 1:
            positions = set()
            for s in sno_list:
                p = sched.find_position(str(s))
                if p < 0:
                    for other in self.schedules.values():
                        if other.schedule_id == sched.schedule_id:
                            continue
                        p2 = other.find_position(str(s))
                        if p2 >= 0:
                            sched = other
                            p = p2
                            break
                if p >= 0:
                    positions.add(p)
            if positions:
                first_pos = min(positions)
                sched.entries = [e for i, e in enumerate(sched.entries) if i not in positions]
            else:
                first_pos = self._find_insert_position_by_sno(
                    sched, str(new_entries_data[0].get("sno", "")) if new_entries_data and isinstance(new_entries_data[0], dict) else "")
                if first_pos < 0:
                    first_pos = len(sched.entries)
        else:
            pos = sched.find_position(sno)
            if pos < 0:
                found_sched = None
                for s in self.schedules.values():
                    if s.schedule_id == sched.schedule_id:
                        continue
                    p = s.find_position(sno)
                    if p >= 0:
                        found_sched = s
                        pos = p
                        break
                if not found_sched:
                    raise ValueError(f"S.No. {sno} not found")
                sched = found_sched
            first_pos = pos
            sched.entries.pop(first_pos)

        for i, e in enumerate(new_entries_data):
            if isinstance(e, dict):
                tariff = str(e.get("tariff_item", ""))
                desc = str(e.get("description", ""))
                # Extract leaked Heading/Chapter/Section prefix from description
                if not tariff and desc:
                    m = re.match(r"^(Heading|Chapter|Section)\s+(\d[\d\s]*?)(?=\s*\(|\s+[A-Z]|\s*$)", desc, re.I)
                    if m:
                        tariff = m.group(1).capitalize() + " " + m.group(2).strip()
                        desc = desc[m.end():].lstrip()
                sched.entries.insert(first_pos + i, ScheduleEntry(
                    sno=str(e.get("sno", "")),
                    tariff_item=tariff,
                    description=desc,
                    rate=str(e.get("rate", "")),
                ))
            elif isinstance(e, (list, tuple)) and len(e) >= 3:
                sched.entries.insert(first_pos + i, ScheduleEntry(sno=str(e[0]), tariff_item=str(e[1]), description=str(e[2])))
        return True

    def _op_rate_insert_words(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = str(payload.get("sno", ""))
        after_words = str(payload.get("after_words", ""))
        insert_words = str(payload.get("insert_words", ""))

        entry = sched.find_entry(sno)
        if not entry:
            entry = self._find_entry_any_schedule(sno, evt)
            if not entry:
                raise ValueError(f"S.No. {sno} not found")

        desc = entry.description
        if not after_words or len(after_words) < 3:
            return False

        best_idx = -1
        for m in re.finditer(re.escape(after_words), desc, re.I):
            idx = m.start()
            before_ok = idx == 0 or not desc[idx-1].isalnum()
            after_ok = idx + len(after_words) >= len(desc) or not desc[idx + len(after_words)].isalnum()
            if before_ok and after_ok:
                best_idx = idx
                break

        if best_idx >= 0:
            insert_pos = best_idx + len(after_words)
            entry.description = desc[:insert_pos] + " " + insert_words + desc[insert_pos:]
            return True

        # Fallback: after_words may be in an alpha-suffixed entry (e.g., 100A)
        # inserted after the target S.No by an earlier notification
        for suffix in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            nearby = sched.find_entry(sno + suffix)
            if nearby and nearby is not entry:
                desc2 = nearby.description
                for m2 in re.finditer(re.escape(after_words), desc2, re.I):
                    idx2 = m2.start()
                    insert_pos = idx2 + len(after_words)
                    nearby.description = desc2[:insert_pos] + " " + insert_words + desc2[insert_pos:]
                    return True

        return False

    def _op_rate_omit_words(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = str(payload.get("sno", ""))
        words = str(payload.get("words", ""))

        entry = sched.find_entry(sno)
        if not entry:
            entry = self._find_entry_any_schedule(sno, evt)
            if not entry:
                raise ValueError(f"S.No. {sno} not found")

        desc = entry.description
        idx = desc.lower().find(words.lower())
        if idx >= 0:
            end = idx + len(words)
            while end < len(desc) and desc[end] in " ,":
                end += 1
            entry.description = self._join_omit(desc[:idx].rstrip(), desc[end:])
        else:
            span = self._fuzzy_find_in_desc(desc, words)
            if span:
                end = span[1]
                while end < len(desc) and desc[end] in " ,":
                    end += 1
                entry.description = self._join_omit(desc[:span[0]].rstrip(), desc[end:])
            else:
                raise ValueError(f"Words '{words}' not found in description of S.No. {sno}")
        return True

    @staticmethod
    def _join_omit(left: str, right: str) -> str:
        # Removing the omitted span can leave two word characters directly
        # adjacent (e.g. "services," + " without" → "serviceswithout" when the
        # comma and surrounding spaces are consumed). Insert a single space so
        # distinct words are not merged.
        if left and right and left[-1].isalnum() and right[0].isalnum():
            return left + " " + right
        return left + right

    def _op_rate_substitute_words(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = str(payload.get("sno", ""))
        old_words = str(payload.get("old_words", ""))
        new_words = str(payload.get("new_words", ""))

        entry = sched.find_entry(sno)
        if not entry:
            entry = self._find_entry_any_schedule(sno, evt)
            if not entry:
                raise ValueError(f"S.No. {sno} not found")

        desc = entry.description
        idx = desc.lower().find(old_words.lower())
        if idx >= 0:
            end = idx + len(old_words)
            entry.description = desc[:idx] + new_words + desc[end:]
        else:
            span = self._fuzzy_find_in_desc(desc, old_words)
            if span:
                entry.description = desc[:span[0]] + new_words + desc[span[1]:]
            else:
                raise ValueError(f"Words '{old_words}' not found in description of S.No. {sno}")
        return True

    def _op_rate_batch_substitute_words(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno_list = payload.get("sno_list", [])
        old_words = str(payload.get("old_words", ""))
        new_words = str(payload.get("new_words", ""))
        match_any = payload.get("match_any", False)
        any_changed = False

        if match_any:
            for entry in sched.entries:
                desc = entry.description
                idx = desc.lower().find(old_words.lower())
                if idx >= 0:
                    end = idx + len(old_words)
                    entry.description = desc[:idx] + new_words + desc[end:]
                    any_changed = True
                else:
                    span = self._fuzzy_find_in_desc(desc, old_words)
                    if span:
                        entry.description = desc[:span[0]] + new_words + desc[span[1]:]
                        any_changed = True
            return any_changed

        for sno in sno_list:
            entry = sched.find_entry(sno)
            if not entry:
                continue
            desc = entry.description
            idx = desc.lower().find(old_words.lower())
            if idx >= 0:
                end = idx + len(old_words)
                entry.description = desc[:idx] + new_words + desc[end:]
                any_changed = True
            else:
                span = self._fuzzy_find_in_desc(desc, old_words)
                if span:
                    entry.description = desc[:span[0]] + new_words + desc[span[1]:]
                    any_changed = True
        return any_changed

    def _op_rate_renumber(self, evt: dict) -> bool:
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        old_sno = payload.get("old_sno", "")
        new_sno = payload.get("new_sno", "")

        entry = sched.find_entry(old_sno)
        if not entry:
            entry = self._find_entry_any_schedule(old_sno, evt)
        if entry:
            entry.sno = new_sno + ("." if not new_sno.endswith(".") else "")
        else:
            raise ValueError(f"S.No. {old_sno} not found for renumber")
        return True

    def _op_rate_amend_opening(self, evt: dict) -> bool:
        return True

    def _op_rate_substitute_portion(self, evt: dict) -> bool:
        """Replace a portion of description between begin/end words."""
        sched = self._get_schedule(evt)
        payload = evt.get("payload", {})
        sno = payload.get("sno", "")
        sno_list = payload.get("sno_list", [sno])
        begin_words = payload.get("begin_words", "")
        end_words = payload.get("end_words", "")
        new_words = payload.get("new_words", "")
        any_found = False

        for sn in sno_list:
            entry = sched.find_entry(sn)
            if not entry:
                entry = self._find_entry_any_schedule(sn, evt)
                if not entry:
                    continue
            any_found = True
            desc = entry.description
            begin_idx = desc.lower().find(begin_words.lower())
            if begin_idx < 0:
                span = self._fuzzy_find_in_desc(desc, begin_words)
                begin_idx = span[0] if span else -1
            if begin_idx < 0:
                continue
            search_from = begin_idx + len(begin_words)
            end_idx = desc.lower().find(end_words.lower(), search_from)
            if end_idx < 0:
                if not end_words or len(end_words) < 5:
                    end_idx = len(desc)
                else:
                    span = self._fuzzy_find_in_desc(desc[search_from:], end_words)
                    end_idx = search_from + span[0] if span else len(desc)
            if end_idx < 0:
                end_idx = len(desc)
            end_idx += len(end_words)
            entry.description = desc[:begin_idx] + new_words + desc[end_idx:]
        return any_found

    def _op_rate_unknown(self, evt: dict) -> bool:
        return False

    def _op_rate_skip(self, evt: dict) -> bool:
        return False

    def _op_rate_supersede(self, evt: dict) -> bool:
        return True


# ── supersession map ─────────────────────────────────────────────────────────

SUPERSESSION_MAP: dict[str, tuple[str, str, str]] = {
    # target → (superseding_notification, base_json, effective_date)
    "1/2017-ct-rate": ("9/2025-ct-rate", "derived/version_history/rate-schedules/base_9-2025.json", "2025-09-22"),
    "2/2017-ct-rate": ("10/2025-ct-rate", "derived/version_history/rate-schedules/base_10-2025.json", "2025-09-22"),
}

BASE_JSON_MAP: dict[str, str] = {
    "1/2017-ct-rate": "derived/version_history/rate-schedules/base_1-2017.json",
    "2/2017-ct-rate": "derived/version_history/rate-schedules/base_2-2017.json",
    "9/2025-ct-rate": "derived/version_history/rate-schedules/base_9-2025.json",
    "10/2025-ct-rate": "derived/version_history/rate-schedules/base_10-2025.json",
    "14/2025-ct-rate": "derived/version_history/rate-schedules/base_14-2025.json",
    "2/2022-ct-rate": "derived/version_history/rate-schedules/base_2-2022.json",
    "11/2017-ct-rate": "derived/version_history/rate-schedules/base_11-2017-ct-rate.json",
    "1/2017-cc-rate": "derived/version_history/rate-schedules/base_cess_1-2017.json",
    "2/2017-cc-rate": "derived/version_history/rate-schedules/base_cess_2-2017.json",
    "1/2025-cc-rate": "derived/version_history/rate-schedules/base_cess_1-2025.json",
}

# Compensation Cess (Rate) targets read their amendment events from a separate
# JSONL produced by ``compile_all_cess_amendments``.
CESS_EVENTS_JSONL = "derived/version_history/rate-schedules/cess_amendment_events.jsonl"


def _checkpoint_filter_date(checkpoint_date: str | None) -> str | None:
    """Return the ISO date used for event filtering.

    Service checkpoint artifacts are named with a ``svc_`` prefix to avoid
    colliding with goods-rate checkpoints, but their internal effective date is
    still the trailing ``YYYY-MM-DD``.  Event filtering must use that ISO date;
    otherwise lexical comparison treats every numeric event date as earlier
    than ``svc_...`` and replays future service amendments.
    """
    if not checkpoint_date:
        return None
    text = str(checkpoint_date)
    match = re.fullmatch(r"svc_(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text


def _resolve_base_for_date(
    target_notification: str,
    checkpoint_date: str | None,
    base_json_path: str | Path | None = None,
) -> tuple[str, str, str]:
    """Resolve which base notification and JSON to use for a given checkpoint date.

    Returns: (notification_id, base_json_path, active_target)
    """
    if checkpoint_date and target_notification in SUPERSESSION_MAP:
        super_notif, super_base, super_date = SUPERSESSION_MAP[target_notification]
        if checkpoint_date >= super_date:
            return super_notif, super_base, super_notif

    if base_json_path:
        return target_notification, str(base_json_path), target_notification

    if target_notification in BASE_JSON_MAP:
        return target_notification, BASE_JSON_MAP[target_notification], target_notification

    return target_notification, str(base_json_path or ""), target_notification


# ── batch materialization ────────────────────────────────────────────────────

def materialize_schedule(
    base_json_path: str | Path,
    events_jsonl_path: str | Path,
    target_notification: str,
    output_path: str | Path | None = None,
    checkpoint_date: str | None = None,
) -> dict:
    """Materialize a schedule by replaying events up to checkpoint_date.

    Handles supersession: if the target notification was superseded before
    the checkpoint date, automatically switches to the superseding instrument.

    Args:
        base_json_path: Path to the base notification JSON
        events_jsonl_path: Path to rate_amendment_events.jsonl
        target_notification: e.g. "1/2017-ct-rate"
        output_path: If provided, save snapshot JSON
        checkpoint_date: If provided, only apply events up to this date

    Returns:
        Snapshot dict with the materialized schedule state.
    """
    filter_date = _checkpoint_filter_date(checkpoint_date)

    # Check for supersession
    active_notif, active_base, event_target = _resolve_base_for_date(
        target_notification, filter_date, base_json_path
    )

    # Compensation Cess targets read their amendment events from a separate
    # JSONL. Auto-route when the caller passed the default CT(Rate) events
    # file for a -cc-rate target.
    if target_notification.endswith("-cc-rate") and str(events_jsonl_path).endswith(
        "rate_amendment_events.jsonl"
    ):
        events_jsonl_path = CESS_EVENTS_JSONL

    with open(active_base) as f:
        base = json.load(f)

    # Load events for the active instrument only
    events = []
    with open(events_jsonl_path) as f:
        for line in f:
            evt = json.loads(line)
            evt_target = evt.get("target_notification", "")
            # Only apply events targeting the active notification
            if evt_target != event_target:
                continue
            if filter_date and evt.get("effective_date", "") > filter_date:
                continue
            # Skip supersession events themselves
            if evt.get("operation") == "RATE_SUPERSEDE":
                continue
            events.append(evt)

    # Sort by effective_date, then publication_date
    events.sort(key=lambda e: (e.get("effective_date", ""), e.get("publication_date", "")))

    mat = RateMaterializer(base)
    mat.apply_events(events, verbose=True)

    snapshot = mat.snapshot()
    snapshot["target_notification"] = target_notification
    snapshot["active_notification"] = active_notif
    snapshot["events_applied"] = len(mat.applied_events)
    snapshot["events_failed"] = len(mat.failed_events)
    snapshot["events_effective"] = len(mat.effective_events)
    snapshot["events_skipped"] = len(mat.skipped_events)
    if checkpoint_date:
        snapshot["checkpoint_date"] = checkpoint_date

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)

    return snapshot


if __name__ == "__main__":
    import sys

    base = sys.argv[1] if len(sys.argv) > 1 else "derived/version_history/rate-schedules/base_1-2017.json"
    events = sys.argv[2] if len(sys.argv) > 2 else "derived/version_history/rate-schedules/rate_amendment_events.jsonl"
    target = sys.argv[3] if len(sys.argv) > 3 else "1/2017-ct-rate"
    checkpoint = sys.argv[4] if len(sys.argv) > 4 else None

    result = materialize_schedule(base, events, target, checkpoint_date=checkpoint)
    print(f"Target: {target}")
    if checkpoint:
        print(f"Checkpoint: {checkpoint}")
    print(f"Events applied: {result['events_applied']}, failed: {result['events_failed']}")
    print(f"Total entries: {result['total_entries']}")
    for sid, sched in sorted(result["schedules"].items()):
        active = len([e for e in sched["entries"] if not e.get("is_omitted")])
        print(f"  Schedule {sid} ({sched['rate_pct']}%): {active} active, {len(sched['entries'])} total")
