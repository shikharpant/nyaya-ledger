"""Tests for the rate-change and law-change MCP tools.

Covers >=3 cases per tool: a happy path, a missing-data -> unresolved case, and
a temporal/edge case. Rate tests read the production rate-data root
(``derived/version_history/rate-schedules/``); law tests resolve through the
statute identity registry from the repo root (the production default).

These are deterministic retrieval tests -- no model calls, no MCP transport.
"""

from __future__ import annotations

import pytest

from src.legal_corpus.rate_law_mcp import (
    RateLawService,
    _hsn_matches,
    _normalize_hsn,
    _tariff_codes,
)

RULE_10 = "/in/union/rules/cgst-rules-2017/rule/10"


@pytest.fixture(scope="module")
def svc() -> RateLawService:
    return RateLawService()


# ───────────────────────────── HSN helpers ──────────────────────────────

def test_hsn_normalize_strips_punctuation_and_preserves_leading_zeros():
    assert _normalize_hsn("03 04") == "0304"
    assert _normalize_hsn(" 01011000 ") == "01011000"


def test_tariff_codes_handles_spaces_commas_chapters_and_rejects_junk():
    assert ("hsn", "24039910") in _tariff_codes("2403 99 10")
    assert ("hsn", "0303") in _tariff_codes("0303, 0304")
    assert ("chapter", "99") in _tariff_codes("Chapter 99")
    assert _tariff_codes("00]") == []  # bracket artifact rejected
    assert _hsn_matches("9901", "Chapter 99")
    assert not _hsn_matches("7777", "Chapter 99")


# ───────────────────────── get_rate_for_hsn ─────────────────────────

def test_get_rate_for_hsn_happy_path(svc):
    r = svc.get_rate_for_hsn("0303", "2024-01-01")
    assert r["result"] == "ok"
    assert r["matches"], "expected at least one rate entry for HSN 0303"
    m = r["matches"][0]
    assert "cgst" in m["rate_breakdown"] and "igst" in m["rate_breakdown"]
    assert r["source_refs"]


def test_get_rate_for_hsn_missing_returns_unresolved(svc):
    r = svc.get_rate_for_hsn("0001", "2024-01-01")  # chapter 00 does not exist
    assert r["result"] == "unresolved"
    assert r["matches"] == []
    assert r["unresolved_gaps"]


def test_get_rate_for_hsn_jurisdiction_filter_and_temporal(svc):
    # jurisdiction=cgst must restrict to the CGST schedule only
    cgst_only = svc.get_rate_for_hsn("0303", "2024-01-01", jurisdiction="cgst")
    assert cgst_only["result"] == "ok"
    assert all(m["notification"] == "1/2017-ct-rate" for m in cgst_only["matches"])
    # 6-digit specificity narrows the match set vs 4-digit
    broad = svc.get_rate_for_hsn("0303", "2024-01-01")
    narrow = svc.get_rate_for_hsn("030311", "2024-01-01")
    assert 0 < len(narrow["matches"]) <= len(broad["matches"])


def test_get_rate_for_hsn_distinguishes_rate_from_exemption(svc):
    # HSN 0303 (fish) is conditionally split: pre-packaged taxed at 2.5% under
    # 1/2017 (goods_rate), loose fish exempt under 2/2017 (goods_exempt). The
    # tool must label instrument_type/notification_kind so the two are not
    # confused as competing flat rates. Ground truth: Notification 2/2017-Central
    # Tax (Rate) is an exemption notification ("hereby exempts ... goods").
    r = svc.get_rate_for_hsn("0303", "2024-01-01")
    assert r["result"] == "ok"
    kinds = {m["notification_kind"] for m in r["matches"]}
    instrument_types = {m["instrument_type"] for m in r["matches"]}
    assert {"rate", "exemption"} <= kinds
    assert "goods_rate" in instrument_types and "goods_exempt" in instrument_types
    # the taxable entry carries the 2.5% CGST rate; the exempt one is 0%
    rate_entry = next(m for m in r["matches"] if m["notification_kind"] == "rate")
    assert rate_entry["rate_pct"] == 2.5
    exempt_entry = next(m for m in r["matches"] if m["notification_kind"] == "exemption")
    assert exempt_entry["rate_pct"] == 0.0
    assert r["coverage_warning"]  # conflict surfaced


