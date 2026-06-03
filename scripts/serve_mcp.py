#!/usr/bin/env python3
"""Run the Nyaya Ledger MCP server."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.legal_corpus.serving import NyayaToolService  # noqa: E402


_SERVICE: NyayaToolService | None = None


def service() -> NyayaToolService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = NyayaToolService.from_env()
    return _SERVICE


def _optional(value: str) -> str | None:
    clean = value.strip()
    return clean or None


def create_mcp_server(*, host: str = "127.0.0.1", port: int = 8090) -> FastMCP:
    server = FastMCP(
        "nyaya-ledger",
        instructions=(
            "Tools for querying the Nyaya Ledger Indian legal corpus. "
            "The XML corpus is authoritative; FalkorDB and LanceDB are serving indexes."
        ),
        host=host,
        port=port,
    )

    @server.tool()
    def lookup_provision(canonical_id: str, include_text: bool = True) -> dict[str, Any]:
        """Fetch exact text and provenance for a section, rule, form, document, or notification."""
        return service().lookup_provision(canonical_id, include_text=include_text)

    @server.tool()
    def semantic_search(query: str, limit: int = 10, document_type: str = "", role: str = "") -> dict[str, Any]:
        """Search legal corpus chunks by meaning through LanceDB and the configured embedding endpoint."""
        return service().semantic_search(
            query,
            limit=limit,
            document_type=_optional(document_type),
            role=_optional(role),
        )

    @server.tool()
    def resolve_citation(citation: str, limit: int = 10) -> dict[str, Any]:
        """Convert a human citation such as 'section 128A CGST Act' into canonical ID candidates."""
        return service().resolve_citation(citation, limit=limit)

    @server.tool()
    def get_incoming_refs(canonical_id: str, limit: int = 50) -> dict[str, Any]:
        """Find provisions that cite the requested canonical ID."""
        return service().get_incoming_refs(canonical_id, limit=limit)

    @server.tool()
    def get_outgoing_refs(canonical_id: str, limit: int = 50) -> dict[str, Any]:
        """Find provisions cited by the requested canonical ID."""
        return service().get_outgoing_refs(canonical_id, limit=limit)

    @server.tool()
    def trace_rule_to_act(canonical_id: str, max_depth: int = 3, limit: int = 10) -> dict[str, Any]:
        """Trace a rule, form, or notification back to enabling Act sections."""
        return service().trace_rule_to_act(canonical_id, max_depth=max_depth, limit=limit)

    @server.tool()
    def find_related_provisions(canonical_id: str, limit: int = 10) -> dict[str, Any]:
        """Find graph-neighbor and semantic-neighbor provisions."""
        return service().find_related_provisions(canonical_id, limit=limit)

    @server.tool()
    def explain_reference_path(source_id: str, target_id: str, max_depth: int = 4, limit: int = 3) -> dict[str, Any]:
        """Show graph paths explaining why two provisions are connected."""
        return service().explain_reference_path(source_id, target_id, max_depth=max_depth, limit=limit)

    @server.tool()
    def get_forms_for_rule(canonical_id: str, limit: int = 50) -> dict[str, Any]:
        """Find GST forms prescribed or referenced by a rule."""
        return service().get_forms_for_rule(canonical_id, limit=limit)

    @server.tool()
    def compare_versions(canonical_id: str, from_date: str = "", to_date: str = "") -> dict[str, Any]:
        """Future tool: compare amended provision states once the time-travel corpus is materialized."""
        return service().compare_versions(canonical_id, from_date=_optional(from_date), to_date=_optional(to_date))

    return server


mcp = create_mcp_server()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.getenv("NYAYA_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("NYAYA_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NYAYA_MCP_PORT", "8090")))
    args = parser.parse_args()

    server = create_mcp_server(host=args.host, port=args.port)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
