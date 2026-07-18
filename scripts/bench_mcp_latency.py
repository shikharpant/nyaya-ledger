#!/usr/bin/env python3
"""MCP latency benchmark using the official Python MCP client.

Not curl. Measures result-arrival, not connection-close.

Six measurement modes (per optimization plan Phase 0):
  1. fresh-process first composed query (cold citation resolution)
  2. warm persistent-session p50/p95
  3. warm reconnect/new-session latency
  4. warmed concurrency at 1/4/8 clients
  5. cold concurrent burst (N sessions, first-call-each)
  6. full bench-set makespan (proxy for acquisition; pass --provision-file for real)

Records: git HEAD, python/mcp versions, transport, RSS, per-case timings.
Writes JSON to derived/bench/mcp_latency_<timestamp>.json.

Usage:
  python3 scripts/bench_mcp_latency.py --port 18097
  python3 scripts/bench_mcp_latency.py --port 18097 --modes 1,2  # subset
  python3 scripts/bench_mcp_latency.py --help
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

COMPOSED_QUERY = {
    "name": "query_law_as_of_date",
    "arguments": {"act": "CGST Act", "section": "16", "date": "2024-01-01"},
}
DIRECT_QUERY = {
    "name": "lookup_provision",
    "arguments": {"canonical_id": "/in/union/acts/cgst-act-2017/section/16", "include_text": True},
}
BENCH_SET = [
    ("act_s16_2024", "query_law_as_of_date", {"act": "CGST Act", "section": "16", "date": "2024-01-01"}),
    ("act_s174", "query_law_as_of_date", {"act": "CGST Act", "section": "174", "date": "2024-01-01"}),
    ("direct_lookup", "lookup_provision", {"canonical_id": "/in/union/acts/cgst-act-2017/section/16", "include_text": True}),
    ("compare_s16", "compare_versions", {"canonical_id": "/in/union/acts/cgst-act-2017/section/16", "from_date": "2017-07-01", "to_date": "2024-01-01"}),
    ("timeline_s16", "get_provision_timeline", {"canonical_id": "/in/union/acts/cgst-act-2017/section/16"}),
    ("amendments_s16", "list_amendments", {"canonical_id": "/in/union/acts/cgst-act-2017/section/16"}),
]


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()
    except Exception:
        return "unknown"


def rss_kb() -> int:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return 0


def mcp_version() -> str:
    try:
        import mcp
        return getattr(mcp, "__version__", "unknown")
    except Exception:
        return "not-installed"


@contextlib.asynccontextmanager
async def mcp_session(url: str):
    """Open an MCP client session over streamable-http."""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.client.session import ClientSession
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def time_call(session, tool_spec: dict) -> tuple[float, dict]:
    t0 = time.perf_counter()
    result = await session.call_tool(tool_spec["name"], tool_spec["arguments"])
    dt = (time.perf_counter() - t0) * 1000
    payload = {}
    for c in (result.content or []):
        if getattr(c, "type", None) == "text":
            try:
                payload = json.loads(c.text)
                break
            except Exception:
                payload = {"text": c.text}
    return dt, payload


def stats(samples_ms: list[float]) -> dict:
    if not samples_ms:
        return {"n": 0}
    s = sorted(samples_ms)
    n = len(s)
    return {
        "n": n,
        "min_ms": round(s[0], 2),
        "p50_ms": round(s[n // 2], 2),
        "p95_ms": round(s[int(n * 0.95)] if n > 1 else s[0], 2),
        "max_ms": round(s[-1], 2),
        "mean_ms": round(statistics.fmean(s), 2),
    }


async def mode1_fresh_process_first_call(url: str) -> dict:
    """Mode 1: brand-new session, very first composed query (cold citation resolution)."""
    t0 = time.perf_counter()
    async with mcp_session(url) as session:
        dt, _ = await time_call(session, COMPOSED_QUERY)
    wall = (time.perf_counter() - t0) * 1000
    return {"mode": "1_fresh_first_call", "first_call_ms": round(dt, 2), "wall_incl_session_ms": round(wall, 2)}


async def mode2_warm_persistent(url: str, calls: int) -> dict:
    """Mode 2: one session, repeated calls, p50/p95 of warm path."""
    samples = []
    async with mcp_session(url) as session:
        await time_call(session, COMPOSED_QUERY)  # warmup discarded
        for _ in range(calls):
            dt, _ = await time_call(session, COMPOSED_QUERY)
            samples.append(dt)
    return {"mode": "2_warm_persistent", "call": COMPOSED_QUERY, **stats(samples)}


async def mode3_warm_reconnect(url: str, sessions: int) -> dict:
    """Mode 3: new session each call, but server already warm."""
    samples = []
    for _ in range(sessions):
        async with mcp_session(url) as session:
            dt, _ = await time_call(session, COMPOSED_QUERY)
            samples.append(dt)
    return {"mode": "3_warm_reconnect", "sessions": sessions, **stats(samples)}


async def mode4_concurrency(url: str, clients: list[int]) -> dict:
    """Mode 4: warmed concurrency at 1/4/8 clients."""
    out = {"mode": "4_concurrency_sweep"}
    for n in clients:
        async def worker():
            async with mcp_session(url) as session:
                await time_call(session, COMPOSED_QUERY)  # warmup
                t0 = time.perf_counter()
                await time_call(session, COMPOSED_QUERY)
                return (time.perf_counter() - t0) * 1000
        samples = await asyncio.gather(*[worker() for _ in range(n)])
        out[f"c{n}"] = stats(samples)
    return out


async def mode5_cold_burst(url: str, n: int) -> dict:
    """Mode 5: N concurrent fresh sessions each making their FIRST call (cold-citation pressure)."""
    async def cold_worker():
        async with mcp_session(url) as session:
            dt, _ = await time_call(session, COMPOSED_QUERY)
            return dt
    t0 = time.perf_counter()
    samples = await asyncio.gather(*[cold_worker() for _ in range(n)])
    wall = (time.perf_counter() - t0) * 1000
    return {"mode": "5_cold_burst", "n": n, **stats(samples), "wall_ms": round(wall, 2)}


async def mode6_bench_makespan(url: str) -> dict:
    """Mode 6: full bench-set makespan (proxy for acquisition)."""
    async with mcp_session(url) as session:
        await time_call(session, COMPOSED_QUERY)  # warm server
        t0 = time.perf_counter()
        per_case = {}
        for label, name, args in BENCH_SET:
            dt, _ = await time_call(session, {"name": name, "arguments": args})
            per_case[label] = round(dt, 2)
        wall = (time.perf_counter() - t0) * 1000
    return {"mode": "6_bench_makespan", "wall_ms": round(wall, 2), "per_case_ms": per_case}


async def run_bench(url: str, modes: list[int], warm_calls: int, sessions: int, cold_n: int) -> dict:
    results = {}
    if 1 in modes:
        results["mode1"] = await mode1_fresh_process_first_call(url)
    if 2 in modes:
        results["mode2"] = await mode2_warm_persistent(url, warm_calls)
    if 3 in modes:
        results["mode3"] = await mode3_warm_reconnect(url, sessions)
    if 4 in modes:
        results["mode4"] = await mode4_concurrency(url, [1, 4, 8])
    if 5 in modes:
        results["mode5"] = await mode5_cold_burst(url, cold_n)
    if 6 in modes:
        results["mode6"] = await mode6_bench_makespan(url)
    return results


async def main_async(args) -> int:
    url = f"http://{args.host}:{args.port}/mcp"
    print(f"target: {url}")
    print(f"git HEAD: {git_head()}  python: {platform.python_version()}  mcp: {mcp_version()}")

    modes = [int(x) for x in args.modes.split(",")] if args.modes else [1, 2, 3, 4, 5, 6]

    t0 = time.perf_counter()
    results = await run_bench(url, modes, args.warm_calls, args.sessions, args.cold_n)
    total_wall = (time.perf_counter() - t0)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head(),
        "python_version": platform.python_version(),
        "mcp_version": mcp_version(),
        "transport": "streamable-http",
        "target_url": url,
        "rss_kb_at_end": rss_kb(),
        "total_bench_wall_s": round(total_wall, 2),
        "modes_requested": modes,
        "results": results,
    }
    out_dir = REPO / "derived" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"mcp_latency_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print(json.dumps(results, indent=2, default=str))
    print(f"\nreport: {out_path}  ({out_path.stat().st_size}B)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18097)
    p.add_argument("--modes", default="", help="comma list e.g. 1,2,4 (default: all)")
    p.add_argument("--warm-calls", type=int, default=20)
    p.add_argument("--sessions", type=int, default=10)
    p.add_argument("--cold-n", type=int, default=4)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
