# v1.1.0 - 2026-04-22 - Phase 7.x: GraphCapabilities pydantic model (plan Pilastro 5)
# v1.0.0 - 2026-04-14 - KG Fase 1b: ranker types (RankClassification, RankedNeighbor)
"""Graph ranker types + capabilities model.

Ranker types are TypedDict / Literal (no pydantic) because the ranker output
is passed through FastAPI as a plain dict — adding pydantic here would force
a second validation pass on every response without any behavioural gain.

GraphCapabilities is pydantic (unlike the ranker types) because it is a fresh
response model with a fixed shape, not an echo of pre-existing dicts.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

# Classification buckets emitted by every ranker. Score -> bucket is derived
# via thresholds in graph_ranker.py (THRESHOLD_SUSPECT, THRESHOLD_LEGITIMATE).
RankClassification = Literal["suspect", "uncertain", "legitimate"]

# Ranker strategies supported by GET /api/v1/graph/neighbors/{node_id}?rank=...
# "none" = no ranking (back-compat with Fase 1a). "suspect_write" = Fase 1b.
# Future rankers land in Fase 1f (graph_impact, etc.).
RankType = Literal["none", "suspect_write"]


class RankedNeighbor(TypedDict):
    """Neighbor dict augmented with ranker fields.

    Carries every key produced by graph_service.get_neighbors() plus:
      - score: 0.0–1.0, higher = more suspect for this ranker
      - classification: derived bucket (see thresholds)
      - signals: granular sub-scores for debug / audit trail
    """

    # Fields from graph_service.get_neighbors (echoed verbatim):
    id: str
    type: str
    name: str
    qualified_name: str
    file_path: str | None
    line_number: int | None
    metadata: dict[str, Any]
    edge: dict[str, Any]
    # Ranker-added fields:
    score: float
    classification: RankClassification
    signals: dict[str, Any]


class GraphCapabilities(BaseModel):
    """KG schema metadata per agent discovery (Phase 7.x, plan Pilastro 5).

    Returned by `GET /api/v1/graph/capabilities`. Lets agents discover valid
    `edge_types` / `node_kinds` / `node_prefixes` at runtime instead of hardcoding
    (prevents the resolves_to sync bug seen in sessione 157).

    Note: mcp_version omitted intentionally (deepen section 9: no fingerprinting).
    Only schema_version exposed, which is needed for drift detection.
    """

    edge_types: list[str] = Field(description="Valid relation values for edge_types param")
    node_kinds: list[str] = Field(description="Valid kind segment in node_id (function|file|module|artifact)")
    node_prefixes: list[str] = Field(description="Valid prefix segment in node_id (py|ts|task|...)")
    schema_version: int | None = Field(description="Current migration version (for drift detection)")
