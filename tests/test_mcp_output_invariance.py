"""Output invariance regression: optimized service must produce byte-identical legal outputs.

Compares sha256 of serialized outputs against golden fixtures captured before
the MCP latency optimization (Phase 1 fast resolver + Phase 2 caches). The
fixtures live in ``tests/fixtures/mcp_latency_baseline/`` and are the legal
oracle — they encode the exact text, provenance, amendment chains, and
verification verdicts the system produced at the locked-in baseline commit.

If this test fails, an optimization changed legal-truth output. Either fix the
regression or, if the change is intentional and legally verified, recapture the
fixture with a recorded decision.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legal_corpus.serving import NyayaToolService

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mcp_latency_baseline"


def _service_method_map(svc):
    return {
        "query_law_as_of_date": svc.query_law_as_of_date,
        "lookup_provision": svc.lookup_provision,
        "compare_versions": svc.compare_versions,
        "get_provision_timeline": svc.get_provision_timeline,
        "list_amendments": svc.list_amendments,
    }


@pytest.fixture(scope="module")
def svc():
    return NyayaToolService()


@pytest.fixture(scope="module")
def manifest():
    return json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))


def _golden_cases(manifest):
    if not manifest:
        pytest.skip("golden baseline manifest not found")
    return sorted(manifest.items())


def test_golden_fixture_set_is_present():
    """Guards against accidentally deleting the fixture directory."""
    manifest_path = FIXTURE_DIR / "MANIFEST.json"
    assert manifest_path.exists(), f"golden manifest missing at {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_labels = {
        "act_s16_2024", "act_s16_2017", "act_s174", "rule_r56", "rule_r21",
        "direct_lookup", "compare_s16", "timeline_s16", "amendments_s16",
        "notfound", "date_pre_baseline",
    }
    actual_labels = set(manifest.keys())
    missing = expected_labels - actual_labels
    assert not missing, f"golden baseline missing cases: {missing}"


def _load_golden_cases():
    manifest_path = FIXTURE_DIR / "MANIFEST.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return sorted(manifest.items())


@pytest.mark.parametrize("label,meta", _load_golden_cases(), ids=[c[0] for c in _load_golden_cases()])
def test_output_matches_golden(svc, label, meta):
    """Each golden case must produce byte-identical output (sha256 match)."""
    method_map = _service_method_map(svc)
    fn = method_map[meta["tool"]]
    result = fn(**meta["args"])
    serialized = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
    actual_sha = hashlib.sha256(serialized.encode()).hexdigest()
    assert actual_sha == meta["sha256"], (
        f"{label}: output changed.\n"
        f"  expected sha256: {meta['sha256']}\n"
        f"  actual   sha256: {actual_sha}\n"
        f"  If this change is intentional and legally verified, recapture the "
        f"fixture and update MANIFEST.json with a recorded decision."
    )