# ───────────────────────── trace_rate_changes ─────────────────────────

def test_trace_rate_changes_happy_path(svc):
    r = svc.trace_rate_changes("0303", from_date="2017-01-01", to_date="2025-12-31")
    assert r["result"] == "ok"
    assert r["changes"], "expected at least one rate-change record"
    # changes are chronological by effective date
    dates = [c["effective_date"] for c in r["changes"] if c["effective_date"]]
    assert dates == sorted(dates)


def test_trace_rate_changes_missing_returns_unresolved(svc):
    # 8888 is absent across every schedule and every event-date checkpoint
    # (verified across all 48 amendment dates x 5 rate notifications).
    r = svc.trace_rate_changes("8888")
    assert r["result"] == "unresolved"
    assert r["changes"] == []


def test_trace_rate_changes_temporal_window_excludes_outside_events(svc):
    # A to_date before the schedule existed must yield no changes.
    r = svc.trace_rate_changes("0303", from_date="2000-01-01", to_date="2000-12-31")
    assert r["result"] == "unresolved"
    assert r["changes"] == []


# ───────────────────────── get_rate_conditions ─────────────────────────

def test_get_rate_conditions_happy_path(svc):
    r = svc.get_rate_conditions("11/2017-ct-rate::sno=3")
    assert r["result"] == "ok"
    cond = r["conditions"]
    assert cond["notification"].startswith("11/2017-ct-rate")
    assert cond["description"]  # construction-services text is substantial


def test_get_rate_conditions_bad_locator_returns_unresolved(svc):
    r = svc.get_rate_conditions("not-a-real-locator")
    assert r["result"] == "unresolved"
    assert r["unresolved_gaps"]


def test_get_rate_conditions_hsn_locator_resolves(svc):
    r = svc.get_rate_conditions("1/2017::hsn=0303", as_of_date="2024-01-01")
    assert r["result"] == "ok"
    assert r["conditions"]["tariff_item"]


# ───────────────────────── compare_rates ─────────────────────────

def test_compare_rates_happy_path(svc):
    r = svc.compare_rates(["0303", "0101"], "2024-01-01")
    assert r["result"] == "ok"
    assert set(r["comparison"].keys()) == {"0303", "0101"}
    assert r["comparison"]["0303"]["result"] == "ok"


def test_compare_rates_mixed_resolves_with_warning(svc):
    r = svc.compare_rates(["0303", "0001"], "2024-01-01")
    assert r["result"] == "ok"  # overall ok even if one HSN unresolved
    assert r["comparison"]["0001"]["result"] == "unresolved"
    assert r["coverage_warning"]  # warns that some HSNs unresolved
    assert r["unresolved_gaps"]


def test_compare_rates_empty_list_is_ok(svc):
    r = svc.compare_rates([], "2024-01-01")
    assert r["result"] == "ok"
    assert r["comparison"] == {}


# ───────────────────────── get_law_as_of ─────────────────────────

def test_get_law_as_of_happy_path(svc):
    r = svc.get_law_as_of(RULE_10, "2024-01-01")
    assert r["result"] == "ok"
    assert r["text"]
    assert r["version_id"]
    assert r["text_sha256"]
    assert r["source_refs"]


def test_get_law_as_of_before_baseline_returns_unresolved(svc):
    r = svc.get_law_as_of(RULE_10, "2010-01-01")
    assert r["result"] == "unresolved"
    assert r["unresolved_gaps"]
    assert r["text"] == ""


def test_get_law_as_of_unknown_component_returns_unresolved(svc):
    r = svc.get_law_as_of("/in/union/rules/cgst-rules-2017/rule/9999", "2024-01-01")
    assert r["result"] == "unresolved"


# ───────────────────────── trace_amendments ─────────────────────────

def test_trace_amendments_happy_path(svc):
    r = svc.trace_amendments(RULE_10)
    assert r["result"] == "ok"
    assert r["amendments"], "rule 10 has a real amendment history"
    a = r["amendments"][0]
    assert {"event_id", "effective_date", "operation"} <= set(a.keys())


def test_trace_amendments_unknown_component_unresolved(svc):
    r = svc.trace_amendments("/in/union/rules/cgst-rules-2017/rule/9999")
    assert r["result"] == "unresolved"
    assert r["amendments"] == []


