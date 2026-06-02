"""Build derived graph indexes from canonical corpus XML."""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _properties(root: ET.Element) -> dict[str, str]:
    props = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            props[name] = prop.attrib.get("value", "")
    return props


def _text_content(element: ET.Element) -> str:
    return " ".join(text.strip() for text in element.itertext() if text and text.strip())


def _add_node(nodes: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    existing = nodes.get(node["id"])
    if not existing:
        if "kinds" not in node:
            node["kinds"] = [node.get("kind", "node")]
        nodes[node["id"]] = node
        return
    kinds = set(existing.get("kinds", [existing.get("kind", "node")]))
    kinds.update(node.get("kinds", [node.get("kind", "node")]))
    existing["kinds"] = sorted(kind for kind in kinds if kind)
    existing["kind"] = "+".join(existing["kinds"])
    existing.update({key: value for key, value in node.items() if value not in (None, "", [])})
    existing["kinds"] = sorted(kind for kind in kinds if kind)
    existing["kind"] = "+".join(existing["kinds"])


def _nearest_source_id(element: ET.Element, parent_map: dict[ET.Element, ET.Element], fallback: str) -> str:
    current: ET.Element | None = element
    while current is not None:
        refers_to = current.attrib.get("refersTo")
        if refers_to:
            return refers_to
        current = parent_map.get(current)
    return fallback


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        edge.get("source", ""),
        edge.get("target", ""),
        edge.get("type", ""),
        edge.get("eId", ""),
    )


def build_graph_index(corpus_dir: Path) -> dict[str, Any]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for path in sorted(corpus_dir.rglob("*.xml")):
        tree = ET.parse(path)
        root = tree.getroot()
        parent_map = {child: parent for parent in root.iter() for child in parent}
        props = _properties(root)
        canonical_id = props.get("canonical_id")
        if not canonical_id:
            continue

        _add_node(
            nodes_by_id,
            {
                "id": canonical_id,
                "kind": "document",
                "kinds": ["document"],
                "document_type": props.get("document_type", ""),
                "title": props.get("title", ""),
                "path": str(path),
                "effective_from": props.get("effective_from", ""),
                "publication_date": props.get("publication_date", ""),
                "review_status": props.get("review_status", ""),
                "source_sha256": props.get("source_sha256", ""),
            },
        )

        for element in root.iter():
            refers_to = element.attrib.get("refersTo")
            if not refers_to:
                continue
            _add_node(
                nodes_by_id,
                {
                    "id": refers_to,
                    "kind": "provision",
                    "kinds": ["provision"],
                    "document_id": canonical_id,
                    "element_tag": element.tag,
                    "eId": element.attrib.get("eId", ""),
                    "title": element.findtext("./heading") or element.findtext("./num") or "",
                    "text": _text_content(element)[:2000],
                    "path": str(path),
                },
            )
            edge = {
                "source": canonical_id,
                "target": refers_to,
                "type": "CONTAINS",
            }
            edges_by_key[_edge_key(edge)] = edge

        for ref in root.findall(".//ref"):
            target = ref.attrib.get("href")
            if target:
                edge = {
                    "source": _nearest_source_id(ref, parent_map, canonical_id),
                    "target": target,
                    "type": ref.attrib.get("type", "REFERS_TO"),
                    "showAs": ref.attrib.get("showAs", target),
                    "eId": ref.attrib.get("eId", ""),
                }
                edges_by_key[_edge_key(edge)] = edge

        for mod in root.findall(".//textualMod"):
            target = mod.attrib.get("href")
            if target:
                edge = {
                    "source": canonical_id,
                    "target": target,
                    "type": mod.attrib.get("type", "AMENDS"),
                    "eId": mod.attrib.get("eId", ""),
                }
                edges_by_key[_edge_key(edge)] = edge

    return {
        "nodes": sorted(nodes_by_id.values(), key=lambda node: node["id"]),
        "edges": sorted(edges_by_key.values(), key=lambda edge: (edge["source"], edge["type"], edge["target"])),
    }


def rebuild_graph_index(corpus_dir: Path, output_path: Path) -> dict[str, Any]:
    graph = build_graph_index(corpus_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    return graph


def _relationship_type(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", value.upper()).strip("_")
    if not cleaned:
        return "RELATED_TO"
    if cleaned[0].isdigit():
        cleaned = f"REL_{cleaned}"
    return cleaned


def build_neo4j_payload(graph: dict[str, Any]) -> dict[str, Any]:
    """Build parameterized Cypher statements for a corpus graph."""
    statements: list[dict[str, Any]] = [
        {
            "cypher": "CREATE CONSTRAINT legal_node_id IF NOT EXISTS FOR (n:LegalNode) REQUIRE n.id IS UNIQUE",
            "params": {},
        },
        {
            "cypher": """
UNWIND $nodes AS node
MERGE (n:LegalNode {id: node.id})
SET n.kind = node.kind,
    n.kinds = coalesce(node.kinds, []),
    n.document_type = coalesce(node.document_type, ''),
    n.title = coalesce(node.title, ''),
    n.path = coalesce(node.path, ''),
    n.document_id = coalesce(node.document_id, ''),
    n.element_tag = coalesce(node.element_tag, ''),
    n.eId = coalesce(node.eId, ''),
    n.effective_from = coalesce(node.effective_from, ''),
    n.publication_date = coalesce(node.publication_date, ''),
    n.review_status = coalesce(node.review_status, ''),
    n.source_sha256 = coalesce(node.source_sha256, ''),
    n.text = coalesce(node.text, '')
""".strip(),
            "params": {"nodes": graph.get("nodes", [])},
        },
    ]

    edges_by_type: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.get("edges", []):
        edges_by_type.setdefault(_relationship_type(edge.get("type", "RELATED_TO")), []).append(edge)

    for relationship_type, edges in sorted(edges_by_type.items()):
        statements.append(
            {
                "cypher": f"""
UNWIND $edges AS edge
MERGE (source:LegalNode {{id: edge.source}})
MERGE (target:LegalNode {{id: edge.target}})
ON CREATE SET target.kind = 'placeholder'
MERGE (source)-[r:{relationship_type}]->(target)
SET r.type = edge.type,
    r.showAs = coalesce(edge.showAs, ''),
    r.eId = coalesce(edge.eId, '')
""".strip(),
                "params": {"edges": edges},
            }
        )

    return {"statements": statements}


def write_neo4j_payload(corpus_dir: Path, output_path: Path) -> dict[str, Any]:
    graph = build_graph_index(corpus_dir)
    payload = build_neo4j_payload(graph)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def load_graph_to_neo4j(
    corpus_dir: Path,
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
    clear: bool = False,
) -> dict[str, int]:
    """Load the derived graph into Neo4j from canonical corpus XML."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("Neo4j loading requires the neo4j Python package") from exc

    graph = build_graph_index(corpus_dir)
    payload = build_neo4j_payload(graph)

    driver = GraphDatabase.driver(
        uri or os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(user or os.getenv("NEO4J_USER", "neo4j"), password or os.getenv("NEO4J_PASSWORD", "gitforlaw123")),
    )
    try:
        with driver.session() as session:
            if clear:
                session.run("MATCH (n:LegalNode) DETACH DELETE n")
            for statement in payload["statements"]:
                session.run(statement["cypher"], **statement["params"])
    finally:
        driver.close()

    return {"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", []))}
