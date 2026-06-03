"""Shared serving tools for the Nyaya Ledger corpus.

This module keeps the downstream API/MCP layer thin. Corpus XML remains the
source of truth; FalkorDB and LanceDB are optional serving indexes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .graph_index import build_graph_index
from .query import build_corpus_lookup, normalize_query_id
from .search_index import build_search_records, read_search_index, search_records


DEFAULT_EMBEDDING_ENDPOINT = "http://127.0.0.1:1234/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-nomic-embed-text-v1.5"


def _clean_text(value: str, limit: int = 0) -> str:
    clean = re.sub(r"\s+", " ", value or "").strip()
    if limit and len(clean) > limit:
        return clean[: limit - 3].rstrip() + "..."
    return clean


def _normalize_form_label(value: str) -> str:
    label = re.sub(r"\s+", "", value).lower()
    label = re.sub(r"[^a-z0-9-]+", "", label)
    if "-" not in label:
        label = re.sub(r"^([a-z]+)([0-9])", r"\1-\2", label)
    return label


class NyayaToolService:
    """Tool methods shared by REST and MCP frontends."""

    def __init__(
        self,
        *,
        corpus_dir: Path | str = "corpus",
        search_index_path: Path | str = "derived/search/corpus_search.jsonl",
        graph_json_path: Path | str = "derived/graph/corpus_graph.json",
        lancedb_path: Path | str = "derived/vector/lancedb",
        lancedb_table: str = "nyaya_ledger_nomic_v1_5",
        falkor_host: str = "127.0.0.1",
        falkor_port: int = 6379,
        falkor_graph: str = "nyaya_ledger",
        embedding_endpoint: str = DEFAULT_EMBEDDING_ENDPOINT,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.corpus_dir = Path(corpus_dir)
        self.search_index_path = Path(search_index_path)
        self.graph_json_path = Path(graph_json_path)
        self.lancedb_path = Path(lancedb_path)
        self.lancedb_table = lancedb_table
        self.falkor_host = falkor_host
        self.falkor_port = falkor_port
        self.falkor_graph = falkor_graph
        self.falkor_enabled = bool(falkor_host) and falkor_port > 0 and os.getenv("NYAYA_DISABLE_FALKORDB", "") != "1"
        self.embedding_endpoint = embedding_endpoint.rstrip("/")
        self.embedding_model = embedding_model

        self._lookup: dict[str, dict[str, Any]] | None = None
        self._search_records: list[dict[str, Any]] | None = None
        self._graph: dict[str, Any] | None = None
        self._nodes_by_id: dict[str, dict[str, Any]] | None = None
        self._outgoing: dict[str, list[dict[str, Any]]] | None = None
        self._incoming: dict[str, list[dict[str, Any]]] | None = None
        self._falkor_graph: Any | None = None
        self._lance_table: Any | None = None

    @classmethod
    def from_env(cls) -> "NyayaToolService":
        return cls(
            corpus_dir=os.getenv("NYAYA_CORPUS_DIR", "corpus"),
            search_index_path=os.getenv("NYAYA_SEARCH_INDEX", "derived/search/corpus_search.jsonl"),
            graph_json_path=os.getenv("NYAYA_GRAPH_JSON", "derived/graph/corpus_graph.json"),
            lancedb_path=os.getenv("NYAYA_LANCEDB_PATH", "derived/vector/lancedb"),
            lancedb_table=os.getenv("NYAYA_LANCEDB_TABLE", "nyaya_ledger_nomic_v1_5"),
            falkor_host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
            falkor_port=int(os.getenv("FALKORDB_PORT", "6379")),
            falkor_graph=os.getenv("FALKORDB_GRAPH", "nyaya_ledger"),
            embedding_endpoint=os.getenv("NYAYA_EMBEDDING_ENDPOINT", DEFAULT_EMBEDDING_ENDPOINT),
            embedding_model=os.getenv("NYAYA_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        )

    def lookup_provision(self, canonical_id: str, *, include_text: bool = True) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        entry = self._corpus_lookup().get(normalized)
        if not entry:
            return {"found": False, "canonical_id": normalized}

        result: dict[str, Any] = {"found": True, "canonical_id": normalized, "roles": entry.get("roles", [])}
        if entry.get("document"):
            result["document"] = self._trim_entry(entry["document"], include_text=include_text)
        if entry.get("provision"):
            result["provision"] = self._trim_entry(entry["provision"], include_text=include_text)
        return result

    def resolve_citation(self, citation: str, *, limit: int = 10) -> dict[str, Any]:
        value = citation.strip()
        if not value:
            return {"citation": citation, "candidates": []}
        if value.startswith("/") or "_" in value:
            normalized = normalize_query_id(value)
            return {
                "citation": citation,
                "candidates": [self._candidate(normalized, "direct")],
            }

        candidates: list[dict[str, Any]] = []
        lowered = re.sub(r"\s+", " ", value.lower())

        form = re.search(r"\bform\s+(?:gst\s+)?([a-z]{2,5}\s*-?\s*\d{1,3}[a-z]?)\b", lowered)
        if form:
            label = _normalize_form_label(form.group(1))
            if "gst" in lowered:
                candidates.append(self._candidate(f"/in/union/forms/gst-{label}", "form"))

        label_match = re.search(r"\b(?:section|sec\.?|s\.|rule|r\.)\s*([0-9]+[a-z]?)\b", lowered)
        if label_match:
            label = label_match.group(1).lower()
            kind = "rule" if re.search(r"\b(rule|r\.)\b", lowered) else "section"
            for phrase, base in self._citation_bases(kind):
                if phrase in lowered:
                    candidates.append(self._candidate(f"{base}/{kind}/{label}", phrase))

        if not candidates:
            for result in self.lexical_search(value, limit=limit):
                candidates.append(self._candidate(result["canonical_id"], "search", result))

        deduped: list[dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            if candidate["canonical_id"] in seen:
                continue
            deduped.append(candidate)
            seen.add(candidate["canonical_id"])
        return {"citation": citation, "candidates": deduped[:limit]}

    def lexical_search(
        self,
        query: str,
        *,
        limit: int = 10,
        document_type: str | None = None,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        return search_records(
            self._search(),
            query,
            limit=limit,
            document_type=document_type,
            role=role,
        )

    def semantic_search(
        self,
        query: str,
        *,
        limit: int = 10,
        document_type: str | None = None,
        role: str | None = None,
        fallback_lexical: bool = True,
    ) -> dict[str, Any]:
        try:
            vector = self._embed(query)
            table = self._lancedb_table()
            rows = table.search(vector).limit(limit * 3).to_list()
            results = []
            for row in rows:
                if document_type and row.get("document_type") != document_type:
                    continue
                if role and row.get("role") != role:
                    continue
                results.append(
                    {
                        "score": 1.0 / (1.0 + float(row.get("_distance", 0.0))),
                        "distance": row.get("_distance", 0.0),
                        "chunk_id": row.get("chunk_id", ""),
                        "canonical_id": row.get("canonical_id", ""),
                        "document_id": row.get("document_id", ""),
                        "document_type": row.get("document_type", ""),
                        "role": row.get("role", ""),
                        "title": row.get("title", ""),
                        "path": row.get("path", ""),
                        "snippet": _clean_text(row.get("text", ""), 360),
                    }
                )
                if len(results) >= limit:
                    break
            return {"mode": "semantic", "query": query, "results": results}
        except Exception as exc:
            if not fallback_lexical:
                raise
            return {
                "mode": "lexical_fallback",
                "query": query,
                "error": str(exc),
                "results": self.lexical_search(query, limit=limit, document_type=document_type, role=role),
            }

    def get_incoming_refs(self, canonical_id: str, *, limit: int = 50) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        edges = self._incoming_edges(normalized, limit=limit)
        return {"canonical_id": normalized, "count": len(edges), "references": [self._edge_payload(edge) for edge in edges]}

    def get_outgoing_refs(self, canonical_id: str, *, limit: int = 50) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        edges = self._outgoing_edges(normalized, limit=limit)
        return {"canonical_id": normalized, "count": len(edges), "references": [self._edge_payload(edge) for edge in edges]}

    def trace_rule_to_act(self, canonical_id: str, *, max_depth: int = 3, limit: int = 10) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        paths = []
        for path in self._bfs_paths(normalized, lambda node_id: "/acts/" in node_id and "/section/" in node_id, max_depth):
            paths.append(path)
            if len(paths) >= limit:
                break
        return {"canonical_id": normalized, "paths": [self._path_payload(path) for path in paths]}

    def find_related_provisions(self, canonical_id: str, *, limit: int = 10) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        related: dict[str, dict[str, Any]] = {}

        for edge in self._outgoing_edges(normalized, limit=limit * 2):
            related.setdefault(edge["target"], self._related_payload(edge["target"], "outgoing_ref", 1.0))
        for edge in self._incoming_edges(normalized, limit=limit * 2):
            related.setdefault(edge["source"], self._related_payload(edge["source"], "incoming_ref", 0.95))

        lookup = self.lookup_provision(normalized, include_text=True)
        text = lookup.get("provision", lookup.get("document", {})).get("text", "")
        if text:
            try:
                semantic = self.semantic_search(text[:2000], limit=limit, fallback_lexical=False)
            except Exception:
                semantic = {"results": []}
            for index, result in enumerate(semantic.get("results", []), start=1):
                result_id = result.get("canonical_id", "")
                if result_id and result_id != normalized:
                    related.setdefault(result_id, self._related_payload(result_id, "semantic", 0.8 / index))

        ranked = sorted(related.values(), key=lambda item: (-item["score"], item["canonical_id"]))
        return {"canonical_id": normalized, "related": ranked[:limit]}

    def explain_reference_path(
        self,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 4,
        limit: int = 3,
    ) -> dict[str, Any]:
        source = normalize_query_id(source_id)
        target = normalize_query_id(target_id)
        paths = []
        for path in self._bfs_paths(source, lambda node_id: node_id == target, max_depth):
            paths.append(path)
            if len(paths) >= limit:
                break
        return {"source": source, "target": target, "paths": [self._path_payload(path) for path in paths]}

    def get_forms_for_rule(self, canonical_id: str, *, limit: int = 50) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        forms: dict[str, dict[str, Any]] = {}
        node_ids = {normalized}
        for edge in self._outgoing_edges(normalized, limit=500):
            if edge.get("type") == "CONTAINS" and edge.get("target", "").startswith(normalized + "/"):
                node_ids.add(edge["target"])

        for node_id in node_ids:
            for edge in self._outgoing_edges(node_id, limit=500):
                if edge.get("target", "").startswith("/in/union/forms/"):
                    forms[edge["target"]] = self._edge_payload(edge)
        return {"canonical_id": normalized, "forms": list(forms.values())[:limit]}

    def compare_versions(self, canonical_id: str, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        return {
            "canonical_id": normalize_query_id(canonical_id),
            "from_date": from_date,
            "to_date": to_date,
            "status": "not_implemented",
            "message": "Version comparison needs the amendment time-travel corpus to be materialized first.",
        }

    def health(self) -> dict[str, Any]:
        return {
            "corpus_dir": str(self.corpus_dir),
            "corpus_available": self.corpus_dir.exists(),
            "search_index_available": self.search_index_path.exists(),
            "graph_json_available": self.graph_json_path.exists(),
            "lancedb_available": self.lancedb_path.exists(),
            "falkordb": {
                "enabled": self.falkor_enabled,
                "host": self.falkor_host,
                "port": self.falkor_port,
                "graph": self.falkor_graph,
            },
            "embedding_model": self.embedding_model,
        }

    def _trim_entry(self, data: dict[str, Any], *, include_text: bool) -> dict[str, Any]:
        item = dict(data)
        if not include_text:
            item.pop("text", None)
        return item

    def _candidate(self, canonical_id: str, reason: str, search_result: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = normalize_query_id(canonical_id)
        lookup = self._corpus_lookup().get(normalized)
        item = {
            "canonical_id": normalized,
            "exists": bool(lookup),
            "reason": reason,
        }
        if lookup:
            payload = lookup.get("provision") or lookup.get("document") or {}
            item.update(
                {
                    "title": payload.get("title", ""),
                    "document_id": payload.get("document_id", payload.get("canonical_id", "")),
                    "document_type": payload.get("document_type", ""),
                }
            )
        if search_result:
            item["search"] = search_result
        return item

    def _related_payload(self, canonical_id: str, reason: str, score: float) -> dict[str, Any]:
        lookup = self._corpus_lookup().get(canonical_id) or {}
        data = lookup.get("provision") or lookup.get("document") or {}
        return {
            "canonical_id": canonical_id,
            "reason": reason,
            "score": score,
            "title": data.get("title", ""),
            "document_id": data.get("document_id", data.get("canonical_id", "")),
            "document_type": data.get("document_type", ""),
            "snippet": _clean_text(data.get("text", ""), 260),
        }

    def _edge_payload(self, edge: dict[str, Any]) -> dict[str, Any]:
        payload = dict(edge)
        source = self._node_payload(edge.get("source", ""))
        target = self._node_payload(edge.get("target", ""))
        payload["source_node"] = source
        payload["target_node"] = target
        payload["title"] = target.get("title", "")
        payload["document_id"] = target.get("document_id", "")
        payload["document_type"] = target.get("document_type", "")
        payload["snippet"] = target.get("snippet", "")
        return payload

    def _path_payload(self, path: list[dict[str, Any]]) -> dict[str, Any]:
        nodes = []
        edges = []
        if not path:
            return {"nodes": [], "edges": []}
        nodes.append(self._node_payload(path[0]["source"]))
        for edge in path:
            edges.append(self._edge_payload(edge))
            nodes.append(self._node_payload(edge["target"]))
        return {"nodes": nodes, "edges": edges}

    def _node_payload(self, canonical_id: str) -> dict[str, Any]:
        lookup = self._corpus_lookup().get(canonical_id) or {}
        data = lookup.get("provision") or lookup.get("document") or self._nodes().get(canonical_id, {})
        return {
            "canonical_id": canonical_id,
            "title": data.get("title", ""),
            "document_id": data.get("document_id", data.get("canonical_id", "")),
            "document_type": data.get("document_type", ""),
            "kind": data.get("kind", ""),
            "snippet": _clean_text(data.get("text", ""), 220),
        }

    def _citation_bases(self, kind: str) -> list[tuple[str, str]]:
        if kind == "rule":
            return [
                ("cgst rules", "/in/union/rules/cgst-rules-2017"),
                ("central goods and services tax rules", "/in/union/rules/cgst-rules-2017"),
                ("income-tax rules", "/in/union/rules/income-tax-rules-2026"),
                ("income tax rules", "/in/union/rules/income-tax-rules-2026"),
            ]
        return [
            ("cgst act", "/in/union/acts/cgst-act-2017"),
            ("central goods and services tax act", "/in/union/acts/cgst-act-2017"),
            ("igst act", "/in/union/acts/igst-act-2017"),
            ("integrated goods and services tax act", "/in/union/acts/igst-act-2017"),
            ("income-tax act 2025", "/in/union/acts/income-tax-act-2025"),
            ("income tax act 2025", "/in/union/acts/income-tax-act-2025"),
            ("income-tax act", "/in/union/acts/income-tax-act-1961"),
            ("income tax act", "/in/union/acts/income-tax-act-1961"),
            ("customs act", "/in/union/acts/customs-act-1962"),
            ("customs tariff act", "/in/union/acts/customs-tariff-act-1975"),
            ("central excise act", "/in/union/acts/central-excise-act-1944"),
        ]

    def _corpus_lookup(self) -> dict[str, dict[str, Any]]:
        if self._lookup is None:
            self._lookup = build_corpus_lookup(self.corpus_dir)
        return self._lookup

    def _search(self) -> list[dict[str, Any]]:
        if self._search_records is None:
            if self.search_index_path.exists():
                self._search_records = read_search_index(self.search_index_path)
            else:
                self._search_records = build_search_records(self.corpus_dir)
        return self._search_records

    def _graph_data(self) -> dict[str, Any]:
        if self._graph is None:
            if self.graph_json_path.exists():
                self._graph = json.loads(self.graph_json_path.read_text(encoding="utf-8"))
            else:
                self._graph = build_graph_index(self.corpus_dir)
        return self._graph

    def _nodes(self) -> dict[str, dict[str, Any]]:
        if self._nodes_by_id is None:
            self._nodes_by_id = {node["id"]: node for node in self._graph_data().get("nodes", [])}
        return self._nodes_by_id

    def _edge_indexes(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        if self._outgoing is None or self._incoming is None:
            outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
            incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for edge in self._graph_data().get("edges", []):
                outgoing[edge.get("source", "")].append(edge)
                incoming[edge.get("target", "")].append(edge)
            self._outgoing = outgoing
            self._incoming = incoming
        return self._outgoing, self._incoming

    def _incoming_edges(self, canonical_id: str, *, limit: int) -> list[dict[str, Any]]:
        try:
            graph = self._falkor()
            result = graph.query(
                """
                MATCH (src:LegalNode)-[r]->(target:LegalNode {id: $id})
                RETURN src.id, target.id, type(r), r.eId, r.showAs
                LIMIT $limit
                """,
                {"id": canonical_id, "limit": limit},
            )
            return [
                {"source": row[0], "target": row[1], "type": row[2], "eId": row[3] or "", "showAs": row[4] or ""}
                for row in result.result_set
            ]
        except Exception:
            _outgoing, incoming = self._edge_indexes()
            return incoming.get(canonical_id, [])[:limit]

    def _outgoing_edges(self, canonical_id: str, *, limit: int) -> list[dict[str, Any]]:
        try:
            graph = self._falkor()
            result = graph.query(
                """
                MATCH (source:LegalNode {id: $id})-[r]->(target:LegalNode)
                RETURN source.id, target.id, type(r), r.eId, r.showAs
                LIMIT $limit
                """,
                {"id": canonical_id, "limit": limit},
            )
            return [
                {"source": row[0], "target": row[1], "type": row[2], "eId": row[3] or "", "showAs": row[4] or ""}
                for row in result.result_set
            ]
        except Exception:
            outgoing, _incoming = self._edge_indexes()
            return outgoing.get(canonical_id, [])[:limit]

    def _bfs_paths(self, source: str, is_target: Any, max_depth: int) -> list[list[dict[str, Any]]]:
        paths: list[list[dict[str, Any]]] = []
        queue = deque([(source, [])])
        seen = {(source, 0)}
        while queue:
            node_id, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            for edge in self._traversal_edges(node_id):
                target = edge.get("target", "")
                next_path = path + [edge]
                if is_target(target):
                    paths.append(next_path)
                state = (target, len(next_path))
                if state not in seen:
                    seen.add(state)
                    queue.append((target, next_path))
        return paths

    def _traversal_edges(self, canonical_id: str) -> list[dict[str, Any]]:
        if self.falkor_enabled:
            return self._outgoing_edges(canonical_id, limit=500)
        outgoing, _incoming = self._edge_indexes()
        return outgoing.get(canonical_id, [])

    def _falkor(self) -> Any:
        if not self.falkor_enabled:
            raise RuntimeError("FalkorDB serving index disabled")
        if self._falkor_graph is None:
            from falkordb import FalkorDB

            self._falkor_graph = FalkorDB(host=self.falkor_host, port=self.falkor_port).select_graph(self.falkor_graph)
        return self._falkor_graph

    def _lancedb_table(self) -> Any:
        if self._lance_table is None:
            import lancedb

            self._lance_table = lancedb.connect(str(self.lancedb_path)).open_table(self.lancedb_table)
        return self._lance_table

    def _embed(self, query: str) -> list[float]:
        request = urllib.request.Request(
            self.embedding_endpoint + "/embeddings",
            data=json.dumps({"model": self.embedding_model, "input": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        embedding = payload["data"][0]["embedding"]
        return [float(value) for value in embedding]


__all__ = ["NyayaToolService"]
