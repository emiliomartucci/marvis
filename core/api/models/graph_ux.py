# v1.0.0 - 2026-04-17 - KG UX endpoints: Pydantic response models for P2
"""Pydantic v2 response models for KG /graph UX endpoints (P2).

Covers: pins, landing bundle, overview, orphans, resolve.

Schema note: graph_nodes DB column is `type` (not `kind`). These models use
`kind` as the public API field name (semantic discriminator) and set
`Field(..., alias="type")` + `populate_by_name=True` where DB rows are
hydrated directly. Callers that build dicts explicitly can set `kind=` directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared type alias (validated at model-field level via annotation)
# ---------------------------------------------------------------------------
# NodeIdStr is used as type annotation in models that accept node_id input.
# Pattern: {prefix}:{kind}:{slug}  e.g. py:function:api.db.get_db
NodeIdStr = str  # runtime annotation; Pydantic Field(pattern=...) used inline


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

class PinIn(BaseModel):
    """Request body for POST /graph/pins."""
    node_id: str = Field(
        ...,
        min_length=6,
        max_length=256,
        pattern=r"^[a-z]+:[a-z]+:.+$",
        description="Node ID in format prefix:kind:slug",
    )
    note: str | None = Field(None, max_length=500)


class PinOut(BaseModel):
    """Response from GET/POST /graph/pins."""
    node_id: str
    pinned_at: datetime  # UTC-aware, parsed via parse_db_datetime
    note: str | None = None
    is_stale: bool = False  # True if node was soft-deleted after pinning (belt-and-braces)


# ---------------------------------------------------------------------------
# Landing bundle
# ---------------------------------------------------------------------------

class HotspotItem(BaseModel):
    node_id: str
    label: str
    kind: str
    touch_count: int
    authors: list[str]


class RecentItem(BaseModel):
    kind: Literal["commit", "pr", "task", "handoff"]
    node_id: str
    label: str
    at: datetime


class LandingBundle(BaseModel):
    hotspots: list[HotspotItem]
    recent: list[RecentItem]
    saved_nodes: list[PinOut]


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class OverviewNode(BaseModel):
    """A node in the macro/module overview graph.

    `kind` is the public field; it maps to column `type` in graph_nodes.
    Use `model_config = ConfigDict(populate_by_name=True)` so rows hydrated
    with key `type` are accepted when using the `alias`.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str
    kind: str = Field(..., alias="type", description="Node type from graph_nodes.type")
    label: str
    sub_nodes: int | None = None
    metadata: dict = Field(default_factory=dict)


class OverviewEdge(BaseModel):
    source: str
    target: str
    relation: str
    weight: int = 1


class OverviewBundle(BaseModel):
    level: Literal["macro", "module"]
    scope: str | None = None
    nodes: list[OverviewNode]
    edges: list[OverviewEdge]
    hidden_cross_project_count: int = 0


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------

class OrphanFile(BaseModel):
    node_id: str
    label: str
    path: str
    last_modified: datetime | None = None


class OrphanSubCluster(BaseModel):
    folder: str
    color: str
    count: int
    files: list[OrphanFile]  # max 30 per sub-cluster
    overflow_count: int = 0


class OrphansBundle(BaseModel):
    scope: str
    sub_clusters: list[OrphanSubCluster]


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

class ResolveOut(BaseModel):
    """Response from GET /graph/resolve."""
    node_id: str
    kind: str
