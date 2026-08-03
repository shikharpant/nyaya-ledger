#!/usr/bin/env python3
"""Run the Nyaya Ledger MCP server."""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.legal_corpus.serving import NyayaToolService  # noqa: E402


_SERVICE: NyayaToolService | None = None
_STDIN_CLOSED = object()


def service() -> NyayaToolService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = NyayaToolService.from_env()
    return _SERVICE


_RATE_LAW_SERVICE: Any = None


def rate_law_service() -> Any:
    """Lazily-built singleton RateLawService (rate/law-change MCP tools)."""
    global _RATE_LAW_SERVICE
    if _RATE_LAW_SERVICE is None:
        from src.legal_corpus.rate_law_mcp import RateLawService
        _RATE_LAW_SERVICE = RateLawService()
    return _RATE_LAW_SERVICE


def _optional(value: str) -> str | None:
    clean = value.strip()
    return clean or None


class _ThreadedStdinReceiveStream:
    """Receive MCP JSON-RPC messages from stdin without AnyIO file threads.

    The installed AnyIO/Python stack can hang while reading process stdin via
    AnyIO's thread-backed file wrapper. A plain daemon thread can read stdin
    reliably; this stream exposes those messages through the async iterator
    protocol expected by the MCP session layer.
    """

    def __init__(self, messages: "queue.Queue[SessionMessage | Exception | object]") -> None:
        self._messages = messages
        self._closed = False

    async def __aenter__(self) -> "_ThreadedStdinReceiveStream":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def __aiter__(self) -> "_ThreadedStdinReceiveStream":
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        while True:
            try:
                message = self._messages.get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.005)

        if message is _STDIN_CLOSED:
            raise StopAsyncIteration
        return message  # type: ignore[return-value]

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._messages.put(_STDIN_CLOSED)


class _StdoutWriteStream:
    """Write MCP JSON-RPC messages as newline-delimited stdio responses."""

    def __init__(self) -> None:
        self._closed = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "_StdoutWriteStream":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def send(self, session_message: SessionMessage) -> None:
        if self._closed:
            return

        payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
        async with self._lock:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()

    async def aclose(self) -> None:
        self._closed = True


