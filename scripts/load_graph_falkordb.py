#!/usr/bin/env python3
"""Load derived/graph/corpus_graph.json into FalkorDB."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


def _rel_type(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", value.upper()).strip("_")
    if not cleaned:
        return "RELATED_TO"
    if cleaned[0].isdigit():
        cleaned = f"REL_{cleaned}"
    return cleaned


def _node_props(node: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "kind",
        "document_type",
        "title",
        "path",
        "document_id",
        "element_tag",
        "eId",
        "effective_from",
        "publication_date",
        "review_status",
        "source_sha256",
        "text",
    )
    props: dict[str, Any] = {}
    for key in allowed:
        value = node.get(key, "")
        if isinstance(value, (str, int, float, bool)):
            props[key] = value
        elif value is None:
            props[key] = ""
    kinds = node.get("kinds")
    if isinstance(kinds, list):
        props["kinds"] = [str(item) for item in kinds]
    else:
        props["kinds"] = []
    return props


def _edge_props(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": str(edge.get("type", "")),
        "showAs": str(edge.get("showAs", "")),
        "eId": str(edge.get("eId", "")),
    }


def _chunks(items: list[dict[str, Any]], size: int) -> Any:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-json", default="derived/graph/corpus_graph.json")
    parser.add_argument("--host", default=os.getenv("FALKORDB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FALKORDB_PORT", "6379")))
    parser.add_argument("--graph", default=os.getenv("FALKORDB_GRAPH", "nyaya_ledger"))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--clear", action="store_true", help="Delete existing graph data before loading")
    args = parser.parse_args()

    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt") from exc

    graph_data = json.loads(Path(args.graph_json).read_text(encoding="utf-8"))
    nodes = [_node_props(node) for node in graph_data.get("nodes", [])]
    edges = graph_data.get("edges", [])

    db = FalkorDB(host=args.host, port=args.port)
    graph = db.select_graph(args.graph)
    if args.clear:
        try:
            graph.delete()
        except Exception:
            pass
        graph = db.select_graph(args.graph)

    try:
        graph.query("CREATE INDEX ON :LegalNode(id)")
    except Exception:
        pass

    loaded_nodes = 0
    for batch in _chunks(nodes, args.batch_size):
        graph.query(
            """
            UNWIND $nodes AS node
            MERGE (n:LegalNode {id: node.id})
            SET n += node
            """,
            {"nodes": batch},
        )
        loaded_nodes += len(batch)
        print(f"nodes={loaded_nodes}/{len(nodes)}", flush=True)

    loaded_edges = 0
    edges_by_type: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        rel_type = _rel_type(str(edge.get("type", "RELATED_TO")))
        edges_by_type.setdefault(rel_type, []).append(
            {
                "source": edge.get("source", ""),
                "target": edge.get("target", ""),
                "props": _edge_props(edge),
            }
        )

    for rel_type, rel_edges in sorted(edges_by_type.items()):
        for batch in _chunks(rel_edges, args.batch_size):
            graph.query(
                f"""
                UNWIND $edges AS edge
                MERGE (source:LegalNode {{id: edge.source}})
                MERGE (target:LegalNode {{id: edge.target}})
                MERGE (source)-[r:{rel_type}]->(target)
                SET r += edge.props
                """,
                {"edges": batch},
            )
            loaded_edges += len(batch)
            print(f"edges={loaded_edges}/{len(edges)} type={rel_type}", flush=True)

    print(f"loaded graph={args.graph} nodes={len(nodes)} edges={len(edges)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
