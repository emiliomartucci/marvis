# v1.0.0 - 2026-05-16 - KG PR-Impact sub-02 MVP: read-side Pydantic models
"""Pydantic v2 models for the PR-impact REST surface (sub-02).

Three endpoints share these types:
- GET /api/v1/graph/pr-impact/{pr_id}
- GET /api/v1/graph/branches
- GET /api/v1/graph/conflicts

The MVP defers HMAC cursor signing + transitive BFS depth>1 to v1.1 — we
ship simple offset pagination and depth-1 transitive lookups to keep this
PR reviewable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


# --- Shared type aliases ---------------------------------------------------

PrArtifactId = Annotated[
    str,
    Field(
        pattern=r"^pr:artifact:[0-9a-f-]{36}$",
        max_length=64,
        description="Canonical PR node id, e.g. pr:artifact:9b2309e0-ed7d-4963-985a-e26e36837468",
    ),
]
"""Strict regex so callers can pass either path or query parameters safely."""

FunctionNodeId = Annotated[
    str,
    Field(
        pattern=r"^(py|ts):function:[A-Za-z0-9_./-]+$",
        max_length=512,
    ),
]

CommitShaStr = Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$", max_length=40)]

TouchKind = Literal["add", "modify", "delete"]
ReviewState = Literal["draft", "open", "merging", "merged", "closed"]


# --- Internal base ---------------------------------------------------------


class PrImpactBaseModel(BaseModel):
    """Strict-by-default base — rejects unknown fields so the API contract
    can't silently drift between sub-01 (producer) and sub-02 (consumer)."""

    model_config = ConfigDict(extra="forbid")


# --- /pr-impact response ---------------------------------------------------


class PrMetadata(PrImpactBaseModel):
    title: str | None
    branch: str
    review_state: ReviewState
    head_sha: CommitShaStr | None = None
    base_sha: str = "main"
    populator_status: Literal["pending", "processed", "failed", "unknown"] = "unknown"
    function_nodes_returned: int = Field(ge=0)
    function_cap_threshold: int = Field(ge=1, default=800)
    function_nodes_capped: bool = False


class ModifiedFunctionItem(PrImpactBaseModel):
    node_id: str = Field(max_length=512)
    qualified_name_snapshot: str = Field(max_length=512)
    source_file: str = Field(max_length=512)
    touch_kind: TouchKind
    lines_added: int = Field(ge=0)
    lines_removed: int = Field(ge=0)
    weight: float = Field(ge=0.0, le=1.0)
    blame_author: str | None = None
    node_missing: bool = False
    first_seen_at: datetime | None = None


class TransitiveImpactItem(PrImpactBaseModel):
    node_id: str = Field(max_length=512)
    depth: int = Field(ge=1, le=4)
    via_edge: Literal["calls", "imports", "defines"]


class VisibilityFooter(PrImpactBaseModel):
    redacted_count: int = Field(ge=0, default=0)


class PrImpactResponse(PrImpactBaseModel):
    pr_id: PrArtifactId
    pr_metadata: PrMetadata
    modified_functions: list[ModifiedFunctionItem]
    transitive_impact: list[TransitiveImpactItem]
    involved_projects: list[str]
    visibility: VisibilityFooter
    next_offset: int | None = None  # MVP: simple offset, HMAC cursor v1.1
    total_estimate: int = Field(ge=0)
    schema_version: Literal["1.0"] = "1.0"


# --- /branches response ----------------------------------------------------


class BranchItem(PrImpactBaseModel):
    name: str = Field(max_length=255)
    head_sha: CommitShaStr | None = None
    head_commit_at: datetime | None = None
    is_main: bool = False
    is_stale: bool = False
    open_pr_ids: list[PrArtifactId] = Field(default_factory=list)
    age_days: int | None = Field(ge=0, default=None)


class BranchesResponse(PrImpactBaseModel):
    branches: list[BranchItem]
    main_head: CommitShaStr | None = None
    main_head_at: datetime | None = None
    next_offset: int | None = None
    total_estimate: int = Field(ge=0)
    schema_version: Literal["1.0"] = "1.0"


# --- /conflicts response ---------------------------------------------------


class ConflictPair(PrImpactBaseModel):
    pr_ids: list[PrArtifactId] = Field(min_length=2)
    shared_function_id: str = Field(max_length=512)
    shared_qualified_name: str = Field(max_length=512)
    touch_kinds: list[TouchKind]