async def _run_threaded_stdio(server: FastMCP) -> None:
    messages: "queue.Queue[SessionMessage | Exception | object]" = queue.Queue()

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                try:
                    message = types.JSONRPCMessage.model_validate_json(line)
                    messages.put(SessionMessage(message))
                except Exception as exc:  # pragma: no cover - defensive protocol error path
                    messages.put(exc)
        finally:
            messages.put(_STDIN_CLOSED)

    threading.Thread(target=read_stdin, name="nyaya-mcp-stdio-reader", daemon=True).start()
    await server._mcp_server.run(  # noqa: SLF001 - FastMCP has no public transport injection API.
        _ThreadedStdinReceiveStream(messages),
        _StdoutWriteStream(),
        server._mcp_server.create_initialization_options(),  # noqa: SLF001
    )


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
    def provision_search(query: str, limit: int = 10, provision_type: str = "", document_type: str = "") -> dict[str, Any]:
        """Semantic search over provision-level chunks (sections, rules, sub-rules, forms).

        Returns results aligned to legal provisions rather than flat token windows.
        Each result carries the provision canonical_id, provision_type, and number.
        """
        return service().semantic_search_provision(
            query,
            limit=limit,
            provision_type=_optional(provision_type),
            document_type=_optional(document_type),
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
        """Compare a provision's text between two dates. Returns the text at each date, a unified diff, and events between."""
        return service().compare_versions(canonical_id, from_date=_optional(from_date), to_date=_optional(to_date))

    @server.tool()
    def get_provision_as_of_date(canonical_id: str, date: str) -> dict[str, Any]:
        """Return the exact provision text in force on a given date, with full provenance (version_id, text_sha256, event_chain, source_basis)."""
        return service().get_provision_as_of_date(canonical_id, date=date)

    @server.tool()
    def list_amendments(canonical_id: str) -> dict[str, Any]:
        """Return the ordered chain of amendments that affected a provision, each with event_id, operation, effective_date, and source document."""
        return service().list_amendments(canonical_id)

    @server.tool()
    def get_provision_timeline(canonical_id: str) -> dict[str, Any]:
        """Return all dated versions of a provision across its lifecycle, each with applicability dates, version_id, text_sha256, and snippet."""
        return service().get_provision_timeline(canonical_id)

    @server.tool()
    def query_law_as_of_date(act: str, section: str, date: str) -> dict[str, Any]:
        """Query the exact position of law by act name + section number + date. Resolves the citation, reconstructs the provision text at that date, and returns the amendment chain. Example: act='CGST Act', section='16', date='2024-01-01'."""
        return service().query_law_as_of_date(act, section, date)

    @server.tool()
    def get_form_structure(form_id: str) -> dict[str, Any]:
        """Return the structural definition of a GST form (sections, fields, tables, columns).

        Args:
            form_id: Form identifier (e.g., 'gstr-1', 'gst-reg-01', 'gst-rfd-01')
        """
        from pathlib import Path
        import json as _json

        form_file = form_id.lower().strip()
        structure_path = Path("derived/form_structure") / f"{form_file}.json"
        if not structure_path.exists():
            return {
                "status": "not_found",
                "form_id": form_id,
                "message": f"No structure file found for '{form_id}'. Available: see derived/form_structure/",
            }

        with open(structure_path) as f:
            structure = _json.load(f)

        source = structure.get("source", "")
        has_sections = bool(structure.get("sections"))

        if has_sections:
            form_status = "complete"
        elif source in ("category_stub",):
            form_status = "category_stub"
        elif source in ("sub_component",):
            form_status = "sub_component"
        elif structure.get("alias_of"):
            form_status = "alias"
        elif source in ("metadata_only",):
            form_status = "placeholder"
        else:
            form_status = "placeholder"

        return {
            "status": "ok",
            "form_id": form_id,
            "structure": structure,
            "form_status": form_status,
            "has_sections": has_sections,
        }

    # ── Rate-change tools (deterministic retrieval over rate schedules) ──

    @server.tool()
    def get_rate_for_hsn(hsn_code: str, as_of_date: str, jurisdiction: str = "") -> dict[str, Any]:
        """Return the applicable GST rate entry(ies) for an HSN code on a date.

        Covers goods + cess schedules (HSN-keyed). Normalizes HSN (4/6/8-digit,
        leading zeros, spaces). Returns rate breakdown, conditions, and the
        notification that set the rate. Returns result=unresolved if no entry
        covers the date -- never silently uses the current rate.
        """
        return rate_law_service().get_rate_for_hsn(
            hsn_code, as_of_date, jurisdiction=_optional(jurisdiction))

    @server.tool()
    def trace_rate_changes(hsn_code: str, from_date: str = "", to_date: str = "") -> dict[str, Any]:
        """Chronological rate changes for an HSN: effective date, old/new rate,
        amending notification, operation, retrospective flag."""
        return rate_law_service().trace_rate_changes(
            hsn_code, from_date=_optional(from_date), to_date=_optional(to_date))

    @server.tool()
    def get_rate_conditions(rate_entry_id_or_hsn_plus_date: str, as_of_date: str = "") -> dict[str, Any]:
        """All conditions, provisos, explanations and exemptions for a rate entry.

        Accepts locators like '11/2017-ct-rate::sno=3', '1/2017::hsn=0101', or
        '1/2017::schedule=I'. Returns the entry description plus inherited
        schedule heading, opening paragraph and explanations.
        """
        return rate_law_service().get_rate_conditions(
            rate_entry_id_or_hsn_plus_date, as_of_date=_optional(as_of_date))

    @server.tool()
    def compare_rates(hsn_codes: list[str], as_of_date: str) -> dict[str, Any]:
        """Side-by-side rate comparison for multiple HSN codes on one date."""
        return rate_law_service().compare_rates(hsn_codes, as_of_date)

    # ── Law-change tools (event-sourced statute version history) ──

    @server.tool()
    def get_law_as_of(citation: str, as_of_date: str) -> dict[str, Any]:
        """Provision text exactly as it stood on a date, with version_id,
        text_sha256, applicability window, event_chain and source_basis.
        Returns result=unresolved if no version covers the date."""
        return rate_law_service().get_law_as_of(citation, as_of_date)

    @server.tool()
    def trace_amendments(citation: str, include_unreviewed: bool = False) -> dict[str, Any]:
        """Chronological reviewed amendments: effective date, amending instrument,
        operation, old->new diff, retrospective flag. Unreviewed candidate events
        appear ONLY in unreviewed_candidates[] when include_unreviewed=true, never
        in the main result."""
        return rate_law_service().trace_amendments(citation, include_unreviewed=include_unreviewed)

    @server.tool()
    def get_amendment_instrument(amendment_id_or_citation_plus_date: str) -> dict[str, Any]:
        """Return the Finance Act / Notification / Circular behind an amendment
        (its document_id, text, commencement date). Accepts '<canonical_id>@<date>'."""
        return rate_law_service().get_amendment_instrument(amendment_id_or_citation_plus_date)

    @server.tool()
    def get_commencement_chain(citation: str, amendment_date: str) -> dict[str, Any]:
        """Enactment date, commencement date, retrospective operation, saving and
        transition provisions for an amendment. Flags commencement_unspecified
        where the commencement is absent."""
        return rate_law_service().get_commencement_chain(citation, amendment_date)

    @server.tool()
    def compare_law_versions(citation: str, version_a_date: str, version_b_date: str) -> dict[str, Any]:
        """Text of a provision at two dates, a unified diff, and the amendment
        event(s) responsible for the change between them."""
        return rate_law_service().compare_law_versions(citation, version_a_date, version_b_date)

    return server


mcp = create_mcp_server()


def _run_simple_stdio(server: FastMCP) -> None:
    """Minimal stdio JSON-RPC loop that bypasses FastMCP's session layer.

    Reads newline-delimited JSON-RPC from stdin, dispatches to the FastMCP
    tool manager, and writes responses to stdout. Avoids AnyIO/threading
    hangs in the default transport.
    """
    import json
    import traceback

    mcp_server = server._mcp_server  # noqa: SLF001
    tool_manager = server._tool_manager  # noqa: SLF001
    initialized = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": "nyaya-ledger", "version": "1.26.0"},
                    "instructions": "Tools for querying the Nyaya Ledger Indian legal corpus.",
                },
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            initialized = True

        elif method == "notifications/initialized":
            pass  # notification, no response

        elif method == "tools/list":
            tools = []
            for tool in server._tool_manager.list_tools():  # noqa: SLF001
                # FastMCP Tool is a pydantic model — extract only JSON-serializable fields
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.parameters,
                })
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                # Call the underlying function directly to avoid asyncio issues
                tool_obj = None
                for t in server._tool_manager.list_tools():  # noqa: SLF001
                    if t.name == tool_name:
                        tool_obj = t
                        break
                if tool_obj is None:
                    response = {"jsonrpc": "2.0", "id": msg_id,
                                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"}}
                else:
                    result = tool_obj.fn(**arguments)
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(result, default=str, ensure_ascii=False)}]
                        },
                    }
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            sys.stdout.write(json.dumps(response, default=str, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        elif method == "ping":
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


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
    if args.transport == "stdio":
        _run_simple_stdio(server)
    else:
        server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
