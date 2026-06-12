# v1.0.0 - 2026-05-16 - KG PR-Impact sub-02 MVP: REST surface
"""Read-side REST endpoints for the PR-impact pipeline.

Three endpoints all mounted under `/api/v1/graph`:

- GET /pr-impact/{pr_id}    — full bundle for one PR
- GET /branches             — branch tree with open-PR rollup
- GET /conflicts            — multi-PR shared-function detection

We deliberately keep this thin: every interesting query lives in
`api/services/kg/pr_impact.py`. The router just validates input, fetches
the data, and lets Pydantic shape the response.

MVP defers:
- HMAC-signed cursors (offset for now)
- WebSocket `pr_changed` (sub-03 frontend wave)
- transitive BFS depth > 1
"""
from __future__ import annotations

import logging
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.config import settings
from core.api.db import get_db
from core.api.models.graph_pr_impact import (
    BranchesResponse,
    CodexFunctionItem,
    CodexFunctionsResponse,
    CodexModuleEdgeItem,
    CodexModuleItem,
    CodexModulesResponse,
    ConflictsResponse,
    PrArtifactId,
    PrImpactResponse,
    SemanticModulesResponse,
    VisibilityFooter,
)
from core.api.security import get_current_user_or_agent
from core.api.services.kg import pr_impact as pr_impact_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/graph", tags=["graph", "pr-impact"])


@router.get(
    "/pr-impact/{pr_id}",
    response_model=PrImpactResponse,
    summary="KG PR impact bundle (modified + transitive impact)",
)
async def graph_pr_impact(
    pr_id: PrArtifactId,
    depth: Annotated[int, Query(ge=0, le=4)] = 1,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_all: Annotated[bool, Query()] = False,
    db: aiosqlite.Connection = Depends(get_db),
    _=Depends(get_current_user_or_agent),
) -> PrImpactResponse:
    """Return the full impact bundle for one PR.

    Returns 404 when the PR isn't registered. For PRs awaiting the
    populator the response shape stays valid — `modified_functions` is
    empty and `pr_metadata.populator_status` carries the state.
    """
    pr_row_id, pr_metadata = await pr_impact_service.get_pr_metadata(db, pr_id)
    if pr_row_id is None or pr_metadata is None:
        raise HTTPException(status_code=404, detail="PR not found")

    cap = getattr(settings, "function_cap_default", 800)
    items, total = await pr_impact_service.list_modified_functions(
        db,
        pr_row_id,
        offset=offset,
        limit=limit,
        cap=cap,
        include_all=include_all,
    )

    transitive = (
        await pr_impact_service.list_transitive_impact(db, pr_row_id, depth=depth)
        if depth >= 1
        else []
    )

    pr_metadata = pr_metadata.model_copy(
        update={
            "function_nodes_returned": len(items),
            "function_nodes_capped": (total > cap) and not include_all,
            "function_cap_threshold": cap,
        }
    )

    next_offset = offset + len(items) if (offset + len(items)) < total else None

    return PrImpactResponse(
        pr_id=pr_id,
        pr_metadata=pr_metadata,
        modified_functions=items,
        transitive_impact=transitive,
        involved_projects=[pr_metadata.branch.split("/")[0]] if pr_metadata.branch else [],
        visibility=VisibilityFooter(redacted_count=0),
        next_offset=next_offset,
        total_estimate=total,
    )