def test_trace_amendments_unreviewed_separation(svc):
    r = svc.trace_amendments(RULE_10, include_unreviewed=True)
    assert "unreviewed_candidates" in r
    assert isinstance(r["unreviewed_candidates"], list)
    # unreviewed never bleeds into the verified list: ids stay disjoint
    rev = {a.get("event_id") for a in r["amendments"]}
    unv = {u.get("event_id") for u in r["unreviewed_candidates"]}
    assert rev.isdisjoint(unv)
    # a coverage_warning is attached whenever the unreviewed lane is requested
    assert r["coverage_warning"] is not None


# ───────────────────────── get_amendment_instrument ─────────────────────────

def test_get_amendment_instrument_happy_path(svc):
    r = svc.get_amendment_instrument(f"{RULE_10}@2024-01-01")
    assert r["result"] == "ok"
    inst_id = (r["instrument"] or {}).get("document_id", "")
    assert inst_id.startswith("/in/union/notifications/")  # a real corpus instrument


def test_get_amendment_instrument_unknown_unresolved(svc):
    r = svc.get_amendment_instrument("/in/union/rules/cgst-rules-2017/rule/9999@2024-01-01")
    assert r["result"] == "unresolved"


def test_get_amendment_instrument_no_date_picks_latest_real_doc(svc):
    # No '@date' suffix: should still resolve to a real corpus instrument.
    r = svc.get_amendment_instrument(RULE_10)
    assert r["result"] == "ok"
    assert (r["instrument"] or {}).get("document_id", "").startswith("/in/union/")


# ───────────────────────── get_commencement_chain ─────────────────────────

def test_get_commencement_chain_happy_path(svc):
    r = svc.get_commencement_chain(RULE_10, "2022-01-01")
    assert r["result"] == "ok"
    comm = r["commencement"]
    # enactment_date must be populated (the instrument notification date), not None.
    assert comm["enactment_date"] == "2017-06-19"
    assert comm["commencement_date"]
    assert "saving_clauses" in comm and "transition_provisions" in comm
    # The corpus stores notification dates; the tool must warn that the statutory
    # appointed date (CGST rules in force from 2017-07-01) may differ.
    assert r["coverage_warning"] and "notification date" in r["coverage_warning"]


def test_get_commencement_chain_unknown_unresolved(svc):
    r = svc.get_commencement_chain("/in/union/rules/cgst-rules-2017/rule/9999", "2022-01-01")
    assert r["result"] == "unresolved"


def test_get_commencement_chain_before_baseline_unresolved(svc):
    r = svc.get_commencement_chain(RULE_10, "2000-01-01")
    assert r["result"] == "unresolved"
    assert r["unresolved_gaps"]


# ───────────────────────── compare_law_versions ─────────────────────────

def test_compare_law_versions_happy_path_detects_change(svc):
    r = svc.compare_law_versions(RULE_10, "2020-01-01", "2025-01-01")
    assert r["result"] == "ok"
    # rule 10 changed between 2020 and 2025
    assert r["text_changed"] is True
    assert r["unified_diff"]


def test_compare_law_versions_unknown_unresolved(svc):
    r = svc.compare_law_versions("/in/union/rules/cgst-rules-2017/rule/9999", "2020-01-01", "2025-01-01")
    assert r["result"] == "unresolved"


def test_compare_law_versions_same_date_no_change(svc):
    r = svc.compare_law_versions(RULE_10, "2024-01-01", "2024-01-01")
    assert r["result"] == "ok"
    assert r["text_changed"] is False
    assert r["unified_diff"] == ""


# ───────────────────────── envelope contract ─────────────────────────

@pytest.mark.parametrize("method,args", [
    ("get_rate_for_hsn", ("0303", "2024-01-01")),
    ("get_law_as_of", (RULE_10, "2024-01-01")),
    ("trace_amendments", (RULE_10,)),
])
def test_every_envelope_has_required_fields(svc, method, args):
    r = getattr(svc, method)(*args)
    for field in ("result", "snapshot_id", "retrieved_at", "coverage_warning",
                  "unresolved_gaps", "source_refs"):
        assert field in r, f"{method} missing envelope field {field}"
    assert r["snapshot_id"].startswith("nyaya-vh-")
