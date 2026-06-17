# v1.0.0 - 2026-04-16 - KG lens Pydantic bundle models (Phase 7.0 Commit 3)
from __future__ import annotations

from pydantic import BaseModel


class KgNeighbor(BaseModel):
    id: str
    type: str
    name: str
    qualified_name: str | None = None
    file_path: str | None = None
    project_id: str | None = None
    relation: str
    confidence: float | None = None
    rank_score: float | None = None


class KgChainItem(BaseModel):
    id: str
    type: str
    name: str
    project_id: str | None = None
    relation: str


class KgContextMeta(BaseModel):
    ranker_version: str
    item_count: int
    truncated: bool
    errors: list[dict] = []
    deep_effective: bool = False     # actual deep value used (after env resolution)
    deep_default_source: str = "client"  # "client" | "env" | "default"


class KgContextBundle(BaseModel):
    node_id: str
    neighbors: list[KgNeighbor] = []
    context_chain: list[KgChainItem] = []
    applicable_learnings: list[dict] = []
    meta: KgContextMeta
