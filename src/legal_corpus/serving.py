"""Shared serving tools for the Nyaya Ledger corpus.

This module keeps the downstream API/MCP layer thin. Corpus XML remains the
source of truth; FalkorDB and LanceDB are optional serving indexes.
"""

from __future__ import annotations

import json
import hashlib
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

# Structured canonical IDs that have materialized version history.
# Used by _fast_exists() to skip the ~35s corpus XML scan for section/rule queries.
_STRUCTURED_PROVISION_RE = re.compile(r"^/in/union/(acts|rules)/[^/]+/(section|rule)/")


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


def _query_terms(value: str) -> list[str]:
    return [term for term in re.findall(r"[a-z0-9]+", value.lower()) if len(term) > 2]


def _provision_rank_bonus(row: dict[str, Any], query: str) -> float:
    """Small lexical tie-breaker for provision-level semantic candidates."""
    haystack = " ".join(
        str(row.get(field, ""))
        for field in ("title", "text", "canonical_id", "document_title")
    ).lower()
    normalized_query = " ".join(_query_terms(query))
    bonus = 0.0
    if normalized_query and normalized_query in haystack:
        bonus += 0.05
    terms = _query_terms(query)
    if terms and all(term in haystack for term in terms):
        bonus += 0.03
    if row.get("provision_type") == "section" and row.get("document_type") == "act":
        bonus += 0.03
    return bonus