class ConflictsResponse(PrImpactBaseModel):
    conflicts: list[ConflictPair]
    pr_ids_examined: list[PrArtifactId]
    total: int = Field(ge=0)
    schema_version: Literal["1.0"] = "1.0"


# --- /semantic-modules response (sub-04 — dormant v1) ---------------------
# Brain v1 sub-03 Memory Ops not yet production. We ship the endpoint
# dormant per architecture-strategist verdict (sub-04 §1 #1): empty bundle
# + envelope matches Brain v1 sub-05 §5.1 shape so the consumer (sub-03
# frontend) can no-op gracefully until cluster data lands.


class SemanticModuleItem(PrImpactBaseModel):
    operation_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    cluster_name: Annotated[str, Field(max_length=120)]
    paths: list[str] = Field(default_factory=list)
    function_node_ids: list[str] = Field(default_factory=list)
    ratified_at: datetime | None = None
    ratified_by_display: str | None = None
    aliases: list[str] = Field(default_factory=list)


class SemanticModulesResponse(PrImpactBaseModel):
    semantic_modules: list[SemanticModuleItem]
    next_cursor: str | None = None
    total_estimate: int = Field(ge=0, default=0)
    redacted_count: int = Field(ge=0, default=0)
    redacted_evidence_count: int = Field(ge=0, default=0)
    cycle_key: str | None = None
    run_id: str | None = None
    as_of: datetime | None = None
    schema_version: Literal["1.0"] = "1.0"
    backend_status: Literal["dormant", "live", "degraded"] = "dormant"


# --- Codex modules + functions (sub-03 zoom-levels) -----------------------
#
# Macro view: planets = semantic modules grouped by path heuristic.
# Zoom-in: planets = functions of one module.

CodexClusterIdLiteral = Literal[
    "auth", "db", "api", "ui", "parse", "search", "graph", "shared"
]


class CodexModuleItem(PrImpactBaseModel):
    slug: Annotated[str, Field(max_length=128)]
    cluster: CodexClusterIdLiteral
    label: Annotated[str, Field(max_length=64)]
    function_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    degree: int = Field(ge=0, default=0)
    top_functions: list[str] = Field(default_factory=list, max_length=10)
    top_paths: list[str] = Field(default_factory=list, max_length=10)
    semantic_label: str | None = None
    ratified: bool = False
    drift: int = Field(ge=0, default=0)


class CodexModuleEdgeItem(PrImpactBaseModel):
    source: Annotated[str, Field(max_length=128)]
    target: Annotated[str, Field(max_length=128)]
    relation: Literal["calls", "imports", "depends_on", "mentions"]
    weight: int = Field(ge=0)
    hot: bool = False


class CodexModulesResponse(PrImpactBaseModel):
    modules: list[CodexModuleItem]
    edges: list[CodexModuleEdgeItem] = Field(default_factory=list)
    project: str
    total_estimate: int = Field(ge=0)
    schema_version: Literal["1.0"] = "1.0"


class CodexFunctionItem(PrImpactBaseModel):
    node_id: Annotated[str, Field(max_length=512)]
    qualified_name: Annotated[str, Field(max_length=512)]
    file_path: str | None = None
    line_number: int | None = Field(default=None, ge=0)
    touch_count_7d: int = Field(ge=0)
    touch_count_30d: int = Field(ge=0)


class CodexFunctionsResponse(PrImpactBaseModel):
    functions: list[CodexFunctionItem]
    project: str
    module: str
    total_estimate: int = Field(ge=0)
    schema_version: Literal["1.0"] = "1.0"


__all__ = [
    "PrArtifactId",
    "FunctionNodeId",
    "CommitShaStr",
    "TouchKind",
    "ReviewState",
    "PrImpactBaseModel",
    "PrMetadata",
    "ModifiedFunctionItem",
    "TransitiveImpactItem",
    "VisibilityFooter",
    "PrImpactResponse",
    "BranchItem",
    "BranchesResponse",
    "ConflictPair",
    "ConflictsResponse",
    "SemanticModuleItem",
    "SemanticModulesResponse",
    "CodexClusterIdLiteral",
    "CodexModuleItem",
    "CodexModuleEdgeItem",
    "CodexModulesResponse",
    "CodexFunctionItem",
    "CodexFunctionsResponse",
]
