"""Agent-native docs governance endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.services.docs_governance.triage_orchestrator import triage_docs_change

router = APIRouter(prefix="/api/v1/docs_governance", tags=["docs-governance"])

DocsLayer = Literal[
    "api",
    "mcp",
    "llm-gateway",
    "kg",
    "code-examples",
    "narrative",
    "concept",
]


class TriageDocsRequest(BaseModel):
    diff_text: str = Field(min_length=1)
    layer: DocsLayer
    change_type: str = Field(min_length=1, max_length=100)
    context: dict[str, Any] = Field(default_factory=dict)
    llm_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    kg_consistency: float = Field(default=1.0, ge=0.0, le=1.0)
    kg_stale: bool = False
    corpus_state: str = "ready"


class DocsGovernanceTriageResponse(BaseModel):
    decision: str
    layer: str
    score: float
    pr_label: str
    opens_pr_draft: bool
    confidence: dict[str, Any]
    hard_gates: list[dict[str, Any]]
    enrichment_markdown: str


@router.post("/triage", response_model=DocsGovernanceTriageResponse)
async def triage_docs(
    req: TriageDocsRequest,
    _user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> DocsGovernanceTriageResponse:
    result = await triage_docs_change(
        layer=req.layer,
        change_type=req.change_type,
        diff_text=req.diff_text,
        context=req.context,
        llm_confidence=req.llm_confidence,
        kg_consistency=req.kg_consistency,
        kg_stale=req.kg_stale,
        corpus_state=req.corpus_state,
    )
    return DocsGovernanceTriageResponse(**result.as_json())
