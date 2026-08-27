# v1.0.0 - 2026-08-05 - Plan 2 U1: graph ingest contract (grafo senza codice sul tenant)
"""Request/response models for the graph ingest endpoint.

The tenant receives a batch of graph nodes and edges parsed elsewhere (a local
client session, or the user's CI) plus a provenance block, and NEVER the source
code. These shapes mirror EXACTLY what ``core/scripts/ast_parser.py`` already
emits (``parse_python_file`` / ``parse_typescript_file`` return ``(nodes,
edges)`` as plain dicts), so a client that reuses the existing parser can POST
its output without any translation layer.

Structure is validated here; the authoritative gate on node ``type`` and edge
``relation`` values is the DB CHECK on graph_nodes/graph_edges (single source of
truth, no duplicated enum to drift — a rejected value rolls the whole atomic
batch back).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class GraphIngestProvenance(BaseModel):
    """How the batch was produced. Freshness is derived from this, never faked."""

    source: Literal["client-attested", "ci-signed"]
    commit_sha: str | None = Field(default=None, max_length=64)
    # True when the batch was parsed against a working tree with uncommitted
    # changes (only meaningful for client-attested; CI batches are never dirty).
    dirty: bool = False
    # ISO-8601 instant the client/CI generated the batch (its own clock).
    generated_at: str | None = Field(default=None, max_length=40)
    parser_version: str | None = Field(default=None, max_length=64)


class GraphIngestNode(BaseModel):
    """One graph node — mirrors ast_parser node dicts."""

    id: str = Field(min_length=3, max_length=512)
    type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=512)
    qualified_name: str | None = Field(default=None, max_length=1024)
    file_path: str | None = Field(default=None, max_length=1024)
    line_number: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        # {lang}:{type}:{qualified_name}. The DB CHECK is the authority on the
        # allowed {type}; here we only require the three-part shape so an edge's
        # source/target ids are well formed.
        if v.count(":") < 2:
            raise ValueError("node id must be '{lang}:{type}:{qualified_name}'")
        return v


class GraphIngestEdge(BaseModel):
    """One graph edge — mirrors ast_parser edge dicts."""

    source_id: str = Field(min_length=3, max_length=512)
    target_id: str = Field(min_length=3, max_length=512)
    relation: str = Field(min_length=1, max_length=32)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = Field(default="ast", max_length=32)
    source_file: str | None = Field(default=None, max_length=1024)
    source_line: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphIngestRequest(BaseModel):
    """A full graph generation for one project, replacing its prior graph atomically."""

    project: str = Field(min_length=1, max_length=64)
    provenance: GraphIngestProvenance
    # A real batch always carries at least one node. An empty node list would
    # wipe a project's graph, so it is rejected rather than silently accepted.
    nodes: list[GraphIngestNode] = Field(min_length=1)
    edges: list[GraphIngestEdge] = Field(default_factory=list)


class GraphProvenanceOut(BaseModel):
    """The recorded provenance for a project's active graph."""

    project: str
    source: str
    commit_sha: str | None
    dirty: bool
    parser_version: str | None
    node_count: int
    edge_count: int
    generated_at: str | None
    ingested_at: str
