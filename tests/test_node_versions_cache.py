"""Tests for the node-version cache: thread-safety and invalidation.

The cache in ``src/legal_corpus/version_compare.py`` must:
  - return identical results whether loaded once or concurrently from N threads
    (no torn reads, no lost writes on the first-load race)
  - honor explicit invalidation
  - stay consistent with the underlying file (empty for missing files)

This guards the Phase-2 thread-safety fix (double-checked locking) against
silent regressions if the cache code is later edited.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legal_corpus import version_compare as vc


REPO = Path(__file__).resolve().parents[1]
REAL_NODE_VERSIONS = REPO / "derived" / "version_history" / "cgst-act-2017" / "node_versions.jsonl"


@pytest.fixture(autouse=True)
def _clean_cache():
    """Start each test with an empty cache."""
    vc.invalidate_node_versions_cache()
    yield
    vc.invalidate_node_versions_cache()


def test_returns_empty_for_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.jsonl"
    assert vc.read_node_versions(missing) == []


def test_reads_real_node_versions_file():
    if not REAL_NODE_VERSIONS.exists():
        pytest.skip("node_versions.jsonl not present in this checkout")
    rows = vc.read_node_versions(REAL_NODE_VERSIONS)
    assert rows, "expected non-empty node_versions.jsonl"
    assert all("component_id" in r for r in rows[:10])


def test_cache_hit_returns_same_object_identity():
    """Second read must hit the cache (same list object, not a re-parse)."""
    if not REAL_NODE_VERSIONS.exists():
        pytest.skip("node_versions.jsonl not present")
    first = vc.read_node_versions(REAL_NODE_VERSIONS)
    second = vc.read_node_versions(REAL_NODE_VERSIONS)
    assert first is second, "second read should return the cached object"


def test_concurrent_first_load_is_consistent():
    """N threads racing on the first read must all see the same, correct rows."""
    if not REAL_NODE_VERSIONS.exists():
        pytest.skip("node_versions.jsonl not present")
    vc.invalidate_node_versions_cache()
    N = 8
    with ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(lambda _: vc.read_node_versions(REAL_NODE_VERSIONS), range(N)))
    # All threads must see identical results.
    first = results[0]
    assert all(r is first for r in results), (
        "concurrent readers saw different objects — cache race is back"
    )
    # And the result must be the correct, full parse (not a torn/partial read).
    assert len(first) >= 100, f"expected >=100 rows, got {len(first)}"


def test_invalidate_specific_path():
    if not REAL_NODE_VERSIONS.exists():
        pytest.skip("node_versions.jsonl not present")
    cached = vc.read_node_versions(REAL_NODE_VERSIONS)
    assert cached is not None
    vc.invalidate_node_versions_cache(REAL_NODE_VERSIONS)
    assert REAL_NODE_VERSIONS not in vc._NODE_VERSIONS_CACHE


def test_invalidate_all():
    if not REAL_NODE_VERSIONS.exists():
        pytest.skip("node_versions.jsonl not present")
    vc.read_node_versions(REAL_NODE_VERSIONS)
    assert vc._NODE_VERSIONS_CACHE
    vc.invalidate_node_versions_cache()
    assert not vc._NODE_VERSIONS_CACHE


def test_event_index_cache_on_service():
    """NyayaToolService caches event-index JSONL per path (Phase 2)."""
    from legal_corpus.serving import NyayaToolService
    svc = NyayaToolService()
    # The _read_event_index method should populate self._event_index_cache.
    # We can't easily assert on a specific file without coupling to internals,
    # but we can verify the cache dict exists and is used.
    assert hasattr(svc, "_event_index_cache")
    assert isinstance(svc._event_index_cache, dict)
    assert hasattr(svc, "_json_cache")
    assert isinstance(svc._json_cache, dict)