class NyayaToolService:
    """Tool methods shared by REST and MCP frontends."""

    def __init__(
        self,
        *,
        corpus_dir: Path | str = "corpus",
        search_index_path: Path | str = "derived/search/corpus_search.jsonl",
        graph_json_path: Path | str = "derived/graph/corpus_graph.json",
        version_history_dir: Path | str = "derived/version_history/cgst-rules-2017",
        lancedb_path: Path | str = "derived/vector/lancedb",
        lancedb_table: str = "nyaya_ledger_nomic_v1_5",
        provision_lancedb_table: str = "nyaya_provisions_v1",
        falkor_host: str = "127.0.0.1",
        falkor_port: int = 6379,
        falkor_graph: str = "nyaya_ledger",
        embedding_endpoint: str = DEFAULT_EMBEDDING_ENDPOINT,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.corpus_dir = Path(corpus_dir)
        self.search_index_path = Path(search_index_path)
        self.graph_json_path = Path(graph_json_path)
        self.version_history_dir = Path(version_history_dir)
        self.lancedb_path = Path(lancedb_path)
        self.lancedb_table = lancedb_table
        self.provision_lancedb_table = provision_lancedb_table
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
        # Per-process caches for immutable version_history artifacts. The data
        # files only change on explicit regeneration; restart the process (or
        # call invalidate_node_versions_cache for node_versions.jsonl) after
        # regenerating. See AGENTS.md "Cache and restart conventions".
        self._event_index_cache: dict[Path, dict[str, dict[str, Any]]] = {}
        self._json_cache: dict[Path, dict[str, Any]] = {}
        self._provision_lance_table: Any | None = None

    @classmethod
    def from_env(cls) -> "NyayaToolService":
        return cls(
            corpus_dir=os.getenv("NYAYA_CORPUS_DIR", "corpus"),
            search_index_path=os.getenv("NYAYA_SEARCH_INDEX", "derived/search/corpus_search.jsonl"),
            graph_json_path=os.getenv("NYAYA_GRAPH_JSON", "derived/graph/corpus_graph.json"),
            lancedb_path=os.getenv("NYAYA_LANCEDB_PATH", "derived/vector/lancedb"),
            lancedb_table=os.getenv("NYAYA_LANCEDB_TABLE", "nyaya_ledger_nomic_v1_5"),
            provision_lancedb_table=os.getenv("NYAYA_PROVISION_LANCEDB_TABLE", "nyaya_provisions_v1"),
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

    def semantic_search_provision(
        self,
        query: str,
        *,
        limit: int = 10,
        provision_type: str | None = None,
        document_type: str | None = None,
        fallback_lexical: bool = True,
    ) -> dict[str, Any]:
        """Semantic search over provision-level chunks (sections/rules/sub-rules/forms).

        Unlike :meth:`semantic_search` (flat 128-token windows), this queries the
        ``nyaya_provisions_v1`` LanceDB table whose chunks are aligned to legal
        provisions. Each result carries the provision ``canonical_id``,
        ``provision_type``, ``number`` and document-level metadata.
        """
        try:
            vector = self._embed(query)
            table = self._provision_lancedb_table()
            rows = table.search(vector).limit(max(limit * 25, 50)).to_list()
            results = []
            for row in rows:
                if provision_type and row.get("provision_type") != provision_type:
                    continue
                if document_type and row.get("document_type") != document_type:
                    continue
                score = 1.0 / (1.0 + float(row.get("_distance", 0.0)))
                results.append(
                    {
                        "_rank_score": score + _provision_rank_bonus(row, query),
                        "score": score,
                        "distance": row.get("_distance", 0.0),
                        "chunk_id": row.get("chunk_id", ""),
                        "canonical_id": row.get("canonical_id", ""),
                        "provision_type": row.get("provision_type", ""),
                        "number": row.get("number", ""),
                        "document_id": row.get("document_id", ""),
                        "document_type": row.get("document_type", ""),
                        "document_title": row.get("document_title", ""),
                        "title": row.get("title", ""),
                        "path": row.get("path", ""),
                        "snippet": _clean_text(row.get("text", ""), 360),
                    }
                )
            results.sort(key=lambda item: item.pop("_rank_score"), reverse=True)
            results = results[:limit]
            return {"mode": "semantic_provision", "query": query, "results": results}
        except Exception as exc:
            if not fallback_lexical:
                raise
            return {
                "mode": "lexical_fallback",
                "query": query,
                "error": str(exc),
                "results": self.lexical_search(query, limit=limit, document_type=document_type),
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

    def compare_versions(
        self,
        canonical_id: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        from .version_compare import compare_component_versions

        return compare_component_versions(
            canonical_id,
            from_date=from_date,
            to_date=to_date,
        )

    def query_law_as_of_date(self, act: str, section: str, date: str) -> dict[str, Any]:
        """Resolve act + section + date into the same dated provision result used by MCP."""
        citation = f"section {section} {act}"
        resolved = self.resolve_citation(citation, limit=5)
        candidates = resolved.get("candidates", [])
        if not candidates:
            return {
                "status": "not_found",
                "citation": citation,
                "date": date,
                "message": f"Could not resolve '{citation}' to a canonical provision.",
                "verification": self._failed_verification("citation_not_resolved"),
            }

        existing = [candidate for candidate in candidates if candidate.get("exists")]
        if len(existing) > 1:
            return {
                "status": "ambiguous_citation",
                "citation": citation,
                "date": date,
                "candidates": existing,
                "message": f"'{citation}' resolved to multiple canonical provisions.",
                "verification": self._failed_verification("ambiguous_citation"),
            }
        selected = existing[0] if existing else candidates[0]
        canonical_id = selected["canonical_id"]
        result = self.get_provision_as_of_date(canonical_id, date=date)
        result["citation"] = citation
        result["resolved_canonical_id"] = canonical_id
        return result

    def get_provision_as_of_date(
        self,
        canonical_id: str,
        *,
        date: str,
        target_work: str | None = None,
    ) -> dict[str, Any]:
        """Return the exact provision text in force on *date*, with provenance
        and the expanded amendment chain up to and including that date."""
        from datetime import datetime

        # Validate date format before reconstruction
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return {
                "status": "invalid_date",
                "canonical_id": normalize_query_id(canonical_id),
                "date": date,
                "text": "",
                "message": f"Invalid date '{date}'. Expected YYYY-MM-DD format.",
                "verification": self._failed_verification("invalid_date"),
            }

        from .version_reconstruct import reconstruct_component

        result = reconstruct_component(
            canonical_id,
            date=date,
            target_work=target_work,
        )
        version = result.get("version") or {}
        component_id = result.get("component_id", normalize_query_id(canonical_id))

        # Expand the amendment chain, filtered to amendments effective on or
        # before the queried date — future amendments are not part of the law
        # at that date and must not appear in a historical position-of-law query.
        amendments: list[dict[str, Any]] = []
        coverage = "complete"
        coverage_gaps: list[dict[str, Any]] = []
        reconciliation_gaps: list[dict[str, Any]] = []
        confidence_tier = "unknown"
        confidence_detail: dict[str, Any] = {}
        comparison: dict[str, Any] = {}
        warnings: list[str] = []
        if result.get("status") == "ok":
            chain_data = self.list_amendments(component_id, target_work=target_work)
            amendments = [
                a for a in chain_data.get("amendments", [])
                if (a.get("effective_date") or "") <= date
            ]
            # Surface coverage/confidence from the comparison engine so callers
            # know whether the position-of-law is exact or has materialization gaps.
            # Filter to only gaps affecting THIS component — other sections' gaps
            # in the same work should not affect this provision's coverage status.
            try:
                comparison = self.compare_versions(
                    component_id,
                    from_date=version.get("applicability_start") or version.get("valid_from"),
                    to_date=date,
                )
                all_gaps = comparison.get("coverage_gaps", [])
                coverage_gaps = [g for g in all_gaps if self._gap_applies_to_component(g, component_id)]
                reconciliation_gaps = comparison.get("reconciliation_gaps", [])
                confidence_tier = comparison.get("confidence_tier", "unknown")
                confidence_detail = comparison.get("confidence_detail", {})
                if coverage_gaps:
                    coverage = "incomplete"
                elif reconciliation_gaps:
                    coverage = "incomplete"
                elif comparison.get("status") != "ok":
                    coverage = "incomplete"
                else:
                    coverage = "complete"
                warnings = comparison.get("warnings", []) if coverage != "complete" else []
            except Exception:
                pass

        raw_status = result.get("status", "not_found")
        verification = self._verification_for_result(
            component_id=component_id,
            date=date,
            raw_status=raw_status,
            text=result.get("text", ""),
            version=version,
            source_basis=version.get("source_basis", {}),
            coverage=coverage,
            coverage_gaps=coverage_gaps,
            reconciliation_gaps=reconciliation_gaps,
            confidence_tier=confidence_tier,
            confidence_detail=confidence_detail,
            comparison=comparison,
            target_work=target_work,
        )
        # When reconstruction succeeded but coverage is incomplete, downgrade
        # the public status so callers do not mistake an inexact position-of-law
        # for a court-ready one.
        if raw_status == "ok" and verification.get("verdict") in {"exact", "exact_with_formatting_debt"}:
            effective_status = "ok"
        elif raw_status == "ok" and verification.get("verdict") == "failed_verification":
            effective_status = "failed_verification"
        elif raw_status == "ok":
            effective_status = "ok_with_gaps"
        else:
            effective_status = raw_status

        return {
            "status": effective_status,
            "canonical_id": component_id,
            "date": date,
            "text": result.get("text", ""),
            "version_id": version.get("version_id"),
            "text_sha256": version.get("text_sha256"),
            "applicability_start": version.get("applicability_start") or version.get("valid_from"),
            "applicability_end": version.get("applicability_end") or version.get("valid_to"),
            "event_chain": version.get("event_chain", []),
            "source_basis": version.get("source_basis", {}),
            "created_by_event_id": version.get("created_by_event_id"),
            "amendments": amendments,
            "coverage": coverage,
            "coverage_gaps": coverage_gaps,
            "reconciliation_gaps": reconciliation_gaps,
            "confidence_tier": confidence_tier,
            "confidence_detail": confidence_detail,
            "warnings": warnings,
            "verification": verification,
        }

    def build_query_proof_pack(self, *, act: str, section: str, date: str) -> dict[str, Any]:
        result = self.query_law_as_of_date(act, section, date)
        text = result.get("text", "")
        verification = result.get("verification", {})
        return {
            "request": {"act": act, "section": section, "date": date},
            "resolved_canonical_id": result.get("resolved_canonical_id") or result.get("canonical_id"),
            "returned_text_hash": result.get("text_sha256") or hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "version_id": result.get("version_id"),
            "applicability_range": {
                "start": result.get("applicability_start"),
                "end": result.get("applicability_end"),
            },
            "event_chain": result.get("event_chain", []),
            "source_basis": result.get("source_basis", {}),
            "source_span_verification": (verification.get("checks") or {}).get("source_span_hash", {}),
            "official_source_refs": (verification.get("checks") or {}).get("official_source_citation", {}).get("refs", []),
            "coverage_summary": {
                "coverage": result.get("coverage"),
                "coverage_gaps": result.get("coverage_gaps", []),
            },
            "confidence_summary": {
                "tier": result.get("confidence_tier"),
                "detail": result.get("confidence_detail", {}),
            },
            "reconciliation_summary": {
                "gaps": result.get("reconciliation_gaps", []),
            },
            "mcp_transcript": [
                {
                    "role": "client",
                    "tool": "query_law_as_of_date",
                    "arguments": {"act": act, "section": section, "date": date},
                },
                {
                    "role": "server",
                    "tool": "query_law_as_of_date",
                    "result": result,
                },
            ],
            "final_verdict": verification.get("verdict", "failed_verification"),
            "verification": verification,
        }

    def _failed_verification(self, reason: str) -> dict[str, Any]:
        return {
            "verdict": "failed_verification",
            "checks": {},
            "blocking_reasons": [reason],
        }

    def _verification_for_result(
        self,
        *,
        component_id: str,
        date: str,
        raw_status: str,
        text: str,
        version: dict[str, Any],
        source_basis: dict[str, Any],
        coverage: str,
        coverage_gaps: list[dict[str, Any]],
        reconciliation_gaps: list[dict[str, Any]],
        confidence_tier: str,
        confidence_detail: dict[str, Any],
        comparison: dict[str, Any],
        target_work: str | None,
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        blocking: list[str] = []

        if raw_status != "ok":
            return self._failed_verification(raw_status)

        checks["text_hash"] = self._text_hash_check(text, str(version.get("text_sha256") or ""))
        checks["source_span_hash"] = self._source_span_check(
            component_id=component_id,
            event_chain=list(version.get("event_chain") or []),
            source_basis=source_basis,
            target_work=target_work,
        )
        checks["coverage_gap"] = {
            "status": "passed" if coverage == "complete" and not coverage_gaps else "failed",
            "coverage": coverage,
            "gap_count": len(coverage_gaps),
            "gaps": coverage_gaps,
        }
        checks["reconciliation"] = {
            "status": "passed" if not reconciliation_gaps else "failed",
            "gap_count": len(reconciliation_gaps),
            "gaps": reconciliation_gaps,
        }
        checks["confidence_tier"] = {
            "status": "passed" if confidence_tier in {"A", "B"} and not confidence_detail.get("tier_blockers") else "failed",
            "tier": confidence_tier,
            "tier_blockers": confidence_detail.get("tier_blockers", []),
            "detail": confidence_detail,
        }
        checks["official_source_citation"] = self._official_source_citation_check(
            component_id=component_id,
            event_chain=list(version.get("event_chain") or []),
            source_basis=source_basis,
            target_work=target_work,
        )
        checks["manifest_consistency"] = self._manifest_consistency_check(component_id, target_work=target_work)

        for name, check in checks.items():
            if check.get("status") == "failed":
                blocking.append(name)
        if comparison.get("status") not in {None, "", "ok"}:
            blocking.append(f"comparison_status:{comparison.get('status')}")

        formatting_debt = self._has_formatting_debt(text)
        checks["formatting"] = {
            "status": "warning" if formatting_debt else "passed",
            "formatting_debt": formatting_debt,
        }

        if blocking:
            verdict = "ok_with_gaps"
        elif formatting_debt:
            verdict = "exact_with_formatting_debt"
        else:
            verdict = "exact"

        return {
            "verdict": verdict,
            "checks": checks,
            "blocking_reasons": blocking,
        }

    def _text_hash_check(self, text: str, expected_hash: str) -> dict[str, Any]:
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        return {
            "status": "passed" if expected_hash and actual_hash == expected_hash else "failed",
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
        }

    def _source_span_check(
        self,
        *,
        component_id: str,
        event_chain: list[str],
        source_basis: dict[str, Any],
        target_work: str | None,
    ) -> dict[str, Any]:
        if not event_chain:
            return {
                "status": "passed" if source_basis.get("type") == "baseline_corpus" else "failed",
                "checked_event_ids": [],
                "missing_event_ids": [],
                "unverified_event_ids": [],
                "mismatched_event_ids": [],
                "baseline_source": source_basis,
            }

        events = self._event_index_for_component(component_id, target_work=target_work)
        checked: list[str] = []
        missing: list[str] = []
        unverified: list[str] = []
        mismatched: list[str] = []
        source_basis_event = source_basis.get("event_id")
        source_basis_hash = ((source_basis.get("source_span") or {}).get("text_hash") or "")
        for event_id in event_chain:
            event = events.get(event_id)
            if not event:
                missing.append(event_id)
                continue
            span = (event.get("evidence") or {}).get("source_span") or {}
            span_hash = span.get("text_hash") or ""
            validation = event.get("validation") or {}
            checked.append(event_id)
            span_verified = bool(validation.get("source_span_verified"))
            deterministic_verified = bool(validation.get("deterministic")) and validation.get("source_hash") == span_hash
            if not span_hash or not (span_verified or deterministic_verified):
                unverified.append(event_id)
            if event_id == source_basis_event and source_basis_hash and span_hash and source_basis_hash != span_hash:
                mismatched.append(event_id)
        status = "passed" if not missing and not unverified and not mismatched else "failed"
        return {
            "status": status,
            "checked_event_ids": checked,
            "missing_event_ids": missing,
            "unverified_event_ids": unverified,
            "mismatched_event_ids": mismatched,
            "source_basis_event_id": source_basis_event,
        }

    def _official_source_citation_check(
        self,
        *,
        component_id: str,
        event_chain: list[str],
        source_basis: dict[str, Any],
        target_work: str | None,
    ) -> dict[str, Any]:
        refs: list[dict[str, Any]] = []
        if not event_chain:
            refs.append(
                {
                    "type": source_basis.get("type", "baseline_corpus"),
                    "path": source_basis.get("path"),
                    "base_as_of": source_basis.get("base_as_of"),
                }
            )
            return {"status": "passed" if source_basis else "failed", "refs": refs}

        events = self._event_index_for_component(component_id, target_work=target_work) if component_id.startswith("/") else {}
        if not events:
            basis_event = str(source_basis.get("event_id") or "")
            events = self._event_index_for_event_id(basis_event, target_work=target_work) if basis_event else {}
        missing: list[str] = []
        for event_id in event_chain:
            event = events.get(event_id)
            if not event:
                missing.append(event_id)
                continue
            source = event.get("source") or {}
            evidence = event.get("evidence") or {}
            ref = {
                "event_id": event_id,
                "source_document_id": source.get("document_id") or evidence.get("source_document_id"),
                "instrument_number": source.get("instrument_number"),
                "source_url": source.get("source_url"),
                "record_id": source.get("record_id"),
                "source_span": evidence.get("source_span") or {},
            }
            if not (ref["source_document_id"] or ref["instrument_number"] or ref["source_url"]):
                missing.append(event_id)
            refs.append(ref)
        return {
            "status": "passed" if not missing else "failed",
            "refs": refs,
            "missing_event_ids": missing,
        }

    def _manifest_consistency_check(self, component_id: str, *, target_work: str | None) -> dict[str, Any]:
        version_dir, _resolved_work = self._version_dir_for_component(component_id, target_work=target_work)
        manifest = self._read_json(version_dir / "materialization_manifest.json")
        coverage = self._read_json(version_dir / "coverage_gaps.json")
        failures: list[str] = []
        paths: dict[str, str] = {}
        for key in ("node_versions", "coverage_gaps"):
            value = str(manifest.get(key) or "")
            paths[key] = value
            if not value:
                failures.append(f"missing_manifest_path:{key}")
                continue
            if value.startswith("/tmp/") or "/tmp/" in value:
                failures.append(f"tmp_manifest_path:{key}")
            if not Path(value).exists():
                failures.append(f"missing_manifest_artifact:{key}")
        manifest_gap_count = manifest.get("coverage_gap_count")
        coverage_gap_count = coverage.get("gap_count", len(coverage.get("gaps", [])))
        actual_gap_count = len(coverage.get("gaps", []))
        if manifest_gap_count != actual_gap_count or coverage_gap_count != actual_gap_count:
            failures.append("coverage_gap_count_mismatch")
        return {
            "status": "passed" if not failures else "failed",
            "version_dir": str(version_dir),
            "paths": paths,
            "manifest_coverage_gap_count": manifest_gap_count,
            "coverage_gap_count": coverage_gap_count,
            "actual_gap_count": actual_gap_count,
            "failures": failures,
        }

    def _event_index_for_component(self, component_id: str, *, target_work: str | None) -> dict[str, dict[str, Any]]:
        version_dir, _resolved_work = self._version_dir_for_component(component_id, target_work=target_work)
        return self._event_index_for_version_dir(version_dir)

    def _event_index_for_event_id(self, event_id: str, *, target_work: str | None) -> dict[str, dict[str, Any]]:
        if target_work:
            version_dir, _resolved_work = self._version_dir_for_component(target_work, target_work=target_work)
            return self._event_index_for_version_dir(version_dir)
        for path in Path("derived/version_history").glob("*/fixed_amendment_events.jsonl"):
            events = self._read_event_index(path)
            if event_id in events:
                return events
        return {}

    def _event_index_for_version_dir(self, version_dir: Path) -> dict[str, dict[str, Any]]:
        manifest = self._read_json(version_dir / "materialization_manifest.json")
        candidates = []
        if manifest.get("events_path"):
            candidates.append(Path(str(manifest["events_path"])))
        candidates.extend(
            [
                version_dir / "fixed_amendment_events.jsonl",
                version_dir / "merged_amendment_events.jsonl",
                version_dir / "amendment_events_reviewed.jsonl",
                Path("derived/version_history/amendment_events_reviewed.jsonl"),
            ]
        )
        for path in candidates:
            events = self._read_event_index(path)
            if events:
                return events
        return {}

    def _read_event_index(self, path: Path) -> dict[str, dict[str, Any]]:
        cached = self._event_index_cache.get(path)
        if cached is not None:
            return cached
        if not path.exists():
            return {}
        events: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = str(event.get("event_id") or "")
            if event_id:
                events[event_id] = event
        self._event_index_cache[path] = events
        return events

    def _version_dir_for_component(self, canonical_id: str, *, target_work: str | None) -> tuple[Path, str | None]:
        from .version_compare import resolve_version_dir

        return resolve_version_dir(canonical_id, target_work=target_work)

    def _read_json(self, path: Path) -> dict[str, Any]:
        cached = self._json_cache.get(path)
        if cached is not None:
            return cached
        if not path.exists():
            return {}
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        self._json_cache[path] = result
        return result

    def _gap_applies_to_component(self, gap: dict[str, Any], component_id: str) -> bool:
        target = gap.get("target") or {}
        candidates = [
            str(target.get("component_id") or ""),
            str(target.get("anchor_component_id") or ""),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if candidate == component_id:
                return True
            if candidate.startswith(component_id + "/") or component_id.startswith(candidate + "/"):
                return True
        return False

    def _has_formatting_debt(self, text: str) -> bool:
        return "\u200b" in text or bool(re.search(r"\b\d+\s*\[(?:\*\*\*\*|[^\]]+)\]", text))

    def _component_version_rows(
        self,
        canonical_id: str,
        *,
        target_work: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read node_versions.jsonl rows for a component, resolving the correct version dir."""
        from .version_compare import read_node_versions, resolve_version_dir

        canonical = normalize_query_id(canonical_id)

        # Forms have their own version-history directory
        if canonical.startswith("/in/union/forms/"):
            forms_dir = self.version_history_dir.parent / "forms"
            node_versions_path = forms_dir / "node_versions.jsonl"
            if node_versions_path.exists():
                rows = [
                    row
                    for row in read_node_versions(node_versions_path)
                    if normalize_query_id(str(row.get("component_id") or "")) == canonical
                ]
                return rows, "/in/union/forms"

        resolved_dir, resolved_work = resolve_version_dir(canonical, target_work=target_work)
        node_versions_path = resolved_dir / "node_versions.jsonl"
        if not node_versions_path.exists():
            return [], resolved_work
        rows = [
            row
            for row in read_node_versions(node_versions_path)
            if normalize_query_id(str(row.get("component_id") or "")) == canonical
        ]
        return rows, resolved_work

    def list_amendments(
        self,
        canonical_id: str,
        *,
        target_work: str | None = None,
    ) -> dict[str, Any]:
        """Return the ordered amendment chain for a provision."""
        from .version_compare import normalize_version_component_id

        component_id = normalize_version_component_id(canonical_id)
        rows, resolved_work = self._component_version_rows(component_id, target_work=target_work)
        amendments: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: r.get("applicability_start") or r.get("valid_from") or ""):
            event_id = row.get("created_by_event_id")
            if not event_id:
                continue  # skip baseline-only versions
            basis = row.get("source_basis", {})
            amendments.append({
                "event_id": event_id,
                "operation": basis.get("operation", ""),
                "effective_date": row.get("applicability_start") or row.get("valid_from"),
                "source_document_id": basis.get("source_document_id", ""),
                "source_record_id": basis.get("source_record_id", ""),
                "source_span": basis.get("source_span", {}),
            })
        return {
            "canonical_id": component_id,
            "target_work": resolved_work,
            "count": len(amendments),
            "amendments": amendments,
        }

    def get_provision_timeline(
        self,
        canonical_id: str,
        *,
        target_work: str | None = None,
    ) -> dict[str, Any]:
        """Return all materialized versions of a provision, ordered by date."""
        from .version_compare import normalize_version_component_id

        component_id = normalize_version_component_id(canonical_id)
        rows, resolved_work = self._component_version_rows(component_id, target_work=target_work)
        versions: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: r.get("applicability_start") or r.get("valid_from") or ""):
            text = row.get("text", "")
            versions.append({
                "version_id": row.get("version_id"),
                "applicability_start": row.get("applicability_start") or row.get("valid_from"),
                "applicability_end": row.get("applicability_end") or row.get("valid_to"),
                "text_sha256": row.get("text_sha256"),
                "created_by_event_id": row.get("created_by_event_id"),
                "event_chain": row.get("event_chain", []),
                "snippet": _clean_text(text, 200),
            })
        return {
            "canonical_id": component_id,
            "target_work": resolved_work,
            "count": len(versions),
            "versions": versions,
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
        item: dict[str, Any] = {
            "canonical_id": normalized,
            "exists": False,
            "reason": reason,
        }
        # Fast path: for structured section/rule canonicals with materialized
        # version history, establish existence from version rows rather than
        # scanning the entire corpus XML (which takes ~35s on first call).
        # Returns None for forms/documents/unknown works -> slow path below.
        fast_exists = self._fast_exists(normalized)
        if fast_exists is not None:
            item["exists"] = fast_exists
            # Enrich with title/document_type only if the corpus index is
            # already built (free); never trigger the 35s scan for enrichment.
            if self._lookup is not None:
                self._enrich_candidate_from_lookup(item, normalized)
        else:
            lookup = self._corpus_lookup().get(normalized)
            item["exists"] = bool(lookup)
            if lookup:
                self._enrich_candidate_from_lookup(item, normalized)
        if search_result:
            item["search"] = search_result
        return item

    def _enrich_candidate_from_lookup(self, item: dict[str, Any], normalized: str) -> None:
        lookup = self._lookup.get(normalized) if self._lookup is not None else None
        if not lookup:
            return
        payload = lookup.get("provision") or lookup.get("document") or {}
        item.setdefault("title", payload.get("title", ""))
        item.setdefault("document_id", payload.get("document_id", payload.get("canonical_id", "")))
        item.setdefault("document_type", payload.get("document_type", ""))

    def _fast_exists(self, canonical_id: str) -> bool | None:
        """Fast existence check via materialized version rows.

        Returns True if version rows exist for this canonical, False if the
        canonical resolves to a version directory but has no rows, or None if
        the canonical is not a structured provision tracked by version history
        (forms, documents, unknown works) — in which case callers fall back to
        the full corpus lookup.

        This avoids the ~35-second ``build_corpus_lookup`` scan for structured
        section/rule queries, which are the common MCP acquisition path.
        """
        if not _STRUCTURED_PROVISION_RE.match(canonical_id):
            return None
        try:
            from .version_compare import read_node_versions
            resolved_dir, _ = self._version_dir_for_component(canonical_id, target_work=None)
            node_versions_path = resolved_dir / "node_versions.jsonl"
            if not node_versions_path.exists():
                return None
            rows = read_node_versions(node_versions_path)
            return any(row.get("component_id") == canonical_id for row in rows)
        except Exception:
            return None

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

    def _provision_lancedb_table(self) -> Any:
        if self._provision_lance_table is None:
            import lancedb

            self._provision_lance_table = lancedb.connect(str(self.lancedb_path)).open_table(
                self.provision_lancedb_table
            )
        return self._provision_lance_table

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

    def get_form_structure(self, form_id: str) -> dict[str, Any]:
        """Return the structural definition of a GST form."""
        from pathlib import Path
        import json as _json

        form_file = form_id.lower().strip()
        structure_path = Path("derived/form_structure") / f"{form_file}.json"
        if not structure_path.exists():
            return {"status": "not_found", "form_id": form_id}

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


__all__ = ["NyayaToolService"]
