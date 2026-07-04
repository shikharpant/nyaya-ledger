#!/usr/bin/env python3
"""Test MCP server as a live process via stdio transport.

Starts the server, sends JSON-RPC messages, verifies responses.
"""

import json
import subprocess
import sys
from pathlib import Path


def test_mcp_server():
    proc = subprocess.Popen(
        [sys.executable, "scripts/serve_mcp.py", "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv():
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)

    results = {"pass": 0, "fail": 0}

    def check(name, condition, detail=""):
        if condition:
            results["pass"] += 1
            print(f"  PASS {name}")
        else:
            results["fail"] += 1
            print(f"  FAIL {name}: {detail}")

    try:
        # 1. Initialize
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1.0"}}})
        resp = recv()
        check("initialize", resp and "result" in resp,
              f"got {resp}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # 2. List tools
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = recv()
        tools = [t["name"] for t in resp.get("result", {}).get("tools", [])]
        check("tools/list returns 15 tools", len(tools) == 15,
              f"got {len(tools)} tools: {tools}")
        check("has query_law_as_of_date", "query_law_as_of_date" in tools)
        check("has get_provision_as_of_date", "get_provision_as_of_date" in tools)

        # 3. Call get_provision_as_of_date
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "get_provision_as_of_date",
                         "arguments": {"canonical_id": "/in/union/acts/cgst-act-2017/section/16",
                                       "date": "2024-01-01"}}})
        resp = recv()
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("get_provision_as_of_date returns ok", result.get("status") in ("ok", "ok_with_gaps"),
                  f"status={result.get('status')}")
            check("has text", len(result.get("text", "")) > 100,
                  f"text_len={len(result.get('text', ''))}")
            check("has version_id", bool(result.get("version_id")))
            check("has amendments", "amendments" in result)
        else:
            check("get_provision_as_of_date content", False, f"no content: {resp}")

        # 4. Call query_law_as_of_date
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "query_law_as_of_date",
                         "arguments": {"act": "CGST Act", "section": "16", "date": "2018-01-01"}}})
        resp = recv()
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("query_law_as_of_date resolves", result.get("status") in ("ok", "ok_with_gaps", "not_found"),
                  f"status={result.get('status')}")
            if result.get("status") in ("ok", "ok_with_gaps"):
                check("resolved canonical_id", bool(result.get("resolved_canonical_id")))
        else:
            check("query_law_as_of_date content", False, f"no content: {resp}")

        # 5. Call list_amendments
        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "list_amendments",
                         "arguments": {"canonical_id": "/in/union/rules/cgst-rules-2017/rule/89"}}})
        resp = recv()
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("list_amendments returns >0", result.get("count", 0) > 0,
                  f"count={result.get('count')}")
        else:
            check("list_amendments content", False)

        # 6. Call lookup_provision (existing tool backward compat)
        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
              "params": {"name": "lookup_provision",
                         "arguments": {"canonical_id": "/in/union/rules/cgst-rules-2017/rule/10"}}})
        resp = recv()
        content = resp.get("result", {}).get("content", [])
        if content:
            result = json.loads(content[0]["text"])
            check("lookup_provision found", result.get("found") is True)
        else:
            check("lookup_provision content", False)

    finally:
        proc.terminate()
        proc.wait()

    total = results["pass"] + results["fail"]
    print(f"\nMCP SERVER LIVE TEST: {results['pass']}/{total} passed")
    return results["fail"] == 0


if __name__ == "__main__":
    success = test_mcp_server()
    sys.exit(0 if success else 1)
