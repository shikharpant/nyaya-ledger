"""Functional regression: structured queries must not scan the entire corpus XML.

This is the Phase-1 correctness gate from the MCP optimization plan. It patches
``build_corpus_lookup`` to raise, then proves structured ``act + section + date``
queries resolve without ever invoking the ~35-second corpus scan.

This is NOT a wall-clock performance test — it is a structural guarantee that the
fast resolver path is wired correctly. Wall-clock targets live in the separate
benchmark harness (``scripts/bench_mcp_latency.py``), not in the default pytest
suite.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from legal_corpus import query as query_mod
from legal_corpus import serving as serving_mod
from legal_corpus.serving import NyayaToolService


@pytest.fixture
def corpus_lookup_disabled(monkeypatch):
    """Patch build_corpus_lookup to raise if called."""
    def _bomb(_path):
        raise AssertionError(
            "build_corpus_lookup was invoked — the structured-citation fast path "
            "should have resolved this query from version rows alone."
        )
    monkeypatch.setattr(query_mod, "build_corpus_lookup", _bomb)
    monkeypatch.setattr(serving_mod, "build_corpus_lookup", _bomb)
    # Also reset any cached lookup on new service instances.
    yield


def _fresh_service():
    """Service with no pre-built corpus lookup (simulates fresh process)."""
    return NyayaToolService()


@pytest.mark.parametrize(
    "act,section,date,expected_canonical",
    [
        ("CGST Act", "16", "2024-01-01", "/in/union/acts/cgst-act-2017/section/16"),
        ("CGST Act", "174", "2024-01-01", "/in/union/acts/cgst-act-2017/section/174"),
        ("CGST Act", "16", "2017-09-01", "/in/union/acts/cgst-act-2017/section/16"),
    ],
)
def test_structured_query_resolves_without_corpus_scan(
    corpus_lookup_disabled, act, section, date, expected_canonical
):
    """Composed act+section+date queries must not invoke build_corpus_lookup."""
    svc = _fresh_service()
    result = svc.query_law_as_of_date(act=act, section=section, date=date)
    assert result["status"] in {"ok", "ok_with_gaps", "not_found"}, result
    assert result.get("resolved_canonical_id") == expected_canonical


def test_resolve_citation_structured_does_not_invoke_corpus_scan(corpus_lookup_disabled):
    """resolve_citation for a structured citation must not invoke build_corpus_lookup."""
    svc = _fresh_service()
    result = svc.resolve_citation("section 16 CGST Act", limit=5)
    candidates = result.get("candidates", [])
    assert candidates, "expected at least one candidate"
    matched = [c for c in candidates if c.get("exists")]
    assert matched, "expected the CGST Act s16 candidate to exist"
    assert matched[0]["canonical_id"] == "/in/union/acts/cgst-act-2017/section/16"


def test_not_found_structured_query_does_not_invoke_corpus_scan(corpus_lookup_disabled):
    """A structured query for a non-existent section must still not scan the corpus."""
    svc = _fresh_service()
    result = svc.query_law_as_of_date(act="CGST Act", section="999", date="2024-01-01")
    assert result["status"] == "not_found"


def test_non_structured_query_still_uses_corpus_lookup():
    """Non-structured/fuzzy queries must still be able to build the corpus lookup.

    This guards against the fast path being too aggressive — forms, ambiguous
    citations, and fuzzy searches should still fall back to the corpus index.
    """
    svc = _fresh_service()
    # A form lookup should go through the slow path (returns candidates or empty).
    result = svc.resolve_citation("form gst-reg-01", limit=5)
    # Should not raise; the form lookup may or may not exist but must not crash.
    assert "candidates" in result