@router.get(
    "/branches",
    response_model=BranchesResponse,
    summary="Active/stale branches with their open-PR rollup",
)
async def graph_branches(
    state: Annotated[str, Query(pattern=r"^(active|stale|all)$")] = "active",
    project: Annotated[str | None, Query(max_length=64)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    db: aiosqlite.Connection = Depends(get_db),
    _=Depends(get_current_user_or_agent),
) -> BranchesResponse:
    stale_days = getattr(settings, "kg_branch_stale_days", 30)
    items, total = await pr_impact_service.list_branches(
        db,
        state=state,
        project=project,
        stale_days=stale_days,
        offset=offset,
        limit=limit,
    )

    main_item = next((b for b in items if b.is_main), None)

    return BranchesResponse(
        branches=items,
        main_head=main_item.head_sha if main_item else None,
        main_head_at=main_item.head_commit_at if main_item else None,
        next_offset=(offset + len(items)) if (offset + len(items)) < total else None,
        total_estimate=total,
    )


@router.get(
    "/conflicts",
    response_model=ConflictsResponse,
    summary="Detect shared touched functions across 2-5 PRs",
)
async def graph_conflicts(
    pr_ids: Annotated[
        list[str],
        Query(
            min_length=2,
            max_length=5,
            description="2-5 PR task UUIDs (with or without pr:artifact: prefix)",
        ),
    ],
    project: Annotated[str | None, Query(max_length=64)] = None,
    db: aiosqlite.Connection = Depends(get_db),
    _=Depends(get_current_user_or_agent),
) -> ConflictsResponse:
    # Normalize to bare UUIDs so the SQL accepts either form.
    cleaned = [_strip_pr_prefix(p) for p in pr_ids]
    conflicts = await pr_impact_service.find_conflicts(db, cleaned, project=project)
    # Echo the inputs back as canonical pr:artifact:<uuid> for the response.
    canonical_inputs = [f"pr:artifact:{p}" for p in cleaned]
    return ConflictsResponse(
        conflicts=conflicts,
        pr_ids_examined=canonical_inputs,
        total=len(conflicts),
    )


def _strip_pr_prefix(pr_id: str) -> str:
    """Accept either 'pr:artifact:<uuid>' or bare UUID — return bare UUID."""
    return pr_id.removeprefix("pr:artifact:")


# --- /semantic-modules — sub-04 dormant endpoint --------------------------
#
# Brain v1 sub-03 Memory Ops not yet production. We return an empty bundle
# with `backend_status='dormant'` so the consumer (sub-03 frontend) can
# no-op gracefully until cluster data lands. This is the
# architecture-strategist verdict from sub-04 §1: ship the substrate now,
# fill data in v1.1 when Brain ratification UX is live.


@router.get(
    "/semantic-modules",
    response_model=SemanticModulesResponse,
    summary="Brain-ratified semantic module clusters (DORMANT — Brain v1 sub-03 dep)",
)
async def graph_semantic_modules(
    project: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _=Depends(get_current_user_or_agent),
) -> SemanticModulesResponse:
    """Return Brain Memory-Op ratified semantic clusters.

    Sub-04 dormant implementation: while Brain v1 sub-03 Memory Ops is not
    in production we return an empty list with `backend_status='dormant'`
    so the consumer can detect the state without a 503. The schema and
    envelope stay stable so v1.1 (when ratification UX ships) just needs to
    replace the query, not the contract.
    """
    # Inputs are silently accepted but unused in v1 — keeps the upstream
    # frontend code stable across the dormant -> live transition.
    del project, cursor, limit
    return SemanticModulesResponse(
        semantic_modules=[],
        backend_status="dormant",
    )


# --------------------------------------------------------------------------
# Codex modules + functions (sub-03 zoom-levels)
# --------------------------------------------------------------------------


@router.get(
    "/codex-modules",
    response_model=CodexModulesResponse,
    summary="Semantic module planets for the Codex lens (macro view)",
)
async def graph_codex_modules(
    project: Annotated[str, Query(max_length=64)] = "marvisx",
    limit: Annotated[int, Query(ge=1, le=64)] = 24,
    db: aiosqlite.Connection = Depends(get_db),
    _=Depends(get_current_user_or_agent),
) -> CodexModulesResponse:
    """Return the top-N modules for the Codex lens macro view.

    The list is the seed for the default Codex canvas: every module
    becomes a planet sized by `function_count` and colored by its
    semantic `cluster` (auth / db / api / ui / parse / search / graph
    / shared). Brain v1 sub-03 will replace this path-heuristic with
    Memory-Op ratified cluster names.
    """
    modules, edges = await pr_impact_service.list_codex_modules_with_edges(
        db, project=project, limit=limit
    )
    items = [
        CodexModuleItem(
            slug=m.slug,
            cluster=m.cluster,
            label=m.label,
            function_count=m.function_count,
            file_count=m.file_count,
            degree=m.degree,
            top_functions=m.top_functions,
            top_paths=m.top_paths,
            semantic_label=m.semantic_label,
            ratified=m.ratified,
            drift=m.drift,
        )
        for m in modules
    ]
    edge_items = [
        CodexModuleEdgeItem(
            source=e.source,
            target=e.target,
            relation=e.relation,
            weight=e.weight,
            hot=e.hot,
        )
        for e in edges
    ]
    return CodexModulesResponse(
        modules=items,
        edges=edge_items,
        project=project,
        total_estimate=len(items),
    )


@router.get(
    "/codex-functions",
    response_model=CodexFunctionsResponse,
    summary="Functions inside one Codex module (zoom-in view)",
)
async def graph_codex_functions(
    module: Annotated[str, Query(min_length=1, max_length=128)],
    project: Annotated[str, Query(max_length=64)] = "marvisx",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    db: aiosqlite.Connection = Depends(get_db),
    _=Depends(get_current_user_or_agent),
) -> CodexFunctionsResponse:
    """Zoom-in view: list the functions whose path starts with `module`.

    Ordered by `touch_count_7d` DESC so hot files surface first. Used
    when the user clicks a module planet on the macro canvas.
    """
    fns = await pr_impact_service.list_codex_functions(
        db, project=project, module=module, limit=limit
    )
    items = [
        CodexFunctionItem(
            node_id=f.node_id,
            qualified_name=f.qualified_name,
            file_path=f.file_path,
            line_number=f.line_number,
            touch_count_7d=f.touch_count_7d,
            touch_count_30d=f.touch_count_30d,
        )
        for f in fns
    ]
    return CodexFunctionsResponse(
        functions=items,
        project=project,
        module=module,
        total_estimate=len(items),
    )
