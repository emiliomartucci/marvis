# v1.3.0 - 2026-04-16 - KG Phase 6.5 A: hybrid search (edge_path, rrf_score, suggested_next_tool)
# v1.2.0 - 2026-04-12 - Add learning, inbox_item, audit doc_types
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, WithJsonSchema

SEMANTIC_REASON_VALUES = (
    "model-not-loadable",
    "vec0-not-loadable",
    "runtime-gate",
    "index-building",
    "semantic-timeout",
)
_SEMANTIC_REASON_SET = frozenset(SEMANTIC_REASON_VALUES)
_INTERNAL_TIMEOUT_REASON = "semantic-timeout"


def _normalize_semantic_reason(value: object) -> str:
    """Keep the client schema open while preserving the N-1 wire values."""
    if value == _INTERNAL_TIMEOUT_REASON:
        return "runtime-gate"
    if not isinstance(value, str) or value not in _SEMANTIC_REASON_SET:
        raise ValueError("unsupported semantic reason")
    return value


SemanticReason = Annotated[
    str,
    BeforeValidator(_normalize_semantic_reason),
    WithJsonSchema(
        {
            "type": "string",
            "x-extensible-enum": list(SEMANTIC_REASON_VALUES),
        }
    ),
]


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
    # Current authority route when a historical span names a retired Marvis
    # repository-lifecycle tool. Kept separate so span line numbers stay exact.
    authority_notice: str | None = None
    # Memory-freshness v2a Phase 2 (A-span, MARVIS_SEARCH_SPANS): the winning
    # chunk's text expanded to line boundaries ±12 lines, so the agent answers
    # FROM the result without a follow-up Read. None when the flag is off, the
    # hit is row-backed (task/learning/inbox), or the source file is gone.
    span_text: str | None = None
    span_path: str | None = None
    span_line_start: int | None = None
    span_line_end: int | None = None


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
    # F1 (OSS pre-client): semantic-search availability, surfaced ALWAYS (not only
    # on empty results). On a clean install where the embedding model / vec0 fail
    # to load, a meaning query must NOT silently return keyword-only hits. None on
    # the legacy semantic-only path (meta=None). `semantic_reason` is a SANITIZED
    # enum — never the raw load-error string, which could leak a path/backend name.
    semantic_available: bool | None = None
    # `index-building` (U5): the index was empty and a self-healing background
    # build was just kicked off — the meaning index is not ready YET but is being
    # built; a retry shortly will succeed.
    # A bounded semantic timeout remains distinct in server logs/metadata but is
    # serialized as `runtime-gate`: that value was already accepted by N-1
    # clients, so a degraded lane cannot break an otherwise useful response.
    semantic_reason: SemanticReason | None = None
