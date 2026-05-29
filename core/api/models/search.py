# v1.3.0 - 2026-04-16 - KG Phase 6.5 A: hybrid search (edge_path, rrf_score, suggested_next_tool)
# v1.2.0 - 2026-04-12 - Add learning, inbox_item, audit doc_types
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SearchHit(BaseModel):
    doc_type: Literal["task", "project", "file", "handoff", "learning", "inbox_item", "audit"]
    doc_id: str
    title: str
    project: str
    score: float
    salience: float = 0.5
    path: str | None = None
    status: str | None = None
    # Phase 6.5 A: hybrid-search enrichment.
    # edge_path — structured list of graph node IDs along the rationale chain
    #   that brought this hit in via the KG retriever (None if hit was purely
    #   semantic). Agent-native: the last entry is trivially usable in
    #   graph_context(node_id=edge_path[-1]).
    # edge_path_summary — short human/LLM label: "handoff -> task -> learning".
    # rrf_score — fused RRF score (None when hybrid=False / semantic-only path).
    edge_path: list[str] | None = None
    edge_path_summary: str | None = None
    rrf_score: float | None = None


class SearchResponse(BaseModel):
    tasks: list[SearchHit] = []
    projects: list[SearchHit] = []
    files: list[SearchHit] = []
    handoffs: list[SearchHit] = []
    learnings: list[SearchHit] = []
    inbox_items: list[SearchHit] = []
    audits: list[SearchHit] = []
    total: int = 0
    query: str = ""
    # Phase 6.5 A: suggestion for agents when no results (empty result guidance).
    suggested_next_tool: list[str] | None = None
