#!/usr/bin/env python3
"""Run the Nyaya Ledger REST API over corpus, graph, and vector artifacts."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.legal_corpus.serving import NyayaToolService  # noqa: E402


TOOL_DESCRIPTIONS = {
    "lookup_provision": "Fetch exact text and provenance for a section, rule, form, document, or notification.",
    "semantic_search": "Search legal corpus chunks by meaning through LanceDB and the configured embedding endpoint.",
    "resolve_citation": "Convert a human citation into canonical corpus ID candidates.",
    "get_incoming_refs": "Find provisions that cite the requested canonical ID.",
    "get_outgoing_refs": "Find provisions cited by the requested canonical ID.",
    "trace_rule_to_act": "Trace a rule, form, or notification back to enabling Act sections.",
    "find_related_provisions": "Find graph-neighbor and semantic-neighbor provisions.",
    "explain_reference_path": "Show graph paths explaining why two provisions are connected.",
    "get_forms_for_rule": "Find GST forms prescribed or referenced by a rule.",
    "compare_versions": "Placeholder for future amended provision state comparison.",
}


class LookupRequest(BaseModel):
    canonical_id: str
    include_text: bool = True


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)
    document_type: str | None = None
    role: str | None = None


class ResolveCitationRequest(BaseModel):
    citation: str
    limit: int = Field(default=10, ge=1, le=100)


class RefRequest(BaseModel):
    canonical_id: str
    limit: int = Field(default=50, ge=1, le=500)


class TraceRequest(BaseModel):
    canonical_id: str
    max_depth: int = Field(default=3, ge=1, le=8)
    limit: int = Field(default=10, ge=1, le=100)


class RelatedRequest(BaseModel):
    canonical_id: str
    limit: int = Field(default=10, ge=1, le=100)


class ExplainPathRequest(BaseModel):
    source_id: str
    target_id: str
    max_depth: int = Field(default=4, ge=1, le=8)
    limit: int = Field(default=3, ge=1, le=20)


class CompareVersionsRequest(BaseModel):
    canonical_id: str
    from_date: str | None = None
    to_date: str | None = None


app = FastAPI(
    title="Nyaya Ledger API",
    description="REST API over Indian legal corpus XML, FalkorDB graph, and LanceDB vector artifacts.",
    version="0.1.0",
)

_SERVICE: NyayaToolService | None = None


def service() -> NyayaToolService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = NyayaToolService.from_env()
    return _SERVICE


@app.get("/")
def root() -> dict[str, Any]:
    return {"name": "nyaya-ledger", "tools": TOOL_DESCRIPTIONS}


@app.get("/health")
def health() -> dict[str, Any]:
    return service().health()


@app.get("/tools")
def tools() -> dict[str, Any]:
    return {"tools": TOOL_DESCRIPTIONS}


@app.post("/tools/lookup_provision")
def lookup_provision(request: LookupRequest) -> dict[str, Any]:
    return service().lookup_provision(request.canonical_id, include_text=request.include_text)


@app.post("/tools/semantic_search")
def semantic_search(request: SearchRequest) -> dict[str, Any]:
    return service().semantic_search(
        request.query,
        limit=request.limit,
        document_type=request.document_type,
        role=request.role,
    )


@app.post("/tools/resolve_citation")
def resolve_citation(request: ResolveCitationRequest) -> dict[str, Any]:
    return service().resolve_citation(request.citation, limit=request.limit)


@app.post("/tools/get_incoming_refs")
def get_incoming_refs(request: RefRequest) -> dict[str, Any]:
    return service().get_incoming_refs(request.canonical_id, limit=request.limit)


@app.post("/tools/get_outgoing_refs")
def get_outgoing_refs(request: RefRequest) -> dict[str, Any]:
    return service().get_outgoing_refs(request.canonical_id, limit=request.limit)


@app.post("/tools/trace_rule_to_act")
def trace_rule_to_act(request: TraceRequest) -> dict[str, Any]:
    return service().trace_rule_to_act(request.canonical_id, max_depth=request.max_depth, limit=request.limit)


@app.post("/tools/find_related_provisions")
def find_related_provisions(request: RelatedRequest) -> dict[str, Any]:
    return service().find_related_provisions(request.canonical_id, limit=request.limit)


@app.post("/tools/explain_reference_path")
def explain_reference_path(request: ExplainPathRequest) -> dict[str, Any]:
    return service().explain_reference_path(
        request.source_id,
        request.target_id,
        max_depth=request.max_depth,
        limit=request.limit,
    )


@app.post("/tools/get_forms_for_rule")
def get_forms_for_rule(request: RefRequest) -> dict[str, Any]:
    return service().get_forms_for_rule(request.canonical_id, limit=request.limit)


@app.post("/tools/compare_versions")
def compare_versions(request: CompareVersionsRequest) -> dict[str, Any]:
    return service().compare_versions(request.canonical_id, from_date=request.from_date, to_date=request.to_date)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("NYAYA_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NYAYA_API_PORT", "8080")))
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
