#!/usr/bin/env python3
"""Live smoke test for the Nyaya Ledger MCP stdio server.

Uses raw subprocess JSON-RPC over stdio — no MCP SDK session layer,
which avoids AnyIO/threading hangs in the default transport.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = [
    "lookup_provision", "semantic_search", "provision_search",
    "resolve_citation", "get_incoming_refs", "get_outgoing_refs",
    "trace_rule_to_act", "find_related_provisions",
    "explain_reference_path", "get_forms_for_rule",
    "compare_versions", "get_provision_as_of_date",
    "list_amendments", "get_provision_timeline",
    "query_law_as_of_date", "get_form_structure",
]


def test_mcp_live() -> int:
    print("Starting MCP live test...", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "scripts" / "serve_mcp.py"), "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(ROOT),
        bufsize=1,
    )
    print(f"Subprocess started: PID={proc.pid}", flush=True)

    msg_id = 0
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}: {detail}")

    def send_recv(msg: dict) -> dict:
        msg_id_val = msg.get("id", 0)
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line.strip())

    try:
        # 1. Initialize
        msg_id += 1
        resp = send_recv({
            "jsonrpc": "2.0", "id": msg_id, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1.0"}}
        })
        server_name = resp.get("result", {}).get("serverInfo", {}).get("name", "")
        check("initialize", server_name == "nyaya-ledger", f"got {server_name}")

        # notifications/initialized
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.flush()

        # 2. List tools
        msg_id += 1
        resp = send_recv({"jsonrpc": "2.0", "id": msg_id, "method": "tools/list", "params": {}})
        tools = [t["name"] for t in resp.get("result", {}).get("tools", [])]
        check(f"tools/list returns {len(EXPECTED_TOOLS)} tools", len(tools) >= len(EXPECTED_TOOLS),
              f"got {len(tools)}")
        for tname in EXPECTED_TOOLS:
            check(f"tool present: {tname}", tname in tools)

        # 3. Call get_provision_as_of_date
        msg_id += 1
        resp = send_recv({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": "get_provision_as_of_date",
                       "arguments": {"canonical_id": "/in/union/acts/cgst-act-2017/section/16", "date": "2024-01-01"}}
        })
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("get_provision_as_of_date returns ok", result.get("status") in ("ok", "ok_with_gaps"),
                  f"status={result.get('status')}")
            check("has text", len(result.get("text", "")) > 100)
            check("has version_id", bool(result.get("version_id")))
        else:
            check("get_provision_as_of_date content", False, str(resp))

        # 4. Call query_law_as_of_date
        msg_id += 1
        resp = send_recv({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": "query_law_as_of_date",
                       "arguments": {"act": "CGST Act", "section": "16", "date": "2018-01-01"}}
        })
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("query_law_as_of_date resolves", result.get("status") in ("ok", "ok_with_gaps", "not_found"),
                  f"status={result.get('status')}")
            if result.get("status") in ("ok", "ok_with_gaps"):
                check("resolved canonical_id", bool(result.get("resolved_canonical_id")))
        else:
            check("query_law_as_of_date content", False, str(resp))

        # 5. Call list_amendments
        msg_id += 1
        resp = send_recv({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": "list_amendments",
                       "arguments": {"canonical_id": "/in/union/rules/cgst-rules-2017/rule/89"}}
        })
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("list_amendments returns >0", result.get("count", 0) > 0)
        else:
            check("list_amendments content", False)

        # 6. Call lookup_provision
        msg_id += 1
        resp = send_recv({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": "lookup_provision",
                       "arguments": {"canonical_id": "/in/union/rules/cgst-rules-2017/rule/10"}}
        })
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("lookup_provision found", result.get("found") is True)
        else:
            check("lookup_provision content", False)

    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait()

    total = passed + failed
    print(f"\nMCP SERVER LIVE TEST: {passed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(test_mcp_live())
